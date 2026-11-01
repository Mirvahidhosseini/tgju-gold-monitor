#!/usr/bin/env python3
"""One-year, no-lookahead DEGEN/USD backtest using the live bot's exact signal rules."""
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
RISK_FRACTION = live.RISK_FRACTION
OUT = Path("degen_backtest_report.json")
SESSION = requests.Session()
SESSION.headers.update(live.HEADERS)


def get(path, params=None):
    r = SESSION.get(BASE_URL + path, params=params, timeout=30)
    if r.status_code == 429:
        time.sleep(3)
        r = SESSION.get(BASE_URL + path, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_hourly():
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
                ts, low, high, op, close, vol = row[:6]
                data[int(ts)] = {
                    "time": int(ts), "open": float(op), "high": float(high),
                    "low": float(low), "close": float(close), "volume": float(vol),
                }
        cursor = stop
    return [data[k] for k in sorted(data)], start, end


def make_4h(hourly, end_epoch):
    # Coinbase omits hourly candles for intervals with no trades. Reconstruct
    # those hours as flat, zero-volume candles using the last traded close.
    by_time = {row["time"]: row for row in hourly}
    if not hourly:
        return [], 0
    first = min(by_time)
    last_complete_hour = (end_epoch // 3600) * 3600
    filled = []
    previous_close = None
    synthetic = 0
    for ts in range(first, last_complete_hour, 3600):
        row = by_time.get(ts)
        if row is None:
            if previous_close is None:
                continue
            row = {
                "time": ts, "open": previous_close, "high": previous_close,
                "low": previous_close, "close": previous_close, "volume": 0.0,
            }
            synthetic += 1
        else:
            previous_close = row["close"]
        filled.append(row)
    buckets = {}
    for row in filled:
        b = (row["time"] // live.CANDLE_SECONDS) * live.CANDLE_SECONDS
        buckets.setdefault(b, []).append(row)
    current = (int(time.time()) // live.CANDLE_SECONDS) * live.CANDLE_SECONDS
    out = []
    for b, rows in sorted(buckets.items()):
        if b >= current:
            continue
        rows.sort(key=lambda x: x["time"])
        if len(rows) != 4:
            continue
        out.append({
            "time": b, "open": rows[0]["open"],
            "high": max(x["high"] for x in rows),
            "low": min(x["low"] for x in rows),
            "close": rows[-1]["close"],
            "volume": sum(x["volume"] for x in rows),
        })
    return out, synthetic


def indicators(candles):
    closes = [x["close"] for x in candles]
    vols = [x["volume"] for x in candles]
    e20, e50 = live.ema(closes, 20), live.ema(closes, 50)
    rsi = live.rsi(closes, 14)
    atr = live.atr(candles, 14)
    out = []
    for i, row in enumerate(candles):
        if i < 50 or any(x[i] is None for x in (e20, e50, rsi, atr)) or rsi[i-1] is None:
            out.append(None)
            continue
        avgvol = sum(vols[i-20:i]) / 20
        checks = {
            "uptrend": e20[i] > e50[i],
            "close_above_ema20": row["close"] > e20[i],
            "rsi_50_to_68_and_rising": 50 <= rsi[i] <= 68 and rsi[i] > rsi[i-1],
            "volume_10pct_above_average": avgvol > 0 and row["volume"] >= avgvol * 1.10,
        }
        reasons = []
        if row["close"] < e20[i]: reasons.append("close_below_ema20")
        if rsi[i] < 45: reasons.append("rsi_below_45")
        if e20[i] < e50[i]: reasons.append("ema20_below_ema50")
        out.append({
            "ema20": e20[i], "ema50": e50[i], "rsi": rsi[i], "atr": atr[i],
            "volume_ratio": row["volume"] / avgvol if avgvol else 0,
            "buy": all(checks.values()), "exit": bool(reasons), "checks": checks,
            "exit_reasons": reasons,
        })
    return out


def sell(cash, pos, qty, reference_price, reason, trades, ts, fill_at_open=False):
    fill = reference_price * (1 - SLIPPAGE)
    gross = qty * fill
    fee = gross * FEE
    cash += gross - fee
    net_piece = (fill - pos["entry_fill"]) * qty - fee - pos["entry_fee_alloc"]
    pos["net_pnl"] += net_piece
    pos["entry_fee_alloc"] = 0.0
    pos["remaining"] -= qty
    trades.append({
        "time": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        "type": reason, "price_reference": reference_price, "fill_price": fill,
        "quantity": qty, "fee_usd": fee,
    })
    return cash


def main():
    product = get(f"/products/{PRODUCT}")
    if str(product.get("status", "")).lower() != "online" or product.get("trading_disabled") is True:
        raise RuntimeError(f"{PRODUCT} not online/tradable: {product}")
    hourly, start, end = fetch_hourly()
    candles, synthetic_hours = make_4h(hourly, end)
    if len(candles) < 100:
        raise RuntimeError(f"Not enough 4h candles: {len(candles)}")
    ind = indicators(candles)
    cash = INITIAL_CAPITAL
    pos = None
    pending_entry = None
    pending_exit = None
    trades = []
    completed = []
    equity_curve = []
    for i, candle in enumerate(candles):
        if i == 0:
            continue
        ts = candle["time"]
        # Signals generated at the previous candle close execute at this candle open.
        if pos and pending_exit:
            qty = pos["remaining"]
            cash = sell(cash, pos, qty, candle["open"], "signal_exit_next_open", trades, ts, True)
            completed.append({"net_pnl_usd": pos["net_pnl"], "exit_reason": "signal_exit_next_open"})
            pos = None
            pending_exit = None
        if pos is None and pending_entry is not None:
            atr_value = pending_entry["atr"]
            entry_fill = candle["open"] * (1 + SLIPPAGE)
            stop = entry_fill - 1.5 * atr_value
            if stop > 0 and stop < entry_fill:
                equity = cash
                risk_budget = equity * RISK_FRACTION
                qty = min(risk_budget / (entry_fill - stop), cash / (entry_fill * (1 + FEE)))
                if qty > 0:
                    entry_fee = qty * entry_fill * FEE
                    cash -= qty * entry_fill + entry_fee
                    pos = {
                        "entry_fill": entry_fill, "entry_reference": candle["open"],
                        "stop": stop, "target1": entry_fill + 2 * (entry_fill - stop),
                        "target2": entry_fill + 3 * (entry_fill - stop),
                        "initial_qty": qty, "remaining": qty, "target1_done": False,
                        "entry_fee_alloc": entry_fee, "net_pnl": 0.0,
                        "opened_at": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                    }
                    trades.append({
                        "time": pos["opened_at"], "type": "buy_next_open",
                        "price_reference": candle["open"], "fill_price": entry_fill,
                        "quantity": qty, "fee_usd": entry_fee,
                    })
            pending_entry = None
        # Intrabar protective/target orders. If stop and target overlap, stop wins.
        if pos:
            low, high = candle["low"], candle["high"]
            if low <= pos["stop"]:
                qty = pos["remaining"]
                reason = "stop_loss" if not pos["target1_done"] else "breakeven_stop_after_tp1"
                cash = sell(cash, pos, qty, pos["stop"], reason, trades, ts)
                completed.append({"net_pnl_usd": pos["net_pnl"], "exit_reason": reason})
                pos = None
            else:
                if not pos["target1_done"] and high >= pos["target1"]:
                    qty = min(pos["initial_qty"] * 0.5, pos["remaining"])
                    cash = sell(cash, pos, qty, pos["target1"], "take_profit_1", trades, ts)
                    pos["target1_done"] = True
                    pos["stop"] = pos["entry_fill"]
                    # If the newly-raised breakeven stop is also touched in
                    # this candle, assume it is hit after TP1 (conservative).
                    if low <= pos["stop"]:
                        qty = pos["remaining"]
                        cash = sell(cash, pos, qty, pos["stop"], "breakeven_stop_after_tp1", trades, ts)
                        completed.append({"net_pnl_usd": pos["net_pnl"], "exit_reason": "breakeven_stop_after_tp1"})
                        pos = None
                if pos and pos["target1_done"] and high >= pos["target2"]:
                    qty = pos["remaining"]
                    cash = sell(cash, pos, qty, pos["target2"], "take_profit_2", trades, ts)
                    completed.append({"net_pnl_usd": pos["net_pnl"], "exit_reason": "take_profit_2"})
                    pos = None
        # Close-based exit signals are executed only at the next candle open.
        if pos and ind[i] and ind[i]["exit"]:
            pending_exit = True
        if pos is None and ind[i] and ind[i]["buy"]:
            pending_entry = ind[i]
        mark = cash + (pos["remaining"] * candle["close"] if pos else 0.0)
        equity_curve.append({"time": datetime.fromtimestamp(ts, timezone.utc).isoformat(), "equity": mark})
    # Liquidate any remaining position at the final candle close, including costs.
    if pos:
        last = candles[-1]
        qty = pos["remaining"]
        cash = sell(cash, pos, qty, last["close"], "end_of_test_liquidation", trades, last["time"])
        completed.append({"net_pnl_usd": pos["net_pnl"], "exit_reason": "end_of_test_liquidation"})
        pos = None
    # Benchmark: buy and hold over the same available 4h candle window, with entry/exit costs.
    benchmark_entry = candles[0]["open"] * (1 + SLIPPAGE)
    benchmark_qty = (INITIAL_CAPITAL / (benchmark_entry * (1 + FEE)))
    benchmark_exit = candles[-1]["close"] * (1 - SLIPPAGE)
    benchmark_final = benchmark_qty * benchmark_exit * (1 - FEE)
    wins = [x for x in completed if x["net_pnl_usd"] > 0]
    losses = [x for x in completed if x["net_pnl_usd"] <= 0]
    final_equity = cash
    peak = INITIAL_CAPITAL
    max_dd = 0.0
    for point in equity_curve:
        peak = max(peak, point["equity"])
        if peak > 0:
            max_dd = max(max_dd, (peak - point["equity"]) / peak)
    report = {
        "product": PRODUCT, "exchange": "Coinbase Exchange", "period_days": DAYS,
        "requested_start_utc": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "requested_end_utc": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        "hourly_candles_received": len(hourly), "synthetic_zero_trade_hours": synthetic_hours,
        "hourly_hours_in_requested_window": (end - start) // 3600, "four_hour_candles_used": len(candles),
        "first_candle_utc": datetime.fromtimestamp(candles[0]["time"], timezone.utc).isoformat(),
        "last_candle_utc": datetime.fromtimestamp(candles[-1]["time"], timezone.utc).isoformat(),
        "initial_capital_usd": INITIAL_CAPITAL, "final_equity_usd": final_equity,
        "net_pnl_usd": final_equity - INITIAL_CAPITAL,
        "net_return_pct": (final_equity / INITIAL_CAPITAL - 1) * 100,
        "buy_and_hold_final_usd": benchmark_final,
        "buy_and_hold_return_pct": (benchmark_final / INITIAL_CAPITAL - 1) * 100,
        "fees_pct_per_side": FEE * 100, "slippage_pct_per_side": SLIPPAGE * 100,
        "completed_trades": len(completed), "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": (len(wins) / len(completed) * 100) if completed else 0,
        "max_drawdown_pct": max_dd * 100,
        "profit_factor": (
            sum(x["net_pnl_usd"] for x in wins) /
            abs(sum(x["net_pnl_usd"] for x in losses))
            if losses and sum(x["net_pnl_usd"] for x in losses) != 0
            else (None if not wins else "infinite")
        ),
        "open_position_at_end": False,
        "trades": trades, "closed_trade_results": completed,
        "equity_curve": equity_curve,
        "methodology": [
            "Entry signal is evaluated on a completed 4h candle and filled at next 4h candle open.",
            "Close-based exit signal is filled at next 4h candle open.",
            "Stop/target orders are triggered intrabar at their levels; sell slippage is applied.",
            "If stop and target are both touched in one candle, stop is assumed first.",
            "Fees 0.1% and slippage 0.1% are applied to every buy and sell fill.",
            "Position sizing follows the live bot: 1% of current cash equity risk, capped at fully-funded spot exposure.",
            "No leverage; no future candle data is used for a signal.",
            "Open position is liquidated at final close with costs."
        ]
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in ("trades","closed_trade_results","equity_curve","methodology")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
