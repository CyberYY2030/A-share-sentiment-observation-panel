from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any


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
        "reused": True,
    }


def _claim_active_path(active_path: Path) -> dict[str, Any] | None:
    """Atomically reserve an active-job path, or return the current owner."""
    reservation = {
        "pid": None,
        "phase": "launching",
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with active_path.open("x", encoding="utf-8") as handle:
            json.dump(reservation, handle, ensure_ascii=False)
        return None
    except FileExistsError:
        return _load_active_job(active_path)


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
    running = _pid_is_running(active.get("pid"))
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
                    break
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    try:
        started = dt.datetime.fromisoformat(str(status.get("started_at")))
        elapsed = max(0, int((dt.datetime.now() - started).total_seconds()))
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
        active = _claim_active_path(active_path)
        if active is None:
            break
        # A launcher reservation also owns the slot.  Treating it as stale
        # would allow two rapid Streamlit reruns to start concurrent writers.
        if active.get("pid") is None or _pid_is_running(active.get("pid")):
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
    except Exception:
        active_path.unlink(missing_ok=True)
        raise
    finally:
        log_file.close()

    result = {
        "pid": proc.pid,
        "log_path": str(log_path),
        "cmd": list(run_cmd),
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "active_path": str(active_path),
    }
    result.update(dict(metadata or {}))
    try:
        active_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return result
