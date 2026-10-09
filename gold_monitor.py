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
ALERT_THRESHOLD = 0.5


def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )
    response.raise_for_status()


def normalize_digits(value):
    for i, digit in enumerate("۰۱۲۳۴۵۶۷۸۹"):
        value = value.replace(digit, str(i))
    for i, digit in enumerate("٠١٢٣٤٥٦٧٨٩"):
        value = value.replace(digit, str(i))
    return value


def fetch_iran_price():
    response = requests.get(
        TGJU_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    match = re.search(
        r"نرخ\s*فعلی\s*[:：]*\s*([0-9۰-۹٠-٩,٬]+)",
        page_text,
    )
    if not match:
        raise RuntimeError("قیمت TGJU پیدا نشد.")

    digits = normalize_digits(match.group(1))
    rial = int(digits.replace(",", "").replace("٬", ""))

    if rial < 1_000_000:
        raise RuntimeError("قیمت دریافتی غیرعادی است.")

    return round(rial / 10)


def fetch_world_price():
    response = requests.get(WORLD_GOLD_URL, timeout=20)
    response.raise_for_status()
    data = response.json()
    price = float(data["price"])

    if price <= 0:
        raise RuntimeError("قیمت جهانی نامعتبر است.")

    return price


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if "iran_price_toman" not in state and "price_toman" in state:
            state["iran_price_toman"] = state["price_toman"]
        return state
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def direction(value, previous):
    if previous is None or previous <= 0:
        return "⚪"
    if value > previous:
        return "🟢"
    if value < previous:
        return "🔴"
    return "⚪"


def change_percent(value, previous):
    if previous is None or previous <= 0:
        return None
    return (value - previous) / previous * 100


def price_line(label, value, previous, unit, decimals=0):
    icon = direction(value, previous)
    if decimals:
        shown = f"{value:,.{decimals}f}"
    else:
        shown = f"{value:,.0f}"

    lines = [f"{icon} {label}: {shown} {unit}"]

    change = change_percent(value, previous)
    if change is not None:
        lines.append(f"   تغییر قبلی: {change:+.3f}%")
        if abs(change) >= ALERT_THRESHOLD:
            side = "افزایش" if change > 0 else "کاهش"
            lines.append(
                f"   🚨 هشدار {side}؛ عبور از {ALERT_THRESHOLD}%"
            )
    else:
        lines.append("   ℹ️ سابقه کافی برای مقایسه نداریم.")

    return lines


def update_daily_stats(stats, date_key, iran, world):
    if stats.get("date") != date_key:
        stats = {
            "date": date_key,
            "iran": None,
            "world": None,
        }

    for key, value in (("iran", iran), ("world", world)):
        if value is None:
            continue

        old = stats.get(key)
        if not old:
            stats[key] = {
                "open": value,
                "high": value,
                "low": value,
                "last": value,
            }
        else:
            old["high"] = max(old["high"], value)
            old["low"] = min(old["low"], value)
            old["last"] = value

    return stats


def daily_summary(stats):
    lines = [
        f"📊 خلاصه روزانه طلا | {stats.get('date', '')}",
        "قیمت‌ها بر اساس بررسی‌های ثبت‌شده ربات هستند.",
        "",
    ]

    for key, title, unit, decimals in (
        ("iran", "🇮🇷 طلای ۱۸ عیار", "تومان", 0),
        ("world", "🌍 طلای جهانی", "دلار/اونس", 2),
    ):
        item = stats.get(key)
        if not item:
            lines.extend([title, "داده کافی ثبت نشده است.", ""])
            continue

        def fmt(v):
            return f"{v:,.{decimals}f}"

        change = change_percent(item["last"], item["open"])
        lines.extend([
            title,
            f"🔹 آغاز روز: {fmt(item['open'])} {unit}",
            f"⬆️ بیشترین: {fmt(item['high'])} {unit}",
            f"⬇️ کمترین: {fmt(item['low'])} {unit}",
            f"🔚 آخرین ثبت: {fmt(item['last'])} {unit}",
            (
                f"📈 تغییر روزانه: {change:+.3f}%"
                if change is not None
                else "📈 تغییر روزانه: نامشخص"
            ),
            "",
        ])

    return "\n".join(lines)


def main():
    now = datetime.now(ZoneInfo("Asia/Tehran"))
    today = now.strftime("%Y-%m-%d")
    state = load_state()

    previous_iran = state.get("iran_price_toman")
    previous_world = state.get("world_gold_usd")

    try:
        previous_iran = (
            float(previous_iran) if previous_iran is not None else None
        )
    except (TypeError, ValueError):
        previous_iran = None

    try:
        previous_world = (
            float(previous_world) if previous_world is not None else None
        )
    except (TypeError, ValueError):
        previous_world = None

    # قیمت‌ها را جداگانه دریافت می‌کنیم تا خطای یکی مانع دیگری نشود.
    iran = None
    world = None
    errors = []

    try:
        iran = fetch_iran_price()
    except Exception as exc:
        errors.append(f"🇮🇷 خطای دریافت قیمت ایران: {exc}")

    try:
        world = fetch_world_price()
    except Exception as exc:
        errors.append(f"🌍 خطای دریافت قیمت جهانی: {exc}")

    # گزارش خلاصه روز قبل هنگام اولین اجرای روز جدید
    old_daily = state.get("daily", {})
    if old_daily.get("date") and old_daily.get("date") != today:
        send_message(daily_summary(old_daily))

    daily = update_daily_stats(old_daily, today, iran, world)

    lines = [
        "🟡 گزارش پایش طلا",
        f"🕒 زمان بررسی: {now:%Y-%m-%d %H:%M}",
        "",
    ]

    if iran is not None:
        lines.append("🇮🇷 طلای ۱۸ عیار ایران")
        lines.extend(
            price_line(
                "هر گرم",
                iran,
                previous_iran,
                "تومان",
            )
        )
    else:
        lines.append("🇮🇷 قیمت ایران دریافت نشد.")

    lines.append("")

    if world is not None:
        lines.append("🌍 طلای جهانی (XAU/USD)")
        lines.extend(
            price_line(
                "هر اونس تروا",
                world,
                previous_world,
                "دلار",
                decimals=2,
            )
        )
    else:
        lines.append("🌍 قیمت جهانی دریافت نشد.")

    lines.extend([
        "",
        "📅 آمار روز جاری (بر اساس ثبت‌های ربات)",
    ])

    for key, title, unit, decimals in (
        ("iran", "ایران", "تومان", 0),
        ("world", "جهانی", "دلار", 2),
    ):
        item = daily.get(key)
        if not item:
            continue

        fmt = lambda v: f"{v:,.{decimals}f}"
        change = change_percent(item["last"], item["open"])

        lines.append(
            f"{title}: آغاز {fmt(item['open'])}، "
            f"بیشینه {fmt(item['high'])}، "
            f"کمینه {fmt(item['low'])} {unit}"
        )
        if change is not None:
            lines.append(f"   تغییر روزانه: {change:+.3f}%")

    if errors:
        lines.extend(["", *errors])

    # فقط قیمت‌های موفق به‌عنوان آخرین قیمت ثبت می‌شوند.
    if iran is not None:
        state["iran_price_toman"] = iran
    if world is not None:
        state["world_gold_usd"] = world

    state["daily"] = daily
    state["checked_at"] = now.isoformat()

    send_message("\n".join(lines))
    save_state(state)


if __name__ == "__main__":
    main()
