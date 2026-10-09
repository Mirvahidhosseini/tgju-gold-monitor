
import os
import re
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

TGJU_URL = "https://www.tgju.org/profile/geram18"
STATE_FILE = Path("last_price.json")
ALERT_THRESHOLD = 0.5  # درصد تغییر برای هشدار شدید


def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )
    response.raise_for_status()


def normalize_digits(value):
    persian = "۰۱۲۳۴۵۶۷۸۹"
    arabic = "٠١٢٣٤٥٦٧٨٩"
    for i, digit in enumerate(persian):
        value = value.replace(digit, str(i))
    for i, digit in enumerate(arabic):
        value = value.replace(digit, str(i))
    return value


def fetch_price_toman():
    response = requests.get(
        TGJU_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    pattern = r"نرخ\s*فعلی\s*[:：]*\s*([0-9۰-۹٠-٩,٬]+)"
    match = re.search(pattern, page_text)

    if not match:
        raise RuntimeError("قیمت در صفحه TGJU پیدا نشد.")

    digits = normalize_digits(match.group(1))
    price_rial = int(digits.replace(",", "").replace("٬", ""))

    # کنترل اولیه برای جلوگیری از گزارش عدد نامعتبر
    if price_rial < 1_000_000:
        raise RuntimeError("قیمت دریافتی غیرعادی است؛ گزارش متوقف شد.")

    return round(price_rial / 10)


def load_previous_price():
    if not STATE_FILE.exists():
        return None

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return int(data["price_toman"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def main():
    now = datetime.now(ZoneInfo("Asia/Tehran"))
    price = fetch_price_toman()
    previous = load_previous_price()

    lines = [
        "🟡 پایش قیمت طلای ۱۸ عیار",
        f"💰 قیمت هر گرم: {price:,} تومان",
        f"🕒 زمان بررسی: {now:%Y-%m-%d %H:%M}",
        "📍 منبع: TGJU",
    ]

    if previous and previous > 0:
        change = (price - previous) / previous * 100
        lines.append(f"📊 تغییر نسبت به بررسی قبلی: {change:+.3f}%")

        if abs(change) >= ALERT_THRESHOLD:
            direction = "افزایش" if change > 0 else "کاهش"
            lines.insert(0, f"🚨 هشدار {direction} شدید قیمت")
            lines.append(
                f"⚠️ تغییر از آستانه {ALERT_THRESHOLD}% عبور کرده است."
            )
    else:
        lines.append("ℹ️ این اولین بررسی است؛ تغییر قبلی موجود نیست.")

    send_message("\n".join(lines))

    STATE_FILE.write_text(
        json.dumps(
            {"price_toman": price, "checked_at": now.isoformat()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
