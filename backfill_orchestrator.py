from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any


LAUNCH_LEASE_SECONDS = 120


def _safe_job_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name).strip("_") or "backfill"


def _pid_is_running(pid: Any) -> bool:
    try:
        pid_int = int(pid)
    except Exception:
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _python_unbuffered_cmd(cmd: list[str]) -> list[str]:
    if not cmd:
        return list(cmd)
    first = Path(str(cmd[0])).name.lower()
    if first.startswith("python") and "-u" not in cmd[1:3]:
        return [cmd[0], "-u", *cmd[1:]]
    return list(cmd)


def _active_job_path(log_root: Path, name: str, exclusive_key: str | None) -> Path:
    """Return the writer lock path; an exclusive key spans changing job names."""
    key = _safe_job_name(exclusive_key) if exclusive_key else _safe_job_name(name)
    return log_root / f"{key}.active.json"


def _load_active_job(active_path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(active_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso_now() -> str:
    return _utcnow().isoformat(timespec="seconds")


def _parse_utc(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _launch_lease_is_live(active: dict[str, Any], *, now: dt.datetime | None = None) -> bool:
    """A pid-less claim blocks briefly; malformed or expired claims are stale."""
    if active.get("pid") is not None:
        return False
    if not isinstance(active.get("claim_id"), str) or not active.get("claim_id"):
        return False
    claimed_at = _parse_utc(active.get("claimed_at"))
    expires_at = _parse_utc(active.get("lease_expires_at"))
    if claimed_at is None or expires_at is None or expires_at <= claimed_at:
        return False
    return expires_at > (now or _utcnow())


def _active_blocks_new_start(active: dict[str, Any], *, now: dt.datetime | None = None) -> bool:
    if _launch_lease_is_live(active, now=now):
        return True
    return _pid_is_running(active.get("pid"))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Publish active state without leaving a partially-written JSON lease."""
    claim = _safe_job_name(str(payload.get("claim_id") or uuid.uuid4().hex))
    temporary = path.with_name(f".{path.name}.{claim}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_claim_if_owned(active_path: Path, claim_id: str) -> None:
    active = _load_active_job(active_path)
    if active.get("claim_id") == claim_id:
        active_path.unlink(missing_ok=True)


def _reused_active_result(active: dict[str, Any], cmd: list[str], active_path: Path) -> dict[str, Any]:
    return {
        "pid": active.get("pid"),
        "log_path": active.get("log_path"),
        "cmd": active.get("cmd") or list(cmd),
        "started_at": active.get("started_at"),
        "target_day": active.get("target_day"),
        "phase": active.get("phase", "running"),
        "budget_seconds": active.get("budget_seconds"),
        "status_path": active.get("status_path"),
        "active_path": str(active_path),
        "started": bool(active.get("pid")),
        "returncode": None,
        "timed_out": False,
        "error_kind": None,
        "output": "",
        "allowance": None,
        "reused": True,
    }


def _claim_active_path(active_path: Path) -> tuple[bool, dict[str, Any]]:
    """Atomically reserve a writer lease, or return the observed current owner."""
    now = _utcnow()
    reservation = {
        "pid": None,
        "phase": "launching",
        "claim_id": uuid.uuid4().hex,
        "claimed_at": now.isoformat(timespec="seconds"),
        "lease_expires_at": (now + dt.timedelta(seconds=LAUNCH_LEASE_SECONDS)).isoformat(timespec="seconds"),
        "started_at": now.isoformat(timespec="seconds"),
    }
    try:
        with active_path.open("x", encoding="utf-8") as handle:
            json.dump(reservation, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        return True, reservation
    except FileExistsError:
        return False, _load_active_job(active_path)


def background_job_status(
    log_dir: str | Path,
    name: str,
    *,
    exclusive_key: str | None = None,
) -> dict[str, Any]:
    """Read one writer's status without starting, waiting for, or changing it."""
    active_path = _active_job_path(Path(log_dir), name, exclusive_key)
    active = _load_active_job(active_path)
    if not active:
        return {"phase": "idle", "active_path": str(active_path)}
    running = _active_blocks_new_start(active)
    status: dict[str, Any] = {
        **active,
        "active_path": str(active_path),
        "phase": active.get("phase", "running") if running else "finished",
        "running": running,
    }
    log_path = active.get("log_path")
    if log_path:
        try:
            tail = Path(str(log_path)).read_bytes()[-16_384:].decode("utf-8", errors="replace")
            for line in reversed(tail.splitlines()):
                if line.startswith("scheduler_status="):
                    status["last_structured_result"] = json.loads(line.split("=", 1)[1])
                    status["phase"] = str(status["last_structured_result"].get("phase") or status["phase"])
                    if status["phase"] == "formal_failed":
                        retry_after = status["last_structured_result"].get("retry_after_seconds")
                        try:
                            completed_at = Path(str(log_path)).stat().st_mtime
                            status["completed_at"] = completed_at
                            status["next_retry_at"] = completed_at + max(0, int(retry_after or 0))
                        except (OSError, TypeError, ValueError):
                            pass
                    break
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    try:
        started = _parse_utc(status.get("started_at"))
        if started is None:
            raise ValueError("invalid started_at")
        elapsed = max(0, int((_utcnow() - started).total_seconds()))
        status["elapsed_seconds"] = elapsed
        if running and status.get("budget_seconds") is not None:
            status["remaining_budget_seconds"] = max(0, int(status["budget_seconds"]) - elapsed)
    except (TypeError, ValueError):
        pass
    return status


def start_background_job(
    cmd: list[str],
    cwd: str | Path,
    log_dir: str | Path,
    name: str,
    *,
    exclusive_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start one background writer, optionally serialized by a base-dir key."""
    log_root = Path(log_dir)
    log_root.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_job_name(name)
    active_path = _active_job_path(log_root, name, exclusive_key)
    while True:
        claimed, active = _claim_active_path(active_path)
        if claimed:
            break
        if _active_blocks_new_start(active):
            return _reused_active_result(active, cmd, active_path)
        try:
            active_path.unlink()
        except FileNotFoundError:
            pass

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_root / f"{safe_name}_{ts}.log"

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    run_cmd = _python_unbuffered_cmd(cmd)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    log_file = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            run_cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            env=env,
        )
    except FileNotFoundError as exc:
        _remove_claim_if_owned(active_path, str(active["claim_id"]))
        return {
            "started": False,
            "pid": None,
            "returncode": None,
            "timed_out": False,
            "error_kind": "file_not_found",
            "output": str(exc),
            "allowance": None,
            "active_path": str(active_path),
            "reused": False,
        }
    except PermissionError as exc:
        _remove_claim_if_owned(active_path, str(active["claim_id"]))
        return {
            "started": False,
            "pid": None,
            "returncode": None,
            "timed_out": False,
            "error_kind": "permission_denied",
            "output": str(exc),
            "allowance": None,
            "active_path": str(active_path),
            "reused": False,
        }
    except OSError as exc:
        _remove_claim_if_owned(active_path, str(active["claim_id"]))
        return {
            "started": False,
            "pid": None,
            "returncode": None,
            "timed_out": False,
            "error_kind": "popen_failed",
            "output": str(exc),
            "allowance": None,
            "active_path": str(active_path),
            "reused": False,
        }
    finally:
        log_file.close()

    result = {
        "pid": proc.pid,
        "log_path": str(log_path),
        "cmd": list(run_cmd),
        "started": True,
        "returncode": None,
        "timed_out": False,
        "error_kind": None,
        "output": "",
        "allowance": None,
        "claim_id": active["claim_id"],
        "started_at": _iso_now(),
        "active_path": str(active_path),
    }
    result.update(dict(metadata or {}))
    try:
        _atomic_write_json(active_path, result)
    except Exception:
        pass
    return result
