
import os
import requests
from datetime import datetime, timezone

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20
    )
    response.raise_for_status()

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    send_message(
        "🤖 ربات پایش طلای ۱۸ عیار راه‌اندازی شده است.\n"
        f"زمان آزمایش: {now}\n"
        "مرحله بعد: اتصال امن به داده‌های قیمت TGJU."
    )

if __name__ == "__main__":
    main()
