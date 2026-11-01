#!/usr/bin/env python3
"""DEGEN/USD swing signal bot. Signals and paper trading only; never places orders."""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PRODUCT = "DEGEN-USD"
BASE_URL = "https://api.exchange.coinbase.com"
STATE_FILE = Path("degen_state.json")
CAPITAL_USD = 10.0
RISK_FRACTION = 0.01
CANDLE_SECONDS = 4 * 60 * 60
HOURLY_SECONDS = 60 * 60
HISTORY_HOURS = 900
CHUNK_HOURS = 250
HEADERS = {"User-Agent": "DEGEN-Swing-Signal-Bot/1.0", "Accept": "application/json"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def api_get(path, params=None):
    response = SESSION.get(BASE_URL + path, params=params, timeout=25)
    if response.status_code == 429:
        time.sleep(3)
        response = SESSION.get(BASE_URL + path, params=params, timeout=25)
    response.raise_for_status()
    return response.json()


def iso_utc(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def verify_product():
    product = api_get(f"/products/{PRODUCT}")
    status = str(product.get("status", "")).lower()
    if status != "online" or product.get("trading_disabled") is True:
        raise RuntimeError(
            f"{PRODUCT} is not confirmed online on Coinbase Exchange "
            f"(status={status!r}, trading_disabled={product.get('trading_disabled')!r})."
        )
    return product


def fetch_hourly_candles():
    now = int(time.time())
    end = (now // HOURLY_SECONDS) * HOURLY_SECONDS
    start = end - HISTORY_HOURS * HOURLY_SECONDS
    candles = {}
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + CHUNK_HOURS * HOURLY_SECONDS, end)
        raw = api_get(
            f"/products/{PRODUCT}/candles",
            params={
                "granularity": 3600,
                "start": iso_utc(cursor),
                "end": iso_utc(chunk_end),
            },
        )
        for item in raw:
            if not isinstance(item, list) or len(item) < 6:
                continue
            ts, low, high, open_, close, volume = item[:6]
            candles[int(ts)] = {
                "time": int(ts), "open": float(open_), "high": float(high),
                "low": float(low), "close": float(close), "volume": float(volume),
            }
        cursor = chunk_end
    ordered = [candles[key] for key in sorted(candles)]
    if len(ordered) < 250:
        raise RuntimeError(f"Insufficient hourly history from Coinbase: {len(ordered)} candles.")
    return ordered


def aggregate_4h(hourly):
    buckets = {}
    for candle in hourly:
        bucket = (candle["time"] // CANDLE_SECONDS) * CANDLE_SECONDS
        buckets.setdefault(bucket, []).append(candle)
    current_bucket = (int(time.time()) // CANDLE_SECONDS) * CANDLE_SECONDS
    result = []
    for bucket in sorted(buckets):
        if bucket >= current_bucket:
            continue
        rows = sorted(buckets[bucket], key=lambda row: row["time"])
        if len(rows) != 4:
            continue
        result.append({
            "time": bucket, "open": rows[0]["open"],
            "high": max(row["high"] for row in rows),
            "low": min(row["low"] for row in rows),
            "close": rows[-1]["close"],
            "volume": sum(row["volume"] for row in rows),
        })
    if len(result) < 70:
        raise RuntimeError(f"Insufficient completed 4-hour candles: {len(result)}.")
    return result


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    current = sum(values[:period]) / period
    out[period - 1] = current
    multiplier = 2.0 / (period + 1)
    for i in range(period, len(values)):
        current = values[i] * multiplier + current * (1.0 - multiplier)
        out[i] = current
    return out


def rsi(values, period=14):
    out = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def atr(candles, period=14):
    trs = [None] * len(candles)
    for i in range(1, len(candles)):
        row, prev = candles[i], candles[i - 1]
        trs[i] = max(
            row["high"] - row["low"],
            abs(row["high"] - prev["close"]),
            abs(row["low"] - prev["close"]),
        )
    out = [None] * len(candles)
    if len(candles) <= period:
        return out
    current = sum(trs[1:period + 1]) / period
    out[period] = current
    for i in range(period + 1, len(candles)):
        current = (current * (period - 1) + trs[i]) / period
        out[i] = current
    return out


def analyse(candles):
    closes = [row["close"] for row in candles]
    volumes = [row["volume"] for row in candles]
    ema20, ema50 = ema(closes, 20), ema(closes, 50)
    rsi14, atr14 = rsi(closes, 14), atr(candles, 14)
    i = len(candles) - 1
    if any(series[i] is None for series in (ema20, ema50, rsi14, atr14)) or rsi14[i - 1] is None:
        raise RuntimeError("Indicators are not ready; more candle history is required.")
    avg_volume = sum(volumes[i - 20:i]) / 20
    row = candles[i]
    checks = {
        "uptrend": ema20[i] > ema50[i],
        "close_above_ema20": row["close"] > ema20[i],
        "rsi_50_to_68_and_rising": 50 <= rsi14[i] <= 68 and rsi14[i] > rsi14[i - 1],
        "volume_10pct_above_average": avg_volume > 0 and row["volume"] >= avg_volume * 1.10,
    }
    exit_reasons = []
    if row["close"] < ema20[i]:
        exit_reasons.append("close below EMA20")
    if rsi14[i] < 45:
        exit_reasons.append("RSI below 45")
    if ema20[i] < ema50[i]:
        exit_reasons.append("EMA20 below EMA50")
    return {
        "candle": row, "ema20": ema20[i], "ema50": ema50[i],
        "rsi": rsi14[i], "atr": atr14[i], "avg_volume": avg_volume,
        "volume_ratio": row["volume"] / avg_volume if avg_volume else 0,
        "buy": all(checks.values()), "exit": bool(exit_reasons),
        "exit_reasons": exit_reasons, "checks": checks,
    }


def load_state():
    if not STATE_FILE.exists():
        return {"capital_usd": CAPITAL_USD, "realized_pnl_usd": 0.0,
                "paper_position": None, "last_candle_time": None}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("State must be a JSON object.")
        state.setdefault("capital_usd", CAPITAL_USD)
        state.setdefault("realized_pnl_usd", 0.0)
        state.setdefault("paper_position", None)
        state.setdefault("last_candle_time", None)
        return state
    except (OSError, json.JSONDecodeError, ValueError):
        raise RuntimeError("degen_state.json is unreadable; refusing to reset paper-trading state.")


def save_state(state):
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)


def fmt_price(value):
    return f"${value:.10f}"


def send_telegram(message):
    token, chat_id = os.environ.get("BOT_TOKEN"), os.environ.get("CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Missing BOT_TOKEN or CHAT_ID repository secrets.")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


def open_paper_position(state, analysis):
    entry = analysis["candle"]["close"]
    stop = entry - 1.5 * analysis["atr"]
    if stop <= 0 or stop >= entry:
        return None
    equity = max(0.0, CAPITAL_USD + float(state["realized_pnl_usd"]))
    risk_budget = equity * RISK_FRACTION
    units = min(risk_budget / (entry - stop), equity / entry if entry > 0 else 0)
    if units <= 0:
        return None
    return {
        "entry": entry, "stop": stop, "initial_stop": stop,
        "target1": entry + 2.0 * (entry - stop),
        "target2": entry + 3.0 * (entry - stop),
        "initial_units": units, "remaining_units": units,
        "target1_done": False, "opened_at": iso_utc(analysis["candle"]["time"]),
        "risk_usd": units * (entry - stop), "realized_position_pnl_usd": 0.0,
    }


def close_remaining(state, position, price, reason):
    units = float(position["remaining_units"])
    pnl = (price - float(position["entry"])) * units
    state["realized_pnl_usd"] = float(state["realized_pnl_usd"]) + pnl
    position["realized_position_pnl_usd"] = float(position["realized_position_pnl_usd"]) + pnl
    total = float(position["realized_position_pnl_usd"])
    state["paper_position"] = None
    equity = CAPITAL_USD + float(state["realized_pnl_usd"])
    return (
        f"🔴 خروج از معامله فرضی DEGEN/USD\n"
        f"علت: {reason}\n"
        f"قیمت خروج بخش باقی‌مانده: {fmt_price(price)}\n"
        f"سود/زیان کل این معامله: {total:+.4f} USD\n"
        f"سود/زیان تحقق‌یافته تجمعی: {float(state['realized_pnl_usd']):+.4f} USD\n"
        f"ارزش فرضی سرمایه: {equity:.4f} USD\n"
        f"کارمزد و لغزش قیمت در این محاسبه لحاظ نشده‌اند."
    )


def process_paper_trade(state, analysis):
    position, candle = state.get("paper_position"), analysis["candle"]
    if position:
        low, high, close = candle["low"], candle["high"], candle["close"]
        # If stop and target are both touched in one OHLC candle, assume stop first.
        if low <= float(position["stop"]):
            return close_remaining(state, position, float(position["stop"]), "حد ضرر")
        if not position["target1_done"] and high >= float(position["target1"]):
            units_to_sell = min(float(position["initial_units"]) * 0.5,
                                float(position["remaining_units"]))
            pnl = (float(position["target1"]) - float(position["entry"])) * units_to_sell
            state["realized_pnl_usd"] = float(state["realized_pnl_usd"]) + pnl
            position["realized_position_pnl_usd"] = float(position["realized_position_pnl_usd"]) + pnl
            position["remaining_units"] = float(position["remaining_units"]) - units_to_sell
            position["target1_done"] = True
            position["stop"] = float(position["entry"])
            return (
                f"🟡 هدف اول DEGEN/USD لمس شد\n"
                f"فروش فرضی ۵۰٪ در {fmt_price(float(position['target1']))}\n"
                f"حد ضرر بخش باقی‌مانده به نقطه ورود منتقل شد: {fmt_price(float(position['entry']))}\n"
                f"سود تحقق‌یافته این بخش: {pnl:+.4f} USD"
            )
        if position["target1_done"] and high >= float(position["target2"]):
            return close_remaining(state, position, float(position["target2"]), "هدف دوم")
        if analysis["exit"]:
            return close_remaining(state, position, close, "; ".join(analysis["exit_reasons"]))
        return None

    if analysis["buy"]:
        position = open_paper_position(state, analysis)
        if position is None:
            return None
        state["paper_position"] = position
        return (
            f"🟢 سیگنال خرید DEGEN/USD — معامله کاغذی\n"
            f"زمان کندل ۴ ساعته: {position['opened_at']}\n"
            f"ورود فرضی: {fmt_price(position['entry'])}\n"
            f"حد ضرر: {fmt_price(position['stop'])}\n"
            f"هدف اول (۲R): {fmt_price(position['target1'])}\n"
            f"هدف دوم (۳R): {fmt_price(position['target2'])}\n"
            f"اندازه موقعیت: {position['initial_units']:.2f} DEGEN\n"
            f"ارزش موقعیت: ${position['initial_units'] * position['entry']:.2f}\n"
            f"ریسک محاسباتی: ${position['risk_usd']:.3f} (۱٪ سرمایه فرضی)\n"
            f"سرمایه مبنا: ${CAPITAL_USD:.2f}\n"
            f"این فقط سیگنال و معامله شبیه‌سازی‌شده است؛ سفارش واقعی ثبت نشده است."
        )
    return None


def main():
    product = verify_product()
    hourly = fetch_hourly_candles()
    candles = aggregate_4h(hourly)
    analysis = analyse(candles)
    state = load_state()
    candle_time = iso_utc(analysis["candle"]["time"])
    print(
        f"{PRODUCT} status={product.get('status')} candles={len(candles)} "
        f"close={analysis['candle']['close']:.10f} EMA20={analysis['ema20']:.10f} "
        f"EMA50={analysis['ema50']:.10f} RSI={analysis['rsi']:.2f} "
        f"volume_ratio={analysis['volume_ratio']:.2f}"
    )
    print("Checks:", json.dumps(analysis["checks"]))
    if state.get("last_candle_time") == candle_time:
        print("No new completed 4-hour candle; skipping duplicate evaluation.")
        return
    message = process_paper_trade(state, analysis)
    state["last_candle_time"] = candle_time
    state["last_analysis"] = {
        "candle_time": candle_time, "close": analysis["candle"]["close"],
        "ema20": analysis["ema20"], "ema50": analysis["ema50"],
        "rsi14": analysis["rsi"], "atr14": analysis["atr"],
        "volume_ratio": analysis["volume_ratio"], "checks": analysis["checks"],
        "decision": "BUY" if analysis["buy"] else ("EXIT" if analysis["exit"] else "WAIT"),
    }
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    if message:
        send_telegram(message)
        print("Telegram alert sent.")
    else:
        print("No new actionable signal; no Telegram alert.")
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"DEGEN signal bot failed: {exc}", file=sys.stderr)
        raise
