#!/usr/bin/env python3
"""Generate or explicitly install a macOS launchd watchdog service."""
from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys


def build_plist(*, label: str, python: str, config: str, operations_db: str, workdir: str, log_path: str) -> dict:
    return {
        "Label": label,
        "ProgramArguments": [python, "-m", "quant.ops.watchdog", "--config", config, "--operations-db", operations_db],
        "WorkingDirectory": workdir,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--operations-db", required=True)
    parser.add_argument("--label", default="com.local.quant.watchdog")
    parser.add_argument("--output", default="quant/ops/launchd/com.local.quant.watchdog.plist")
    parser.add_argument("--log", default="quant/jobs/watchdog-launchd.log")
    parser.add_argument("--install", action="store_true", help="copy into ~/Library/LaunchAgents and bootstrap it")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    plist = build_plist(
        label=args.label,
        python=sys.executable,
        config=str(Path(args.config).expanduser().resolve()),
        operations_db=str(Path(args.operations_db).expanduser().resolve()),
        workdir=str(root.parent),
        log_path=str(Path(args.log).expanduser().resolve()),
    )
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
    subprocess.run(["launchctl", "bootout", f"gui/{__import__('os').getuid()}", str(destination)], check=False)
    subprocess.run(["launchctl", "bootstrap", f"gui/{__import__('os').getuid()}", str(destination)], check=True)
    print(destination)


if __name__ == "__main__":
    main()
