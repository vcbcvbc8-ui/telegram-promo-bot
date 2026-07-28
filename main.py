import asyncio
import logging
import os
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
PROMO_TEXT_FILE = Path(os.getenv("PROMO_TEXT_FILE", "promo.txt"))
PROMO_IMAGE_FILE = Path(os.getenv("PROMO_IMAGE_FILE", "promo.jpg"))
TARGETS_FILE = Path(os.getenv("TARGETS_FILE", "targets.txt"))
PROMO1_MESSAGE_ID_FILE = Path(os.getenv("PROMO1_MESSAGE_ID_FILE", "promo1_message_id.txt"))
PROMO2_MESSAGE_ID_FILE = Path(os.getenv("PROMO2_MESSAGE_ID_FILE", "promo2_message_id.txt"))
AUTO_SEND_ENABLED_FILE = Path(os.getenv("AUTO_SEND_ENABLED_FILE", "auto_send_enabled.txt"))
SCHEDULE_FILE = Path(os.getenv("SCHEDULE_FILE", "schedule.txt"))
FAILED_TARGETS_FILE = Path(os.getenv("FAILED_TARGETS_FILE", "failed_targets.txt"))
SEND_LOG_FILE = Path(os.getenv("SEND_LOG_FILE", "send_log.txt"))
KST = ZoneInfo("Asia/Seoul")
AUTO_SEND_DEFAULT = os.getenv("AUTO_SEND_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_SCHEDULE = os.getenv("AUTO_SEND_SCHEDULE", "times:00:00,06:00,12:00,18:00").strip()

client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
send_task = None
stop_requested = False
waiting_for_promo = None
scheduler_task = None
progress_current = 0
progress_total = 0
progress_success = 0
progress_failed = 0
progress_started_at = None


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


def append_send_log(target: str, result: str, detail: str = "") -> None:
    timestamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    clean_detail = detail.replace("\n", " ").strip()
    line = f"{timestamp}\t{target}\t{result}"
    if clean_detail:
        line += f"\t{clean_detail}"
    with SEND_LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def save_failed_targets(targets: list[str]) -> None:
    FAILED_TARGETS_FILE.write_text(
        "\n".join(targets) + ("\n" if targets else ""),
        encoding="utf-8",
    )


async def send_progress_message(current: int, total: int, success: int, failed: int) -> None:
    remaining = max(0, total - current)
    percent = int((current / total) * 100) if total else 100
    await control_message(
        f"📤 발송 진행률\n"
        f"{current} / {total} ({percent}%)\n"
        f"성공: {success}개\n"
        f"실패: {failed}개\n"
        f"남음: {remaining}개"
    )


async def send_promotions():
    global stop_requested
    global progress_current, progress_total, progress_success, progress_failed
    global progress_started_at

    stop_requested = False
    progress_current = 0
    progress_total = 0
    progress_success = 0
    progress_failed = 0
    progress_started_at = datetime.now(KST)

    try:
        targets = read_targets()
        promo1 = await get_saved_promo_message(
            PROMO1_MESSAGE_ID_FILE, "사진+텍스트 홍보"
        )
        promo2 = await get_saved_promo_message(
            PROMO2_MESSAGE_ID_FILE, "텍스트 전용 홍보"
        )
        text_value = None if promo1 else read_promo_text()
    except Exception as exc:
        await control_message(f"❌ 설정 오류\n{exc}")
        return

    image_exists = PROMO_IMAGE_FILE.exists() and promo1 is None
    total = len(targets)
    progress_total = total
    failed_targets = []

    await control_message(
        f"🚀 발송 시작\n대상: {total}개\n간격: {SEND_INTERVAL}초\n"
        f"홍보 방식: {'사진+텍스트 / 텍스트 자동 구분' if promo1 else ('이미지+문구' if image_exists else '문구')}"
    )

    for index, target in enumerate(targets, start=1):
        if stop_requested:
            save_failed_targets(failed_targets)
            await control_message(
                f"⛔ 발송 중지\n"
                f"진행: {progress_current}/{total}\n"
                f"성공: {progress_success}개\n"
                f"실패: {progress_failed}개"
            )
            return

        result = "실패"
        detail = ""

        try:
            key = int(target) if target.lstrip("-").isdigit() else target
            entity = await client.get_entity(key)
            mode = await send_to_entity(
                entity, promo1, promo2, text_value, image_exists
            )
            progress_success += 1
            result = "성공"
            detail = mode
            logger.info("[%s/%s] 성공: %s (%s)", index, total, target, mode)

        except FloodWaitError as exc:
            wait_seconds = int(exc.seconds) + 5
            await control_message(
                f"⏳ Telegram 제한으로 {wait_seconds}초 대기합니다.\n대상: {target}"
            )
            await asyncio.sleep(wait_seconds)

            try:
                key = int(target) if target.lstrip("-").isdigit() else target
                entity = await client.get_entity(key)
                mode = await send_to_entity(
                    entity, promo1, promo2, text_value, image_exists
                )
                progress_success += 1
                result = "성공"
                detail = f"재시도 성공/{mode}"
            except Exception as retry_exc:
                progress_failed += 1
                failed_targets.append(target)
                detail = f"재시도 실패: {retry_exc}"
                logger.exception("재시도 실패 %s: %s", target, retry_exc)

        except (RPCError, ValueError, TypeError) as exc:
            progress_failed += 1
            failed_targets.append(target)
            detail = str(exc)
            logger.warning("[%s/%s] 실패 %s: %s", index, total, target, exc)

        except Exception as exc:
            progress_failed += 1
            failed_targets.append(target)
            detail = str(exc)
            logger.exception("[%s/%s] 예외 %s: %s", index, total, target, exc)

        progress_current = index
        append_send_log(target, result, detail)
        save_failed_targets(failed_targets)

        # 10개마다, 마지막 대상에서 진행률을 저장된 메시지로 알립니다.
        if index % 10 == 0 or index == total:
            await send_progress_message(
                progress_current,
                progress_total,
                progress_success,
                progress_failed,
            )

        if index < total and not stop_requested:
            await asyncio.sleep(SEND_INTERVAL)

    elapsed = datetime.now(KST) - progress_started_at
    elapsed_minutes = int(elapsed.total_seconds() // 60)
    elapsed_seconds = int(elapsed.total_seconds() % 60)

    await control_message(
        f"✅ 발송 완료\n"
        f"성공: {progress_success}개\n"
        f"실패: {progress_failed}개\n"
        f"전체: {total}개\n"
        f"소요 시간: {elapsed_minutes}분 {elapsed_seconds}초"
    )

    if failed_targets:
        await client.send_file(
            "me",
            FAILED_TARGETS_FILE,
            caption=f"실패한 그룹 {len(failed_targets)}개 목록입니다.",
        )


async def automatic_scheduler():
    global send_task

    while True:
        if not is_auto_send_enabled():
            await asyncio.sleep(60)
            continue

        run_at = next_auto_run()
        wait_seconds = max(1, (run_at - datetime.now(KST)).total_seconds())
        logger.info("다음 자동 발송: %s", run_at.strftime("%Y-%m-%d %H:%M KST"))
        await asyncio.sleep(wait_seconds)

        if not is_auto_send_enabled():
            continue

        if send_task and not send_task.done():
            await control_message("⚠️ 예약 시간이 되었지만 기존 발송이 진행 중이라 이번 자동 발송은 건너뜁니다.")
            continue

        await control_message(
            f"⏰ 예약 자동 발송을 시작합니다.\n"
            f"시간: {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}"
        )
        send_task = asyncio.create_task(send_promotions())




@client.on(events.NewMessage(chats="me", outgoing=True))
async def control_handler(event):
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

    if command == "/help":
        await event.respond(
            "명령어\n"
            "/scan - 가입한 그룹 목록 만들기\n"
            "/send - 전체 발송 시작\n"
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
        await event.respond(
            "✅ 자동 발송을 켰습니다.\n"
            f"일정: {schedule_description()}\n"
            f"다음 발송: {next_auto_run().strftime('%Y-%m-%d %H:%M KST')}"
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
            await event.respond(
                "✅ 자동 발송 일정을 저장하고 자동 발송을 켰습니다.\n"
                f"일정: {schedule_description()}\n"
                f"다음 발송: {next_auto_run().strftime('%Y-%m-%d %H:%M KST')}"
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

    elif command == "/stop":
        stop_requested = True
        await event.respond("⏹ 중지 요청을 받았습니다. 현재 작업 후 멈춥니다.")

    elif command == "/status":
        running = bool(send_task and not send_task.done())
        auto_enabled = is_auto_send_enabled()
        auto_line = (
            f"켜짐 / {schedule_description()} / 다음: {next_auto_run().strftime('%Y-%m-%d %H:%M KST')}"
            if auto_enabled else "꺼짐"
        )
        if running and progress_total:
            remaining = max(0, progress_total - progress_current)
            progress_line = (
                f"진행률: {progress_current}/{progress_total}\n"
                f"성공: {progress_success}개 / 실패: {progress_failed}개\n"
                f"남음: {remaining}개\n"
            )
        else:
            progress_line = ""

        await event.respond(
            f"상태: {'발송 중' if running else '대기 중'}\n"
            f"{progress_line}"
            f"그룹 간 발송 간격: {SEND_INTERVAL}초\n"
            f"자동 발송: {auto_line}"
        )

    elif command == "/progress":
        running = bool(send_task and not send_task.done())

        if not running or not progress_total:
            await event.respond(
                "현재 진행 중인 발송이 없습니다.\n"
                f"마지막 상태: 성공 {progress_success}개 / 실패 {progress_failed}개"
            )
            return

        remaining = max(0, progress_total - progress_current)
        percent = int((progress_current / progress_total) * 100) if progress_total else 0

        elapsed_seconds = 0
        if progress_started_at:
            elapsed_seconds = max(
                0,
                int((datetime.now(KST) - progress_started_at).total_seconds()),
            )

        if progress_current > 0:
            average_seconds = elapsed_seconds / progress_current
            estimated_seconds = int(average_seconds * remaining)
        else:
            estimated_seconds = remaining * SEND_INTERVAL

        estimated_minutes = estimated_seconds // 60
        estimated_remainder = estimated_seconds % 60

        await event.respond(
            "📊 현재 발송 진행률\n"
            f"진행: {progress_current}/{progress_total} ({percent}%)\n"
            f"성공: {progress_success}개\n"
            f"실패: {progress_failed}개\n"
            f"남음: {remaining}개\n"
            f"예상 남은 시간: 약 {estimated_minutes}분 {estimated_remainder}초"
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

        await event.respond(
            "📁 현재 파일 상태\n"
            f"targets.txt: {targets_status}\n"
            f"promo.txt: {promo_status}\n"
            f"promo.jpg: {image_status}\n"
            f"사진+텍스트 홍보: {promo1_status}\n"
            f"텍스트 전용 홍보: {promo2_status}"
        )


async def main():
    global scheduler_task
    await client.start()
    me = await client.get_me()
    logger.info("로그인 완료: %s (%s)", me.first_name, me.id)
    scheduler_task = asyncio.create_task(automatic_scheduler())
    auto_status = "켜짐" if is_auto_send_enabled() else "꺼짐"
    await control_message(
        "✅ 홍보 프로그램이 실행되었습니다.\n"
        "저장한 메시지에 /help를 입력하세요.\n"
        f"자동 발송: {auto_status}\n"
        f"일정: {schedule_description()}"
    )
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
