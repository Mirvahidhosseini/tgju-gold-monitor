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
WORLD_GOLD_URL = "https://api.gold-api.com/price/XAU"

STATE_FILE = Path("last_price.json")
ALERT_THRESHOLD = 0.5  # درصد تغییر برای هشدار


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

    if price_rial < 1_000_000:
        raise RuntimeError("قیمت دریافتی از TGJU غیرعادی است.")

    return round(price_rial / 10)


def fetch_world_gold_usd():
    response = requests.get(WORLD_GOLD_URL, timeout=20)
    response.raise_for_status()

    data = response.json()
    price = float(data["price"])

    if price <= 0:
        raise RuntimeError("قیمت جهانی دریافتی نامعتبر است.")

    return price, data.get("updatedAtReadable", "")


def load_previous_state():
    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        # پشتیبانی از فایل قدیمی ربات
        if "iran_price_toman" not in data and "price_toman" in data:
            data["iran_price_toman"] = data["price_toman"]

        return data

    except (ValueError, TypeError, json.JSONDecodeError):
        return {}


def add_change_message(lines, label, current, previous):
    if previous is None or previous <= 0:
        lines.append(f"ℹ️ تغییر {label}: سابقه کافی نداریم.")
        return

    change = (current - previous) / previous * 100
    lines.append(f"📊 تغییر {label}: {change:+.3f}%")

    if abs(change) >= ALERT_THRESHOLD:
        direction = "افزایش" if change > 0 else "کاهش"
        lines.append(
            f"🚨 هشدار {direction} {label} "
            f"(آستانه {ALERT_THRESHOLD}٪)"
        )


def main():
    now = datetime.now(ZoneInfo("Asia/Tehran"))
    previous = load_previous_state()

    lines = [
        "🟡 گزارش پایش طلا",
        f"🕒 زمان بررسی: {now:%Y-%m-%d %H:%M}",
        "",
    ]

    new_state = dict(previous)

    # قیمت طلای ۱۸ عیار ایران
    try:
        iran_price = fetch_price_toman()
        lines.extend([
            "🇮🇷 طلای ۱۸ عیار ایران",
            f"💰 هر گرم: {iran_price:,} تومان",
        ])

        old_iran = previous.get("iran_price_toman")
        add_change_message(
            lines, "طلای ایران", iran_price,
            float(old_iran) if old_iran is not None else None,
        )

        new_state["iran_price_toman"] = iran_price

    except Exception as error:
        lines.extend([
            "🇮🇷 طلای ایران",
            f"⚠️ دریافت قیمت ناموفق بود: {error}",
        ])

    lines.append("")

    # قیمت جهانی طلا
    try:
        world_price, updated = fetch_world_gold_usd()
        lines.extend([
            "🌍 طلای جهانی (XAU/USD)",
            f"💵 هر اونس تروا: ${world_price:,.2f}",
        ])

        if updated:
            lines.append(f"🕓 به‌روزرسانی منبع: {updated}")

        old_world = previous.get("world_gold_usd")
        add_change_message(
            lines, "طلای جهانی", world_price,
            float(old_world) if old_world is not None else None,
        )

        new_state["world_gold_usd"] = world_price

    except Exception as error:
        lines.extend([
            "🌍 طلای جهانی",
            f"⚠️ دریافت قیمت ناموفق بود: {error}",
        ])

    # گزارش ارسال شود حتی اگر یکی از منابع خطا بدهد
    send_message("\n".join(lines))

    new_state["checked_at"] = now.isoformat()
    STATE_FILE.write_text(
        json.dumps(new_state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
