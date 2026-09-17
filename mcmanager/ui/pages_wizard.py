"""4-step setup wizard: probe -> choose -> install -> start, with live log.
Also offers detection of an existing installation as a shortcut."""
from __future__ import annotations

import customtkinter as ctk

from .. import apis
from .. import theme as T
from ..installer import MinecraftInstaller
from ..models import Server
from .widgets import AccentButton, Card, GhostButton, LogBox, LabeledField, SectionTitle

STEPS = ["Probe system", "Choose platform", "Install", "Start"]


class WizardPage(ctk.CTkFrame):
    def __init__(self, master, app, server: Server | None):
        super().__init__(master, fg_color=T.BG, corner_radius=0)
        self.app = app
        self.server = server
        self.installer: MinecraftInstaller | None = None
        self._versions: list[str] = []
        self._installing = False

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(24, 4))
        SectionTitle(head, "Setup Wizard").pack(side="left")
        ctk.CTkLabel(head, text=f"on  {server.username}@{server.host}" if server else "",
                     font=T.font(12), text_color=T.TEXT_DIM).pack(side="left", padx=12)

        # stepper
        self.step_lbls: list[ctk.CTkLabel] = []
        stepper = ctk.CTkFrame(self, fg_color="transparent")
        stepper.pack(fill="x", padx=30, pady=(2, 8))
        for i, name in enumerate(STEPS):
            if i > 0:
                ctk.CTkLabel(stepper, text="›", font=T.font(13),
                             text_color=T.BORDER).pack(side="left", padx=(0, 12))
            lbl = ctk.CTkLabel(stepper, text=f"{i + 1}. {name}", font=T.font(12),
                               text_color=T.TEXT_DIM)
            lbl.pack(side="left", padx=(0, 12))
            self.step_lbls.append(lbl)
        self._set_step(0)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=28, pady=(4, 20))
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        # left: options / actions
        self.left = Card(body)
        self.left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        # right: live log
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        ctk.CTkLabel(right, text="Live output", font=T.font(12),
                     text_color=T.TEXT_DIM, anchor="w").pack(fill="x")
        self.log = LogBox(right)
        self.log.pack(fill="both", expand=True, pady=(6, 0))

        if server:
            self._render_probe()

    # -------------------------------------------------------------- stepper
    def _set_step(self, idx: int) -> None:
        for i, lbl in enumerate(self.step_lbls):
            lbl.configure(
                text_color=T.ACCENT if i == idx else T.TEXT_DIM,
                font=T.font(12, "bold" if i == idx else "normal"))

    # --------------------------------------------------------------- step 1
    def _render_probe(self) -> None:
        for w in self.left.winfo_children():
            w.destroy()
        self._set_step(0)
        ctk.CTkLabel(self.left, text="Step 1 - Probe the machine",
                     font=T.font(15, "bold"), text_color=T.TEXT).pack(
            anchor="w", padx=18, pady=(16, 4))
        ctk.CTkLabel(self.left, text="Checks OS, package manager, Java and curl\n"
                                     "so the installer knows what to do.",
                     font=T.font(12), text_color=T.TEXT_DIM,
                     justify="left").pack(anchor="w", padx=18, pady=(0, 14))
        self.probe_result = ctk.CTkLabel(self.left, text="not probed yet",
                                         font=T.font(12), text_color=T.TEXT_DIM,
                                         anchor="w", justify="left")
        self.probe_result.pack(anchor="w", padx=18, pady=6)
        AccentButton(self.left, text="Run probe", command=self._do_probe).pack(
            anchor="w", padx=18, pady=8)
        GhostButton(self.left, text="Already have a server? Find existing installs",
                    command=self._detect).pack(anchor="w", padx=18, pady=4)

    def _detect(self) -> None:
        self.app.open_detector()

    def _do_probe(self) -> None:
        self._ensure_installer()
        assert self.installer is not None
        self.probe_result.configure(text="probing...", text_color=T.AMBER)
        self.app.runner.run(
            self.installer.probe,
            on_success=lambda p: (self.probe_result.configure(
                text=p.summary(), text_color=T.ACCENT_SOFT), self._render_choose()),
            on_error=lambda e: self.probe_result.configure(
                text=str(e), text_color=T.RED),
        )

    # --------------------------------------------------------------- step 2
    def _render_choose(self) -> None:
        for w in self.left.winfo_children():
            w.destroy()
        self._set_step(1)
        ctk.CTkLabel(self.left, text="Step 2 - Choose your server",
                     font=T.font(15, "bold"), text_color=T.TEXT).pack(
            anchor="w", padx=18, pady=(16, 10))

        ctk.CTkLabel(self.left, text="Platform", font=T.font(11),
                     text_color=T.TEXT_DIM, anchor="w").pack(anchor="w", padx=18)
        self.platform_var = ctk.StringVar(value="paper")
        pr = ctk.CTkFrame(self.left, fg_color="transparent")
        pr.pack(fill="x", padx=18, pady=(2, 8))
        for i, (val, label) in enumerate([
                ("paper", "Paper  (plugins)"), ("fabric", "Fabric  (mods)"),
                ("vanilla", "Vanilla"), ("forge", "Forge  (mods)")]):
            ctk.CTkRadioButton(pr, text=label, variable=self.platform_var,
                               value=val, font=T.font(12),
                               fg_color=T.ACCENT, hover_color=T.ACCENT_DARK,
                               border_color=T.BORDER,
                               command=self._on_platform).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 14), pady=3)

        ctk.CTkLabel(self.left, text="Minecraft version", font=T.font(11),
                     text_color=T.TEXT_DIM, anchor="w").pack(anchor="w", padx=18, pady=(6, 0))
        self.version_menu = ctk.CTkOptionMenu(
            self.left, values=["(loading...)"], font=T.font(12),
            fg_color=T.SURFACE2, button_color=T.ACCENT_DARK,
            button_hover_color=T.ACCENT)
        self.version_menu.pack(fill="x", padx=18, pady=4)
        self._load_versions()

        grid = ctk.CTkFrame(self.left, fg_color="transparent")
        grid.pack(fill="x", padx=18, pady=(8, 0))
        grid.grid_columnconfigure(0, weight=2)
        grid.grid_columnconfigure(1, weight=1)
        self.f_dir = LabeledField(
            grid, "Install directory on the server",
            value=f"/home/{self.server.username}/mc-server")
        self.f_dir.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.f_port = LabeledField(grid, "Server port", value="25565")
        self.f_port.grid(row=0, column=1, sticky="ew")

        ctk.CTkLabel(self.left, text="RAM for the server (MB)", font=T.font(11),
                     text_color=T.TEXT_DIM, anchor="w").pack(anchor="w", padx=18, pady=(8, 0))
        self.ram_lbl = ctk.CTkLabel(self.left, text="4096 MB", font=T.font(12),
                                    text_color=T.ACCENT_SOFT, anchor="w")
        self.ram_lbl.pack(anchor="w", padx=18)
        self.ram = ctk.CTkSlider(self.left, from_=1024, to=16384, number_of_steps=15,
                                 fg_color=T.SURFACE2, progress_color=T.ACCENT,
                                 command=self._ram_moved)
        self.ram.set(4096)
        self.ram.pack(fill="x", padx=18, pady=(0, 10))

        AccentButton(self.left, text="Install now", command=self._install).pack(
            anchor="w", padx=18, pady=(6, 18))

    def _ram_moved(self, value) -> None:
        self.ram_lbl.configure(text=f"{int(value)} MB")

    def _on_platform(self) -> None:
        self._load_versions()

    def _load_versions(self) -> None:
        platform = self.platform_var.get()
        fetch = {"paper": apis.paper_versions, "fabric": apis.fabric_versions,
                 "vanilla": apis.vanilla_versions, "forge": apis.forge_versions}[platform]
        self.version_menu.configure(values=["(loading...)"])
        self.app.runner.run(
            fetch,
            on_success=self._versions_cb,
            on_error=lambda e: self.version_menu.configure(values=[f"(error: {e})"]),
        )

    def _versions_cb(self, versions) -> None:
        self._versions = list(versions)
        if not self._versions:
            self.version_menu.configure(values=["(none found)"])
            return
        self.version_menu.configure(values=self._versions[:60])
        self.version_menu.set(self._versions[0])

    # --------------------------------------------------------------- step 3
    def _install(self) -> None:
        if self._installing:
            return
        try:
            port = int(self.f_port.get() or 25565)
        except ValueError:
            self.app.notify("Port must be a number", ok=False)
            return
        platform = self.platform_var.get()
        version = self.version_menu.get()
        if version.startswith("("):
            self.app.notify("Pick a valid version", ok=False)
            return
        mc_dir = self.f_dir.get()
        if not mc_dir.startswith("/"):
            self.app.notify("Directory must be absolute (start with /)", ok=False)
            return
        self._installing = True
        self._set_step(2)
        self.app.runner.run(
            lambda: self.installer.full_install(platform, version, mc_dir, port, int(self.ram.get())),
            on_success=lambda _: (self._set_busy(False), self._render_start()),
            on_error=lambda e: (self._set_busy(False),
                                self.app.notify(str(e)[:220], ok=False)),
        )

    def _set_busy(self, busy: bool) -> None:
        self._installing = busy

    # --------------------------------------------------------------- step 4
    def _render_start(self) -> None:
        for w in self.left.winfo_children():
            w.destroy()
        self._set_step(3)
        srv = self.server
        ctk.CTkLabel(self.left, text="Step 4 - Launch it",
                     font=T.font(15, "bold"), text_color=T.TEXT).pack(
            anchor="w", padx=18, pady=(16, 4))
        ctk.CTkLabel(self.left, text="Installation finished. Start the server now\n"
                                     "or come back later from the Control page.",
                     font=T.font(12), text_color=T.TEXT_DIM,
                     justify="left").pack(anchor="w", padx=18, pady=(0, 12))
        AccentButton(self.left, text="Start server", command=self._do_start).pack(
            anchor="w", padx=18, pady=4)
        GhostButton(self.left, text="Go to Control page",
                    command=lambda: self.app.show_page("server")).pack(
            anchor="w", padx=18, pady=4)
        ctk.CTkLabel(self.left,
                     text=f"Players join at:  {srv.host}:{self.f_port.get()}",
                     font=T.mono(12), text_color=T.ACCENT_SOFT,
                     anchor="w").pack(anchor="w", padx=18, pady=(14, 0))

    def _do_start(self) -> None:
        self._ensure_installer()
        self.app.runner.run(
            lambda: self.app.control.start(self.server, lambda m: self.app.runner.emit(
                lambda l: self.winfo_exists() and self.log.append(l), m)),
            on_success=lambda _: self.app.notify("Server is starting - open Console"),
            on_error=lambda e: self.app.notify(str(e)[:220], ok=False),
        )

    # --------------------------------------------------------------- helpers
    def _ensure_installer(self) -> None:
        if self.installer is None:
            self.installer = MinecraftInstaller(
                self.app.ssh, self.server,
                lambda line: self.app.runner.emit(
                    lambda l: self.winfo_exists() and self.log.append(l), line))

    def on_show(self) -> None:
        pass
