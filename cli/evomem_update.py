"""
evomem_update — check the vendored evomem binary against the latest GitHub
release and (optionally) download + install the newest build.

Used by `evonic doctor`: plain mode reports whether the binary is up to date,
`--fix` mode downloads and atomically replaces it. Network access is
best-effort with timeouts; callers degrade to a warning on any failure.

Releases: https://github.com/anvie/evomem/releases
Assets:   evomem-<version>-<target>.zip, each containing a single `evomem`.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile

logger = logging.getLogger(__name__)

# Owner/repo whose GitHub Releases hold the evomem binaries (overridable).
RELEASE_REPO = os.environ.get("EVOMEM_RELEASE_REPO", "anvie/evomem")

# Host (system, machine) -> Rust target triple that `make dist` publishes.
_TARGETS = {
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "aarch64"): "aarch64-apple-darwin",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("linux", "amd64"): "x86_64-unknown-linux-musl",
}


def host_target() -> str | None:
    """Return the evomem release target triple for this host, or None if the
    platform/arch has no published build."""
    return _TARGETS.get((platform.system().lower(), platform.machine().lower()))


def parse_semver(s: str) -> tuple:
    """Parse a version like 'v0.2.5', '0.2.5', or 'evomem 0.2.5' into a tuple of
    ints, e.g. (0, 2, 5). Non-numeric/missing parts are ignored; returns () when
    no numbers are found."""
    if not s:
        return ()
    token = s.strip().split()[-1].lstrip("vV")
    parts = []
    for piece in token.split("."):
        num = ""
        for ch in piece:
            if ch.isdigit():
                num += ch
            else:
                break
        if num == "":
            break
        parts.append(int(num))
    return tuple(parts)


def is_outdated(current: str, latest: str) -> bool:
    """True if `current` is a strictly older version than `latest`. Unknown/empty
    versions are treated as not-outdated (don't nag on parse failure)."""
    cur, lat = parse_semver(current), parse_semver(latest)
    if not cur or not lat:
        return False
    return cur < lat


def current_version(binary: str) -> str | None:
    """Return the installed evomem version string (e.g. '0.2.0'), or None if the
    binary is missing or doesn't report a version."""
    try:
        out = subprocess.run(
            [binary, "--version"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    ver = parse_semver(out.stdout)
    return ".".join(str(n) for n in ver) if ver else None


def latest_release(timeout: int = 15) -> dict | None:
    """Fetch the latest GitHub release. Returns
    {'version': 'X.Y.Z', 'tag': 'vX.Y.Z', 'assets': {target: download_url}}
    or None on any network/parse failure (best-effort)."""
    url = f"https://api.github.com/repos/{RELEASE_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "evonic-doctor",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception as e:  # noqa: BLE001 — best-effort, never raise to caller
        logger.debug("evomem latest_release fetch failed: %s", e)
        return None

    tag = data.get("tag_name") or ""
    ver = ".".join(str(n) for n in parse_semver(tag))
    if not ver:
        return None
    assets = {}
    for asset in data.get("assets", []) or []:
        name = asset.get("name", "")
        dl = asset.get("browser_download_url")
        if not name.endswith(".zip") or not dl:
            continue
        for triple in set(_TARGETS.values()):
            if triple in name:
                assets[triple] = dl
    return {"version": ver, "tag": tag, "assets": assets}


def download_and_install(url: str, dest: str, timeout: int = 60) -> None:
    """Download the release zip, extract its `evomem` binary, validate it runs,
    and atomically replace `dest`. Raises on any failure; `dest` is only touched
    by a successful atomic replace, so a failed update leaves it intact."""
    dest_dir = os.path.dirname(os.path.abspath(dest)) or "."
    os.makedirs(dest_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "evomem.zip")
        req = urllib.request.Request(url, headers={"User-Agent": "evonic-doctor"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(zip_path, "wb") as f:
            shutil.copyfileobj(resp, f)

        with zipfile.ZipFile(zip_path) as zf:
            member = next((n for n in zf.namelist()
                           if os.path.basename(n) == "evomem" and not n.endswith("/")), None)
            if member is None:
                raise ValueError("downloaded archive does not contain an 'evomem' binary")
            extracted = zf.extract(member, tmp)

        # Make executable and validate before swapping it in.
        os.chmod(extracted, os.stat(extracted).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        check = subprocess.run([extracted, "--version"], capture_output=True, text=True, timeout=10)
        if check.returncode != 0 or not parse_semver(check.stdout):
            raise ValueError("downloaded evomem binary failed to run")

        # Atomic replace within the destination filesystem.
        staged = os.path.join(dest_dir, ".evomem.download.tmp")
        shutil.copyfile(extracted, staged)
        os.chmod(staged, os.stat(staged).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(staged, dest)
