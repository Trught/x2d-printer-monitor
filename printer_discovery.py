"""Non-blocking LAN discovery and certificate selection for the Tk window."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import bambu_monitor as lan
from i18n import bind_static_text, tr


class DiscoveryDialog:
    def __init__(self, parent, on_select):
        self.on_select = on_select
        self.results = queue.Queue()
        self.candidates: list[lan.Candidate] = []
        self.saved_certs: dict[str, str] = {}
        self.window = tk.Toplevel(parent)
        self.window.title(tr("Discover Bambu printers on LAN"))
        self.window.geometry("920x440")
        self.window.minsize(760, 380)
        self.window.transient(parent)
        self.window.grab_set()
        self.status = tk.StringVar(
            value=tr("Discovering on the local subnet (at most /24)…")
        )
        ttk.Label(self.window, textvariable=self.status, wraplength=860).pack(
            anchor="w", padx=12, pady=10
        )
        columns = ("ip", "model", "name", "serial", "confidence")
        self.tree = ttk.Treeview(
            self.window, columns=columns, show="headings", height=8
        )
        for column, width in (
            ("ip", 125),
            ("model", 75),
            ("name", 175),
            ("serial", 175),
            ("confidence", 100),
        ):
            self.tree.heading(column, text=column.capitalize())
            self.tree.column(column, width=width, stretch=True)
        scrollbar = ttk.Scrollbar(self.window, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=12)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Double-1>", lambda event: self._use())
        self.fingerprint = tk.StringVar(
            value=tr("Select a printer to inspect its certificate.")
        )
        ttk.Label(self.window, textvariable=self.fingerprint, wraplength=870).pack(
            anchor="w", padx=12, pady=8
        )
        ttk.Label(
            self.window,
            text="Verify the printer/IP and SHA-256 fingerprint through a trusted path before saving trust.",
            wraplength=870,
        ).pack(anchor="w", padx=12)
        buttons = ttk.Frame(self.window)
        buttons.pack(fill="x", padx=12, pady=12)
        self.use_btn = ttk.Button(
            buttons, text="Use selected printer", command=self._use, state="disabled"
        )
        self.use_btn.pack(side="left")
        self.save_btn = ttk.Button(
            buttons,
            text="Save certificate…",
            command=self._save_certificate,
            state="disabled",
        )
        self.save_btn.pack(side="left", padx=8)
        ttk.Button(buttons, text="Close", command=self.window.destroy).pack(
            side="right"
        )
        self._text_bindings = bind_static_text(self.window)
        threading.Thread(target=self._discover, daemon=True).start()
        self.window.after(100, self._poll)

    def _discover(self):
        try:
            context = lan.get_network_context()
            candidates = lan.discover_candidates(context)
            self.results.put(
                (
                    candidates,
                    f"{len(candidates)} candidate(s) found on {context.scan_network}.",
                )
            )
        except Exception as exc:
            self.results.put(([], f"Discovery failed: {exc}"))

    def _poll(self):
        try:
            self.candidates, message = self.results.get_nowait()
        except queue.Empty:
            self.window.after(100, self._poll)
            return
        self.status.set(
            message
            if self.candidates
            else message + " You can also enter an IP in the main window."
        )
        for index, candidate in enumerate(self.candidates):
            model = candidate.model or ""
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    str(candidate.ip),
                    lan.SSDP_MODEL_ALIASES.get(model.upper(), model),
                    candidate.name or candidate.hostname or "",
                    candidate.serial or "",
                    candidate.confidence,
                ),
            )
        if len(self.candidates) == 1:
            self.tree.selection_set("0")

    def _selected(self):
        selection = self.tree.selection()
        return self.candidates[int(selection[0])] if selection else None

    def _selection_changed(self, event=None):
        candidate = self._selected()
        if candidate is None:
            return
        certificate = candidate.certificate
        self.use_btn.configure(
            state="normal" if candidate.port_8883_open else "disabled"
        )
        self.save_btn.configure(
            state="normal" if certificate and certificate.pem else "disabled"
        )
        self.fingerprint.set(
            f"Certificate CN: {certificate.common_name or 'unknown'}\nSHA-256: {certificate.sha256 or 'unavailable'}"
            if certificate
            else "No TLS certificate available."
        )

    def _save_certificate(self):
        candidate = self._selected()
        if candidate is None:
            return
        destination = filedialog.asksaveasfilename(
            parent=self.window,
            defaultextension=".pem",
            filetypes=[("PEM certificate", "*.pem")],
            initialfile=f"printer-{candidate.ip}.pem",
            confirmoverwrite=False,
        )
        if not destination:
            return
        try:
            saved = lan.save_candidate_certificate(candidate, Path(destination))
        except (lan.BambuMonitorError, OSError, UnicodeError) as exc:
            messagebox.showerror("Certificate not saved", str(exc), parent=self.window)
            return
        self.saved_certs[str(candidate.ip)] = str(saved)
        self.status.set(
            f"Certificate saved. Use selected printer to enable this certificate trust: {saved}"
        )

    def _use(self):
        candidate = self._selected()
        if candidate is not None and candidate.port_8883_open:
            self.on_select(candidate, self.saved_certs.get(str(candidate.ip), ""))
            self.window.destroy()
