#!/usr/bin/env python3
"""
restfeed.py — 5-minute candles from REST, synchronised across spot/CE/PE
════════════════════════════════════════════════════════════════════════
The hybrid model from sensex-vwap-ladder:

    REST       candles -> ranges, breaks, closes, reversal patterns, signals
    WebSocket  LTP only -> stop loss, target, fill price, day extremes

Carried-over protections, each from a real incident on that project:
  * drop_forming   Dhan serves the still-forming minute; it is removed before
                   anything sees it, so no signal uses a half-built candle
  * session anchor nothing before today's 09:15 is ever emitted
  * hard deadline  each poll runs in a worker and is abandoned after 25s, so
                   one dead keep-alive socket cannot blind the strategy
  * blind alarm    no new candle for 5 minutes in market hours says so loudly
  * lag recorded   every bar carries how late it arrived; the engine refuses
                   to ENTER on a stale bar (it still updates state)

BarSync is new here: this strategy compares spot, CE and PE on the SAME
candle, so bars are released as one Bundle per timestamp, in order.
"""

import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Dict, Callable, Optional, List, Tuple

from candles import aggregate, members_per_bar

log = logging.getLogger("FCOS")

LegSpec = Tuple[str, str, str]          # (security_id, exchange_segment, instrument)


class RestCandleFeed:
    def __init__(self, legs: Dict[str, LegSpec], fetch_1m: Callable,
                 anchor_of: Callable, on_bar: Callable, period: int = 5,
                 poll: float = 3.0, grace: float = 8.0, agg_mode: str = "clock",
                 session_anchor: int = 0, on_poll: Optional[Callable] = None,
                 on_blind: Optional[Callable] = None, blind_seconds: float = 300.0,
                 clock: Callable[[], float] = time.time):
        self.legs = dict(legs)
        self.fetch_1m = fetch_1m
        self.anchor_of = anchor_of
        self.on_bar = on_bar
        self.on_poll = on_poll
        self.on_blind = on_blind
        self.period = period
        self.poll = poll
        self.grace = grace
        self.agg_mode = agg_mode
        self.session_anchor = int(session_anchor or 0)
        self.blind_seconds = blind_seconds
        self.clock = clock

        self._last_ts: Dict[str, int] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="restpoll")
        self.hard_deadline = 25.0
        self.latencies: List[float] = []
        self.polls = self.errors = self.abandoned = 0
        self.last_bar_at = clock()
        self._blind_warned = 0.0

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="restfeed")
        self._thread.start()
        log.info(f"  REST candle feed started — {len(self.legs)} legs, every "
                 f"{self.poll:.0f}s, {self.period}m bars ({self.agg_mode})")

    def stop(self):
        self._stop.set()
        try:
            self._pool.shutdown(wait=False)
        except Exception:
            pass

    @staticmethod
    def drop_forming(raw: List[dict], now: float, minute: int = 60) -> List[dict]:
        return [c for c in raw if int(c["ts"]) + minute <= now]

    def _loop(self):
        while not self._stop.is_set():
            for leg, spec in list(self.legs.items()):
                if self._stop.is_set():
                    break
                try:
                    fut = self._pool.submit(self.poll_leg, leg, spec)
                    fut.result(timeout=self.hard_deadline)
                except FutureTimeout:
                    self.abandoned += 1
                    self.errors += 1
                    log.error(f"  [{leg}] poll exceeded {self.hard_deadline:.0f}s — "
                              f"abandoned, carrying on")
                except Exception as e:
                    self.errors += 1
                    log.error(f"  [{leg}] REST poll error: {e}")
            self._check_blind()
            self._stop.wait(self.poll)

    def _check_blind(self):
        quiet = self.clock() - self.last_bar_at
        if quiet < self.blind_seconds:
            return
        now = self.clock()
        if now - self._blind_warned > 120:
            self._blind_warned = now
            log.error(f"  NO CANDLES FOR {quiet / 60:.0f} MINUTES — the strategy is "
                      f"blind. Check token / network / market status.")
            if self.on_blind:
                try:
                    self.on_blind(quiet)
                except Exception:
                    pass

    def poll_leg(self, leg: str, spec: LegSpec):
        sec, seg, inst = spec
        raw = self.fetch_1m(sec, seg, inst)
        self.polls += 1
        now = self.clock()
        if self.on_poll:
            self.on_poll(leg, now)
        if not raw:
            return
        raw = self.drop_forming(raw, now)
        if self.session_anchor:
            raw = [c for c in raw if int(c["ts"]) >= self.session_anchor]
        if not raw:
            return
        bars = aggregate(raw, self.period, self.agg_mode, self.anchor_of)
        members = members_per_bar(raw, bars, self.period, self.agg_mode, self.anchor_of)
        span = self.period * 60
        last = self._last_ts.get(leg, 0)
        for i, b in enumerate(bars):
            if b["ts"] <= last:
                continue
            closed_at = b["ts"] + span
            if not (members[i] >= self.period or now >= closed_at + self.grace):
                break               # bars must be emitted strictly in order
            lag = max(0.0, now - closed_at)
            b["lag"] = lag
            self.latencies.append(lag)
            self.last_bar_at = now
            self._last_ts[leg] = b["ts"]
            self.on_bar(leg, b)

    def health(self) -> dict:
        lat = self.latencies[-50:]
        return {"polls": self.polls, "errors": self.errors, "abandoned": self.abandoned,
                "avg_lag": (sum(lat) / len(lat)) if lat else None,
                "max_lag": max(lat) if lat else None}


class BarSync:
    """
    Release one Bundle per 5-minute timestamp once every leg has either
    delivered that bar, moved past it, or been polled well after it closed
    without producing it (an illiquid option minute). Always in order.
    """

    def __init__(self, legs: List[str], on_bundle: Callable, period: int = 5,
                 grace: float = 20.0, clock: Callable[[], float] = time.time):
        self.legs = list(legs)
        self.on_bundle = on_bundle
        self.span = period * 60
        self.grace = grace
        self.clock = clock
        self._bars: Dict[int, Dict[str, dict]] = {}
        self._last_emitted_leg: Dict[str, int] = {l: 0 for l in legs}
        self._last_poll: Dict[str, float] = {l: 0.0 for l in legs}
        self._released = 0
        self._lock = threading.RLock()

    def add(self, leg: str, bar: dict):
        with self._lock:
            if bar["ts"] <= self._released:
                return
            self._bars.setdefault(bar["ts"], {})[leg] = bar
            self._last_emitted_leg[leg] = max(self._last_emitted_leg[leg], bar["ts"])
            self.pump()

    def polled(self, leg: str, at: float):
        with self._lock:
            self._last_poll[leg] = at
            self.pump()

    def _leg_done(self, leg: str, ts: int, have: Dict[str, dict]) -> bool:
        if leg in have:
            return True
        if self._last_emitted_leg[leg] > ts:
            return True
        return self._last_poll[leg] >= ts + self.span + self.grace

    def pump(self):
        with self._lock:
            while self._bars:
                ts = min(self._bars)
                have = self._bars[ts]
                if not all(self._leg_done(l, ts, have) for l in self.legs):
                    return
                del self._bars[ts]
                self._released = ts
                if "SPOT" not in have:
                    log.warning(f"  bar {ts}: no spot candle — skipped")
                    continue
                lag = max(b.get("lag", 0.0) for b in have.values())
                lag = max(lag, self.clock() - (ts + self.span))
                try:
                    self.on_bundle(ts, have.get("SPOT"), have.get("CE"), have.get("PE"), lag)
                except Exception as e:
                    log.exception(f"  bundle handler error: {e}")
