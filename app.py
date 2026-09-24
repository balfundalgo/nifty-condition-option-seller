#!/usr/bin/env python3
"""
app.py — GUI for the NIFTY First-Candle Condition Option Seller
═══════════════════════════════════════════════════════════════
Left: credentials + settings.  Right: live dashboard — prices, ranges, the
ten condition machines (5 conditions x CE/PE), position and activity log.

Balfund Trading Pvt Ltd
"""

import queue
import logging
import threading
from tkinter import messagebox

import customtkinter as ctk

import dhan
from dhan import CREDS, DhanTokenManager, set_credentials, save_credentials_to_env
from engine import Engine, EngineConfig, VERSION, log
from patterns import PATTERNS, PATTERN_LABELS
from strategy import CONDITIONS, CONDITION_NAMES

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

BG = "#f0f2f5"; CARD = "#ffffff"; ACC = "#0369a1"; GRN = "#16a34a"; RED = "#dc2626"
AMB = "#b45309"; TXT = "#111827"; DIM = "#6b7280"; BD = "#d1d5db"
FT = ("Segoe UI", 18, "bold"); FH = ("Segoe UI", 13, "bold"); FS = ("Segoe UI", 12)
FL = ("Segoe UI", 11); FB = ("Segoe UI", 20, "bold"); FX = ("Consolas", 11)

STATE_COLOURS = {"WAIT_SETUP": DIM, "WAIT_TRIGGER": AMB, "WAIT_REVERSAL": ACC,
                 "WAIT_REV1": AMB, "WAIT_BREAK2": AMB, "WAIT_REV2": ACC,
                 "FIRED": GRN, "DEAD": BD, "LOCKED": DIM}
STATE_LABELS = {"WAIT_SETUP": "waiting for setup", "WAIT_TRIGGER": "armed — waiting trigger",
                "WAIT_REVERSAL": "waiting reversal", "WAIT_REV1": "waiting reversal #1",
                "WAIT_BREAK2": "waiting break of peak 1", "WAIT_REV2": "waiting reversal #2",
                "FIRED": "FIRED", "DEAD": "not today", "LOCKED": "locked"}


class QueueHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.q.put(("log", self.format(record)))


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"NIFTY First-Candle Condition Option Seller v{VERSION} — Balfund Trading")
        self.geometry("1500x940")
        self.minsize(1250, 800)
        self.configure(fg_color=BG)
        self.q = queue.Queue()
        log.addHandler(QueueHandler(self.q))
        self.engine = None
        self.cfg = EngineConfig.load()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._drain)

    # ═══ layout ═══

    def _build(self):
        top = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=60)
        top.pack(fill="x")
        ctk.CTkLabel(top, text="NIFTY First-Candle Condition Option Seller",
                     font=FT, text_color=TXT).pack(side="left", padx=16, pady=12)
        self.mode_badge = ctk.CTkLabel(top, text="", font=FH, corner_radius=6, width=90)
        self.mode_badge.pack(side="left", padx=6)
        self.status_lbl = ctk.CTkLabel(top, text="IDLE", font=FH, text_color=DIM)
        self.status_lbl.pack(side="left", padx=16)
        self.sq_btn = ctk.CTkButton(top, text="SQUARE OFF", fg_color=AMB, width=120,
                                    command=self._square_off, state="disabled")
        self.sq_btn.pack(side="right", padx=8)
        self.stop_btn = ctk.CTkButton(top, text="STOP", fg_color=RED, width=100,
                                      command=self._stop, state="disabled")
        self.stop_btn.pack(side="right", padx=4)
        self.start_btn = ctk.CTkButton(top, text="START", fg_color=GRN, width=100,
                                       command=self._start)
        self.start_btn.pack(side="right", padx=4)

        body = ctk.CTkFrame(self, fg_color=BG)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        left = ctk.CTkScrollableFrame(body, fg_color=CARD, width=360)
        left.pack(side="left", fill="y", padx=(0, 10))
        right = ctk.CTkFrame(body, fg_color=BG)
        right.pack(side="left", fill="both", expand=True)
        self._build_settings(left)
        self._build_dashboard(right)
        self._refresh_mode_badge()

    def _sec(self, p, text):
        ctk.CTkLabel(p, text=text, font=FH, text_color=ACC, anchor="w").pack(
            fill="x", padx=10, pady=(14, 4))

    def _entry(self, p, label, value="", show=None, width=150):
        row = ctk.CTkFrame(p, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(row, text=label, font=FL, text_color=TXT, anchor="w",
                     width=170).pack(side="left")
        e = ctk.CTkEntry(row, width=width, show=show)
        e.insert(0, str(value))
        e.pack(side="right")
        return e

    def _build_settings(self, p):
        self._sec(p, "Dhan credentials")
        self.e_cid = self._entry(p, "Client ID", CREDS.client_id)
        self.e_pin = self._entry(p, "PIN", CREDS.pin, show="•")
        self.e_totp = self._entry(p, "TOTP secret", CREDS.totp_secret, show="•")
        self.e_tok = self._entry(p, "Access token", CREDS.token, show="•")
        r = ctk.CTkFrame(p, fg_color="transparent")
        r.pack(fill="x", padx=10, pady=6)
        ctk.CTkButton(r, text="Save", width=80, command=self._save_creds).pack(side="left", padx=2)
        ctk.CTkButton(r, text="Generate token", width=120,
                      command=self._gen_token).pack(side="left", padx=2)
        ctk.CTkButton(r, text="Verify", width=80, command=self._verify).pack(side="left", padx=2)

        c = self.cfg
        self._sec(p, "Trading")
        row = ctk.CTkFrame(p, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(row, text="Mode", font=FL, width=170, anchor="w").pack(side="left")
        self.seg_mode = ctk.CTkSegmentedButton(row, values=["paper", "live"],
                                               command=lambda _: self._refresh_mode_badge())
        self.seg_mode.set(c.mode)
        self.seg_mode.pack(side="right")
        self.e_lots = self._entry(p, "Lots", c.lots)
        self.e_lot_ovr = self._entry(p, "Lot size (0 = auto)", c.lot_size_override)
        self.e_itm = self._entry(p, "ITM offset (points)", c.itm_offset)

        self._sec(p, "Rules")
        self.e_slbuf = self._entry(p, "SL buffer (points)", c.sl_buffer)
        self.e_manip = self._entry(p, "Manipulation close (pts)", c.manip_points)
        self.e_start = self._entry(p, "Entry from (candle close)", c.entry_start)
        self.e_end = self._entry(p, "Entry until (candle close)", c.entry_end)
        self.e_sq = self._entry(p, "Square-off", c.square_off)
        self.e_age = self._entry(p, "Max signal age (s)", c.max_signal_age)
        self.cb_same = ctk.CTkCheckBox(p, text="Allow reversal on the trigger candle", font=FL)
        if c.allow_reversal_on_trigger_bar:
            self.cb_same.select()
        self.cb_same.pack(anchor="w", padx=10, pady=4)

        self._sec(p, "Conditions")
        self.cb_cond = {}
        for k in CONDITIONS:
            cb = ctk.CTkCheckBox(p, text=CONDITION_NAMES[k], font=FL)
            if c.conditions.get(k, True):
                cb.select()
            cb.pack(anchor="w", padx=10, pady=2)
            self.cb_cond[k] = cb

        self._sec(p, "Reversal candles")
        self.cb_pat = {}
        for k in PATTERNS:
            cb = ctk.CTkCheckBox(p, text=PATTERN_LABELS[k], font=FL)
            if c.patterns.get(k, True):
                cb.select()
            cb.pack(anchor="w", padx=10, pady=2)
            self.cb_pat[k] = cb

        ctk.CTkButton(p, text="Save settings", command=self._save_settings).pack(
            fill="x", padx=10, pady=14)

    def _card(self, parent, title):
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=8)
        ctk.CTkLabel(f, text=title, font=FL, text_color=DIM).pack(anchor="w", padx=10, pady=(6, 0))
        v = ctk.CTkLabel(f, text="-", font=FB, text_color=TXT)
        v.pack(anchor="w", padx=10, pady=(0, 6))
        return f, v

    def _build_dashboard(self, p):
        cards = ctk.CTkFrame(p, fg_color=BG)
        cards.pack(fill="x")
        self.v = {}
        for i, (k, t) in enumerate((("SPOT", "NIFTY spot"), ("CE", "CE"), ("PE", "PE"),
                                    ("HL", "Day high / low"), ("FEED", "Feed"))):
            f, lbl = self._card(cards, t)
            f.grid(row=0, column=i, sticky="nsew", padx=4)
            cards.grid_columnconfigure(i, weight=1)
            self.v[k] = lbl
            if k in ("CE", "PE"):
                self.v[k + "_title"] = f.winfo_children()[0]

        self.setup_lbl = ctk.CTkLabel(p, text="Strikes resolve at 09:20 from the 09:15 candle close.",
                                      font=FS, text_color=DIM, anchor="w")
        self.setup_lbl.pack(fill="x", padx=6, pady=(8, 2))

        mid = ctk.CTkFrame(p, fg_color=BG)
        mid.pack(fill="x", pady=4)
        rf = ctk.CTkFrame(mid, fg_color=CARD, corner_radius=8)
        rf.pack(side="left", fill="both", padx=4)
        ctk.CTkLabel(rf, text="Ranges", font=FH, text_color=TXT).pack(anchor="w", padx=10, pady=4)
        self.ranges = ctk.CTkTextbox(rf, width=360, height=150, font=FX)
        self.ranges.pack(padx=8, pady=(0, 8))
        self.ranges.insert("end", "Waiting for the 09:15 candle...")
        self.ranges.configure(state="disabled")

        pf = ctk.CTkFrame(mid, fg_color=CARD, corner_radius=8)
        pf.pack(side="left", fill="both", expand=True, padx=4)
        ctk.CTkLabel(pf, text="Position", font=FH, text_color=TXT).pack(anchor="w", padx=10, pady=4)
        self.pos_lbl = ctk.CTkLabel(pf, text="Flat", font=("Segoe UI", 14), text_color=DIM,
                                    justify="left", anchor="w")
        self.pos_lbl.pack(anchor="w", padx=12)
        self.pnl_lbl = ctk.CTkLabel(pf, text="", font=FB, text_color=TXT)
        self.pnl_lbl.pack(anchor="w", padx=12, pady=6)

        cf = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        cf.pack(fill="x", padx=4, pady=6)
        ctk.CTkLabel(cf, text="Conditions", font=FH, text_color=TXT).pack(anchor="w", padx=10, pady=4)
        grid = ctk.CTkFrame(cf, fg_color=CARD)
        grid.pack(fill="x", padx=8, pady=(0, 8))
        self.rows = {}
        r = 0
        for cond in CONDITIONS:
            for side in ("CE", "PE"):
                key = f"{cond}-{side}"
                ctk.CTkLabel(grid, text=f"{CONDITION_NAMES[cond]}  · sell {side}", font=FL,
                             width=190, anchor="w").grid(row=r, column=0, sticky="w")
                st = ctk.CTkLabel(grid, text="—", font=("Segoe UI", 11, "bold"),
                                  width=190, anchor="w", text_color=DIM)
                st.grid(row=r, column=1, sticky="w")
                nt = ctk.CTkLabel(grid, text="", font=FL, anchor="w", text_color=DIM)
                nt.grid(row=r, column=2, sticky="w")
                self.rows[key] = (st, nt)
                r += 1
        grid.grid_columnconfigure(2, weight=1)

        lf = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        lf.pack(fill="both", expand=True, padx=4, pady=4)
        ctk.CTkLabel(lf, text="Activity", font=FH, text_color=TXT).pack(anchor="w", padx=10, pady=4)
        self.logbox = ctk.CTkTextbox(lf, font=FX)
        self.logbox.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # ═══ credentials ═══

    def _read_creds(self):
        set_credentials(self.e_cid.get(), self.e_pin.get(), self.e_totp.get(), self.e_tok.get())

    def _save_creds(self):
        self._read_creds()
        save_credentials_to_env()
        self._log("Credentials saved to .env")

    def _gen_token(self):
        self._read_creds()

        def run():
            dhan.auto_select_ip_family()
            t = DhanTokenManager().generate()
            if t:
                CREDS.token = t
                save_credentials_to_env()
                self.q.put(("token", t))
                log.info("New access token generated and saved")
            else:
                log.error("Token generation failed — check client id, PIN and TOTP secret")
        threading.Thread(target=run, daemon=True).start()

    def _verify(self):
        self._read_creds()

        def run():
            ok = DhanTokenManager().verify(CREDS.token)
            (log.info if ok else log.error)(f"Token {'is VALID' if ok else 'is INVALID or expired'}")
        threading.Thread(target=run, daemon=True).start()

    # ═══ settings ═══

    def _collect(self) -> EngineConfig:
        c = EngineConfig.load()
        try:
            c.mode = self.seg_mode.get()
            c.lots = max(1, int(self.e_lots.get()))
            c.lot_size_override = max(0, int(self.e_lot_ovr.get() or 0))
            c.itm_offset = int(self.e_itm.get())
            c.sl_buffer = float(self.e_slbuf.get())
            c.manip_points = float(self.e_manip.get())
            for e in (self.e_start, self.e_end, self.e_sq):
                dhan.hhmm(e.get().strip())
            c.entry_start = self.e_start.get().strip()
            c.entry_end = self.e_end.get().strip()
            c.square_off = self.e_sq.get().strip()
            c.max_signal_age = float(self.e_age.get())
        except Exception as e:
            raise ValueError(f"Invalid setting: {e}")
        c.allow_reversal_on_trigger_bar = bool(self.cb_same.get())
        c.conditions = {k: bool(cb.get()) for k, cb in self.cb_cond.items()}
        c.patterns = {k: bool(cb.get()) for k, cb in self.cb_pat.items()}
        return c

    def _save_settings(self):
        try:
            self.cfg = self._collect()
        except ValueError as e:
            messagebox.showerror("Settings", str(e))
            return
        self.cfg.save()
        self._log("Settings saved")
        self._refresh_mode_badge()

    def _refresh_mode_badge(self):
        m = self.seg_mode.get()
        self.mode_badge.configure(text=m.upper(), fg_color=(RED if m == "live" else ACC),
                                  text_color="#ffffff")

    # ═══ run control ═══

    def _start(self):
        try:
            self.cfg = self._collect()
        except ValueError as e:
            messagebox.showerror("Settings", str(e))
            return
        if not any(self.cfg.conditions.values()):
            messagebox.showerror("Settings", "Enable at least one condition.")
            return
        if not any(self.cfg.patterns.values()):
            messagebox.showerror("Settings", "Enable at least one reversal candle type.")
            return
        if self.cfg.mode == "live" and not messagebox.askyesno(
                "LIVE mode", "LIVE mode places real SELL orders on Dhan.\n\nContinue?"):
            return
        self.cfg.save()
        self._read_creds()
        save_credentials_to_env()
        self.engine = Engine(self.cfg, gui_callback=lambda e, d: self.q.put((e, d)))
        threading.Thread(target=self._run_engine, daemon=True).start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.sq_btn.configure(state="normal")
        self.seg_mode.configure(state="disabled")

    def _run_engine(self):
        try:
            self.engine.run()
        except Exception as e:
            log.exception(f"Engine crashed: {e}")
        finally:
            self.q.put(("engine_done", {}))

    def _stop(self):
        if self.engine:
            if self.engine.position and not messagebox.askyesno(
                    "Stop", "A position is OPEN. Stopping does NOT square it off.\n\n"
                            "Stop anyway?"):
                return
            self.engine.stop()

    def _square_off(self):
        if self.engine and self.engine.position:
            if messagebox.askyesno("Square off", "Buy back the open position now?"):
                self.engine.manual_square_off()
        else:
            self._log("No open position")

    def _on_close(self):
        if self.engine and self.engine.position and not messagebox.askyesno(
                "Exit", "A position is OPEN and will NOT be squared off.\n\nExit anyway?"):
            return
        if self.engine:
            self.engine.stop()
        self.destroy()

    # ═══ events ═══

    def _log(self, msg):
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")
        n = int(self.logbox.index("end-1c").split(".")[0])
        if n > 3000:
            self.logbox.delete("1.0", f"{n - 2500}.0")

    def _drain(self):
        try:
            for _ in range(300):
                ev, d = self.q.get_nowait()
                self._handle(ev, d)
        except queue.Empty:
            pass
        self.after(200, self._drain)

    def _handle(self, ev, d):
        if ev == "log":
            self._log(d)
        elif ev == "token":
            self.e_tok.delete(0, "end")
            self.e_tok.insert(0, d)
        elif ev == "status":
            s = d.get("status", "")
            col = GRN if s in ("RUNNING", "IN TRADE") else RED if (
                "FAIL" in s or s in ("NO DATA", "STOPPED")) else AMB
            self.status_lbl.configure(text=s, text_color=col)
        elif ev == "setup":
            self.v["CE_title"].configure(text=f"CE {d['strikes']['CE']:.0f}")
            self.v["PE_title"].configure(text=f"PE {d['strikes']['PE']:.0f}")
            self.setup_lbl.configure(
                text=f"ATM {d['atm']}   ·   CE {d['strikes']['CE']:.0f}   ·   "
                     f"PE {d['strikes']['PE']:.0f}   ·   expiry {d['expiry']}   ·   "
                     f"qty {d['lot_size']} x {d['lots']} = {d['lot_size'] * d['lots']}   ·   "
                     f"{d['mode'].upper()}", text_color=TXT)
        elif ev == "strategy":
            self._show_strategy(d)
        elif ev == "tick":
            self._show_tick(d)
        elif ev == "trade_closed":
            pts = d["entry"] - d["exit"]
            self._log(f"TRADE CLOSED {d['side']} {d['reason']}  {pts:+.2f} pts  "
                      f"{pts * d['qty']:+,.2f}")
        elif ev == "engine_done":
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.sq_btn.configure(state="disabled")
            self.seg_mode.configure(state="normal")

    def _show_strategy(self, s):
        lines = []
        for name, key in (("R1 09:15", "r1"), ("R2 09:20", "r2")):
            r = s.get(key) or {}
            if not r:
                continue
            lines.append(f"{name}      high      low      open")
            for leg in ("SPOT", "CE", "PE"):
                x = r.get(leg)
                if x:
                    lines.append(f"  {leg:<5} {x['high']:>10.2f} {x['low']:>9.2f} {x['open']:>9.2f}")
            lines.append("")
        self.ranges.configure(state="normal")
        self.ranges.delete("1.0", "end")
        self.ranges.insert("end", "\n".join(lines) if lines else "Waiting for the 09:15 candle...")
        self.ranges.configure(state="disabled")
        for m in s.get("machines", []):
            st, nt = self.rows[m["key"]]
            st.configure(text=STATE_LABELS.get(m["state"], m["state"]),
                         text_color=STATE_COLOURS.get(m["state"], DIM))
            nt.configure(text=m["note"][:110])

    def _show_tick(self, d):
        ltp = d["ltp"]
        for k in ("SPOT", "CE", "PE"):
            self.v[k].configure(text=f"{ltp[k]:,.2f}" if ltp[k] else "-")
        if d["day_high"]:
            self.v["HL"].configure(text=f"{d['day_high']:,.0f} / {d['day_low']:,.0f}")
        lag = d.get("avg_lag")
        self.v["FEED"].configure(
            text=f"WS {'on' if d['ws'] else 'off'} · lag {lag:.1f}s" if lag is not None
            else f"WS {'on' if d['ws'] else 'off'}",
            text_color=GRN if d["ws"] else RED)
        p = d.get("position")
        if p:
            self.pos_lbl.configure(
                text=f"SHORT {p['side']} {p['strike']:.0f}  x{p['qty']}  "
                     f"({CONDITION_NAMES[p['condition']]}, {p['pattern']})\n"
                     f"Entry {p['entry']:.2f} at {p['entry_time']}   ·   "
                     f"SL {p['sl']:.2f} (option)   ·   "
                     f"Target {p['target']:.2f} (spot {p['target_kind'].replace('_', ' ')})\n"
                     f"Status {p['status']}", text_color=TXT)
            u = d["upnl"]
            self.pnl_lbl.configure(text=f"{u:+,.2f}", text_color=GRN if u >= 0 else RED)
        else:
            r = d.get("realised", 0.0)
            self.pos_lbl.configure(text="Flat" + ("  ·  traded today" if d.get("traded_today") else ""),
                                   text_color=DIM)
            self.pnl_lbl.configure(text=f"Day {r:+,.2f}" if d.get("traded_today") else "",
                                   text_color=GRN if r >= 0 else RED)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
