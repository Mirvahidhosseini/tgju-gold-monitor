#!/usr/bin/env python3
"""Three independent DEGEN/USD paper portfolios. No live orders."""
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
import degen_signal as live
import degen_strategy_lab as lab
import degen_regime_lab as regime

PRODUCT="DEGEN-USD"
INITIAL=10.0
FEE=0.001
SLIP=0.001
RISK=0.01
STATE=Path("degen_multi_paper_state.json")
REPORT=Path("degen_multi_paper_report.json")
STRATS={"baseline":{"label":"اصلی EMA/RSI","stop":1.5},
        "breakout":{"label":"Breakout","stop":2.0},
        "regime":{"label":"Regime","stop":2.0}}
DASH="https://github.com/Mirvahidhosseini/tgju-gold-monitor/blob/main/degen_multi_paper_report.json"

def iso(ts):
    return datetime.fromtimestamp(ts,timezone.utc).isoformat().replace("+00:00","Z")

def empty():
    return {"cash":INITIAL,"position":None,"pending_entry":None,"pending_exit":None,
            "trades":[],"events":[],"peak":INITIAL,"max_dd":0.0,"last_signal":"WAIT"}

def load():
    if not STATE.exists():
        return {"version":1,"last_candle":None,"created_at":datetime.now(timezone.utc).isoformat(),
                "portfolios":{k:empty() for k in STRATS}}
    try:
        x=json.loads(STATE.read_text(encoding="utf-8"))
        if x.get("version")!=1 or set(x.get("portfolios",{}))!=set(STRATS):
            raise ValueError("state schema mismatch")
        return x
    except Exception as e:
        raise RuntimeError("Paper state unreadable; refusing to reset it.") from e

def save(path,obj):
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8")
    tmp.replace(path)

def event(p,msg,ts):
    p["events"].append({"time":iso(ts),"event":msg})
    p["events"]=p["events"][-30:]

def close(p,ref,reason,ts,gap=False):
    pos=p["position"]
    if not pos: return
    fill=ref*(1-SLIP)
    qty=pos["qty"]
    fee=qty*fill*FEE
    p["cash"]+=qty*fill-fee
    pnl=pos.get("partial_pnl",0.0)+(fill-pos["entry"])*qty-fee-pos["entry_fee"]
    p["trades"].append({"entry_time":pos["time"],"exit_time":iso(ts),"mode":pos["mode"],
        "entry":pos["entry"],"exit":fill,"pnl_usd":pnl,"reason":reason,"gap_stop":gap})
    event(p,"بستن معامله: {:+.4f} USD ({})".format(pnl,reason),ts)
    p["position"]=None

def enter(p,c,pending,key,ts):
    entry=c["open"]*(1+SLIP)
    distance=pending["atr"]*STRATS[key]["stop"]
    stop=entry-distance
    if entry<=0 or distance<=0 or stop<=0: return
    qty=min((p["cash"]*RISK)/distance,p["cash"]/(entry*(1+FEE)))
    if qty<=0:return
    fee=qty*entry*FEE
    p["cash"]-=qty*entry+fee
    p["position"]={"mode":pending.get("mode",key),"entry":entry,"time":iso(ts),
        "qty":qty,"entry_fee":fee,"stop":stop,"target1":entry+2*distance,
        "target2":entry+3*distance,"target1_done":False,"partial_pnl":0.0}
    event(p,"ورود فرضی در {:.10f}؛ حد ضرر {:.10f}".format(entry,stop),ts)

def step(p,c,key,sig,ts):
    if p["position"] and p["pending_exit"]:
        close(p,c["open"],p["pending_exit"],ts)
        p["pending_exit"]=None
    if not p["position"] and p["pending_entry"]:
        q=p["pending_entry"]; p["pending_entry"]=None
        enter(p,c,q,key,ts)
    pos=p["position"]
    if pos:
        # Conservative OHLC assumption: stop has priority over targets.
        if c["low"]<=pos["stop"]:
            gap=c["open"]<pos["stop"]
            close(p,c["open"] if gap else pos["stop"],"حد ضرر",ts,gap)
        elif key=="baseline":
            if not pos["target1_done"] and c["high"]>=pos["target1"]:
                qty=pos["qty"]*0.5
                fill=pos["target1"]*(1-SLIP)
                fee=qty*fill*FEE
                p["cash"]+=qty*fill-fee
                pos["partial_pnl"]+=(fill-pos["entry"])*qty-fee
                pos["qty"]-=qty
                pos["target1_done"]=True
                pos["stop"]=pos["entry"]
                event(p,"هدف اول؛ فروش فرضی ۵۰٪",ts)
            elif pos["target1_done"] and c["high"]>=pos["target2"]:
                close(p,pos["target2"],"هدف دوم 3R",ts)
    pos=p["position"]
    if pos:
        if key=="baseline" and sig["exit"]:
            p["pending_exit"]="خروج روندی در بازشدن بعد"
        elif key=="breakout" and sig and sig["breakout_exit"]:
            p["pending_exit"]="خروج شکست در بازشدن بعد"
        elif key=="regime" and sig and ((pos["mode"]=="trend" and sig["trend_exit"]) or
                                         (pos["mode"]=="range" and sig["range_exit"])):
            p["pending_exit"]="خروج تغییر رژیم در بازشدن بعد"
    if not p["position"] and not p["pending_entry"]:
        if key=="baseline" and sig["buy"]:
            p["pending_entry"]={"atr":sig["atr"]}; p["last_signal"]="BUY_PENDING"
        elif key=="breakout" and sig and sig["breakout_buy"]:
            p["pending_entry"]={"atr":sig["atr"]}; p["last_signal"]="BUY_PENDING"
        elif key=="regime" and sig and sig["trend_entry"]:
            p["pending_entry"]={"atr":sig["atr"],"mode":"trend"}; p["last_signal"]="BUY_TREND_PENDING"
        elif key=="regime" and sig and sig["range_entry"]:
            p["pending_entry"]={"atr":sig["atr"],"mode":"range"}; p["last_signal"]="BUY_RANGE_PENDING"
        else:p["last_signal"]="WAIT"

def stats(p,mark):
    value=p["cash"]+(p["position"]["qty"]*mark if p["position"] else 0.0)
    p["peak"]=max(p["peak"],value)
    p["max_dd"]=max(p["max_dd"],(p["peak"]-value)/p["peak"]*100 if p["peak"] else 0)
    trades=p["trades"]; wins=[t["pnl_usd"] for t in trades if t["pnl_usd"]>0]
    losses=[t["pnl_usd"] for t in trades if t["pnl_usd"]<=0]
    gl=abs(sum(losses))
    return {"equity_usd":value,"net_pnl_usd":value-INITIAL,
        "return_pct":(value/INITIAL-1)*100,"closed_trades":len(trades),
        "wins":len(wins),"losses":len(losses),
        "win_rate_pct":len(wins)/len(trades)*100 if trades else 0,
        "profit_factor":sum(wins)/gl if gl else ("infinite" if wins else None),
        "max_drawdown_pct":p["max_dd"],"open_position":bool(p["position"]),
        "pending_entry":bool(p["pending_entry"]),"last_signal":p["last_signal"]}

def send_telegram(msg):
    token=os.environ.get("BOT_TOKEN"); chat=os.environ.get("CHAT_ID")
    if not token or not chat: raise RuntimeError("Missing BOT_TOKEN or CHAT_ID secrets.")
    r=live.SESSION.post("https://api.telegram.org/bot{}/sendMessage".format(token),
        json={"chat_id":chat,"text":msg,"disable_web_page_preview":True},timeout=25)
    r.raise_for_status()
    if not r.json().get("ok"): raise RuntimeError("Telegram rejected comparison report.")

def pf(x):
    return "—" if x is None else ("∞" if x=="infinite" else "{:.2f}".format(x))

def message(report):
    lines=["📊 DEGEN — گزارش مقایسه‌ای Paper Trading",
        "کندل ۴ساعته: "+report["last_candle_utc"],
        "قیمت پایانی: {:.10f} USD".format(report["last_price"]),
        "سرمایه مستقل هر مدل: 10 USD | کارمزد و لغزش: 0.1٪ هر سمت",""]
    for key,label in [("baseline","۱) اصلی EMA/RSI"),("breakout","۲) Breakout"),("regime","۳) Regime")]:
        x=report["strategies"][key]
        lines.append("{}: {:+.2f}% | P/L {:+.4f} USD | معاملات {} | برد {:.0f}% | PF {} | DD {:.2f}%".format(
            label,x["return_pct"],x["net_pnl_usd"],x["closed_trades"],x["win_rate_pct"],
            pf(x["profit_factor"]),x["max_drawdown_pct"]))
        pos=report["open_positions"].get(key)
        if pos: lines.append("   موقعیت باز: ورود {:.10f} | حد ضرر {:.10f}".format(pos["entry"],pos["stop"]))
        elif x["pending_entry"]: lines.append("   سیگنال ورود؛ اجرا در بازشدن کندل بعد")
        else: lines.append("   بدون موقعیت باز")
    lines += ["","وضعیت رژیم بازار: "+str(report["regime_now"]),
        "کندل جدید پردازش شد: "+("بله" if report["new_candle_processed"] else "خیر"),
        "فقط شبیه‌سازی؛ هیچ سفارش واقعی ارسال نمی‌شود.",DASH]
    return "\n".join(lines)

def main():
    product=live.verify_product()
    live.HISTORY_HOURS=1200
    hourly=live.fetch_hourly_candles()
    candles=live.aggregate_4h(hourly)
    if len(candles)<215: raise RuntimeError("Insufficient completed 4h candles: {}".format(len(candles)))
    c=candles[-1]; ts=c["time"]; stamp=iso(ts)
    base=live.analyse(candles)
    signals=lab.build_signals(candles)
    features,regimes=regime.build_features(candles)
    state=load()
    if state["last_candle"] is None:
        # Clean start: do not simulate entries from pre-deployment historical data.
        state["last_candle"]=stamp
        state["started_at"]=datetime.now(timezone.utc).isoformat()
        new=False
    elif state["last_candle"]==stamp:
        new=False
    else:
        new=True
        for key in STRATS:
            sig=base if key=="baseline" else (signals[-1] if key=="breakout" else features[-1])
            step(state["portfolios"][key],c,key,sig,ts)
        state["last_candle"]=stamp
    metrics={k:stats(state["portfolios"][k],c["close"]) for k in STRATS}
    positions={k:state["portfolios"][k]["position"] for k in STRATS
               if state["portfolios"][k]["position"]}
    report={"product":PRODUCT,"exchange":"Coinbase Exchange","mode":"paper_trading_only",
        "started_at":state.get("started_at",state.get("created_at")),
        "last_candle_utc":stamp,"last_price":c["close"],"new_candle_processed":new,
        "product_status":product.get("status"),"hourly_candles_loaded":len(hourly),
        "completed_4h_candles_loaded":len(candles),
        "regime_now":features[-1]["regime"] if features[-1] else "not_ready",
        "regime_history_counts_loaded":regimes,"initial_capital_per_strategy_usd":INITIAL,
        "fees_pct_per_side":FEE*100,"slippage_pct_per_side":SLIP*100,
        "risk_budget_pct_per_trade":RISK*100,"strategies":metrics,"open_positions":positions,
        "recent_events":{k:state["portfolios"][k]["events"][-10:] for k in STRATS},
        "trade_history":{k:state["portfolios"][k]["trades"][-100:] for k in STRATS},
        "methodology":{
          "baseline":"EMA20>EMA50; close>EMA20; RSI14 50-68 rising; volume >=1.1x prior 20 average; 1.5 ATR stop; 50% at 2R, rest at 3R; trend exits.",
          "breakout":"Close above prior 20-candle high; EMA50>EMA200; volume >=1.2x prior 20 average; 2 ATR stop; exit below EMA20 or EMA50<EMA200.",
          "regime":"ADX14>=25 and EMA50>EMA200: trend breakout. ADX14<=18 and close>EMA200: RSI2<8 below lower 2SD Bollinger. Otherwise no new entry. 2 ATR stop.",
          "execution":"Signal at completed 4h close; execute at next 4h open. Stop checked intrabar; stop-first for ambiguous OHLC paths.",
          "costs":"0.1% fee and 0.1% slippage per side. Independent $10 virtual account for each strategy.",
          "limitations":"Candle simulation cannot model order-book depth, actual spread, partial fills, outages or execution latency."
        }}
    save(STATE,state); save(REPORT,report)
    msg=message(report); print(msg); send_telegram(msg)
    print("Comparison report sent; paper state and GitHub report saved.")

if __name__=="__main__":
    try: main()
    except Exception as exc:
        print("DEGEN multi-strategy paper bot failed: {}".format(exc),file=sys.stderr); raise
