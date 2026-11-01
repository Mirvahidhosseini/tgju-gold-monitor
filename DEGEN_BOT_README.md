# DEGEN Swing Signal Bot

Signal-only DEGEN/USD swing monitor. It does not place exchange orders and does not use Coinbase trading or withdrawal credentials.

## Market and data
- Coinbase Exchange product: \`DEGEN-USD\`
- Public Exchange REST API; no Coinbase API key is used.
- Every run checks Coinbase product status and refuses to analyse if the product is not online.
- Downloads hourly candles and aggregates completed 4-hour candles. Unfinished candles are never used.

## Initial strategy
BUY requires all four conditions:
1. EMA20 > EMA50.
2. Last completed 4-hour close > EMA20.
3. RSI(14) is between 50 and 68 and rising.
4. Current 4-hour volume is at least 10% above the previous 20-candle average.

A paper position exits on the first of:
- Stop: 1.5 × ATR(14) below entry.
- Target 1: 2R (sell 50%, move remaining stop to breakeven).
- Target 2: 3R (close remainder).
- Trend exit: close below EMA20, RSI below 45, or EMA20 below EMA50.

If stop and target are both touched in one OHLC candle, simulation assumes stop first.

## Paper capital and risk
- Starting paper capital: USD 10.
- Risk budget: 1% of simulated equity per position.
- Position size is capped by available simulated capital.
- No leverage, averaging down, or martingale.
- Fees and slippage are not included; reported results are optimistic.

## Telegram and schedule
Reuses existing repository secrets: \`BOT_TOKEN\` and \`CHAT_ID\`.
Runs every four hours and can be started manually from **Actions → DEGEN Swing Signal Bot → Run workflow**.
State is saved in \`degen_state.json\`.

## Limitations
- Experimental rules, not a validated profitable strategy or investment advice.
- Signals are generated only after a completed 4-hour candle.
- Market availability can vary by region.
- GitHub Actions schedules are not guaranteed to run at the exact minute.
- The first run may initialize the state without sending a signal.
- No live orders are sent. Do not add Coinbase trading keys to this workflow.
