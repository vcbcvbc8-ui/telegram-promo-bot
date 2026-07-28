import asyncio
import getpass

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    print("Telegram STRING_SESSION 생성기")
    print("API_HASH와 STRING_SESSION은 외부에 공개하지 마세요.\n")

    api_id_text = input("API_ID: ").strip()
    api_hash = getpass.getpass("API_HASH: ").strip()
    phone = input("전화번호(국가번호 포함): ").strip()

    try:
        api_id = int(api_id_text)
    except ValueError:
        print("오류: API_ID는 숫자만 입력해야 합니다.")
        return

    print(f"\n확인: API_ID = {api_id}")
    print(f"확인: API_HASH 길이 = {len(api_hash)}자리")

    if len(api_hash) != 32:
        print("오류: API_HASH는 정확히 32자리여야 합니다.")
        return

    client = TelegramClient(StringSession(), api_id, api_hash)

    try:
        await client.start(phone=phone)
        session_string = client.session.save()

        print("\n로그인 성공!")
        print("\nSTRING_SESSION:")
        print(session_string)
        print("\n이 값을 외부에 공개하지 마세요.")

    except Exception as error:
        print(f"\n오류: {type(error).__name__}: {error}")

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())