"""Official API clients: PaperMC Fill v3, Fabric meta, Mojang, Forge, Modrinth."""
from __future__ import annotations

import tempfile
from pathlib import Path

import requests

UA = {"User-Agent": "MCManager-Desktop/1.4 (Minecraft server controller)"}
TIMEOUT = 25

PAPER_FILL = "https://fill.papermc.io/v3"
FABRIC_META = "https://meta.fabricmc.net/v2"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
MODRINTH = "https://api.modrinth.com/v2"


class ApiError(Exception):
    pass


def _get_json(url: str, params: dict | None = None) -> dict | list:
    try:
        r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ApiError(f"Network error: {exc}") from exc
    if r.status_code >= 400:
        raise ApiError(f"HTTP {r.status_code} from {url}")
    try:
        return r.json()
    except ValueError as exc:
        raise ApiError("Malformed API response.") from exc


def download_to(url: str, dest: Path, progress=None) -> Path:
    """Stream a download to dest; progress(fraction 0..1) optional."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, headers=UA, timeout=60, stream=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        if progress and total:
                            progress(min(1.0, done / total))
    except requests.RequestException as exc:
        raise ApiError(f"Download failed: {exc}") from exc
    return dest


def temp_file(suffix: str = ".jar") -> Path:
    return Path(tempfile.gettempdir()) / f"mcmanager_dl{suffix}"


# ============================ Paper (Fill v3) =============================

def paper_versions() -> list[str]:
    data = _get_json(f"{PAPER_FILL}/projects/paper")
    versions: list[str] = []
    for _family, lst in data.get("versions", {}).items():
        versions.extend(lst)
    return versions


def paper_jar_info(version: str) -> dict:
    """Latest Paper build for a version: url + sha256 checksum + size.

    The checksum comes from the official PaperMC Fill API itself and is
    what `updater` verifies on the server AFTER downloading (issue 28).
    """
    data = _get_json(f"{PAPER_FILL}/projects/paper/versions/{version}/builds/latest")
    dl = (data.get("downloads") or {}).get("server:default", {})
    url = dl.get("url")
    if not url:
        raise ApiError(f"No Paper build found for {version}.")
    sums = dl.get("checksums") or {}
    return {"url": url, "sha256": sums.get("sha256", ""),
            "sha1": sums.get("sha1", ""), "size": int(dl.get("size") or 0),
            "build": data.get("build", "")}


def paper_jar_url(version: str) -> str:
    return paper_jar_info(version)["url"]


# ================================ Fabric ==================================

def fabric_versions() -> list[str]:
    data = _get_json(f"{FABRIC_META}/versions/game")
    return [v["version"] for v in data if v.get("stable")]


def fabric_jar_url(version: str) -> str:
    loader = _get_json(f"{FABRIC_META}/versions/loader?limit=1")[0]["version"]
    installer = _get_json(f"{FABRIC_META}/versions/installer?limit=1")[0]["version"]
    return (f"{FABRIC_META}/versions/loader/{version}/{loader}/{installer}"
            f"/server/jar")


# ================================ Vanilla =================================

def vanilla_versions() -> list[str]:
    data = _get_json(MOJANG_MANIFEST)
    return [v["id"] for v in data.get("versions", []) if v.get("type") == "release"]


def vanilla_jar_info(version: str) -> dict:
    """Mojang server jar with its official sha1 (issue 28)."""
    data = _get_json(MOJANG_MANIFEST)
    for v in data.get("versions", []):
        if v.get("id") == version:
            meta = _get_json(v["url"])
            dl = (meta.get("downloads") or {}).get("server", {})
            if not dl.get("url"):
                raise ApiError(f"No server jar published for {version}.")
            return {"url": dl["url"], "sha1": dl.get("sha1", ""),
                    "sha256": "", "size": int(dl.get("size") or 0),
                    "build": ""}
    raise ApiError(f"Unknown vanilla version '{version}'.")


def vanilla_jar_url(version: str) -> str:
    return vanilla_jar_info(version)["url"]


# ================================= Forge ==================================

def forge_versions() -> list[str]:
    data = _get_json(FORGE_PROMOS)
    promos: dict[str, str] = data.get("promos", {})
    return [
        k[:-len("-recommended")] for k in promos
        if k.endswith("-recommended")
    ]


def forge_versions_for(mc_version: str) -> list[str]:
    data = _get_json(FORGE_PROMOS)
    promos: dict[str, str] = data.get("promos", {})
    out = []
    for suffix in ("recommended", "latest"):
        key = f"{mc_version}-{suffix}"
        if key in promos:
            out.append(f"{mc_version}-{suffix}")
    return out


def forge_installer_url(mc_version: str, promo: str) -> str:
    data = _get_json(FORGE_PROMOS)
    build = data.get("promos", {}).get(promo)
    if not build:
        raise ApiError(f"No Forge build for {promo}.")
    return (f"https://maven.minecraftforge.net/net/minecraftforge/forge/"
            f"{mc_version}-{build}/forge-{mc_version}-{build}-installer.jar")


def forge_installer_info(mc_version: str, promo: str) -> dict:
    """Forge installer URL (+ maven-side .sha1 sidecar for verification)."""
    url = forge_installer_url(mc_version, promo)
    sha1 = ""
    try:
        r = requests.get(url + ".sha1", headers=UA, timeout=TIMEOUT)
        if r.status_code == 200:
            sha1 = r.text.strip().split()[0] if r.text.strip() else ""
    except requests.RequestException:
        pass
    return {"url": url, "sha1": sha1, "sha256": "", "size": 0,
            "build": promo}


# =============================== Modrinth =================================

def modrinth_search(query: str, project_type: str, loader: str | None,
                    game_version: str | None = None,
                    limit: int = 25) -> list[dict]:
    """Search Modrinth; when game_version is given only builds released
    for THAT Minecraft version are returned (server-matched automatically)."""
    facets = [[f"project_type:{project_type}"]]
    if loader:
        facets.append([f"categories:{loader}"])
    if game_version:
        facets.append([f"versions:{game_version}"])
    data = _get_json(f"{MODRINTH}/search", params={
        "query": query or "", "limit": limit,
        "index": "relevance", "facets": str(facets).replace("'", '"'),
    })
    return data.get("hits", [])


def modrinth_project(slug_or_id: str) -> dict:
    return _get_json(f"{MODRINTH}/project/{slug_or_id}")


def modrinth_versions(slug_or_id: str, loaders: list[str] | None = None,
                      game_version: str | None = None) -> list[dict]:
    params: dict[str, str] = {}
    if loaders:
        params["loaders"] = str(loaders).replace("'", '"')
    if game_version:
        params["game_versions"] = f'["{game_version}"]'
    data = _get_json(f"{MODRINTH}/project/{slug_or_id}/version", params=params)
    return data if isinstance(data, list) else []


def primary_file(version: dict) -> dict:
    files = version.get("files") or []
    for f in files:
        if f.get("primary"):
            return f
    return files[0] if files else {}
