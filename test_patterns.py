#!/usr/bin/env python3
"""test_patterns.py — each reversal pattern, hand-built.   python test_patterns.py"""

import sys
from patterns import bearish_reversal, bullish_reversal, PATTERNS

FAILS = []


def B(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def only(key):
    return {p: (p == key) for p in PATTERNS}


def check(name, got, want):
    ok = (got is not None) == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  -> {got}")
    if not ok:
        FAILS.append(name)


print("Bearish")
check("colour flip", bearish_reversal([B(100, 106, 99, 105), B(105, 106, 101, 102)], only("color_flip")), True)
check("engulfing", bearish_reversal([B(100, 106, 99, 105), B(106, 107, 97, 98)], only("engulfing")), True)
check("engulfing: body too small", bearish_reversal([B(100, 106, 99, 105), B(105, 106, 102, 103)], only("engulfing")), False)
check("shooting star", bearish_reversal([B(100, 105, 99, 104), B(104, 112, 103, 103.5)], only("pin_bar")), True)
check("shooting star: not at the high", bearish_reversal([B(100, 115, 99, 104), B(104, 112, 103, 103.5)], only("pin_bar")), False)
check("harami", bearish_reversal([B(100, 111, 99, 110), B(107, 108, 104, 105)], only("harami")), True)
check("dark cloud", bearish_reversal([B(100, 111, 99, 110), B(112, 113, 103, 104)], only("piercing")), True)
check("evening star", bearish_reversal([B(100, 111, 99, 110), B(110, 112, 109, 111), B(110, 110, 101, 102)], only("star")), True)
check("doji + red", bearish_reversal([B(100, 104, 96, 100.2), B(100, 101, 95, 96)], only("doji_confirm")), True)
check("tweezer top", bearish_reversal([B(100, 110, 99, 108), B(108, 110.02, 101, 102)], only("tweezer")), True)
check("all on: green bar is never bearish", bearish_reversal([B(100, 106, 99, 105), B(105, 110, 104, 109)]), False)

print("Bullish")
check("colour flip", bullish_reversal([B(105, 106, 99, 100), B(100, 104, 99, 103)], only("color_flip")), True)
check("engulfing", bullish_reversal([B(105, 106, 99, 100), B(99, 108, 98, 107)], only("engulfing")), True)
check("hammer", bullish_reversal([B(105, 106, 99, 100), B(100.5, 101, 92, 101)], only("pin_bar")), True)
check("harami", bullish_reversal([B(110, 111, 99, 100), B(103, 106, 102, 105)], only("harami")), True)
check("piercing", bullish_reversal([B(110, 111, 99, 100), B(98, 108, 97, 107)], only("piercing")), True)
check("morning star", bullish_reversal([B(110, 111, 99, 100), B(100, 101, 98, 99), B(100, 109, 99, 108)], only("star")), True)
check("doji + green", bullish_reversal([B(100, 104, 96, 99.8), B(100, 106, 99, 105)], only("doji_confirm")), True)
check("tweezer bottom", bullish_reversal([B(108, 109, 100, 102), B(102, 107, 100.02, 106)], only("tweezer")), True)
check("disabled pattern not matched", bullish_reversal([B(105, 106, 99, 100), B(100, 104, 99, 103)], only("engulfing")), False)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")
