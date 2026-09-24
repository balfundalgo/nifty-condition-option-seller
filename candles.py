#!/usr/bin/env python3
"""
candles.py — roll Dhan 1-minute bars into 5-minute bars
═══════════════════════════════════════════════════════
Same rollup code path as sensex-vwap-ladder: always built from 1-minute
source, never from a native interval, so there is one implementation to trust.

Default mode here is "clock": bars are wall-clock buckets anchored to 09:15.
This strategy is defined on the 09:15–09:20 and 09:20–09:25 candles by the
clock, so a missing minute on an option must NOT shift every later candle —
which is what "count" mode (ChartIQ-style) would do. "count" is kept for
comparison against a chart.
"""

from typing import List, Dict, Sequence, Callable, Optional

AGG_MODES = ("clock", "count")


def _new(ts: int, c: dict) -> dict:
    return {"ts": ts, "open": c["open"], "high": c["high"], "low": c["low"],
            "close": c["close"], "volume": c.get("volume", 0.0)}


def _merge(b: dict, c: dict):
    b["high"] = max(b["high"], c["high"])
    b["low"] = min(b["low"], c["low"])
    b["close"] = c["close"]
    b["volume"] += c.get("volume", 0.0)


def aggregate(candles_1m: Sequence[dict], period: int = 5, mode: str = "clock",
              anchor_of: Optional[Callable[[int], int]] = None) -> List[dict]:
    if mode not in AGG_MODES:
        raise ValueError(f"mode must be one of {AGG_MODES}")
    src = sorted((c for c in candles_1m if c.get("ts")), key=lambda c: c["ts"])
    if not src:
        return []
    if mode == "clock":
        if anchor_of is None:
            raise ValueError("clock mode needs anchor_of")
        step = period * 60
        buckets: Dict[int, dict] = {}
        for c in src:
            a = anchor_of(int(c["ts"]))
            if int(c["ts"]) < a:
                continue                    # pre-open minutes never form a bar
            key = a + ((int(c["ts"]) - a) // step) * step
            if key in buckets:
                _merge(buckets[key], c)
            else:
                buckets[key] = _new(key, c)
        return [buckets[k] for k in sorted(buckets)]

    # count mode — restart grouping each session
    day_of = anchor_of or (lambda ts: ts // 86400)
    out, run, cur = [], [], None
    for c in src:
        d = day_of(int(c["ts"]))
        if cur is not None and d != cur:
            out += _group(run, period)
            run = []
        cur = d
        run.append(c)
    out += _group(run, period)
    return out


def _group(run: List[dict], period: int) -> List[dict]:
    out = []
    for i in range(0, len(run), period):
        chunk = run[i:i + period]
        b = _new(int(chunk[0]["ts"]), chunk[0])
        for x in chunk[1:]:
            _merge(b, x)
        out.append(b)
    return out


def members_per_bar(raw: Sequence[dict], bars: Sequence[dict], period: int,
                    mode: str, anchor_of: Optional[Callable[[int], int]]) -> List[int]:
    """How many source minutes went into each rolled-up bar."""
    starts = [b["ts"] for b in bars]
    counts = [0] * len(bars)
    if not starts:
        return counts
    idx = {t: n for n, t in enumerate(starts)}
    if mode == "clock":
        step = period * 60
        for c in raw:
            ts = int(c["ts"])
            a = anchor_of(ts)
            if ts < a:
                continue
            key = a + ((ts - a) // step) * step
            if key in idx:
                counts[idx[key]] += 1
        return counts
    cur = -1
    for t in sorted(int(c["ts"]) for c in raw):
        if t in idx:
            cur = idx[t]
        if cur >= 0:
            counts[cur] += 1
    return counts
