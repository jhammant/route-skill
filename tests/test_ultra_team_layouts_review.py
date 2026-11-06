"""Offline repros for commands prescribed by the built-in analyst briefs.

Run with --noconftest to avoid importing route or its optional review machinery.
Only synthetic files in pytest's temporary directory are scanned.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib


PATH = Path(__file__).resolve().parent.parent / "src" / "route" / "data" / "teams.toml"


def _brief(layout: str, analyst: str) -> str:
    layouts = tomllib.loads(PATH.read_text(encoding="utf-8"))["layouts"]
    return next(a["brief"] for a in layouts[layout]["analysts"] if a["id"] == analyst)


def _snippets(brief: str) -> list[str]:
    return re.findall(r"`([^`]+)`", brief)


def test_pagespeed_fallback_requests_performance():
    snippets = _snippets(_brief("website-audit", "performance"))
    command = next(s for s in snippets if s.startswith("curl ") and "runPagespeed" in s)
    url = next(s for s in shlex.split(command) if "runPagespeed" in s)
    # Include the query fragment the brief explicitly tells the analyst to append.
    url += "".join(s for s in snippets if s.startswith("&category="))
    url = url.replace("<url>", quote("https://example.com/", safe=""))

    requested = parse_qs(urlsplit(url).query).get("category", [])
    # API contract: Performance is the default ONLY when category is absent.
    # https://developers.google.com/speed/docs/insights/v5/reference/pagespeedapi/runpagespeed
    categories = {c.lower().replace("_", "-") for c in requested} if requested else {"performance"}

    assert "performance" in categories, (
        "The fallback explicitly requests other categories, disabling the default "
        f"performance analysis: {sorted(categories)}"
    )


def test_swift_scan_finds_ordinary_force_unwraps(tmp_path):
    brief = _brief("app-review", "performance")
    command = next(s for s in _snippets(brief) if s.startswith("grep "))
    sample = tmp_path / "Crash.swift"
    sample.write_text(
        "let user: User? = nil\n"
        "print(user!)\n"
        "let userId = user!.id\n"
        "print(values[0]!)\n"
        "let other = makeOptional()!\n"
        "try! fail()\n"
        "let forced = object as! User\n"
        "let safe = user?.id\n",
        encoding="utf-8",
    )
    command = command.replace("<repo>", shlex.quote(str(tmp_path)))

    result = subprocess.run(
        ["bash", "-c", command], cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    matched_lines = {int(n) for n in re.findall(r"Crash\.swift:(\d+):", result.stdout)}

    assert result.returncode == 0, result.stderr
    assert matched_lines == {2, 3, 4, 5, 6, 7}, (
        f"The prescribed scan missed force unwraps on lines {sorted({2, 3, 4, 5} - matched_lines)}; "
        f"it matched only {sorted(matched_lines)}"
    )


def test_secret_scan_finds_private_key_header(tmp_path):
    brief = _brief("go-live", "secrets")
    command = re.search(r"working tree: `([^`]+)`", brief).group(1)
    pattern = next(s for s in _snippets(brief) if "BEGIN " in s and "PRIVATE KEY" in s)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "fixture.pem").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "SYNTHETIC-NON-SECRET\n"
        "-----END RSA PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "fixture.pem"], check=True, capture_output=True,
    )
    command = command.replace("<repo>", shlex.quote(str(tmp_path))).replace("<pattern>", pattern)

    result = subprocess.run(
        ["bash", "-c", command], cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "fixture.pem:1", (
        "The prescribed location-only scan missed the private-key header: " + result.stderr
    )
    assert "SYNTHETIC-NON-SECRET" not in result.stdout + result.stderr
