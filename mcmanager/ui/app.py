"""Main window: sidebar navigation + page host. Pages are rebuilt on switch."""
from __future__ import annotations

import customtkinter as ctk

from .. import theme as T
from ..models import Server, ServerStore
from ..control import ServerControl
from ..ssh_manager import SSHManager
from ..tasks import TaskRunner
from . import wallpaper
from .widgets import Toast

PAGES_DASHBOARD = [
    ("dashboard", "Dashboard"),
]
PAGES_SERVER = [
    ("server", "Control"),
    ("wizard", "Setup Wizard"),
    ("detect", None),  # handled by dialog, not a page
    ("browse", "Mods & Plugins"),
    ("console", "Console"),
    ("terminal", "Terminal"),
    ("files", "Files"),
    ("monitor", "Monitor"),
]
PAGES_SERVER = [(k, l) for k, l in PAGES_SERVER if l]


class MCManagerApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        T.configure()
        self.title("MC Manager - Minecraft Server Controller")
        self.geometry("1180x740")
        self.minsize(980, 620)
        self.configure(fg_color=T.BG)

        self.store = ServerStore()
        self.ssh = SSHManager()
        self.control = ServerControl(self.ssh)
        self.runner = TaskRunner()
        self.selected_id: str | None = self.store.servers[0].id if self.store.servers else None
        self._current_page: ctk.CTkFrame | None = None
        self._current_key: str | None = None

        self._build_sidebar()
        self.content = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0)
        self.content.pack(side="right", fill="both", expand=True)

        self.toast = Toast(self)
        self.after(100, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.show_page("dashboard")
        self._center()

    # -------------------------------------------------------------- helpers
    def server(self) -> Server | None:
        return self.store.get(self.selected_id)

    def notify(self, message: str, ok: bool = True) -> None:
        self.toast.show(message, ok)

    def select_server(self, server_id: str) -> None:
        self.selected_id = server_id
        self.show_page("server")

    def _center(self) -> None:
        self.update_idletasks()
        w, h = 1180, 740
        x = max(0, (self.winfo_screenwidth() - w) // 2)
        y = max(0, (self.winfo_screenheight() - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")

    # -------------------------------------------------------------- sidebar
    def _build_sidebar(self) -> None:
        bar = ctk.CTkFrame(self, width=210, fg_color=T.SURFACE, corner_radius=0)
        bar.pack(side="left", fill="y")
        bar.pack_propagate(False)
        wallpaper.attach(bar)   # vertical wallpaper strip behind the nav

        t1 = ctk.CTkLabel(bar, text="MC Manager", font=T.font(21, "bold"),
                          text_color=T.TEXT)
        t1.pack(pady=(24, 2))
        t2 = ctk.CTkLabel(bar, text="SERVER CONTROLLER", font=T.font(10),
                          text_color=T.ACCENT)
        t2.pack(pady=(0, 18))
        wallpaper.blend_corners(t1)
        wallpaper.blend_corners(t2)

        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self._nav_box = ctk.CTkFrame(bar, fg_color="transparent")
        self._nav_box.pack(fill="both", expand=True, padx=12)
        wallpaper.attach(self._nav_box)

        footer = ctk.CTkFrame(bar, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=12, pady=16)
        wallpaper.attach(footer)
        add_btn = ctk.CTkButton(footer, text="+  Add Server", height=40,
                      corner_radius=T.BUTTON_RADIUS,
                      fg_color=T.ACCENT, hover_color=T.ACCENT_DARK,
                      text_color=T.ON_ACCENT, font=T.font(13, "bold"),
                      command=self._open_add)
        add_btn.pack(fill="x")
        wallpaper.blend_corners(add_btn)
        vlbl = ctk.CTkLabel(footer, text="Python Edition v1.4.2", font=T.font(10),
                            text_color=T.TEXT_DIM)
        vlbl.pack(pady=(10, 0))
        wallpaper.blend_corners(vlbl)

    def _open_add(self) -> None:
        from .pages_editor import open_editor
        open_editor(self, on_saved=lambda srv: (
            setattr(self, "selected_id", srv.id), self._refresh_nav(),
            self.show_page("server")))

    def open_detector(self) -> None:
        from .pages_detect import open_detector
        srv = self.server()
        if srv is None:
            return
        open_detector(self, srv,
                      on_done=lambda s: (self._refresh_nav(), self.show_page("server")))

    def _refresh_nav(self) -> None:
        for w in self._nav_box.winfo_children():
            w.destroy()
        if not getattr(self._nav_box, "_mcm_wallpaper_attached", False):
            wallpaper.attach(self._nav_box)
        self._nav_buttons.clear()

        srv = self.server()
        entries = list(PAGES_DASHBOARD)
        if srv:
            entries += [(k, lbl) for k, lbl in PAGES_SERVER]

        for key, label in entries:
            active = key == self._current_key
            btn = ctk.CTkButton(
                self._nav_box, text=label, anchor="w", height=36,
                corner_radius=T.BUTTON_RADIUS, font=T.font(13, "bold" if active else "normal"),
                fg_color=T.SURFACE2 if active else "transparent",
                hover_color=T.SURFACE2,
                text_color=T.ACCENT if active else T.TEXT_DIM,
                command=lambda k=key: self.show_page(k),
            )
            btn.pack(fill="x", pady=2)
            wallpaper.blend_corners(btn)
            self._nav_buttons[key] = btn

        if srv:
            self._server_badge(srv)

    def _server_badge(self, srv) -> None:
        """Compact 'active server' card at the bottom of the nav (replaces
        the old raw dashed separator + plain text lines)."""
        box = ctk.CTkFrame(self._nav_box, fg_color=T.SURFACE2,
                           corner_radius=T.BUTTON_RADIUS, border_width=1,
                           border_color=T.BORDER, cursor="hand2")
        box.pack(fill="x", pady=(14, 4), ipady=4)
        wallpaper.blend_corners(box)
        ctk.CTkLabel(box, text="ACTIVE SERVER", font=T.font(9, "bold"),
                     text_color=T.TEXT_DIM, anchor="w").pack(
            fill="x", padx=12, pady=(7, 0))
        ctk.CTkLabel(box, text=srv.name, font=T.font(13, "bold"),
                     text_color=T.TEXT, anchor="w").pack(
            fill="x", padx=12, pady=(1, 0))
        sub = (f"{T.platform_label(srv.platform)}"
               + (f" · MC {srv.mc_version}" if srv.mc_version else ""))
        ctk.CTkLabel(box, text=sub, font=T.font(10), text_color=T.TEXT_DIM,
                     anchor="w", wraplength=160, justify="left").pack(
            fill="x", padx=12, pady=(0, 7))

        def go(_e=None):
            self.show_page("server")

        def hover_in(_e=None):
            box.configure(border_color=T.ACCENT)

        def hover_out(_e=None):
            box.configure(border_color=T.BORDER)

        box.bind("<Button-1>", go)
        box.bind("<Enter>", hover_in)
        box.bind("<Leave>", hover_out)

    # ------------------------------------------------------------- switching
    def show_page(self, key: str) -> None:
        if key != "dashboard" and self.server() is None:
            key = "dashboard"
        self._current_key = key
        self._refresh_nav()

        if self._current_page is not None:
            try:
                if hasattr(self._current_page, "on_hide"):
                    self._current_page.on_hide()
                self._current_page.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._current_page = None

        page_cls = self._page_class(key)
        page = page_cls(self.content, self, self.server())
        page.configure(fg_color="transparent")  # let the wallpaper show through
        wallpaper.attach(page)
        page.pack(fill="both", expand=True)
        self._current_page = page
        if hasattr(page, "on_show"):
            page.on_show()

    def _page_class(self, key: str):
        from .pages_dashboard import DashboardPage
        from .pages_server import ServerPage
        from .pages_wizard import WizardPage
        from .pages_browse import PluginsPage
        from .pages_console import ConsolePage
        from .pages_terminal import TerminalPage
        from .pages_files import FilesPage
        from .pages_monitor import MonitorPage
        return {
            "dashboard": DashboardPage, "server": ServerPage,
            "wizard": WizardPage, "browse": PluginsPage,
            "console": ConsolePage, "terminal": TerminalPage,
            "files": FilesPage, "monitor": MonitorPage,
        }[key]

    # ------------------------------------------------------------ main loop
    def _tick(self) -> None:
        try:
            self.runner.poll()
            # re-sample corner-blended widgets that moved (layout settling,
            # window resize) - cheap winfo checks, no redraw when unchanged
            wallpaper.refresh_blends()
        except Exception:  # noqa: BLE001
            pass
        self.after(100, self._tick)

    def _on_close(self) -> None:
        try:
            if self._current_page is not None and hasattr(self._current_page, "on_hide"):
                self._current_page.on_hide()
        except Exception:  # noqa: BLE001
            pass
        self.ssh.close_all()
        self.destroy()


def launch() -> None:
    app = MCManagerApp()
    app.mainloop()
