#!/usr/bin/env python3
"""Regime-switching DEGEN/USD paper backtest. No live orders."""
import json
from datetime import datetime, timezone
from pathlib import Path

import degen_strategy_lab as lab
import degen_signal as live

INITIAL_CAPITAL = 10.0
FEE = 0.001
SLIPPAGE = 0.001
RISK_FRACTION = 0.01
STOP_ATR = 2.0
OUT = Path("degen_regime_report.json")


def adx(candles, period=14):
    n = len(candles)
    tr = [None] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        cur, prev = candles[i], candles[i - 1]
        tr[i] = max(cur["high"] - cur["low"],
                    abs(cur["high"] - prev["close"]),
                    abs(cur["low"] - prev["close"]))
        up = cur["high"] - prev["high"]
        down = prev["low"] - cur["low"]
        if up > down and up > 0:
            plus_dm[i] = up
        elif down > up and down > 0:
            minus_dm[i] = down

    out = [None] * n
    if n <= period * 2:
        return out
    sm_tr = sum(tr[1:period + 1])
    sm_plus = sum(plus_dm[1:period + 1])
    sm_minus = sum(minus_dm[1:period + 1])
    dx = [None] * n

    def calc_dx(i):
        if sm_tr <= 0:
            return 0.0
        pdi = 100.0 * sm_plus / sm_tr
        mdi = 100.0 * sm_minus / sm_tr
        denom = pdi + mdi
        return 0.0 if denom == 0 else 100.0 * abs(pdi - mdi) / denom

    dx[period] = calc_dx(period)
    for i in range(period + 1, n):
        sm_tr = sm_tr - sm_tr / period + tr[i]
        sm_plus = sm_plus - sm_plus / period + plus_dm[i]
        sm_minus = sm_minus - sm_minus / period + minus_dm[i]
        dx[i] = calc_dx(i)

    first = period * 2 - 1
    if first < n and all(x is not None for x in dx[period:first + 1]):
        current = sum(dx[period:first + 1]) / period
        out[first] = current
        for i in range(first + 1, n):
            current = (current * (period - 1) + dx[i]) / period
            out[i] = current
    return out


def build_features(candles):
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    e20 = live.ema(closes, 20)
    e50 = live.ema(closes, 50)
    e200 = live.ema(closes, 200)
    atr = live.atr(candles, 14)
    rsi2 = live.rsi(closes, 2)
    sma5 = lab.sma(closes, 5)
    sma20 = lab.sma(closes, 20)
    sd20 = lab.rolling_std(closes, 20)
    adx14 = adx(candles, 14)
    features = []
    regime_counts = {"trend": 0, "range": 0, "neutral": 0}
    for i, c in enumerate(candles):
        ready = all(x[i] is not None for x in
                    (e20, e50, e200, atr, rsi2, sma5, sma20, sd20, adx14))
        if not ready:
            features.append(None)
            continue
        # Hysteresis band: ADX 25+ is trend-strength; 18 or below is range.
        trend = adx14[i] >= 25 and e50[i] > e200[i]
        ranging = adx14[i] <= 18 and c["close"] > e200[i]
        regime = "trend" if trend else ("range" if ranging else "neutral")
        regime_counts[regime] += 1
        avgvol = sum(volumes[i - 20:i]) / 20
        prior_high = max(x["high"] for x in candles[i - 20:i])
        lower_band = sma20[i] - 2.0 * sd20[i]
        features.append({
            "regime": regime, "adx": adx14[i], "atr": atr[i],
            "trend_entry": (trend and c["close"] > prior_high and
                            avgvol > 0 and c["volume"] >= 1.2 * avgvol),
            "range_entry": (ranging and rsi2[i] < 8 and c["close"] < lower_band),
            "trend_exit": (not trend or c["close"] < e20[i] or e50[i] <= e200[i]),
            "range_exit": (not ranging or c["close"] > sma5[i] or rsi2[i] > 60),
        })
    return features, regime_counts


def run(candles, features, start_index=200, end_index=None, slippage=SLIPPAGE):
    if end_index is None:
        end_index = len(candles)
    cash = INITIAL_CAPITAL
    pos = None
    pending_entry = None
    pending_exit = None
    trades = []
    curve = [INITIAL_CAPITAL]
    regime_exposure = {"trend": 0, "range": 0, "neutral": 0}

    def close_position(ref_price, reason, gap=False):
        nonlocal cash, pos
        # A stop crossed by a gap fills at the worse opening price; otherwise at stop.
        raw = ref_price
        fill = raw * (1 - slippage)
        qty = pos["qty"]
        fee = qty * fill * FEE
        cash += qty * fill - fee
        pnl = (fill - pos["entry"]) * qty - fee - pos["entry_fee"]
        trades.append({"pnl_usd": pnl, "reason": reason,
                       "mode": pos["mode"], "entry_price": pos["entry"],
                       "exit_price": fill, "gap_stop": gap})
        pos = None

    for i in range(start_index, end_index):
        c = candles[i]
        f = features[i]
        if f is None:
            curve.append(cash + (pos["qty"] * c["close"] if pos else 0))
            continue

        if pos and pending_exit:
            close_position(c["open"], pending_exit)
            pending_exit = None

        if pos is None and pending_entry:
            entry = c["open"] * (1 + slippage)
            stop = entry - STOP_ATR * pending_entry["atr"]
            if 0 < stop < entry:
                risk_qty = (cash * RISK_FRACTION) / (entry - stop)
                qty = min(risk_qty, cash / (entry * (1 + FEE)))
                if qty > 0:
                    entry_fee = qty * entry * FEE
                    cash -= qty * entry + entry_fee
                    pos = {"entry": entry, "stop": stop, "qty": qty,
                           "entry_fee": entry_fee, "mode": pending_entry["mode"]}
            pending_entry = None

        if pos:
            regime_exposure[pos["mode"]] += 1
            if c["low"] <= pos["stop"]:
                gap = c["open"] < pos["stop"]
                close_position(c["open"] if gap else pos["stop"], "stop_loss", gap)
            elif pos and ((pos["mode"] == "trend" and f["trend_exit"]) or
                          (pos["mode"] == "range" and f["range_exit"])):
                pending_exit = "regime_or_rule_exit_next_open"

        if pos is None and pending_entry is None:
            if f["trend_entry"]:
                pending_entry = {"atr": f["atr"], "mode": "trend"}
            elif f["range_entry"]:
                pending_entry = {"atr": f["atr"], "mode": "range"}

        equity = cash + (pos["qty"] * c["close"] if pos else 0.0)
        curve.append(equity)

    if pos:
        last = candles[end_index - 1]
        close_position(last["close"], "end_of_test")

    wins = [t["pnl_usd"] for t in trades if t["pnl_usd"] > 0]
    losses = [t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0]
    peak = INITIAL_CAPITAL
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak:
            max_dd = max(max_dd, (peak - value) / peak)
    gp, gl = sum(wins), abs(sum(losses))
    return {
        "final_equity_usd": cash, "net_pnl_usd": cash - INITIAL_CAPITAL,
        "net_return_pct": (cash / INITIAL_CAPITAL - 1) * 100,
        "closed_trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": len(wins) / len(trades) * 100 if trades else 0,
        "profit_factor": gp / gl if gl else ("infinite" if gp else None),
        "max_drawdown_pct": max_dd * 100,
        "average_trade_pnl_usd": sum(t["pnl_usd"] for t in trades) / len(trades) if trades else 0,
        "trade_modes": {m: sum(1 for t in trades if t["mode"] == m)
                        for m in ("trend", "range")},
        "exit_reasons": {r: sum(1 for t in trades if t["reason"] == r)
                         for r in sorted(set(t["reason"] for t in trades))},
        "regime_candles_while_position_open": regime_exposure,
        "trades": trades,
    }


def main():
    product = lab.get(f"/products/{lab.PRODUCT}")
    if str(product.get("status", "")).lower() != "online" or product.get("trading_disabled") is True:
        raise RuntimeError(f"{lab.PRODUCT} is not online/tradable: {product}")
    candles, hourly_count, synthetic, start, end = lab.fetch_candles()
    features, regime_counts = build_features(candles)
    split_ts = start + 365 * 24 * 3600
    split = next((i for i, c in enumerate(candles) if c["time"] >= split_ts), None)
    if split is None or split < 250 or len(candles) - split < 250:
        raise RuntimeError("Insufficient candles for chronological 1y/1y split.")

    global SLIPPAGE
    full = run(candles, features, 200, len(candles), SLIPPAGE)
    train = run(candles, features, 200, split, SLIPPAGE)
    oos = run(candles, features, split, len(candles), SLIPPAGE)
    sensitivity = {}
    for slip in (0.001, 0.002, 0.003):
        sensitivity[f"slippage_{slip*100:.1f}pct_per_side"] = run(
            candles, features, 200, len(candles), slip
        )
    report = {
        "product": lab.PRODUCT, "exchange": "Coinbase Exchange",
        "period_days": lab.DAYS,
        "start_utc": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "end_utc": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        "first_candle_utc": datetime.fromtimestamp(candles[0]["time"], timezone.utc).isoformat(),
        "last_candle_utc": datetime.fromtimestamp(candles[-1]["time"], timezone.utc).isoformat(),
        "hourly_candles_received": hourly_count,
        "synthetic_zero_trade_hours": synthetic,
        "four_hour_candles_used": len(candles),
        "initial_capital_usd": INITIAL_CAPITAL,
        "risk_per_trade_pct": RISK_FRACTION * 100,
        "fees_pct_per_side": FEE * 100,
        "slippage_pct_per_side": SLIPPAGE * 100,
        "stop_atr_multiple": STOP_ATR,
        "regime_candle_counts": regime_counts,
        "full_period": full,
        "first_year_exploratory": train,
        "second_year_chronological_segment": oos,
        "split_utc": datetime.fromtimestamp(candles[split]["time"], timezone.utc).isoformat(),
        "cost_sensitivity": sensitivity,
        "methodology": [
            "4h completed candles only; signal at close, entry/close-based exit at next candle open.",
            "Regime trend: ADX(14) >= 25 and EMA50 > EMA200.",
            "Regime range: ADX(14) <= 18 and close > EMA200; otherwise neutral/no new entries.",
            "Trend entry: close above prior 20-candle high and volume >= 1.2x prior 20-candle average.",
            "Trend exit: regime ceases to qualify as trend, close below EMA20, or EMA50 <= EMA200.",
            "Range entry: RSI(2) < 8 and close below lower 2-standard-deviation Bollinger band.",
            "Range exit: regime ceases to qualify as range, close above SMA5, or RSI(2) > 60.",
            "ADX 18-25 is a no-new-entry transition band; existing positions exit when their own regime invalidates.",
            "Protective stop is 2 ATR below entry; gap-through stop fills at candle open; otherwise at stop.",
            "Long-only spot, no leverage, no pyramiding; risk budget 1% of equity, position capped by cash.",
            "Fees 0.1% and slippage 0.1% each side; sensitivity tests slippage 0.2% and 0.3%.",
            "Historical periods have already been inspected in prior experiments; the second-year segment is not a blind OOS validation.",
            "Synthetic flat zero-volume hours are inserted where Coinbase returns no trades; results may be sensitive to sparse liquidity.",
            "Candidate research only; no live orders or claim of a durable trading edge."
        ]
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "methodology"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
