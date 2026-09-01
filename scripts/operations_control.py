#!/usr/bin/env python3
"""Issue an audited safety command to a running strategy."""
from __future__ import annotations

import argparse
import json

from quant.ops.state import OperationsStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operations-db", default="quant/jobs/operations.sqlite3")
    parser.add_argument("--target", required=True, help="strategy:<dashboard-job-id>")
    parser.add_argument("--action", choices=("FREEZE_ENTRIES", "RESUME_ENTRIES", "CANCEL_ALL", "FLATTEN", "KILL"), required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    expected = f"{args.action} {args.target}"
    if args.action in {"CANCEL_ALL", "FLATTEN", "KILL"} and args.confirm != expected:
        raise SystemExit(f"--confirm must exactly equal {expected!r}")
    store = OperationsStore(args.operations_db)
    try:
        command = store.request_command(
            args.target,
            args.action,
            args.reason,
            payload={"operator": args.operator},
            dedupe_key=f"manual:{args.action}",
        )
        print(json.dumps(command.__dict__, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
