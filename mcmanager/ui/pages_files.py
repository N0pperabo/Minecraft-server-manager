"""SFTP file browser v2.

Fixes over v1:
- shows EVERY file and folder (including hidden dotfiles) with robust
  stat.S_ISDIR detection
- path bar: type any absolute path and press Go; quick buttons for
  Home / filesystem root / the linked server folder
- chunked rendering (200 rows at a time) so huge directories stay fast
- live filter box to instantly find a name inside the current folder
- unreadable folders produce a clear message instead of an empty list
"""
from __future__ import annotations

import os
import posixpath
import stat as statmod
import time
from tkinter import filedialog, messagebox, simpledialog

import customtkinter as ctk

from .. import theme as T
from ..models import Server
from ..ssh_manager import SSHError
from . import wallpaper
from .widgets import Card, DangerButton, GhostButton

CHUNK = 200


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


class FilesPage(ctk.CTkFrame):
    def __init__(self, master, app, server: Server | None):
        super().__init__(master, fg_color=T.BG, corner_radius=0)
        self.app = app
        self.server = server
        # start at the linked server folder; otherwise at filesystem root so
        # the user sees the whole disk (not only /root of the ssh account)
        self.path = server.mc_dir if (server and server.mc_dir) else "/"
        self.entries: list[dict] = []
        self._filtered: list[dict] = []
        self._shown = 0
        self._listing = False
        self._busy = False

        # -- header ---------------------------------------------------------
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(24, 4))
        ctk.CTkLabel(head, text="Files", font=T.font(19, "bold"),
                     text_color=T.TEXT).pack(side="left")

        quick = ctk.CTkFrame(head, fg_color="transparent")
        quick.pack(side="right")
        GhostButton(quick, text="New folder", width=96, height=28,
                    command=self._mkdir).pack(side="left", padx=3)
        GhostButton(quick, text="Upload here", width=96, height=28,
                    command=self._upload).pack(side="left", padx=3)
        GhostButton(quick, text="Refresh", width=76, height=28,
                    command=self._load).pack(side="left", padx=3)

        # -- path bar ---------------------------------------------------------
        pathbar = ctk.CTkFrame(self, fg_color="transparent")
        pathbar.pack(fill="x", padx=28, pady=(2, 2))
        self.path_var = ctk.StringVar(value=self.path)
        self.path_entry = ctk.CTkEntry(pathbar, textvariable=self.path_var,
                                       height=34, font=T.mono(11),
                                       corner_radius=T.BUTTON_RADIUS,
                                       fg_color=T.SURFACE, border_color=T.BORDER)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.path_entry.bind("<Return>", lambda e: self._go())
        go_btn = ctk.CTkButton(pathbar, text="Go", width=60, height=34,
                      corner_radius=T.BUTTON_RADIUS,
                      fg_color=T.ACCENT, hover_color=T.ACCENT_DARK,
                      text_color=T.ON_ACCENT, font=T.font(12, "bold"),
                      command=self._go)
        go_btn.pack(side="left", padx=(6, 0))
        wallpaper.blend_corners(go_btn)
        GhostButton(pathbar, text="Home", width=64, height=34,
                    command=self._home).pack(side="left", padx=(6, 0))
        GhostButton(pathbar, text="Root /", width=64, height=34,
                    command=self._go_root).pack(side="left", padx=(6, 0))
        if server and server.mc_dir:
            GhostButton(pathbar, text="Server dir", width=88, height=34,
                        command=self._srvdir).pack(side="left", padx=(6, 0))

        # -- filter + status --------------------------------------------------
        filterbar = ctk.CTkFrame(self, fg_color="transparent")
        filterbar.pack(fill="x", padx=28, pady=(2, 2))
        self.filter_var = ctk.StringVar()
        self.filter_entry = ctk.CTkEntry(
            filterbar, textvariable=self.filter_var, height=30,
            placeholder_text="Filter names in this folder...",
            font=T.font(11), fg_color=T.SURFACE, border_color=T.BORDER)
        self.filter_entry.pack(side="left", fill="x", expand=True)
        self.filter_entry.bind("<KeyRelease>", lambda e: self._apply_filter())
        self.status = ctk.CTkLabel(filterbar, text="", font=T.font(11),
                                   text_color=T.TEXT_DIM, anchor="e")
        self.status.pack(side="right", padx=(10, 0))

        # -- listing ----------------------------------------------------------
        self.list_box = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        self.list_box.pack(fill="both", expand=True, padx=22, pady=(4, 18))
        wallpaper.attach(self.list_box)

    # ================================================================ listing
    def on_show(self) -> None:
        self._load()

    def _load(self) -> None:
        if self.server is None or self._listing:
            return
        srv = self.server
        path = self.path_var.get().strip() or self.path
        self._listing = True
        self.status.configure(text="loading...", text_color=T.TEXT_DIM)
        self._hint_row(f"Opening {path} ...")
        self.app.runner.run(
            lambda: self._list_dir(srv, path),
            on_success=self._render_full,
            on_error=self._list_error,
        )

    def _hint_row(self, text: str) -> None:
        """Dim single-line placeholder row while a folder loads / is empty."""
        wallpaper.clear(self.list_box)
        lbl = ctk.CTkLabel(self.list_box, text=text, font=T.font(12),
                           text_color=T.TEXT_DIM, anchor="w")
        lbl.pack(fill="x", padx=14, pady=12)

    def _list_dir(self, srv, path: str) -> tuple:
        with self.app.ssh.sftp(srv) as sftp:
            try:
                real = sftp.normalize(path.replace("~", ".", 1) if path.startswith("~")
                                      else path)
                attrs = sftp.listdir_attr(real)
            except OSError as exc:
                code = getattr(exc, "errno", None)
                if code == 13:
                    raise SSHError(
                        f"Permission denied for '{path}' - this account cannot read it.")
                if code == 2:
                    raise SSHError(f"Folder '{path}' does not exist.")
                raise SSHError(f"Cannot list '{path}': {exc}")
            out = []
            for attr in attrs:
                mode = attr.st_mode or 0
                out.append({
                    "name": attr.filename,
                    "path": posixpath.join(real, attr.filename),
                    "is_dir": statmod.S_ISDIR(mode),
                    "size": attr.st_size or 0,
                    "mtime": attr.st_mtime or 0,
                })
        out.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return real, out

    def _render_full(self, data) -> None:
        self._listing = False
        if not self.winfo_exists():
            return
        real, entries = data
        self.path = real
        self.path_var.set(real)
        self.entries = entries
        self._apply_filter()

    def _list_error(self, exc) -> None:
        self._listing = False
        if self.winfo_exists():
            self.status.configure(text=str(exc)[:140], text_color=T.RED)
            wallpaper.clear(self.list_box)
            card = Card(self.list_box)
            card.pack(fill="x", pady=6, padx=4)
            ctk.CTkLabel(card, text="Could not open this folder",
                         font=T.font(14, "bold"), text_color=T.TEXT).pack(
                anchor="w", padx=16, pady=(14, 2))
            ctk.CTkLabel(card, text=str(exc)[:200], font=T.font(12),
                         text_color=T.TEXT_DIM, wraplength=640,
                         justify="left").pack(anchor="w", padx=16, pady=(0, 14))

    # ================================================================ rendering
    def _apply_filter(self) -> None:
        needle = self.filter_var.get().strip().lower()
        self._filtered = ([e for e in self.entries if needle in e["name"].lower()]
                          if needle else list(self.entries))
        self._shown = 0
        wallpaper.clear(self.list_box)
        if self.path != "/" and not needle:
            self._parent_row()
        self._render_chunk()

    def _parent_row(self) -> None:
        parent = posixpath.dirname(self.path) or "/"
        row = ctk.CTkFrame(self.list_box, fg_color=T.SURFACE, corner_radius=10,
                           bg_color=T.CORNER_BG,
                           border_width=1, border_color=T.BORDER)
        wallpaper.blend_corners(row)
        row.pack(fill="x", pady=3, padx=4)
        ctk.CTkButton(row, text="‹‹  parent folder", anchor="w", height=30,
                      corner_radius=T.BUTTON_RADIUS,
                      fg_color="transparent", hover_color=T.SURFACE2,
                      text_color=T.TEXT_DIM, font=T.font(12),
                      command=lambda: self._open_path(parent)).pack(
            fill="x", padx=8, pady=6)

    def _render_chunk(self) -> None:
        total = len(self._filtered)
        if total == 0 and not self.filter_var.get():
            self._hint_row("This folder is empty")
        elif total == 0:
            self._hint_row("No name matches the filter")
        start, end = self._shown, min(self._shown + CHUNK, total)
        for e in self._filtered[start:end]:
            try:
                self._row(e)
            except Exception:  # noqa: BLE001
                # one broken row must never kill the whole listing (that
                # is how v1.4.1 ended up showing an empty file manager)
                import traceback
                traceback.print_exc()
                self._hint_row(f"Could not render item '{e.get('name', '?')}'")
        self._shown = end
        extra = f"  (showing {end} of {total})" if total > end else ""
        suffix = f" - filtered from {len(self.entries)}" if self.filter_var.get() else ""
        self.status.configure(text=f"{total} items{extra}{suffix}")

        more = getattr(self, "_more_btn", None)
        if more is not None:
            try:
                more.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._more_btn = None
        if end < total:
            self._more_btn = ctk.CTkButton(
                self.list_box, text=f"Show {min(CHUNK, total - end)} more...",
                height=34, corner_radius=T.BUTTON_RADIUS, fg_color=T.SURFACE2,
                hover_color="#242944", text_color=T.ACCENT_SOFT,
                font=T.font(12), command=self._render_chunk)
            self._more_btn.pack(fill="x", pady=6, padx=4, ipady=2)

    def _row(self, e: dict) -> None:
        row = ctk.CTkFrame(self.list_box, fg_color=T.SURFACE, corner_radius=10,
                           bg_color=T.CORNER_BG,
                           border_width=1, border_color=T.BORDER)
        wallpaper.blend_corners(row)
        row.pack(fill="x", pady=3, padx=4)
        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(fill="x", padx=8, pady=7)

        if e["is_dir"]:
            name = "▸  " + e["name"]
        else:
            name = "      " + e["name"]
        name_btn = ctk.CTkButton(inner, text=name, anchor="w", height=26,
                      corner_radius=T.BUTTON_RADIUS,
                      fg_color="transparent", hover_color=T.SURFACE2,
                      text_color=T.ACCENT_SOFT if e["is_dir"] else T.TEXT_DIM,
                      font=T.font(12),
                      command=lambda: self._open_path(e["path"], e["is_dir"]))
        name_btn.pack(side="left", fill="x", expand=True)
        if not e["is_dir"]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime"]))
            meta = f"{fmt_size(e['size'])}  ·  {when}"
        else:
            meta = "folder"
        ctk.CTkLabel(inner, text=meta, font=T.font(10),
                     text_color=T.TEXT_DIM).pack(side="left", padx=8)
        if not e["is_dir"]:
            GhostButton(inner, text="Download", width=92, height=26,
                        command=lambda: self._download(e)).pack(side="right", padx=6)
        DangerButton(inner, text="Delete", width=70, height=26,
                     command=lambda: self._delete(e)).pack(side="right", padx=4)

    # ================================================================ navigation
    def _open_path(self, path: str, is_dir: bool = True) -> None:
        if not is_dir:
            return
        self.path_var.set(path)
        self.filter_var.set("")
        self._load()

    def _go(self) -> None:
        self.filter_var.set("")
        self._load()

    def _home(self) -> None:
        self.path_var.set("~")
        self.filter_var.set("")
        self._load()

    def _go_root(self) -> None:
        # NOTE: must NOT be named `_root` - tkinter's Misc._root() is used
        # internally by nametowidget/winfo_children and must not be shadowed.
        self.path_var.set("/")
        self.filter_var.set("")
        self._load()

    def _srvdir(self) -> None:
        if self.server and self.server.mc_dir:
            self.path_var.set(self.server.mc_dir)
            self.filter_var.set("")
            self._load()

    def _up(self) -> None:
        parent = posixpath.dirname(self.path) or "/"
        self.path_var.set(parent)
        self._load()

    # ================================================================ actions
    def _guard(self) -> bool:
        if self._busy:
            self.app.notify("Busy - wait for the current transfer", ok=False)
            return True
        return False

    def _mkdir(self) -> None:
        name = simpledialog.askstring("New folder", "Folder name:")
        if not name or self.server is None:
            return
        srv = self.server
        target = posixpath.join(self.path, name)
        self.app.runner.run(
            lambda: self.app.ssh.exec(srv, f"mkdir -p '{target}'", timeout=20),
            on_success=lambda _: self._load(),
            on_error=lambda e: self.app.notify(str(e), ok=False))

    def _upload(self) -> None:
        if self._guard() or self.server is None:
            return
        local = filedialog.askopenfilename(title="Upload file")
        if not local:
            return
        self._busy = True
        srv = self.server
        remote = posixpath.join(self.path, os.path.basename(local))

        def work():
            with self.app.ssh.sftp(srv) as sftp:
                sftp.put(local, remote)
            return os.path.basename(local)

        self.status.configure(text="uploading...", text_color=T.AMBER)
        self.app.runner.run(
            work,
            on_success=lambda n: (self._set_idle(),
                                  self.app.notify(f"Uploaded {n}"), self._load()),
            on_error=lambda e: (self._set_idle(),
                                self.app.notify(str(e)[:160], ok=False)))

    def _download(self, e: dict) -> None:
        if self._guard() or self.server is None:
            return
        local = filedialog.asksaveasfilename(initialfile=e["name"])
        if not local:
            return
        self._busy = True
        srv = self.server
        remote = e["path"]
        self.status.configure(text="downloading...", text_color=T.AMBER)
        self.app.runner.run(
            lambda: self._do_download(srv, remote, local),
            on_success=lambda n: (self._set_idle(),
                                  self.app.notify(f"Saved {n}")),
            on_error=lambda e: (self._set_idle(),
                                self.app.notify(str(e)[:160], ok=False)))

    def _do_download(self, srv, remote: str, local: str) -> str:
        with self.app.ssh.sftp(srv) as sftp:
            sftp.get(remote, local)
        return os.path.basename(local)

    def _delete(self, e: dict) -> None:
        if self.server is None:
            return
        kind = "folder (and everything inside)" if e["is_dir"] else "file"
        if not messagebox.askyesno("Delete", f"Delete {kind} '{e['name']}' from the server?"):
            return
        srv = self.server
        target = e["path"]
        self.app.runner.run(
            lambda: self.app.ssh.exec(srv, f"rm -rf -- '{target}'", timeout=120),
            on_success=lambda _: self._load(),
            on_error=lambda e2: self.app.notify(str(e2), ok=False))

    def _set_idle(self) -> None:
        self._busy = False
        if self.winfo_exists():
            self.status.configure(text="done", text_color=T.ACCENT_SOFT)
