import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ChatType
from telegram.error import RetryAfter, TelegramError
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("PROMO_BOT_TOKEN", "").strip()
ADMIN_USER_ID_RAW = os.getenv("ADMIN_USER_ID", "").strip()

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
GROUPS_FILE = DATA_DIR / "promo_groups.json"
SOURCE_FILE = DATA_DIR / "promo_source.json"

MIN_AUTOSEND_MINUTES = 60
SEND_DELAY_SECONDS = 2

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("promo_bot")


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        logger.exception("설정 파일 읽기 실패: %s", path)
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    temp.replace(path)


def admin_user_id() -> int | None:
    try:
        return int(ADMIN_USER_ID_RAW) if ADMIN_USER_ID_RAW else None
    except ValueError:
        return None


async def require_admin(update: Update) -> bool:
    user = update.effective_user
    message = update.effective_message
    configured_admin = admin_user_id()

    if not user:
        return False

    if configured_admin is None:
        if message:
            await message.reply_text(
                "ADMIN_USER_ID가 아직 설정되지 않았습니다.\n"
                "봇 개인 채팅에서 /myid를 입력하고 Railway Variables에 등록하세요."
            )
        return False

    if user.id != configured_admin:
        if message:
            await message.reply_text("관리자만 사용할 수 있습니다.")
        return False

    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        "홍보 전용 봇입니다.\n\n"
        "/myid - 내 숫자 ID 확인\n"
        "/setphoto - 답장한 사진+글 원본 등록\n"
        "/settext - 답장한 글 원본 등록\n"
        "/addgroup photo - 현재 그룹을 사진 그룹으로 등록\n"
        "/addgroup text - 현재 그룹을 글 전용 그룹으로 등록\n"
        "/removegroup - 현재 그룹 등록 해제\n"
        "/groups - 등록 그룹 확인\n"
        "/send - 즉시 전체 발송\n"
        "/autosend 60 - 60분마다 자동 발송\n"
        "/stop - 자동 발송 중지\n"
        "/status - 설정 상태 확인"
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_message:
        await update.effective_message.reply_text(str(update.effective_user.id))


async def set_source(update: Update, mode: str) -> None:
    if not await require_admin(update):
        return

    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    replied = message.reply_to_message
    if not replied:
        await message.reply_text("등록할 원본 메시지에 답장한 뒤 명령어를 입력하세요.")
        return

    source = load_json(SOURCE_FILE, {})
    source[mode] = {
        "chat_id": chat.id,
        "message_id": replied.message_id,
        "chat_title": chat.title or chat.username or str(chat.id),
    }
    save_json(SOURCE_FILE, source)

    label = "사진+글" if mode == "photo" else "글 전용"
    await message.reply_text(f"✅ {label} 원본 등록 완료")


async def setphoto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_source(update, "photo")


async def settext(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_source(update, "text")


async def addgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return

    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.reply_text("등록할 그룹 안에서 사용하세요.")
        return

    if not context.args or context.args[0].lower() not in {"photo", "text"}:
        await message.reply_text("사용법:\n/addgroup photo\n또는\n/addgroup text")
        return

    mode = context.args[0].lower()
    groups = load_json(GROUPS_FILE, {})
    groups[str(chat.id)] = {
        "chat_id": chat.id,
        "title": chat.title or chat.username or str(chat.id),
        "mode": mode,
    }
    save_json(GROUPS_FILE, groups)

    label = "사진+글" if mode == "photo" else "글 전용"
    await message.reply_text(f"✅ {label} 그룹으로 등록 완료")


async def removegroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return

    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    groups = load_json(GROUPS_FILE, {})
    if groups.pop(str(chat.id), None) is None:
        await message.reply_text("등록되지 않은 그룹입니다.")
        return

    save_json(GROUPS_FILE, groups)
    await message.reply_text("✅ 그룹 등록 해제 완료")


async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return

    message = update.effective_message
    if not message:
        return

    groups = load_json(GROUPS_FILE, {})
    if not groups:
        await message.reply_text("등록된 그룹이 없습니다.")
        return

    photo = [g["title"] for g in groups.values() if g.get("mode") == "photo"]
    text = [g["title"] for g in groups.values() if g.get("mode") == "text"]

    result = (
        f"총 {len(groups)}개\n\n"
        f"📷 사진+글 {len(photo)}개\n" + "\n".join(f"• {x}" for x in photo) +
        f"\n\n📝 글 전용 {len(text)}개\n" + "\n".join(f"• {x}" for x in text)
    )

    for i in range(0, len(result), 3900):
        await message.reply_text(result[i:i + 3900])


async def copy_message_with_retry(
    application: Application,
    destination_chat_id: int,
    source_chat_id: int,
    source_message_id: int,
) -> None:
    try:
        await application.bot.copy_message(
            chat_id=destination_chat_id,
            from_chat_id=source_chat_id,
            message_id=source_message_id,
        )
    except RetryAfter as error:
        await asyncio.sleep(int(error.retry_after) + 1)
        await application.bot.copy_message(
            chat_id=destination_chat_id,
            from_chat_id=source_chat_id,
            message_id=source_message_id,
        )


async def send_all(application: Application) -> tuple[int, int, list[str]]:
    groups = load_json(GROUPS_FILE, {})
    source = load_json(SOURCE_FILE, {})

    if not groups:
        raise RuntimeError("등록된 그룹이 없습니다.")
    if "photo" not in source or "text" not in source:
        raise RuntimeError("사진 원본과 글 원본을 모두 등록하세요.")

    success = 0
    failed = 0
    failures: list[str] = []

    for group in groups.values():
        mode = group.get("mode")
        src = source.get(mode)
        title = group.get("title", str(group.get("chat_id")))

        try:
            await copy_message_with_retry(
                application,
                int(group["chat_id"]),
                int(src["chat_id"]),
                int(src["message_id"]),
            )
            success += 1
        except TelegramError as error:
            failed += 1
            failures.append(f"{title}: {error}")
            logger.exception("전송 실패: %s", title)

        await asyncio.sleep(SEND_DELAY_SECONDS)

    return success, failed, failures


async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return

    message = update.effective_message
    if not message:
        return

    await message.reply_text("발송을 시작합니다.")

    try:
        success, failed, failures = await send_all(context.application)
    except RuntimeError as error:
        await message.reply_text(f"❌ {error}")
        return

    result = f"✅ 발송 완료\n성공: {success}개\n실패: {failed}개"
    if failures:
        result += "\n\n실패 목록:\n" + "\n".join(failures[:20])

    await message.reply_text(result[:4000])


async def scheduled_send(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        success, failed, _ = await send_all(context.application)
        logger.info("자동 발송 완료 | 성공=%s 실패=%s", success, failed)
    except Exception:
        logger.exception("자동 발송 실패")


async def autosend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return

    message = update.effective_message
    if not message:
        return

    try:
        minutes = int(context.args[0])
    except (IndexError, ValueError):
        await message.reply_text("사용법: /autosend 60")
        return

    if minutes < MIN_AUTOSEND_MINUTES:
        await message.reply_text(f"최소 간격은 {MIN_AUTOSEND_MINUTES}분입니다.")
        return

    if context.application.job_queue is None:
        await message.reply_text("JobQueue가 설치되지 않았습니다.")
        return

    for job in context.application.job_queue.get_jobs_by_name("promo_autosend"):
        job.schedule_removal()

    context.application.job_queue.run_repeating(
        scheduled_send,
        interval=minutes * 60,
        first=minutes * 60,
        name="promo_autosend",
    )
    await message.reply_text(f"✅ {minutes}분마다 자동 발송을 시작했습니다.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return

    message = update.effective_message
    if not message:
        return

    jobs = (
        context.application.job_queue.get_jobs_by_name("promo_autosend")
        if context.application.job_queue
        else []
    )
    for job in jobs:
        job.schedule_removal()

    await message.reply_text("✅ 자동 발송을 중지했습니다.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return

    message = update.effective_message
    if not message:
        return

    groups = load_json(GROUPS_FILE, {})
    source = load_json(SOURCE_FILE, {})
    jobs = (
        context.application.job_queue.get_jobs_by_name("promo_autosend")
        if context.application.job_queue
        else []
    )

    await message.reply_text(
        "현재 상태\n\n"
        f"사진+글 원본: {'등록됨' if source.get('photo') else '미등록'}\n"
        f"글 전용 원본: {'등록됨' if source.get('text') else '미등록'}\n"
        f"등록 그룹: {len(groups)}개\n"
        f"자동 발송: {'실행 중' if jobs else '중지'}"
    )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("PROMO_BOT_TOKEN 환경변수를 설정하세요.")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("setphoto", setphoto))
    application.add_handler(CommandHandler("settext", settext))
    application.add_handler(CommandHandler("addgroup", addgroup))
    application.add_handler(CommandHandler("removegroup", removegroup))
    application.add_handler(CommandHandler("groups", groups_command))
    application.add_handler(CommandHandler("send", send_command))
    application.add_handler(CommandHandler("autosend", autosend))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("status", status))

    logger.info("홍보 봇 시작")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
