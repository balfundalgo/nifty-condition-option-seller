#!/usr/bin/env python3
"""
test_gui.py — build the window and push every event type through it.
Needs a display (Windows runner has one; Linux: xvfb-run -a python test_gui.py).
"""

import sys
import app as A
from strategy import Strategy, Bundle

FAILS = []


def check(name, cond, info=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + str(info)) if not cond else ''}")
    if not cond:
        FAILS.append(name)


def B(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


w = A.App()
w.update()
check("window built", w.winfo_exists())

w._handle("setup", {"atm": 24000, "strikes": {"CE": 23800, "PE": 24200}, "sec": {},
                    "expiry": "2026-09-29", "lot_size": 65, "lots": 1, "mode": "paper"})
check("CE card retitled", w.v["CE_title"].cget("text") == "CE 23800")

s = Strategy()
s.on_bundle(Bundle(0, B(24000, 24050, 23950, 24010), B(300, 320, 280, 305), B(290, 310, 270, 285)))
s.on_bundle(Bundle(300, B(24010, 24040, 23960, 24000), B(300, 305, 275, 285), B(290, 315, 280, 300)))
w._handle("strategy", s.snapshot())
check("C1-PE row shows armed", "armed" in w.rows["C1-PE"][0].cget("text"), w.rows["C1-PE"][0].cget("text"))
check("ranges filled", "SPOT" in w.ranges.get("1.0", "end"))

pos = {"condition": "C1", "side": "PE", "security_id": "1", "strike": 24200, "qty": 65,
       "entry": 306.0, "sl": 347.0, "target": 24050.0, "target_kind": "day_high",
       "pattern": "hammer", "entry_time": "09:35:01", "order_id": "PAPER", "status": "OPEN",
       "exit": 0.0, "exit_time": "", "reason": ""}
w._handle("tick", {"status": "IN TRADE", "mode": "paper",
                   "ltp": {"SPOT": 23990.0, "CE": 290.0, "PE": 300.0},
                   "day_high": 24050.0, "day_low": 23930.0, "position": pos, "upnl": 390.0,
                   "realised": 0.0, "ws": True, "packets": 10, "avg_lag": 1.2,
                   "max_lag": 2.0, "traded_today": True})
check("position shown", "SHORT PE" in w.pos_lbl.cget("text"))
check("uP&L shown", "+390" in w.pnl_lbl.cget("text"), w.pnl_lbl.cget("text"))
w._handle("status", {"status": "IN TRADE"})
w._handle("log", "hello")
w._handle("trade_closed", dict(pos, exit=250.0, reason="TARGET"))
check("closed trade logged", "TRADE CLOSED" in w.logbox.get("1.0", "end"))
cfg = w._collect()
check("settings collect", cfg.sl_buffer == 7.0 and cfg.manip_points == 10.0)
w.update()
w.destroy()

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")
