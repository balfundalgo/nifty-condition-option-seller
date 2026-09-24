#!/usr/bin/env python3
"""
dhan.py — Dhan v2 connection layer
══════════════════════════════════
Auth, REST, orders and WebSocket packet parsing. Lifted from the hardened
hybrid model in sensex-vwap-ladder (itself built on RA17's order engine), made
index-generic for NIFTY.

What is carried over, and why each piece exists:

* Token: verify -> renew -> TOTP. Any 401 / 403 / DH-901 / DH-906 mid-session
  re-authenticates IN PLACE and flags the websocket to rebuild, because the
  websocket URL embeds the token.
* IPv6 probe at start-up. A dead IPv6 route makes every call stall for minutes
  before falling back; the probe forces IPv4 when that is the case.
* Two HTTP sessions. Reads retry on a stale pooled socket. Orders NEVER retry
  automatically — a POST that looks failed may have reached the exchange.
* Rate gates on data, order and option-chain calls.
* Repeated identical API errors log once a minute with a suppressed count.

Balfund Trading Pvt Ltd | www.balfund.com
"""

import os, sys, io, csv, time, struct, socket, threading, logging
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Optional, List, Dict, Tuple

import requests
import pyotp
from urllib3.util.retry import Retry
from dotenv import load_dotenv, set_key

log = logging.getLogger("FCOS")

# ═══════════════════════════════════════════════════════════════════════════
# PATHS / CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).resolve().parent

ENV_FILE = BASE_DIR / ".env"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
if not ENV_FILE.exists():
    try:
        ENV_FILE.touch()
    except Exception:
        pass
load_dotenv(str(ENV_FILE), override=True)

IST = timezone(timedelta(hours=5, minutes=30))
BASE_URL = "https://api.dhan.co/v2"
AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
CONNECT_TIMEOUT = 5.0
SLOW_CALL_SECONDS = 3.0

NIFTY = {"name": "NIFTY", "security_id": "13", "idx_segment": "IDX_I",
         "segment": "NSE_FNO", "strike_gap": 50}


# ═══════════════════════════════════════════════════════════════════════════
# CREDENTIALS (single shared state — every caller reads from here)
# ═══════════════════════════════════════════════════════════════════════════

class _Creds:
    def __init__(self):
        self.client_id = os.getenv("DHAN_CLIENT_ID", "").strip()
        self.pin = os.getenv("DHAN_PIN", "").strip()
        self.totp_secret = os.getenv("DHAN_TOTP_SECRET", "").strip()
        self.token = os.getenv("DHAN_ACCESS_TOKEN", "").strip()

    @property
    def ws_url(self) -> str:
        return (f"wss://api-feed.dhan.co?version=2&token={self.token}"
                f"&clientId={self.client_id}&authType=2")

    def headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json", "Accept": "application/json",
                "access-token": self.token, "client-id": self.client_id}


CREDS = _Creds()
WS_NEEDS_RECONNECT = threading.Event()
AUTH_FAILED = threading.Event()


def set_credentials(client_id: str, pin: str, totp_secret: str, token: str = ""):
    CREDS.client_id = (client_id or "").strip()
    CREDS.pin = (pin or "").strip()
    CREDS.totp_secret = (totp_secret or "").strip()
    CREDS.token = (token or "").strip()


def save_credentials_to_env():
    for k, v in (("DHAN_CLIENT_ID", CREDS.client_id), ("DHAN_PIN", CREDS.pin),
                 ("DHAN_TOTP_SECRET", CREDS.totp_secret),
                 ("DHAN_ACCESS_TOKEN", CREDS.token)):
        try:
            set_key(str(ENV_FILE), k, v)
        except Exception as e:
            log.warning(f"Could not write {k} to .env: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# NETWORK — IPv6 probe, sessions, rate gates
# ═══════════════════════════════════════════════════════════════════════════

def force_ipv4(enabled: bool = True) -> bool:
    try:
        import urllib3.util.connection as u3c
        u3c.allowed_gai_family = (lambda: socket.AF_INET) if enabled else \
            (lambda: socket.AF_UNSPEC)
        return True
    except Exception as e:
        log.warning(f"Could not set IPv4-only mode: {e}")
        return False


def auto_select_ip_family(host="api.dhan.co", port=443, budget=2.0) -> str:
    """
    One probe at start-up. api.dhan.co advertises several IPv6 addresses; if
    the route is dead Python tries each in turn with the full timeout. The
    client runs an EXE and will never edit .env, so this decides by itself.
    """
    v = os.getenv("DHAN_FORCE_IPV4", "").strip().lower()
    if v in ("1", "true", "yes"):
        force_ipv4(True)
        log.info("DHAN_FORCE_IPV4=1 — resolving IPv4 only")
        return "forced"
    if v in ("0", "false", "no"):
        return "disabled"
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)
    except Exception:
        return "no-ipv6"
    if not infos:
        return "no-ipv6"
    af, st_, proto, _, sa = infos[0]
    sock = None
    try:
        sock = socket.socket(af, st_, proto)
        sock.settimeout(budget)
        sock.connect(sa)
        return "ipv6-ok"
    except Exception:
        force_ipv4(True)
        log.warning(f"IPv6 to {host} did not answer in {budget:.0f}s — forcing IPv4")
        return "auto-ipv4"
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


_read_retry = Retry(total=3, connect=3, read=2, status=0, backoff_factor=0.4,
                    allowed_methods=frozenset(["GET", "POST"]),
                    raise_on_status=False)
_NO_POOL = os.getenv("DHAN_NO_POOL", "").strip().lower() in ("1", "true", "yes")

SESSION = requests.Session()            # reads: charts, chains, quotes
SESSION.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=1 if _NO_POOL else 4,
    pool_maxsize=1 if _NO_POOL else 8, max_retries=_read_retry))
if _NO_POOL:
    SESSION.headers["Connection"] = "close"

ORDER_SESSION = requests.Session()      # orders: never retried automatically
ORDER_SESSION.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=2, pool_maxsize=4, max_retries=0))


class RateGate:
    def __init__(self, max_per_sec: float):
        self.min_gap = 1.0 / max_per_sec
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            gap = time.time() - self._last
            if gap < self.min_gap:
                time.sleep(self.min_gap - gap)
            self._last = time.time()


DATA_GATE = RateGate(4.0)
ORDER_GATE = RateGate(8.0)
OC_GATE = RateGate(0.30)


# ═══════════════════════════════════════════════════════════════════════════
# TOKEN MANAGER + MID-SESSION RE-AUTH
# ═══════════════════════════════════════════════════════════════════════════

class DhanTokenManager:
    def verify(self, token: str) -> bool:
        if not token:
            return False
        try:
            h = {"access-token": token, "client-id": CREDS.client_id}
            return SESSION.get(f"{BASE_URL}/profile", headers=h,
                               timeout=(CONNECT_TIMEOUT, 10)).status_code == 200
        except Exception:
            return False

    def renew(self, token: str) -> Optional[str]:
        try:
            h = {"access-token": token, "dhanClientId": CREDS.client_id,
                 "Content-Type": "application/json"}
            d = SESSION.get(f"{BASE_URL}/RenewToken", headers=h, timeout=15).json()
            if "accessToken" in d:
                log.info("Token renewed")
                return d["accessToken"]
            log.warning(f"Renew failed: {d}")
        except Exception as e:
            log.warning(f"Renew error: {e}")
        return None

    def generate(self, max_retries=3) -> Optional[str]:
        if not (CREDS.client_id and CREDS.pin and CREDS.totp_secret):
            log.error("Client ID, PIN and TOTP secret are all required to generate a token")
            return None
        for attempt in range(max_retries):
            rem = 30 - (int(time.time()) % 30)
            if attempt > 0 or rem < 10:
                log.info(f"  Waiting {rem + 1}s for a fresh TOTP window...")
                time.sleep(rem + 1)
            totp = pyotp.TOTP(CREDS.totp_secret).now()
            try:
                params = {"dhanClientId": CREDS.client_id, "pin": CREDS.pin, "totp": totp}
                d = SESSION.post(AUTH_URL, params=params, timeout=15).json()
                if "accessToken" in d:
                    log.info("Token generated via TOTP")
                    return d["accessToken"]
                log.warning(f"Generate attempt {attempt + 1} failed: "
                            f"{d.get('errorMessage', d)}")
            except Exception as e:
                log.warning(f"Generate error: {e}")
        return None

    def ensure_token(self) -> str:
        if CREDS.token:
            log.info("Verifying existing token...")
            if self.verify(CREDS.token):
                return CREDS.token
            log.info("Token invalid, trying renew...")
            t = self.renew(CREDS.token)
            if t:
                self._save(t)
                return t
            log.info("Renew failed, generating via TOTP...")
        else:
            log.info("No token, generating via TOTP...")
        t = self.generate()
        if not t:
            raise RuntimeError("Could not obtain a Dhan access token")
        self._save(t)
        return t

    @staticmethod
    def _save(token: str):
        try:
            set_key(str(ENV_FILE), "DHAN_ACCESS_TOKEN", token)
        except Exception:
            pass


_AUTH_LOCK = threading.Lock()
_LAST_REAUTH = 0.0
REAUTH_COOLDOWN = 40.0          # a TOTP window is 30s — do not hammer it


def reauthenticate(reason: str = "") -> bool:
    """Fresh token mid-session. Serialised and rate-limited. True if it changed."""
    global _LAST_REAUTH
    with _AUTH_LOCK:
        if time.time() - _LAST_REAUTH < REAUTH_COOLDOWN:
            return False
        _LAST_REAUTH = time.time()
        log.warning(f"  TOKEN REJECTED ({reason}) — re-authenticating in place")
        try:
            CREDS.token = DhanTokenManager().ensure_token()
        except Exception as e:
            AUTH_FAILED.set()
            log.error(f"  Re-authentication FAILED: {e}")
            return False
        WS_NEEDS_RECONNECT.set()
        AUTH_FAILED.clear()
        log.info("  Re-authenticated — websocket will reconnect with the new token")
        return True


def init_credentials(status_cb=None) -> str:
    auto_select_ip_family()
    if status_cb:
        status_cb("Authenticating with Dhan...")
    CREDS.token = DhanTokenManager().ensure_token()
    log.info("Dhan credentials ready")
    return CREDS.token


# ═══════════════════════════════════════════════════════════════════════════
# TIME HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def now_ist() -> datetime:
    return datetime.now(IST)


def ws_epoch(ts) -> int:
    """WebSocket LTT may arrive IST-shifted; REST epochs are true UNIX."""
    ts = int(ts)
    if int(4.5 * 3600) <= (ts - int(time.time())) <= int(6.5 * 3600):
        ts -= 19800
    return ts


def epoch_to_ist(ts, fmt="%H:%M") -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts), tz=IST).strftime(fmt)


def session_anchor_epoch(dt: Optional[datetime] = None) -> int:
    """Epoch of 09:15:00 IST on the given day."""
    d = (dt or now_ist()).astimezone(IST)
    return int(d.replace(hour=9, minute=15, second=0, microsecond=0).timestamp())


def anchor_of(ts: int) -> int:
    return session_anchor_epoch(datetime.fromtimestamp(int(ts), tz=IST))


def hhmm(s: str):
    return datetime.strptime(s, "%H:%M").time()


# ═══════════════════════════════════════════════════════════════════════════
# REST
# ═══════════════════════════════════════════════════════════════════════════

_ERR_SEEN: Dict[Tuple[str, int], Tuple[float, int]] = {}


def _log_api_error(endpoint: str, code: int, body: str):
    key = (endpoint, code)
    now = time.time()
    last, hidden = _ERR_SEEN.get(key, (0.0, 0))
    if now - last > 60:
        extra = f"  ({hidden} identical suppressed)" if hidden else ""
        log.error(f"  API {endpoint} -> HTTP {code}: {body[:160]}{extra}")
        _ERR_SEEN[key] = (now, 0)
    else:
        _ERR_SEEN[key] = (last, hidden + 1)


def api_post(endpoint: str, payload: dict, retries: int = 2, timeout: float = 15):
    if "optionchain" in endpoint:
        OC_GATE.wait()
    else:
        DATA_GATE.wait()
    for att in range(retries + 1):
        t0 = time.time()
        try:
            r = SESSION.post(f"{BASE_URL}{endpoint}", headers=CREDS.headers(),
                             json=payload, timeout=(CONNECT_TIMEOUT, timeout))
            dt = time.time() - t0
            if dt > SLOW_CALL_SECONDS:
                log.warning(f"  SLOW API {endpoint} took {dt:.1f}s (HTTP {r.status_code})")
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                log.warning(f"  API {endpoint} rate limited, backing off")
                time.sleep(2 ** (att + 1))
                continue
            body = r.text or ""
            if r.status_code in (401, 403) or "DH-901" in body or "DH-906" in body:
                if reauthenticate(f"HTTP {r.status_code} on {endpoint}"):
                    continue
                _log_api_error(endpoint, r.status_code, body)
                time.sleep(2.0)
                continue
            _log_api_error(endpoint, r.status_code, body)
            if att < retries:
                time.sleep(1.5)
        except requests.exceptions.Timeout:
            log.error(f"  API {endpoint} TIMED OUT after {timeout}s "
                      f"(attempt {att + 1}/{retries + 1})")
            if att < retries:
                time.sleep(1.5)
        except Exception as e:
            log.error(f"  API {endpoint} error after {time.time() - t0:.1f}s: {e}")
            if att < retries:
                time.sleep(1.5)
    return None


def api_get(endpoint: str, timeout: float = 10):
    DATA_GATE.wait()
    try:
        r = SESSION.get(f"{BASE_URL}{endpoint}", headers=CREDS.headers(),
                        timeout=(CONNECT_TIMEOUT, timeout))
        if r.status_code == 200:
            return r.json()
        if r.status_code in (401, 403):
            reauthenticate(f"HTTP {r.status_code} on {endpoint}")
        _log_api_error(endpoint, r.status_code, r.text or "")
    except Exception as e:
        log.error(f"  API GET {endpoint}: {e}")
    return None


# ─── market data ───

def fetch_intraday_1m(security_id: str, segment: str, instrument: str,
                      days: int = 1) -> List[dict]:
    """1-minute bars. Every higher timeframe is rolled up from these."""
    to_d = now_ist().strftime("%Y-%m-%d")
    fr_d = (now_ist() - timedelta(days=days)).strftime("%Y-%m-%d")
    payload = {"securityId": str(security_id), "exchangeSegment": segment,
               "instrument": instrument, "interval": "1",
               "fromDate": fr_d, "toDate": to_d}
    resp = api_post("/charts/intraday", payload, retries=1, timeout=12)
    if not resp or "open" not in resp:
        return []
    n = len(resp["open"])
    tss = resp.get("timestamp", [0] * n)
    vols = resp.get("volume", [0] * n)
    out = []
    for i in range(n):
        out.append({"ts": int(tss[i]) if i < len(tss) else 0,
                    "open": float(resp["open"][i]), "high": float(resp["high"][i]),
                    "low": float(resp["low"][i]), "close": float(resp["close"][i]),
                    "volume": float(vols[i]) if i < len(vols) else 0.0})
    return out


def fetch_expiry_list(underlying: dict = NIFTY) -> List[str]:
    payload = {"UnderlyingScrip": int(underlying["security_id"]),
               "UnderlyingSeg": underlying["idx_segment"]}
    resp = api_post("/optionchain/expirylist", payload)
    if resp and resp.get("status") == "success":
        return resp.get("data", [])
    log.error(f"  Expiry list failed: {resp}")
    return []


def pick_expiry(expiries: List[str], today: date) -> Optional[str]:
    """
    Current weekly — but never the one expiring today. On expiry day the
    next weekly is used, so a sold option is never carried into its own
    expiry session.
    """
    future = sorted(datetime.strptime(e, "%Y-%m-%d").date() for e in expiries
                    if datetime.strptime(e, "%Y-%m-%d").date() > today)
    return future[0].strftime("%Y-%m-%d") if future else None


def fetch_option_chain(expiry: str, underlying: dict = NIFTY) -> Optional[dict]:
    payload = {"UnderlyingScrip": int(underlying["security_id"]),
               "UnderlyingSeg": underlying["idx_segment"], "Expiry": expiry}
    resp = api_post("/optionchain", payload)
    if resp and resp.get("status") == "success":
        return {"spot": float(resp["data"].get("last_price", 0) or 0),
                "oc": resp["data"]["oc"]}
    log.error(f"  Option chain failed for {expiry}: {str(resp)[:200]}")
    return None


def chain_security_id(oc: dict, strike: float, opt: str) -> Optional[str]:
    """Look up the security id for strike + 'CE'/'PE' in an option chain."""
    for k, v in oc.items():
        try:
            if abs(float(k) - float(strike)) < 0.01:
                leg = v.get(opt.lower())
                if leg and leg.get("security_id"):
                    return str(leg["security_id"])
        except Exception:
            continue
    return None


def fetch_lot_size(root: str = "NIFTY") -> Optional[int]:
    """Lot size read fresh every session from the scrip master — never hard-coded."""
    log.info(f"  Fetching {root} lot size from scrip master...")
    try:
        r = SESSION.get(MASTER_URL, stream=True, timeout=120)
        r.raise_for_status()
        raw = r.content.decode("utf-8", errors="ignore")
        if raw.startswith("\ufeff"):
            raw = raw[1:]
        for row in csv.DictReader(io.StringIO(raw)):
            if row.get("SEM_INSTRUMENT_NAME", "").strip().upper() != "OPTIDX":
                continue
            ts = row.get("SEM_TRADING_SYMBOL", "").upper()
            if not ts.startswith(root + "-"):
                continue
            try:
                lot = int(float(row.get("SEM_LOT_UNITS", "0").strip()))
            except Exception:
                continue
            if lot > 0:
                log.info(f"  {root} lot size = {lot}")
                return lot
    except Exception as e:
        log.error(f"  Scrip master error: {e}")
    return None


# ─── orders ───

def place_order_limit_ioc(security_id, segment, side, qty, price,
                          price_buffer=0.10, max_retries=3) -> dict:
    """Limit+IOC with a widening buffer, then Market fallback. Never auto-retried at HTTP level."""
    for att in range(max_retries):
        ORDER_GATE.wait()
        adj = round(price + price_buffer * (1 if side == "BUY" else -1), 2)
        adj = max(adj, 0.05)
        pl = {"dhanClientId": str(CREDS.client_id), "transactionType": side,
              "exchangeSegment": segment, "productType": "INTRADAY",
              "orderType": "LIMIT", "validity": "IOC",
              "securityId": str(security_id), "quantity": int(qty),
              "disclosedQuantity": 0, "price": adj, "triggerPrice": 0,
              "afterMarketOrder": False}
        log.info(f"  [ORDER] {side} {segment}:{security_id} qty={qty} "
                 f"LIMIT@{adj} IOC (att {att + 1}/{max_retries})")
        try:
            r = ORDER_SESSION.post(f"{BASE_URL}/orders", headers=CREDS.headers(),
                                   json=pl, timeout=15)
            if r.status_code == 200:
                d = r.json()
                oid = str(d.get("orderId", ""))
                status = str(d.get("orderStatus", "")).upper()
                avg = float(d.get("averageTradedPrice", 0) or 0)
                if status == "TRADED" and avg > 0:
                    log.info(f"  [FILLED] {side} qty={qty} @ {avg:.2f} id={oid}")
                    return {"filled": True, "price": avg, "order_id": oid}
                if status in ("REJECTED", "CANCELLED"):
                    err = d.get("omsErrorDescription") or d.get("errorMessage") or status
                    log.warning(f"  [REJECTED] {err} id={oid}")
                    break
                if oid:
                    for _ in range(6):
                        time.sleep(0.4)
                        ORDER_GATE.wait()
                        try:
                            pr = ORDER_SESSION.get(f"{BASE_URL}/orders/{oid}",
                                                   headers=CREDS.headers(), timeout=10)
                            if pr.status_code == 200:
                                pd = pr.json()
                                if isinstance(pd, list):
                                    pd = pd[0] if pd else {}
                                ps = str(pd.get("orderStatus", "")).upper()
                                pp = float(pd.get("averageTradedPrice", 0) or 0)
                                if ps == "TRADED" and pp > 0:
                                    log.info(f"  [FILLED] {side} qty={qty} @ {pp:.2f}")
                                    return {"filled": True, "price": pp, "order_id": oid}
                                if ps in ("REJECTED", "CANCELLED", "EXPIRED"):
                                    break
                        except Exception:
                            pass
                    try:
                        ORDER_SESSION.delete(f"{BASE_URL}/orders/{oid}",
                                             headers=CREDS.headers(), timeout=10)
                    except Exception:
                        pass
            else:
                log.error(f"  [ORDER] HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log.error(f"  [ORDER] Error: {e}")
        price_buffer += 0.10
        time.sleep(0.5)
    log.warning(f"  [FALLBACK] {side} {segment}:{security_id} -> MARKET")
    return place_order_market(security_id, segment, side, qty)


def place_order_market(security_id, segment, side, qty) -> dict:
    ORDER_GATE.wait()
    pl = {"dhanClientId": str(CREDS.client_id), "transactionType": side,
          "exchangeSegment": segment, "productType": "INTRADAY",
          "orderType": "MARKET", "validity": "DAY",
          "securityId": str(security_id), "quantity": int(qty),
          "disclosedQuantity": 0, "price": 0, "triggerPrice": 0,
          "afterMarketOrder": False}
    try:
        r = ORDER_SESSION.post(f"{BASE_URL}/orders", headers=CREDS.headers(),
                               json=pl, timeout=15)
        if r.status_code == 200:
            d = r.json()
            oid = str(d.get("orderId", ""))
            avg = float(d.get("averageTradedPrice", 0) or 0)
            if avg > 0:
                log.info(f"  [MKT FILLED] {side} qty={qty} @ {avg:.2f}")
                return {"filled": True, "price": avg, "order_id": oid}
            for _ in range(8):
                time.sleep(0.5)
                ORDER_GATE.wait()
                try:
                    pr = ORDER_SESSION.get(f"{BASE_URL}/orders/{oid}",
                                           headers=CREDS.headers(), timeout=10)
                    if pr.status_code == 200:
                        pd = pr.json()
                        if isinstance(pd, list):
                            pd = pd[0] if pd else {}
                        pp = float(pd.get("averageTradedPrice", 0) or 0)
                        if pp > 0:
                            log.info(f"  [MKT FILLED] {side} qty={qty} @ {pp:.2f}")
                            return {"filled": True, "price": pp, "order_id": oid}
                        if str(pd.get("orderStatus", "")).upper() == "REJECTED":
                            log.error(f"  [MKT REJECTED] {pd.get('omsErrorDescription', '')}")
                            break
                except Exception:
                    pass
            return {"filled": False, "price": 0.0, "order_id": oid}
        log.error(f"  [MKT ORDER] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"  [MKT ORDER] Error: {e}")
    return {"filled": False, "price": 0.0, "order_id": ""}


def get_positions() -> List[dict]:
    resp = api_get("/positions")
    if not isinstance(resp, list):
        return []
    return [p for p in resp if isinstance(p, dict) and int(p.get("netQty", 0) or 0) != 0]


# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET PACKETS — Ticker mode only (RequestCode 15 -> response code 2)
# ═══════════════════════════════════════════════════════════════════════════

REQ_SUB_TICKER = 15
RESP_TICKER = 2
RESP_DISCONNECT = 50
EXCH_SEG_MAP = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CUR",
                4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CUR", 8: "BSE_FNO"}


def parse_header(msg: bytes) -> Optional[dict]:
    if len(msg) < 8:
        return None
    return {"resp_code": msg[0], "seg": EXCH_SEG_MAP.get(msg[3], str(msg[3])),
            "security_id": str(struct.unpack_from("<I", msg, 4)[0]),
            "payload": msg[8:]}


def parse_ticker(payload: bytes) -> Optional[dict]:
    if len(payload) < 8:
        return None
    return {"ltp": float(struct.unpack_from("<f", payload, 0)[0]),
            "ltt": int(struct.unpack_from("<I", payload, 4)[0])}
