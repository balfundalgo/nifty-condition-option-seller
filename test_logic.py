#!/usr/bin/env python3
"""
test_logic.py — every condition, both directions where it matters, driven
bar by bar through the pure Strategy. No network.

    python test_logic.py
"""

import sys
from strategy import Strategy, StrategyParams, Bundle

T0 = 1_758_685_500          # any 09:15 anchor; only ordering matters here
FAILS = []


def B(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


# First candle, shared by every scenario:
#   spot 24000 / 24050 / 23950 / 24010
#   CE    300 /   320 /   280 /   305
#   PE    290 /   310 /   270 /   285
R1 = (B(24000, 24050, 23950, 24010), B(300, 320, 280, 305), B(290, 310, 270, 285))


def run(bars, params=None, can_enter=None):
    s = Strategy(params)
    sig = None
    for n, (sp, ce, pe) in enumerate([R1] + bars):
        ok = True if can_enter is None else can_enter(n)
        out = s.on_bundle(Bundle(T0 + 300 * n, sp, ce, pe), can_enter=ok)
        if out and sig is None:
            sig = (n, out)
    return s, sig


def check(name, cond, info=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + info) if info and not cond else ''}")
    if not cond:
        FAILS.append(name)


def state(s, key):
    return next(m for m in s.machines if m.key == key).state


# ─────────────────────────────────────────────────────────────────────────────
print("Condition 1 — PE side")
bars = [
    # 09:20 spot inside; CE breaks LOW (275<280), PE breaks HIGH (315>310)
    (B(24010, 24040, 23960, 24000), B(300, 305, 275, 285), B(290, 315, 280, 300)),
    # 09:25 spot CLOSES below 23950
    (B(23990, 23995, 23930, 23940), B(285, 290, 260, 262), B(300, 340, 298, 335)),
    # 09:30 bullish reversal on spot
    (B(23940, 23990, 23935, 23985), B(262, 290, 260, 288), B(330, 335, 300, 305)),
]
s, sig = run(bars)
check("fires on bar 3", sig and sig[0] == 3, str(sig))
if sig:
    _, g = sig
    check("condition C1", g.condition == "C1", g.condition)
    check("sells PE", g.side == "PE", g.side)
    check("target = spot day high 24050", g.target == 24050 and g.target_kind == "day_high",
          f"{g.target} {g.target_kind}")
    check("SL = max(335, 340) + 7 = 347", g.sl == 347, str(g.sl))
check("C2-PE died (CE had broken)", state(s, "C2-PE") == "DEAD")
check("C5-PE died (CE not untouched)", state(s, "C5-PE") == "DEAD")
check("everything else locked", state(s, "C3-CE") in ("LOCKED", "DEAD"))

print("Condition 1 — no entry while can_enter is False, fires on the next reversal")
bars2 = bars + [
    (B(23985, 23990, 23945, 23950), B(288, 300, 285, 298), B(305, 330, 300, 325)),   # red
    (B(23950, 24000, 23948, 23995), B(298, 300, 280, 282), B(325, 326, 295, 298)),   # green
]
s, sig = run(bars2, can_enter=lambda n: n != 3)
check("skipped stale bar 3, fired on bar 5", sig and sig[0] == 5, str(sig and sig[0]))
check("still C1 PE", sig and sig[1].condition == "C1" and sig[1].side == "PE")

print("Condition 1 — spot closed outside before the split: dead")
bars = [
    (B(24010, 24060, 24000, 24055), B(305, 318, 300, 316), B(285, 290, 272, 274)),   # spot closes above
    (B(24055, 24058, 23990, 24000), B(316, 325, 300, 302), B(274, 290, 268, 288)),   # split now
]
s, sig = run(bars)
check("C1-CE dead", state(s, "C1-CE") == "DEAD")
check("C1-PE dead", state(s, "C1-PE") == "DEAD")

# ─────────────────────────────────────────────────────────────────────────────
print("Condition 2 — CE side")
bars = [
    # spot wicks above 24050 (24070), CE breaks high (325), PE stays 272..300
    (B(24010, 24070, 24005, 24045), B(305, 325, 300, 318), B(285, 300, 272, 276)),
    # spot closes above new high 24070
    (B(24046, 24085, 24040, 24080), B(318, 335, 315, 332), B(276, 280, 265, 266)),
    # bearish engulfing on spot
    (B(24080, 24085, 24030, 24035), B(332, 338, 310, 312), B(266, 285, 262, 282)),
]
s, sig = run(bars)
check("fires on bar 3", sig and sig[0] == 3, str(sig and sig[0]))
if sig:
    g = sig[1]
    check("condition C2", g.condition == "C2", g.condition)
    check("sells CE", g.side == "CE")
    check("target = first open 24000", g.target == 24000 and g.target_kind == "first_open")
    check("SL = max(338, 335) + 7 = 345", g.sl == 345, str(g.sl))
    check("pattern named engulfing", "engulfing" in g.pattern, g.pattern)
check("C3-CE died (CE broke with spot)", state(s, "C3-CE") == "DEAD")

print("Condition 2 — PE broke too (not untouched): dead")
bars = [
    (B(24010, 24070, 24005, 24045), B(305, 325, 300, 318), B(285, 300, 265, 268)),
]
s, _ = run(bars)
check("C2-CE dead", state(s, "C2-CE") == "DEAD")

print("Condition 2 — PE side (mirror)")
bars = [
    (B(24000, 24005, 23930, 23955), B(300, 302, 283, 285), B(285, 318, 280, 312)),   # spot wicks low, PE breaks high, CE held
    (B(23955, 23958, 23910, 23915), B(285, 288, 270, 272), B(312, 335, 310, 332)),   # close below new low 23930
    (B(23915, 23960, 23912, 23955), B(272, 300, 270, 298), B(332, 334, 300, 302)),   # green after red on spot
]
s, sig = run(bars)
check("C2 PE fires", sig and sig[1].condition == "C2" and sig[1].side == "PE", str(sig))
check("target first open", sig and sig[1].target == 24000)

# ─────────────────────────────────────────────────────────────────────────────
print("Condition 3 — PE side")
bars = [
    # spot wicks below 23950, both options inside their ranges
    (B(24000, 24010, 23940, 23960), B(300, 312, 285, 290), B(285, 305, 275, 300)),
    # PE breaks its high 310 (318)
    (B(23960, 23990, 23955, 23970), B(290, 300, 284, 286), B(300, 318, 298, 315)),
    # PE red after green
    (B(23970, 24000, 23965, 23995), B(286, 305, 285, 303), B(314, 316, 290, 292)),
]
s, sig = run(bars)
check("fires on bar 3", sig and sig[0] == 3, str(sig and sig[0]))
if sig:
    g = sig[1]
    check("condition C3", g.condition == "C3", g.condition)
    check("sells PE", g.side == "PE")
    check("target = day high 24050", g.target == 24050 and g.target_kind == "day_high")
    check("SL = max(316, 318) + 7 = 325", g.sl == 325, str(g.sl))

print("Condition 3 — CE side (mirror)")
bars = [
    (B(24010, 24060, 24000, 24040), B(305, 318, 300, 315), B(285, 290, 272, 276)),   # spot wicks high, both held
    (B(24040, 24048, 24020, 24030), B(315, 326, 312, 324), B(276, 282, 274, 280)),   # CE breaks 320
    (B(24030, 24035, 24000, 24005), B(324, 325, 305, 306), B(280, 292, 278, 290)),   # CE bearish
]
s, sig = run(bars)
check("C3 CE fires", sig and sig[1].condition == "C3" and sig[1].side == "CE", str(sig))
check("target = day low 23950", sig and sig[1].target == 23950)

# ─────────────────────────────────────────────────────────────────────────────
print("Condition 5 — CE side (double top)")
PE_FLAT = B(285, 295, 280, 285)
bars = [
    (B(24010, 24030, 23980, 24000), B(305, 325, 303, 322), PE_FLAT),   # CE breaks 320, PE untouched
    (B(24000, 24020, 23985, 24010), B(322, 324, 305, 308), PE_FLAT),   # rev #1, peak1 = 325
    (B(24010, 24040, 24000, 24020), B(308, 330, 306, 328), PE_FLAT),   # breaks peak1
    (B(24020, 24030, 23990, 23995), B(328, 331, 310, 312), PE_FLAT),   # rev #2
]
s, sig = run(bars)
check("fires on bar 4", sig and sig[0] == 4, str(sig and sig[0]))
if sig:
    g = sig[1]
    check("condition C5", g.condition == "C5", g.condition)
    check("sells CE", g.side == "CE")
    check("target = day low 23950", g.target == 23950 and g.target_kind == "day_low")
    check("SL = max(331, 330) + 7 = 338", g.sl == 338, str(g.sl))

print("Condition 5 — no fire on the first reversal alone")
s, sig = run(bars[:2])
check("no signal yet", sig is None)
check("C5-CE waiting for break of peak 1", state(s, "C5-CE") == "WAIT_BREAK2", state(s, "C5-CE"))

print("Condition 5 — PE side (mirror)")
CE_FLAT = B(300, 310, 290, 300)
bars = [
    (B(24010, 24030, 23980, 23990), CE_FLAT, B(290, 315, 288, 312)),
    (B(23990, 24000, 23970, 23995), CE_FLAT, B(312, 313, 295, 297)),
    (B(23995, 24000, 23960, 23975), CE_FLAT, B(297, 320, 296, 318)),
    (B(23975, 24010, 23970, 24005), CE_FLAT, B(318, 319, 300, 302)),
]
s, sig = run(bars)
check("C5 PE fires", sig and sig[1].condition == "C5" and sig[1].side == "PE", str(sig))
check("target = day high 24050", sig and sig[1].target == 24050)

# ─────────────────────────────────────────────────────────────────────────────
print("Manipulation — PE side")
bars = [
    # 09:20 = R2: spot 23980..24030, CE 290..310, PE 285..300 (all inside R1)
    (B(24000, 24030, 23980, 24010), B(300, 310, 290, 305), B(290, 300, 285, 288)),
    # PE > 300, CE < 290 (split vs R2), spot closes inside R2
    (B(24010, 24020, 23982, 23990), B(305, 306, 285, 287), B(288, 305, 287, 302)),
    # spot closes 23965 <= 23980 - 10  (still inside R1: low 23955)
    (B(23990, 23992, 23955, 23965), B(287, 288, 282, 283), B(302, 308, 300, 307)),
    # bullish reversal on spot
    (B(23965, 23995, 23960, 23990), B(283, 300, 282, 298), B(307, 309, 292, 294)),
]
s, sig = run(bars)
check("fires on bar 4", sig and sig[0] == 4, str(sig and sig[0]))
if sig:
    g = sig[1]
    check("condition MANIP", g.condition == "MANIP", g.condition)
    check("sells PE", g.side == "PE")
    check("target = day high 24050", g.target == 24050)
    check("SL = max(309, 308) + 7 = 316", g.sl == 316, str(g.sl))

print("Manipulation — 10-point filter not met: no trigger")
bars_nf = bars[:2] + [
    (B(23990, 23992, 23970, 23975), B(287, 288, 282, 283), B(302, 308, 300, 307)),   # 23975 > 23970
    (B(23975, 23995, 23972, 23990), B(283, 300, 282, 298), B(307, 309, 292, 294)),
]
s, sig = run(bars_nf)
check("no signal", sig is None, str(sig))
check("MANIP-PE still waiting for the close", state(s, "MANIP-PE") == "WAIT_TRIGGER",
      state(s, "MANIP-PE"))

print("Manipulation — disabled in settings")
p = StrategyParams()
p.enabled["MANIP"] = False
s, sig = run(bars, params=p)
check("no MANIP signal", not sig or sig[1].condition != "MANIP", str(sig))

# ─────────────────────────────────────────────────────────────────────────────
print("One trade per day")
bars = [
    (B(24010, 24040, 23960, 24000), B(300, 305, 275, 285), B(290, 315, 280, 300)),
    (B(23990, 23995, 23930, 23940), B(285, 290, 260, 262), B(300, 340, 298, 335)),
    (B(23940, 23990, 23935, 23985), B(262, 290, 260, 288), B(330, 335, 300, 305)),
    (B(23985, 23990, 23900, 23905), B(288, 290, 250, 252), B(305, 360, 300, 355)),
    (B(23905, 23960, 23900, 23955), B(252, 280, 250, 278), B(355, 356, 320, 322)),
]
st = Strategy()
got = []
for n, (sp, ce, pe) in enumerate([R1] + bars):
    out = st.on_bundle(Bundle(T0 + 300 * n, sp, ce, pe))
    if out:
        got.append(out)
check("exactly one signal", len(got) == 1, str(len(got)))

print("Reversal on the trigger bar is off by default, on when enabled")
bars = [
    (B(24010, 24040, 23960, 24000), B(300, 305, 275, 285), B(290, 315, 280, 300)),
    (B(23990, 23995, 23930, 23945), B(285, 290, 260, 262), B(300, 340, 298, 335)),   # red, closes below
    (B(23945, 23950, 23920, 23948), B(262, 270, 255, 258), B(335, 345, 330, 340)),   # green AND closes below
]
s, sig = run(bars)
check("default: bar 3 counts as reversal (after trigger bar 2)", sig and sig[0] == 3, str(sig and sig[0]))
bars_same = [
    (B(24010, 24040, 23960, 24000), B(300, 305, 275, 285), B(290, 315, 280, 300)),   # split, C1-PE armed
    (B(24000, 24005, 23985, 23990), B(285, 290, 283, 286), B(300, 305, 296, 302)),   # red, inside
    (B(23920, 23948, 23900, 23945), B(286, 288, 262, 264), B(302, 340, 300, 330)),   # GREEN and closes below 23950
]
s, sig = run(bars_same)
check("default: no fire on the trigger bar itself", sig is None, str(sig))
p = StrategyParams(allow_reversal_on_trigger_bar=True)
s, sig = run(bars_same, params=p)
check("enabled: fires on the trigger bar", sig and sig[0] == 3, str(sig and sig[0]))

print("Skipped signal releases the lock; that machine is done, others resume")
bars = [
    (B(24010, 24040, 23960, 24000), B(300, 305, 275, 285), B(290, 315, 280, 300)),
    (B(23990, 23995, 23930, 23940), B(285, 290, 260, 262), B(300, 340, 298, 335)),
    (B(23940, 23990, 23935, 23985), B(262, 290, 260, 288), B(330, 335, 300, 305)),
]
s, sig = run(bars)
s.release(sig[1], "test")
check("C1-PE dead after release", state(s, "C1-PE") == "DEAD")
check("not locked", not s.locked)
check("other machines restored (not LOCKED)",
      all(m.state != "LOCKED" for m in s.machines), [m.state for m in s.machines])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")
