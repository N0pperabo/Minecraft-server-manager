"""Automatic Minecraft server installer on the remote Linux machine.
probe -> Java (auto-managed, MC-version-aware) -> verified jar download
-> config -> start."""
from __future__ import annotations

import re

from . import apis
from . import javas
from .models import Server
from .ssh_manager import SSHManager, shq


class SystemProbe:
    def __init__(self, home: str, os_id: str, package_manager: str,
                 java_major: int, has_curl: bool) -> None:
        self.home = home
        self.os_id = os_id
        self.package_manager = package_manager
        self.java_major = java_major
        self.has_curl = has_curl

    def summary(self) -> str:
        pm = self.package_manager or "unknown"
        java = f"Java {self.java_major}" if self.java_major else "no Java"
        curl = "curl OK" if self.has_curl else "no curl"
        return f"OS: {self.os_id or 'unknown'}   PM: {pm}   {java}   {curl}"


# required_java lives in javas.py now (fixes MC 26.x -> Java 25, issue 1)
required_java = javas.required_java


class MinecraftInstaller:
    def __init__(self, ssh: SSHManager, server: Server, log) -> None:
        self.ssh = ssh
        self.server = server
        self.log = log  # callable(str) -> None, called from worker thread

    # ------------------------------------------------------------------ steps
    def run(self, command: str, timeout: float = 60):
        return self.ssh.exec(self.server, command, timeout=timeout)

    def probe(self) -> SystemProbe:
        home = self.run("echo $HOME").stdout.strip() or f"/home/{self.server.username}"
        os_id = self.run(
            "grep -E '^ID=' /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '\"' | head -1"
        ).stdout.strip().lower()
        pkg = self.run(
            "for m in apt-get dnf yum apk; do command -v $m >/dev/null 2>&1 "
            "&& { echo $m; break; }; done"
        ).stdout.strip()
        java_raw = self.run("java -version 2>&1 | head -1").stdout
        m = re.search(r'version "(\d+)', java_raw)
        java_major = int(m.group(1)) if m else 0
        has_curl = "yes" in self.run("command -v curl >/dev/null 2>&1 && echo yes || echo no").stdout
        probe = SystemProbe(home, os_id, pkg, java_major, has_curl)
        self.log(f"[probe] {probe.summary()}")
        return probe

    def install_java(self, probe: SystemProbe, major: int) -> str:
        """Ensure a suitable JVM exists; returns the java path start.sh uses.

        v2.0: discovers EVERY installed JVM first (a present Java 25 no
        longer triggers a pointless OpenJDK 21 install), installs missing
        runtimes user-locally from Temurin (no root), and only falls back
        to the distro package manager (password piped to sudo -S - no
        NOPASSWD:ALL, issue 4).
        """
        return javas.ensure_java(self.ssh, self.server,
                                 self.server.mc_version,
                                 probe.package_manager, self.log)

    def download_jar(self, platform: str, mc_version: str) -> str:
        """Download the server jar into the remote dir. Returns jar filename."""
        self.log(f"[download] resolving {platform} {mc_version} server jar...")
        if platform == "paper":
            url = apis.paper_jar_url(mc_version)
            jar_name = "paper.jar"
        elif platform == "fabric":
            url = apis.fabric_jar_url(mc_version)
            jar_name = "fabric-server.jar"
        elif platform == "vanilla":
            url = apis.vanilla_jar_url(mc_version)
            jar_name = "server.jar"
        elif platform == "forge":
            promos = apis.forge_versions_for(mc_version)
            if not promos:
                raise RuntimeError(f"Forge has no build for MC {mc_version}.")
            url = apis.forge_installer_url(mc_version, promos[0])
            jar_name = "forge-installer.jar"
        else:
            raise RuntimeError(f"Unknown platform '{platform}'.")
        self.log(f"[download] {url}")
        self.run(f"cd {shq(self.server.mc_dir)} && curl -fL -o {shq(jar_name)} {shq(url)}",
                 timeout=900)
        check = self.run(f"cd {shq(self.server.mc_dir)} && ls -la {shq(jar_name)}")
        if check.exit_code != 0 or "No such" in check.stdout:
            raise RuntimeError("Download failed - jar not found on the server.")
        self.log(f"[download] saved as {jar_name}")
        return jar_name

    def run_forge_installer(self) -> None:
        self.log("[forge] running installer (this can take a few minutes)...")
        res = self.run(
            f"cd {shq(self.server.mc_dir)} && "
            f"java -jar forge-installer.jar --installServer",
            timeout=1800,
        )
        if res.exit_code != 0:
            tail = (res.stderr or res.stdout).strip()[-800:]
            raise RuntimeError("Forge installer failed:\n" + tail)
        self.log("[forge] libraries installed.")

    def write_configs(self, platform: str, port: int, motd: str = "Powered by MC Manager") -> None:
        d = shq(self.server.mc_dir)
        self.run(f"cd {d} && echo 'eula=true' > eula.txt")
        props = (
            f"server-port={port}\n"
            f"motd={motd}\n"
            "online-mode=true\n"
            "max-players=20\n"
            "view-distance=10\n"
            "enable-rcon=false\n"
            "level-seed=\n"
        )
        self.run(f"cd {d} && cat > server.properties << 'MCEOF'\n{props}MCEOF")
        self.server.mc_port = port

        ram = max(1024, self.server.ram_mb)
        # absolute java path when we provisioned a specific runtime (issue 1)
        java_bin = self.server.java_path or "java"
        extra = (" " + self.server.extra_jvm) if self.server.extra_jvm else ""
        if platform == "forge":
            self.run(f"cd {d} && cat > user_jvm_args.txt << 'MCEOF'\n-Xmx{ram}M{extra}\nMCEOF")
            self.run(
                f"cd {d} && cat > start.sh << 'MCEOF'\n"
                "#!/usr/bin/env bash\n"
                "cd \"$(dirname \"$0\")\"\n"
                "if [ -f run.sh ]; then exec bash run.sh; fi\n"
                f"exec {shq(java_bin)} @user_jvm_args.txt -jar forge-installer.jar nogui\n"
                "MCEOF"
            )
        else:
            jar = {"paper": "paper.jar", "fabric": "fabric-server.jar",
                   "vanilla": "server.jar"}.get(platform, "server.jar")
            self.run(
                f"cd {d} && cat > start.sh << 'MCEOF'\n"
                "#!/usr/bin/env bash\n"
                "cd \"$(dirname \"$0\")\"\n"
                f"exec {shq(java_bin)} -Xms512M -Xmx{ram}M{extra} -jar {jar} nogui\n"
                "MCEOF"
            )
        self.run(f"cd {d} && chmod +x start.sh")
        self.log("[config] eula.txt, server.properties and start.sh written "
                 f"(java: {java_bin}).")

    def full_install(self, platform: str, mc_version: str, mc_dir: str,
                     port: int, ram_mb: int) -> None:
        self.server.platform = platform
        self.server.mc_version = mc_version
        self.server.mc_dir = mc_dir
        self.server.ram_mb = ram_mb

        probe = self.probe()
        self.log(f"[setup] creating directory {mc_dir}")
        self.run(f"mkdir -p {shq(mc_dir)}")
        self.install_java(probe, required_java(mc_version))
        self.server.mc_version = mc_version
        jar = self.download_jar(platform, mc_version)
        if jar == "forge-installer.jar":
            self.run_forge_installer()
        self.write_configs(platform, port)
        self.log("[done] installation complete - ready to start!")
