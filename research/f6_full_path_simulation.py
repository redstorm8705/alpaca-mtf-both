#!/usr/bin/env python3
# ruff: noqa: E402, E501
"""Hermetic, source-bound launch simulation for the Forever-6 tier.

The adapter in this module records orders in memory and has no network client.
Production F6 orchestration is exercised against temporary journals.  The live
feature flag is never written and must be false before and after the run.

Signed: OpenAI Codex, 2026-10-23.
"""

from __future__ import annotations

import argparse
import builtins
import ctypes
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

import config
import run_ledger_sync
from execution import broker as broker_module
from execution import forever_hold_manager as f6
from execution import order_admission
from execution import ownership_guard
from execution import pending_protected_buys as ppb
from execution import risk_manager
from execution import tier_capital_allocator as allocator
from execution.f6_cycle import run_f6_after_close
from execution.ownership_ledger_codec import dumps as dump_ledger


_BOUND_PRODUCTION_DIRS = ("data", "events", "execution", "reporting", "strategy")

_HERMETIC_AUDIT_DEPTH = 0
_BLOCKED_AUDIT_EVENTS = frozenset(
    {
        "ctypes.dlopen",
        "ctypes.dlsym",
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.system",
        "subprocess.Popen",
    }
)


def _hermetic_audit_hook(event: str, _args: tuple[Any, ...]) -> None:
    if _HERMETIC_AUDIT_DEPTH and event in _BLOCKED_AUDIT_EVENTS:
        raise RuntimeError(f"F6 simulator blocked audited process event: {event}")


sys.addaudithook(_hermetic_audit_hook)

PROCESS_LAUNCH_APIS = tuple(
    name
    for name in (
        "system",
        "popen",
        "fork",
        "forkpty",
        "posix_spawn",
        "posix_spawnp",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
    )
    if hasattr(os, name)
)


def _is_metadata_sidecar(path: Path) -> bool:
    """Exclude macOS AppleDouble files that can masquerade as Python sources."""
    return any(part.startswith("._") for part in path.relative_to(ROOT).parts)


_bound_paths = {
    path
    for path in ROOT.glob("*.py")
    if path.is_file() and not _is_metadata_sidecar(path)
}
for _directory in _BOUND_PRODUCTION_DIRS:
    _bound_paths.update(
        path
        for path in (ROOT / _directory).rglob("*.py")
        if path.is_file() and not _is_metadata_sidecar(path)
    )
_bound_paths.update(
    {
        ROOT / "research" / "f6_full_path_simulation.py",
        ROOT / "tests" / "test_f6_cycle.py",
        ROOT / "tests" / "test_f6_full_path_simulation.py",
        ROOT / "tests" / "test_run_cycle_kill_switch_exits.py",
    }
)
# Bind the proof to every production Python module, rather than maintaining a
# hand-curated import list that can silently miss a late or transitive import.
SOURCE_FILES = tuple(sorted(str(path.relative_to(ROOT)) for path in _bound_paths))

REQUIRED_SCENARIOS = frozenset(
    {
        "startup_clean",
        "ownership_fault_blocks",
        "cash_floor_blocks",
        "etf_blocks_before_reservation",
        "empirical_trigger_plans",
        "cross_cycle_quote_liveness",
        "bounded_entry_reserved",
        "restart_pending_order_blocks",
        "entry_fill_ownership_replay",
        "restart_reconcile",
        "trim_reserved_not_assumed_filled",
        "trim_fill_ownership_replay",
        "partial_trim_fail_closed_then_terminal_replay",
        "typed_kill_verdict",
        "order_state_matrix",
        "shared_stop_collision_preserved",
        "network_escape_refused",
        "filesystem_escape_refused",
        "process_state_restored",
        "source_bytes_unchanged",
    }
)

REQUIRED_FILESYSTEM_REFUSALS = frozenset(
    {
        "subprocess_write",
        "native_loader",
        "builtin_open",
        "path_write_text",
        "path_mkdir",
        "path_unlink",
        "path_replace",
        "os_symlink",
        "os_hardlink",
        "os_open_dir_fd",
        "os_mkdir_dir_fd",
        "os_unlink_dir_fd",
        "os_rename_dir_fd",
        "os_replace_dir_fd",
        "os_symlink_dir_fd",
        "os_hardlink_dir_fd",
        "path_symlink",
        "path_hardlink",
    } | {f"process_{name}" for name in PROCESS_LAUNCH_APIS}
)


def _scenario_rows_valid(scenarios: Any) -> bool:
    """Require one PASS row for every scenario; duplicates cannot hide failures."""
    if not isinstance(scenarios, list) or len(scenarios) != len(REQUIRED_SCENARIOS):
        return False
    if not all(isinstance(row, dict) for row in scenarios):
        return False
    names = [str(row.get("name") or "") for row in scenarios]
    return (
        set(names) == set(REQUIRED_SCENARIOS)
        and len(names) == len(set(names))
        and all(row.get("status") == "PASS" for row in scenarios)
    )


def _order_transcript_valid(rows: Any) -> bool:
    """Require the exact four expected submissions and unique client IDs."""
    if not isinstance(rows, list) or len(rows) != 4:
        return False
    if not all(isinstance(row, dict) for row in rows):
        return False
    expected = (
        {"kind": "entry", "symbol": "NVDA", "side": "buy", "qty": 1, "limit": 100.0},
        {"kind": "trim", "symbol": "NVDA", "side": "sell", "qty": 1},
        {"kind": "trim", "symbol": "NVDA", "side": "sell", "qty": 2},
        {"kind": "entry", "symbol": "NVDA", "side": "buy", "qty": 2, "limit": 100.0},
    )
    for row, facts in zip(rows, expected, strict=True):
        if any(row.get(key) != value for key, value in facts.items()):
            return False
        if isinstance(row.get("qty"), bool):
            return False
        if set(row) != set(facts) | {"client_order_id"}:
            return False
    client_ids = [row.get("client_order_id") for row in rows]
    return all(isinstance(value, str) and value for value in client_ids) and len(
        client_ids
    ) == len(set(client_ids))


def _artifact_runtime_evidence_valid(artifact: dict[str, Any]) -> bool:
    """Validate non-source launch facts that must survive serialization."""
    scenarios = artifact.get("scenarios")
    by_name = {
        row.get("name"): row
        for row in scenarios
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    } if isinstance(scenarios, list) else {}
    entry_raw = (by_name.get("entry_fill_ownership_replay") or {}).get(
        "evidence", {}
    )
    trim_raw = (by_name.get("trim_fill_ownership_replay") or {}).get(
        "evidence", {}
    )
    partial_raw = (
        by_name.get("partial_trim_fail_closed_then_terminal_replay") or {}
    ).get("evidence", {})
    entry = entry_raw if isinstance(entry_raw, dict) else {}
    trim = trim_raw if isinstance(trim_raw, dict) else {}
    partial = partial_raw if isinstance(partial_raw, dict) else {}
    filesystem_raw = (
        by_name.get("filesystem_escape_refused") or {}
    ).get("evidence", {})
    filesystem = filesystem_raw if isinstance(filesystem_raw, dict) else {}
    refusals_raw = filesystem.get("refused")
    refusals = refusals_raw if isinstance(refusals_raw, dict) else {}
    return bool(
        artifact.get("network_capable") is False
        and artifact.get("production_flag_remained_disabled") is True
        and _order_transcript_valid(artifact.get("order_transcript"))
        and type(artifact.get("final_f6_qty")) in (int, float)
        and artifact.get("final_f6_qty") == 3.0
        and entry.get("f6_qty") == 4.0
        and trim.get("final_f6_qty") == 3.0
        and partial.get("partial_f6_qty") == 8.0
        and partial.get("completed_f6_qty") == 6.0
        and partial.get("new_orders_after_restart") == 0
        and set(refusals) == set(REQUIRED_FILESYSTEM_REFUSALS)
        and all(refusals.get(name) is True for name in REQUIRED_FILESYSTEM_REFUSALS)
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_head() -> str:
    run = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return run.stdout.strip() if run.returncode == 0 else os.getenv("F6_SIM_SOURCE_COMMIT", "uncommitted")


def _source_hashes() -> dict[str, str]:
    return {name: _sha256(ROOT / name) for name in SOURCE_FILES}


def _source_tree_digest(hashes: dict[str, str]) -> str:
    payload = json.dumps(hashes, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_bound_source_name(name: str) -> bool:
    if not name.endswith(".py") or any(
        part.startswith("._") for part in name.split("/")
    ):
        return False
    if "/" not in name:
        return True
    if name.startswith(tuple(f"{part}/" for part in _BOUND_PRODUCTION_DIRS)):
        return True
    return name in {
        "research/f6_full_path_simulation.py",
        "tests/test_f6_cycle.py",
        "tests/test_f6_full_path_simulation.py",
        "tests/test_run_cycle_kill_switch_exits.py",
    }


def _git_source_hashes(revision: str = "HEAD") -> dict[str, str] | None:
    """Hash bound blobs from a Git tree, independent of index/worktree bytes."""
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", revision],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return None
    names = sorted(
        name for name in listed.stdout.splitlines() if _is_bound_source_name(name)
    )
    hashes: dict[str, str] = {}
    for name in names:
        shown = subprocess.run(
            ["git", "show", f"{revision}:{name}"],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        if shown.returncode != 0:
            return None
        hashes[name] = hashlib.sha256(shown.stdout).hexdigest()
    return hashes


def _git_index_source_hashes() -> dict[str, str] | None:
    """Hash bound blobs from the Git index, independent of working-tree bytes."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return None
    names = sorted(
        name for name in listed.stdout.splitlines() if _is_bound_source_name(name)
    )
    hashes: dict[str, str] = {}
    for name in names:
        shown = subprocess.run(
            ["git", "show", f":{name}"],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        if shown.returncode != 0:
            return None
        hashes[name] = hashlib.sha256(shown.stdout).hexdigest()
    return hashes


def _artifact_source_binding_valid(
    artifact: dict[str, Any],
    *,
    head_sources: dict[str, str],
    index_sources: dict[str, str],
    working_sources: dict[str, str],
) -> bool:
    """Require evidence for HEAD blobs and clean bound index/worktree bytes."""
    return bool(
        head_sources
        and index_sources == head_sources
        and working_sources == head_sources
        and artifact.get("source_sha256_before") == head_sources
        and artifact.get("source_sha256_after") == head_sources
        and artifact.get("source_tree_sha256") == _source_tree_digest(head_sources)
    )


def _inside(path: Any, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except (OSError, TypeError, ValueError):
        return False


@contextmanager
def _hermetic_guards(write_root: Path):
    """Deny transport and any write outside the simulation's temporary state."""
    global _HERMETIC_AUDIT_DEPTH
    real_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open
    real_replace = os.replace
    real_mkdir = os.mkdir
    real_unlink = os.unlink
    real_rename = os.rename
    real_symlink = os.symlink
    real_link = os.link
    real_path_open = Path.open
    real_path_mkdir = Path.mkdir
    real_path_unlink = Path.unlink
    real_path_replace = Path.replace
    real_path_symlink_to = Path.symlink_to
    real_path_hardlink_to = Path.hardlink_to

    def resolved_at(path: Any, dir_fd: Any = None) -> Path:
        candidate = Path(path)
        if candidate.is_absolute() or dir_fd is None:
            return candidate
        try:
            fd = int(dir_fd)
        except (TypeError, ValueError) as exc:
            raise PermissionError(
                f"simulation cannot verify descriptor-relative path: {path}"
            ) from exc
        base: str | None = None
        for fd_path in (f"/proc/self/fd/{fd}", f"/dev/fd/{fd}"):
            try:
                base = os.readlink(fd_path)
                break
            except OSError:
                continue
        if base is None:
            raise PermissionError(
                f"simulation cannot resolve dir_fd={fd} for path: {path}"
            )
        return Path(base) / candidate

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")) and not _inside(file, write_root):
            raise PermissionError(f"simulation write escaped sandbox: {file}")
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(file: Any, flags: int, *args: Any, **kwargs: Any):
        writes = flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC)
        resolved = resolved_at(file, kwargs.get("dir_fd"))
        if writes and not _inside(resolved, write_root):
            raise PermissionError(f"simulation os.open escaped sandbox: {file}")
        return real_os_open(file, flags, *args, **kwargs)

    def guarded_io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")) and not _inside(file, write_root):
            raise PermissionError(f"simulation io.open escaped sandbox: {file}")
        return real_io_open(file, mode, *args, **kwargs)

    def guarded_replace(src: Any, dst: Any, *args: Any, **kwargs: Any):
        resolved_src = resolved_at(src, kwargs.get("src_dir_fd"))
        resolved_dst = resolved_at(dst, kwargs.get("dst_dir_fd"))
        if not (_inside(resolved_src, write_root) and _inside(resolved_dst, write_root)):
            raise PermissionError(f"simulation replace escaped sandbox: {src} -> {dst}")
        return real_replace(src, dst, *args, **kwargs)

    def guarded_mkdir(path: Any, *args: Any, **kwargs: Any):
        resolved = resolved_at(path, kwargs.get("dir_fd"))
        if not _inside(resolved, write_root):
            raise PermissionError(f"simulation mkdir escaped sandbox: {path}")
        return real_mkdir(path, *args, **kwargs)

    def guarded_unlink(path: Any, *args: Any, **kwargs: Any):
        resolved = resolved_at(path, kwargs.get("dir_fd"))
        if not _inside(resolved, write_root):
            raise PermissionError(f"simulation unlink escaped sandbox: {path}")
        return real_unlink(path, *args, **kwargs)

    def guarded_rename(src: Any, dst: Any, *args: Any, **kwargs: Any):
        resolved_src = resolved_at(src, kwargs.get("src_dir_fd"))
        resolved_dst = resolved_at(dst, kwargs.get("dst_dir_fd"))
        if not (_inside(resolved_src, write_root) and _inside(resolved_dst, write_root)):
            raise PermissionError(f"simulation rename escaped sandbox: {src} -> {dst}")
        return real_rename(src, dst, *args, **kwargs)

    def guarded_symlink(src: Any, dst: Any, *args: Any, **kwargs: Any):
        resolved_dst = resolved_at(dst, kwargs.get("dir_fd"))
        source = Path(src)
        if not source.is_absolute():
            source = resolved_dst.parent / source
        if not (_inside(source, write_root) and _inside(resolved_dst, write_root)):
            raise PermissionError(f"simulation symlink escaped sandbox: {src} -> {dst}")
        return real_symlink(src, dst, *args, **kwargs)

    def guarded_link(src: Any, dst: Any, *args: Any, **kwargs: Any):
        resolved_src = resolved_at(src, kwargs.get("src_dir_fd"))
        resolved_dst = resolved_at(dst, kwargs.get("dst_dir_fd"))
        if not (_inside(resolved_src, write_root) and _inside(resolved_dst, write_root)):
            raise PermissionError(f"simulation hardlink escaped sandbox: {src} -> {dst}")
        return real_link(src, dst, *args, **kwargs)

    def guarded_path_open(
        path: Path, mode: str = "r", *args: Any, **kwargs: Any
    ):
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")) and not _inside(path, write_root):
            raise PermissionError(f"simulation Path.open escaped sandbox: {path}")
        return real_path_open(path, mode, *args, **kwargs)

    def guarded_path_mkdir(path: Path, *args: Any, **kwargs: Any):
        if not _inside(path, write_root):
            raise PermissionError(f"simulation Path.mkdir escaped sandbox: {path}")
        return real_path_mkdir(path, *args, **kwargs)

    def guarded_path_unlink(path: Path, *args: Any, **kwargs: Any):
        if not _inside(path, write_root):
            raise PermissionError(f"simulation Path.unlink escaped sandbox: {path}")
        return real_path_unlink(path, *args, **kwargs)

    def guarded_path_replace(path: Path, target: Any, *args: Any, **kwargs: Any):
        if not (_inside(path, write_root) and _inside(target, write_root)):
            raise PermissionError(f"simulation Path.replace escaped sandbox: {path} -> {target}")
        return real_path_replace(path, target, *args, **kwargs)

    def guarded_path_symlink_to(
        path: Path, target: Any, *args: Any, **kwargs: Any
    ):
        source = Path(target)
        if not source.is_absolute():
            source = path.parent / source
        if not (_inside(path, write_root) and _inside(source, write_root)):
            raise PermissionError(
                f"simulation Path.symlink_to escaped sandbox: {path} -> {target}"
            )
        return real_path_symlink_to(path, target, *args, **kwargs)

    def guarded_path_hardlink_to(
        path: Path, target: Any, *args: Any, **kwargs: Any
    ):
        if not (_inside(path, write_root) and _inside(target, write_root)):
            raise PermissionError(
                f"simulation Path.hardlink_to escaped sandbox: {path} -> {target}"
            )
        return real_path_hardlink_to(path, target, *args, **kwargs)

    def guarded_popen(*args: Any, **kwargs: Any):
        raise RuntimeError(
            "subprocess creation disabled by F6 simulator: "
            f"{args[0] if args else kwargs.get('args')}"
        )

    def guarded_process_launch(*args: Any, **kwargs: Any):
        raise RuntimeError(
            "process creation disabled by F6 simulator: "
            f"{args[0] if args else kwargs.get('path')}"
        )

    _HERMETIC_AUDIT_DEPTH += 1
    try:
        with ExitStack() as process_guards:
            for name in PROCESS_LAUNCH_APIS:
                process_guards.enter_context(
                    mock.patch.object(os, name, guarded_process_launch)
                )
            patches = (
            mock.patch.object(builtins, "open", guarded_open),
            mock.patch.object(io, "open", guarded_io_open),
            mock.patch.object(os, "open", guarded_os_open),
            mock.patch.object(os, "replace", guarded_replace),
            mock.patch.object(os, "mkdir", guarded_mkdir),
            mock.patch.object(os, "unlink", guarded_unlink),
            mock.patch.object(os, "remove", guarded_unlink),
            mock.patch.object(os, "rename", guarded_rename),
            mock.patch.object(os, "symlink", guarded_symlink),
            mock.patch.object(os, "link", guarded_link),
            mock.patch.object(Path, "open", guarded_path_open),
            mock.patch.object(Path, "mkdir", guarded_path_mkdir),
            mock.patch.object(Path, "unlink", guarded_path_unlink),
            mock.patch.object(Path, "replace", guarded_path_replace),
            mock.patch.object(Path, "symlink_to", guarded_path_symlink_to),
            mock.patch.object(Path, "hardlink_to", guarded_path_hardlink_to),
            mock.patch.object(subprocess, "Popen", guarded_popen),
            mock.patch.object(
                socket,
                "socket",
                side_effect=RuntimeError("network disabled by F6 simulator"),
            ),
            mock.patch.object(
                socket,
                "create_connection",
                side_effect=RuntimeError("network disabled by F6 simulator"),
            ),
            mock.patch.object(
                broker_module,
                "TradingClient",
                side_effect=RuntimeError("SDK client disabled by F6 simulator"),
            ),
            )
            for patch in patches:
                process_guards.enter_context(patch)
            yield
    finally:
        _HERMETIC_AUDIT_DEPTH -= 1


def _bars(as_of: datetime) -> pd.DataFrame:
    index = pd.date_range("2024-01-02", as_of.date().isoformat(), freq="B", tz=f6.ET)
    close = pd.Series(100.0, index=index)
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000.0,
        }
    )
    frame.iloc[-1] = [100.0, 101.0, 88.0, 90.0, 10_000_000.0]
    return frame


def _ledger(
    qty: float,
    avg_cost: float = 0.0,
    *,
    qhm_qty: float = 0.0,
    day_qty: float = 0.0,
) -> dict[str, Any]:
    if qty <= 0 and qhm_qty <= 0 and day_qty == 0:
        return {
            "version": 2,
            "last_reconciled_utc": datetime.now(timezone.utc).isoformat(),
            "positions": {},
        }
    net = qty + qhm_qty + day_qty
    return {
        "version": 2,
        "last_reconciled_utc": datetime.now(timezone.utc).isoformat(),
        "positions": {
            "NVDA": {
                "alpaca_net_qty": net,
                "drift": 0.0,
                "tiers": {
                    "day": {
                        "qty": day_qty,
                        "avg_cost": 100.0 if day_qty else 0.0,
                        "last_fill_id": "sim-day-fill" if day_qty else None,
                    },
                    "swing": {"qty": 0.0, "avg_cost": 0.0, "last_fill_id": None},
                    "qhm": {
                        "qty": qhm_qty,
                        "avg_cost": avg_cost if qhm_qty else 0.0,
                        "last_fill_id": "sim-qhm-seed" if qhm_qty else None,
                    },
                    "forever_6": {
                        "qty": qty,
                        "avg_cost": avg_cost,
                        "last_fill_id": "sim-f6-seed" if qty else None,
                    },
                },
            }
        },
    }


class OfflineBroker:
    """In-memory Alpaca-shaped adapter; deliberately has no transport field."""

    network_capable = False

    def __init__(self) -> None:
        self.cash = 1_000.0
        self.account = SimpleNamespace(
            equity="5000.0",
            cash="1000.0",
            buying_power="1000.0",
            maintenance_margin="0.0",
        )
        self.orders: dict[str, SimpleNamespace] = {}
        self.order_calls: list[dict[str, Any]] = []
        self.submission_errors: list[str] = []
        self.cancelled_order_ids: list[str] = []
        self.position_qty = 3.0
        self.asset = SimpleNamespace(
            symbol="NVDA",
            status="active",
            tradable=True,
            asset_class="us_equity",
            name="NVIDIA Corporation Common Stock",
        )
        seed = SimpleNamespace(
            id="sim-seed-order",
            client_order_id="F6-NVDA-b-seed",
            symbol="NVDA",
            side="buy",
            qty="3",
            filled_qty="3",
            status="filled",
            type="limit",
            time_in_force="day",
            limit_price="20.00",
            filled_avg_price="20.00",
            extended_hours=True,
        )
        self.orders[seed.id] = seed
        self.fills: list[dict[str, Any]] = [
            {
                "id": "sim-seed-fill",
                "order_id": seed.id,
                "symbol": "NVDA",
                "side": "buy",
                "qty": "3",
                "price": "20.00",
                "transaction_time": "2026-10-01T20:00:00Z",
            }
        ]

    def get_account(self) -> SimpleNamespace:
        self.account.cash = str(self.cash)
        return self.account

    def get_open_positions(self) -> list[SimpleNamespace]:
        if self.position_qty <= 0:
            return []
        return [
            SimpleNamespace(
                symbol="NVDA",
                qty=str(self.position_qty),
                side="long",
                current_price="100.0",
                market_value=str(self.position_qty * 100.0),
            )
        ]

    def get_trading_client(self) -> "OfflineBroker":
        return self

    def get_asset(self, symbol: str) -> SimpleNamespace:
        if symbol != "NVDA":
            raise LookupError(symbol)
        return self.asset

    def get_open_orders(self, *_args: Any, **_kwargs: Any) -> list[SimpleNamespace]:
        return [
            order
            for order in self.orders.values()
            if str(order.status) in {"accepted", "new", "partially_filled"}
        ]

    def raw_orders(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": order.id,
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "side": order.side,
                "qty": order.qty,
                "filled_qty": order.filled_qty,
                "status": order.status,
                "type": order.type,
                "time_in_force": order.time_in_force,
                "limit_price": order.limit_price,
                "filled_avg_price": order.filled_avg_price,
                "extended_hours": order.extended_hours,
            }
            for order in self.orders.values()
        ]

    def raw_fills(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.fills]

    def raw_positions(self) -> list[dict[str, Any]]:
        if self.position_qty <= 0:
            return []
        return [
            {
                "symbol": "NVDA",
                "qty": str(self.position_qty),
                "avg_entry_price": "20.0",
            }
        ]

    def fill(
        self,
        order_id: str,
        *,
        price: float,
        qty: int = 1,
        terminal: bool = True,
    ) -> None:
        order = self.orders[order_id]
        cumulative = int(float(order.filled_qty or 0)) + qty
        order.status = "filled" if terminal else "partially_filled"
        order.filled_qty = str(cumulative)
        order.filled_avg_price = f"{price:.2f}"
        self.position_qty += qty if order.side == "buy" else -qty
        self.fills.append(
            {
                "id": f"sim-fill-{len(self.fills)}",
                "order_id": order.id,
                "symbol": order.symbol,
                "side": order.side,
                "qty": str(qty),
                "price": f"{price:.2f}",
                "transaction_time": datetime.now(timezone.utc).isoformat(),
            }
        )

    def get_order(self, order_id: str, *_args: Any, **_kwargs: Any) -> SimpleNamespace | None:
        return self.orders.get(str(order_id))

    def get_order_by_client_order_id(self, coid: str) -> SimpleNamespace | None:
        return next(
            (order for order in self.orders.values() if order.client_order_id == coid),
            None,
        )

    def get_open_position(self, symbol: str) -> SimpleNamespace:
        if symbol != "NVDA" or self.position_qty <= 0:
            return None  # type: ignore[return-value]
        return SimpleNamespace(symbol=symbol, qty=str(self.position_qty), side="long")

    def submit_order(self, request: Any) -> SimpleNamespace:
        if self.submission_errors:
            raise RuntimeError(self.submission_errors.pop(0))
        symbol = str(request.symbol).upper()
        qty = int(float(request.qty))
        side = str(getattr(request.side, "value", request.side)).lower()
        client_order_id = str(request.client_order_id)
        if any(
            order.client_order_id == client_order_id
            for order in self.orders.values()
        ):
            raise RuntimeError(f"duplicate client_order_id: {client_order_id}")
        limit_raw = getattr(request, "limit_price", None)
        limit = float(limit_raw) if limit_raw is not None else None
        kind = "entry" if side == "buy" else "trim"
        call = {
            "kind": kind,
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "client_order_id": client_order_id,
        }
        if limit is not None:
            call["limit"] = limit
        self.order_calls.append(call)
        sequence = sum(1 for row in self.order_calls if row["kind"] == kind)
        order_id = f"sim-{kind}-{sequence}"
        order = SimpleNamespace(
            id=order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            qty=str(qty),
            filled_qty="0",
            status="accepted",
            type="limit" if limit is not None else "market",
            time_in_force="day",
            limit_price=f"{limit:.2f}" if limit is not None else None,
            filled_avg_price=None,
            extended_hours=bool(getattr(request, "extended_hours", False)),
        )
        self.orders[order.id] = order
        return order

    def cancel_order_by_id(self, order_id: str) -> None:
        order = self.orders[str(order_id)]
        order.status = "canceled"
        self.cancelled_order_ids.append(str(order_id))


def _scenario(name: str, passed: bool, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "evidence": evidence}


def _run_simulation_inner(
    state: Path,
    *,
    source_commit: str,
    inject_failure: bool = False,
) -> dict[str, Any]:
    if config.FOREVER6_ENABLED is not False:
        raise RuntimeError("production FOREVER6_ENABLED must be false before simulation")

    broker = OfflineBroker()
    scenarios: list[dict[str, Any]] = []
    now = datetime.combine(datetime.now(f6.ET).date(), datetime.min.time(), f6.ET).replace(hour=17)
    quote_times = iter((now - timedelta(seconds=2), now - timedelta(seconds=1), now))
    idem_counts: dict[tuple[str, str], int] = {}

    def sim_idem_id(_tier: str, symbol: str, side: str) -> str:
        normalized_side = str(getattr(side, "value", side)).lower()
        key = (symbol, normalized_side)
        idem_counts[key] = idem_counts.get(key, 0) + 1
        marker = "b" if normalized_side == "buy" else "s"
        return f"F6-{symbol}-{marker}-{idem_counts[key]}-sim"

    def quote(_symbol: str) -> dict[str, Any]:
        return {"bid": 99.99, "ask": 100.0, "ts": next(quote_times).isoformat()}

    capital_allocator = allocator.TierCapitalAllocator(
        broker,
        state_path=state / "capital.json",
        audit_path=state / "capital.jsonl",
        ledger_loader=ownership_guard.load_ledger,
    )

    with ExitStack() as stack:
        patches: tuple[Any, ...] = (
            mock.patch.object(config, "FOREVER6_ENABLED", True),
            mock.patch.object(config, "FOREVER6_UNIVERSE", ["NVDA"]),
            mock.patch.object(config, "FOREVER6_STARTER_MAX_NAMES", 1),
            mock.patch.object(config, "TIER_CAPITAL_FOREVER6_ENABLED", True),
            mock.patch.object(f6, "_STATE", state / "forever6_holds.json"),
            mock.patch.object(f6, "_STATE_LOCK", state / ".forever6_holds.lock"),
            mock.patch.object(f6, "_TRIGGER_EVIDENCE", state / "trigger.jsonl"),
            mock.patch.object(f6, "_SYNC_DEGRADED", state / "sync.json"),
            mock.patch.object(f6, "_TRIM_STATE", state / "trims.json"),
            mock.patch.object(f6, "_TRIM_STATE_LOCK", state / ".trims.lock"),
            mock.patch.object(ppb, "JOURNAL_PATH", state / "pending.json"),
            mock.patch.object(ppb, "LOCK_PATH", state / ".pending.lock"),
            mock.patch.object(order_admission, "_REDUCTION_PATH", state / "reductions.json"),
            mock.patch.object(order_admission, "_REDUCTION_LOCK", state / ".reductions.lock"),
            mock.patch.object(order_admission, "_LOCK_DIR", state / "symbol-locks"),
            mock.patch.object(ownership_guard, "_LEDGER_PATH", state / "ledger.json"),
            mock.patch.object(
                ownership_guard, "_LEDGER_BAK_PATH", state / "ledger.bak.json"
            ),
            mock.patch.object(
                ownership_guard, "_LEDGER_LOCK_PATH", state / ".ledger.lock"
            ),
            mock.patch.object(
                ownership_guard,
                "_LEDGER_MIGRATION_LOCK_PATH",
                state / ".ledger.migration.lock",
            ),
            mock.patch.object(
                risk_manager, "_KILL_STATE_FILE", state / "kill_state.json"
            ),
            mock.patch.object(
                risk_manager, "_KILL_SNAPSHOT_FILE", state / "kill_snapshot.json"
            ),
            mock.patch("reporting.pnl_ledger.fetch_all_fills", broker.raw_fills),
            mock.patch("reporting.pnl_ledger.fetch_all_orders", broker.raw_orders),
            mock.patch("reporting.pnl_ledger.fetch_positions", broker.raw_positions),
            mock.patch(
                "execution.quarterly_hold_manager.get_quarterly_hold_quantities",
                lambda: {},
            ),
            mock.patch("execution.tier_transfers.load_transfers", lambda: []),
            mock.patch.object(run_ledger_sync.time, "sleep", lambda _seconds: None),
            mock.patch.object(run_ledger_sync, "_read_streak", lambda: {"count": 0}),
            mock.patch.object(run_ledger_sync, "_write_streak", lambda _state: None),
            mock.patch.object(run_ledger_sync, "_slack", lambda _message: None),
            mock.patch.object(
                broker_module,
                "_make_idem_id",
                sim_idem_id,
            ),
            mock.patch.object(broker_module, "_get_trading_client", lambda: broker),
            mock.patch.object(broker_module, "get_open_orders", broker.get_open_orders),
            mock.patch.object(broker_module, "get_order", broker.get_order),
            mock.patch.object(broker_module, "get_order_by_client_order_id", broker.get_order_by_client_order_id),
            mock.patch.object(broker_module, "get_open_position", broker.get_open_position),
            mock.patch.object(allocator, "_LIVE_ALLOCATOR", capital_allocator),
            mock.patch.object(
                allocator,
                "_load_regime_evidence",
                lambda: {
                    "regime": "crash",
                    "verified": True,
                    "ts": datetime.now(timezone.utc).isoformat(),
                },
            ),
            mock.patch(
                "execution.overnight_capacity.overnight_k_strict_current",
                lambda: 1.9,
            ),
            mock.patch("events.catalyst_engine.has_blocking_catalyst", lambda _symbol: False),
            mock.patch.object(f6, "_slack_safe", lambda _message: None),
        )
        for patch in patches:
            stack.enter_context(patch)

        ownership_guard._LEDGER_PATH.write_bytes(
            dump_ledger(_ledger(3.0, 20.0), target=2)
        )
        f6._STARTUP_READY = False
        f6._F6_QUOTE_OBSERVATIONS.clear()
        if inject_failure:
            raise RuntimeError("injected F6 simulation failure")
        manager = f6.ForeverHoldManager(broker, clock=lambda: now, quote_loader=quote)
        manager._fetch_prices = lambda: {"NVDA": 100.0}  # type: ignore[method-assign]

        startup = manager.reconcile_on_startup()
        scenarios.append(_scenario("startup_clean", startup.ready, {"reason": startup.reason}))

        unready_calls = len(broker.order_calls)
        f6._STARTUP_READY = False
        unready = manager.execute_starter([{"symbol": "NVDA", "price": 100.0}], 200.0)
        scenarios.append(_scenario(
            "ownership_fault_blocks",
            unready["reason"] == "startup ownership preflight not ready" and len(broker.order_calls) == unready_calls,
            {"reason": unready["reason"], "orders": len(broker.order_calls) - unready_calls},
        ))
        f6._STARTUP_READY = True

        floor_calls = len(broker.order_calls)
        floor = manager.execute_starter(
            [{"symbol": "NVDA", "price": 100.0}], 200.0, settled_cash=100.0
        )
        scenarios.append(_scenario(
            "cash_floor_blocks",
            floor["reason"] == "cash-floor/budget guard" and len(broker.order_calls) == floor_calls,
            {"reason": floor["reason"], "orders": len(broker.order_calls) - floor_calls},
        ))

        etf_calls = len(broker.order_calls)
        with mock.patch.object(config, "FOREVER6_UNIVERSE", ["NVDA", "EWY"]):
            etf = manager.execute_starter(
                [{"symbol": "EWY", "price": 50.0}], 200.0, settled_cash=1_000.0
            )
        scenarios.append(_scenario(
            "etf_blocks_before_reservation",
            etf["reason"] == "no admissible F6 starter names"
            and len(broker.order_calls) == etf_calls
            and not ppb.load()["reservations"],
            {"reason": etf["reason"], "orders": len(broker.order_calls) - etf_calls},
        ))

        bars = _bars(now)
        plan = manager.maybe_start_accumulation(
            bars,
            bars.index[-1].date(),
            price_by_sym={"NVDA": 100.0},
            held_qty_by_sym={},
            settled_cash=1_000.0,
        )
        scenarios.append(_scenario(
            "empirical_trigger_plans",
            plan.get("triggered") is True and [row["symbol"] for row in plan.get("plan", [])] == ["NVDA"],
            {"reason": plan.get("reason"), "symbols": [row["symbol"] for row in plan.get("plan", [])]},
        ))

        first = run_f6_after_close(
            enabled=True,
            is_after_close=True,
            now_et=now,
            block_starter=False,
            fetch_bars=lambda *_args, **_kwargs: bars,
            manager=manager,
        )
        scenarios.append(_scenario(
            "cross_cycle_quote_liveness",
            first["reason"] == "quote feed freshness not yet proven across cycles" and not broker.order_calls,
            {"reason": first["reason"], "orders": len(broker.order_calls)},
        ))

        second = run_f6_after_close(
            enabled=True,
            is_after_close=True,
            now_et=now,
            block_starter=False,
            fetch_bars=lambda *_args, **_kwargs: bars,
            manager=manager,
        )
        pending = ppb.load()["reservations"]
        allocator_state = capital_allocator._load_state() or {}
        scenarios.append(_scenario(
            "bounded_entry_reserved",
            len(second.get("submitted", [])) == 1
            and second["reserved"] == 100.0
            and list(pending.values())[0]["status"] == "submitted"
            and len(allocator_state["reservations"]) == 1,
            {
                "submitted": second.get("submitted"),
                "reservation_status": list(pending.values())[0]["status"],
                "allocator_state": allocator_state,
            },
        ))

        pending_restart = f6.ForeverHoldManager(
            broker, clock=lambda: now, quote_loader=quote
        ).reconcile_on_startup()
        scenarios.append(_scenario(
            "restart_pending_order_blocks",
            not pending_restart.ready
            and "reservation" in pending_restart.reason.lower(),
            {"reason": pending_restart.reason},
        ))

        entry = broker.orders["sim-entry-1"]
        broker.fill(entry.id, price=100.0)
        replay = run_ledger_sync.sync_once()
        entry_reservation = ppb.load()["reservations"][entry.client_order_id]
        scenarios.append(_scenario(
            "entry_fill_ownership_replay",
            replay["ok"] is True
            and entry_reservation["status"] == "released_accounted"
            and ownership_guard.tier_qty(
                ownership_guard.load_ledger(), "NVDA", "forever6"
            )
            == 4.0,
            {
                "sync": replay,
                "reservation_status": entry_reservation["status"],
                "f6_qty": 4.0,
            },
        ))

        restarted = f6.ForeverHoldManager(broker, clock=lambda: now, quote_loader=quote)
        restart = restarted.reconcile_on_startup()
        scenarios.append(_scenario(
            "restart_reconcile",
            restart.ready and restart.symbols_reconciled == ("NVDA",),
            {"reason": restart.reason, "symbols": list(restart.symbols_reconciled)},
        ))

        restarted.universe = ["NVDA"]
        restarted._fetch_prices = lambda: {"NVDA": 400.0}  # type: ignore[method-assign]
        trim_pending_route = run_f6_after_close(
            enabled=True,
            is_after_close=True,
            now_et=now,
            block_starter=True,
            fetch_bars=lambda *_args, **_kwargs: bars,
            manager=restarted,
        )
        trim_pending = trim_pending_route["trim_result"]
        trim = broker.orders["sim-trim-1"]
        scenarios.append(_scenario(
            "trim_reserved_not_assumed_filled",
            trim_pending == {} and restarted._load_trim_state()["NVDA"]["trim1"] == "reserved",
            {"status": restarted._load_trim_state()["NVDA"]["trim1"], "filled_qty": trim.filled_qty},
        ))

        broker.fill(trim.id, price=400.0)

        trim_done_route = run_f6_after_close(
            enabled=True,
            is_after_close=True,
            now_et=now,
            block_starter=True,
            fetch_bars=lambda *_args, **_kwargs: bars,
            manager=restarted,
        )
        trim_done = trim_done_route["trim_result"]
        trim_sync = run_ledger_sync.sync_once()
        trim_state = restarted._load_trim_state()["NVDA"]
        final_ledger = ownership_guard.load_ledger()
        scenarios.append(_scenario(
            "trim_fill_ownership_replay",
            trim_state["trim1"] == "done"
            and final_ledger["positions"]["NVDA"]["tiers"]["forever6"]["qty"] == 3.0
            and ("10x", 1) in trim_done["NVDA"]["fired"],
            {
                "status": trim_state["trim1"],
                "final_f6_qty": 3.0,
                "result": trim_done,
                "sync": trim_sync,
            },
        ))

        before_partial = ownership_guard.load_canonical_ledger()
        partial_seed_order = SimpleNamespace(
            id="sim-partial-seed-order",
            client_order_id="F6-NVDA-b-partial-seed",
            symbol="NVDA",
            side="buy",
            qty="5",
            filled_qty="5",
            status="filled",
            type="limit",
            time_in_force="day",
            limit_price="20.00",
            filled_avg_price="20.00",
            extended_hours=True,
        )
        broker.orders[partial_seed_order.id] = partial_seed_order
        broker.fills.append(
            {
                "id": "sim-partial-seed-fill",
                "order_id": partial_seed_order.id,
                "symbol": "NVDA",
                "side": "buy",
                "qty": "5",
                "price": "20.00",
                "transaction_time": datetime.now(timezone.utc).isoformat(),
            }
        )
        broker.position_qty = 8.0
        run_ledger_sync.sync_once()
        if f6._TRIM_STATE.exists():
            f6._TRIM_STATE.unlink()
        partial_manager = f6.ForeverHoldManager(
            broker, clock=lambda: now, quote_loader=quote
        )
        partial_manager.universe = ["NVDA"]
        partial_manager._fetch_prices = lambda: {  # type: ignore[method-assign]
            "NVDA": 400.0
        }
        f6._STARTUP_READY = True
        partial_manager.evaluate_and_execute_trims()
        partial_trim = broker.orders["sim-trim-2"]
        broker.fill(partial_trim.id, price=400.0, qty=1, terminal=False)
        partial_sync = run_ledger_sync.sync_once()
        partial_qty = ownership_guard.tier_qty(
            ownership_guard.load_ledger(), "NVDA", "forever6"
        )
        calls_before_restart = len(broker.order_calls)
        partial_restart = f6.ForeverHoldManager(
            broker, clock=lambda: now, quote_loader=quote
        )
        partial_restart.universe = ["NVDA"]
        partial_restart._fetch_prices = lambda: {  # type: ignore[method-assign]
            "NVDA": 400.0
        }
        restart_preflight = partial_restart.reconcile_on_startup()
        partial_restart.evaluate_and_execute_trims()
        calls_after_restart = len(broker.order_calls)
        reserved_after_restart = partial_restart._load_trim_state()["NVDA"]["trim1"]
        broker.fill(partial_trim.id, price=400.0, qty=1, terminal=True)
        completed = partial_restart.evaluate_and_execute_trims()
        completed_sync = run_ledger_sync.sync_once()
        completed_qty = ownership_guard.tier_qty(
            ownership_guard.load_ledger(), "NVDA", "forever6"
        )
        scenarios.append(
            _scenario(
                "partial_trim_fail_closed_then_terminal_replay",
                partial_sync["ok"] is False
                and partial_qty == 8.0
                and not restart_preflight.ready
                and reserved_after_restart == "reserved"
                and calls_after_restart == calls_before_restart
                and completed_sync["ok"] is True
                and completed_qty == 6.0
                and partial_restart._load_trim_state()["NVDA"]["trim1"] == "done"
                and ("10x", 2) in completed["NVDA"]["fired"],
                {
                    "partial_f6_qty": partial_qty,
                    "restart_reason": restart_preflight.reason,
                    "reserved_after_restart": reserved_after_restart,
                    "new_orders_after_restart": calls_after_restart
                    - calls_before_restart,
                    "completed_f6_qty": completed_qty,
                    "partial_sync": partial_sync,
                    "completed_sync": completed_sync,
                },
            )
        )
        ownership_guard.save_ledger(before_partial)
        broker.position_qty = 3.0

        final_before_kill = ownership_guard.load_canonical_ledger()
        ownership_guard.save_ledger(_ledger(1.0, 10.0, day_qty=10.0))
        today = datetime.now(timezone.utc).isoformat()
        risk_order = SimpleNamespace(
            id="sim-day-risk-order",
            client_order_id="DT-NVDA-b-1-sim",
            symbol="NVDA",
            side="buy",
            qty="10",
            filled_qty="10",
            status="filled",
        )
        broker.orders[risk_order.id] = risk_order
        day_fill = {
            "id": "sim-day-risk-fill",
            "order_id": risk_order.id,
            "symbol": "NVDA",
            "side": "buy",
            "qty": "10",
            "price": "100.0",
            "transaction_time": today,
        }
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = [
            {
                "symbol": "NVDA",
                "qty": "11",
                "current_price": "93.0",
                "lastday_price": "100.0",
                "unrealized_intraday_pl": "-77.0",
            }
        ]
        risk_manager._intraday_cache.update(
            ts=0.0, pt_date="", fills=(), ok=False
        )
        risk_manager._intraday_order_tier_cache.clear()
        risk_manager._intraday_order_tier_cache.update(pt_date="", tiers={})
        with (
            mock.patch(
                "reporting.pnl_ledger.fetch_all_fills", lambda: [day_fill]
            ),
            mock.patch.object(
                risk_manager.requests, "get", lambda *_args, **_kwargs: response
            ),
            mock.patch.dict(
                os.environ,
                {"ALPACA_API_KEY": "offline", "ALPACA_SECRET_KEY": "offline"},
            ),
        ):
            live_risk = risk_manager.RiskManager(
                930.0, daily_start_value=1_000.0
            )
            kill_tripped = live_risk.check_kill_switch()
            from execution.f6_cycle import resolve_f6_kill_route

            genuine_route = resolve_f6_kill_route(
                enabled=True,
                kill_tripped=kill_tripped,
                control_fault="",
                risk=live_risk,
            )
            control_route = resolve_f6_kill_route(
                enabled=True,
                kill_tripped=True,
                control_fault="ownership attribution unresolved",
                risk=live_risk,
            )
        ownership_guard.save_ledger(final_before_kill)
        scenarios.append(_scenario(
            "typed_kill_verdict",
            kill_tripped
            and live_risk.last_kill_measure == -70.0
            and not genuine_route.block_starter
            and control_route.block_starter,
            {
                "trading_pnl": live_risk.last_kill_measure,
                "genuine_loss_route": genuine_route.reason,
                "control_fault_route": control_route.reason,
            },
        ))

        partial = broker_module.submit_limit_order(
            "NVDA",
            2,
            "buy",
            100.0,
            extended_hours=True,
            tier="forever6",
            client_order_id="F6-NVDA-b-2-partial",
            distinguish_rejection=True,
        )
        assert partial is not None
        partial.status = "partially_filled"
        partial.filled_qty = "1"
        broker.submission_errors = [
            '{"code": 42210000, "message": "simulation rejection"}'
        ]
        rejected = broker_module.submit_limit_order(
            "NVDA",
            1,
            "buy",
            100.0,
            extended_hours=True,
            tier="forever6",
            client_order_id="F6-NVDA-b-3-rejected",
            distinguish_rejection=True,
        )
        broker.submission_errors = ["connection timeout"] * 3
        ambiguous = broker_module.submit_limit_order(
            "NVDA",
            1,
            "buy",
            100.0,
            extended_hours=True,
            tier="forever6",
            client_order_id="F6-NVDA-b-4-ambiguous",
            distinguish_rejection=True,
        )
        scenarios.append(
            _scenario(
                "order_state_matrix",
                str(entry.status) == "filled"
                and str(partial.status) == "partially_filled"
                and partial.filled_qty == "1"
                and rejected is broker_module.ORDER_DEFINITIVELY_REJECTED
                and ambiguous is None,
                {
                    "accepted_then_filled": entry.id,
                    "partial": partial.id,
                    "definitive_reject": (
                        rejected is broker_module.ORDER_DEFINITIVELY_REJECTED
                    ),
                    "ambiguous_none": ambiguous is None,
                },
            )
        )

        ownership_guard.save_ledger(_ledger(4.0, 10.0, qhm_qty=2.0))
        broker.position_qty = 6.0
        qhm_stop = SimpleNamespace(
            id="sim-qhm-stop",
            client_order_id="QH-NVDA-s-1-stop",
            symbol="NVDA",
            side="sell",
            qty="2",
            filled_qty="0",
            status="new",
            type="stop",
            time_in_force="gtc",
            limit_price=None,
            stop_price="90.00",
            filled_avg_price=None,
            extended_hours=False,
        )
        broker.orders[qhm_stop.id] = qhm_stop
        if f6._TRIM_STATE.exists():
            f6._TRIM_STATE.unlink()
        broker.submission_errors = [
            '{"code": 40310000, "message": "insufficient qty", '
            '"held_for_orders": "2", "available": "4", "existing_qty": "6"}',
            '{"code": 40310000, "message": "insufficient qty", '
            '"held_for_orders": "2", "available": "4", "existing_qty": "6"}',
        ]
        f6._STARTUP_READY = True
        collision_manager = f6.ForeverHoldManager(
            broker, clock=lambda: now, quote_loader=quote
        )
        collision_manager.universe = ["NVDA"]
        collision_manager._fetch_prices = lambda: {  # type: ignore[method-assign]
            "NVDA": 400.0
        }
        collision_route = run_f6_after_close(
            enabled=True,
            is_after_close=True,
            now_et=now,
            block_starter=True,
            fetch_bars=lambda *_args, **_kwargs: bars,
            manager=collision_manager,
        )
        collision_result = collision_route["trim_result"]
        collision_state = collision_manager._load_trim_state()
        scenarios.append(
            _scenario(
                "shared_stop_collision_preserved",
                collision_result == {}
                and qhm_stop.status == "new"
                and qhm_stop.id not in broker.cancelled_order_ids
                and collision_state["NVDA"]["trim1"] == "reserved",
                {
                    "qhm_stop_status": qhm_stop.status,
                    "cancelled_order_ids": broker.cancelled_order_ids,
                    "trim_state": collision_state,
                },
            )
        )
        ownership_guard.save_ledger(final_before_kill)
        broker.position_qty = 3.0

    if config.FOREVER6_ENABLED is not False:
        raise RuntimeError("simulation mutated production FOREVER6_ENABLED")
    sources = _source_hashes()
    passed = all(row["status"] == "PASS" for row in scenarios) and (
        _order_transcript_valid(broker.order_calls)
    )
    return {
        "schema_version": 1,
        "kind": "f6_hermetic_full_path_simulation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_head": source_commit,
        "network_capable": broker.network_capable,
        "source_sha256": sources,
        "source_tree_sha256": _source_tree_digest(sources),
        "scenarios": scenarios,
        "order_transcript": broker.order_calls,
        "final_f6_qty": 3.0,
        "overall": "PASS" if passed and not broker.network_capable else "FAIL",
    }


def run_simulation(*, inject_failure: bool = False) -> dict[str, Any]:
    """Run in-process with exact global restoration and hard I/O containment.

    The command-line entry point adds a second process boundary. Tests use this
    function directly so they can independently verify restoration after both a
    success and an injected exception.
    """
    before_sources = _source_hashes()
    source_commit = _git_head()
    before_flag = config.FOREVER6_ENABLED
    before_ready = f6._STARTUP_READY
    before_quotes = dict(f6._F6_QUOTE_OBSERVATIONS)
    before_allocator = allocator._LIVE_ALLOCATOR
    before_intraday_cache = dict(risk_manager._intraday_cache)
    before_intraday_order_tier_cache = dict(
        risk_manager._intraday_order_tier_cache
    )
    credentials = {
        key: os.environ.pop(key, None)
        for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
    }
    network_refused = False
    filesystem_refusals = {
        name: False for name in REQUIRED_FILESYSTEM_REFUSALS
    }
    result: dict[str, Any] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="f6-full-path-") as tmp:
            state = Path(tmp).resolve()
            with _hermetic_guards(state):
                try:
                    subprocess.run(
                        [
                            "touch",
                            str(
                                ROOT
                                / "data"
                                / "state"
                                / ".f6-sim-subprocess-escape"
                            ),
                        ],
                        check=False,
                    )
                except RuntimeError:
                    filesystem_refusals["subprocess_write"] = True
                try:
                    ctypes.CDLL(None)
                except RuntimeError:
                    filesystem_refusals["native_loader"] = True
                for name in PROCESS_LAUNCH_APIS:
                    try:
                        getattr(os, name)()
                    except RuntimeError:
                        filesystem_refusals[f"process_{name}"] = True
                try:
                    socket.socket()
                except RuntimeError:
                    network_refused = True
                try:
                    with open(ROOT / "data" / "state" / ".f6-sim-escape", "w"):
                        pass
                except PermissionError:
                    filesystem_refusals["builtin_open"] = True
                outside = ROOT / "data" / "state"
                try:
                    (outside / ".f6-sim-write-escape").write_text(
                        "escape", encoding="utf-8"
                    )
                except PermissionError:
                    filesystem_refusals["path_write_text"] = True
                try:
                    (outside / ".f6-sim-mkdir-escape").mkdir()
                except PermissionError:
                    filesystem_refusals["path_mkdir"] = True
                try:
                    (outside / ".f6-sim-unlink-escape").unlink()
                except PermissionError:
                    filesystem_refusals["path_unlink"] = True
                try:
                    (outside / ".f6-sim-replace-escape").replace(
                        outside / ".f6-sim-replace-moved"
                    )
                except PermissionError:
                    filesystem_refusals["path_replace"] = True
                try:
                    os.symlink(
                        state / "inside-target",
                        outside / ".f6-sim-os-symlink-escape",
                    )
                except PermissionError:
                    filesystem_refusals["os_symlink"] = True
                try:
                    os.link(
                        ROOT / "config.py",
                        outside / ".f6-sim-os-hardlink-escape",
                    )
                except PermissionError:
                    filesystem_refusals["os_hardlink"] = True
                outside_fd = os.open(outside, os.O_RDONLY)
                state_fd = os.open(state, os.O_RDONLY)
                try:
                    descriptor_probes = (
                        (
                            "os_open_dir_fd",
                            lambda: os.open(
                                ".f6-sim-openat-escape",
                                os.O_CREAT | os.O_WRONLY,
                                dir_fd=outside_fd,
                            ),
                        ),
                        (
                            "os_mkdir_dir_fd",
                            lambda: os.mkdir(
                                ".f6-sim-mkdirat-escape", dir_fd=outside_fd
                            ),
                        ),
                        (
                            "os_unlink_dir_fd",
                            lambda: os.unlink(
                                ".f6-sim-unlinkat-escape", dir_fd=outside_fd
                            ),
                        ),
                        (
                            "os_rename_dir_fd",
                            lambda: os.rename(
                                "inside-source",
                                ".f6-sim-renameat-escape",
                                src_dir_fd=state_fd,
                                dst_dir_fd=outside_fd,
                            ),
                        ),
                        (
                            "os_replace_dir_fd",
                            lambda: os.replace(
                                "inside-source",
                                ".f6-sim-replaceat-escape",
                                src_dir_fd=state_fd,
                                dst_dir_fd=outside_fd,
                            ),
                        ),
                        (
                            "os_symlink_dir_fd",
                            lambda: os.symlink(
                                "inside-target",
                                ".f6-sim-symlinkat-escape",
                                dir_fd=outside_fd,
                            ),
                        ),
                        (
                            "os_hardlink_dir_fd",
                            lambda: os.link(
                                "inside-source",
                                ".f6-sim-linkat-escape",
                                src_dir_fd=state_fd,
                                dst_dir_fd=outside_fd,
                            ),
                        ),
                    )
                    for name, probe in descriptor_probes:
                        try:
                            probe()
                        except PermissionError:
                            filesystem_refusals[name] = True
                finally:
                    os.close(state_fd)
                    os.close(outside_fd)
                try:
                    (outside / ".f6-sim-path-symlink-escape").symlink_to(
                        state / "inside-target"
                    )
                except PermissionError:
                    filesystem_refusals["path_symlink"] = True
                try:
                    (outside / ".f6-sim-path-hardlink-escape").hardlink_to(
                        ROOT / "config.py"
                    )
                except PermissionError:
                    filesystem_refusals["path_hardlink"] = True
                result = _run_simulation_inner(
                    state,
                    source_commit=source_commit,
                    inject_failure=inject_failure,
                )
    finally:
        f6._STARTUP_READY = before_ready
        f6._F6_QUOTE_OBSERVATIONS.clear()
        f6._F6_QUOTE_OBSERVATIONS.update(before_quotes)
        allocator._LIVE_ALLOCATOR = before_allocator
        risk_manager._intraday_cache.clear()
        risk_manager._intraday_cache.update(before_intraday_cache)
        risk_manager._intraday_order_tier_cache.clear()
        risk_manager._intraday_order_tier_cache.update(
            before_intraday_order_tier_cache
        )
        config.FOREVER6_ENABLED = before_flag
        for key, value in credentials.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
    if result is None:
        raise RuntimeError("simulation produced no result")
    after_sources = _source_hashes()
    restored = (
        config.FOREVER6_ENABLED is before_flag
        and f6._STARTUP_READY is before_ready
        and f6._F6_QUOTE_OBSERVATIONS == before_quotes
        and allocator._LIVE_ALLOCATOR is before_allocator
        and risk_manager._intraday_cache == before_intraday_cache
        and risk_manager._intraday_order_tier_cache
        == before_intraday_order_tier_cache
    )
    result["source_sha256_before"] = before_sources
    result["source_sha256_after"] = after_sources
    result["source_sha256"] = after_sources
    result["source_tree_sha256"] = _source_tree_digest(after_sources)
    result["scenarios"].extend(
        [
            _scenario("network_escape_refused", network_refused, {"refused": network_refused}),
            _scenario(
                "filesystem_escape_refused",
                all(filesystem_refusals.values()),
                {"refused": filesystem_refusals},
            ),
            _scenario("process_state_restored", restored, {"restored": restored}),
            _scenario(
                "source_bytes_unchanged",
                before_sources == after_sources,
                {"before": before_sources, "after": after_sources},
            ),
        ]
    )
    result["production_flag_remained_disabled"] = bool(
        before_flag is False and config.FOREVER6_ENABLED is False
    )
    inner_passed = result.get("overall") == "PASS"
    result["overall"] = (
        "PASS"
        if inner_passed
        and _scenario_rows_valid(result["scenarios"])
        and before_sources == after_sources
        and _artifact_runtime_evidence_valid(result)
        else "FAIL"
    )
    return result


def write_atomic(result: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f"{output.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, output)
    finally:
        if tmp.exists():
            tmp.unlink()


def check_committed_artifact() -> dict[str, Any]:
    """Validate the reviewed HEAD blob; never regenerate evidence in check mode."""
    path = "logs/f6_full_path_simulation_latest.json"
    shown = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        return {"overall": "FAIL", "reason": "committed artifact missing"}
    try:
        artifact = json.loads(shown.stdout)
    except (TypeError, ValueError) as exc:
        return {"overall": "FAIL", "reason": f"committed artifact unreadable: {exc}"}
    scenarios = artifact.get("scenarios")
    by_name = {
        str(row.get("name")): row
        for row in scenarios
        if isinstance(row, dict) and row.get("name")
    } if isinstance(scenarios, list) else {}
    current = _source_hashes()
    head_sources = _git_source_hashes("HEAD")
    index_sources = _git_index_source_hashes()
    head_commit = _git_head()
    valid = (
        artifact.get("overall") == "PASS"
        and _scenario_rows_valid(scenarios)
        and _artifact_runtime_evidence_valid(artifact)
        and head_sources is not None
        and index_sources is not None
        and _artifact_source_binding_valid(
            artifact,
            head_sources=head_sources,
            index_sources=index_sources,
            working_sources=current,
        )
        and config.FOREVER6_ENABLED is False
    )
    return {
        "overall": "PASS" if valid else "FAIL",
        "artifact_commit": artifact.get("source_commit") or artifact.get("git_head"),
        "head_commit": head_commit,
        "source_sha256": head_sources,
        "bound_index_clean": (
            head_sources is not None and index_sources == head_sources
        ),
        "bound_worktree_clean": head_sources is not None and current == head_sources,
        "scenario_count": len(by_name),
    }


def run_isolated(output: Path) -> dict[str, Any]:
    """Execute the harness in a disposable full checkout and return its JSON."""
    source_commit = _git_head()
    with tempfile.TemporaryDirectory(prefix="f6-sim-checkout-") as tmp:
        checkout = Path(tmp) / "repo"
        shutil.copytree(
            ROOT,
            checkout,
            ignore=shutil.ignore_patterns(
                ".git",
                ".env",
                ".pytest_cache",
                ".venv",
                "venv",
                "__pycache__",
                "*.pyc",
                "work",
            ),
        )
        child_output = checkout / "logs" / ".f6_sim_child.json"
        env = dict(os.environ)
        env.pop("ALPACA_API_KEY", None)
        env.pop("ALPACA_SECRET_KEY", None)
        env["MTF_TEST_MODE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["F6_SIM_SOURCE_COMMIT"] = source_commit
        proc = subprocess.run(
            [
                sys.executable,
                str(checkout / "research" / "f6_full_path_simulation.py"),
                "--child",
                "--output",
                str(child_output),
            ],
            cwd=checkout,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0 or not child_output.exists():
            raise RuntimeError(
                f"isolated child failed rc={proc.returncode}: "
                f"{proc.stderr[-2000:]} {proc.stdout[-2000:]}"
            )
        result = json.loads(child_output.read_text())
    write_atomic(result, output)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "logs" / "f6_full_path_simulation_latest.json",
    )
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        result = check_committed_artifact()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["overall"] == "PASS" else 1
    if args.child:
        result = run_simulation()
        result["source_commit"] = os.getenv("F6_SIM_SOURCE_COMMIT", _git_head())
        write_atomic(result, args.output)
    else:
        result = run_isolated(args.output)
    print(json.dumps({"overall": result["overall"], "output": str(args.output)}))
    return 0 if result["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
