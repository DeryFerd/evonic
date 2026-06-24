"""
Tests for cli/evomem_update.py — evomem latest-release check + installer.
All network/subprocess access is mocked; no real downloads.
"""
import io
import os
import json
import stat
import zipfile
import subprocess
from unittest import mock

import pytest

from cli import evomem_update as evup


# ── version parsing / comparison ─────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("v0.2.5", (0, 2, 5)),
    ("0.2.5", (0, 2, 5)),
    ("evomem 0.2.0", (0, 2, 0)),
    ("V1.2", (1, 2)),
    ("", ()),
    ("notaversion", ()),
])
def test_parse_semver(raw, expected):
    assert evup.parse_semver(raw) == expected


@pytest.mark.parametrize("cur,latest,outdated", [
    ("0.2.0", "0.2.5", True),
    ("0.2.5", "0.2.5", False),
    ("0.3.0", "0.2.5", False),
    ("0.2", "0.2.5", True),
    ("", "0.2.5", False),      # unknown current → don't nag
    ("0.2.0", "", False),      # unknown latest → don't nag
])
def test_is_outdated(cur, latest, outdated):
    assert evup.is_outdated(cur, latest) is outdated


# ── host target mapping ──────────────────────────────────────────────────────

@pytest.mark.parametrize("system,machine,expected", [
    ("Darwin", "arm64", "aarch64-apple-darwin"),
    ("Darwin", "x86_64", "x86_64-apple-darwin"),
    ("Linux", "x86_64", "x86_64-unknown-linux-musl"),
    ("Linux", "amd64", "x86_64-unknown-linux-musl"),
    ("Windows", "AMD64", None),
    ("Linux", "aarch64", None),  # not published by make dist
])
def test_host_target(system, machine, expected):
    with mock.patch.object(evup.platform, "system", return_value=system), \
         mock.patch.object(evup.platform, "machine", return_value=machine):
        assert evup.host_target() == expected


# ── current_version ──────────────────────────────────────────────────────────

def test_current_version_parses():
    cp = subprocess.CompletedProcess([], 0, stdout="evomem 0.2.0\n", stderr="")
    with mock.patch.object(evup.subprocess, "run", return_value=cp):
        assert evup.current_version("/bin/evomem") == "0.2.0"


def test_current_version_nonzero_returns_none():
    cp = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
    with mock.patch.object(evup.subprocess, "run", return_value=cp):
        assert evup.current_version("/bin/evomem") is None


def test_current_version_missing_binary_returns_none():
    with mock.patch.object(evup.subprocess, "run", side_effect=OSError):
        assert evup.current_version("/nope/evomem") is None


# ── latest_release ───────────────────────────────────────────────────────────

def _fake_release_json():
    return {
        "tag_name": "v0.2.5",
        "assets": [
            {"name": "evomem-0.2.5-aarch64-apple-darwin.zip",
             "browser_download_url": "https://x/aarch64-apple-darwin.zip"},
            {"name": "evomem-0.2.5-x86_64-apple-darwin.zip",
             "browser_download_url": "https://x/x86_64-apple-darwin.zip"},
            {"name": "evomem-0.2.5-x86_64-unknown-linux-musl.zip",
             "browser_download_url": "https://x/x86_64-unknown-linux-musl.zip"},
            {"name": "notes.txt", "browser_download_url": "https://x/notes.txt"},
        ],
    }


def test_latest_release_parses():
    body = io.BytesIO(json.dumps(_fake_release_json()).encode())
    with mock.patch.object(evup.urllib.request, "urlopen", return_value=body):
        rel = evup.latest_release()
    assert rel["version"] == "0.2.5"
    assert rel["tag"] == "v0.2.5"
    assert rel["assets"]["aarch64-apple-darwin"] == "https://x/aarch64-apple-darwin.zip"
    assert "x86_64-unknown-linux-musl" in rel["assets"]
    assert len(rel["assets"]) == 3  # notes.txt ignored


def test_latest_release_network_error_returns_none():
    with mock.patch.object(evup.urllib.request, "urlopen", side_effect=OSError("offline")):
        assert evup.latest_release() is None


# ── download_and_install ─────────────────────────────────────────────────────

def _zip_with(member_name: str, body: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member_name, body)
    return buf.getvalue()


def test_download_and_install_replaces_binary(tmp_path):
    zip_bytes = _zip_with("evomem", '#!/bin/sh\necho "evomem 9.9.9"\n')
    dest = tmp_path / "bin" / "evomem"

    with mock.patch.object(evup.urllib.request, "urlopen",
                           return_value=io.BytesIO(zip_bytes)):
        evup.download_and_install("https://x/evomem.zip", str(dest))

    assert dest.is_file()
    assert os.access(str(dest), os.X_OK)
    out = subprocess.run([str(dest), "--version"], capture_output=True, text=True)
    assert "9.9.9" in out.stdout


def test_download_and_install_rejects_archive_without_binary(tmp_path):
    zip_bytes = _zip_with("readme.txt", "no binary here")
    dest = tmp_path / "evomem"
    with mock.patch.object(evup.urllib.request, "urlopen",
                           return_value=io.BytesIO(zip_bytes)):
        with pytest.raises(ValueError):
            evup.download_and_install("https://x/evomem.zip", str(dest))
    assert not dest.exists()  # dest untouched on failure
