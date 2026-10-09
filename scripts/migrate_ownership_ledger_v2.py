#!/usr/bin/env python3
"""Dry-run/apply/rollback ownership-ledger schema v2 without broker I/O.

OpenAI Codex (GPT-6), 2026-10-09.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from execution.ownership_ledger_codec import (  # noqa: E402
    convert,
    dumps,
    loads_strict,
    ownership_fingerprint,
    schema_of,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_bytes(path: Path, raw: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


@contextmanager
def _exclusive(lock_path: Path, timeout: float) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    held = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("exclusive migration lock unavailable") from None
                time.sleep(0.05)
        yield
    finally:
        if held:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def inspect(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    source = loads_strict(raw)
    version = schema_of(source)
    canonical = convert(source, 2)
    target = dumps(canonical, target=2)
    if ownership_fingerprint(source) != ownership_fingerprint(canonical):
        raise RuntimeError("ownership invariant changed during conversion")
    return {
        "version": version,
        "symbols": len(source["positions"]),
        "source_sha256": _sha(raw),
        "target_sha256": _sha(target),
        "already_v2": version == 2,
        "source_raw": raw,
        "target_raw": target,
        "fingerprint": ownership_fingerprint(source),
        "expected_v1": convert(source, 1),
        "expected_v2": canonical,
    }


def _public(info: dict[str, object]) -> dict[str, object]:
    private = {"source_raw", "target_raw", "fingerprint", "expected_v1", "expected_v2"}
    return {key: value for key, value in info.items() if key not in private}


def _restore_cas(
    path: Path, archive: Path, expected_post_sha: str, expected_pre_sha: str
) -> None:
    current = path.read_bytes()
    if _sha(current) != expected_post_sha:
        raise RuntimeError(
            "rollback refused: current hash differs from migration postimage"
        )
    original = archive.read_bytes()
    if _sha(original) != expected_pre_sha:
        raise RuntimeError(
            "rollback refused: archive hash differs from manifest preimage"
        )
    _atomic_bytes(path, original)
    if _sha(path.read_bytes()) != expected_pre_sha:
        raise RuntimeError("rollback verification failed")


def apply(
    path: Path, lock_path: Path, archive_dir: Path, timeout: float
) -> dict[str, object]:
    with _exclusive(lock_path, timeout):
        info = inspect(path)
        if info["already_v2"]:
            return _public(info)
        source_raw = info["source_raw"]
        target_raw = info["target_raw"]
        assert isinstance(source_raw, bytes) and isinstance(target_raw, bytes)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive = archive_dir / f"ownership_ledger.v1.{stamp}.json"
        manifest = archive.with_suffix(".manifest.json")
        with open(archive, "xb") as handle:
            handle.write(source_raw)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_dir(archive_dir)
        record = {
            "status": "prepared",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "ledger_path": str(path),
            "archive_path": str(archive),
            "pre_sha256": _sha(source_raw),
            "post_sha256": _sha(target_raw),
        }
        _atomic_bytes(manifest, (json.dumps(record, indent=2) + "\n").encode())
        try:
            _atomic_bytes(path, target_raw)
            current = path.read_bytes()
            if _sha(current) != record["post_sha256"]:
                raise RuntimeError("postimage hash verification failed")
            parsed = loads_strict(current)
            if schema_of(parsed) != 2:
                raise RuntimeError("postimage is not schema v2")
            if parsed != info["expected_v2"]:
                raise RuntimeError("canonical-view invariant mismatch")
            if convert(parsed, 1) != info["expected_v1"]:
                raise RuntimeError("compatibility-view invariant mismatch")
            record["status"] = "applied"
            record["applied_utc"] = datetime.now(timezone.utc).isoformat()
            _atomic_bytes(manifest, (json.dumps(record, indent=2) + "\n").encode())
        except Exception as exc:
            # _atomic_bytes replaces before directory fsync. If that final fsync
            # raises, the function does not return and `replaced` remains False even
            # though the postimage is already visible. Decide from hashes, never from
            # control-flow position, then restore only by compare-and-swap.
            current_sha = _sha(path.read_bytes())
            if current_sha == record["post_sha256"]:
                _restore_cas(
                    path,
                    archive,
                    str(record["post_sha256"]),
                    str(record["pre_sha256"]),
                )
            elif current_sha != record["pre_sha256"]:
                raise RuntimeError(
                    "migration failed with an unrecognized current ledger hash"
                ) from exc
            raise
        return {
            "version": 2,
            "symbols": info["symbols"],
            "source_sha256": info["source_sha256"],
            "target_sha256": info["target_sha256"],
            "already_v2": False,
            "archive": str(archive),
            "manifest": str(manifest),
        }


def rollback(
    path: Path, lock_path: Path, manifest_path: Path, timeout: float
) -> dict[str, object]:
    with _exclusive(lock_path, timeout):
        manifest = loads_strict(manifest_path.read_bytes())
        archive = Path(str(manifest["archive_path"]))
        original = archive.read_bytes()
        if _sha(original) != manifest["pre_sha256"]:
            raise RuntimeError("archive hash mismatch")
        _restore_cas(
            path,
            archive,
            str(manifest["post_sha256"]),
            str(manifest["pre_sha256"]),
        )
        return {"status": "rolled_back", "sha256": _sha(path.read_bytes())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger", type=Path, default=ROOT / "data/state/ownership_ledger.json"
    )
    parser.add_argument(
        "--lock", type=Path, default=ROOT / "data/state/.ledger.migration.lock"
    )
    parser.add_argument(
        "--archive-dir", type=Path, default=ROOT / "data/state/ledger_migrations"
    )
    parser.add_argument("--lock-timeout", type=float, default=5.0)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", type=Path, metavar="MANIFEST")
    args = parser.parse_args()
    try:
        result: dict[str, object]
        if args.rollback:
            result = rollback(args.ledger, args.lock, args.rollback, args.lock_timeout)
        elif args.apply:
            result = apply(args.ledger, args.lock, args.archive_dir, args.lock_timeout)
        else:
            result = _public(inspect(args.ledger))
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:  # CLI boundary: fail closed and nonzero
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
