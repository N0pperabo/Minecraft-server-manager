"""Mods & Plugins page - TWO sections as requested by the user:

1) INSTALLED - every plugin/mod already on the server (plugins/ or mods/
   folder is detected automatically), each with a sliding ON/OFF switch
   (rename *.jar <-> *.jar.disabled) and a delete button, plus a
   "Reload Plugins" button at the bottom that sends `reload confirm` to
   the server console (or offers a restart when the platform can't
   hot-reload, e.g. Fabric / Forge / Folia / proxies).

2) GET PLUGINS - search modrinth.com; the query is automatically filtered
   by the ACTIVE server: project_type (plugin/mod), loader (paper,
   fabric, forge...) and the exact Minecraft version, so results always
   match what the server runs. Compatible builds are ranked first and
   badged in the version picker.
"""
from __future__ import annotations

import os
import posixpath
import time
from tkinter import messagebox

import customtkinter as ctk

from .. import apis
from .. import plugins as PL
from .. import theme as T
from ..models import Server
from . import wallpaper
from .widgets import Card, GhostButton


def _fmt_size(n: int) -> str:
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _fmt_date(ts: int) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except Exception:  # noqa: BLE001
        return ""


class PluginsPage(ctk.CTkFrame):
    def __init__(self, master, app, server: Server | None):
        super().__init__(master, fg_color=T.BG, corner_radius=0)
        self.app = app
        self.server = server
        self._busy = False
        self._items: list[PL.InstalledItem] = []
        self._folder = ""
        self._folder_exists = False
        self._switches: dict[str, object] = {}

        # ---------------------------------------------------------- header
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(24, 4))
        ctk.CTkLabel(head, text="Mods & Plugins", font=T.font(19, "bold"),
                     text_color=T.TEXT).pack(side="left")
        ctk.CTkLabel(head, text="powered by modrinth.com", font=T.font(11),
                     text_color=T.TEXT_DIM).pack(side="left", padx=10)

        self.chip = ctk.CTkLabel(head, text=self._chip_text(), font=T.font(11),
                                 text_color=T.ACCENT_SOFT)
        self.chip.pack(side="right")

        self.status = ctk.CTkLabel(self, text="", font=T.font(11),
                                   text_color=T.TEXT_DIM, anchor="w",
                                   height=22)
        self.status.pack(fill="x", padx=30)

        # ------------------------------------------------------- tab bar
        tabs = ctk.CTkFrame(self, fg_color="transparent")
        tabs.pack(fill="x", padx=28, pady=(6, 8))
        self.tab_btns: dict[str, ctk.CTkButton] = {}
        for key, label in (("installed", "Installed on server"),
                           ("get", "Get from Modrinth")):
            btn = ctk.CTkButton(tabs, text=label, width=170, height=34,
                                corner_radius=T.BUTTON_RADIUS, font=T.font(13, "bold"),
                                command=lambda k=key: self._show_tab(k))
            btn.pack(side="left", padx=(0, 8))
            wallpaper.blend_corners(btn)
            self.tab_btns[key] = btn
        GhostButton(tabs, text="Refresh", width=90, height=34,
                    command=self.refresh_installed).pack(side="right")

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True)

        self._build_installed_tab()
        self._build_get_tab()
        self._show_tab("installed")

    # ------------------------------------------------------------ helpers
    def _chip_text(self) -> str:
        s = self.server
        if s is None:
            return "no server selected"
        plat = T.platform_label(s.platform) if s.platform else "platform unknown"
        ver = s.mc_version or "?"
        return f"{plat}  ·  MC {ver}"

    def _set_status(self, text: str, color: str = T.TEXT_DIM) -> None:
        if self.winfo_exists():
            self.status.configure(text=text, text_color=color)

    def _idle_ok(self) -> str:
        return "done"

    # ================================================== TAB 1: INSTALLED ==
    def _build_installed_tab(self) -> None:
        tab = ctk.CTkFrame(self.body, fg_color="transparent")
        self.tab_installed = tab

        # bottom action bar (user asked for the reload button at the bottom)
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(side="bottom", fill="x", padx=28, pady=(6, 14))
        self.reload_btn = ctk.CTkButton(
            bar, text="Reload Plugins", width=170, height=42, corner_radius=T.BUTTON_RADIUS,
            fg_color=T.ACCENT, hover_color=T.ACCENT_DARK,
            text_color=T.ON_ACCENT, font=T.font(13, "bold"),
            command=self._reload_plugins)
        self.reload_btn.pack(side="left")
        self.reload_hint = ctk.CTkLabel(
            bar, text="sends 'reload confirm' to the console",
            font=T.font(11), text_color=T.TEXT_DIM)
        self.reload_hint.pack(side="left", padx=12)

        self.inst_list = ctk.CTkScrollableFrame(tab, fg_color=T.BG)
        self.inst_list.pack(fill="both", expand=True, padx=22, pady=(0, 4))
        wallpaper.attach(self.inst_list)   # rows float over the wallpaper

    def _show_tab(self, key: str) -> None:
        for k, btn in self.tab_btns.items():
            active = k == key
            btn.configure(
                fg_color=T.ACCENT if active else T.SURFACE2,
                hover_color=T.ACCENT_DARK if active else T.SURFACE2,
                text_color=T.ON_ACCENT if active else T.TEXT_DIM)
        top = self.tab_installed if key == "installed" else self.tab_get
        other = self.tab_get if key == "installed" else self.tab_installed
        other.pack_forget()
        top.pack(fill="both", expand=True)
        if key == "installed":
            self.refresh_installed()
        else:
            self.query.focus_set()

    def on_show(self) -> None:
        self.chip.configure(text=self._chip_text())
        self._show_tab("installed")

    # ------------------------------------------------------- listing
    def refresh_installed(self) -> None:
        if self.server is None or not self.server.mc_dir:
            self._render_installed(None)
            return
        srv = self.server
        self._set_status("loading installed plugins...")
        self.app.runner.run(
            lambda: PL.list_installed(self.app.ssh, srv),
            on_success=self._render_installed,
            on_error=lambda e: (self._set_status(str(e)[:140], T.RED),
                                self._render_installed(None, error=True)),
        )

    def _render_installed(self, data: dict | None, error: bool = False) -> None:
        if not self.winfo_exists():
            return
        wallpaper.clear(self.inst_list)
        self._items = []
        self._switches.clear()
        srv = self.server
        n = len(data["items"]) if data else 0
        self.tab_btns["installed"].configure(
            text=f"Installed on server ({n})")

        if srv is None or not srv.mc_dir:
            self._empty_card(
                "No install linked yet",
                "Run the Setup Wizard or let the app search the whole disk "
                "for an existing server first.")
            self._set_status("")
            return
        if error:
            self._set_status("could not list the folder", T.RED)
            return
        if not data:
            self._set_status("")
            return
        self._folder = data["folder"]
        self._folder_exists = data["exists"]
        self._items = data["items"]

        if not data["exists"]:
            self._empty_card(
                f"No {data['folder']}/ folder yet",
                "Install a plugin from the 'Get from Modrinth' tab - the "
                "folder is created automatically.")
            self._set_status("")
            return

        self._set_status(
            f"{len(self._items)} file(s) in {self._folder}/ on "
            f"{srv.name}  ·  flip a switch to turn a plugin ON/OFF")
        for item in self._items:
            self._installed_row(item)

    def _empty_card(self, title: str, sub: str) -> None:
        card = Card(self.inst_list)
        card.pack(fill="x", pady=6, padx=4)
        box = ctk.CTkFrame(card, fg_color="transparent")
        box.pack(fill="x", padx=16, pady=16)
        ctk.CTkLabel(box, text=title, font=T.font(14, "bold"),
                     text_color=T.TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(box, text=sub, font=T.font(12),
                     text_color=T.TEXT_DIM, anchor="w").pack(anchor="w")
        if self.server is not None and not self.server.mc_dir:
            GhostButton(box, text="Find existing installs", width=180,
                        command=self.app.open_detector).pack(anchor="w", pady=(10, 0))

    def _installed_row(self, item: PL.InstalledItem) -> None:
        row = Card(self.inst_list)
        row.pack(fill="x", pady=4, padx=4)
        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=9)

        info = ctk.CTkFrame(inner, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)
        base = item.filename[:-len(PL.DISABLED_SUFFIX)] if not item.enabled \
            else item.filename
        ctk.CTkLabel(info, text=base, font=T.font(13, "bold"),
                     text_color=T.TEXT if item.enabled else T.TEXT_DIM,
                     anchor="w").pack(anchor="w")
        meta = (f"{_fmt_size(item.size)}  ·  {_fmt_date(item.mtime)}"
                f"  ·  {self._folder}/")
        ctk.CTkLabel(info, text=meta, font=T.font(10),
                     text_color=T.TEXT_DIM, anchor="w").pack(anchor="w")
        state = ctk.CTkLabel(
            info, text="ENABLED" if item.enabled else "DISABLED",
            font=T.font(10, "bold"),
            text_color=T.ACCENT_SOFT if item.enabled else T.AMBER)
        state.pack(anchor="w", pady=(2, 0))

        GhostButton(inner, text="Delete", width=76, height=28,
                    command=lambda: self._delete(item)).pack(
            side="right", padx=(8, 0))

        sw = ctk.CTkSwitch(inner, text="", width=56,
                           progress_color=T.ACCENT,
                           button_color=T.TEXT_DIM,
                           button_hover_color=T.TEXT,
                           command=lambda: self._toggle(item, sw))
        if item.enabled:
            sw.select()
        sw.pack(side="right")
        self._switches[item.filename] = sw

    # ------------------------------------------------------- toggle/del
    def _toggle(self, item: PL.InstalledItem, switch) -> None:
        if self._busy or self.server is None:
            # revert visual state, operation refused
            (switch.deselect if switch.get() else switch.select)()
            return
        enable = bool(switch.get())
        srv = self.server
        self._busy = True
        switch.configure(state="disabled")
        action = "Enabling" if enable else "Disabling"
        self._set_status(f"{action} {item.filename} ...", T.AMBER)

        def work():
            return PL.set_enabled(self.app.ssh, srv, self._folder,
                                  item.filename, enable)

        def ok(new_name):
            self._busy = False
            self.app.notify(f"{new_name} - "
                            + ("press Reload Plugins (or restart) to apply"
                               if enable else
                               "it will not load on the next start/reload"))
            self.refresh_installed()

        def fail(exc):
            self._busy = False
            self._set_status(str(exc)[:140], T.RED)
            self.refresh_installed()

        self.app.runner.run(work, on_success=ok, on_error=fail)

    def _delete(self, item: PL.InstalledItem) -> None:
        if self._busy or self.server is None:
            return
        if not messagebox.askyesno(
                "Delete plugin",
                f"Delete '{item.filename}' from {self._folder}/ ?\n"
                "This cannot be undone."):
            return
        srv = self.server
        self._busy = True
        self._set_status(f"deleting {item.filename} ...", T.AMBER)

        def work():
            return PL.delete_item(self.app.ssh, srv, self._folder,
                                  item.filename)

        self.app.runner.run(
            work,
            on_success=lambda gone: (self._set_busy_off(),
                                     self.app.notify("Deleted"),
                                     self.refresh_installed()),
            on_error=lambda e: (self._set_busy_off(),
                                self._set_status(str(e)[:140], T.RED)))

    def _set_busy_off(self) -> None:
        self._busy = False

    # ------------------------------------------------------------- reload
    def _reload_plugins(self) -> None:
        if self._busy or self.server is None:
            return
        srv = self.server
        if not srv.mc_dir:
            self.app.notify("No install linked - run the Wizard or Detect first.",
                            ok=False)
            return
        cmd = PL.reload_command(srv.platform)
        if cmd is None:
            plat = T.platform_label(srv.platform)
            if messagebox.askyesno(
                    "Restart required",
                    f"{plat} cannot hot-reload extensions.\n\n"
                    "Restart the server now to load the changes?"):
                self._busy = True
                self._set_status("restarting server...", T.AMBER)
                self.app.runner.run(
                    lambda: self.app.control.restart(srv),
                    on_success=lambda _: (
                        self._set_busy_off(),
                        self.app.notify("Server restarted - extensions loaded"),
                        self._set_status("done", T.ACCENT_SOFT)),
                    on_error=lambda e: (
                        self._set_busy_off(),
                        self.app.notify(str(e), ok=False),
                        self._set_status(str(e)[:140], T.RED)))
            return

        self._busy = True
        self.reload_btn.configure(state="disabled")
        self._set_status(f"sending '{cmd}' ...", T.AMBER)

        def work():
            if not self.app.control.status(srv):
                raise RuntimeError("The server is not running - start it first.")
            self.app.control.send_command(srv, cmd)
            return True

        def ok(_):
            self._busy = False
            if self.winfo_exists():
                self.reload_btn.configure(state="normal")
                self._set_status("done", T.ACCENT_SOFT)
            if srv.external_screen:
                self.app.store.update(srv)  # persist a discovered session
            self.app.notify(f"'{cmd}' sent - watch the Console for the result")

        def fail(exc):
            self._busy = False
            if self.winfo_exists():
                self.reload_btn.configure(state="normal")
                self._set_status(str(exc)[:140], T.RED)
            self.app.notify(str(exc), ok=False)

        self.app.runner.run(work, on_success=ok, on_error=fail)

    # ============================================== TAB 2: GET (MODRINTH) ==
    def _build_get_tab(self) -> None:
        tab = ctk.CTkFrame(self.body, fg_color="transparent")
        self.tab_get = tab

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", padx=28, pady=(0, 4))
        self.query = ctk.CTkEntry(
            bar, placeholder_text="Search e.g. essentials, worldedit, LuckPerms...",
            height=38, font=T.font(13), fg_color=T.SURFACE,
            border_color=T.BORDER)
        self.query.pack(side="left", fill="x", expand=True)
        self.query.bind("<Return>", lambda e: self._search())
        ctk.CTkButton(bar, text="Search", width=100, height=38,
                      corner_radius=T.BUTTON_RADIUS, fg_color=T.ACCENT,
                      hover_color=T.ACCENT_DARK, text_color=T.ON_ACCENT,
                      font=T.font(13, "bold"),
                      command=self._search).pack(side="left", padx=(8, 0))

        self.match_var = ctk.StringVar(value="on")
        self.match_chk = ctk.CTkCheckBox(
            tab, text="Only builds for my Minecraft version",
            variable=self.match_var, onvalue="on", offvalue="off",
            font=T.font(12), text_color=T.TEXT_DIM,
            fg_color=T.ACCENT, hover_color=T.ACCENT_DARK,
            checkbox_width=18, checkbox_height=18)
        self.match_chk.pack(anchor="w", padx=30, pady=(4, 0))
        self.filter_lbl = ctk.CTkLabel(tab, text=self._filter_text(),
                                       font=T.font(11),
                                       text_color=T.ACCENT_SOFT)
        self.filter_lbl.pack(anchor="w", padx=30)

        self.results = ctk.CTkScrollableFrame(tab, fg_color=T.BG)
        self.results.pack(fill="both", expand=True, padx=22, pady=(6, 14))
        wallpaper.attach(self.results)

    def _filter_text(self) -> str:
        s = self.server
        if s is None or not s.platform:
            return ("auto-match: platform unknown - run the Wizard or "
                    "'Find existing installs' first")
        ptype = PL.project_type(s.platform)
        loader = PL.loader_for(s.platform)
        ver = s.mc_version or "any"
        return (f"auto-match: {s.platform} -> {ptype}s"
                + (f" · loader '{loader}'" if loader else " · any loader")
                + f" · MC {ver}")

    def _search(self) -> None:
        if self._busy:
            return
        if self.server is None:
            return
        q = self.query.get().strip()
        srv = self.server
        ptype = PL.project_type(srv.platform)
        loader = PL.loader_for(srv.platform)
        gv = srv.mc_version if (self.match_var.get() == "on"
                                and srv.mc_version) else None
        self._set_status(f"searching Modrinth for {ptype}s ...", T.TEXT_DIM)
        self.app.runner.run(
            lambda: apis.modrinth_search(q, ptype, loader, game_version=gv),
            on_success=lambda hits: self._render(hits, gv),
            on_error=lambda e: self._set_status(str(e)[:140], T.RED),
        )

    def _render(self, hits: list, gv: str | None) -> None:
        if not self.winfo_exists():
            return
        wallpaper.clear(self.results)
        if not hits:
            note = "Nothing found."
            if gv:
                note += (f"  (no build for MC {gv} - untick 'Only builds for "
                         "my Minecraft version' to widen the search)")
            ctk.CTkLabel(self.results, text=note, font=T.font(13),
                         text_color=T.TEXT_DIM).pack(pady=30)
            self._set_status("0 results")
            return
        self._set_status(f"{len(hits)} results"
                         + (f" for MC {gv}" if gv else ""))
        for h in hits:
            self._result_row(h)

    def _result_row(self, h: dict) -> None:
        row = Card(self.results)
        row.pack(fill="x", pady=5, padx=4)
        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=10)

        info = ctk.CTkFrame(inner, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)
        title = f"{h.get('title', h.get('slug', '?'))}   by {h.get('author', '?')}"
        ctk.CTkLabel(info, text=title, font=T.font(14, "bold"),
                     text_color=T.TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(info, text=(h.get("description") or "")[:120],
                     font=T.font(11), text_color=T.TEXT_DIM,
                     anchor="w").pack(anchor="w")
        downloads = h.get("downloads", 0)
        ctk.CTkLabel(info, text=f"{downloads:,} downloads   ·   {h.get('slug', '')}",
                     font=T.font(10), text_color=T.ACCENT_SOFT,
                     anchor="w").pack(anchor="w", pady=(2, 0))

        ctk.CTkButton(inner, text="Install", width=90, height=32,
                      corner_radius=T.BUTTON_RADIUS, fg_color=T.ACCENT,
                      hover_color=T.ACCENT_DARK, text_color=T.ON_ACCENT,
                      font=T.font(12, "bold"),
                      command=lambda: self._pick_version(h)).pack(side="right")

    # ------------------------------------------------------- installing
    def _pick_version(self, h: dict) -> None:
        if self.server is None or self._busy:
            return
        if not self.server.mc_dir:
            messagebox.showinfo(
                "Link a server first",
                "Install a server (Setup Wizard) or use 'Find existing "
                "installs' on the Control page first.")
            return
        slug = h.get("slug") or h.get("project_id")
        self._busy = True
        self._set_status(f"fetching versions for {slug}...", T.AMBER)
        self.app.runner.run(
            lambda: apis.modrinth_versions(slug),
            on_success=lambda vs: (self._set_busy_off(), self._choose(slug, h, vs)),
            on_error=lambda e: (self._set_busy_off(),
                                self._set_status(str(e)[:140], T.RED)),
        )

    def _choose(self, slug: str, h: dict, versions: list) -> None:
        if not self.winfo_exists():
            return
        if not versions:
            self._set_status("No versions available.", T.RED)
            return
        self._choose_window(slug, h, versions)

    def _choose_window(self, slug: str, h: dict, versions: list) -> None:
        win = ctk.CTkToplevel(self)
        win.title(f"Install {h.get('title', slug)}")
        win.geometry("580x440")
        win.configure(fg_color=T.BG)
        win.transient(self)
        win.grab_set()

        srv = self.server
        mc_ver = srv.mc_version
        ptype = PL.project_type(srv.platform)
        loader = PL.loader_for(srv.platform)

        def rank(v: dict) -> int:
            gvs = v.get("game_versions") or []
            lds = v.get("loaders") or []
            return (0 if mc_ver in gvs else 1) + (0 if loader in lds else 2)

        versions = sorted(versions, key=rank)[:40]
        labels = []
        for v in versions:
            gvs = ", ".join((v.get("game_versions") or [])[:3])
            lds = ",".join(v.get("loaders") or [])
            match = "compatible" if mc_ver in (v.get("game_versions") or []) \
                else "other MC version"
            labels.append(f"{v.get('version_number')}   [{lds}]   {gvs}   - {match}")

        ctk.CTkLabel(win, text=f"Choose a build of {h.get('title', slug)}",
                     font=T.font(15, "bold"), text_color=T.TEXT).pack(pady=(18, 4))
        ctk.CTkLabel(win, text=f"server runs MC {mc_ver or '?'} on {srv.platform or '?'}",
                     font=T.font(11), text_color=T.TEXT_DIM).pack()
        menu = ctk.CTkOptionMenu(win, values=labels or ["(none)"], font=T.mono(11),
                                 fg_color=T.SURFACE2, button_color=T.ACCENT_DARK,
                                 button_hover_color=T.ACCENT)
        if labels:
            menu.set(labels[0])  # pre-select the best-matching build
        menu.pack(fill="x", padx=30, pady=14)

        def do_install() -> None:
            idx = labels.index(menu.get()) if labels else -1
            if idx < 0:
                return
            version = versions[idx]
            file_meta = apis.primary_file(version)
            url = file_meta.get("url")
            if not url:
                self.app.notify("No downloadable file in that build", ok=False)
                return
            win.destroy()
            self._download_and_install(url, file_meta.get("filename", "file.jar"),
                                       ptype)

        ctk.CTkButton(win, text="Download & install on server", height=40,
                      corner_radius=T.BUTTON_RADIUS, fg_color=T.ACCENT,
                      hover_color=T.ACCENT_DARK, text_color=T.ON_ACCENT,
                      font=T.font(13, "bold"), command=do_install).pack(
            padx=30, pady=(4, 18))
        ctk.CTkLabel(win, text="Installs to plugins/ or mods/ depending on your platform.",
                     font=T.font(10), text_color=T.TEXT_DIM).pack(pady=(0, 12))

    def _download_and_install(self, url: str, filename: str, ptype: str) -> None:
        if self.server is None or self._busy:
            return
        srv = self.server
        self._busy = True
        folder = "mods" if ptype == "mod" else "plugins"
        remote = posixpath.join(srv.mc_dir, folder, filename)
        self._set_status(f"downloading {filename}...", T.AMBER)

        def work():
            local = apis.temp_file(".jar")
            apis.download_to(url, local)
            with self.app.ssh.sftp(srv) as sftp:
                try:
                    sftp.mkdir(posixpath.join(srv.mc_dir, folder))
                except OSError:
                    pass  # already exists
                sftp.put(str(local), remote)
            try:
                os.remove(local)
            except OSError:
                pass
            return filename

        self.app.runner.run(
            work,
            on_success=lambda n: (self._set_busy_off(),
                                  self.app.notify(
                                      f"{n} installed to {folder}/ - use "
                                      "'Reload Plugins' or restart to load it"),
                                  self.refresh_installed()),
            on_error=lambda e: (self._set_busy_off(),
                                self._set_status(str(e)[:160], T.RED)),
        )


# Backwards-compatible name used by app.py
BrowsePage = PluginsPage
