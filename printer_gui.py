#!/usr/bin/env python3
"""Bambu printer LAN monitor window (written for the X2D).

Enter the printer's IP address and LAN access code and press Connect. The
serial number is read from the printer's TLS certificate, so it does not have
to be typed. The window shows the print state, stage, speed mode and alerts
(HMS text from Bambu Studio's downloaded files), every temperature and fan,
the nozzle and filament in each hotend, live charts with hotend-switch
markers, measured hotend-switch times, the AMS units and an event log.
History can be saved as CSV and every report recorded as JSON lines.

Passive MQTT by default. Disconnect and turn off "Passive MQTT" to request
full status or firmware/calibration data. "Enable controls" additionally lets
the Controls row pause,
resume or stop the print and set the chamber light, target temperatures,
printing speed and air duct mode (printer_control.py; the printer must be in
Developer Mode). Passive mode blocks all application publishes in the transport.
English/Czech translations are editable JSON; the Inspector preserves unknown
fields and query replies. Standard library only (Tkinter + MQTT 3.1.1).
"""
from __future__ import annotations

import bisect
import csv
import json
import logging
import os
import queue
import sys
import time
import tkinter as tk
from datetime import datetime
from itertools import repeat
from types import SimpleNamespace
import webbrowser
from tkinter import filedialog, font, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bambu_monitor as lan  # noqa: E402
import printer_control as pc  # noqa: E402
from printer_link import PrinterLink  # noqa: E402
from printer_discovery import DiscoveryDialog  # noqa: E402
from printer_inspector import ReportInspector  # noqa: E402
from printer_history import RingSeries, iter_window  # noqa: E402
from printer_settings import write_settings  # noqa: E402
from printer_state import TELEMETRY_TIMEOUT, merged_copy, validate_message  # noqa: E402
from printer_queries import QUERIES, build_query  # noqa: E402
from i18n import available_languages, bind_static_text, get_language, set_language, tr  # noqa: E402
from monitor import HOTEND_NAMES, decode  # noqa: E402
from printer_report import SWITCH_STATES, HmsText, SwitchTimer, decode_extras  # noqa: E402

APP_TITLE = "Printer Monitor"
SETTINGS = os.path.join(os.path.expanduser("~"), ".bambu-printer-monitor.json")
HISTORY_LIMIT = 12 * 3600          # samples kept (1 per second)
GAP_SECONDS = 10                   # break lines across longer gaps
LOGGER = logging.getLogger(__name__)

# Chart colours: reference palette (dataviz skill), light surface. Fixed per entity.
SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
STATUS = {"ok": "#0ca30c", "busy": "#fab219", "error": "#d03b3b", "off": "#898781"}

TEMP_SERIES = [  # key, label, colour, target key
    ("T_Main", "Main hotend (left)", SLOTS[0], "T_Main_tgt"),
    ("T_Aux", "Aux hotend (right)", SLOTS[1], "T_Aux_tgt"),
    ("T_bed", "Bed", SLOTS[2], "T_bed_tgt"),
    ("T_chamber", "Chamber", SLOTS[3], "T_chamber_tgt"),
]
FAN_ORDER = ["Parts", "Aux left", "Aux right", "Exhaust", "Hotend", "Heat", "MCBoard", "Hotend1"]
FAN_SERIES = [("fan_" + n, n, SLOTS[i], None) for i, n in enumerate(FAN_ORDER)]
STATE_TEXT = {"RUNNING": "Printing", "IDLE": "Idle", "PAUSE": "Paused", "FINISH": "Finished",
              "FAILED": "Failed", "PREPARE": "Preparing", "SLICING": "Slicing"}
QUIET_FANS = {"Parts", "Hotend", "Hotend1", "MCBoard"}  # change constantly; logged only on request
WINDOWS = [("2 min", 120), ("10 min", 600), ("30 min", 1800), ("2 h", 7200), ("All", None)]


# --------------------------------------------------------------------------- charts

def nice_step(span: float, target: int) -> float:
    for s in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500):
        if span / s <= target:
            return s
    return 1000


TIME_STEPS = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800]


class Chart:
    """Line chart on a Tk canvas: legend toggles, direct labels, hotend markers, crosshair."""

    L, R, T, B = 48, 195, 30, 22

    def __init__(self, app: "App", parent, title: str, unit: str, series, fixed_max=None, step=False):
        self.app, self.title, self.unit, self.series = app, title, unit, series
        self.fixed_max, self.step = fixed_max, step
        self.hidden: set = set()
        self.canvas = tk.Canvas(parent, bg=SURFACE, highlightthickness=1, highlightbackground=GRID, height=210)
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda e: app.set_hover(None))
        self.geom = None

    def _x(self, t):
        L, T, pw, ph, t0, t1, y0, y1 = self.geom
        return L + (t - t0) / max(t1 - t0, 1e-9) * pw

    def _y(self, v):
        L, T, pw, ph, t0, t1, y0, y1 = self.geom
        return T + ph - (v - y0) / max(y1 - y0, 1e-9) * ph

    def _motion(self, e):
        if not self.geom:
            return
        L, T, pw, ph, t0, t1, *_ = self.geom
        if L <= e.x <= L + pw and T <= e.y <= T + ph:
            self.app.set_hover(t0 + (e.x - L) / pw * (t1 - t0))
        else:
            self.app.set_hover(None)

    def _toggle(self, key):
        self.hidden.symmetric_difference_update({key})
        self.draw()

    def present(self):
        return [s for s in self.series if s[0] in self.app.chart_data().vals]

    def draw(self):
        c, app = self.canvas, self.app
        data = app.chart_data()
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if W < 200 or H < 100:
            return
        L, R, T, B = self.L, self.R, self.T, self.B
        pw, ph = W - L - R, H - T - B
        t0, t1 = app.view_range()
        i0, i1 = bisect.bisect_left(data.t, t0), bisect.bisect_right(data.t, t1)
        shown = [s for s in self.present() if s[0] not in self.hidden]

        # title and legend (click to show/hide)
        c.create_text(L, 14, text=tr(self.title), anchor="w", font=app.f_bold, fill=INK)
        x = L + app.f_bold_m.measure(tr(self.title)) + 18
        for key, label, colour, tgt in self.present():
            off = key in self.hidden
            tag = "lg_" + key
            c.create_line(x, 14, x + 14, 14, fill=AXIS if off else colour, width=3, tags=tag)
            c.create_text(x + 19, 14, text=tr(label), anchor="w", font=app.f_small, fill=MUTED if off else INK2, tags=tag)
            c.tag_bind(tag, "<Button-1>", lambda e, k=key: self._toggle(k))
            x += 19 + app.f_small_m.measure(tr(label)) + 16
        if self.series is TEMP_SERIES:
            c.create_text(W - 8, 14, text=tr("dashed = target"), anchor="e", font=app.f_small, fill=MUTED)

        # y range
        vmax = 0.0
        for key, _, _, tgt in shown:
            for k in (key, tgt):
                if k and k in data.vals:
                    vmax = max(vmax, max((v for v in iter_window(data.vals[k], i0, i1)
                                        if v is not None), default=0))
        if self.fixed_max:
            y0, y1 = 0.0, float(self.fixed_max)
        else:
            y0, y1 = 0.0, max(50.0, (int(vmax * 1.1) // 50 + 1) * 50)
        self.geom = (L, T, pw, ph, t0, t1, y0, y1)

        # grid and axes
        ystep = nice_step(y1 - y0, 5)
        v = y0
        while v <= y1 + 1e-9:
            y = self._y(v)
            c.create_line(L, y, L + pw, y, fill=AXIS if v == y0 else GRID)
            c.create_text(L - 6, y, text=f"{v:g}", anchor="e", font=app.f_small, fill=MUTED)
            v += ystep
        c.create_text(L - 6, T - 12, text=self.unit, anchor="e", font=app.f_small, fill=MUTED)
        span = t1 - t0
        tstep = next((s for s in TIME_STEPS if span / s <= 7), 21600)
        tt = (int(t0) // tstep + 1) * tstep
        fmt = "%H:%M:%S" if tstep < 60 else "%H:%M"
        while tt < t1:
            xx = self._x(tt)
            c.create_line(xx, T, xx, T + ph, fill=GRID)
            c.create_text(xx, T + ph + 4, text=time.strftime(fmt, time.localtime(tt)), anchor="n",
                          font=app.f_small, fill=MUTED)
            tt += tstep

        # hotend switch markers
        for mt, name in data.switches:
            if t0 <= mt <= t1:
                xx = self._x(mt)
                c.create_line(xx, T, xx, T + ph, fill=INK2, dash=(2, 3))
                c.create_text(xx + 3, T + 2, text="→" + name, anchor="nw", font=app.f_small, fill=INK2)

        # lines (targets first, dashed)
        stride = max(1, (i1 - i0 + pw - 1) // pw) if pw > 0 else 1
        visible_times = list(iter_window(data.t, i0, i1))
        for key, _, colour, tgt in shown:
            if tgt and tgt in data.vals:
                for run in self._runs(data.vals[tgt], i0, i1, stride, step=True, times=visible_times):
                    c.create_line(*run, fill=colour, width=1.5, dash=(4, 3))
        ends = []
        for key, label, colour, _ in shown:
            last = None
            for run in self._runs(data.vals[key], i0, i1, stride, step=self.step, times=visible_times):
                c.create_line(*run, fill=colour, width=2)
                last = run
            if last:
                ends.append([last[-1], last[-1], label, colour, last[-2]])

        # direct labels at the line ends (only with four lines or fewer)
        if 0 < len(ends) <= 4:
            ends.sort(key=lambda e: e[0])
            for a, b in zip(ends, ends[1:]):
                if b[1] - a[1] < 13:
                    b[1] = a[1] + 13
            for y, ly, label, colour, xe in ends:
                lx = L + pw + 8
                c.create_line(lx, ly, lx + 10, ly, fill=colour, width=3)
                c.create_text(lx + 14, ly, text=tr(label), anchor="w", font=app.f_small, fill=INK2)
        self.draw_hover()

    def _runs(self, vals, i0, i1, stride, step, times=None):
        """Reduce continuous runs by bucket extrema, preserving gaps and peaks."""
        if times is None:
            times = list(iter_window(self.app.chart_data().t, i0, i1))
        vals = list(iter_window(vals, i0, i1))
        i, i1 = 0, len(vals)
        while i < i1:
            if vals[i] is None:
                i += 1
                continue
            start = i
            i += 1
            while i < i1 and vals[i] is not None and times[i] - times[i - 1] <= GAP_SECONDS:
                i += 1
            stop = i
            cur, previous = [], None
            for bucket in range(start, stop, stride):
                end = min(bucket + stride, stop)
                low = min(range(bucket, end), key=vals.__getitem__)
                high = max(range(bucket, end), key=vals.__getitem__)
                for index in sorted({bucket, low, high, end - 1}):
                    value = vals[index]
                    x, y = self._x(times[index]), self._y(value)
                    if not cur:
                        cur = [x, y]
                    elif step:
                        if value != previous:
                            cur.extend((x, cur[-1], x, y))
                    else:
                        cur.extend((x, y))
                    previous = value
            if step and cur:
                cur.extend((self._x(times[stop - 1]), cur[-1]))
            if len(cur) == 2:
                cur.extend((cur[0], cur[1]))
            if cur:
                yield cur

    def draw_hover(self):
        c, app = self.canvas, self.app
        data = app.chart_data()
        c.delete("hover")
        if app.hover_t is None or not self.geom or not data.t:
            return
        L, T, pw, ph, t0, t1, *_ = self.geom
        i = min(max(bisect.bisect_left(data.t, app.hover_t), 0), len(data.t) - 1)
        if i > 0 and abs(data.t[i - 1] - app.hover_t) < abs(data.t[i] - app.hover_t):
            i -= 1
        t = data.t[i]
        if not t0 <= t <= t1:
            return
        x = self._x(t)
        c.create_line(x, T, x, T + ph, fill=INK2, tags="hover")
        rows = [(None, time.strftime("%H:%M:%S", time.localtime(t))
                  + (f"  ·  {data.active[i]}" if data.active[i] else ""))]
        for key, label, colour, tgt in self.present():
            if key in self.hidden:
                continue
            v = data.vals[key][i]
            if v is None:
                continue
            txt = f"{tr(label)}  {v:g} {self.unit}"
            if tgt and tgt in data.vals and data.vals[tgt][i]:
                txt += f"  (target {data.vals[tgt][i]:g})"
            rows.append((colour, txt))
            if v is not None:
                c.create_oval(x - 4, self._y(v) - 4, x + 4, self._y(v) + 4, fill=colour,
                              outline=SURFACE, width=2, tags="hover")
        lh = 16
        w = max(app.f_small_m.measure(r[1]) for r in rows) + 30
        h = lh * len(rows) + 10
        bx = x + 12 if x + 12 + w < L + pw + self.R - 4 else x - 12 - w
        by = T + 6
        c.create_rectangle(bx, by, bx + w, by + h, fill=SURFACE, outline=AXIS, tags="hover")
        for n, (colour, txt) in enumerate(rows):
            yy = by + 5 + lh * n + lh / 2
            if colour:
                c.create_rectangle(bx + 8, yy - 4, bx + 16, yy + 4, fill=colour, outline="", tags="hover")
            c.create_text(bx + (22 if colour else 8), yy, text=txt, anchor="w", font=app.f_small,
                          fill=INK if colour else INK2, tags="hover")


class HotendStrip:
    """Thin band showing which hotend was active over time."""

    def __init__(self, app: "App", parent):
        self.app = app
        self.canvas = tk.Canvas(parent, bg=SURFACE, highlightthickness=0, height=22)
        self.canvas.bind("<Configure>", lambda e: self.draw())

    def draw(self):
        c, app = self.canvas, self.app
        data = app.chart_data()
        c.delete("all")
        W = c.winfo_width()
        L, R = Chart.L, Chart.R
        pw = W - L - R
        if pw < 50 or not data.t:
            return
        t0, t1 = app.view_range()
        c.create_text(L - 6, 11, text=tr("Hotend"), anchor="e", font=app.f_small, fill=MUTED)
        X = lambda t: L + (t - t0) / max(t1 - t0, 1e-9) * pw  # noqa: E731
        i0, i1 = bisect.bisect_left(data.t, t0), bisect.bisect_right(data.t, t1)
        seg_start, seg_name = None, None
        for i in range(i0, i1 + 1):
            name = data.active[i] if i < i1 else None
            gap = i > i0 and i < i1 and data.t[i] - data.t[i - 1] > GAP_SECONDS
            if name != seg_name or i == i1 or gap:
                if seg_name:
                    xa, xb = X(max(seg_start, t0)), X(data.t[i - 1])
                    dark = seg_name == "Aux"
                    c.create_rectangle(xa, 3, min(L + pw, max(xb, xa + 1)), 19,
                                       fill=INK2 if dark else AXIS, outline="")
                    if xb - xa > 40:
                        c.create_text((xa + xb) / 2, 11, text=tr(seg_name), font=app.f_small,
                                      fill="#ffffff" if dark else INK)
                seg_start, seg_name = (data.t[i] if i < i1 else None), name


# --------------------------------------------------------------------------- app

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("1240x1000")
        root.minsize(1000, 860)
        base = font.nametofont("TkDefaultFont")
        fam, size = base.actual("family"), abs(base.actual("size"))
        self.f_small = font.Font(family=fam, size=max(size - 2, 9))
        self.f_bold = font.Font(family=fam, size=size, weight="bold")
        self.f_big = font.Font(family=fam, size=size + 6, weight="bold")
        self.f_value = font.Font(family=fam, size=size + 1)
        self.f_small_m, self.f_bold_m = self.f_small, self.f_bold

        self.q: queue.Queue = queue.Queue()
        self._closing = False
        self.link: PrinterLink | None = None
        self.state: dict = {}
        self.extras: dict = {}
        self.t = RingSeries(HISTORY_LIMIT)
        self.vals: dict = {}
        self.active = RingSeries(HISTORY_LIMIT)
        self.stage_hist = RingSeries(HISTORY_LIMIT)
        self.switches: list = []
        self.prev = None
        self.prev_x: dict = {}
        self.hover_t = None
        self.frozen_at = None
        self.frozen_data = None
        self.last_msg = 0.0
        self.msg_times: list = []
        self.timer = SwitchTimer()
        self.hms = HmsText()
        self.alerts: dict = {}          # code -> (kind, first seen)
        self.ams_sig = None
        self.sensor_sig = None
        self.alert_links: dict = {}
        self.inspector = ReportInspector()
        self.inspector_revision = -1
        self.inspector_rows = {}
        self.pending_query = None
        self.raw_fh = None
        self.raw_count = 0
        self.controls: dict = {}        # decoded by printer_control.decode_controls
        self.sent: dict = {}            # sequence id -> description, to match the printer's replies
        self.hold: dict = {}            # control -> time until which the report must not overwrite it

        self.settings = self._load_settings()
        set_language(self.settings.get("language", "cs"))
        self._build()
        self._text_bindings = bind_static_text(root, exclude=(self.status_lbl, self.data_age_lbl, self.alert_lbl, self.ctl_msg,
            self.raw_lbl, self.switch_lbl, self.freeze_btn, self.connect_btn, self.raw_btn, self.ams_frame))
        self._set_status("off", "Not connected")
        self.connect_btn.configure(text=tr("Connect"))
        self.raw_btn.configure(text=tr("Record raw…"))
        self.freeze_btn.configure(text=tr("Freeze view"))
        self._refresh_controls()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(200, self._drain)
        root.after(1000, self._tick)

    # ---- settings
    def _load_settings(self) -> dict:
        try:
            with open(SETTINGS, encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        data = {"ip": self.ip.get().strip(), "serial": self.serial.get().strip(),
                "window": self.window.get(), "remember": bool(self.remember.get()),
                "all_fans": bool(self.log_all_fans.get()),
                "passive": bool(self.passive.get()), "cert_file": self.cert_file.get().strip(),
                "model": self.model.get().strip(), "language": get_language()}
        trusted = dict(self.settings.get("trusted_printers") or {})
        if data["ip"] and data["cert_file"]:
            trusted[data["ip"].casefold()] = {"serial": data["serial"], "cert_file": data["cert_file"]}
        data["trusted_printers"] = trusted
        if self.remember.get():
            data["code"] = self.code.get().strip()
        try:
            write_settings(SETTINGS, data)
            self.settings = data
        except (OSError, ValueError) as exc:
            LOGGER.warning("Could not save monitor preferences: %s", exc)

    # ---- layout
    def _build(self) -> None:
        r = self.root
        pad = {"padx": 10, "pady": 3}
        s = self.settings

        language_row = ttk.Frame(r)
        language_row.pack(fill="x", **pad)
        ttk.Label(language_row, text="X2D Printer Monitor", font=self.f_bold).pack(side="left")
        self.languages = available_languages()
        self.language = tk.StringVar(value=self.languages.get(get_language(), "English"))
        language_cb = ttk.Combobox(language_row, textvariable=self.language, values=list(self.languages.values()),
                                   state="readonly", width=14)
        language_cb.pack(side="right")
        language_cb.bind("<<ComboboxSelected>>", self._change_language)
        ttk.Label(language_row, text="Language").pack(side="right", padx=8)

        conn = ttk.Frame(r)
        conn.pack(fill="x", **pad)
        ttk.Label(conn, text="IP address").grid(row=0, column=0, sticky="w")
        self.ip = tk.StringVar(value=s.get("ip", ""))
        ttk.Entry(conn, textvariable=self.ip, width=16).grid(row=0, column=1, padx=(4, 14))
        ttk.Label(conn, text="Access code").grid(row=0, column=2, sticky="w")
        self.code = tk.StringVar(value=s.get("code", ""))
        self.code_entry = ttk.Entry(conn, textvariable=self.code, width=12, show="•")
        self.code_entry.grid(row=0, column=3, padx=4)
        self.show_code = tk.BooleanVar(value=False)
        ttk.Checkbutton(conn, text="Show", variable=self.show_code,
                        command=lambda: self.code_entry.config(show="" if self.show_code.get() else "•")
                        ).grid(row=0, column=4, padx=(0, 14))
        ttk.Label(conn, text="Serial").grid(row=0, column=5, sticky="w")
        self.serial = tk.StringVar(value=s.get("serial", ""))
        ttk.Entry(conn, textvariable=self.serial, width=17).grid(row=0, column=6, padx=4)
        ttk.Label(conn, text="(blank = automatic)", foreground=MUTED).grid(row=0, column=7, padx=(0, 14))
        self.connect_btn = ttk.Button(conn, text="Connect", command=self.toggle_connect)
        self.connect_btn.grid(row=0, column=8)
        self.remember = tk.BooleanVar(value=s.get("remember", False))
        ttk.Checkbutton(conn, text="Remember access code on this computer", variable=self.remember
                        ).grid(row=1, column=2, columnspan=5, sticky="w", pady=(2, 0))
        self.status_dot = tk.Canvas(conn, width=12, height=12, highlightthickness=0)
        self.status_dot.grid(row=4, column=0, sticky="w")
        self.status_lbl = ttk.Label(conn, text="Not connected")
        self.status_lbl.grid(row=4, column=1, columnspan=8, sticky="w", padx=(4, 0))
        self.data_age_lbl = ttk.Label(conn, text=tr("No telemetry received yet."), foreground=MUTED)
        self.data_age_lbl.grid(row=5, column=1, columnspan=8, sticky="w", padx=(4, 0))
        self._set_status("off", "Not connected")
        for w in conn.winfo_children():
            if isinstance(w, ttk.Entry):
                w.bind("<Return>", lambda e: self.toggle_connect())

        self.discover_btn = ttk.Button(conn, text="Discover LAN…", command=self.discover)
        self.discover_btn.grid(row=1, column=0, columnspan=2, sticky="w", pady=2)
        options = ttk.Frame(conn)
        options.grid(row=2, column=0, columnspan=9, sticky="w", pady=2)
        self.passive = tk.BooleanVar(value=s.get("passive", True))
        ttk.Checkbutton(options, text="Passive MQTT (no commands or status requests)", variable=self.passive,
                        command=self._refresh_controls).pack(side="left")
        ttk.Label(options, text="Model (optional)").pack(side="left", padx=(18, 4))
        self.model = tk.StringVar(value=s.get("model", "X2D"))
        ttk.Entry(options, textvariable=self.model, width=12).pack(side="left")
        trust = ttk.Frame(conn)
        trust.grid(row=3, column=0, columnspan=9, sticky="we", pady=2)
        ttk.Label(trust, text="Trusted certificate").pack(side="left")
        self.cert_file = tk.StringVar(value=s.get("cert_file", ""))
        ttk.Entry(trust, textvariable=self.cert_file, width=48).pack(side="left", padx=6)
        ttk.Button(trust, text="Browse…", command=self._browse_certificate).pack(side="left")
        ttk.Label(trust, text="Empty = TLS identity unverified", foreground=MUTED).pack(side="left", padx=8)
        self.connection_widgets = [w for frame in (conn, options, trust) for w in frame.winfo_children()
                                   if isinstance(w, (ttk.Entry, ttk.Checkbutton, ttk.Button))
                                   and w is not self.connect_btn]

        ttk.Separator(r).pack(fill="x", padx=10, pady=3)

        # print status
        top = ttk.Frame(r)
        top.pack(fill="x", **pad)
        self.state_lbl = ttk.Label(top, text="—", font=self.f_big)
        self.state_lbl.grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(top, length=260, maximum=100)
        self.progress.grid(row=0, column=1, padx=16)
        self.progress_lbl = ttk.Label(top, text="", font=self.f_value)
        self.progress_lbl.grid(row=0, column=2, sticky="w")
        self.hotend_lbl = ttk.Label(top, text="", font=self.f_bold)
        self.hotend_lbl.grid(row=0, column=3, sticky="e", padx=(24, 0))
        top.columnconfigure(2, weight=1)
        line2 = ttk.Frame(top)
        line2.grid(row=1, column=0, columnspan=4, sticky="we", pady=(2, 0))
        self.stage_lbl = ttk.Label(line2, text="", font=self.f_value)
        self.stage_lbl.pack(side="left")
        self.speed_lbl = ttk.Label(line2, text="", foreground=INK2)
        self.speed_lbl.pack(side="left", padx=(18, 0))
        self.file_lbl = ttk.Label(line2, text="", foreground=INK2)
        self.file_lbl.pack(side="left", padx=(18, 0))
        alert = ttk.Frame(top)
        alert.grid(row=2, column=0, columnspan=4, sticky="we", pady=(2, 0))
        self.alert_dot = tk.Canvas(alert, width=12, height=12, highlightthickness=0)
        self.alert_dot.pack(side="left")
        self.alert_lbl = ttk.Label(alert, text="No alerts", foreground=INK2)
        self.alert_lbl.pack(side="left", padx=(4, 0))
        self._set_alert_dot(None)

        # current values
        vals = ttk.Frame(r)
        vals.pack(fill="x", **pad)
        self.temp_box = ttk.LabelFrame(vals, text="Temperatures")
        self.temp_box.pack(side="left", fill="y")
        self.fan_box = ttk.LabelFrame(vals, text="Fans")
        self.fan_box.pack(side="left", fill="y", padx=(10, 0))
        self.hot_box = ttk.LabelFrame(vals, text="Hotends")
        self.hot_box.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.value_labels: dict = {}
        for n, (key, label, colour, _) in enumerate(TEMP_SERIES):
            self._value_row(self.temp_box, key, label, colour, n, 0)
        self.hot_rows: dict = {}
        for row, name in enumerate(("Main", "Aux")):
            side = "left" if name == "Main" else "right"
            ttk.Label(self.hot_box, text=f"{name} ({side})", font=self.f_bold).grid(
                row=row, column=0, sticky="w", padx=(8, 6), pady=2)
            nz = ttk.Label(self.hot_box, text="—", width=30)
            nz.grid(row=row, column=1, sticky="w")
            sw = tk.Canvas(self.hot_box, width=14, height=14, highlightthickness=0)
            sw.grid(row=row, column=2, padx=(6, 4))
            fil = ttk.Label(self.hot_box, text="", width=58)
            fil.grid(row=row, column=3, sticky="w")
            step = ttk.Label(self.hot_box, text="", foreground=INK2)
            step.grid(row=row, column=4, sticky="w", padx=(6, 8))
            self.hot_rows[name] = (nz, sw, fil, step)
        self.switch_lbl = ttk.Label(self.hot_box, text=tr("Hotend switches: none measured yet"), foreground=INK2)
        self.switch_lbl.grid(row=2, column=0, columnspan=5, sticky="w", padx=8, pady=(4, 4))

        self._build_controls(r, pad)

        # charts
        charts = ttk.Frame(r)
        charts.pack(fill="both", expand=True, **pad)
        self.temp_chart = Chart(self, charts, "Temperatures", "°C", TEMP_SERIES)
        self.fan_chart = Chart(self, charts, "Fans", "%", FAN_SERIES, fixed_max=100, step=True)
        self.fan_chart.hidden = {"fan_Hotend", "fan_Hotend1"}  # flips 90/100 % every second; click to show
        self.temp_chart.canvas.config(height=130)   # minimum; both charts take the spare height
        self.fan_chart.canvas.config(height=130)
        self.strip = HotendStrip(self, charts)
        self.temp_chart.canvas.pack(fill="both", expand=True)
        self.fan_chart.canvas.pack(fill="both", expand=True, pady=(6, 0))
        self.strip.canvas.pack(fill="x")

        # controls
        ctl = ttk.Frame(r)
        ctl.pack(fill="x", **pad)
        ttk.Label(ctl, text="Show last").pack(side="left")
        self.window = tk.StringVar(value=s.get("window", "10 min"))
        cb = ttk.Combobox(ctl, textvariable=self.window, values=[w[0] for w in WINDOWS], width=8, state="readonly")
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.redraw())
        self.freeze_btn = ttk.Button(ctl, text="Freeze view", command=self.toggle_freeze)
        self.freeze_btn.pack(side="left", padx=4)
        ttk.Button(ctl, text="Clear history", command=self.clear).pack(side="left", padx=4)
        ttk.Button(ctl, text="Save CSV…", command=self.save_csv).pack(side="left", padx=4)
        self.raw_btn = ttk.Button(ctl, text="Record raw…", command=self.toggle_raw)
        self.raw_btn.pack(side="left", padx=4)
        self.raw_lbl = ttk.Label(ctl, text="", foreground=INK2)
        self.raw_lbl.pack(side="left", padx=4)
        self.log_all_fans = tk.BooleanVar(value=s.get("all_fans", False))
        ttk.Checkbutton(ctl, text="Log part-cooling and hotend fan changes", variable=self.log_all_fans
                        ).pack(side="right")

        # tabs: events, switches, AMS, alerts
        nb = self.notebook = ttk.Notebook(r, height=170)
        nb.pack(fill="x", padx=10, pady=(0, 10))
        ev = ttk.Frame(nb)
        nb.add(ev, text="Events")
        self.events = tk.Text(ev, height=8, wrap="word", font=font.nametofont("TkFixedFont"),
                              relief="flat", background=SURFACE, foreground=INK)
        sb = ttk.Scrollbar(ev, command=self.events.yview)
        self.events.config(yscrollcommand=sb.set, state="disabled")
        self.events.tag_config("switch", font=self.f_bold)
        self.events.tag_config("alert", foreground=STATUS["error"])
        self.events.tag_config("sent", foreground=SLOTS[0])
        sb.pack(side="right", fill="y")
        self.events.pack(fill="both", expand=True, padx=4, pady=4)

        swf = ttk.Frame(nb)
        nb.add(swf, text="Hotend switches")
        cols = ("time", "switch", "duration", "gap", "layer", "steps")
        self.sw_tree = ttk.Treeview(swf, columns=cols, show="headings", height=7)
        for c, w, txt in (("time", 80, "Time"), ("switch", 120, "Switch"), ("duration", 90, "Duration (s)"),
                          ("gap", 120, "Since previous (s)"), ("layer", 60, "Layer"), ("steps", 560, "Steps seen")):
            self.sw_tree.heading(c, text=txt)
            self.sw_tree.column(c, width=w, anchor="w", stretch=(c == "steps"))
        sb2 = ttk.Scrollbar(swf, command=self.sw_tree.yview)
        self.sw_tree.config(yscrollcommand=sb2.set)
        sb2.pack(side="right", fill="y")
        self.sw_tree.pack(fill="both", expand=True, padx=4, pady=4)

        self.ams_frame = ttk.Frame(nb)
        nb.add(self.ams_frame, text="AMS")
        ttk.Label(self.ams_frame, text="No AMS data yet.", foreground=INK2).pack(anchor="w", padx=8, pady=8)

        alf = ttk.Frame(nb)
        nb.add(alf, text="Alerts")
        self.al_tree = ttk.Treeview(alf, columns=("time", "kind", "code", "text"), show="headings", height=7)
        for c, w, txt in (("time", 80, "Since"), ("kind", 70, "Type"), ("code", 170, "Code"), ("text", 760, "Message")):
            self.al_tree.heading(c, text=txt)
            self.al_tree.column(c, width=w, anchor="w", stretch=(c == "text"))
        self.al_tree.tag_configure("cleared", foreground=MUTED)
        sb3 = ttk.Scrollbar(alf, command=self.al_tree.yview)
        self.al_tree.config(yscrollcommand=sb3.set)
        sb3.pack(side="right", fill="y")
        self.al_tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.al_tree.bind("<Double-1>", self._open_alert_link)
        ttk.Label(alf, text="Double-click an HMS alert to open its Bambu Wiki page.", foreground=MUTED).pack(anchor="w", padx=8)

        sensors = ttk.Frame(nb)
        nb.add(sensors, text="Sensors")
        self.sensor_tree = ttk.Treeview(sensors, columns=("value",), show="headings", height=7)
        self.sensor_tree.heading("value", text="Latest received sensor state")
        self.sensor_tree.column("value", width=1100)
        sensor_scroll = ttk.Scrollbar(sensors, command=self.sensor_tree.yview)
        self.sensor_tree.configure(yscrollcommand=sensor_scroll.set)
        sensor_scroll.pack(side="right", fill="y")
        self.sensor_tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.sensor_tree.insert("", "end", values=(tr("No sensor data received yet."),))

        diagnostics = ttk.Frame(nb)
        nb.add(diagnostics, text="Queries and calibration")
        query_row = ttk.Frame(diagnostics)
        query_row.pack(fill="x", padx=8, pady=4)
        self.query_ids = list(QUERIES)
        self.query_choice = ttk.Combobox(query_row, values=[tr(q.label) for q in QUERIES.values()],
                                         state="readonly", width=42)
        self.query_choice.current(0)
        self.query_choice.pack(side="left")
        ttk.Label(query_row, text="Nozzle mm").pack(side="left", padx=(8, 3))
        self.query_nozzle = ttk.Combobox(query_row, values=("0.2", "0.4", "0.6", "0.8"), width=4, state="readonly")
        self.query_nozzle.set("0.4")
        self.query_nozzle.pack(side="left")
        ttk.Label(query_row, text="Extruder id").pack(side="left", padx=(8, 3))
        self.query_extruder = ttk.Combobox(query_row, values=("1", "0"), width=3, state="readonly")
        self.query_extruder.set("1")
        self.query_extruder.pack(side="left")
        self.query_btn = ttk.Button(query_row, text="Send read query", command=self._send_query)
        self.query_btn.pack(side="left", padx=8)
        self.experimental_queries = tk.BooleanVar(value=False)
        ttk.Checkbutton(diagnostics, text="Enable experimental calibration reads (firmware-dependent)",
                        variable=self.experimental_queries).pack(anchor="w", padx=8)
        ttk.Label(diagnostics, text="X2D: Studio declares support_extrusion_cali=false. Bed mesh and vibration coefficients: no verified LAN read command.",
                  wraplength=1100, foreground=INK2).pack(anchor="w", padx=8, pady=3)
        self.query_status = ttk.Label(diagnostics, text="Active MQTT is required. Query responses appear in the Inspector.", wraplength=1100)
        self.query_status.pack(anchor="w", padx=8, pady=4)

        inspector = ttk.Frame(nb)
        nb.add(inspector, text="Inspector")
        search_row = ttk.Frame(inspector)
        search_row.pack(fill="x", padx=6, pady=3)
        ttk.Label(search_row, text="Filter fields").pack(side="left")
        self.field_filter = tk.StringVar()
        ttk.Entry(search_row, textvariable=self.field_filter, width=35).pack(side="left", padx=6)
        self.field_filter.trace_add("write", lambda *args: self._update_inspector(force=True))
        ttk.Button(search_row, text="Export observed JSON…", command=self._export_inspector).pack(side="left", padx=4)
        self.field_count = ttk.Label(search_row, text="")
        self.field_count.pack(side="left", padx=6)
        columns = ("path", "value", "previous", "changes", "seen", "hint")
        self.field_tree = ttk.Treeview(inspector, columns=columns, show="headings", height=5)
        for column, width, label in (("path", 340, "JSON path"), ("value", 150, "Raw value"),
                ("previous", 120, "Previous value"), ("changes", 60, "Changes"),
                ("seen", 85, "Received"), ("hint", 380, "Interpretation / source")):
            self.field_tree.heading(column, text=label)
            self.field_tree.column(column, width=width, minwidth=50, stretch=(column in ("path", "hint")))
        field_scroll = ttk.Scrollbar(inspector, command=self.field_tree.yview)
        field_scroll.pack(side="right", fill="y")
        horizontal = ttk.Scrollbar(inspector, orient="horizontal", command=self.field_tree.xview)
        horizontal.pack(side="bottom", fill="x")
        self.field_tree.configure(yscrollcommand=field_scroll.set, xscrollcommand=horizontal.set)
        self.field_tree.pack(fill="both", expand=True)
        nb.bind("<<NotebookTabChanged>>", lambda event: self._update_inspector(force=True))

    # ---- controls
    def _build_controls(self, r, pad) -> None:
        box = ttk.LabelFrame(r, text="Controls")
        box.pack(fill="x", **pad)
        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=(4, 2))
        self.ctl_on = tk.BooleanVar(value=False)
        self.controls_chk = ttk.Checkbutton(row, text="Enable controls", variable=self.ctl_on,
                                            command=self._refresh_controls)
        self.controls_chk.pack(side="left")
        self.pause_btn = ttk.Button(row, text="Pause", width=11, command=lambda: self._send(pc.pause()))
        self.resume_btn = ttk.Button(row, text="Resume", width=11, command=lambda: self._send(pc.resume()))
        self.stop_btn = ttk.Button(row, text="Stop…", width=7, command=self._stop_print)
        self.pause_btn.pack(side="left", padx=(16, 2))
        self.resume_btn.pack(side="left", padx=2)
        self.stop_btn.pack(side="left", padx=2)
        self.light_var = tk.BooleanVar(value=False)
        self.light_chk = ttk.Checkbutton(row, text="Chamber light", variable=self.light_var, command=self._set_light)
        self.light_chk.pack(side="left", padx=(16, 0))
        ttk.Label(row, text="Speed").pack(side="left", padx=(16, 4))
        self.speed_var = tk.StringVar()
        self.speed_cb = ttk.Combobox(row, textvariable=self.speed_var, values=list(pc.SPEED_LEVELS.values()),
                                     width=10, state="disabled")
        self.speed_cb.pack(side="left")
        self.speed_cb.bind("<<ComboboxSelected>>", lambda e: self._set_speed())
        ttk.Label(row, text="Air duct").pack(side="left", padx=(16, 4))
        self.duct_var = tk.StringVar()
        self.duct_cb = ttk.Combobox(row, textvariable=self.duct_var, values=[], width=10, state="disabled")
        self.duct_cb.pack(side="left")
        self.duct_cb.bind("<<ComboboxSelected>>", lambda e: self._set_duct())
        self.buzzer_btn = ttk.Button(row, text="Silence buzzer", command=lambda: self._send(pc.stop_buzzer()))
        self.buzzer_btn.pack(side="left", padx=(16, 0))
        self.sound_var = tk.BooleanVar(value=False)
        self.sound_chk = ttk.Checkbutton(row, text="Prompt sound", variable=self.sound_var, command=self._set_sound)
        self.sound_shown = False        # packed only when the printer reports that it has the option

        row2 = ttk.Frame(box)
        row2.pack(fill="x", padx=8, pady=(2, 6))
        ttk.Label(row2, text="Set target °C (0 = off)").pack(side="left")
        self.temp_entries: dict = {}
        for key, label in (("Main", "Main"), ("Aux", "Aux"), ("bed", "Bed"), ("chamber", "Chamber")):
            ttk.Label(row2, text=label).pack(side="left", padx=(14, 4))
            var = tk.StringVar()
            ent = ttk.Entry(row2, textvariable=var, width=5, justify="right")
            ent.pack(side="left")
            ent.bind("<Return>", lambda e, k=key: self._set_temp(k))
            btn = ttk.Button(row2, text="Set", width=6, command=lambda k=key: self._set_temp(k))
            btn.pack(side="left", padx=(3, 0))
            self.temp_entries[key] = (var, ent, btn)
        self.ctl_msg = ttk.Label(row2, text="", foreground=INK2)
        self.ctl_msg.pack(side="left", padx=(16, 0))
        self._refresh_controls()

    def _controls_live(self) -> bool:
        return bool(self.ctl_on.get() and self.link and not self.link.passive and self.link.online and self.state.get("print")
                    and self.last_msg and time.monotonic() - self.last_msg <= TELEMETRY_TIMEOUT)

    def _refresh_controls(self) -> None:
        """Enable what the printer can do now and show its current light, speed and air duct mode."""
        live = self._controls_live()
        passive = self.link.passive if self.link else self.passive.get()
        self.controls_chk.configure(state="disabled" if passive else "normal")
        if passive:
            self.ctl_on.set(False)
        if hasattr(self, "query_btn"):
            self.query_btn.configure(state="normal" if self.link and self.link.online and not passive
                                     and self.pending_query is None else "disabled")
        c = self.controls = pc.decode_controls(self.state.get("print") or {})
        now = time.time()

        def able(widget, on, normal="normal"):
            widget.config(state=normal if (live and on) else "disabled")

        able(self.pause_btn, c["state"] == "RUNNING")
        able(self.resume_btn, c["state"] == "PAUSE")
        able(self.stop_btn, c["printing"])
        able(self.light_chk, c["light"] is not None)
        able(self.speed_cb, c["printing"] and c["speed"], "readonly")
        able(self.duct_cb, bool(c["duct_modes"]), "readonly")
        able(self.buzzer_btn, True)
        able(self.sound_chk, True)
        if c["sound_supported"] != self.sound_shown:
            self.sound_shown = c["sound_supported"]
            if self.sound_shown:
                self.sound_chk.pack(side="left", padx=(16, 0))
            else:
                self.sound_chk.pack_forget()
        single = not c["extruder_ids"]
        for key, (var, ent, btn) in self.temp_entries.items():
            on = {"Main": c["nozzle_ready"], "Aux": c["nozzle_ready"] and not single,
                  "bed": True, "chamber": c["has_chamber"]}[key]
            able(ent, on)
            able(btn, on)

        if now > self.hold.get("light", 0) and c["light"] is not None:
            self.light_var.set(c["light"])
        if now > self.hold.get("speed", 0):
            self.speed_var.set(pc.SPEED_LEVELS.get(c["speed"], ""))
        if now > self.hold.get("sound", 0):
            self.sound_var.set(c["sound_on"])
        names = [pc.airduct_name(m) for m in c["duct_modes"]]
        if list(self.duct_cb.cget("values")) != names:
            self.duct_cb.config(values=names)
        if now > self.hold.get("duct", 0):
            self.duct_var.set(pc.airduct_name(c["duct_mode"]) if c["duct_mode"] in c["duct_modes"] else "")
        if now > self.hold.get("msg", 0):
            if passive:
                hint = "Passive MQTT. To use controls, disconnect and turn off Passive MQTT."
            elif not self.ctl_on.get():
                hint = "Tick “Enable controls” to send commands (printer in Developer Mode)."
            elif not live:
                hint = "Connect to the printer to use the controls."
            else:
                lim = c["limits"]
                hint = f"Limits: hotend {lim['nozzle']}, bed {lim['bed']}, chamber {lim['chamber']} °C."
            self.ctl_msg.config(text=tr(hint), foreground=INK2)

    def _note(self, text, error=False) -> None:
        self.ctl_msg.config(text=tr(text), foreground=STATUS["error"] if error else INK)
        self.hold["msg"] = time.time() + 8

    def _send(self, msgs, hold=None) -> bool:
        """Publish one command (or a list that belongs together) and log it."""
        if not self._controls_live():
            return False
        msgs = msgs if isinstance(msgs, list) else [msgs]
        text = pc.describe(msgs[0])
        for n, m in enumerate(msgs):
            if not self.link.publish(m):
                self._note("Not sent: the printer is not connected.", error=True)
                return False
            if n == 0:  # only the first answer is reported (the second light node may not exist)
                body = m.get("print") or m.get("system") or {}
                self.sent[body.get("sequence_id")] = pc.describe(m)
        if len(self.sent) > 200:
            for k in list(self.sent)[:100]:
                del self.sent[k]
        if hold:
            self.hold[hold] = time.time() + 4
        self._log("sent: " + text, "sent")
        self._note("Sent: " + text)
        return True

    def _is_reply(self, data) -> bool:
        """True for the printer's answer to a command (anything but a status push); logs our own."""
        for section in ("print", "system", "info", "pushing", "camera"):
            body = data.get(section)
            if not isinstance(body, dict) or "command" not in body or body["command"] == "push_status":
                continue
            text = self.sent.pop(body.get("sequence_id"), None)
            if text is not None and "result" in body:
                ok = str(body["result"]).lower() == "success"
                reason = body.get("reason") or body.get("err_code") or ""
                line = f"printer: {text}: {body['result']}" + (f" ({reason})" if reason and not ok else "")
                self._log(line, None if ok else "alert")
                if not ok:
                    self._note(line, error=True)
            return True
        return False

    def _stop_print(self) -> None:
        name = (self.state.get("print") or {}).get("subtask_name") or "the current print"
        if messagebox.askyesno(APP_TITLE, f"Stop {name}?\n\nA stopped print cannot be resumed.", icon="warning",
                               default="no"):
            self._send(pc.stop())

    def _set_light(self) -> None:
        if not self._send(pc.chamber_light(self.light_var.get()), hold="light"):
            self._refresh_controls()

    def _set_speed(self) -> None:
        level = next((k for k, v in pc.SPEED_LEVELS.items() if v == self.speed_var.get()), None)
        if level and level != self.controls.get("speed"):
            self._send(pc.speed(level), hold="speed")
        self.speed_cb.selection_clear()

    def _set_duct(self) -> None:
        mode = next((m for m in self.controls.get("duct_modes", []) if pc.airduct_name(m) == self.duct_var.get()), None)
        if mode is not None and mode != self.controls.get("duct_mode"):
            self._send(pc.airduct(mode), hold="duct")
        self.duct_cb.selection_clear()

    def _set_sound(self) -> None:
        if not self._send(pc.prompt_sound(self.sound_var.get()), hold="sound"):
            self._refresh_controls()

    def _set_temp(self, key) -> None:
        if not self._controls_live():
            return
        c = self.controls
        var = self.temp_entries[key][0]
        limit = c["limits"]["nozzle" if key in ("Main", "Aux") else key]
        try:
            temp = int(var.get().strip())
        except ValueError:
            self._note("Enter a whole number of °C.", error=True)
            return
        if not 0 <= temp <= limit:
            self._note(f"Out of range: 0 to {limit} °C.", error=True)
            return
        if key == "bed":
            msg = pc.bed_temp(temp, c["bed_mqtt"])
        elif key == "chamber":
            # same rules as Bambu Studio's Device tab (StatusPanel::on_set_chamber_temp)
            if c["printing"] and c["duct_mode"] == 0:
                self._note("The chamber temperature cannot be changed in cooling mode while printing.", error=True)
                return
            if c["duct_mode"] != 1 and temp >= pc.CHAMBER_HEATING_FROM and not messagebox.askokcancel(
                    APP_TITLE, f"From {pc.CHAMBER_HEATING_FROM} °C the printer switches the air duct to heating "
                               "mode by itself. Continue?"):
                return
            msg = pc.chamber_temp(temp)
        else:
            ids = c["extruder_ids"]
            ext = None if not ids else next((i for i in ids if HOTEND_NAMES.get(i) == key), None)
            if ids and ext is None:
                self._note(f"The printer does not report a {key} hotend.", error=True)
                return
            msg = pc.nozzle_temp(temp, ext)
        if self._send(msg):
            var.set("")

    def _value_row(self, box, key, label, colour, row, col):
        sw = tk.Canvas(box, width=12, height=12, highlightthickness=0)
        sw.create_rectangle(1, 1, 11, 11, fill=colour, outline="")
        sw.grid(row=row, column=col, padx=(8, 4), pady=2)
        ttk.Label(box, text=label).grid(row=row, column=col + 1, sticky="w")
        v = ttk.Label(box, text="—", font=self.f_value, width=12)
        v.grid(row=row, column=col + 2, sticky="w", padx=(6, 8))
        self.value_labels[key] = v

    def _set_status(self, kind, text):
        self._status = (kind, text)
        self.status_dot.delete("all")
        self.status_dot.create_oval(1, 1, 11, 11, fill=STATUS[kind], outline="")
        self.status_lbl.config(text=tr(text))

    def _set_alert_dot(self, kind):
        self.alert_dot.delete("all")
        self.alert_dot.create_oval(1, 1, 11, 11, fill=STATUS[kind] if kind else STATUS["ok"], outline="")

    def _log(self, text, tag=None):
        self.events.config(state="normal")
        self.events.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n", (tag,) if tag else ())
        self.events.see("end")
        self.events.config(state="disabled")

    # ---- connection
    def _change_language(self, event=None):
        code = next((key for key, value in self.languages.items() if value == self.language.get()), "en")
        selection = self.query_choice.current()
        set_language(code)
        for apply in self._text_bindings:
            apply()
        self.query_choice.configure(values=[tr(q.label) for q in QUERIES.values()])
        self.query_choice.current(max(0, selection))
        self.connect_btn.configure(text=tr("Disconnect" if self.link else "Connect"))
        self.raw_btn.configure(text=tr("Stop recording" if self.raw_fh else "Record raw…"))
        self.freeze_btn.configure(text=tr("Follow live" if self.frozen_at else "Freeze view"))
        self._set_status(*self._status)
        self.hms.load(self.serial.get().strip(), lang=code, model=self.model.get().strip() or None)
        for iid in self.al_tree.get_children():
            values = list(self.al_tree.item(iid, "values"))
            raw_code = values[2].replace("_", "")
            if values[1] == "Error":
                values[3] = self.hms.error_text(raw_code) or values[3]
            elif values[1] == "HMS":
                values[3] = self.hms.hms_text(raw_code) or values[3]
            self.al_tree.item(iid, values=values)
        self._refresh_controls()
        self._update_values(decode(self.state.get("print") or {}), self.extras)
        self._update_inspector(force=True)
        self.redraw()
        self._save_settings()

    def _browse_certificate(self):
        path = filedialog.askopenfilename(filetypes=[("PEM certificate", "*.pem"), ("All files", "*")])
        if path:
            self.cert_file.set(path)

    def discover(self):
        if not self.link:
            DiscoveryDialog(self.root, self._select_printer)

    def _select_printer(self, candidate, cert_file):
        self.ip.set(str(candidate.ip))
        self.serial.set(candidate.serial or (candidate.certificate.common_name if candidate.certificate else "") or "")
        model = candidate.model or ""
        self.model.set(lan.SSDP_MODEL_ALIASES.get(model.upper(), model))
        trusted = (self.settings.get("trusted_printers") or {}).get(str(candidate.ip).casefold(), {})
        # Restore trust only for the same host and serial, never another device.
        saved = trusted.get("cert_file", "") if trusted.get("serial") == self.serial.get() else ""
        self.cert_file.set(cert_file or saved)

    def _connection_editable(self, editable):
        for widget in self.connection_widgets:
            widget.configure(state="normal" if editable else "disabled")

    def _reset_session(self):
        """Keep histories for one printer only, without stale alerts or switch timing."""
        self.state, self.extras = {}, {}
        self.prev, self.prev_x = None, {}
        self.timer = SwitchTimer()
        self.last_msg, self.msg_times = 0.0, []
        self.alerts.clear()
        self.alert_links.clear()
        self.sent.clear()
        self.inspector = ReportInspector()
        self.inspector_revision = -1
        self.inspector_rows.clear()
        self.pending_query = None
        self.hold.clear()
        self.ams_sig = self.sensor_sig = None
        self.ctl_on.set(False)
        self.clear()
        for iid in self.al_tree.get_children():
            self.al_tree.delete(iid)
        self._alerts({})
        self._update_values(decode({}), {})
        self._update_ams({})
        self._update_sensors()
        for key, widget in self.value_labels.items():
            widget.configure(text="—")

    def toggle_connect(self):
        if self.link:
            self.link.stop()
            self.link = None
            self.q = queue.Queue()  # isolate late messages from the stopped worker
            self.pending_query = None
            self.ctl_on.set(False)
            self._invalidate_telemetry()
            self.connect_btn.config(text=tr("Connect"))
            self._connection_editable(True)
            self._set_status("off", "Disconnected")
            self._refresh_controls()
            return
        ip, code = self.ip.get().strip(), self.code.get().strip()
        if ip.startswith("http"):
            ip = ip.split("//", 1)[-1].strip("/")
            self.ip.set(ip)
        if not ip or not code:
            messagebox.showwarning(APP_TITLE, "Enter the printer's IP address and LAN access code.")
            return
        serial = self.serial.get().strip()
        try:
            if serial:
                lan.validate_serial(serial)
        except lan.BambuMonitorError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        self._reset_session()
        self.hms.load(serial, lang=get_language(), model=self.model.get().strip() or None)
        self.q = queue.Queue()
        self.link = PrinterLink(ip, code, serial, self.q, passive=self.passive.get(),
                                cert_file=self.cert_file.get().strip() or None)
        self.link.start()
        self.connect_btn.config(text=tr("Disconnect"))
        self._connection_editable(False)
        self._refresh_controls()
        self._save_settings()

    def _drain(self):
        # Bound a burst so drawing and user input still get a turn in Tk's loop.
        deadline = time.monotonic() + 0.02
        try:
            for _ in range(100):
                if time.monotonic() >= deadline:
                    break
                kind, data = self.q.get_nowait()
                if kind == "msg":
                    try:
                        self._receive_message(data)
                    except (ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError, RecursionError) as exc:
                        LOGGER.warning("Ignored invalid printer report: %s", exc)
                elif kind == "serial":
                    if not self.serial.get().strip():
                        self.serial.set(data)
                    self.hms.load(data, lang=get_language(), model=self.model.get().strip() or None)
                elif kind == "status":
                    if data[0] != "ok":
                        self._invalidate_telemetry()
                    self._set_status(*data)
                    self._refresh_controls()
                elif kind in ("auth", "fatal"):
                    self._invalidate_telemetry()
                    self._set_status("error", data)
                    self.link = None
                    self.connect_btn.config(text=tr("Connect"))
                    self._connection_editable(True)
                    self._refresh_controls()
        except queue.Empty:
            pass
        finally:
            if not self._closing:
                self.root.after(10 if not self.q.empty() else 200, self._drain)

    def _receive_message(self, data):
        now, elapsed = time.time(), time.monotonic()
        if self.last_msg and elapsed - self.last_msg > TELEMETRY_TIMEOUT:
            self._invalidate_telemetry(clear_age=False)
        self._record_raw({"t": round(now, 3), "monotonic": elapsed, "msg": data})
        validate_message(data)
        if self._is_reply(data):
            self.inspector.observe(data, now)
            self._query_response(data)
            return
        if not data.get("print"):
            self.inspector.observe(data, now)
            return
        candidate = merged_copy(self.state, data)
        p = candidate["print"]
        # Decode before commit: malformed deltas cannot corrupt the last good state.
        decode(p)
        extras = decode_extras(p)
        pc.decode_controls(p)
        lan.sensor_summary(candidate)
        self.state, self.extras = candidate, extras
        self.inspector.observe(data, now)
        self._query_response(data)
        self.last_msg = elapsed
        self.msg_times = (self.msg_times + [elapsed])[-60:]
        self._on_message(now, monotonic=elapsed)

    def _invalidate_telemetry(self, clear_age=True):
        had_data = bool(self.state)
        self.state, self.extras = {}, {}
        self.prev, self.prev_x = None, {}
        self.timer.interrupt()
        self.msg_times = []
        self.sent.clear()
        self.pending_query = None
        self.hold.clear()
        if clear_age:
            self.last_msg = 0.0
        if had_data:
            self._record_raw({"t": time.time(), "event": "telemetry_gap"})
            # Existing alert rows remain history; a disconnect is not an all-clear.
            self.alerts.clear()
            self._update_values(decode({}), {})
            for label in self.value_labels.values():
                label.configure(text="—")
            self._update_ams({})
            self._update_sensors()
            self._set_alert_dot("off")
            self.alert_lbl.configure(text=tr("Alerts unavailable until a fresh report arrives."))

    def _update_data_age(self):
        if not self.last_msg:
            text, colour = "No telemetry received yet.", MUTED
        else:
            age = max(0, time.monotonic() - self.last_msg)
            stale = age > TELEMETRY_TIMEOUT
            text = tr("Telemetry stale: last report {seconds:.0f} s ago." if stale
                      else "Last telemetry {seconds:.0f} s ago.", seconds=age)
            colour = STATUS["error"] if stale else INK2
            if stale and self.state:
                self._invalidate_telemetry(clear_age=False)
        self.data_age_lbl.configure(text=tr(text), foreground=colour)

    def _on_message(self, now, *, monotonic=None):
        """Per report: decode extras and time hotend switches with message precision."""
        p = self.state.get("print")
        if not p:
            return
        rec = self.timer.feed(now, self.extras, layer=p.get("layer_num"),
                              names=lambda i: HOTEND_NAMES.get(i, i), monotonic=monotonic)
        if rec:
            gap = rec.get("gap")
            dur = "not timed" if rec["duration"] is None else f"{rec['duration']:.1f}"
            steps = ", ".join(rec["steps"]) or "—"
            self.sw_tree.insert("", "end", values=(
                datetime.fromtimestamp(rec["start"]).strftime("%H:%M:%S"), f"{rec['from']} → {rec['to']}",
                dur, "" if gap is None else f"{gap:.1f}", rec["layer"] if rec["layer"] is not None else "", steps))
            self.sw_tree.see(self.sw_tree.get_children()[-1])
            self._log(f"hotend switch {rec['from']} → {rec['to']}: "
                      + ("finished between two reports (not timed)" if rec["duration"] is None
                         else f"{rec['duration']:.1f} s") + f"  [{steps}]", "switch")

    # ---- sampling
    def _tick(self):
        if self._closing:
            return
        self.root.after(1000, self._tick)
        self._update_data_age()
        self._refresh_controls()
        self._update_inspector()
        if self.pending_query and time.monotonic() - self.pending_query[2] > 15:
            self.query_status.configure(text=tr("No matching response within 15 s. The firmware may not support this query; missing data is not zero."))
            self.pending_query = None
        if self.raw_fh:
            try:
                self.raw_fh.flush()
                self.raw_lbl.config(text=tr("Recording: {count} messages", count=self.raw_count))
            except (OSError, ValueError) as exc:
                self._stop_raw(error=exc)
        p = self.state.get("print")
        if not self.link or not self.link.online or not p or not self.last_msg \
                or time.monotonic() - self.last_msg > TELEMETRY_TIMEOUT:
            return
        d = decode(p)
        x = self.extras or decode_extras(p)
        now = time.time()
        sample = {s[0]: d.get(s[0]) for s in TEMP_SERIES}
        sample.update({s[3]: (d.get(s[3]) or None) for s in TEMP_SERIES})  # target 0 = heater off
        for name, val in d["fans"].items():
            sample["fan_" + name] = val
        n = len(self.t)
        for k, v in sample.items():
            if v is not None and k not in self.vals:
                self.vals[k] = RingSeries(HISTORY_LIMIT, repeat(None, n))
        for k, series in self.vals.items():
            series.append(sample.get(k))
        self.t.append(now)
        self.active.append(d.get("active") if isinstance(d.get("active"), str) else None)
        self.stage_hist.append(x.get("stage"))
        self._events(d, x)
        self._alerts(x)
        self._update_values(d, x)
        self._update_ams(x)
        self._update_sensors()
        self.redraw()

    def _events(self, d, x):
        prev, self.prev = self.prev, d
        prev_x, self.prev_x = self.prev_x, x
        if prev is None:
            return
        msgs, switch = [], False
        quiet = set() if self.log_all_fans.get() else QUIET_FANS
        for k in sorted(set(d["fans"]) | set(prev["fans"])):
            if k not in quiet and d["fans"].get(k) != prev["fans"].get(k):
                msgs.append(f"{k} fan {prev['fans'].get(k)}% → {d['fans'].get(k)}%")
        if d.get("active") != prev.get("active") and isinstance(d.get("active"), str) \
                and isinstance(prev.get("active"), str):
            msgs.append(f"hotend {prev.get('active')} → {d.get('active')}")
            self.switches.append((time.time(), str(d.get("active"))))
            switch = True
        if d.get("state") != prev.get("state"):
            msgs.append(f"state {prev.get('state')} → {d.get('state')}")
        if x.get("stage") != prev_x.get("stage"):
            msgs.append(f"stage {prev_x.get('stage') or '-'} → {x.get('stage') or '-'}")
        if x.get("speed") != prev_x.get("speed") and prev_x:
            msgs.append(f"speed {prev_x.get('speed')} → {x.get('speed')}")
        if d.get("duct_mode") != prev.get("duct_mode"):
            msgs.append(f"air duct {prev.get('duct_mode')} → {d.get('duct_mode')}")
        if msgs:
            self._log(f"L{d.get('layer')}  active={d.get('active', '-')}  " + "; ".join(msgs),
                      "switch" if switch else None)

    def _alerts(self, x):
        current = {}
        for code in x.get("hms") or []:
            formatted = "HMS_" + "_".join(code[i:i + 4] for i in range(0, len(code), 4))
            current[code] = "Status" if formatted in lan.KNOWN_NORMAL_HMS_NOTES else "HMS"
        if x.get("print_error"):
            current[x["print_error"]] = "Error"
        for code, kind in current.items():
            if code not in self.alerts:
                text = self.hms.error_text(code) if kind == "Error" else self.hms.hms_text(code)
                if kind == "Status":
                    formatted = "HMS_" + "_".join(code[i:i + 4] for i in range(0, len(code), 4))
                    text = tr(lan.KNOWN_NORMAL_HMS_NOTES[formatted])
                shown = "_".join(code[i:i + 4] for i in range(0, len(code), 4))
                iid = self.al_tree.insert("", "end", values=(f"{datetime.now():%H:%M:%S}", kind, shown,
                                                             text or "(unknown code; check the printer display)"))
                if kind != "Error":
                    self.alert_links[iid] = self.hms.wiki_url(code)
                self.alerts[code] = (kind, iid, text)
                self._log(f"{kind} {shown}: {text or 'unknown code'}", "alert" if kind != "Status" else None)
        for code in [c for c in self.alerts if c not in current]:
            kind, iid, text = self.alerts.pop(code)
            self.al_tree.item(iid, tags=("cleared",))
            vals = list(self.al_tree.item(iid, "values"))
            vals[0] = f"{vals[0]}–{datetime.now():%H:%M:%S}"
            self.al_tree.item(iid, values=vals)
            self._log(f"{kind} {code} cleared")
        faults = [v for v in self.alerts.values() if v[0] != "Status"]
        if faults:
            first = faults[0]
            n = len(faults)
            self._set_alert_dot("error" if any(v[0] == "Error" for v in faults) else "busy")
            self.alert_lbl.config(text=tr("Alerts: {count}. {message}", count=n, message=first[2] or tr("See the Alerts tab"))[:160],
                                  foreground=INK)
        else:
            self._set_alert_dot(None)
            self.alert_lbl.config(text=tr("No alerts"), foreground=INK2)

    def _open_alert_link(self, event):
        iid = self.al_tree.identify_row(event.y)
        url = self.alert_links.get(iid)
        if url and url.startswith("https://wiki.bambulab.com/"):
            webbrowser.open(url)

    def _update_sensors(self):
        summary = lan.sensor_summary(self.state)
        hotends = self.extras.get("hotends") or {}
        signature = (summary, json.dumps(hotends, sort_keys=True), get_language())
        if signature == self.sensor_sig:
            return
        self.sensor_sig = signature
        self.sensor_tree.delete(*self.sensor_tree.get_children())
        for description in summary.split(" | ") if summary else ["No sensor data received yet."]:
            self.sensor_tree.insert("", "end", values=(tr(description),))
        for name, hotend in hotends.items():
            if "has_filament" in hotend:
                description = tr("{hotend}: filament {filament}, buffer {buffer}, nozzle {nozzle}",
                    hotend=tr(name), filament=tr("present" if hotend["has_filament"] else "absent"),
                    buffer=tr("present" if hotend["buffer_filament"] else "absent"),
                    nozzle=tr("present" if hotend["has_nozzle"] else "absent"))
                self.sensor_tree.insert("", "end", values=(description,))

    def _send_query(self):
        if not self.link or not self.link.online or self.link.passive or self.pending_query:
            return
        name = self.query_ids[self.query_choice.current()]
        query = QUERIES[name]
        if query.experimental and not self.experimental_queries.get():
            self.query_status.configure(text=tr("Enable experimental calibration reads first. Support on X2D is unverified."))
            return
        try:
            message = build_query(name, self.query_nozzle.get(), int(self.query_extruder.get()))
        except ValueError as exc:
            self.query_status.configure(text=str(exc))
            return
        if self.link.publish(message):
            sequence = message[query.section]["sequence_id"]
            self.pending_query = (name, sequence, time.monotonic())
            self.query_status.configure(text=tr("Read query sent: {command}. Waiting for a response…", command=query.command))
            self._log(tr("Read query sent: {command}", command=query.command), "sent")
            self._refresh_controls()

    def _query_response(self, data):
        if self.pending_query is None:
            return
        name, sequence, _ = self.pending_query
        query = QUERIES[name]
        for section, body in data.items():
            if not isinstance(body, dict):
                continue
            if name == "status" and section == "print" and body.get("command") == "push_status":
                self.query_status.configure(text=tr("A status report arrived after the request (pushall has no guaranteed response correlation)."))
                self.pending_query = None
                return
            if section == query.section and body.get("command") == query.command and str(body.get("sequence_id")) == sequence:
                result = body.get("result", "response received")
                self.query_status.configure(text=tr("Response to {command}: {result}. Raw fields are in the Inspector.", command=query.command, result=result))
                self.pending_query = None
                return

    def _update_inspector(self, force=False):
        if not hasattr(self, "field_tree"):
            return
        if not force and self.inspector_revision == self.inspector.revision:
            return
        if not force and self.notebook.select() != str(self.field_tree.master):
            return
        self.inspector_revision = self.inspector.revision
        search = self.field_filter.get().casefold().strip()
        fields = [(path, field) for path, field in sorted(self.inspector.fields.items())
                  if not search or search in path.casefold() or search in str(field.value).casefold()]
        shown = fields[:2000]
        paths = {path for path, _ in shown}
        old_order = self.field_tree.get_children()
        for path in old_order:
            if path not in paths:
                self.field_tree.delete(path)
                self.inspector_rows.pop(path, None)
        for path, field in shown:
            value = json.dumps(field.value, ensure_ascii=False)
            previous = json.dumps(field.previous, ensure_ascii=False) if field.changes else ""
            values = (path, value[:500], previous[:200], field.changes,
                datetime.fromtimestamp(field.seen).strftime("%H:%M:%S"), tr(self.inspector.hint(path)))
            if not self.field_tree.exists(path):
                self.field_tree.insert("", "end", iid=path, values=values)
            elif self.inspector_rows.get(path) != values:
                self.field_tree.item(path, values=values)
            self.inspector_rows[path] = values
        if old_order != tuple(path for path, _ in shown):
            for index, (path, _) in enumerate(shown):
                self.field_tree.move(path, "", index)
        self.field_count.configure(text=tr("{shown} / {total} fields", shown=min(2000, len(fields)), total=len(fields)))

    def _export_inspector(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")],
                                           initialfile=f"printer-observed-{datetime.now():%Y%m%d-%H%M}.json")
        if path:
            try:
                self.inspector.export(path)
            except (OSError, ValueError) as exc:
                messagebox.showerror(tr("Export failed"), str(exc))

    def _update_values(self, d, x):
        st = d.get("state", "?")
        self.state_lbl.config(text=tr(STATE_TEXT.get(st, st)))
        pct = d.get("percent")
        self.progress["value"] = pct or 0
        parts = []
        if pct is not None:
            parts.append(f"{pct}%")
        if d.get("layers"):
            parts.append(tr("layer {current}/{total}", current=d.get('layer'), total=d.get('layers')))
        if d.get("remain_min") not in (None, 0):
            m = int(d["remain_min"])
            parts.append(tr("{hours} h {minutes:02d} min left", hours=m // 60, minutes=m % 60)
                         if m >= 60 else tr("{minutes} min left", minutes=m))
        self.progress_lbl.config(text="  ·  ".join(parts))
        hot = d.get("active")
        txt = tr("Active hotend: {hotend}", hotend=tr(hot)) if isinstance(hot, str) else ""
        if d.get("switching"):
            txt += f" → {d.get('target_hotend')} ({SWITCH_STATES.get(d.get('switching'), 'busy')})"
        self.hotend_lbl.config(text=txt)
        self.stage_lbl.config(text=tr("Stage: {stage}", stage=tr(x["stage"])) if x.get("stage") else "")
        sp = ""
        if x.get("speed"):
            sp = tr("Speed: {speed}", speed=tr(x["speed"])) + (f" ({x['speed_pct']}%)" if x.get("speed_pct") else "")
        if len(self.msg_times) > 5:
            span = self.msg_times[-1] - self.msg_times[0]
            sp += "   ·   " + tr("reports every {seconds:.1f} s", seconds=span / (len(self.msg_times) - 1))
        self.speed_lbl.config(text=sp)
        self.file_lbl.config(text=d.get("file") or "")
        for key, label, colour, tgt in TEMP_SERIES:
            v, t = d.get(key), d.get(tgt)
            self.value_labels[key].config(text="—" if v is None else (f"{v} / {t} °C" if t else f"{v} °C"))
        for n, name in enumerate([f for f in FAN_ORDER if f in d["fans"]]):
            key = "fan_" + name
            if key not in self.value_labels:
                colour = SLOTS[FAN_ORDER.index(name)]
                self._value_row(self.fan_box, key, tr(name), colour, n % 4, (n // 4) * 3)
            self.value_labels[key].config(text=f"{d['fans'][name]} %")

        hot = x.get("hotends") or {}
        for name, (nz, sw, fil, step) in self.hot_rows.items():
            h = hot.get(name) or {}
            nz.config(text=(h.get("nozzle") or "—") + (f"  (wear {h['wear']})" if h.get("wear") else ""))
            f = h.get("filament") or {}
            draw_swatch(sw, f, 14)
            ftxt = " · ".join(p for p in (f.get("name"), h.get("loaded"),
                                          f"{f['remain']}%" if isinstance(f.get("remain"), int) and f["remain"] >= 0 else "") if p)
            if ftxt and h.get("in_extruder") is False:
                ftxt += "  (this print; not in the extruder now)"
            fil.config(text=ftxt or (tr("nothing loaded") if h else ""), foreground=INK2 if h.get("in_extruder") is False else INK)
            st_txt = h.get("step") if h.get("step") not in (None, "idle") else ""
            if name == hot_name_active(d):
                st_txt = (tr("active") + (f", {tr(st_txt)}" if st_txt else ""))
            step.config(text=st_txt)
        stats = self.timer.stats()
        if not stats:
            self.switch_lbl.configure(text=tr("Hotend switches: none measured yet"))
        if stats:
            txt = f"Hotend switches: {stats['count']}"
            if stats.get("timed"):
                txt += (f"   ·   last {stats['last']:.1f} s" if stats.get("last") is not None else "") + \
                       f"   ·   average {stats['avg']:.1f} s (min {stats['min']:.1f}, max {stats['max']:.1f})" + \
                       f"   ·   total {stats['total'] / 60:.1f} min"
            if stats.get("avg_gap") is not None:
                txt += f"   ·   one every {stats['avg_gap'] + (stats.get('avg') or 0):.0f} s"
            if stats["timed"] < stats["count"]:
                txt += f"   ·   {stats['count'] - stats['timed']} not timed"
            self.switch_lbl.config(text=txt, foreground=INK)

    def _update_ams(self, x):
        units = x.get("ams") or []
        loaded = {name: h.get("loaded_key") for name, h in (x.get("hotends") or {}).items()
                  if h.get("in_extruder") is not False}
        sig = json.dumps([units, loaded], sort_keys=True, default=str)
        if sig == self.ams_sig:
            return
        self.ams_sig = sig
        for w in self.ams_frame.winfo_children():
            w.destroy()
        if not units:
            ttk.Label(self.ams_frame, text="No AMS reported.", foreground=INK2).pack(anchor="w", padx=8, pady=8)
            return
        for u in units:
            row = ttk.Frame(self.ams_frame)
            row.pack(fill="x", padx=8, pady=(6, 0))
            info = [u.get("name", "AMS"), u.get("type") or ""]
            if u.get("hotend"):
                info.append(f"feeds {u['hotend']}")
            if u.get("temp") is not None:
                info.append(f"{u['temp']:g} °C")
            if u.get("humidity_pct") is not None:
                info.append(f"{u['humidity_pct']} % RH")
            elif u.get("humidity_level") is not None:
                info.append(f"humidity level {u['humidity_level']}/5 (higher = drier)")
            if u.get("dry") and u["dry"] != "off":
                info.append(f"drying: {u['dry']}" + (f", {u['dry_min']} min left" if u.get("dry_min") else ""))
            ttk.Label(row, text="  ·  ".join(i for i in info if i), font=self.f_bold).pack(side="left")
            slots = ttk.Frame(self.ams_frame)
            slots.pack(fill="x", padx=24, pady=(2, 2))
            for s in u.get("slots") or []:
                cell = ttk.Frame(slots)
                cell.pack(side="left", padx=(0, 18))
                c = tk.Canvas(cell, width=16, height=16, highlightthickness=0)
                draw_swatch(c, s, 16)
                c.pack(side="left")
                txt = f"{s['slot'] + 1}  {s.get('name') or 'empty'}"
                if isinstance(s.get("remain"), int) and s["remain"] >= 0:
                    txt += f"  {s['remain']}%"
                for name, key in loaded.items():
                    if key and tuple(key) == (u["id"], s["slot"]):
                        txt += f"  ◀ {name}"
                if s.get("job"):
                    txt += "  · print filament " + ", ".join(str(j) for j in s["job"])
                ttk.Label(cell, text=txt).pack(side="left", padx=(4, 0))

    # ---- view
    def chart_data(self):
        return self.frozen_data if self.frozen_data is not None else self

    def view_range(self):
        data = self.chart_data()
        end = self.frozen_at if self.frozen_at is not None else (data.t[-1] if data.t else time.time())
        span = dict(WINDOWS).get(self.window.get(), 600)
        if span is None:
            start = data.t[0] if data.t else end - 600
            span = max(end - start, 60)
        return end - span, end

    def redraw(self):
        self.temp_chart.draw()
        self.fan_chart.draw()
        self.strip.draw()

    def set_hover(self, t):
        self.hover_t = t
        self.temp_chart.draw_hover()
        self.fan_chart.draw_hover()

    def toggle_freeze(self):
        if self.frozen_data is None:
            self.frozen_at = self.t[-1] if self.t else time.time()
            self.frozen_data = SimpleNamespace(t=list(self.t), active=list(self.active),
                vals={key: list(values) for key, values in self.vals.items()}, switches=list(self.switches))
        else:
            self.frozen_at = self.frozen_data = None
        self.freeze_btn.config(text=tr("Follow live" if self.frozen_data is not None else "Freeze view"))
        self.redraw()

    def clear(self):
        self.t, self.active, self.stage_hist = (RingSeries(HISTORY_LIMIT) for _ in range(3))
        self.vals, self.switches = {}, []
        self.frozen_at = self.frozen_data = self.hover_t = None
        self.freeze_btn.configure(text=tr("Freeze view"))
        self.timer.records = []
        self.timer.interrupt()
        for iid in self.sw_tree.get_children():
            self.sw_tree.delete(iid)
        self.switch_lbl.config(text=tr("Hotend switches: none measured yet"), foreground=INK2)
        self.redraw()

    def _stop_raw(self, error=None):
        handle, self.raw_fh = self.raw_fh, None
        if handle:
            try:
                handle.close()
            except (OSError, ValueError) as exc:
                error = error or exc
        self.raw_btn.config(text=tr("Record raw…"))
        text = tr("Recording stopped: {error}", error=error) if error else tr("Saved {count} messages", count=self.raw_count)
        self.raw_lbl.config(text=text)
        if error:
            self._log(text, "alert")

    def _record_raw(self, record):
        if not self.raw_fh:
            return
        try:
            self.raw_fh.write(json.dumps(record) + "\n")
            self.raw_count += int("msg" in record)
        except (OSError, ValueError) as exc:
            self._stop_raw(error=exc)

    def toggle_raw(self):
        if self.raw_fh:
            self._stop_raw()
            return
        path = filedialog.asksaveasfilename(defaultextension=".jsonl", filetypes=[("JSON lines", "*.jsonl")],
                                            initialfile=f"printer-raw-{datetime.now():%Y%m%d-%H%M}.jsonl")
        if not path:
            return
        try:
            self.raw_fh = open(path, "a", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(tr("Recording failed"), str(exc))
            return
        self.raw_count = 0
        self.raw_btn.config(text=tr("Stop recording"))
        if self.state:  # start with the full merged state so the file is usable on its own
            self._record_raw({"t": round(time.time(), 3), "monotonic": time.monotonic(), "merged_state": self.state})

    def save_csv(self):
        if not self.t:
            messagebox.showinfo(APP_TITLE, "No data recorded yet.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            initialfile=f"printer-{datetime.now():%Y%m%d-%H%M}.csv")
        if not path:
            return
        keys = [k for s in TEMP_SERIES for k in (s[0], s[3]) if k in self.vals]
        keys += [s[0] for s in FAN_SERIES if s[0] in self.vals]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "active_hotend", "stage"] + keys)
            for i, t in enumerate(self.t):
                w.writerow([datetime.fromtimestamp(t).isoformat(timespec="seconds"), self.active[i],
                            self.stage_hist[i]] + [self.vals[k][i] for k in keys])
        if self.timer.records:
            base, ext = os.path.splitext(path)
            with open(base + "-switches" + ext, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["start", "end", "from", "to", "duration_s", "layer", "steps"])
                for rec in self.timer.records:
                    w.writerow([datetime.fromtimestamp(rec["start"]).isoformat(timespec="milliseconds"),
                                datetime.fromtimestamp(rec["end"]).isoformat(timespec="milliseconds"),
                                rec["from"], rec["to"],
                                "" if rec["duration"] is None else f"{rec['duration']:.2f}",
                                rec["layer"], "; ".join(rec["steps"])])

    def close(self):
        self._closing = True
        self._save_settings()
        if self.raw_fh:
            self._stop_raw()
        if self.link:
            self.link.stop()
        self.root.destroy()


def draw_swatch(c, f, size):
    """Filament colour square; translucent filament (alpha 0) as a crossed clear square."""
    c.delete("all")
    if not f or not f.get("color"):
        if f and f.get("name"):
            c.create_rectangle(1, 1, size - 1, size - 1, fill=SURFACE, outline=AXIS)
        elif f is not None:
            c.create_rectangle(1, 1, size - 1, size - 1, fill=SURFACE, outline=AXIS, dash=(2, 2))
        return
    if f.get("clear"):
        c.create_rectangle(1, 1, size - 1, size - 1, fill=SURFACE, outline=INK2)
        c.create_line(2, size - 2, size - 2, 2, fill=INK2)
    else:
        c.create_rectangle(1, 1, size - 1, size - 1, fill=f["color"], outline=AXIS)


def hot_name_active(d):
    a = d.get("active")
    return a if isinstance(a, str) else None


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
