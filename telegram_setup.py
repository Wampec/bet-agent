"""
telegram_setup.py -- jednorazove dokonceni Telegramu.

Spusteni:  python telegram_setup.py

Co udela:
  1. overi TELEGRAM_BOT_TOKEN z .env proti Telegramu (getMe),
  2. najde chat, ze ktereho jsi botovi psal (getUpdates),
  3. zapise TELEGRAM_CHAT_ID do .env,
  4. posle testovaci zpravu, aby bylo videt, ze cely retez funguje.

Pokud nenajde zadny chat, napis svemu botovi v Telegramu /start a spust znovu.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

from config import BASE_DIR, enable_utf8_stdout

ENV_PATH = BASE_DIR / ".env"
TELEGRAM_API = "https://api.telegram.org"


def read_env_value(key: str) -> str:
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def write_env_value(key: str, value: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8-sig")
    if re.search(rf"^{key}=.*$", text, re.M):
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    enable_utf8_stdout()
    load_dotenv(ENV_PATH, override=True)

    token = read_env_value("TELEGRAM_BOT_TOKEN")
    if not token:
        print("V .env chybi TELEGRAM_BOT_TOKEN.")
        return 2

    # --- 1. overeni tokenu ------------------------------------------------- #
    resp = requests.get(f"{TELEGRAM_API}/bot{token}/getMe", timeout=20).json()
    if not resp.get("ok"):
        print(f"Token Telegram neuznava: {resp.get('description')}")
        print("Vezmi aktualni token z @BotFather (/mybots -> bot -> API Token)")
        print("a vloz ho do .env jako TELEGRAM_BOT_TOKEN.")
        return 2
    bot = resp["result"]
    print(f"Bot overen: @{bot.get('username')}")

    # --- 2. hledani chatu -------------------------------------------------- #
    updates = requests.get(
        f"{TELEGRAM_API}/bot{token}/getUpdates", timeout=20
    ).json().get("result", [])

    chats: dict[int, str] = {}
    for update in updates:
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            payload = update.get(key)
            if not payload or not payload.get("chat"):
                continue
            chat = payload["chat"]
            name = (
                chat.get("title")
                or " ".join(filter(None, [chat.get("first_name"),
                                          chat.get("last_name")]))
                or chat.get("username")
                or "?"
            )
            chats[chat["id"]] = f"{name} ({chat.get('type')})"

    if not chats:
        print("\nZadny chat nenalezen.")
        print("Napis svemu botovi v Telegramu libovolnou zpravu (napr. /start)")
        print("a spust tenhle skript znovu. Bot nesmi psat jako prvni.")
        return 1

    if len(chats) > 1:
        print("\nNalezeno vic chatu:")
        for chat_id, label in chats.items():
            print(f"  {chat_id}  {label}")
        print("Vyber jeden a zapis ho do .env jako TELEGRAM_CHAT_ID rucne.")
        return 1

    chat_id, label = next(iter(chats.items()))
    print(f"Chat nalezen: {chat_id}  {label}")

    # --- 3. zapis do .env -------------------------------------------------- #
    write_env_value("TELEGRAM_CHAT_ID", str(chat_id))
    print(f"Zapsano do .env: TELEGRAM_CHAT_ID={chat_id}")

    # --- 4. testovaci zprava ----------------------------------------------- #
    text = (
        "✅ *Bet Agent je propojeny*\n\n"
        "Tohle je testovaci zprava\\. Od ted sem chodi denni tip nebo NO\\_BET\\.\n"
        "Rezim: all\\-in, cil 2x do 7 vyher v rade\\."
    )
    send = requests.post(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
        timeout=20,
    ).json()
    if send.get("ok"):
        print("\nTestovaci zprava odeslana -- podivej se do Telegramu.")
        print("Ted uz muzes spustit:  python main.py --scan")
        return 0
    print(f"\nOdeslani selhalo: {send.get('description')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
