#!/usr/bin/env python3
"""
test_engine.py — engine entry/exit plumbing in paper mode, no network.
Drives _on_bundle and the tick handler directly.   python test_engine.py
"""

import sys, time, tempfile
from pathlib import Path
import engine as E

FAILS = []
E.STATE_FILE = Path(tempfile.mkdtemp()) / "state.json"


def check(name, cond, info=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + str(info)) if not cond else ''}")
    if not cond:
        FAILS.append(name)


def B(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def make():
    cfg = E.EngineConfig(mode="paper", lots=2, entry_start="00:00", entry_end="23:59")
    eng = E.Engine(cfg)
    eng.lot_size = 65
    eng.strikes = {"CE": 23800, "PE": 24200}
    eng.sec = {"CE": "111", "PE": "222"}
    eng.by_sec = {"13": "SPOT", "111": "CE", "222": "PE"}
    return eng


T0 = int(time.time()) - 3600
BARS = [
    (B(24000, 24050, 23950, 24010), B(300, 320, 280, 305), B(290, 310, 270, 285)),
    (B(24010, 24040, 23960, 24000), B(300, 305, 275, 285), B(290, 315, 280, 300)),
    (B(23990, 23995, 23930, 23940), B(285, 290, 260, 262), B(300, 340, 298, 335)),
    (B(23940, 23990, 23935, 23985), B(262, 290, 260, 288), B(330, 335, 300, 305)),
]

print("Paper entry on Condition 1 PE, exit at target")
eng = make()
eng.ltp = {"SPOT": 23985.0, "CE": 288.0, "PE": 306.0}
for n, (s, c, p) in enumerate(BARS):
    eng._on_bundle(T0 + 300 * n, s, c, p, lag=2.0)
pos = eng.position
check("position opened", pos is not None)
if pos:
    check("short PE", pos.side == "PE" and pos.condition == "C1")
    check("qty = 65 x 2", pos.qty == 130, pos.qty)
    check("entry at PE LTP 306", pos.entry == 306.0, pos.entry)
    check("SL 347", pos.sl == 347, pos.sl)
    check("target 24050", pos.target == 24050, pos.target)
    eng._on_ws_message(None, b"")          # tolerated
    eng.ltp["PE"] = 250.0
    eng._check_exit_on_tick("SPOT", 24051.0)
    for _ in range(50):
        if eng.position is None:
            break
        time.sleep(0.02)
    check("closed on target", eng.position is None and eng.closed and eng.closed[-1].reason == "TARGET")
    if eng.closed:
        check("P&L = (306-250) x 130", abs((eng.closed[-1].entry - eng.closed[-1].exit) * 130 - 7280) < 1e-6)

print("Stop loss on the sold option's LTP")
eng = make()
eng.ltp = {"SPOT": 23985.0, "CE": 288.0, "PE": 306.0}
for n, (s, c, p) in enumerate(BARS):
    eng._on_bundle(T0 + 300 * n, s, c, p, lag=2.0)
eng.ltp["PE"] = 347.5
eng._check_exit_on_tick("PE", 347.5)
for _ in range(50):
    if eng.position is None:
        break
    time.sleep(0.02)
check("closed on SL", eng.closed and eng.closed[-1].reason == "SL")

print("Stale reversal bar: no entry")
eng = make()
eng.ltp = {"SPOT": 23985.0, "CE": 288.0, "PE": 306.0}
for n, (s, c, p) in enumerate(BARS):
    eng._on_bundle(T0 + 300 * n, s, c, p, lag=400.0 if n == 3 else 2.0)
check("no position", eng.position is None)

print("Target already met at signal: skipped, lock released")
eng = make()
eng.ltp = {"SPOT": 24060.0, "CE": 288.0, "PE": 306.0}
for n, (s, c, p) in enumerate(BARS):
    eng._on_bundle(T0 + 300 * n, s, c, p, lag=2.0)
check("no position", eng.position is None)
check("strategy not locked", not eng.strategy.locked)
check("not marked traded", not eng.traded_today)

print("Second signal after a trade is ignored (one trade per day)")
eng = make()
eng.ltp = {"SPOT": 23985.0, "CE": 288.0, "PE": 306.0}
more = BARS + [
    (B(23985, 23990, 23900, 23905), B(288, 290, 250, 252), B(305, 360, 300, 355)),
    (B(23905, 23960, 23900, 23955), B(252, 280, 250, 278), B(355, 356, 320, 322)),
]
for n, (s, c, p) in enumerate(more):
    eng._on_bundle(T0 + 300 * n, s, c, p, lag=2.0)
check("exactly one position opened", eng.position is not None and eng.traded_today)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")
