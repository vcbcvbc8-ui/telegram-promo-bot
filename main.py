import asyncio
import logging
import os
from pathlib import Path
from typing import List

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
SEND_INTERVAL = max(30, int(os.getenv("SEND_INTERVAL", "30")))
PROMO_TEXT_FILE = Path(os.getenv("PROMO_TEXT_FILE", "promo.txt"))
PROMO_IMAGE_FILE = Path(os.getenv("PROMO_IMAGE_FILE", "promo.jpg"))
TARGETS_FILE = Path(os.getenv("TARGETS_FILE", "targets.txt"))
PROMO_MESSAGE_ID_FILE = Path(os.getenv("PROMO_MESSAGE_ID_FILE", "promo_message_id.txt"))

client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
send_task = None
stop_requested = False
waiting_for_promo = False


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


def read_promo_message_id():
    if not PROMO_MESSAGE_ID_FILE.exists():
        return None
    value = PROMO_MESSAGE_ID_FILE.read_text(encoding="utf-8").strip()
    return int(value) if value.isdigit() else None


async def get_saved_promo_message():
    message_id = read_promo_message_id()
    if not message_id:
        return None
    message = await client.get_messages("me", ids=message_id)
    if not message:
        raise ValueError("저장한 홍보 메시지를 찾을 수 없습니다. /setpromo로 다시 저장하세요.")
    return message


async def send_saved_promo(entity, message):
    text = message.message or ""
    entities = message.entities or None
    if message.media:
        await client.send_file(
            entity,
            message.media,
            caption=text or None,
            formatting_entities=entities,
        )
    else:
        await client.send_message(
            entity,
            text,
            formatting_entities=entities,
            link_preview=True,
        )


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


async def send_promotions():
    global stop_requested
    stop_requested = False

    try:
        targets = read_targets()
        saved_promo = await get_saved_promo_message()
        text = None if saved_promo else read_promo_text()
    except Exception as exc:
        await control_message(f"❌ 설정 오류\n{exc}")
        return

    image_exists = PROMO_IMAGE_FILE.exists() and saved_promo is None
    total = len(targets)
    success = 0
    failed = 0

    await control_message(
        f"🚀 발송 시작\n대상: {total}개\n간격: {SEND_INTERVAL}초\n"
        f"홍보 방식: {'저장 메시지' if saved_promo else ('이미지+문구' if image_exists else '문구')}"
    )

    for index, target in enumerate(targets, start=1):
        if stop_requested:
            await control_message(
                f"⛔ 발송 중지\n성공 {success}개 / 실패 {failed}개 / 전체 {total}개"
            )
            return

        try:
            key = int(target) if target.lstrip("-").isdigit() else target
            entity = await client.get_entity(key)

            if saved_promo:
                await send_saved_promo(entity, saved_promo)
            elif image_exists:
                await client.send_file(entity, PROMO_IMAGE_FILE, caption=text)
            else:
                await client.send_message(entity, text)

            success += 1
            logger.info("[%s/%s] 성공: %s", index, total, target)

        except FloodWaitError as exc:
            wait_seconds = int(exc.seconds) + 5
            await control_message(
                f"⏳ Telegram 제한으로 {wait_seconds}초 대기합니다.\n대상: {target}"
            )
            await asyncio.sleep(wait_seconds)

            try:
                key = int(target) if target.lstrip("-").isdigit() else target
                entity = await client.get_entity(key)

                if image_exists:
                    await client.send_file(entity, PROMO_IMAGE_FILE, caption=text)
                else:
                    await client.send_message(entity, text)

                success += 1
            except Exception as retry_exc:
                failed += 1
                logger.exception("재시도 실패 %s: %s", target, retry_exc)

        except (RPCError, ValueError, TypeError) as exc:
            failed += 1
            logger.warning("[%s/%s] 실패 %s: %s", index, total, target, exc)
        except Exception as exc:
            failed += 1
            logger.exception("[%s/%s] 예외 %s: %s", index, total, target, exc)

        if index < total and not stop_requested:
            await asyncio.sleep(SEND_INTERVAL)

    await control_message(
        f"✅ 발송 완료\n성공: {success}개\n실패: {failed}개\n전체: {total}개"
    )


@client.on(events.NewMessage(chats="me", outgoing=True))
async def control_handler(event):
    global send_task, stop_requested, waiting_for_promo

    if waiting_for_promo:
        command_preview = event.raw_text.strip()
        if command_preview == "/cancel":
            waiting_for_promo = False
            await event.respond("❎ 홍보 메시지 저장을 취소했습니다.")
            return
        if command_preview.startswith("/") and not event.media:
            await event.respond("⚠️ 지금은 홍보 메시지를 기다리고 있습니다. 취소하려면 /cancel을 입력하세요.")
            return
        PROMO_MESSAGE_ID_FILE.write_text(str(event.id), encoding="utf-8")
        waiting_for_promo = False
        await event.respond(
            "✅ 홍보 메시지를 저장했습니다.\n"
            "굵게, 링크, 프리미엄 커스텀 이모지와 첨부 미디어를 가능한 한 그대로 발송합니다.\n"
            "테스트하려면 대상 1개로 /send를 입력하세요."
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
            "/files - 현재 파일 상태 확인\n"
            "/setpromo - 다음 메시지를 홍보 원본으로 저장\n"
            "/clearpromo - 저장한 홍보 원본 삭제\n"
            "/help - 도움말\n\n"
            "파일 업데이트\n"
            "targets.txt - 발송 대상 교체\n"
            "promo.txt - 홍보 문구 교체\n"
            "promo.jpg - 홍보 이미지 교체\n\n"
            "프리미엄 이모지/서식을 유지하려면 /setpromo 입력 후\n"
            "홍보할 메시지를 저장한 메시지에 그대로 보내세요.\n"
            "명령어와 파일은 텔레그램 '저장한 메시지'에 보내세요."
        )

    elif command == "/setpromo":
        waiting_for_promo = True
        await event.respond(
            "📝 다음에 보내는 메시지를 홍보 원본으로 저장합니다.\n"
            "텍스트, 사진, 동영상, 굵게, 링크, 프리미엄 커스텀 이모지를 포함해 작성하세요.\n"
            "취소하려면 /cancel을 입력하세요."
        )

    elif command == "/clearpromo":
        PROMO_MESSAGE_ID_FILE.unlink(missing_ok=True)
        await event.respond("✅ 저장한 홍보 원본을 삭제했습니다. 이제 promo.txt/promo.jpg를 사용합니다.")

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
        await event.respond(
            f"상태: {'발송 중' if running else '대기 중'}\n발송 간격: {SEND_INTERVAL}초"
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
        saved_message_status = "있음" if read_promo_message_id() else "없음"

        await event.respond(
            "📁 현재 파일 상태\n"
            f"targets.txt: {targets_status}\n"
            f"promo.txt: {promo_status}\n"
            f"promo.jpg: {image_status}\n"
            f"저장한 홍보 메시지: {saved_message_status}"
        )


async def main():
    await client.start()
    me = await client.get_me()
    logger.info("로그인 완료: %s (%s)", me.first_name, me.id)
    await control_message(
        "✅ 홍보 프로그램이 실행되었습니다.\n저장한 메시지에 /help를 입력하세요."
    )
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
