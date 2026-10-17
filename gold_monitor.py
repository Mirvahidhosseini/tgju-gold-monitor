
import os
import re
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# =========================
# تنظیمات اصلی
# =========================

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

IRAN_GOLD_URL = "https://www.tgju.org/gold-chart"
TGJU_HOME_URL = "https://www.tgju.org/home"
WORLD_GOLD_URL = "https://api.gold-api.com/price/XAU"

STATE_FILE = Path("last_price.json")
HISTORY_FILE = Path("price_history.json")
HISTORY_LIMIT = 2000
ALERT_THRESHOLD = 0.5
TEHRAN = ZoneInfo("Asia/Tehran")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
}


# =========================
# ابزارهای کمکی
# =========================

def normalize_digits(text):
    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )
    return text.translate(table)


def normalize_label(text):
    text = normalize_digits(text).replace("\u200c", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def parse_number(text):
    text = normalize_digits(text)
    digits = re.sub(r"[^0-9]", "", text)

    if not digits:
        raise RuntimeError(f"عدد قابل خواندن نیست: {text!r}")

    return int(digits)


def get_page(url):
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def find_table_price(soup, label_match, minimum):
    """
    ردیف موردنظر را در جدول پیدا می‌کند.
    مقدار ستون دوم، یعنی قیمت زنده، خوانده می‌شود.
    """
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)

        if len(cells) < 2:
            continue

        label = normalize_label(cells[0].get_text(" ", strip=True))

        if not label_match(label):
            continue

        price_text = cells[1].get_text(" ", strip=True)
        price = parse_number(price_text)

        if price < minimum:
            raise RuntimeError(
                f"قیمت غیرعادی برای {label}: {price_text}"
            )

        return price

    raise RuntimeError("ردیف قیمت موردنظر در جدول پیدا نشد.")


# =========================
# قیمت طلای ۱۸ عیار ایران
# =========================

def fetch_iran_price():
    soup = get_page(IRAN_GOLD_URL)

    rial = find_table_price(
        soup,
        lambda label: (
            "18" in label
            and "750" in label
            and "عیار" in label
        ),
        minimum=1_000_000,
    )

    # قیمت سایت ریال است؛ گزارش ربات تومان خواهد بود.
    return round(rial / 10)


# =========================
# قیمت دلار آزاد
# =========================

def fetch_dollar_price():
    soup = get_page(TGJU_HOME_URL)

    rial = find_table_price(
        soup,
        lambda label: label == "دلار",
        minimum=100_000,
    )

    # فقط ردیف «دلار» در جدول ارز آزاد خوانده می‌شود.
    return round(rial / 10)


# =========================
# قیمت جهانی طلا
# =========================

def fetch_world_price():
    response = requests.get(
        WORLD_GOLD_URL,
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    price = float(data["price"])

    if price <= 0:
        raise RuntimeError("قیمت جهانی طلا غیرعادی است.")

    return price


# =========================
# خواندن و ذخیره وضعیت
# =========================

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)
            return state if isinstance(state, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2,
        )

def save_history(now, prices):
    """ذخیره تاریخچه قیمت‌ها در فایل JSON."""
    if not any(value is not None for value in prices.values()):
        return

    history = []

    if HISTORY_FILE.exists():
        try:
            with HISTORY_FILE.open("r", encoding="utf-8") as file:
                loaded = json.load(file)
                if isinstance(loaded, list):
                    history = loaded
        except (json.JSONDecodeError, OSError):
            history = []

    history.append({
        "timestamp": now.isoformat(),
        "iran_toman": prices.get("iran"),
        "world_usd": prices.get("world"),
        "dollar_toman": prices.get("dollar"),
    })

    history = history[-HISTORY_LIMIT:]

    with HISTORY_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            history,
            file,
            ensure_ascii=False,
            indent=2,
        )


# =========================
# تغییر قیمت و جهت حرکت
# =========================

def get_change(current, previous):
    if previous is None or previous == 0:
        return None

    return (current - previous) / previous * 100


def direction(change):
    if change is None or abs(change) < 0.0000001:
        return "⚪"

    return "🟢" if change > 0 else "🔴"


def format_change(change):
    if change is None:
        return "اولین ثبت"

    return f"{change:+.3f}%"


def format_price(value, decimals=0):
    if decimals:
        return f"{value:,.{decimals}f}"
    return f"{int(round(value)):,}"


def format_market(
    title,
    unit,
    current,
    previous,
    decimals=0,
):
    change = get_change(current, previous)
    icon = direction(change)

    lines = [
        title,
        f"{icon} قیمت فعلی: {format_price(current, decimals)} {unit}",
        f"   تغییر نسبت به بررسی قبلی: {format_change(change)}",
    ]

    if change is not None and abs(change) >= ALERT_THRESHOLD:
        if change > 0:
            lines.append(
                f"🚨 هشدار: افزایش {abs(change):.3f}%"
            )
        else:
            lines.append(
                f"🚨 هشدار: کاهش {abs(change):.3f}%"
            )

    return "\n".join(lines)


# =========================
# آمار روزانه
# =========================

def update_daily_stats(state, today, prices):
    daily = state.get("daily", {})

    if daily.get("date") != today:
        daily = {
            "date": today,
            "iran": {},
            "world": {},
            "dollar": {},
        }

    for key, price in prices.items():
        if price is None:
            continue

        stats = daily.setdefault(key, {})

        if "open" not in stats:
            stats["open"] = price
            stats["high"] = price
            stats["low"] = price
        else:
            stats["high"] = max(stats["high"], price)
            stats["low"] = min(stats["low"], price)

        stats["last"] = price

    state["daily"] = daily


def daily_market_line(name, stats, unit, decimals=0):
    if not stats or "open" not in stats:
        return f"{name}: هنوز داده‌ای ثبت نشده است."

    return (
        f"{name}: آغاز {format_price(stats['open'], decimals)}، "
        f"بیشینه {format_price(stats['high'], decimals)}، "
        f"کمینه {format_price(stats['low'], decimals)} {unit}"
    )


def build_daily_summary(daily):
    return "\n".join([
        f"📅 آمار روزانه ({daily.get('date', '-')})",
        daily_market_line(
            "🇮🇷 ایران",
            daily.get("iran", {}),
            "تومان",
        ),
        daily_market_line(
            "💵 دلار آزاد",
            daily.get("dollar", {}),
            "تومان",
        ),
        daily_market_line(
            "🌍 طلای جهانی",
            daily.get("world", {}),
            "دلار",
            decimals=2,
        ),
    ])


# =========================
# ارسال پیام تلگرام
# =========================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )


# =========================
# اجرای اصلی ربات
# =========================

def main():
    state = load_state()
    now = datetime.now(TEHRAN)
    today = now.strftime("%Y-%m-%d")

    previous_daily = state.get("daily", {})
    previous_date = previous_daily.get("date")

    previous = {
        "iran": state.get(
            "iran_toman",
            state.get("price_toman"),
        ),
        "world": state.get("world_usd"),
        "dollar": state.get("dollar_toman"),
    }

    current = {
        "iran": None,
        "world": None,
        "dollar": None,
    }

    errors = []

    # هر منبع مستقل است تا خرابی یکی مانع دریافت بقیه نشود.
    fetchers = [
        ("iran", "طلای ۱۸ عیار ایران", fetch_iran_price),
        ("world", "طلای جهانی", fetch_world_price),
        ("dollar", "دلار آزاد", fetch_dollar_price),
    ]

    for key, name, fetcher in fetchers:
        try:
            current[key] = fetcher()
        except Exception as exc:
            errors.append(f"⚠️ خطا در دریافت {name}: {exc}")

    update_daily_stats(state, today, current)

    lines = [
        "🟡 گزارش پایش طلا و دلار",
        f"🕒 زمان بررسی: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    if current["iran"] is not None:
        lines.append(
            format_market(
                "🇮🇷 طلای ۱۸ عیار ایران",
                "تومان/گرم",
                current["iran"],
                previous["iran"],
            )
        )
        lines.append("")

    if current["world"] is not None:
        lines.append(
            format_market(
                "🌍 طلای جهانی (XAU/USD)",
                "دلار/اونس",
                current["world"],
                previous["world"],
                decimals=2,
            )
        )
        lines.append("")

    if current["dollar"] is not None:
        lines.append(
            format_market(
                "💵 دلار آزاد TGJU",
                "تومان",
                current["dollar"],
                previous["dollar"],
            )
        )
        lines.append("")

    lines.append("📊 آمار روز جاری")
    daily = state.get("daily", {})

    lines.append(
        daily_market_line(
            "🇮🇷 طلای ۱۸ عیار",
            daily.get("iran", {}),
            "تومان",
        )
    )
    lines.append(
        daily_market_line(
            "💵 دلار آزاد",
            daily.get("dollar", {}),
            "تومان",
        )
    )
    lines.append(
        daily_market_line(
            "🌍 طلای جهانی",
            daily.get("world", {}),
            "دلار",
            decimals=2,
        )
    )

    # اگر تاریخ عوض شده باشد، خلاصه روز قبل نیز ارسال می‌شود.
    if (
        previous_date
        and previous_date != today
        and previous_daily
    ):
        lines.extend([
            "",
            "📆 خلاصه روز قبل",
            build_daily_summary(previous_daily),
        ])

    if errors:
        lines.extend(["", *errors])

    message = "\n".join(lines)

    # ابتدا پیام را می‌فرستیم؛ بعد وضعیت را ذخیره می‌کنیم.
    send_telegram(message)

    if current["iran"] is not None:
        state["iran_toman"] = current["iran"]
        state["price_toman"] = current["iran"]

    if current["world"] is not None:
        state["world_usd"] = current["world"]

    if current["dollar"] is not None:
        state["dollar_toman"] = current["dollar"]

    state["last_checked"] = now.isoformat()
    save_state(state)
    save_history(now, current)
    
    print("گزارش با موفقیت ارسال و وضعیت ذخیره شد.")

if __name__ == "__main__":
    main()
