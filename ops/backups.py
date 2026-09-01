"""Verified SQLite and Redis logical backups with guarded restore."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import re

from redis import Redis


RESTORE_CONFIRMATION = "RESTORE QUANT STATE"
_BACKUP_NAME = re.compile(r"quant-backup-\d{8}T\d{6}Z$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        result = str(destination_connection.execute("PRAGMA integrity_check").fetchone()[0])
        if result.lower() != "ok":
            raise RuntimeError(f"backup integrity check failed: {result}")
    finally:
        source_connection.close()
        destination_connection.close()


def create_backup(
    destination_root: str,
    *,
    sqlite_paths: list[str],
    redis_client: Redis | None = None,
    redis_patterns: tuple[str, ...] = ("quant:*", "trader-*"),
) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(destination_root).expanduser().resolve() / f"quant-backup-{timestamp}"
    target.mkdir(parents=True, exist_ok=False)
    files: list[dict] = []
    for raw_path in sqlite_paths:
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            continue
        destination = target / "sqlite" / f"{len(files):02d}-{source.name}"
        _sqlite_backup(source, destination)
        files.append(
            {
                "kind": "sqlite",
                "source": str(source),
                "backup": str(destination.relative_to(target)),
                "sha256": _sha256(destination),
                "size": destination.stat().st_size,
            }
        )
    redis_count = 0
    if redis_client is not None:
        seen: set[bytes | str] = set()
        records: list[dict] = []
        for pattern in redis_patterns:
            for key in redis_client.scan_iter(match=pattern, count=500):
                if key in seen:
                    continue
                seen.add(key)
                dumped = redis_client.dump(key)
                if dumped is None:
                    continue
                key_bytes = key if isinstance(key, bytes) else str(key).encode("utf-8")
                records.append(
                    {
                        "key_b64": base64.b64encode(key_bytes).decode("ascii"),
                        "dump_b64": base64.b64encode(dumped).decode("ascii"),
                        "pttl_ms": int(redis_client.pttl(key)),
                    }
                )
        redis_path = target / "redis.json"
        redis_path.write_text(json.dumps({"records": records}, separators=(",", ":")))
        files.append(
            {
                "kind": "redis",
                "backup": redis_path.name,
                "sha256": _sha256(redis_path),
                "size": redis_path.stat().st_size,
            }
        )
        redis_count = len(records)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "redis_key_count": redis_count,
    }
    manifest_path = target / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return target


def verify_backup(backup_dir: str) -> tuple[bool, list[str]]:
    root = Path(backup_dir).expanduser().resolve()
    try:
        manifest = json.loads((root / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"manifest unavailable: {exc}"]
    errors: list[str] = []
    for item in manifest.get("files", []):
        path = root / item["backup"]
        if not path.exists():
            errors.append(f"missing {path}")
        elif _sha256(path) != item["sha256"]:
            errors.append(f"checksum mismatch {path}")
        elif item["kind"] == "sqlite":
            try:
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
                    result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                if result.lower() != "ok":
                    errors.append(f"SQLite integrity failed {path}: {result}")
            except sqlite3.Error as exc:
                errors.append(f"SQLite unreadable {path}: {exc}")
    return not errors, errors


def prune_backups(destination_root: str, *, retain: int) -> list[str]:
    """Remove only recognized, manifested backup directories beyond retention."""
    root = Path(destination_root).expanduser().resolve()
    candidates = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir()
            and _BACKUP_NAME.fullmatch(path.name)
            and (path / "manifest.json").is_file()
        ),
        reverse=True,
    ) if root.exists() else []
    removed: list[str] = []
    for path in candidates[max(1, int(retain)) :]:
        if path.parent != root:
            raise RuntimeError(f"refusing to prune path outside backup root: {path}")
        shutil.rmtree(path)
        removed.append(str(path))
    return removed


def restore_backup(
    backup_dir: str,
    *,
    confirmation: str,
    redis_client: Redis | None = None,
) -> dict:
    if confirmation != RESTORE_CONFIRMATION:
        raise ValueError(f"confirmation must exactly equal {RESTORE_CONFIRMATION!r}")
    valid, errors = verify_backup(backup_dir)
    if not valid:
        raise ValueError("backup verification failed: " + "; ".join(errors))
    root = Path(backup_dir).expanduser().resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    restored_sqlite: list[str] = []
    pre_restore_copies: list[str] = []
    restored_redis = 0
    for item in manifest["files"]:
        backup = root / item["backup"]
        if item["kind"] == "sqlite":
            destination = Path(item["source"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                safety_copy = destination.with_name(
                    f"{destination.name}.pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                )
                shutil.copy2(destination, safety_copy)
                pre_restore_copies.append(str(safety_copy))
            temporary = destination.with_name(f".{destination.name}.restore")
            shutil.copy2(backup, temporary)
            temporary.replace(destination)
            for suffix in ("-wal", "-shm"):
                destination.with_name(destination.name + suffix).unlink(missing_ok=True)
            restored_sqlite.append(str(destination))
        elif item["kind"] == "redis" and redis_client is not None:
            payload = json.loads(backup.read_text())
            for record in payload.get("records", []):
                key = base64.b64decode(record["key_b64"])
                dumped = base64.b64decode(record["dump_b64"])
                ttl = int(record["pttl_ms"])
                redis_client.restore(key, max(ttl, 0), dumped, replace=True)
                restored_redis += 1
    return {
        "sqlite": restored_sqlite,
        "pre_restore_copies": pre_restore_copies,
        "redis_keys": restored_redis,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "verify", "restore"))
    parser.add_argument("--destination", default="quant/backups")
    parser.add_argument("--backup-dir")
    parser.add_argument("--sqlite", action="append", default=[])
    parser.add_argument("--redis-host", default="127.0.0.1")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--no-redis", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--retain", type=int, default=56, help="verified snapshot directories to retain")
    args = parser.parse_args()
    client = None if args.no_redis else Redis(host=args.redis_host, port=args.redis_port)
    defaults = [
        "quant/data/news.sqlite3",
        "quant/optimize/studies.db",
        "quant/jobs/operations.sqlite3",
        "quant/models/registry.sqlite3",
    ]
    if args.command == "create":
        path = create_backup(args.destination, sqlite_paths=args.sqlite or defaults, redis_client=client)
        print(path)
        valid, errors = verify_backup(str(path))
        if not valid:
            raise SystemExit("new backup failed verification: " + "; ".join(errors))
        removed = prune_backups(args.destination, retain=args.retain)
        if removed:
            print(json.dumps({"pruned": removed}))
    elif args.command == "verify":
        if not args.backup_dir:
            raise SystemExit("--backup-dir is required")
        valid, errors = verify_backup(args.backup_dir)
        print(json.dumps({"valid": valid, "errors": errors}, indent=2))
        raise SystemExit(0 if valid else 2)
    else:
        if not args.backup_dir:
            raise SystemExit("--backup-dir is required")
        print(json.dumps(restore_backup(args.backup_dir, confirmation=args.confirm, redis_client=client), indent=2))


if __name__ == "__main__":
    main()
