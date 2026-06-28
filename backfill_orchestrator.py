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


def start_background_job(
    cmd: list[str],
    cwd: str | Path,
    log_dir: str | Path,
    name: str,
) -> dict[str, Any]:
    log_root = Path(log_dir)
    log_root.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_job_name(name)
    active_path = log_root / f"{safe_name}.active.json"
    if active_path.exists():
        try:
            active = json.loads(active_path.read_text(encoding="utf-8"))
        except Exception:
            active = {}
        if _pid_is_running(active.get("pid")):
            return {
                "pid": active.get("pid"),
                "log_path": active.get("log_path"),
                "cmd": active.get("cmd") or list(cmd),
                "started_at": active.get("started_at"),
                "reused": True,
            }
        try:
            active_path.unlink()
        except Exception:
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
    finally:
        log_file.close()

    result = {
        "pid": proc.pid,
        "log_path": str(log_path),
        "cmd": list(run_cmd),
        "started_at": ts,
    }
    try:
        active_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return result
