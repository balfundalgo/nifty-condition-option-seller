# NIFTY First-Candle Condition Option Seller

**Balfund Trading Pvt Ltd** | Strategy code `BF-NFT-FCOS` | Broker: **Dhan v2** | Timeframe: **5-minute, fixed**

Intraday NIFTY option-selling strategy built on the first (09:15) and second (09:20) 5-minute candles of NIFTY spot and two 200-point ITM options. Five conditions run in parallel — **Condition 1, 2, 3, 5** and the **Manipulation** condition. Every condition trades independently: several can fire on the same candle or while others are in a trade, each with its own position, SL and target. Each condition side (e.g. Condition 1 PE) trades at most once a day.

## Setup of the day

```
09:15 candle closes (09:20)   ATM = round(close / 50) * 50
                              CE  = ATM - 200     PE = ATM + 200     fixed all day
expiry                        current weekly; on expiry day, the next weekly
R1                            09:15 candle high / low / open  on spot, CE, PE
R2                            09:20 candle high / low / open  on spot, CE, PE   (Manipulation)
```

## Common rules

```
"break"                 wick counts
"stays in range"        the WHOLE candle, wicks included, inside the range —
                        for spot and options alike; once an instrument wicks
                        out, "stays in range" has failed for the day
"closes beyond"         candle close (the triggers of Conditions 1, 2, Manipulation)
entry                   SELL at the close of the reversal candle
SL                      sold option premium: max(high of reversal candle,
                        high of the candle before it) + 7
target                  a SPOT level, fixed at the moment of entry
exit                    SL on option LTP | target on spot LTP | 15:15 square-off
entries                 reversal candle must close between 09:25 and 14:30
multiple trades         conditions trade independently; each condition side
                        trades at most once a day; each position has its own
                        SL and target; 15:15 squares off everything
```

## The conditions

| Condition | Setup | Then | Reversal on | Sell | Target |
|---|---|---|---|---|---|
| **1** | spot fully inside R1 (wicks too); PE breaks high **and** CE breaks low | spot closes below R1 low | spot (bullish) | PE | day high |
| | mirror: CE high, PE low | spot closes above R1 high | spot (bearish) | CE | day low |
| **2** | spot breaks R1 high, CE broke high, PE fully inside | new high = that spot candle's high; spot closes above it | spot (bearish) | CE | first-candle open |
| | mirror: spot breaks low, PE broke high, CE fully inside | close below new low | spot (bullish) | PE | first-candle open |
| **3** | spot breaks R1 high, CE and PE both fully inside | CE breaks its R1 high | CE (bearish) | CE | day low |
| | mirror: spot breaks low | PE breaks its R1 high | PE (bearish) | PE | day high |
| **5** | spot fully inside (wicks too); CE breaks high, PE fully inside | reversal #1 → peak 1 = CE swing high → CE breaks peak 1 | CE (bearish) #2 | CE | day low |
| | mirror on PE | | PE (bearish) #2 | PE | day high |
| **Manipulation** | as Condition 1, on **R2** (spot fully inside R2) | spot closes ≥ 10 points beyond R2 | spot | PE / CE | day high / low |

Conditions 2 and 3 are decided on the **first** candle where spot breaks that side of R1.

## Reversal candles

All on by default, each switchable in Settings:
colour flip, engulfing, shooting star / hammer, harami, dark cloud / piercing, evening / morning star, doji + confirmation, tweezer top / bottom.

The reversal must come on a candle **after** the trigger candle. "Allow reversal on the trigger candle" relaxes that.

## Data — the hybrid model

| | Source | Why |
|---|---|---|
| 5-min candles for spot, CE, PE → every signal | **REST** (1-min `/charts/intraday`, rolled up, anchored to 09:15) | Signals are candle-close decisions; REST can also fetch the 09:15 option candle *after* the strikes are known at 09:20 |
| SL, target, fill price, day high/low | **WebSocket** (Ticker mode) | These must act inside a candle |

Candles are released to the strategy only when spot, CE and PE have **all** delivered that 5-minute bar (`BarSync`), so every comparison is on the same candle.

Carried over from `sensex-vwap-ladder`:

* token verify → renew → TOTP, and **in-place re-auth** on 401 / DH-901 / DH-906 with a websocket rebuild
* IPv6 probe with IPv4 fallback
* separate HTTP sessions — reads retry, **orders never retry**
* rate gates, Limit+IOC entry/exit with market fallback
* forming-minute removal, session-anchor guard, 25s poll deadline, blind-feed alarm, websocket silence watchdog
* **stale bars cannot enter**: a candle that arrives more than 60s after its close updates state but cannot trigger a trade (power cut, sleep, API stall)

## Restarts

`fcos_daily_state.json` records today's strikes, which condition sides have traded, and every open position. A restart the same day resumes managing the open positions, and a condition side that already traded will not trade again. Started mid-day, the app replays today's candles to rebuild every condition's state; replayed candles can never trigger an entry.

## Running

```
pip install -r requirements.txt
python app.py            # GUI
python engine.py         # console, paper
python engine.py --live  # console, live
```

The EXE is built by GitHub Actions on every push to `main` (Actions → artifacts).

## Tests

```
python test_patterns.py
python test_logic.py
python test_restfeed.py
python test_engine.py
python test_gui.py       # needs a display; Linux: xvfb-run -a python test_gui.py
```

## Files

```
app.py        GUI
engine.py     live engine: strikes, feeds, orders, SL/target, state
strategy.py   the five conditions as pure state machines
patterns.py   reversal candles
restfeed.py   REST candle poller + BarSync
candles.py    1-min -> 5-min rollup
dhan.py       Dhan connection layer
logs/         activity log, trades CSV, candles CSV
```

---
Balfund Trading Pvt Ltd | www.balfund.com | info@balfund.com
