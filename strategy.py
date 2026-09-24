#!/usr/bin/env python3
"""
strategy.py — the five conditions, as pure state machines
═════════════════════════════════════════════════════════
No network, no clock, no broker. Feed it synchronised 5-minute bars for
spot, CE and PE (one Bundle per candle, in order) and it tells you when a
condition fires. The engine owns everything live; the tests drive this file
directly.

Ranges
  R1  = 09:15 candle (first)   on spot, CE, PE
  R2  = 09:20 candle (second)  on spot, CE, PE   — Manipulation only

Breaks
  option "breaks high/low"  : wick (bar high > range high / bar low < range low)
  spot "breaks high/low"    : wick
  spot "stays inside"       : checked at candle CLOSE
  spot "closes beyond"      : close

Common to every condition
  entry   SELL the option at the close of the reversal candle
  SL      sold option premium: max(high of reversal candle, high of the candle
          before it) + sl_buffer
  target  a SPOT level, fixed at the moment of entry
  one trade per day — the first condition to fire locks all the others

Condition 1  (R1) spot closes inside; both options break, opposite sides
  PE broke high + CE broke low  -> spot close < spot low -> bullish reversal
                                   (spot) -> SELL PE, target spot day high
  CE broke high + PE broke low  -> spot close > spot high -> bearish reversal
                                   (spot) -> SELL CE, target spot day low

Condition 2  (R1) decided on the first bar where spot breaks a side
  spot breaks high, CE broke high, PE untouched
     new high = high of that spot bar -> spot close > new high
     -> bearish reversal (spot) -> SELL CE, target first-candle open
  mirror: spot breaks low, PE broke high, CE untouched -> SELL PE

Condition 3  (R1) decided on the first bar where spot breaks a side
  spot breaks high, CE and PE both untouched
     -> CE breaks its high -> bearish reversal (CE) -> SELL CE, target day low
  mirror: spot breaks low -> PE breaks its high -> bearish reversal (PE)
     -> SELL PE, target day high

Condition 5  (R1) spot closes inside; one option breaks high, other untouched
  CE broke high, PE untouched -> bearish reversal (CE), peak 1 = CE high of
     that swing -> CE breaks peak 1 -> second bearish reversal (CE)
     -> SELL CE, target day low
  mirror on PE -> SELL PE, target day high

Manipulation  (R2) Condition 1 on the second candle, with a close filter
  PE broke R2 high + CE broke R2 low -> spot close <= R2 spot low - manip_points
     -> bullish reversal (spot) -> SELL PE, target day high
  mirror -> spot close >= R2 spot high + manip_points -> SELL CE, target day low

Balfund Trading Pvt Ltd
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict

from patterns import bullish_reversal, bearish_reversal, PATTERNS

CONDITIONS = ("C1", "C2", "C3", "C5", "MANIP")
CONDITION_NAMES = {"C1": "Condition 1", "C2": "Condition 2", "C3": "Condition 3",
                   "C5": "Condition 5", "MANIP": "Manipulation"}


@dataclass
class Rng:
    open: float
    high: float
    low: float
    close: float

    @classmethod
    def of(cls, b: Optional[dict]) -> Optional["Rng"]:
        if not b:
            return None
        return cls(b["open"], b["high"], b["low"], b["close"])


@dataclass
class Bundle:
    ts: int
    spot: Optional[dict]
    ce: Optional[dict]
    pe: Optional[dict]
    lag: float = 0.0


@dataclass
class Signal:
    condition: str              # C1 | C2 | C3 | C5 | MANIP
    side: str                   # CE | PE  (the option to SELL)
    ts: int                     # reversal candle start epoch
    pattern: str
    target_kind: str            # day_high | day_low | first_open
    target: float               # spot level (from bars; engine may widen with ticks)
    sl: float                   # option premium level
    sl_ref_high: float
    option_close: float         # sold option's reversal-candle close
    spot_close: float
    detail: str = ""


@dataclass
class StrategyParams:
    sl_buffer: float = 7.0
    manip_points: float = 10.0
    allow_reversal_on_trigger_bar: bool = False
    enabled: Dict[str, bool] = field(default_factory=lambda: {c: True for c in CONDITIONS})
    patterns: Dict[str, bool] = field(default_factory=lambda: {p: True for p in PATTERNS})


# ═══════════════════════════════════════════════════════════════════════════
# CONTEXT — everything the machines read, updated once per bar
# ═══════════════════════════════════════════════════════════════════════════

class Ctx:
    def __init__(self):
        self.spot: List[dict] = []
        self.ce: List[Optional[dict]] = []
        self.pe: List[Optional[dict]] = []
        self.ts: List[int] = []
        self.r1: Dict[str, Optional[Rng]] = {}
        self.r2: Dict[str, Optional[Rng]] = {}
        self.i = -1

    # series helpers
    def series(self, leg: str) -> List[Optional[dict]]:
        return {"SPOT": self.spot, "CE": self.ce, "PE": self.pe}[leg]

    def upto(self, leg: str, i: Optional[int] = None) -> List[dict]:
        """Non-missing bars of a leg up to index i (inclusive), newest last."""
        i = self.i if i is None else i
        return [b for b in self.series(leg)[: i + 1] if b]

    def day_high(self) -> float:
        return max(b["high"] for b in self.spot)

    def day_low(self) -> float:
        return min(b["low"] for b in self.spot)


class BreakTracker:
    """Cumulative wick breaks of one range set, from the bar after the range."""

    def __init__(self, rng: Dict[str, Rng]):
        self.r = rng
        self.hi = {"CE": False, "PE": False, "SPOT": False}
        self.lo = {"CE": False, "PE": False, "SPOT": False}
        self.spot_closed_outside = False       # before the current bar
        self.first_spot_hi_idx: Optional[int] = None
        self.first_spot_lo_idx: Optional[int] = None

    def update(self, i: int, spot: dict, ce: Optional[dict], pe: Optional[dict]):
        for leg, b in (("CE", ce), ("PE", pe), ("SPOT", spot)):
            if not b or not self.r.get(leg):
                continue
            if b["high"] > self.r[leg].high:
                if leg == "SPOT" and not self.hi["SPOT"]:
                    self.first_spot_hi_idx = i
                self.hi[leg] = True
            if b["low"] < self.r[leg].low:
                if leg == "SPOT" and not self.lo["SPOT"]:
                    self.first_spot_lo_idx = i
                self.lo[leg] = True

    def untouched(self, leg: str) -> bool:
        return not self.hi[leg] and not self.lo[leg]

    def spot_close_inside(self, spot: dict) -> bool:
        r = self.r["SPOT"]
        return r.low <= spot["close"] <= r.high


def _other(side: str) -> str:
    return "PE" if side == "CE" else "CE"


# ═══════════════════════════════════════════════════════════════════════════
# MACHINES
# ═══════════════════════════════════════════════════════════════════════════

class Machine:
    """One condition, one side. Subclasses implement step()."""

    def __init__(self, cond: str, side: str, strat: "Strategy"):
        self.cond = cond
        self.side = side                  # option that would be SOLD
        self.s = strat
        self.state = "WAIT_SETUP"
        self.note = ""
        self.trigger_idx: Optional[int] = None

    @property
    def key(self) -> str:
        return f"{self.cond}-{self.side}"

    def dead(self, why: str):
        self.state = "DEAD"
        self.note = why

    def _rev_allowed(self, i: int) -> bool:
        if self.trigger_idx is None:
            return False
        if self.s.params.allow_reversal_on_trigger_bar:
            return i >= self.trigger_idx
        return i > self.trigger_idx

    def _rev(self, chart: str, bullish: bool) -> Optional[str]:
        ctx = self.s.ctx
        if not ctx.series(chart)[ctx.i]:
            return None
        bars = ctx.upto(chart)[-3:]
        fn = bullish_reversal if bullish else bearish_reversal
        return fn(bars, self.s.params.patterns)

    def _fire(self, pattern: str, target_kind: str, detail: str) -> Optional[Signal]:
        ctx = self.s.ctx
        i = ctx.i
        opt = ctx.series(self.side)
        cur = opt[i]
        if not cur:
            return None
        prev = next((opt[j] for j in range(i - 1, -1, -1) if opt[j]), None)
        ref = max(cur["high"], prev["high"]) if prev else cur["high"]
        if target_kind == "day_high":
            tgt = ctx.day_high()
        elif target_kind == "day_low":
            tgt = ctx.day_low()
        else:
            tgt = ctx.r1["SPOT"].open
        return Signal(condition=self.cond, side=self.side, ts=ctx.ts[i],
                      pattern=pattern, target_kind=target_kind, target=tgt,
                      sl=round(ref + self.s.params.sl_buffer, 2), sl_ref_high=ref,
                      option_close=cur["close"], spot_close=ctx.spot[i]["close"],
                      detail=detail)

    def step(self, can_enter: bool) -> Optional[Signal]:
        raise NotImplementedError


class SplitMachine(Machine):
    """
    Condition 1 (R1) and Manipulation (R2).

    side PE: PE broke high + CE broke low while spot closes inside
             -> spot close below low (minus filter) -> bullish spot reversal
    side CE: mirror.
    """

    def __init__(self, cond, side, strat, which: str, filter_pts: float = 0.0):
        super().__init__(cond, side, strat)
        self.which = which                # "r1" | "r2"
        self.filter = filter_pts

    def step(self, can_enter):
        s, ctx = self.s, self.s.ctx
        bt = s.bt1 if self.which == "r1" else s.bt2
        if bt is None or self.state == "DEAD":
            return None
        i, spot = ctx.i, ctx.spot[ctx.i]
        r = bt.r["SPOT"]
        opp = _other(self.side)

        if self.state == "WAIT_SETUP":
            if bt.spot_closed_outside:
                self.dead("spot closed outside the range before both options split")
                return None
            if bt.hi[self.side] and bt.lo[opp]:
                if bt.spot_close_inside(spot):
                    self.state = "WAIT_TRIGGER"
                    self.note = (f"{self.side} broke high, {opp} broke low, spot inside "
                                 f"— waiting for spot close "
                                 f"{'below' if self.side == 'PE' else 'above'} "
                                 f"{(r.low - self.filter) if self.side == 'PE' else (r.high + self.filter):.2f}")
                else:
                    self.dead("options split on the same candle spot closed outside")
            return None

        if self.state == "WAIT_TRIGGER":
            if self.side == "PE":
                hit = spot["close"] <= r.low - self.filter if self.filter else spot["close"] < r.low
            else:
                hit = spot["close"] >= r.high + self.filter if self.filter else spot["close"] > r.high
            if hit:
                self.trigger_idx = i
                self.state = "WAIT_REVERSAL"
                self.note = (f"spot closed {spot['close']:.2f} beyond the range "
                             f"— waiting for {'bullish' if self.side == 'PE' else 'bearish'} "
                             f"reversal on spot")

        if self.state == "WAIT_REVERSAL" and self._rev_allowed(i) and can_enter:
            pat = self._rev("SPOT", bullish=(self.side == "PE"))
            if pat:
                return self._fire(pat, "day_high" if self.side == "PE" else "day_low",
                                  f"spot {pat}")
        return None


class BreakoutMachine(Machine):
    """
    Condition 2. Decided on the first bar spot breaks the relevant side.

    side CE: spot breaks high; CE broke high; PE untouched
             new high = that bar's high -> spot close > new high
             -> bearish spot reversal -> SELL CE, target first open
    side PE: mirror on the low.
    """

    def step(self, can_enter):
        s, ctx = self.s, self.s.ctx
        bt = s.bt1
        if bt is None or self.state == "DEAD":
            return None
        i, spot = ctx.i, ctx.spot[ctx.i]
        up = self.side == "CE"

        if self.state == "WAIT_SETUP":
            first = bt.first_spot_hi_idx if up else bt.first_spot_lo_idx
            if first != i:
                return None
            opt_broke = bt.hi[self.side]
            other_ok = bt.untouched(_other(self.side))
            if opt_broke and other_ok:
                self.level = spot["high"] if up else spot["low"]
                self.state = "WAIT_TRIGGER"
                self.note = (f"spot broke {'high' if up else 'low'} with {self.side}, "
                             f"{_other(self.side)} held — new {'high' if up else 'low'} "
                             f"{self.level:.2f}, waiting for a close beyond it")
            else:
                self.dead(f"at spot's first {'high' if up else 'low'} break: "
                          f"{self.side} broke={opt_broke}, "
                          f"{_other(self.side)} untouched={other_ok}")
            return None

        if self.state == "WAIT_TRIGGER":
            if (up and spot["close"] > self.level) or (not up and spot["close"] < self.level):
                self.trigger_idx = i
                self.state = "WAIT_REVERSAL"
                self.note = (f"spot closed {spot['close']:.2f} beyond {self.level:.2f} "
                             f"— waiting for {'bearish' if up else 'bullish'} reversal on spot")

        if self.state == "WAIT_REVERSAL" and self._rev_allowed(i) and can_enter:
            pat = self._rev("SPOT", bullish=not up)
            if pat:
                return self._fire(pat, "first_open", f"spot {pat}")
        return None


class LagMachine(Machine):
    """
    Condition 3. Decided on the first bar spot breaks the relevant side.

    side CE: spot breaks high, CE and PE both untouched
             -> CE breaks its high -> bearish CE reversal -> SELL CE, day low
    side PE: spot breaks low, both untouched
             -> PE breaks its high -> bearish PE reversal -> SELL PE, day high
    """

    def step(self, can_enter):
        s, ctx = self.s, self.s.ctx
        bt = s.bt1
        if bt is None or self.state == "DEAD":
            return None
        i = ctx.i
        up = self.side == "CE"

        if self.state == "WAIT_SETUP":
            first = bt.first_spot_hi_idx if up else bt.first_spot_lo_idx
            if first != i:
                return None
            # "At the same time": the tracker already includes this bar, so an
            # option that breaks on the same candle as spot counts as broken
            # (that is Condition 2's case, not this one).
            if bt.untouched("CE") and bt.untouched("PE"):
                self.state = "WAIT_TRIGGER"
                self.note = (f"spot broke {'high' if up else 'low'}, both options held "
                             f"— waiting for {self.side} to break its high "
                             f"{ctx.r1[self.side].high:.2f}")
            else:
                self.dead("an option had already broken when spot broke")
            return None

        if self.state == "WAIT_TRIGGER":
            b = ctx.series(self.side)[i]
            if b and b["high"] > ctx.r1[self.side].high:
                self.trigger_idx = i
                self.state = "WAIT_REVERSAL"
                self.note = f"{self.side} broke its high — waiting for bearish reversal on {self.side}"

        if self.state == "WAIT_REVERSAL" and self._rev_allowed(i) and can_enter:
            pat = self._rev(self.side, bullish=False)
            if pat:
                return self._fire(pat, "day_low" if up else "day_high",
                                  f"{self.side} {pat}")
        return None


class DoubleTopMachine(Machine):
    """
    Condition 5.

    side CE: spot closes inside; CE broke high; PE untouched
             -> bearish CE reversal #1, peak 1 = CE high of that swing
             -> CE breaks peak 1 -> bearish CE reversal #2 -> SELL CE, day low
    side PE: mirror on PE -> SELL PE, day high
    """

    def step(self, can_enter):
        s, ctx = self.s, self.s.ctx
        bt = s.bt1
        if bt is None or self.state == "DEAD":
            return None
        i = ctx.i
        opt = ctx.series(self.side)
        b = opt[i]
        opp = _other(self.side)

        if self.state == "WAIT_SETUP":
            if bt.spot_closed_outside:
                self.dead("spot closed outside the range first")
                return None
            if bt.hi[self.side]:
                if bt.untouched(opp) and bt.spot_close_inside(ctx.spot[i]):
                    self.trigger_idx = i
                    self.swing_start = i
                    self.state = "WAIT_REV1"
                    self.note = (f"{self.side} broke high, {opp} held, spot inside "
                                 f"— waiting for first bearish reversal on {self.side}")
                else:
                    self.dead(f"{self.side} broke high but {opp} was not untouched "
                              f"or spot closed outside")
                    return None
            else:
                return None

        if self.state == "WAIT_REV1":
            if self._rev_allowed(i):
                pat = self._rev(self.side, bullish=False)
                if pat:
                    seg = [opt[j] for j in range(self.swing_start, i + 1) if opt[j]]
                    self.peak1 = max(x["high"] for x in seg)
                    self.state = "WAIT_BREAK2"
                    self.note = (f"first reversal ({pat}) — peak 1 = {self.peak1:.2f}, "
                                 f"waiting for {self.side} to break it")
            return None

        if self.state == "WAIT_BREAK2":
            if b and b["high"] > self.peak1:
                self.trigger_idx = i
                self.state = "WAIT_REV2"
                self.note = f"{self.side} broke peak 1 — waiting for second bearish reversal"

        if self.state == "WAIT_REV2" and self._rev_allowed(i) and can_enter:
            pat = self._rev(self.side, bullish=False)
            if pat:
                return self._fire(pat, "day_low" if self.side == "CE" else "day_high",
                                  f"{self.side} second {pat} (peak 1 {self.peak1:.2f})")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY
# ═══════════════════════════════════════════════════════════════════════════

class Strategy:
    def __init__(self, params: Optional[StrategyParams] = None):
        self.params = params or StrategyParams()
        self.ctx = Ctx()
        self.bt1: Optional[BreakTracker] = None
        self.bt2: Optional[BreakTracker] = None
        self.locked = False
        self.fired: Optional[Signal] = None
        self.machines: List[Machine] = []
        for side in ("CE", "PE"):
            self.machines += [
                SplitMachine("C1", side, self, "r1"),
                BreakoutMachine("C2", side, self),
                LagMachine("C3", side, self),
                DoubleTopMachine("C5", side, self),
                SplitMachine("MANIP", side, self, "r2",
                             filter_pts=self.params.manip_points),
            ]
        for m in self.machines:
            if not self.params.enabled.get(m.cond, True):
                m.dead("disabled in settings")

    def lock(self, why: str = "trade taken"):
        self.locked = True
        for m in self.machines:
            if m.state not in ("DEAD", "FIRED", "LOCKED"):
                m._prelock = (m.state, m.note)
                m.state = "LOCKED"
                m.note = why

    def release(self, sig: Signal, why: str):
        """
        A signal fired but the engine declined to trade it (target already
        met, stop already through). That machine is finished for the day;
        every other machine resumes exactly where it was.
        """
        self.locked = False
        self.fired = None
        for m in self.machines:
            if m.cond == sig.condition and m.side == sig.side:
                m.dead(f"signal skipped — {why}")
            elif m.state == "LOCKED" and hasattr(m, "_prelock"):
                m.state, m.note = m._prelock

    def on_bundle(self, b: Bundle, can_enter: bool = True) -> Optional[Signal]:
        """Process one synchronised candle. Returns a Signal at most once per day."""
        ctx = self.ctx
        if not b.spot:
            return None
        ctx.spot.append(b.spot)
        ctx.ce.append(b.ce)
        ctx.pe.append(b.pe)
        ctx.ts.append(b.ts)
        ctx.i = len(ctx.spot) - 1
        i = ctx.i

        if i == 0:
            ctx.r1 = {"SPOT": Rng.of(b.spot), "CE": Rng.of(b.ce), "PE": Rng.of(b.pe)}
            if all(ctx.r1.values()):
                self.bt1 = BreakTracker(ctx.r1)
            return None

        # R1 trackers see every bar after the first candle
        if self.bt1:
            self.bt1.update(i, b.spot, b.ce, b.pe)

        if i == 1:
            ctx.r2 = {"SPOT": Rng.of(b.spot), "CE": Rng.of(b.ce), "PE": Rng.of(b.pe)}
            if all(ctx.r2.values()):
                self.bt2 = BreakTracker(ctx.r2)
        elif self.bt2:
            self.bt2.update(i, b.spot, b.ce, b.pe)

        sig = None
        if not self.locked:
            for m in self.machines:
                if m.state in ("DEAD", "LOCKED", "FIRED"):
                    continue
                if m.cond == "MANIP" and i < 2:
                    continue
                s = m.step(can_enter)
                if s and sig is None:
                    sig = s
                    m.state = "FIRED"
                    m.note = f"FIRED — sell {s.side} ({s.pattern})"

        # "stays inside, checked at candle close" — record AFTER the machines
        # have seen this bar, so a machine arming on this bar sees it inside.
        for bt in (self.bt1, self.bt2 if i >= 2 else None):
            if bt and not bt.spot_close_inside(b.spot):
                bt.spot_closed_outside = True

        if sig:
            self.fired = sig
            self.lock(f"locked — {CONDITION_NAMES[sig.condition]} {sig.side} fired")
        return sig

    def snapshot(self) -> dict:
        return {
            "r1": {k: vars(v) for k, v in self.ctx.r1.items() if v},
            "r2": {k: vars(v) for k, v in self.ctx.r2.items() if v},
            "bars": len(self.ctx.spot),
            "machines": [{"key": m.key, "cond": m.cond, "side": m.side,
                          "state": m.state, "note": m.note} for m in self.machines],
            "locked": self.locked,
        }
