"""Exercise the in-app updater on the build machine, end to end, against the freshly built .dmg.

    python update_check.py dist/TaskTrace.app TaskTrace.dmg

1. Installs the new build over a copy of itself (same certificate): must succeed and keep the signature.
2. Tries to install a build signed by a different certificate over it: must be refused.
3. Tries a download whose checksum doesn't match: must be refused.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tasktrace import updater


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main(app: str, dmg: str) -> int:
    app, dmg = Path(app).resolve(), Path(dmg).resolve()
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        installed = tmp / "Applications" / "TaskTrace.app"
        installed.parent.mkdir()
        subprocess.run(["ditto", str(app), str(installed)], check=True)
        before = updater.designated_requirement(installed)

        info = {"version": "99.0.0", "sha256": sha(dmg), "dmg": dmg.as_uri()}
        updater.install(info, installed, log=lambda m: None)
        after = updater.designated_requirement(installed)
        results["same_certificate_update"] = {"ok": installed.is_dir() and before == after,
                                              "requirement": after}

        # A build signed by someone else (ad-hoc here) must never replace a certificate-signed install.
        other = tmp / "other"
        other.mkdir()
        subprocess.run(["ditto", str(app), str(other / "TaskTrace.app")], check=True)
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(other / "TaskTrace.app")],
                       check=True, capture_output=True)
        other_dmg = tmp / "other.dmg"
        subprocess.run(["hdiutil", "create", "-volname", "TaskTrace", "-srcfolder", str(other), "-ov",
                        "-format", "UDZO", str(other_dmg)], check=True, capture_output=True)
        try:
            updater.install({"version": "99.0.1", "sha256": sha(other_dmg), "dmg": other_dmg.as_uri()}, installed,
                            log=lambda m: None)
            results["different_certificate_refused"] = {"ok": "cdhash" in (before or "")}
        except RuntimeError as exc:
            results["different_certificate_refused"] = {"ok": "different certificate" in str(exc)}

        try:
            updater.install({"version": "99.0.2", "sha256": "0" * 64, "dmg": dmg.as_uri()}, installed,
                            log=lambda m: None)
            results["bad_checksum_refused"] = {"ok": False}
        except RuntimeError as exc:
            results["bad_checksum_refused"] = {"ok": "checksum" in str(exc)}

        results["install_still_intact"] = {"ok": updater.designated_requirement(installed) == before}
    print(json.dumps(results, indent=1))
    return 0 if all(r["ok"] for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:3]))
