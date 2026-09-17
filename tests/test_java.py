"""Java version management tests (issues 1, 15)."""
from __future__ import annotations

from mcmanager.javas import (required_java, java_for_server, pick_java,
                             discover_javas)

from conftest import make_server


def test_year_based_versions_need_java_25():
    """The exact bug from issue 1: MC 26.2 must need Java 25, not 21."""
    assert required_java("26.1") == 25
    assert required_java("26.2") == 25
    assert required_java("27.3") == 25
    assert required_java("26.2-rc1") == 25


def test_classic_versions():
    assert required_java("1.21.4") == 21
    assert required_java("1.21.11") == 21
    assert required_java("1.20.5") == 21
    assert required_java("1.20.6") == 21
    assert required_java("1.20.4") == 17
    assert required_java("1.18.2") == 17
    assert required_java("1.17.1") == 16
    assert required_java("1.16.5") == 8
    assert required_java("1.12.2") == 8


def test_snapshot_versions():
    # 2026 snapshots -> Java 25, 2025 snapshots -> 21
    assert required_java("26w13a") == 25
    assert required_java("25w46a") == 21
    assert required_java("24w14a") == 21


def test_weird_inputs_fall_back_sanely():
    assert required_java("") == 21
    assert required_java("unknown") == 21
    assert required_java("1.21.4-pre.2") in (21, 25)


def test_per_server_override():
    srv = make_server(java_major=17)
    assert java_for_server(srv) == 17
    srv2 = make_server(java_major=0)
    assert java_for_server(srv2) == 25  # from mc_version 26.2


def test_pick_java_prefers_newest_sufficient():
    javas = [
        {"path": "/usr/bin/java", "major": 17, "raw": "17.0.1"},
        {"path": "/usr/lib/jvm/java-25/bin/java", "major": 25,
         "raw": "25-beta"},
        {"path": "/usr/lib/jvm/java-21/bin/java", "major": 21, "raw": "21"},
    ]
    # required 25: only the Java 25 install qualifies even though 21 is the
    # distro default on PATH (the exact v1 bug)
    picked = pick_java(javas, 25)
    assert picked["major"] == 25
    # required 21: the newest that satisfies is 25
    assert pick_java(javas, 21)["major"] == 25
    # nothing satisfies 8? -> 17 does (>= 8)
    assert pick_java(javas, 8)["major"] == 25


def test_discover_parses_remote_output(fake_ssh, server):
    fake_ssh.on("java", (
        "JAVA|/usr/bin/java|17|openjdk version \"17.0.12\" 2024-07-16\n"
        "JAVA|/usr/lib/jvm/java-25-openjdk/bin/java|25|openjdk version \"25\" 2025-09-16\n"
        "JAVA|/home/user/.jdks/temurin-21/bin/java|21|openjdk version \"21.0.4\"\n"
    ))
    javas = discover_javas(fake_ssh, server)
    assert [j["major"] for j in javas] == [25, 21, 17]
    assert javas[0]["path"].endswith("java-25-openjdk/bin/java")


def test_discover_legacy_1_8_naming(fake_ssh, server):
    fake_ssh.on("java", 'JAVA|/usr/bin/java|8|openjdk version "1.8.0_412"')
    javas = discover_javas(fake_ssh, server)
    assert javas and javas[0]["major"] == 8
