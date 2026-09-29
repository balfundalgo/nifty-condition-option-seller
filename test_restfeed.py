#!/usr/bin/env python3
"""test_restfeed.py — rollup, forming-bar removal, ordering and sync.   python test_restfeed.py"""

import sys
from datetime import datetime
from candles import aggregate
from restfeed import RestCandleFeed, BarSync
from dhan import IST, anchor_of

FAILS = []
A = int(datetime(2026, 9, 24, 9, 15, tzinfo=IST).timestamp())


def check(name, cond, info=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + str(info)) if not cond else ''}")
    if not cond:
        FAILS.append(name)


def m(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1}


print("Clock rollup anchored to 09:15")
src = [m(A - 60, 1, 1, 1, 1)] + [m(A + 60 * k, 100 + k, 101 + k, 99 + k, 100.5 + k) for k in range(10)]
bars = aggregate(src, 5, "clock", anchor_of)
check("pre-open minute dropped, two bars", len(bars) == 2, len(bars))
check("first bar starts 09:15", bars[0]["ts"] == A)
check("first bar OHLC", (bars[0]["open"], bars[0]["high"], bars[0]["low"], bars[0]["close"]) == (100, 105, 99, 104.5), bars[0])
gap = [x for x in src if x["ts"] != A + 120]
check("missing minute does not shift the next bar",
      aggregate(gap, 5, "clock", anchor_of)[1]["ts"] == A + 300)


print("Feed: forming minute removed, bars in order, lag recorded")
now = [A + 305]
data = {"13": [m(A + 60 * k, 1, 2, 0.5, 1.5) for k in range(6)]}       # 09:15..09:20 (09:20 forming)
out = []
f = RestCandleFeed({"SPOT": ("13", "IDX_I", "INDEX")},
                   fetch_1m=lambda s, g, i: data[s], anchor_of=anchor_of,
                   on_bar=lambda leg, b: out.append((leg, b["ts"], round(b["lag"]))),
                   session_anchor=A, clock=lambda: now[0])
f.poll_leg("SPOT", ("13", "IDX_I", "INDEX"))
check("09:15 bar emitted once complete", out == [("SPOT", A, 5)], out)
f.poll_leg("SPOT", ("13", "IDX_I", "INDEX"))
check("not emitted twice", len(out) == 1, out)
now[0] = A + 600 + 1
data["13"] += [m(A + 60 * k, 1, 2, 0.5, 1.5) for k in range(6, 10)]
f.poll_leg("SPOT", ("13", "IDX_I", "INDEX"))
check("09:20 bar emitted", [x[1] for x in out] == [A, A + 300], out)


print("Feed: a failed / empty fetch never reports the leg as polled")
polls = []
f2 = RestCandleFeed({"SPOT": ("13", "IDX_I", "INDEX")}, fetch_1m=lambda s, g, i: [],
                    anchor_of=anchor_of, on_bar=lambda leg, b: None,
                    on_poll=lambda *a: polls.append(a), session_anchor=A,
                    clock=lambda: A + 3600)
f2.poll_leg("SPOT", ("13", "IDX_I", "INDEX"))
check("on_poll not called", polls == [], polls)
now = [A + 305]
polls = []
f3 = RestCandleFeed({"SPOT": ("13", "IDX_I", "INDEX")},
                    fetch_1m=lambda s, g, i: [m(A + 60 * k, 1, 2, 0.5, 1.5) for k in range(6)],
                    anchor_of=anchor_of, on_bar=lambda leg, b: polls.append(("bar", b["ts"])),
                    on_poll=lambda leg, at, last: polls.append(("poll", last)),
                    session_anchor=A, clock=lambda: now[0])
f3.poll_leg("SPOT", ("13", "IDX_I", "INDEX"))
check("bars handed over BEFORE the poll is reported, with last completed minute",
      polls == [("bar", A), ("poll", A + 240)], polls)


print("BarSync: waits for all legs, releases in order")
now = [A + 310]
got = []
bs = BarSync(["SPOT", "CE", "PE"], lambda ts, s, c, p, lag: got.append((ts, bool(s), bool(c), bool(p))),
             clock=lambda: now[0])
bar = lambda ts: {"ts": ts, "open": 1, "high": 1, "low": 1, "close": 1, "lag": 2}
bs.add("SPOT", bar(A))
bs.add("CE", bar(A))
check("not released with PE missing", got == [], got)
bs.add("PE", bar(A))
check("released when all three arrive", got == [(A, True, True, True)], got)
bs.add("SPOT", bar(A + 300))
bs.add("PE", bar(A + 300))
now[0] = A + 3600
check("time alone never releases a bar", got[-1][0] == A, got)
bs.polled("CE", now[0], A + 540)          # CE data reaches 09:24 — still inside the 09:20 bar
check("CE data not yet past the bar -> held", got[-1][0] == A, got)
bs.polled("CE", now[0], A + 600)          # CE has a completed 09:25 minute, no 09:20 trades
check("CE data past the bar with no candle there -> released without CE",
      got[-1] == (A + 300, True, False, True), got)
bs.add("SPOT", bar(A + 600))
bs.add("SPOT", bar(A + 900))
bs.add("CE", bar(A + 600))
check("900 not released ahead of 600", got[-1][0] == A + 300, got)
bs.add("PE", bar(A + 600))
bs.add("CE", bar(A + 900))
bs.add("PE", bar(A + 900))
check("interleaved legs still released in order, all complete",
      [g[0] for g in got] == [A, A + 300, A + 600, A + 900], [g[0] for g in got])


print("29-Sep outage replay: options come back before spot")
got = []
bs = BarSync(["SPOT", "CE", "PE"], lambda ts, s, c, p, lag: got.append((ts, bool(s), bool(c), bool(p))),
             clock=lambda: A + 4 * 3600)
for leg in ("SPOT", "CE", "PE"):
    bs.add(leg, bar(A))
# network down for two hours: no successful polls at all -> nothing to report
backlog = [A + 300 * k for k in range(1, 24)]
for ts in backlog:                         # CE and PE recover first and dump their backlog
    bs.add("CE", bar(ts))
    bs.polled("CE", 0, ts + 300)
    bs.add("PE", bar(ts))
    bs.polled("PE", 0, ts + 300)
check("nothing released while spot is still missing", [g[0] for g in got] == [A], got[-3:])
for ts in backlog:                         # spot recovers
    bs.add("SPOT", bar(ts))
bs.polled("SPOT", 0, backlog[-1] + 300)
check("whole backlog released, in order, every bar WITH spot",
      [g[0] for g in got] == [A] + backlog and all(g[1] and g[2] and g[3] for g in got),
      got[:3])
check("no spot candle dropped as late", bs.late_dropped == 0, bs.late_dropped)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")
