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

client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
send_task = None
stop_requested = False


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


def read_targets() -> List[str]:
    env_targets = os.getenv("TARGETS", "").strip()
    if env_targets:
        return [x.strip() for x in env_targets.split(",") if x.strip()]
    if not TARGETS_FILE.exists():
        raise FileNotFoundError("targets.txt 또는 TARGETS가 없습니다.")
    targets = []
    for raw in TARGETS_FILE.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            targets.append(value)
    if not targets:
        raise ValueError("발송 대상이 없습니다.")
    return targets


async def control_message(text: str):
    await client.send_message("me", text)


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
        text = read_promo_text()
    except Exception as exc:
        await control_message(f"❌ 설정 오류\n{exc}")
        return

    image_exists = PROMO_IMAGE_FILE.exists()
    total = len(targets)
    success = 0
    failed = 0
    await control_message(
        f"🚀 발송 시작\n대상: {total}개\n간격: {SEND_INTERVAL}초\n"
        f"이미지: {'사용' if image_exists else '없음'}"
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
            if image_exists:
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
    global send_task, stop_requested
    command = event.raw_text.strip()

    if command == "/help":
        await event.respond(
            "명령어\n"
            "/scan - 가입한 그룹 목록 만들기\n"
            "/send - 전체 발송 시작\n"
            "/stop - 진행 중인 발송 중지\n"
            "/status - 현재 상태 확인\n"
            "/help - 도움말\n\n"
            "명령어는 텔레그램 '저장한 메시지'에 입력하세요."
        )
    elif command == "/scan":
        await event.respond("🔎 그룹 목록을 확인하고 있습니다...")
        try:
            output = await scan_groups()
            await client.send_file(
                "me", output,
                caption="가입한 그룹 목록입니다. 게시가 허용된 그룹만 대상에 넣으세요."
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
