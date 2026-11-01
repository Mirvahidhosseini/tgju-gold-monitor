#!/usr/bin/env python3
"""Compare two independent DEGEN/USD strategies against the existing baseline."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import degen_signal as live

PRODUCT = live.PRODUCT
BASE_URL = live.BASE_URL
DAYS = 365
FEE = 0.001
SLIPPAGE = 0.001
INITIAL_CAPITAL = 10.0
RISK_FRACTION = 0.01
OUT = Path("degen_strategy_lab_report.json")
SESSION = requests.Session()
SESSION.headers.update(live.HEADERS)


def get(path, params=None):
    response = SESSION.get(BASE_URL + path, params=params, timeout=30)
    if response.status_code == 429:
        time.sleep(3)
        response = SESSION.get(BASE_URL + path, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_candles():
    end = (int(time.time()) // 3600) * 3600
    start = end - DAYS * 24 * 3600
    data = {}
    cursor = start
    while cursor < end:
        stop = min(cursor + 250 * 3600, end)
        rows = get(f"/products/{PRODUCT}/candles", {
            "granularity": 3600,
            "start": datetime.fromtimestamp(cursor, timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(stop, timezone.utc).isoformat(),
        })
        for row in rows:
            if isinstance(row, list) and len(row) >= 6:
                ts, low, high, op, close, volume = row[:6]
                data[int(ts)] = {"time": int(ts), "open": float(op),
                    "high": float(high), "low": float(low),
                    "close": float(close), "volume": float(volume)}
        cursor = stop
    if not data:
        raise RuntimeError("Coinbase returned no hourly candles.")
    current = (int(time.time()) // 3600) * 3600
    previous = None
    hourly = []
    synthetic = 0
    for ts in range(min(data), current, 3600):
        row = data.get(ts)
        if row is None:
            if previous is None:
                continue
            row = {"time": ts, "open": previous, "high": previous,
                   "low": previous, "close": previous, "volume": 0.0}
            synthetic += 1
        else:
            previous = row["close"]
        hourly.append(row)
    buckets = {}
    for row in hourly:
        bucket = (row["time"] // live.CANDLE_SECONDS) * live.CANDLE_SECONDS
        buckets.setdefault(bucket, []).append(row)
    candles = []
    current_bucket = (int(time.time()) // live.CANDLE_SECONDS) * live.CANDLE_SECONDS
    for bucket, rows in sorted(buckets.items()):
        if bucket >= current_bucket:
            continue
        rows.sort(key=lambda x: x["time"])
        if len(rows) != 4:
            continue
        candles.append({"time": bucket, "open": rows[0]["open"],
            "high": max(x["high"] for x in rows),
            "low": min(x["low"] for x in rows),
            "close": rows[-1]["close"],
            "volume": sum(x["volume"] for x in rows)})
    if len(candles) < 300:
        raise RuntimeError(f"Insufficient 4h history: {len(candles)}")
    return candles, len(data), synthetic, start, end


def sma(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i-period+1:i+1]) / period
    return out


def rolling_std(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i-period+1:i+1]
        mean = sum(window) / period
        out[i] = (sum((x-mean)**2 for x in window) / period) ** 0.5
    return out


def build_signals(candles):
    closes = [x["close"] for x in candles]
    volumes = [x["volume"] for x in candles]
    e20, e50, e200 = (live.ema(closes, n) for n in (20, 50, 200))
    atr = live.atr(candles, 14)
    rsi2 = live.rsi(closes, 2)
    rsi14 = live.rsi(closes, 14)
    sma5, sma20 = sma(closes, 5), sma(closes, 20)
    sd20 = rolling_std(closes, 20)
    out = []
    for i, c in enumerate(candles):
        ready = i >= 200 and all(x[i] is not None for x in (e20,e50,e200,atr,rsi2,rsi14,sma5,sma20,sd20))
        if not ready:
            out.append(None)
            continue
        avgvol = sum(volumes[i-20:i]) / 20
        prior_high = max(x["high"] for x in candles[i-20:i])
        lower_band = sma20[i] - 2.0 * sd20[i]
        breakout_buy = (c["close"] > prior_high and e50[i] > e200[i]
                        and avgvol > 0 and c["volume"] >= 1.20 * avgvol)
        breakout_exit = c["close"] < e20[i] or e50[i] < e200[i]
        mean_buy = c["close"] > e200[i] and rsi2[i] < 8 and c["close"] < lower_band
        mean_exit = c["close"] > sma5[i] or rsi2[i] > 60
        out.append({
            "atr": atr[i],
            "breakout_buy": breakout_buy, "breakout_exit": breakout_exit,
            "mean_buy": mean_buy, "mean_exit": mean_exit,
        })
    return out


def run_strategy(name, candles, signals, buy_key, exit_key, stop_atr):
    cash = INITIAL_CAPITAL
    position = None
    pending_entry = None
    pending_exit = False
    closed = []
    curve = []
    for i, c in enumerate(candles):
        if i == 0:
            continue
        ts = c["time"]
        if position and pending_exit:
            ref = c["open"]
            fill = ref * (1 - SLIPPAGE)
            qty = position["qty"]
            fee = qty * fill * FEE
            cash += qty * fill - fee
            pnl = (fill - position["entry"]) * qty - fee - position["entry_fee"]
            closed.append({"pnl": pnl, "reason": "signal_exit_next_open"})
            position = None
            pending_exit = False
        if position is None and pending_entry is not None:
            entry = c["open"] * (1 + SLIPPAGE)
            stop = entry - stop_atr * pending_entry["atr"]
            if 0 < stop < entry:
                risk_qty = (cash * RISK_FRACTION) / (entry - stop)
                qty = min(risk_qty, cash / (entry * (1 + FEE)))
                if qty > 0:
                    entry_fee = qty * entry * FEE
                    cash -= qty * entry + entry_fee
                    position = {"entry": entry, "stop": stop, "qty": qty,
                                "entry_fee": entry_fee}
            pending_entry = None
        if position:
            if c["low"] <= position["stop"]:
                ref = position["stop"]
                fill = ref * (1 - SLIPPAGE)
                qty = position["qty"]
                fee = qty * fill * FEE
                cash += qty * fill - fee
                pnl = (fill - position["entry"]) * qty - fee - position["entry_fee"]
                closed.append({"pnl": pnl, "reason": "stop_loss"})
                position = None
        if position and signals[i] and signals[i][exit_key]:
            pending_exit = True
        if position is None and signals[i] and signals[i][buy_key]:
            pending_entry = signals[i]
        equity = cash + (position["qty"] * c["close"] if position else 0.0)
        curve.append(equity)
    if position:
        c = candles[-1]
        fill = c["close"] * (1 - SLIPPAGE)
        qty = position["qty"]
        fee = qty * fill * FEE
        cash += qty * fill - fee
        pnl = (fill - position["entry"]) * qty - fee - position["entry_fee"]
        closed.append({"pnl": pnl, "reason": "end_of_test"})
    wins = [x["pnl"] for x in closed if x["pnl"] > 0]
    losses = [x["pnl"] for x in closed if x["pnl"] <= 0]
    peak, max_dd = INITIAL_CAPITAL, 0.0
    for equity in curve:
        peak = max(peak, equity)
        if peak:
            max_dd = max(max_dd, (peak-equity)/peak)
    final = cash
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    return {
        "strategy": name, "final_equity_usd": final,
        "net_pnl_usd": final - INITIAL_CAPITAL,
        "net_return_pct": (final / INITIAL_CAPITAL - 1) * 100,
        "closed_trades": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": (len(wins)/len(closed)*100) if closed else 0,
        "profit_factor": gross_profit/gross_loss if gross_loss else (None if not wins else "infinite"),
        "max_drawdown_pct": max_dd*100,
        "average_trade_pnl_usd": (sum(x["pnl"] for x in closed)/len(closed)) if closed else 0,
        "exit_reasons": {reason: sum(1 for x in closed if x["reason"] == reason)
                         for reason in sorted(set(x["reason"] for x in closed))},
    }


def main():
    product = get(f"/products/{PRODUCT}")
    if str(product.get("status", "")).lower() != "online" or product.get("trading_disabled") is True:
        raise RuntimeError(f"{PRODUCT} is not online/tradable: {product}")
    candles, hourly_count, synthetic, start, end = fetch_candles()
    signals = build_signals(candles)
    results = [
        run_strategy("Donchian breakout + EMA50/200 + volume confirmation",
                     candles, signals, "breakout_buy", "breakout_exit", 2.0),
        run_strategy("RSI(2) mean reversion + Bollinger + EMA200 regime filter",
                     candles, signals, "mean_buy", "mean_exit", 2.0),
    ]
    baseline = {}
    baseline_path = Path("degen_backtest_report.json")
    if baseline_path.exists():
        old = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline = {k: old.get(k) for k in (
            "net_return_pct", "profit_factor", "max_drawdown_pct",
            "completed_trades", "win_rate_pct")}
    report = {
        "product": PRODUCT, "exchange": "Coinbase Exchange",
        "period_days": DAYS,
        "start_utc": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "end_utc": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        "hourly_candles_received": hourly_count,
        "synthetic_zero_trade_hours": synthetic,
        "four_hour_candles_used": len(candles),
        "first_candle_utc": datetime.fromtimestamp(candles[0]["time"], timezone.utc).isoformat(),
        "last_candle_utc": datetime.fromtimestamp(candles[-1]["time"], timezone.utc).isoformat(),
        "initial_capital_usd": INITIAL_CAPITAL,
        "risk_per_trade_pct": RISK_FRACTION*100,
        "fees_pct_per_side": FEE*100,
        "slippage_pct_per_side": SLIPPAGE*100,
        "baseline_strategy_reference": baseline,
        "candidate_results": results,
        "methodology": [
            "Signals use completed 4h candles; entries and close-based exits execute at next candle open.",
            "Long-only spot, no leverage, no pyramiding; risk budget is 1% of current equity, position capped by available cash.",
            "Entry/exit fees 0.1% and slippage 0.1% are charged on each side.",
            "Protective stop is 2 ATR from entry; intrabar stop fills at stop less sell slippage.",
            "Breakout: close above prior 20-candle high, EMA50 above EMA200, volume at least 1.2x prior 20-candle average; exit below EMA20 or EMA50 below EMA200.",
            "Mean reversion: close above EMA200, RSI(2) below 8, close below lower 2-standard-deviation Bollinger band; exit above SMA5 or RSI(2) above 60.",
            "No future candle data is used; synthetic flat zero-volume hourly candles are inserted where Coinbase returns no trades.",
            "No parameter optimization was performed. These are candidate tests, not proof of a durable edge."
        ]
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**{k:v for k,v in report.items() if k != "methodology"},
                      "candidate_results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
