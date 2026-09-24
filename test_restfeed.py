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
now[0] = A + 600 + 25
bs.polled("CE", now[0])
check("CE missing but polled after close+grace -> released without CE",
      got[-1] == (A + 300, True, False, True), got)
# each leg is in order (the feed guarantees it) but legs interleave freely
bs.add("SPOT", bar(A + 600))
bs.add("SPOT", bar(A + 900))
bs.add("CE", bar(A + 600))
check("900 not released ahead of 600", [g[0] for g in got][-1] == A + 300, got)
bs.add("PE", bar(A + 600))
bs.add("CE", bar(A + 900))
bs.add("PE", bar(A + 900))
check("interleaved legs still released in order, all complete",
      [g[0] for g in got] == [A, A + 300, A + 600, A + 900], [g[0] for g in got])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")
