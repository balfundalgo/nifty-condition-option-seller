#!/usr/bin/env python3
"""
patterns.py — reversal candles
══════════════════════════════
"All possible reversal candles considered." Each pattern is objective and
individually switchable; all are on by default. A pattern is evaluated on
the LAST bar of the list passed in, using up to two bars before it.

Bearish (topping)                 Bullish (bottoming)
─────────────────                 ───────────────────
color_flip     red after green    green after red
engulfing      bearish engulfing  bullish engulfing
pin_bar        shooting star      hammer
harami         bearish harami     bullish harami
piercing       dark cloud cover   piercing line
star           evening star       morning star
doji_confirm   doji + red close   doji + green close
tweezer        tweezer top        tweezer bottom

Bars are dicts with open/high/low/close.
"""

from typing import List, Optional, Dict

PATTERNS = ("color_flip", "engulfing", "pin_bar", "harami", "piercing",
            "star", "doji_confirm", "tweezer")

PATTERN_LABELS = {
    "color_flip": "Colour flip (red after green / green after red)",
    "engulfing": "Engulfing",
    "pin_bar": "Shooting star / Hammer (pin bar)",
    "harami": "Harami",
    "piercing": "Dark cloud cover / Piercing line",
    "star": "Evening star / Morning star",
    "doji_confirm": "Doji + confirmation candle",
    "tweezer": "Tweezer top / bottom",
}

PIN_WICK_RATIO = 2.0        # wick >= 2 x body
DOJI_BODY_RATIO = 0.10      # body <= 10% of range
STAR_BODY_RATIO = 0.30      # middle star body <= 30% of first body
TWEEZER_TOL = 0.0005        # highs/lows within 0.05% of price


def _body(b):   return abs(b["close"] - b["open"])
def _range(b):  return b["high"] - b["low"]
def _green(b):  return b["close"] > b["open"]
def _red(b):    return b["close"] < b["open"]
def _top(b):    return max(b["open"], b["close"])
def _bot(b):    return min(b["open"], b["close"])
def _mid(b):    return (b["open"] + b["close"]) / 2.0


def _is_doji(b) -> bool:
    r = _range(b)
    return r > 0 and _body(b) <= DOJI_BODY_RATIO * r


# ─── bearish ───

def _bear_color_flip(c, p, p2):
    return p is not None and _green(p) and _red(c)


def _bear_engulfing(c, p, p2):
    return (p is not None and _green(p) and _red(c)
            and c["open"] >= p["close"] and c["close"] <= p["open"]
            and _body(c) > _body(p))


def _bear_pin(c, p, p2):
    r = _range(c)
    if r <= 0:
        return False
    upper = c["high"] - _top(c)
    body = max(_body(c), 1e-9)
    in_lower_third = _top(c) <= c["low"] + r / 3.0
    at_high = p is None or c["high"] >= p["high"]
    return upper >= PIN_WICK_RATIO * body and in_lower_third and at_high


def _bear_harami(c, p, p2):
    return (p is not None and _green(p) and _red(c) and _body(p) > 0
            and _top(c) <= p["close"] and _bot(c) >= p["open"]
            and _body(c) < _body(p))


def _bear_dark_cloud(c, p, p2):
    return (p is not None and _green(p) and _red(c)
            and c["open"] >= p["close"]
            and p["open"] < c["close"] < _mid(p))


def _bear_star(c, p, p2):
    return (p2 is not None and p is not None and _green(p2) and _red(c)
            and _body(p2) > 0 and _body(p) <= STAR_BODY_RATIO * _body(p2)
            and _bot(p) >= _mid(p2) and c["close"] < _mid(p2))


def _bear_doji(c, p, p2):
    return p is not None and _is_doji(p) and _red(c) and c["close"] < _bot(p)


def _bear_tweezer(c, p, p2):
    if p is None or not (_green(p) and _red(c)):
        return False
    tol = TWEEZER_TOL * max(c["high"], 1e-9)
    return abs(c["high"] - p["high"]) <= tol


# ─── bullish (mirror) ───

def _bull_color_flip(c, p, p2):
    return p is not None and _red(p) and _green(c)


def _bull_engulfing(c, p, p2):
    return (p is not None and _red(p) and _green(c)
            and c["open"] <= p["close"] and c["close"] >= p["open"]
            and _body(c) > _body(p))


def _bull_pin(c, p, p2):
    r = _range(c)
    if r <= 0:
        return False
    lower = _bot(c) - c["low"]
    body = max(_body(c), 1e-9)
    in_upper_third = _bot(c) >= c["high"] - r / 3.0
    at_low = p is None or c["low"] <= p["low"]
    return lower >= PIN_WICK_RATIO * body and in_upper_third and at_low


def _bull_harami(c, p, p2):
    return (p is not None and _red(p) and _green(c) and _body(p) > 0
            and _top(c) <= p["open"] and _bot(c) >= p["close"]
            and _body(c) < _body(p))


def _bull_piercing(c, p, p2):
    return (p is not None and _red(p) and _green(c)
            and c["open"] <= p["close"]
            and _mid(p) < c["close"] < p["open"])


def _bull_star(c, p, p2):
    return (p2 is not None and p is not None and _red(p2) and _green(c)
            and _body(p2) > 0 and _body(p) <= STAR_BODY_RATIO * _body(p2)
            and _top(p) <= _mid(p2) and c["close"] > _mid(p2))


def _bull_doji(c, p, p2):
    return p is not None and _is_doji(p) and _green(c) and c["close"] > _top(p)


def _bull_tweezer(c, p, p2):
    if p is None or not (_red(p) and _green(c)):
        return False
    tol = TWEEZER_TOL * max(c["low"], 1e-9)
    return abs(c["low"] - p["low"]) <= tol


_BEAR = {"color_flip": _bear_color_flip, "engulfing": _bear_engulfing,
         "pin_bar": _bear_pin, "harami": _bear_harami, "piercing": _bear_dark_cloud,
         "star": _bear_star, "doji_confirm": _bear_doji, "tweezer": _bear_tweezer}
_BULL = {"color_flip": _bull_color_flip, "engulfing": _bull_engulfing,
         "pin_bar": _bull_pin, "harami": _bull_harami, "piercing": _bull_piercing,
         "star": _bull_star, "doji_confirm": _bull_doji, "tweezer": _bull_tweezer}

_BEAR_NAMES = {"color_flip": "red-after-green", "engulfing": "bearish engulfing",
               "pin_bar": "shooting star", "harami": "bearish harami",
               "piercing": "dark cloud cover", "star": "evening star",
               "doji_confirm": "doji + red", "tweezer": "tweezer top"}
_BULL_NAMES = {"color_flip": "green-after-red", "engulfing": "bullish engulfing",
               "pin_bar": "hammer", "harami": "bullish harami",
               "piercing": "piercing line", "star": "morning star",
               "doji_confirm": "doji + green", "tweezer": "tweezer bottom"}

# Most specific first, so the log names the strongest pattern that matched.
_ORDER = ("engulfing", "star", "piercing", "pin_bar", "harami", "tweezer",
          "doji_confirm", "color_flip")


def _detect(bars: List[dict], table, names, enabled: Optional[Dict[str, bool]]):
    if not bars:
        return None
    c = bars[-1]
    p = bars[-2] if len(bars) >= 2 else None
    p2 = bars[-3] if len(bars) >= 3 else None
    for key in _ORDER:
        if enabled is not None and not enabled.get(key, True):
            continue
        try:
            if table[key](c, p, p2):
                return names[key]
        except Exception:
            continue
    return None


def bearish_reversal(bars: List[dict], enabled: Optional[Dict[str, bool]] = None) -> Optional[str]:
    """Name of the bearish reversal completed by bars[-1], or None."""
    return _detect(bars, _BEAR, _BEAR_NAMES, enabled)


def bullish_reversal(bars: List[dict], enabled: Optional[Dict[str, bool]] = None) -> Optional[str]:
    """Name of the bullish reversal completed by bars[-1], or None."""
    return _detect(bars, _BULL, _BULL_NAMES, enabled)
