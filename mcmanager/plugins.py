"""Server-side plugin / mod management for the Plugins & Mods page.

- Figures out the right folder by itself: plugins/ for Bukkit-family
  servers, mods/ for Fabric/Forge/... (hybrids and unknown platforms are
  resolved by whatever folder actually exists on the remote machine).
- Installed items are every *.jar (enabled) and *.jar.disabled (turned
  off) inside that folder.
- Enable/disable = remote rename  Foo.jar  <->  Foo.jar.disabled  so the
  jar stays on disk and can be switched back on at any time.
- Reload support map: only Bukkit-family cores can hot-reload plugins
  with `reload confirm`; everything else needs a full restart.
"""
from __future__ import annotations

import posixpath
import stat as statmod
from dataclasses import dataclass

from .models import Server
from .ssh_manager import SSHManager

DISABLED_SUFFIX = ".disabled"

# platforms that store extensions in mods/ first (or exclusively)
_MOD_FIRST = {
    "fabric", "forge", "quilt", "neoforge", "vanilla", "sponge",
    "arclight", "banner",
}

# platforms whose console accepts  reload confirm  (hot plugin reload)
_RELOADABLE = {
    "paper", "purpur", "spigot", "bukkit", "pufferfish", "airplane",
    "cardboard",
}

# Modrinth loader facet for the search (None = don't restrict)
LOADER_FOR_PLATFORM = {
    "paper": "paper", "purpur": "purpur", "spigot": "spigot",
    "bukkit": "bukkit", "folia": "paper", "pufferfish": "paper",
    "airplane": "paper", "cardboard": "bukkit",
    "fabric": "fabric", "quilt": "quilt", "neoforge": "neoforge",
    "forge": "forge", "bungeecord": "bungeecord",
    "waterfall": "waterfall", "velocity": "velocity",
    # hybrids (mohist, magma, ...) / vanilla / sponge: leave open
}

# plugin vs mod project_type on Modrinth
PLUGIN_PLATFORMS = {
    "paper", "purpur", "spigot", "bukkit", "folia", "pufferfish",
    "airplane", "mohist", "magma", "catserver", "cardboard",
    "bungeecord", "waterfall", "velocity",
}
MOD_PLATFORMS = {
    "fabric", "forge", "quilt", "neoforge", "vanilla", "sponge",
    "arclight", "banner",
}


@dataclass
class InstalledItem:
    filename: str
    path: str
    enabled: bool
    size: int = 0
    mtime: int = 0


# ------------------------------------------------------------ platform ----
def project_type(platform: str) -> str:
    return "mod" if (platform or "").lower() in MOD_PLATFORMS else "plugin"


def loader_for(platform: str) -> str | None:
    return LOADER_FOR_PLATFORM.get((platform or "").lower())


def reload_command(platform: str) -> str | None:
    """`reload confirm` for Bukkit-family, None when a restart is needed."""
    return "reload confirm" if (platform or "").lower() in _RELOADABLE else None


def candidate_folders(platform: str) -> list[str]:
    return ["mods", "plugins"] if (platform or "").lower() in _MOD_FIRST \
        else ["plugins", "mods"]


# ------------------------------------------------------------- listing ----
def _is_dir(attr) -> bool:
    return statmod.S_ISDIR(attr.st_mode or 0)


def resolve_folder(ssh: SSHManager, server: Server) -> tuple[str, bool]:
    """Return (folder, folder_exists). Self-detects plugins/ vs mods/."""
    cands = candidate_folders(server.platform)
    fallback = cands[0]
    if not server.mc_dir:
        return fallback, False
    base = server.mc_dir.rstrip("/")
    with ssh.sftp(server) as sftp:
        for f in cands:
            try:
                if _is_dir(sftp.stat(posixpath.join(base, f))):
                    return f, True
            except OSError:
                continue
    return fallback, False


def list_installed(ssh: SSHManager, server: Server,
                   folder: str | None = None) -> dict:
    """Every enabled / disabled jar in the server's extension folder."""
    if folder is None:
        folder, exists = resolve_folder(ssh, server)
    else:
        exists = bool(server.mc_dir)
    items: list[InstalledItem] = []
    if exists and server.mc_dir:
        base = posixpath.join(server.mc_dir, folder)
        jar_suffix, off_suffix = ".jar", ".jar" + DISABLED_SUFFIX
        with ssh.sftp(server) as sftp:
            try:
                entries = sftp.listdir_attr(base)
            except OSError:
                entries = []
            for e in entries:
                name = e.filename
                if not isinstance(name, str) or name.startswith("."):
                    continue
                low = name.lower()
                if low.endswith(jar_suffix):
                    items.append(InstalledItem(
                        name, posixpath.join(base, name), True,
                        int(e.st_size or 0), int(e.st_mtime or 0)))
                elif low.endswith(off_suffix):
                    items.append(InstalledItem(
                        name, posixpath.join(base, name), False,
                        int(e.st_size or 0), int(e.st_mtime or 0)))
    items.sort(key=lambda i: (not i.enabled, i.filename.lower()))
    return {"folder": folder, "exists": exists, "items": items}


# ------------------------------------------------------- enable / delete ---
def set_enabled(ssh: SSHManager, server: Server, folder: str,
                filename: str, enabled: bool) -> str:
    """Rename a jar on/off; returns the new filename."""
    base = posixpath.join(server.mc_dir, folder)
    src = posixpath.join(base, filename)
    if enabled:
        if not filename.lower().endswith(DISABLED_SUFFIX):
            return filename  # already enabled
        dst_name = filename[: -len(DISABLED_SUFFIX)]
    else:
        if filename.lower().endswith(DISABLED_SUFFIX):
            return filename  # already disabled
        dst_name = filename + DISABLED_SUFFIX
    dst = posixpath.join(base, dst_name)

    with ssh.sftp(server) as sftp:
        try:
            sftp.stat(dst)
            raise RuntimeError(f"'{dst_name}' already exists in {folder}/ - "
                               "delete one of them first.")
        except FileNotFoundError:
            pass
        except OSError as exc:
            if "Name or service" in str(exc) or "No such file" in str(exc):
                pass
            else:
                raise
        try:
            sftp.posix_rename(src, dst)
        except (AttributeError, IOError):
            sftp.rename(src, dst)
    return dst_name


def delete_item(ssh: SSHManager, server: Server, folder: str,
                filename: str) -> bool:
    path = posixpath.join(server.mc_dir, folder, filename)
    with ssh.sftp(server) as sftp:
        try:
            sftp.remove(path)
            return True
        except FileNotFoundError:
            return False
