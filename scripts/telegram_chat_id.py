"""Find the operator's private Telegram chat for the initial EduFlow setup."""

import re
import secrets
import sys
from getpass import getpass

import httpx2


def telegram_result(client, method, **params):
    response = client.post(method, json=params)
    if response.status_code in {401, 404}:
        raise ValueError(
            f"Telegram отклонил токен (HTTP {response.status_code}). Скопируй его из @BotFather ещё раз."
        )
    if response.status_code == 409:
        raise ValueError(
            "HTTP 409. Обновления этого бота получает другой процесс или активен webhook."
        )
    if response.status_code != 200:
        raise ValueError(f"Telegram вернул HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("ok") is not True or "result" not in data:
        raise ValueError("Telegram вернул неожиданный ответ.")
    return data["result"]


def main() -> int:
    token = getpass("Вставь токен из @BotFather и нажми Enter (ввод скрыт): ").strip()
    if not re.fullmatch(r"[0-9]+:[a-zA-Z0-9_-]+", token):
        print(
            "Проверь формат токена. Скопируй его целиком, включая цифры перед двоеточием."
        )
        return 1
    try:
        with httpx2.Client(
            base_url=f"https://api.telegram.org/bot{token}/",
            follow_redirects=False,
            timeout=12,
        ) as client:
            bot = telegram_result(client, "getMe")
            code = secrets.token_hex(5)
            print(f"Открой личный чат с @{bot['username']} и нажми Start.")
            print("Отправь боту одним сообщением вот эту строку:", code)
            while True:
                input("Когда отправишь, нажми Enter здесь: ")
                updates = telegram_result(client, "getUpdates", limit=100)
                chats = set()
                for update in updates:
                    msg = update.get("message") or {}
                    chat = msg.get("chat") or {}
                    if (
                        chat.get("type") == "private"
                        and chat.get("id") == msg.get("from", {}).get("id")
                        and msg.get("text") == code
                    ):
                        chats.add(chat["id"])
                if len(chats) == 1:
                    print(
                        f"Скопируй эту строку в .env:\nEDUFLOW_TELEGRAM_CHAT_ID={chats.pop()}"
                    )
                    return 0
                if chats:
                    raise ValueError(
                        "Код пришёл из нескольких чатов. Запусти проверку заново."
                    )
                print(
                    f"Сообщение с кодом пока не найдено. Проверь чат и строку {code}."
                )
    except httpx2.RequestError:
        print(
            "Не удалось подключиться к api.telegram.org. Проверь сеть или настройки прокси."
        )
    except ValueError as error:
        print(error)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        sys.exit(1)
