#!/usr/bin/env python3
"""
engine.py — NIFTY First-Candle Condition Option Seller (live engine)
═══════════════════════════════════════════════════════════════════
Strategy code: BF-NFT-FCOS | Broker: Dhan v2 | Timeframe: 5-minute, fixed

Day flow
  before 09:15   wait, with a countdown
  09:20          09:15 candle closed -> ATM = round(close / 50) * 50
                 CE = ATM - 200, PE = ATM + 200 (ITM), fixed for the day
                 expiry = current weekly; on expiry day, the next weekly
  from 09:20     REST: 5-min candles for spot, CE, PE -> five condition
                 machines run in parallel (see strategy.py)
                 WebSocket: LTP for spot, CE, PE -> SL, target, fills
  entry          SELL at the close of the reversal candle (09:25 - 14:30)
  exit           SL on sold option LTP | target on spot LTP | 15:15 square-off
  one trade per day

Balfund Trading Pvt Ltd | www.balfund.com
"""

import csv, json, time, threading, logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Dict, Callable, List

import websocket

from dhan import (IST, NIFTY, CREDS, BASE_DIR, LOG_DIR, now_ist, hhmm, anchor_of,
                  session_anchor_epoch, epoch_to_ist, init_credentials,
                  fetch_intraday_1m, fetch_expiry_list, pick_expiry,
                  fetch_option_chain, chain_security_id, fetch_lot_size,
                  place_order_limit_ioc, get_positions, parse_header, parse_ticker,
                  REQ_SUB_TICKER, RESP_TICKER, RESP_DISCONNECT, WS_NEEDS_RECONNECT)
from candles import aggregate, members_per_bar
from patterns import PATTERNS
from restfeed import RestCandleFeed, BarSync
from strategy import (Strategy, StrategyParams, Bundle, Signal, CONDITIONS,
                      CONDITION_NAMES)

VERSION = "1.0.0"
PERIOD = 5                              # fixed by the strategy
STATE_FILE = BASE_DIR / "fcos_daily_state.json"
CONFIG_FILE = BASE_DIR / "fcos_config.json"

_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
_activity_file = LOG_DIR / f"fcos_activity_{_ts}.log"
_trade_csv = LOG_DIR / f"fcos_trades_{datetime.now().strftime('%Y%m%d')}.csv"
_candle_csv = LOG_DIR / f"fcos_candles_{datetime.now().strftime('%Y%m%d')}.csv"

log = logging.getLogger("FCOS")
if not log.handlers:
    log.setLevel(logging.INFO)
    _fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
    _fh = logging.FileHandler(str(_activity_file), encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    log.addHandler(_sh)
    log.propagate = False


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class EngineConfig:
    mode: str = "paper"                 # paper | live
    lots: int = 1
    lot_size_override: int = 0          # 0 = read from scrip master
    itm_offset: int = 200
    sl_buffer: float = 7.0
    manip_points: float = 10.0
    entry_start: str = "09:25"          # earliest reversal-candle CLOSE
    entry_end: str = "14:30"            # latest reversal-candle CLOSE
    square_off: str = "15:15"
    max_signal_age: float = 60.0        # seconds after candle close
    allow_reversal_on_trigger_bar: bool = False
    conditions: Dict[str, bool] = field(default_factory=lambda: {c: True for c in CONDITIONS})
    patterns: Dict[str, bool] = field(default_factory=lambda: {p: True for p in PATTERNS})
    agg_mode: str = "clock"
    poll_seconds: float = 3.0
    ws_silence_seconds: float = 90.0

    def params(self) -> StrategyParams:
        return StrategyParams(sl_buffer=self.sl_buffer, manip_points=self.manip_points,
                              allow_reversal_on_trigger_bar=self.allow_reversal_on_trigger_bar,
                              enabled=dict(self.conditions), patterns=dict(self.patterns))

    def save(self, path=CONFIG_FILE):
        try:
            path.write_text(json.dumps(asdict(self), indent=2))
        except Exception as e:
            log.warning(f"Could not save config: {e}")

    @classmethod
    def load(cls, path=CONFIG_FILE) -> "EngineConfig":
        cfg = cls()
        try:
            if path.exists():
                d = json.loads(path.read_text())
                for k, v in d.items():
                    if hasattr(cfg, k):
                        if isinstance(getattr(cfg, k), dict) and isinstance(v, dict):
                            getattr(cfg, k).update(v)
                        else:
                            setattr(cfg, k, v)
        except Exception as e:
            log.warning(f"Could not load config, using defaults: {e}")
        return cfg


# ═══════════════════════════════════════════════════════════════════════════
# LOGS
# ═══════════════════════════════════════════════════════════════════════════

class CsvLog:
    def __init__(self, path, headers):
        self.path, self.headers = path, headers
        self._lock = threading.Lock()
        if not path.exists():
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(headers)

    def row(self, **kw):
        with self._lock:
            with open(self.path, "a", newline="") as f:
                csv.writer(f).writerow([kw.get(h, "") for h in self.headers])


TRADES = CsvLog(_trade_csv, ["date", "mode", "condition", "side", "strike", "expiry",
                             "security_id", "qty", "entry_time", "entry", "sl",
                             "target_kind", "target", "pattern", "exit_time", "exit",
                             "reason", "pnl_points", "pnl_value"])
CANDLES = CsvLog(_candle_csv, ["time", "lag", "spot_o", "spot_h", "spot_l", "spot_c",
                               "ce_o", "ce_h", "ce_l", "ce_c",
                               "pe_o", "pe_h", "pe_l", "pe_c"])


# ═══════════════════════════════════════════════════════════════════════════
# ENGINE
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    condition: str
    side: str
    security_id: str
    strike: float
    qty: int
    entry: float
    sl: float
    target: float
    target_kind: str
    pattern: str
    entry_time: str
    order_id: str = ""
    status: str = "OPEN"                # OPEN | EXITING | EXIT_FAILED | CLOSED
    exit: float = 0.0
    exit_time: str = ""
    reason: str = ""


class Engine:
    def __init__(self, config: Optional[EngineConfig] = None,
                 gui_callback: Optional[Callable] = None):
        self.cfg = config or EngineConfig()
        self.cb = gui_callback
        self.strategy = Strategy(self.cfg.params())
        self.stop_event = threading.Event()
        self._lock = threading.RLock()

        self.lot_size = 0
        self.expiry = ""
        self.atm = 0
        self.strikes: Dict[str, float] = {}
        self.sec: Dict[str, str] = {}            # CE/PE -> security id
        self.by_sec: Dict[str, str] = {}         # security id -> SPOT/CE/PE
        self.ltp: Dict[str, float] = {"SPOT": 0.0, "CE": 0.0, "PE": 0.0}
        self.tick_high = 0.0
        self.tick_low = 0.0

        self.position: Optional[Position] = None
        self.closed: List[Position] = []
        self.traded_today = False
        self.halted = False
        self.status = "IDLE"

        self.feed: Optional[RestCandleFeed] = None
        self.sync: Optional[BarSync] = None
        self.ws = None
        self.ws_connected = threading.Event()
        self.last_tick_at = 0.0
        self.packets = 0
        self._last_loop = time.time()
        self._exit_retry_at = 0.0

    # ─── plumbing ───

    def _emit(self, event: str, data=None):
        if self.cb:
            try:
                self.cb(event, data or {})
            except Exception:
                pass

    def _set_status(self, s: str):
        self.status = s
        self._emit("status", {"status": s})

    # ─── daily state (survives a restart) ───

    def _save_state(self):
        d = {"date": now_ist().strftime("%Y-%m-%d"), "expiry": self.expiry,
             "atm": self.atm, "strikes": self.strikes, "sec": self.sec,
             "traded_today": self.traded_today,
             "position": asdict(self.position) if self.position else None,
             "closed": [asdict(p) for p in self.closed]}
        try:
            STATE_FILE.write_text(json.dumps(d, indent=2))
        except Exception as e:
            log.warning(f"Could not save state: {e}")

    def _load_state(self) -> dict:
        try:
            if STATE_FILE.exists():
                d = json.loads(STATE_FILE.read_text())
                if d.get("date") == now_ist().strftime("%Y-%m-%d"):
                    return d
        except Exception as e:
            log.warning(f"Could not read state: {e}")
        return {}

    # ─── start-up ───

    def initialize(self) -> bool:
        log.info("=" * 70)
        log.info(f"NIFTY First-Candle Condition Option Seller v{VERSION} — "
                 f"{self.cfg.mode.upper()} mode")
        log.info("=" * 70)
        self._set_status("CONNECTING")
        try:
            init_credentials(lambda m: self._emit("status", {"status": m}))
        except Exception as e:
            log.error(f"Authentication failed: {e}")
            self._set_status("AUTH FAILED")
            return False

        if self.cfg.lot_size_override > 0:
            self.lot_size = int(self.cfg.lot_size_override)
            log.info(f"  Lot size (manual override) = {self.lot_size}")
        else:
            self.lot_size = fetch_lot_size("NIFTY") or 0
            if not self.lot_size:
                log.error("  Could not read NIFTY lot size. Set a manual lot size in "
                          "Settings and restart.")
                self._set_status("NO LOT SIZE")
                return False

        st = self._load_state()
        if st:
            self.traded_today = bool(st.get("traded_today"))
            self.closed = [Position(**p) for p in st.get("closed", [])]
            if st.get("position") and st["position"].get("status") != "CLOSED":
                self.position = Position(**st["position"])
                log.warning(f"  RESUMING open position from earlier today: "
                            f"SHORT {self.position.side} {self.position.strike:.0f} "
                            f"x{self.position.qty} @ {self.position.entry:.2f}  "
                            f"SL {self.position.sl:.2f}  target {self.position.target:.2f}")
            if self.traded_today:
                log.info("  A trade was already taken today — no new entries.")
        return True

    def _wait_for_first_candle(self) -> Optional[float]:
        """
        Block until the 09:15 5-minute spot candle is closed, then return its
        close. Never guesses from a pre-open quote — a wrong strike is a
        different instrument, not a degraded trade.
        """
        anchor = session_anchor_epoch()
        last_note = 0.0
        while not self.stop_event.is_set():
            now = time.time()
            t = now_ist().time()
            if t >= hhmm("15:30"):
                log.error("  Market is closed for the day — nothing to do.")
                return None
            if now < anchor + 300 + 2:
                if now - last_note > 120:
                    wait = anchor + 300 - now
                    what = "market open" if now < anchor else "09:15 candle close"
                    log.info(f"  Waiting for the {what} — "
                             f"{int(wait // 60)}m {int(wait % 60):02d}s "
                             f"(strike is read from the 09:15 candle close)")
                    self._set_status(f"WAITING ({int(wait // 60)}m)")
                    last_note = now
                self.stop_event.wait(1.0)
                continue
            raw = fetch_intraday_1m(NIFTY["security_id"], "IDX_I", "INDEX", days=1)
            raw = RestCandleFeed.drop_forming(raw, now)
            raw = [c for c in raw if anchor <= int(c["ts"]) < anchor + 300]
            if raw:
                bars = aggregate(raw, PERIOD, "clock", anchor_of)
                n = members_per_bar(raw, bars, PERIOD, "clock", anchor_of)
                if bars and bars[0]["ts"] == anchor and (n[0] >= PERIOD or now > anchor + 330):
                    c = bars[0]["close"]
                    log.info(f"  NIFTY 09:15 candle: O {bars[0]['open']:.2f} "
                             f"H {bars[0]['high']:.2f} L {bars[0]['low']:.2f} "
                             f"C {c:.2f}  (resolved {now - anchor - 300:.0f}s after close)")
                    return c
            if now - last_note > 30:
                log.info("  09:15 candle not available from REST yet — retrying")
                last_note = now
            self.stop_event.wait(3.0)
        return None

    def _resolve_strikes(self, close_0915: float) -> bool:
        gap = NIFTY["strike_gap"]
        self.atm = int(round(close_0915 / gap) * gap)
        self.strikes = {"CE": self.atm - self.cfg.itm_offset,
                        "PE": self.atm + self.cfg.itm_offset}
        exps = fetch_expiry_list(NIFTY)
        self.expiry = pick_expiry(exps, now_ist().date()) or ""
        if not self.expiry:
            log.error(f"  No usable expiry in {exps}")
            return False
        nearest = sorted(exps)[0] if exps else ""
        if nearest == now_ist().strftime("%Y-%m-%d"):
            log.info(f"  Today is expiry day ({nearest}) — using next weekly {self.expiry}")
        oc = fetch_option_chain(self.expiry, NIFTY)
        if not oc:
            return False
        for side in ("CE", "PE"):
            sid = chain_security_id(oc["oc"], self.strikes[side], side)
            if not sid:
                log.error(f"  {self.strikes[side]:.0f} {side} not in the {self.expiry} chain")
                return False
            self.sec[side] = sid
        self.by_sec = {NIFTY["security_id"]: "SPOT", self.sec["CE"]: "CE",
                       self.sec["PE"]: "PE"}
        log.info(f"  ATM {self.atm}  |  CE {self.strikes['CE']:.0f} (id {self.sec['CE']})"
                 f"  |  PE {self.strikes['PE']:.0f} (id {self.sec['PE']})"
                 f"  |  expiry {self.expiry}  |  lot {self.lot_size} x {self.cfg.lots}")
        self._emit("setup", {"atm": self.atm, "strikes": self.strikes, "sec": self.sec,
                             "expiry": self.expiry, "lot_size": self.lot_size,
                             "lots": self.cfg.lots, "mode": self.cfg.mode})
        self._save_state()
        return True

    # ─── candles ───

    def _fetch(self, sec, seg, inst):
        return fetch_intraday_1m(sec, seg, inst, days=1)

    def _entry_window(self, ts: int) -> bool:
        close_t = datetime.fromtimestamp(ts + PERIOD * 60, tz=IST).time()
        return hhmm(self.cfg.entry_start) <= close_t <= hhmm(self.cfg.entry_end)

    def _on_bundle(self, ts, spot, ce, pe, lag):
        with self._lock:
            fresh = lag <= self.cfg.max_signal_age
            can_enter = (fresh and self._entry_window(ts) and not self.traded_today
                         and not self.halted and self.position is None)
            hm = epoch_to_ist(ts)
            CANDLES.row(time=hm, lag=f"{lag:.1f}",
                        **{f"{k}_{f[0]}": (b or {}).get(f, "")
                           for k, b in (("spot", spot), ("ce", ce), ("pe", pe))
                           for f in ("open", "high", "low", "close")})
            waiting = [m.key for m in self.strategy.machines
                       if m.state in ("WAIT_REVERSAL", "WAIT_REV2")]
            if not fresh and waiting and self._entry_window(ts):
                log.info(f"  {hm} candle arrived {lag:.0f}s late — state updated, "
                         f"entries not allowed on it")
            sig = self.strategy.on_bundle(Bundle(ts, spot, ce, pe, lag), can_enter)
            self._emit("candle", {"time": hm, "lag": lag, "spot": spot, "ce": ce, "pe": pe})
            self._emit("strategy", self.strategy.snapshot())
            if len(self.strategy.ctx.spot) == 1:
                log.info(f"  R1 (09:15)  spot {spot['high']:.2f}/{spot['low']:.2f}  "
                         f"CE {self._hl(ce)}  PE {self._hl(pe)}")
            elif len(self.strategy.ctx.spot) == 2:
                log.info(f"  R2 (09:20)  spot {spot['high']:.2f}/{spot['low']:.2f}  "
                         f"CE {self._hl(ce)}  PE {self._hl(pe)}")
            self._log_transitions()
            if sig:
                self._enter(sig)

    @staticmethod
    def _hl(b):
        return f"{b['high']:.2f}/{b['low']:.2f}" if b else "n/a"

    def _log_transitions(self):
        if not hasattr(self, "_seen"):
            self._seen = {}
        for m in self.strategy.machines:
            k = (m.state, m.note)
            if self._seen.get(m.key) != k:
                self._seen[m.key] = k
                if m.state not in ("WAIT_SETUP", "LOCKED") and m.note:
                    log.info(f"  [{CONDITION_NAMES[m.cond]} {m.side}] {m.state}: {m.note}")

    # ─── entry / exit ───

    def _enter(self, sig: Signal):
        side = sig.side
        spot = self.ltp["SPOT"] or sig.spot_close
        opt_ltp = self.ltp[side] or sig.option_close
        target = sig.target
        if sig.target_kind == "day_high" and self.tick_high:
            target = max(target, self.tick_high)
        if sig.target_kind == "day_low" and self.tick_low:
            target = min(target, self.tick_low)

        name = CONDITION_NAMES[sig.condition]
        log.info(f"  SIGNAL {name}: SELL {side} {self.strikes[side]:.0f} — {sig.detail} | "
                 f"SL {sig.sl:.2f} (ref high {sig.sl_ref_high:.2f} + {self.cfg.sl_buffer}) | "
                 f"target {sig.target_kind} {target:.2f}")

        why = ""
        if side == "CE" and spot <= target:
            why = f"spot {spot:.2f} already at/below target {target:.2f}"
        elif side == "PE" and spot >= target:
            why = f"spot {spot:.2f} already at/above target {target:.2f}"
        elif opt_ltp >= sig.sl:
            why = f"{side} LTP {opt_ltp:.2f} already at/above SL {sig.sl:.2f}"
        if why:
            log.warning(f"  Signal NOT traded — {why}. Other conditions stay live.")
            self.strategy.release(sig, why)
            self._emit("strategy", self.strategy.snapshot())
            return

        qty = self.lot_size * max(1, int(self.cfg.lots))
        sec = self.sec[side]
        if self.cfg.mode == "live":
            r = place_order_limit_ioc(sec, NIFTY["segment"], "SELL", qty, opt_ltp)
            if not r.get("filled"):
                log.error("  ENTRY ORDER NOT FILLED — halting for the day. "
                          "Check the broker terminal for any partial position.")
                self.halted = True
                self.traded_today = True
                self._save_state()
                self._set_status("ENTRY FAILED")
                return
            fill, oid = float(r["price"]), r.get("order_id", "")
        else:
            fill, oid = opt_ltp, "PAPER"

        self.position = Position(condition=sig.condition, side=side, security_id=sec,
                                 strike=self.strikes[side], qty=qty, entry=fill,
                                 sl=sig.sl, target=round(target, 2),
                                 target_kind=sig.target_kind, pattern=sig.pattern,
                                 entry_time=now_ist().strftime("%H:%M:%S"), order_id=oid)
        self.traded_today = True
        self._save_state()
        log.info(f"  ENTERED SHORT {side} {self.strikes[side]:.0f} x{qty} @ {fill:.2f} "
                 f"[{self.cfg.mode}]  SL {sig.sl:.2f}  target spot {target:.2f}")
        self._set_status("IN TRADE")
        self._emit("position", asdict(self.position))

    def _check_exit_on_tick(self, leg: str, ltp: float):
        p = self.position
        if not p or p.status != "OPEN":
            return
        if leg == p.side and ltp >= p.sl:
            self._exit(f"SL ({p.side} {ltp:.2f} >= {p.sl:.2f})", "SL")
        elif leg == "SPOT":
            if (p.side == "CE" and ltp <= p.target) or (p.side == "PE" and ltp >= p.target):
                self._exit(f"TARGET (spot {ltp:.2f} reached {p.target:.2f})", "TARGET")

    def _exit(self, why: str, reason: str):
        with self._lock:
            p = self.position
            if not p or p.status not in ("OPEN", "EXIT_FAILED"):
                return
            p.status = "EXITING"
        threading.Thread(target=self._do_exit, args=(why, reason), daemon=True).start()

    def _do_exit(self, why: str, reason: str):
        p = self.position
        log.info(f"  EXIT — {why}")
        ltp = self.ltp[p.side] or p.entry
        if self.cfg.mode == "live":
            r = place_order_limit_ioc(p.security_id, NIFTY["segment"], "BUY", p.qty, ltp)
            if not r.get("filled"):
                with self._lock:
                    p.status = "EXIT_FAILED"
                    self._exit_retry_at = time.time() + 5
                log.error("  EXIT ORDER NOT FILLED — retrying in 5s. Watch the terminal.")
                self._set_status("EXIT FAILED — RETRYING")
                self._save_state()
                return
            fill = float(r["price"])
        else:
            fill = ltp
        with self._lock:
            p.exit, p.reason, p.status = fill, reason, "CLOSED"
            p.exit_time = now_ist().strftime("%H:%M:%S")
            pts = p.entry - p.exit
            TRADES.row(date=now_ist().strftime("%Y-%m-%d"), mode=self.cfg.mode,
                       condition=CONDITION_NAMES[p.condition], side=p.side,
                       strike=f"{p.strike:.0f}", expiry=self.expiry,
                       security_id=p.security_id, qty=p.qty, entry_time=p.entry_time,
                       entry=f"{p.entry:.2f}", sl=f"{p.sl:.2f}", target_kind=p.target_kind,
                       target=f"{p.target:.2f}", pattern=p.pattern, exit_time=p.exit_time,
                       exit=f"{p.exit:.2f}", reason=reason, pnl_points=f"{pts:.2f}",
                       pnl_value=f"{pts * p.qty:.2f}")
            self.closed.append(p)
            self.position = None
            self._save_state()
        log.info(f"  CLOSED {p.side} @ {fill:.2f}  {reason}  P&L {pts:+.2f} pts "
                 f"= {pts * p.qty:+,.2f}")
        self._emit("trade_closed", asdict(p))
        self._set_status("DONE FOR THE DAY")

    def manual_square_off(self):
        if self.position and self.position.status in ("OPEN", "EXIT_FAILED"):
            self._exit("manual square-off", "MANUAL")

    # ─── websocket ───

    def _subscribe(self, ws):
        lst = [{"ExchangeSegment": "IDX_I", "SecurityId": NIFTY["security_id"]}]
        lst += [{"ExchangeSegment": NIFTY["segment"], "SecurityId": s}
                for s in self.sec.values()]
        ws.send(json.dumps({"RequestCode": REQ_SUB_TICKER,
                            "InstrumentCount": len(lst), "InstrumentList": lst}))
        log.info(f"  WebSocket subscribed (ticker/LTP) — spot + {len(self.sec)} options")
        self._emit("ws", {"connected": True})

    def _on_ws_open(self, ws):
        self.ws_connected.set()
        try:
            self._subscribe(ws)
        except Exception as e:
            log.error(f"  Subscribe error: {e}")

    def _on_ws_message(self, ws, msg):
        if isinstance(msg, str):
            return
        h = parse_header(bytes(msg))
        if not h:
            return
        if h["resp_code"] == RESP_DISCONNECT:
            log.warning("  WebSocket: server sent disconnect packet")
            return
        if h["resp_code"] != RESP_TICKER:
            return
        t = parse_ticker(h["payload"])
        leg = self.by_sec.get(h["security_id"])
        if not t or not leg or t["ltp"] <= 0:
            return
        self.packets += 1
        self.last_tick_at = time.time()
        ltp = t["ltp"]
        self.ltp[leg] = ltp
        if leg == "SPOT":
            tm = now_ist().time()
            if hhmm("09:15") <= tm <= hhmm("15:30"):
                self.tick_high = max(self.tick_high, ltp) if self.tick_high else ltp
                self.tick_low = min(self.tick_low, ltp) if self.tick_low else ltp
        self._check_exit_on_tick(leg, ltp)

    def _on_ws_error(self, ws, error):
        log.error(f"  WS error: {error}")

    def _on_ws_close(self, ws, code, msg):
        self.ws_connected.clear()
        log.warning(f"  WS closed: {code} {msg}")
        self._emit("ws", {"connected": False})

    def _run_ws(self):
        while not self.stop_event.is_set():
            try:
                self.ws = websocket.WebSocketApp(
                    CREDS.ws_url, on_open=self._on_ws_open, on_message=self._on_ws_message,
                    on_error=self._on_ws_error, on_close=self._on_ws_close)
                self.ws.run_forever(ping_interval=30, ping_timeout=15)
            except Exception as e:
                log.error(f"  WS exception: {e}")
            if not self.stop_event.is_set():
                log.info("  WS reconnecting in 2s...")
                time.sleep(2)

    def _ws_watchdog(self):
        while not self.stop_event.is_set():
            self.stop_event.wait(10.0)
            if self.stop_event.is_set():
                break
            if WS_NEEDS_RECONNECT.is_set():
                WS_NEEDS_RECONNECT.clear()
                log.info("  Reconnecting the websocket with the new token")
                self.last_tick_at = time.time()
                self._close_ws()
                continue
            if not (hhmm("09:15") <= now_ist().time() <= hhmm("15:30")):
                continue
            if self.last_tick_at and time.time() - self.last_tick_at > self.cfg.ws_silence_seconds:
                log.warning(f"  No websocket tick for {time.time() - self.last_tick_at:.0f}s "
                            f"in market hours — forcing a reconnect")
                self.last_tick_at = time.time()
                self._close_ws()

    def _close_ws(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    # ─── main loop ───

    def _monitor(self):
        sq = hhmm(self.cfg.square_off)
        while not self.stop_event.is_set():
            now = time.time()
            gap = now - self._last_loop
            if gap > 30:
                log.warning(f"  PROCESS WAS NOT RUNNING for {gap:.0f}s (sleep / power / "
                            f"freeze). Backlogged candles will update state but cannot "
                            f"trigger an entry.")
            self._last_loop = now
            if self.sync:
                self.sync.pump()
            p = self.position
            if p:
                if now_ist().time() >= sq and p.status == "OPEN":
                    self._exit(f"square-off {self.cfg.square_off}", "SQUARE_OFF")
                elif p.status == "EXIT_FAILED" and now >= self._exit_retry_at:
                    self._exit("retrying failed exit", p.reason or "RETRY")
            self._emit("tick", self.summary())
            self.stop_event.wait(1.0)

    def summary(self) -> dict:
        p = self.position
        upnl = 0.0
        if p and self.ltp.get(p.side):
            upnl = (p.entry - self.ltp[p.side]) * p.qty
        realised = sum((c.entry - c.exit) * c.qty for c in self.closed)
        h = self.feed.health() if self.feed else {}
        return {"status": self.status, "mode": self.cfg.mode, "ltp": dict(self.ltp),
                "day_high": self.tick_high, "day_low": self.tick_low,
                "position": asdict(p) if p else None, "upnl": upnl, "realised": realised,
                "ws": self.ws_connected.is_set(), "packets": self.packets,
                "avg_lag": h.get("avg_lag"), "max_lag": h.get("max_lag"),
                "traded_today": self.traded_today}

    def run(self):
        if not self.initialize():
            return
        c = self._wait_for_first_candle()
        if c is None or self.stop_event.is_set():
            self._set_status("STOPPED")
            return
        if not self._resolve_strikes(c):
            self._set_status("SETUP FAILED")
            return

        if self.traded_today:
            self.strategy.lock("a trade was already taken today")

        if self.position and self.cfg.mode == "live":
            held = [x for x in get_positions()
                    if str(x.get("securityId")) == self.position.security_id]
            if not held:
                log.warning("  Resumed position is NOT open at the broker — dropping it "
                            "from state. Verify in the terminal.")
                self.position = None
                self._save_state()

        threading.Thread(target=self._run_ws, daemon=True, name="ws").start()
        threading.Thread(target=self._ws_watchdog, daemon=True, name="ws-watchdog").start()

        self.sync = BarSync(["SPOT", "CE", "PE"], self._on_bundle, period=PERIOD)
        legs = {"SPOT": (NIFTY["security_id"], "IDX_I", "INDEX"),
                "CE": (self.sec["CE"], NIFTY["segment"], "OPTIDX"),
                "PE": (self.sec["PE"], NIFTY["segment"], "OPTIDX")}
        self.feed = RestCandleFeed(legs, self._fetch, anchor_of, self.sync.add,
                                   period=PERIOD, poll=self.cfg.poll_seconds,
                                   agg_mode=self.cfg.agg_mode,
                                   session_anchor=session_anchor_epoch(),
                                   on_poll=self.sync.polled,
                                   on_blind=lambda q: self._set_status("NO DATA"))
        self.feed.start()
        self._set_status("IN TRADE" if self.position else
                         ("DONE FOR THE DAY" if self.traded_today else "RUNNING"))
        self._monitor()

    def stop(self):
        self.stop_event.set()
        if self.feed:
            self.feed.stop()
        self._close_ws()
        if self.position and self.position.status != "CLOSED":
            log.warning("  Engine stopped with a position OPEN — it is NOT squared off. "
                        "Restart to resume managing it, or close it in the terminal.")
        self._save_state()
        self._set_status("STOPPED")


# ═══════════════════════════════════════════════════════════════════════════
# CONSOLE RUNNER
# ═══════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    ap = argparse.ArgumentParser(description="NIFTY First-Candle Condition Option Seller")
    ap.add_argument("--live", action="store_true", help="place real orders (default paper)")
    ap.add_argument("--lots", type=int, default=None)
    a = ap.parse_args()
    cfg = EngineConfig.load()
    if a.live:
        cfg.mode = "live"
    if a.lots:
        cfg.lots = a.lots
    eng = Engine(cfg)
    try:
        eng.run()
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop()


if __name__ == "__main__":
    main()
