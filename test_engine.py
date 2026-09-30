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


def wait_closed(eng, n_open, tries=100):
    for _ in range(tries):
        if len(eng.positions) == n_open:
            return
        time.sleep(0.02)


T0 = int(time.time()) - 3600
R1 = (B(24000, 24050, 23950, 24010), B(300, 320, 280, 305), B(290, 310, 270, 285))
C1_PE = [R1,
         (B(24010, 24040, 23960, 24000), B(300, 305, 275, 285), B(290, 315, 280, 300)),
         (B(23990, 23995, 23930, 23940), B(285, 290, 260, 262), B(300, 340, 298, 335)),
         (B(23940, 23990, 23935, 23985), B(262, 290, 260, 288), B(330, 335, 300, 305))]
# C1-PE and MANIP-PE fire on the same candle (bar 4)
BOTH = [R1,
        (B(24010, 24030, 23990, 24000), B(300, 310, 290, 300), B(290, 300, 285, 295)),
        (B(24000, 24020, 23992, 23995), B(300, 302, 275, 280), B(295, 315, 294, 312)),
        (B(23995, 23996, 23935, 23940), B(280, 281, 262, 263), B(315, 340, 310, 335)),
        (B(23940, 23990, 23938, 23985), B(262, 290, 260, 288), B(330, 335, 300, 305))]


def feed(eng, bars, lag=2.0, lag_at=None):
    for n, (s, c, p) in enumerate(bars):
        eng._on_bundle(T0 + 300 * n, s, c, p, lag=(lag_at(n) if lag_at else lag))


print("Paper entry on Condition 1 PE, exit at target")
eng = make()
eng.ltp = {"SPOT": 23985.0, "CE": 288.0, "PE": 306.0}
feed(eng, C1_PE)
pos = eng.positions.get("C1-PE")
check("position opened", pos is not None, list(eng.positions))
if pos:
    check("qty = 65 x 2", pos.qty == 130, pos.qty)
    check("entry at PE LTP 306", pos.entry == 306.0, pos.entry)
    check("SL 347", pos.sl == 347, pos.sl)
    check("target 24050", pos.target == 24050, pos.target)
    eng._on_ws_message(None, b"")          # tolerated
    eng.ltp["PE"] = 250.0
    eng._check_exit_on_tick("SPOT", 24051.0)
    wait_closed(eng, 0)
    check("closed on target", not eng.positions and eng.closed and eng.closed[-1].reason == "TARGET")
    if eng.closed:
        check("P&L = (306-250) x 130", abs((eng.closed[-1].entry - eng.closed[-1].exit) * 130 - 7280) < 1e-6)

print("Stop loss on the sold option's LTP")
eng = make()
eng.ltp = {"SPOT": 23985.0, "CE": 288.0, "PE": 306.0}
feed(eng, C1_PE)
eng.ltp["PE"] = 347.5
eng._check_exit_on_tick("PE", 347.5)
wait_closed(eng, 0)
check("closed on SL", eng.closed and eng.closed[-1].reason == "SL")

print("Stale reversal bar: no entry")
eng = make()
eng.ltp = {"SPOT": 23985.0, "CE": 288.0, "PE": 306.0}
feed(eng, C1_PE, lag_at=lambda n: 400.0 if n == 3 else 2.0)
check("no position", not eng.positions)

print("Target already met at signal: skipped, others carry on")
eng = make()
eng.ltp = {"SPOT": 24060.0, "CE": 288.0, "PE": 306.0}
feed(eng, C1_PE)
check("no position", not eng.positions)
check("C1-PE not marked traded", "C1-PE" not in eng.traded_keys)
check("C1-PE dead, C2-CE still checking",
      eng.strategy.machine("C1-PE").state == "DEAD"
      and eng.strategy.machine("C2-CE").state == "WAIT_SETUP")

print("NEW: two conditions fire on one candle — two independent positions")
eng = make()
eng.ltp = {"SPOT": 23985.0, "CE": 288.0, "PE": 306.0}
feed(eng, BOTH)
check("C1-PE and MANIP-PE both open", sorted(eng.positions) == ["C1-PE", "MANIP-PE"],
      list(eng.positions))
check("each has its own qty", all(p.qty == 130 for p in eng.positions.values()))
check("both marked traded", eng.traded_keys == {"C1-PE", "MANIP-PE"}, eng.traded_keys)

print("NEW: each position exits on its own; the other stays open")
eng.positions["MANIP-PE"].sl = 400.0        # give MANIP a wider stop for this test
eng.ltp["PE"] = 350.0
eng._check_exit_on_tick("PE", 350.0)        # above C1's SL 347, below MANIP's 400
wait_closed(eng, 1)
check("C1-PE closed on SL", [c.key for c in eng.closed] == ["C1-PE"], [c.key for c in eng.closed])
check("MANIP-PE still open", list(eng.positions) == ["MANIP-PE"], list(eng.positions))
eng.manual_square_off()
wait_closed(eng, 0)
check("manual square-off closes the rest", not eng.positions and eng.closed[-1].reason == "MANUAL")

print("NEW: restart restores open positions and traded condition sides")
eng = make()
eng.ltp = {"SPOT": 23985.0, "CE": 288.0, "PE": 306.0}
feed(eng, BOTH)
eng._save_state()
eng2 = make()
st = eng2._load_state()
eng2.traded_keys = set(st["traded_keys"])
eng2.positions = {d["key"]: E.position_from(d) for d in st["positions"]}
eng2.strategy.mark_done(eng2.traded_keys)
eng2.ltp = {"SPOT": 23985.0, "CE": 288.0, "PE": 306.0}
feed(eng2, BOTH)
check("still exactly the two original positions", sorted(eng2.positions) == ["C1-PE", "MANIP-PE"],
      list(eng2.positions))

print("v1.0.x state rows (no 'key') still load")
old = {"condition": "C1", "side": "PE", "security_id": "222", "strike": 24200, "qty": 65,
       "entry": 306.0, "sl": 347.0, "target": 24050.0, "target_kind": "day_high",
       "pattern": "hammer", "entry_time": "09:35:01", "status": "CLOSED", "exit": 250.0}
check("key derived", E.position_from(old).key == "C1-PE")

print("Summary rows cover open and closed trades")
eng = make()
eng.ltp = {"SPOT": 23985.0, "CE": 288.0, "PE": 306.0}
feed(eng, BOTH)
eng.ltp["PE"] = 300.0
sm = eng.summary()
check("two rows, both with live P&L", len(sm["positions"]) == 2
      and all(abs(r["pnl"] - (306 - 300) * 130) < 1e-6 for r in sm["positions"]), sm["positions"])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")
