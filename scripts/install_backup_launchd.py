#!/usr/bin/env python3
"""Generate or explicitly install a scheduled macOS state-backup job."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, help="prefer an encrypted off-host/synced volume")
    parser.add_argument("--interval-seconds", type=int, default=21_600)
    parser.add_argument("--label", default="com.local.quant.backup")
    parser.add_argument("--output", default="quant/ops/launchd/com.local.quant.backup.plist")
    parser.add_argument("--log", default="quant/jobs/backup-launchd.log")
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    package_root = Path(__file__).resolve().parents[1]
    workspace = package_root.parent
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": args.label,
        "ProgramArguments": [
            sys.executable,
            "-m",
            "quant.ops.backups",
            "create",
            "--destination",
            str(Path(args.destination).expanduser().resolve()),
        ],
        "WorkingDirectory": str(workspace),
        "RunAtLoad": False,
        "StartInterval": max(300, args.interval_seconds),
        "ProcessType": "Background",
        "StandardOutPath": str(Path(args.log).expanduser().resolve()),
        "StandardErrorPath": str(Path(args.log).expanduser().resolve()),
    }
    with output.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=True)
    print(output)
    if not args.install:
        return
    if sys.platform != "darwin" or shutil.which("launchctl") is None:
        raise SystemExit("--install requires macOS launchctl")
    destination = Path.home() / "Library" / "LaunchAgents" / f"{args.label}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(output, destination)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(destination)], check=False)
    subprocess.run(["launchctl", "bootstrap", domain, str(destination)], check=True)
    print(destination)


if __name__ == "__main__":
    main()
