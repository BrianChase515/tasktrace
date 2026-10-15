"""In-app updates: download the latest TaskTrace build from GitHub and swap it in place.

Updates keep their macOS permissions as long as both builds are signed with the same certificate
(see packaging/sign.sh). The updater refuses a build signed by a different certificate, so a
tampered download can never inherit TaskTrace's permissions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

RELEASE = "https://github.com/BrianChase515/tasktrace/releases/download/latest"
# Fingerprint of the TaskTrace signing certificate (public). Builds signed with it keep their permissions.
STABLE_CERT = "f1a757394b721ed9abb7585fd3089d4fe7e4e35a"
INFO_URL = f"{RELEASE}/latest.json"


def parse_version(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def _curl(url: str, out: Path | None = None, timeout: int = 300) -> bytes:
    # System curl uses the macOS certificate store, which a bundled Python may not have.
    cmd = ["/usr/bin/curl", "-fsSL", "--max-time", str(timeout), url]
    if out is not None:
        cmd += ["-o", str(out)]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    if r.returncode != 0:
        raise RuntimeError(f"Download failed ({url}): {r.stderr.decode(errors='replace').strip()[:200]}")
    return r.stdout


def latest_info() -> dict:
    """{"version", "sha256", "dmg", "signing", "built_from"} for the newest published build."""
    info = json.loads(_curl(INFO_URL, timeout=20))
    if not info.get("version") or not info.get("sha256"):
        raise RuntimeError("The update information is incomplete.")
    return info


def designated_requirement(app: Path) -> str | None:
    r = subprocess.run(["/usr/bin/codesign", "-d", "-r-", str(app)], capture_output=True, text=True)
    m = re.search(r"designated => (.+)", r.stdout + r.stderr)
    return m.group(1).strip() if m else None


def is_stable(dr: str | None) -> bool:
    return bool(dr) and STABLE_CERT in dr.lower()


def same_signer(current_dr: str | None, new_dr: str | None) -> bool:
    """May the new build replace the current one?

    Once TaskTrace is signed with the project certificate, only builds signed with that same
    certificate may replace it, so a tampered download can never inherit its permissions. Before
    that (ad-hoc or temporary signatures), permissions reset on every update anyway, so any valid
    build may replace it."""
    if not new_dr:
        return False
    if is_stable(current_dr):
        return new_dr == current_dr
    return True


def install(info: dict, app_path: Path, log=print) -> Path:
    """Download, verify and swap in the new build. Returns the installed app path."""
    app_path = Path(app_path)
    if app_path.suffix != ".app" or not app_path.is_dir():
        raise RuntimeError("TaskTrace isn't running from an installed app, so it can't update itself.")
    if not os.access(app_path.parent, os.W_OK):
        raise RuntimeError(f"Can't write to {app_path.parent}. Download the new version from GitHub instead.")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dmg = tmp / "TaskTrace.dmg"
        log("Downloading")
        _curl(info.get("dmg") or f"{RELEASE}/TaskTrace.dmg", dmg)
        digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
        if digest != info["sha256"]:
            raise RuntimeError("The download didn't match its checksum, so it wasn't installed.")

        mount = tmp / "mnt"
        mount.mkdir()
        subprocess.run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount), str(dmg)],
                       check=True, capture_output=True, timeout=120)
        try:
            new_app = mount / "TaskTrace.app"
            subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(new_app)],
                           check=True, capture_output=True, timeout=120)
            if not same_signer(designated_requirement(app_path), designated_requirement(new_app)):
                raise RuntimeError("The new build is signed by a different certificate, so it wasn't installed.")
            staged = app_path.with_name(app_path.name + ".new")
            shutil.rmtree(staged, ignore_errors=True)
            log("Installing")
            subprocess.run(["/usr/bin/ditto", str(new_app), str(staged)], check=True, capture_output=True, timeout=300)
        finally:
            subprocess.run(["/usr/bin/hdiutil", "detach", str(mount), "-force"], capture_output=True, timeout=60)

    old = app_path.with_name(app_path.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    app_path.rename(old)
    try:
        staged.rename(app_path)
    except OSError:
        old.rename(app_path)  # put the working version back
        raise
    shutil.rmtree(old, ignore_errors=True)
    return app_path
