import asyncio
import logging
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat

logging.basicConfig(
    format="[%(levelname)s %(asctime)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("promo")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"].strip()
STRING_SESSION = os.environ["STRING_SESSION"].strip()
SEND_INTERVAL = max(10, int(os.getenv("SEND_INTERVAL", "10")))
SEND_TIMEOUT = max(30, int(os.getenv("SEND_TIMEOUT", "60")))
MAX_FLOOD_WAIT = max(0, int(os.getenv("MAX_FLOOD_WAIT", "30")))
PROMO_TEXT_FILE = Path(os.getenv("PROMO_TEXT_FILE", "promo.txt"))
PROMO_IMAGE_FILE = Path(os.getenv("PROMO_IMAGE_FILE", "promo.jpg"))
TARGETS_FILE = Path(os.getenv("TARGETS_FILE", "targets.txt"))
PROMO1_MESSAGE_ID_FILE = Path(os.getenv("PROMO1_MESSAGE_ID_FILE", "promo1_message_id.txt"))
PROMO2_MESSAGE_ID_FILE = Path(os.getenv("PROMO2_MESSAGE_ID_FILE", "promo2_message_id.txt"))
AUTO_SEND_ENABLED_FILE = Path(os.getenv("AUTO_SEND_ENABLED_FILE", "auto_send_enabled.txt"))
SCHEDULE_FILE = Path(os.getenv("SCHEDULE_FILE", "schedule.txt"))
FAILED_TARGETS_FILE = Path(os.getenv("FAILED_TARGETS_FILE", "failed_targets.txt"))
NEXT_AUTO_RUN_FILE = Path(os.getenv("NEXT_AUTO_RUN_FILE", "next_auto_run.txt"))
RUN_STATE_FILE = Path(os.getenv("RUN_STATE_FILE", "run_state.json"))
LAST_AUTO_SLOT_FILE = Path(os.getenv("LAST_AUTO_SLOT_FILE", "last_auto_slot.txt"))
SCHEDULER_POLL_SECONDS = max(5, int(os.getenv("SCHEDULER_POLL_SECONDS", "10")))
SCHEDULE_GRACE_MINUTES = max(1, int(os.getenv("SCHEDULE_GRACE_MINUTES", "10")))
KST = ZoneInfo("Asia/Seoul")
AUTO_SEND_DEFAULT = os.getenv("AUTO_SEND_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_SCHEDULE = os.getenv("AUTO_SEND_SCHEDULE", "times:00:00,06:00,12:00,18:00").strip()

client = TelegramClient(
    StringSession(STRING_SESSION),
    API_ID,
    API_HASH,
    flood_sleep_threshold=MAX_FLOOD_WAIT,
)
send_task = None
stop_requested = False
waiting_for_promo = None
scheduler_task = None
progress_current = 0
progress_total = 0
progress_success = 0
progress_failed = 0
progress_started_at = None
last_run_status = "대기 중"
last_run_finished_at = None



def save_run_state(status: str, finished_at: datetime | None = None) -> None:
    global last_run_status, last_run_finished_at

    last_run_status = status
    if finished_at is not None:
        last_run_finished_at = finished_at

    data = {
        "status": last_run_status,
        "finished_at": (
            last_run_finished_at.isoformat()
            if last_run_finished_at is not None
            else None
        ),
        "progress_current": progress_current,
        "progress_total": progress_total,
        "progress_success": progress_success,
        "progress_failed": progress_failed,
    }

    try:
        RUN_STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("실행 상태 저장 실패: %s", exc)


def load_run_state() -> None:
    global last_run_status, last_run_finished_at
    global progress_current, progress_total, progress_success, progress_failed

    if not RUN_STATE_FILE.exists():
        return

    try:
        data = json.loads(RUN_STATE_FILE.read_text(encoding="utf-8"))
        last_run_status = str(data.get("status") or "대기 중")

        finished_text = data.get("finished_at")
        if finished_text:
            last_run_finished_at = datetime.fromisoformat(finished_text)

        progress_current = int(data.get("progress_current") or 0)
        progress_total = int(data.get("progress_total") or 0)
        progress_success = int(data.get("progress_success") or 0)
        progress_failed = int(data.get("progress_failed") or 0)
    except Exception as exc:
        logger.warning("실행 상태 불러오기 실패: %s", exc)


def write_next_auto_run(run_at: datetime) -> None:
    try:
        NEXT_AUTO_RUN_FILE.write_text(run_at.isoformat(), encoding="utf-8")
    except Exception as exc:
        logger.warning("다음 자동 발송 시간 저장 실패: %s", exc)


def read_next_auto_run() -> datetime | None:
    if not NEXT_AUTO_RUN_FILE.exists():
        return None

    try:
        value = NEXT_AUTO_RUN_FILE.read_text(encoding="utf-8").strip()
        if not value:
            return None
        run_at = datetime.fromisoformat(value)
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=KST)
        return run_at.astimezone(KST)
    except Exception as exc:
        logger.warning("다음 자동 발송 시간 불러오기 실패: %s", exc)
        return None


def calculate_and_store_next_auto_run(
    now: datetime | None = None,
    previous_run: datetime | None = None,
) -> datetime:
    now = now or datetime.now(KST)
    mode, value = parse_schedule(read_schedule())

    if mode == "interval":
        if previous_run is None:
            candidate = read_next_auto_run()

            if candidate is None:
                candidate = now + timedelta(hours=value)
            else:
                while candidate <= now:
                    candidate += timedelta(hours=value)
        else:
            candidate = previous_run + timedelta(hours=value)
            while candidate <= now:
                candidate += timedelta(hours=value)
    else:
        candidate = next_auto_run(now)

    write_next_auto_run(candidate)
    return candidate




def read_last_auto_slot() -> str:
    if not LAST_AUTO_SLOT_FILE.exists():
        return ""

    try:
        return LAST_AUTO_SLOT_FILE.read_text(encoding="utf-8").strip()
    except Exception as exc:
        logger.warning("마지막 자동 발송 회차 불러오기 실패: %s", exc)
        return ""


def write_last_auto_slot(slot_key: str) -> None:
    try:
        LAST_AUTO_SLOT_FILE.write_text(slot_key, encoding="utf-8")
    except Exception as exc:
        logger.warning("마지막 자동 발송 회차 저장 실패: %s", exc)


def scheduled_slot_due(now: datetime | None = None):
    """
    현재 시각에 실행해야 할 자동 발송 회차를 반환합니다.

    반환값:
    - 실행할 회차가 없으면 None
    - 실행할 회차가 있으면 (slot_datetime, slot_key, next_run)
    """
    now = now or datetime.now(KST)
    mode, value = parse_schedule(read_schedule())

    if mode == "interval":
        run_at = read_next_auto_run()
        if run_at is None:
            run_at = now + timedelta(hours=value)
            write_next_auto_run(run_at)
            return None

        if now < run_at:
            return None

        slot_key = f"interval:{run_at.isoformat()}"
        next_run = run_at + timedelta(hours=value)
        while next_run <= now:
            next_run += timedelta(hours=value)

        return run_at, slot_key, next_run

    grace = timedelta(minutes=SCHEDULE_GRACE_MINUTES)
    candidates = [
        now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for hour, minute in value
    ]
    due_candidates = [
        candidate
        for candidate in candidates
        if candidate <= now and now - candidate <= grace
    ]

    if not due_candidates:
        return None

    slot = max(due_candidates)
    slot_key = f"times:{slot.strftime('%Y-%m-%dT%H:%M')}"
    next_run = next_auto_run(now)
    return slot, slot_key, next_run


def scheduler_next_display(now: datetime | None = None) -> datetime:
    now = now or datetime.now(KST)
    mode, value = parse_schedule(read_schedule())

    if mode == "interval":
        saved = read_next_auto_run()
        if saved and saved > now:
            return saved

        candidate = now + timedelta(hours=value)
        write_next_auto_run(candidate)
        return candidate

    return next_auto_run(now)



def read_promo_text() -> str:
    env_text = os.getenv("PROMO_TEXT", "").strip()
    if env_text:
        return env_text
    if not PROMO_TEXT_FILE.exists():
        raise FileNotFoundError("promo.txt 또는 PROMO_TEXT가 없습니다.")
    text = PROMO_TEXT_FILE.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("홍보 문구가 비어 있습니다.")
    return text


def read_message_id(path: Path):
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return int(value) if value.isdigit() else None



def is_auto_send_enabled() -> bool:
    if AUTO_SEND_ENABLED_FILE.exists():
        value = AUTO_SEND_ENABLED_FILE.read_text(encoding="utf-8").strip().lower()
        return value in {"1", "true", "yes", "on"}
    return AUTO_SEND_DEFAULT


def set_auto_send_enabled(enabled: bool) -> None:
    AUTO_SEND_ENABLED_FILE.write_text("true" if enabled else "false", encoding="utf-8")


def read_schedule() -> str:
    if SCHEDULE_FILE.exists():
        value = SCHEDULE_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    return DEFAULT_SCHEDULE


def write_schedule(value: str) -> None:
    SCHEDULE_FILE.write_text(value, encoding="utf-8")


def parse_schedule(value: str):
    value = value.strip().lower()

    if value.startswith("interval:"):
        hours = int(value.split(":", 1)[1])
        if hours < 1 or hours > 168:
            raise ValueError("간격은 1시간~168시간 사이여야 합니다.")
        return ("interval", hours)

    if value.startswith("times:"):
        raw_times = [x.strip() for x in value.split(":", 1)[1].split(",") if x.strip()]
        parsed = []
        for item in raw_times:
            hour_text, minute_text = item.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("시간 형식이 올바르지 않습니다.")
            parsed.append((hour, minute))
        if not parsed:
            raise ValueError("예약 시간이 없습니다.")
        return ("times", sorted(set(parsed)))

    raise ValueError("예약 형식이 올바르지 않습니다.")


def schedule_description() -> str:
    mode, value = parse_schedule(read_schedule())
    if mode == "interval":
        return f"{value}시간마다"
    return ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in value) + " (KST)"


def next_auto_run(now: datetime | None = None) -> datetime:
    now = now or datetime.now(KST)
    mode, value = parse_schedule(read_schedule())

    if mode == "interval":
        return now + timedelta(hours=value)

    candidates = [
        now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for hour, minute in value
    ]
    for candidate in candidates:
        if candidate > now:
            return candidate

    tomorrow = now + timedelta(days=1)
    first_hour, first_minute = value[0]
    return tomorrow.replace(
        hour=first_hour, minute=first_minute, second=0, microsecond=0
    )


async def get_saved_promo_message(path: Path, label: str):
    message_id = read_message_id(path)
    if not message_id:
        return None
    message = await client.get_messages("me", ids=message_id)
    if not message:
        raise ValueError(f"{label} 메시지를 찾을 수 없습니다. 다시 저장하세요.")
    return message



async def send_text_promo(entity, message):
    text_value = message.message or ""
    if not text_value:
        raise ValueError("텍스트 전용 홍보 메시지에 문구가 없습니다.")
    await client.send_message(
        entity,
        text_value,
        formatting_entities=message.entities or None,
        link_preview=True,
    )
    return "text"


async def send_dual_promo(entity, promo1, promo2):
    """사진+텍스트를 먼저 보내고, 사진 제한 시 텍스트 전용 홍보로 전환합니다."""
    promo1_text = promo1.message or ""
    promo1_entities = promo1.entities or None

    if promo1.media:
        try:
            await client.send_file(
                entity,
                promo1.media,
                caption=promo1_text or None,
                formatting_entities=promo1_entities,
            )
            return "media"
        except FloodWaitError as media_exc:
            logger.warning(
                "사진 FloodWait %s초, 텍스트 전용 홍보로 즉시 전환",
                int(media_exc.seconds),
            )
            if promo2:
                await send_text_promo(entity, promo2)
            elif promo1_text:
                await client.send_message(
                    entity,
                    promo1_text,
                    formatting_entities=promo1_entities,
                    link_preview=True,
                )
            else:
                raise media_exc
            return "text-fallback"
        except RPCError as media_exc:
            logger.info("사진 전송 실패, 텍스트 전용 홍보로 전환: %s", media_exc)
            if promo2:
                await send_text_promo(entity, promo2)
            elif promo1_text:
                await client.send_message(
                    entity,
                    promo1_text,
                    formatting_entities=promo1_entities,
                    link_preview=True,
                )
            else:
                raise media_exc
            return "text-fallback"

    if promo2:
        return await send_text_promo(entity, promo2)

    if promo1_text:
        await client.send_message(
            entity,
            promo1_text,
            formatting_entities=promo1_entities,
            link_preview=True,
        )
        return "text"

    raise ValueError("저장된 홍보 메시지에 보낼 내용이 없습니다.")


async def send_to_entity(entity, promo1, promo2, text, image_exists):
    if promo1:
        return await send_dual_promo(entity, promo1, promo2)

    if image_exists:
        try:
            await client.send_file(entity, PROMO_IMAGE_FILE, caption=text)
            return "media"
        except FloodWaitError as media_exc:
            logger.warning(
                "사진 FloodWait %s초, promo.txt 텍스트로 즉시 전환",
                int(media_exc.seconds),
            )
            await client.send_message(entity, text, link_preview=True)
            return "text-fallback"
        except RPCError as media_exc:
            logger.info("사진 전송 실패, promo.txt 문구로 전환: %s", media_exc)
            await client.send_message(entity, text, link_preview=True)
            return "text-fallback"

    await client.send_message(entity, text, link_preview=True)
    return "text"

def read_targets() -> List[str]:
    env_targets = os.getenv("TARGETS", "").strip()
    if env_targets:
        return [x.strip() for x in env_targets.split(",") if x.strip()]
    if not TARGETS_FILE.exists():
        raise FileNotFoundError("targets.txt 또는 TARGETS가 없습니다.")

    targets: List[str] = []
    for raw in TARGETS_FILE.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            targets.append(value)

    if not targets:
        raise ValueError("발송 대상이 없습니다.")
    return targets


async def control_message(text: str):
    await client.send_message("me", text)


async def safe_control_message(text: str) -> bool:
    """알림 전송 실패가 발송 작업이나 자동 스케줄러를 멈추지 않도록 보호합니다."""
    try:
        await asyncio.wait_for(
            client.send_message("me", text),
            timeout=SEND_TIMEOUT,
        )
        return True
    except FloodWaitError as exc:
        logger.warning(
            "저장된 메시지 알림 FloodWait %s초 - 알림만 생략",
            int(exc.seconds),
        )
    except asyncio.TimeoutError:
        logger.warning("저장된 메시지 알림 시간 초과 - 알림만 생략")
    except Exception as exc:
        logger.exception("저장된 메시지 알림 실패 - 알림만 생략: %s", exc)

    return False


async def safe_send_failed_file(caption: str) -> bool:
    if not FAILED_TARGETS_FILE.exists():
        return False

    try:
        await asyncio.wait_for(
            client.send_file(
                "me",
                FAILED_TARGETS_FILE,
                caption=caption,
            ),
            timeout=SEND_TIMEOUT,
        )
        return True
    except FloodWaitError as exc:
        logger.warning(
            "실패 목록 파일 전송 FloodWait %s초 - 파일 전송만 생략",
            int(exc.seconds),
        )
    except asyncio.TimeoutError:
        logger.warning("실패 목록 파일 전송 시간 초과 - 파일 전송만 생략")
    except Exception as exc:
        logger.exception("실패 목록 파일 전송 실패 - 파일 전송만 생략: %s", exc)

    return False


async def save_uploaded_file(event) -> bool:
    """저장된 메시지에 올린 설정 파일을 Railway 실행 폴더에 저장합니다."""
    if not event.file:
        return False

    uploaded_name = (event.file.name or "").strip().lower()
    allowed = {
        "targets.txt": TARGETS_FILE,
        "promo.txt": PROMO_TEXT_FILE,
        "promo.jpg": PROMO_IMAGE_FILE,
        "promo.jpeg": PROMO_IMAGE_FILE,
    }

    destination = allowed.get(uploaded_name)
    if destination is None:
        return False

    temp_file = destination.with_name(f".{destination.name}.uploading")

    try:
        downloaded = await event.download_media(file=str(temp_file))
        if not downloaded or not temp_file.exists():
            raise RuntimeError("파일 다운로드에 실패했습니다.")

        if destination.suffix.lower() == ".txt":
            # UTF-8 텍스트인지 확인하고 BOM이 있으면 제거합니다.
            content = temp_file.read_text(encoding="utf-8-sig")
            destination.write_text(content, encoding="utf-8")
            temp_file.unlink(missing_ok=True)
        else:
            temp_file.replace(destination)

        if destination == TARGETS_FILE:
            try:
                count = len(read_targets())
                await event.respond(f"✅ targets.txt 업데이트 완료\n발송 대상: {count}개")
            except Exception as exc:
                await event.respond(f"⚠️ targets.txt는 저장됐지만 확인이 필요합니다.\n{exc}")
        elif destination == PROMO_TEXT_FILE:
            try:
                text_length = len(read_promo_text())
                await event.respond(f"✅ promo.txt 업데이트 완료\n문자 수: {text_length}자")
            except Exception as exc:
                await event.respond(f"⚠️ promo.txt는 저장됐지만 확인이 필요합니다.\n{exc}")
        else:
            await event.respond("✅ promo.jpg 업데이트 완료")

        logger.info("업로드 파일 저장 완료: %s", destination)
        return True

    except UnicodeDecodeError:
        temp_file.unlink(missing_ok=True)
        await event.respond("❌ 텍스트 파일은 UTF-8 형식으로 저장해서 다시 보내주세요.")
        return True
    except Exception as exc:
        temp_file.unlink(missing_ok=True)
        logger.exception("파일 업데이트 실패: %s", exc)
        await event.respond(f"❌ 파일 업데이트 실패\n{exc}")
        return True


async def scan_groups() -> Path:
    lines = [
        "# 홍보 게시가 허용된 그룹만 targets.txt에 복사하세요.",
        "# 형식: 숫자 ID 또는 @username",
        "",
    ]

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        is_group = isinstance(entity, Chat) or (
            isinstance(entity, Channel) and bool(getattr(entity, "megagroup", False))
        )
        if not is_group:
            continue

        username = getattr(entity, "username", None)
        target = f"@{username}" if username else str(entity.id)
        lines.append(f"{target}\t{dialog.name}")

    output = Path("groups_scan.txt")
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def save_failed_targets(targets: list[str]) -> None:
    FAILED_TARGETS_FILE.write_text(
        "\n".join(targets) + ("\n" if targets else ""),
        encoding="utf-8",
    )


def read_failed_targets() -> list[str]:
    if not FAILED_TARGETS_FILE.exists():
        raise FileNotFoundError("failed_targets.txt가 없습니다.")

    targets = []
    for raw in FAILED_TARGETS_FILE.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            targets.append(value)

    if not targets:
        raise ValueError("다시 보낼 실패 그룹이 없습니다.")

    return targets


async def send_promotions(targets_override=None, run_label="발송"):
    global stop_requested
    global progress_current, progress_total, progress_success, progress_failed
    global progress_started_at, last_run_status, last_run_finished_at

    stop_requested = False
    progress_current = 0
    progress_total = 0
    progress_success = 0
    progress_failed = 0
    progress_started_at = datetime.now(KST)
    failed_targets = []
    total = 0
    final_status = "오류 종료"

    save_run_state(f"{run_label} 준비 중")

    try:
        try:
            targets = (
                list(targets_override)
                if targets_override is not None
                else read_targets()
            )
            promo1 = await get_saved_promo_message(
                PROMO1_MESSAGE_ID_FILE,
                "사진+텍스트 홍보",
            )
            promo2 = await get_saved_promo_message(
                PROMO2_MESSAGE_ID_FILE,
                "텍스트 전용 홍보",
            )
            text_value = None if promo1 else read_promo_text()
        except Exception as exc:
            final_status = "설정 오류"
            logger.exception("%s 설정 오류: %s", run_label, exc)
            await safe_control_message(f"❌ 설정 오류\n{exc}")
            return

        image_exists = PROMO_IMAGE_FILE.exists() and promo1 is None
        total = len(targets)
        progress_total = total
        save_run_state(f"{run_label} 진행 중")

        await safe_control_message(
            f"🚀 {run_label} 시작\n"
            f"대상: {total}개\n"
            f"간격: {SEND_INTERVAL}초\n"
            f"홍보 방식: "
            f"{'사진+텍스트 / 텍스트 자동 구분' if promo1 else ('이미지+문구' if image_exists else '문구')}"
        )

        for index, target in enumerate(targets, start=1):
            if stop_requested:
                final_status = "사용자 중지"
                break

            try:
                key = int(target) if target.lstrip("-").isdigit() else target
                entity = await asyncio.wait_for(
                    client.get_entity(key),
                    timeout=SEND_TIMEOUT,
                )

                mode = await asyncio.wait_for(
                    send_to_entity(
                        entity,
                        promo1,
                        promo2,
                        text_value,
                        image_exists,
                    ),
                    timeout=SEND_TIMEOUT,
                )
                progress_success += 1
                logger.info(
                    "[%s/%s] 성공: %s (%s)",
                    index,
                    total,
                    target,
                    mode,
                )

            except FloodWaitError as exc:
                wait_seconds = int(exc.seconds)

                if wait_seconds <= MAX_FLOOD_WAIT:
                    logger.warning(
                        "[%s/%s] 짧은 FloodWait %s초: %s",
                        index,
                        total,
                        wait_seconds,
                        target,
                    )
                    await asyncio.sleep(wait_seconds + 1)

                    try:
                        key = (
                            int(target)
                            if target.lstrip("-").isdigit()
                            else target
                        )
                        entity = await asyncio.wait_for(
                            client.get_entity(key),
                            timeout=SEND_TIMEOUT,
                        )
                        mode = await asyncio.wait_for(
                            send_to_entity(
                                entity,
                                promo1,
                                promo2,
                                text_value,
                                image_exists,
                            ),
                            timeout=SEND_TIMEOUT,
                        )
                        progress_success += 1
                        logger.info(
                            "[%s/%s] 재시도 성공: %s (%s)",
                            index,
                            total,
                            target,
                            mode,
                        )
                    except Exception as retry_exc:
                        progress_failed += 1
                        failed_targets.append(target)
                        logger.exception(
                            "[%s/%s] 재시도 실패 %s: %s",
                            index,
                            total,
                            target,
                            retry_exc,
                        )
                else:
                    progress_failed += 1
                    failed_targets.append(target)
                    logger.warning(
                        "[%s/%s] 긴 FloodWait %s초로 건너뜀: %s",
                        index,
                        total,
                        wait_seconds,
                        target,
                    )

            except asyncio.TimeoutError:
                progress_failed += 1
                failed_targets.append(target)
                logger.warning(
                    "[%s/%s] 시간 초과 %s: %s초 안에 응답 없음",
                    index,
                    total,
                    target,
                    SEND_TIMEOUT,
                )

            except (RPCError, ValueError, TypeError) as exc:
                progress_failed += 1
                failed_targets.append(target)
                logger.warning(
                    "[%s/%s] 실패 %s: %s",
                    index,
                    total,
                    target,
                    exc,
                )

            except Exception as exc:
                progress_failed += 1
                failed_targets.append(target)
                logger.exception(
                    "[%s/%s] 예외 %s: %s",
                    index,
                    total,
                    target,
                    exc,
                )

            finally:
                progress_current = index
                save_failed_targets(failed_targets)
                save_run_state(f"{run_label} 진행 중")

            if index < total and not stop_requested:
                await asyncio.sleep(SEND_INTERVAL)

        if stop_requested:
            final_status = "사용자 중지"
        else:
            final_status = "완료"

    except asyncio.CancelledError:
        final_status = "배포 또는 재시작으로 중단"
        logger.warning("%s 작업이 취소되었습니다.", run_label)
        raise

    except Exception as exc:
        final_status = "예상하지 못한 오류 종료"
        logger.exception("%s 전체 작업 오류: %s", run_label, exc)

    finally:
        finished_at = datetime.now(KST)
        last_run_finished_at = finished_at

        if progress_started_at:
            elapsed = finished_at - progress_started_at
            elapsed_minutes = int(elapsed.total_seconds() // 60)
            elapsed_seconds = int(elapsed.total_seconds() % 60)
        else:
            elapsed_minutes = 0
            elapsed_seconds = 0

        save_failed_targets(failed_targets)
        save_run_state(final_status, finished_at=finished_at)

        logger.info(
            "%s 종료 - 상태:%s 성공:%s 실패:%s 전체:%s 소요:%s분 %s초",
            run_label,
            final_status,
            progress_success,
            progress_failed,
            total,
            elapsed_minutes,
            elapsed_seconds,
        )

        if final_status == "완료":
            result_title = f"✅ {run_label} 완료"
        elif final_status == "사용자 중지":
            result_title = f"⛔ {run_label} 중지"
        else:
            result_title = f"⚠️ {run_label} 종료"

        await safe_control_message(
            f"{result_title}\n"
            f"상태: {final_status}\n"
            f"성공: {progress_success}개\n"
            f"실패: {progress_failed}개\n"
            f"처리: {progress_current}/{total}\n"
            f"소요 시간: {elapsed_minutes}분 {elapsed_seconds}초"
        )

        if failed_targets:
            await safe_send_failed_file(
                f"{run_label} 후에도 실패한 그룹 "
                f"{len(failed_targets)}개 목록입니다."
            )


async def automatic_scheduler():
    global send_task

    logger.info(
        "자동 스케줄러 시작 - 확인 간격:%s초, 예약 유예:%s분",
        SCHEDULER_POLL_SECONDS,
        SCHEDULE_GRACE_MINUTES,
    )

    last_logged_next = None

    while True:
        try:
            if not is_auto_send_enabled():
                last_logged_next = None
                await asyncio.sleep(SCHEDULER_POLL_SECONDS)
                continue

            now = datetime.now(KST)
            next_display = scheduler_next_display(now)
            next_key = next_display.strftime("%Y-%m-%d %H:%M")

            if next_key != last_logged_next:
                logger.info(
                    "다음 자동 발송: %s",
                    next_display.strftime("%Y-%m-%d %H:%M KST"),
                )
                last_logged_next = next_key

            due = scheduled_slot_due(now)
            if due is None:
                await asyncio.sleep(SCHEDULER_POLL_SECONDS)
                continue

            slot_time, slot_key, next_run = due

            # 이미 실제로 시작한 회차만 중복 방지합니다.
            if read_last_auto_slot() == slot_key:
                await asyncio.sleep(SCHEDULER_POLL_SECONDS)
                continue

            # 기존 발송 중이면 예약을 지우거나 다음 시간으로 넘기지 않습니다.
            # 작업이 끝난 뒤 같은 예약 회차를 다시 실행합니다.
            if send_task and not send_task.done():
                logger.warning(
                    "예약 회차 %s 대기 중: 기존 발송 종료 후 자동 시작합니다.",
                    slot_time.strftime("%Y-%m-%d %H:%M KST"),
                )
                await asyncio.sleep(SCHEDULER_POLL_SECONDS)
                continue

            logger.info(
                "자동 발송 회차 실행: %s",
                slot_time.strftime("%Y-%m-%d %H:%M KST"),
            )

            # 가장 먼저 실제 발송 작업을 생성합니다.
            # 알림 실패나 Railway 순간 오류 때문에 예약만 다음 시간으로 넘어가는 것을 막습니다.
            new_task = asyncio.create_task(
                send_promotions(run_label="자동 발송")
            )
            send_task = new_task

            # 작업 생성이 성공한 후에만 이번 회차 완료 표시와 다음 예약을 저장합니다.
            write_last_auto_slot(slot_key)

            mode, value = parse_schedule(read_schedule())
            if mode == "interval":
                write_next_auto_run(next_run)

            last_logged_next = None

            # 시작 알림은 발송 작업과 분리해 알림 오류가 실제 발송을 막지 못하게 합니다.
            asyncio.create_task(
                safe_control_message(
                    "⏰ 예약 자동 발송을 시작합니다.\n"
                    f"예약 회차: {slot_time.strftime('%Y-%m-%d %H:%M KST')}\n"
                    f"실행 시간: {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}\n"
                    f"다음 예약: {next_run.strftime('%Y-%m-%d %H:%M KST')}"
                )
            )

            await asyncio.sleep(SCHEDULER_POLL_SECONDS)

        except asyncio.CancelledError:
            logger.warning("자동 스케줄러가 종료되었습니다.")
            raise

        except Exception as exc:
            logger.exception(
                "자동 스케줄러 오류 - %s초 후 다시 확인: %s",
                SCHEDULER_POLL_SECONDS,
                exc,
            )
            await asyncio.sleep(SCHEDULER_POLL_SECONDS)



async def handle_control_message(event):
    global send_task, stop_requested, waiting_for_promo

    if waiting_for_promo:
        command_preview = event.raw_text.strip()

        if command_preview == "/cancel":
            waiting_for_promo = None
            await event.respond("❎ 홍보 메시지 저장을 취소했습니다.")
            return

        if command_preview.startswith("/") and not event.media:
            await event.respond(
                "⚠️ 지금은 홍보 메시지를 기다리고 있습니다. 취소하려면 /cancel을 입력하세요."
            )
            return

        if waiting_for_promo == "promo1":
            PROMO1_MESSAGE_ID_FILE.write_text(str(event.id), encoding="utf-8")
            saved_label = "사진+텍스트 홍보 메시지"
        else:
            if event.media:
                await event.respond(
                    "❌ /setpromo2에는 사진 없이 텍스트만 보내주세요.\n"
                    "다시 텍스트 메시지를 보내거나 /cancel을 입력하세요."
                )
                return
            if not event.raw_text.strip():
                await event.respond(
                    "❌ 텍스트 문구가 비어 있습니다. 다시 보내거나 /cancel을 입력하세요."
                )
                return
            PROMO2_MESSAGE_ID_FILE.write_text(str(event.id), encoding="utf-8")
            saved_label = "텍스트 전용 홍보 메시지"

        waiting_for_promo = None
        await event.respond(
            f"✅ {saved_label}를 저장했습니다.\n"
            "이제 /send 한 번으로 사진 가능 그룹에는 사진+텍스트, "
            "사진 제한 그룹에는 텍스트만 자동 발송합니다."
        )
        return

    if await save_uploaded_file(event):
        return

    command = event.raw_text.strip()
    if command.startswith("/"):
        logger.info("명령 수신: %s", command)

    if command == "/ping":
        await event.respond("✅ 프로그램이 정상 작동 중입니다.")

    elif command == "/help":
        await event.respond(
            "명령어\n"
            "/scan - 가입한 그룹 목록 만들기\n"
            "/send - 전체 발송 시작\n"
            "/retry - 실패한 그룹만 다시 발송\n"
            "/stop - 진행 중인 발송 중지\n"
            "/status - 현재 상태 확인\n"
            "/progress - 현재 발송 진행률 확인\n"
            "/files - 현재 파일 상태 확인\n"
            "/setpromo1 - 사진+텍스트 홍보 저장\n"
            "/setpromo2 - 텍스트 전용 홍보 저장\n"
            "/clearpromo - 저장한 홍보 원본 2개 삭제\n"
            "/autoon - 자동 발송 켜기\n"
            "/autooff - 자동 발송 끄기\n"
            "/schedule 6h - 6시간마다 발송\n"
            "/schedule 08:00 14:00 20:00 - 지정 시간 발송\n"
            "/ping - 프로그램 응답 확인\n"
            "/help - 도움말\n\n"
            "파일 업데이트\n"
            "targets.txt - 발송 대상 교체\n"
            "promo.txt - 홍보 문구 교체\n"
            "promo.jpg - 홍보 이미지 교체\n\n"
            "사용 순서\n"
            "1. /setpromo1 입력 후 사진+텍스트 메시지 전송\n"
            "2. /setpromo2 입력 후 텍스트 전용 메시지 전송\n"
            "3. /send 또는 자동 일정으로 발송\n"
            "사진 제한 그룹에는 텍스트 전용 홍보가 자동 전송됩니다.\n"
            "긴 Telegram 대기 제한이 발생하면 해당 그룹만 실패 처리하고 다음 그룹으로 진행합니다.\n"
            f"현재 자동 일정: {schedule_description()}\n"
            "명령어와 파일은 텔레그램 '저장한 메시지'에 보내세요."
        )

    elif command in {"/setpromo", "/setpromo1"}:
        waiting_for_promo = "promo1"
        await event.respond(
            "🖼 다음에 보내는 메시지를 사진+텍스트 홍보 원본으로 저장합니다.\n"
            "사진, 텍스트, 굵게, 링크, 프리미엄 커스텀 이모지를 포함해 보내세요.\n"
            "취소하려면 /cancel을 입력하세요."
        )

    elif command == "/setpromo2":
        waiting_for_promo = "promo2"
        await event.respond(
            "📝 다음에 보내는 메시지를 텍스트 전용 홍보 원본으로 저장합니다.\n"
            "사진 없이 텍스트, 굵게, 링크, 프리미엄 커스텀 이모지만 보내세요.\n"
            "취소하려면 /cancel을 입력하세요."
        )

    elif command == "/clearpromo":
        PROMO1_MESSAGE_ID_FILE.unlink(missing_ok=True)
        PROMO2_MESSAGE_ID_FILE.unlink(missing_ok=True)
        await event.respond(
            "✅ 사진+텍스트 및 텍스트 전용 홍보 원본을 모두 삭제했습니다. "
            "이제 promo.txt/promo.jpg를 사용합니다."
        )

    elif command == "/autoon":
        set_auto_send_enabled(True)
        mode, value = parse_schedule(read_schedule())

        if mode == "interval":
            next_run = datetime.now(KST) + timedelta(hours=value)
            write_next_auto_run(next_run)
        else:
            next_run = next_auto_run()

        await event.respond(
            "✅ 자동 발송을 켰습니다.\n"
            f"일정: {schedule_description()}\n"
            f"다음 발송: {next_run.strftime('%Y-%m-%d %H:%M KST')}\n"
            f"스케줄러 확인 간격: {SCHEDULER_POLL_SECONDS}초"
        )

    elif command.startswith("/schedule"):
        parts = command.split()
        if len(parts) < 2:
            await event.respond(
                "사용 예시\n"
                "/schedule 6h\n"
                "/schedule 08:00 14:00 20:00"
            )
            return

        try:
            args = parts[1:]
            if len(args) == 1 and args[0].lower().endswith("h"):
                hours = int(args[0][:-1])
                if hours < 1 or hours > 168:
                    raise ValueError
                write_schedule(f"interval:{hours}")
            else:
                parsed_times = []
                for item in args:
                    hour_text, minute_text = item.split(":", 1)
                    hour = int(hour_text)
                    minute = int(minute_text)
                    if not (0 <= hour <= 23 and 0 <= minute <= 59):
                        raise ValueError
                    parsed_times.append(f"{hour:02d}:{minute:02d}")
                write_schedule("times:" + ",".join(parsed_times))

            set_auto_send_enabled(True)
            NEXT_AUTO_RUN_FILE.unlink(missing_ok=True)
            LAST_AUTO_SLOT_FILE.unlink(missing_ok=True)

            mode, value = parse_schedule(read_schedule())
            if mode == "interval":
                next_run = datetime.now(KST) + timedelta(hours=value)
                write_next_auto_run(next_run)
            else:
                next_run = next_auto_run()

            await event.respond(
                "✅ 자동 발송 일정을 저장하고 자동 발송을 켰습니다.\n"
                f"일정: {schedule_description()}\n"
                f"다음 발송: {next_run.strftime('%Y-%m-%d %H:%M KST')}\n"
                "변경된 일정은 최대 "
                f"{SCHEDULER_POLL_SECONDS}초 안에 반영됩니다."
            )
        except Exception:
            await event.respond(
                "❌ 일정 형식이 올바르지 않습니다.\n"
                "예시: /schedule 6h\n"
                "예시: /schedule 08:00 14:00 20:00"
            )

    elif command == "/autooff":
        set_auto_send_enabled(False)
        await event.respond("⏸ 자동 발송을 껐습니다. 수동 /send는 계속 사용할 수 있습니다.")

    elif command == "/scan":
        await event.respond("🔎 그룹 목록을 확인하고 있습니다...")
        try:
            output = await scan_groups()
            await client.send_file(
                "me",
                output,
                caption="가입한 그룹 목록입니다. 게시가 허용된 그룹만 대상에 넣으세요.",
            )
        except Exception as exc:
            await event.respond(f"❌ 그룹 스캔 실패\n{exc}")

    elif command == "/send":
        if send_task and not send_task.done():
            await event.respond("⚠️ 이미 발송 중입니다.")
            return
        send_task = asyncio.create_task(send_promotions())

    elif command in {"/retry", "/retry_failed"}:
        if send_task and not send_task.done():
            await event.respond("⚠️ 이미 발송 중입니다.")
            return

        try:
            retry_targets = read_failed_targets()
        except Exception as exc:
            await event.respond(f"❌ 재발송할 수 없습니다.\n{exc}")
            return

        await event.respond(f"🔄 실패 그룹 재발송 준비\n대상: {len(retry_targets)}개")
        send_task = asyncio.create_task(
            send_promotions(retry_targets, "실패 그룹 재발송")
        )

    elif command == "/stop":
        stop_requested = True
        await event.respond("⏹ 중지 요청을 받았습니다. 현재 작업 후 멈춥니다.")

    elif command == "/status":
        running = bool(send_task and not send_task.done())
        auto_enabled = is_auto_send_enabled()

        if auto_enabled:
            next_run = scheduler_next_display()
            auto_line = (
                f"켜짐 / {schedule_description()} / "
                f"다음: {next_run.strftime('%Y-%m-%d %H:%M KST')}"
            )
        else:
            auto_line = "꺼짐"

        if running and progress_total:
            remaining = max(0, progress_total - progress_current)
            progress_line = (
                f"진행률: {progress_current}/{progress_total}\n"
                f"성공: {progress_success}개 / 실패: {progress_failed}개\n"
                f"남음: {remaining}개\n"
            )
        else:
            progress_line = (
                f"마지막 상태: {last_run_status}\n"
                f"마지막 결과: 성공 {progress_success}개 / "
                f"실패 {progress_failed}개\n"
            )

        finished_line = (
            last_run_finished_at.strftime("%Y-%m-%d %H:%M KST")
            if last_run_finished_at
            else "기록 없음"
        )

        await event.respond(
            f"상태: {'발송 중' if running else '대기 중'}\n"
            f"{progress_line}"
            f"마지막 종료: {finished_line}\n"
            f"그룹 간 발송 간격: {SEND_INTERVAL}초\n"
            f"그룹별 제한 시간: {SEND_TIMEOUT}초\n"
            f"최대 FloodWait 대기: {MAX_FLOOD_WAIT}초\n"
            f"스케줄러 확인: {SCHEDULER_POLL_SECONDS}초마다\n"
            f"자동 발송: {auto_line}"
        )

    elif command == "/progress":
        running = bool(send_task and not send_task.done())

        if not running or not progress_total:
            finished_line = (
                last_run_finished_at.strftime("%Y-%m-%d %H:%M KST")
                if last_run_finished_at
                else "기록 없음"
            )

            if is_auto_send_enabled():
                next_run = scheduler_next_display()
                next_line = next_run.strftime("%Y-%m-%d %H:%M KST")
            else:
                next_line = "자동 발송 꺼짐"

            await event.respond(
                "📋 현재 진행 중인 발송이 없습니다.\n"
                f"마지막 상태: {last_run_status}\n"
                f"마지막 결과: 성공 {progress_success}개 / "
                f"실패 {progress_failed}개\n"
                f"마지막 종료: {finished_line}\n"
                f"다음 자동 발송: {next_line}"
            )
            return

        remaining = max(0, progress_total - progress_current)
        percent = (
            int((progress_current / progress_total) * 100)
            if progress_total
            else 0
        )

        elapsed_seconds = 0
        if progress_started_at:
            elapsed_seconds = max(
                0,
                int(
                    (
                        datetime.now(KST) - progress_started_at
                    ).total_seconds()
                ),
            )

        if progress_current > 0:
            average_seconds = elapsed_seconds / progress_current
            estimated_seconds = int(average_seconds * remaining)
        else:
            estimated_seconds = remaining * SEND_INTERVAL

        await event.respond(
            "📊 현재 발송 진행률\n"
            f"진행: {progress_current}/{progress_total} ({percent}%)\n"
            f"성공: {progress_success}개\n"
            f"실패: {progress_failed}개\n"
            f"남음: {remaining}개\n"
            f"예상 남은 시간: 약 "
            f"{estimated_seconds // 60}분 {estimated_seconds % 60}초"
        )

    elif command == "/files":
        try:
            target_count = len(read_targets())
            targets_status = f"있음 ({target_count}개)"
        except Exception as exc:
            targets_status = f"확인 필요 ({exc})"

        try:
            promo_length = len(read_promo_text())
            promo_status = f"있음 ({promo_length}자)"
        except Exception as exc:
            promo_status = f"확인 필요 ({exc})"

        image_status = "있음" if PROMO_IMAGE_FILE.exists() else "없음"
        promo1_status = "있음" if read_message_id(PROMO1_MESSAGE_ID_FILE) else "없음"
        promo2_status = "있음" if read_message_id(PROMO2_MESSAGE_ID_FILE) else "없음"
        try:
            failed_status = f"있음 ({len(read_failed_targets())}개)"
        except Exception:
            failed_status = "없음"

        await event.respond(
            "📁 현재 파일 상태\n"
            f"targets.txt: {targets_status}\n"
            f"promo.txt: {promo_status}\n"
            f"promo.jpg: {image_status}\n"
            f"사진+텍스트 홍보: {promo1_status}\n"
            f"텍스트 전용 홍보: {promo2_status}\n"
            f"실패 그룹 목록: {failed_status}"
        )


@client.on(events.NewMessage(chats="me", outgoing=True))
async def control_handler(event):
    try:
        await handle_control_message(event)
    except Exception as exc:
        logger.exception("명령 처리 오류: %s", exc)
        try:
            await event.respond(
                "❌ 명령 처리 중 오류가 발생했습니다.\n"
                f"{type(exc).__name__}: {exc}"
            )
        except Exception:
            pass




async def main():
    global scheduler_task
    load_run_state()
    await client.start()
    me = await client.get_me()
    logger.info("로그인 완료: %s (%s)", me.first_name, me.id)
    scheduler_task = asyncio.create_task(automatic_scheduler())
    auto_status = "켜짐" if is_auto_send_enabled() else "꺼짐"
    next_line = (
        scheduler_next_display().strftime("%Y-%m-%d %H:%M KST")
        if is_auto_send_enabled()
        else "자동 발송 꺼짐"
    )
    await safe_control_message(
        "✅ 홍보 프로그램 안정판 v2가 실행되었습니다.\n"
        "저장한 메시지에 /help를 입력하세요.\n"
        f"자동 발송: {auto_status}\n"
        f"일정: {schedule_description()}\n"
        f"다음 발송: {next_line}\n"
        f"스케줄러 확인 간격: {SCHEDULER_POLL_SECONDS}초"
    )
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
