"""Backups & Maintenance page (v2.0).

One place for everything around data safety and disk hygiene:
- Backup now (quick / full) with live progress bar (issue 34)
- Backup list with restore (transactional + pre-restore snapshot) and delete
- Retention + schedule policy editor (issues 6 / 17)
- Disk usage breakdown + one-click Clean up (issues 29 / 30)

Every mutating action goes through the app's operation locks, so a
backup can never race an update or restore (issues 25/26).
"""
from __future__ import annotations

import time

import customtkinter as ctk
from tkinter import messagebox

from .. import backup as BK
from .. import maint as MT
from .. import theme as T
from ..models import Server
from .widgets import AccentButton, Card, DangerButton, GhostButton

KIND_ICON = {"backup": "ok", "restore": "warn", "cleanup": "ok"}


def _fmt_mb(n: float) -> str:
    if n >= 1024:
        return f"{n / 1024:.1f} GB"
    return f"{n:.0f} MB"


def _fmt_when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


class BackupsPage(ctk.CTkFrame):
    def __init__(self, master, app, server: Server | None):
        super().__init__(master, fg_color=T.BG, corner_radius=0)
        self.app = app
        self.server = server
        self._busy = False

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(24, 4))
        ctk.CTkLabel(head, text="Backups & Maintenance", font=T.font(19, "bold"),
                     text_color=T.TEXT).pack(side="left")
        self.state = ctk.CTkLabel(head, text="", font=T.font(11),
                                  text_color=T.TEXT_DIM)
        self.state.pack(side="left", padx=12)
        GhostButton(head, text="Refresh", width=90, height=28,
                    command=self.refresh).pack(side="right")

        # -- actions + progress -------------------------------------------------
        act = Card(self)
        act.pack(fill="x", padx=28, pady=(6, 4))
        row = ctk.CTkFrame(act, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(12, 4))
        AccentButton(row, text="Backup now", width=120, height=32,
                     command=self._backup_quick).pack(side="left", padx=4)
        GhostButton(row, text="Full backup", width=110, height=32,
                    command=self._backup_full).pack(side="left", padx=4)
        GhostButton(row, text="Clean up disk", width=120, height=32,
                    command=self._cleanup).pack(side="left", padx=4)
        ctk.CTkLabel(row, text="quick = worlds, plugins, mods, configs   |   "
                               "full = everything except logs",
                     font=T.font(10), text_color=T.TEXT_DIM).pack(
            side="left", padx=10)
        self.progress = ctk.CTkProgressBar(act, height=10,
                                           corner_radius=T.BUTTON_RADIUS)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=16, pady=(2, 2))
        self.pstate = ctk.CTkLabel(act, text="", font=T.font(11),
                                   text_color=T.TEXT_DIM, anchor="w")
        self.pstate.pack(fill="x", padx=16, pady=(0, 10))

        # -- body: list left, policy+disk right ---------------------------------
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=28, pady=(6, 20))
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        # backups list
        list_card = Card(body)
        list_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        lrow = ctk.CTkFrame(list_card, fg_color="transparent")
        lrow.pack(fill="x", padx=14, pady=(12, 0))
        ctk.CTkLabel(lrow, text="Archives", font=T.font(12, "bold"),
                     text_color=T.TEXT).pack(side="left")
        self.list_info = ctk.CTkLabel(lrow, text="", font=T.font(10),
                                      text_color=T.TEXT_DIM)
        self.list_info.pack(side="right")
        self.listbox = ctk.CTkScrollableFrame(list_card, fg_color="transparent")
        self.listbox.pack(fill="both", expand=True, padx=10, pady=(4, 12))

        # right column
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")

        pol = Card(right)
        pol.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(pol, text="Schedule & retention", font=T.font(12, "bold"),
                     text_color=T.TEXT).pack(anchor="w", padx=14, pady=(12, 4))
        grid = ctk.CTkFrame(pol, fg_color="transparent")
        grid.pack(fill="x", padx=14, pady=(0, 4))
        for i in range(2):
            grid.grid_columnconfigure(i, weight=1)
        self.e_backup_time = self._field(grid, "Daily backup (HH:MM)",
                                         server.backup_time if server else "", 0, 0)
        self.e_restart_time = self._field(grid, "Daily restart (HH:MM)",
                                          server.restart_time if server else "", 0, 1)
        self.e_keep = self._field(grid, "Keep newest N backups",
                                  str(server.backup_keep if server else 10), 1, 0)
        self.e_notify = self._field(grid, "Notify (yes/no)",
                                    "yes" if (server and server.notify) else "no", 1, 1)
        GhostButton(pol, text="Save policy", width=110, height=28,
                    command=self._save_policy).pack(anchor="e", padx=14, pady=(6, 12))

        self.disk_card = Card(right)
        self.disk_card.pack(fill="both", expand=True)
        ctk.CTkLabel(self.disk_card, text="Disk usage", font=T.font(12, "bold"),
                     text_color=T.TEXT).pack(anchor="w", padx=14, pady=(12, 4))
        self.disk_bar = ctk.CTkProgressBar(self.disk_card, height=10)
        self.disk_bar.pack(fill="x", padx=14, pady=2)
        self.disk_text = ctk.CTkLabel(self.disk_card, text="...",
                                      font=T.mono(11), text_color=T.TEXT_DIM,
                                      anchor="nw", justify="left")
        self.disk_text.pack(fill="both", expand=True, padx=14, pady=(4, 12))

    # ------------------------------------------------------------- helpers
    def _field(self, parent, label, value, r, c):
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid(row=r, column=c, sticky="ew", padx=4, pady=4)
        ctk.CTkLabel(box, text=label, font=T.font(10), text_color=T.TEXT_DIM,
                     anchor="w").pack(fill="x")
        entry = ctk.CTkEntry(box, height=30, font=T.mono(11),
                             fg_color=T.SURFACE2, border_color=T.BORDER,
                             corner_radius=T.BUTTON_RADIUS)
        entry.delete(0, "end")
        entry.insert(0, value)
        entry.pack(fill="x")
        return entry

    def on_show(self):
        self.refresh()

    def _set(self, text, color=T.TEXT_DIM):
        if self.winfo_exists():
            self.state.configure(text=text, text_color=color)

    def _guard(self) -> bool:
        if self.server is None:
            return True
        if self._busy:
            self.app.notify("Another backup/maintenance action is running...",
                            ok=False)
            return True
        if not self.app.allowed(self.server, "files"):
            self.app.notify("File management is not allowed in restricted "
                            "mode.", ok=False)
            return True
        return False

    # --------------------------------------------------------------- refresh
    def refresh(self):
        if self.server is None or not self.server.mc_dir:
            self._render_list([], 0)
            return
        srv = self.server
        self._set("loading...")
        self.app.runner.run(
            lambda: (BK.list_backups(self.app.ssh, srv),
                     MT.usage(self.app.ssh, srv)),
            on_success=lambda r: self._render(r[0], r[1]),
            on_error=lambda e: self._set(str(e)[:90], T.RED),
        )

    def _render(self, items, usage):
        if not self.winfo_exists():
            return
        self._render_list(items, len(items))
        total = usage.get("total") or 1
        free = usage.get("free") or 0
        self.disk_bar.set(max(0.0, min(1.0, (total - free) / total)))
        parts = usage.get("parts_mb") or {}
        lines = [
            f"free   {free / (1 << 30):.1f} GB of {total / (1 << 30):.1f} GB",
        ]
        for name in ("world", "world_nether", "world_the_end", "plugins",
                     "mods", "libraries", "logs", "backups"):
            if parts.get(name):
                lines.append(f"{name:<14} {_fmt_mb(parts[name])}")
        self.disk_text.configure(text="\n".join(lines))
        self._set(f"{len(items)} backup(s)", T.ACCENT_SOFT)

    def _render_list(self, items, count):
        for w in self.listbox.winfo_children():
            w.destroy()
        self.list_info.configure(text=f"{count} archive(s)")
        if not items:
            ctk.CTkLabel(self.listbox,
                         text="no backups yet - create the first one above",
                         font=T.font(11), text_color=T.TEXT_DIM).pack(pady=20)
            return
        for it in items:
            row = ctk.CTkFrame(self.listbox, fg_color=T.SURFACE2,
                               corner_radius=T.BUTTON_RADIUS)
            row.pack(fill="x", pady=3, ipady=4)
            # action buttons packed FIRST (side=right) so long archive
            # names can never push them out of the row (v1.4 lesson)
            GhostButton(row, text="Restore", width=76, height=26,
                        command=lambda a=it: self._restore(a)).pack(
                side="right", padx=(4, 8))
            DangerButton(row, text="Delete", width=70, height=26,
                         command=lambda a=it: self._delete(a)).pack(
                side="right", padx=2)
            meta = ctk.CTkFrame(row, fg_color="transparent")
            meta.pack(side="left", fill="both", expand=True, padx=8)
            ctk.CTkLabel(meta, text=it["name"], font=T.mono(11),
                         text_color=T.TEXT, anchor="w", wraplength=330,
                         justify="left").pack(fill="x")
            ctk.CTkLabel(meta,
                         text=f"{_fmt_when(it['mtime'])}   ·   "
                              f"{_fmt_mb(it['size'] / (1 << 20))}",
                         font=T.font(10), text_color=T.TEXT_DIM,
                         anchor="w").pack(fill="x")

    # --------------------------------------------------------------- actions
    def _progress_cb(self):
        def cb(frac, msg):
            def apply():
                if self.winfo_exists():
                    self.progress.set(max(0.0, min(1.0, frac)))
                    self.pstate.configure(text=msg or "")
            self.app.runner.emit(apply)
        return cb

    def _log_cb(self):
        def log(line):
            self.app.runner.emit(lambda l=line: (
                self.winfo_exists() and self.pstate.configure(text=l[:120])))
        return log

    def _backup(self, profile: str):
        if self._guard() or self.server is None:
            return
        srv = self.server
        self._busy = True
        self._set("backing up...", T.AMBER)
        from ..oplock import OperationConflict

        def work():
            with self.app.locks.guard(srv.id, "backup"):
                return BK.create_backup(self.app.ssh, srv,
                                        log=self._log_cb(),
                                        progress=self._progress_cb(),
                                        profile=profile)

        def ok(info):
            self._busy = False
            self.progress.set(0)
            self.app.notify_event("backup", srv,
                                  f"{srv.name}: backup "
                                  f"{info['name']} created "
                                  f"({info['size'] / (1 << 20):.0f} MB).")
            self.refresh()

        def fail(exc):
            self._busy = False
            self.progress.set(0)
            msg = str(exc)
            if isinstance(exc, OperationConflict):
                self.app.notify(msg, ok=False)
            else:
                self.app.notify_event("backup", srv,
                                      f"{srv.name}: backup FAILED - {msg}",
                                      ok=False)
            self._set(msg[:90], T.RED)

        self.app.runner.run(work, on_success=ok, on_error=fail)

    def _backup_quick(self):
        self._backup("quick")

    def _backup_full(self):
        self._backup("full")

    def _restore(self, item):
        if self._guard() or self.server is None:
            return
        srv = self.server
        if not messagebox.askyesno(
                "Restore backup",
                f"Restore\n\n{item['name']}\n\nonto {srv.name}?\n\n"
                "- the server stops if it is running (and restarts after)\n"
                "- CURRENT data is snapshotted first (rollback possible)\n"
                "- the operation is journaled - SSH drops are recoverable"):
            return
        self._busy = True
        self._set("restoring...", T.AMBER)

        def work():
            with self.app.locks.guard(srv.id, "restore"):
                return BK.restore_backup(self.app.ssh, srv, item["path"],
                                         self.app.control,
                                         log=self._log_cb(),
                                         progress=self._progress_cb())

        def ok(info):
            self._busy = False
            self.progress.set(0)
            self.app.notify_event("restore", srv,
                                  f"{srv.name}: backup restored. Rollback "
                                  "snapshot kept on the server.")
            self.refresh()

        def fail(exc):
            self._busy = False
            self.progress.set(0)
            self.app.notify_event("restore", srv,
                                  f"{srv.name}: restore FAILED - {exc}",
                                  ok=False)
            self._set(str(exc)[:90], T.RED)

        self.app.runner.run(work, on_success=ok, on_error=fail)

    def _delete(self, item):
        if self._guard() or self.server is None:
            return
        srv = self.server
        if not messagebox.askyesno("Delete backup",
                                   f"Delete {item['name']} permanently?"):
            return

        def work():
            return BK.delete_backup(self.app.ssh, srv, item["name"])

        self.app.runner.run(work, on_success=lambda ok: self.refresh(),
                            on_error=lambda e: self._set(str(e)[:90], T.RED))

    def _cleanup(self):
        if self._guard() or self.server is None:
            return
        srv = self.server
        self._busy = True
        self._set("cleaning up...", T.AMBER)

        def work():
            with self.app.locks.guard(srv.id, "cleanup"):
                return MT.cleanup(self.app.ssh, srv, log=self._log_cb(),
                                  progress=self._progress_cb())

        def ok(res):
            self._busy = False
            self.progress.set(0)
            self.app.notify_event("cleanup", srv,
                                  f"{srv.name}: cleanup done - "
                                  f"{res.get('free_after', 0) / (1 << 30):.1f} "
                                  "GB free now.")
            self.refresh()

        def fail(exc):
            self._busy = False
            self.progress.set(0)
            self._set(str(exc)[:90], T.RED)
            self.app.notify(str(exc)[:160], ok=False)

        self.app.runner.run(work, on_success=ok, on_error=fail)

    # ---------------------------------------------------------------- policy
    def _save_policy(self):
        srv = self.server
        if srv is None:
            return
        if not self.app.allowed(srv, "edit"):
            self.app.notify("Editing is not allowed in restricted mode.",
                            ok=False)
            return
        bt = self.e_backup_time.get().strip()
        rt = self.e_restart_time.get().strip()
        try:
            keep = max(1, int(self.e_keep.get().strip() or "10"))
        except ValueError:
            keep = srv.backup_keep
        notify = self.e_notify.get().strip().lower() not in ("no", "off", "0", "false")
        if bt and srv.parsed_backup_time() is None:
            self.app.notify("Backup time must look like 04:30", ok=False)
            return
        if rt and srv.parsed_restart_time() is None:
            self.app.notify("Restart time must look like 04:30", ok=False)
            return
        srv.backup_time, srv.restart_time = bt, rt
        srv.backup_keep, srv.notify = keep, notify
        self.app.store.update(srv)
        self.app.notify("Policy saved - the scheduler picks it up "
                        "within 30 seconds.")
