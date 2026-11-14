"""Find, stop and restart this project's Python processes (Windows).

Process layout:
  web_app.py (dashboard)  ->  main.py --telegram-poll (listener, runs the Telegram bot)
                          ->  main.py --produce-telegram / --process-queue / ... (workers, + Chrome)

"Stop" kills the workers (and the Chrome they drive) but keeps the dashboard and the listener.
"Restart" cannot be done by the listener itself because it is one of the processes being killed,
so it spawns a detached helper (`python -m core.server_control restart ...`) that kills the old
processes, waits for the Telegram polling lock to be released, then starts the server again.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from core.config import ROOT_DIR, STORAGE_DIR
from core.storage import PROCESS_LOCK_FILE, TELEGRAM_POLL_LOCK_FILE

LOGGER = logging.getLogger("short_video_distributor")

RESTART_MARKER_FILE = STORAGE_DIR / "server_restart.json"
LOG_FILE = STORAGE_DIR / "dashboard.log"
WORKFLOW_STATUS_FILE = STORAGE_DIR / "workflow_status.json"

ROLE_DASHBOARD = "dashboard"
ROLE_LISTENER = "listener"
ROLE_WORKER = "worker"
ROLE_LOGIN = "login"
ROLE_LABELS = {
    ROLE_DASHBOARD: "🖥 Dashboard (web_app.py)",
    ROLE_LISTENER: "🤖 Listener Telegram",
    ROLE_WORKER: "⚙️ Worker",
    ROLE_LOGIN: "🔑 Đăng nhập Flow",
}

_SCRIPT_RE = re.compile(r"(?:^|[\\/\s\"'])(web_app|main|save_flow_auth)\.py\b", re.IGNORECASE)
_MODE_RE = re.compile(r"\s--([a-z][a-z-]+)")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)

_PS_LIST = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process | ForEach-Object { [pscustomobject]@{ "
    "pid = [int]$_.ProcessId; ppid = [int]$_.ParentProcessId; name = [string]$_.Name; "
    "exe = [string]$_.ExecutablePath; cmd = [string]$_.CommandLine; "
    "created = $(if ($_.CreationDate) { $_.CreationDate.ToString('o') } else { '' }) } } "
    "| ConvertTo-Json -Compress"
)


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    name: str
    exe: str
    cmd: str
    created: str = ""


@dataclass(frozen=True)
class ProjectProcess:
    proc: ProcInfo
    role: str
    mode: str = ""

    @property
    def label(self) -> str:
        base = ROLE_LABELS.get(self.role, self.role)
        return f"{base} --{self.mode}" if self.role == ROLE_WORKER and self.mode else base


def _norm(text: str) -> str:
    return (text or "").replace("/", "\\").lower()


def python_executable() -> str:
    venv = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    return str(venv if venv.is_file() else Path(sys.executable))


def list_processes() -> list[ProcInfo]:
    if os.name != "nt":
        raise RuntimeError("Điều khiển tiến trình chỉ hỗ trợ Windows.")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_LIST],
        capture_output=True,
        timeout=30,
        check=True,
        creationflags=_NO_WINDOW,
    )
    raw = result.stdout.decode("utf-8", errors="replace").strip()
    data = json.loads(raw) if raw else []
    if isinstance(data, dict):
        data = [data]
    return [
        ProcInfo(
            pid=int(item.get("pid") or 0),
            ppid=int(item.get("ppid") or 0),
            name=str(item.get("name") or ""),
            exe=str(item.get("exe") or ""),
            cmd=str(item.get("cmd") or ""),
            created=str(item.get("created") or ""),
        )
        for item in data
        if isinstance(item, dict)
    ]


def _classify(cmd: str) -> tuple[str, str] | None:
    match = _SCRIPT_RE.search(cmd or "")
    if not match:
        return None
    script = match.group(1).lower()
    if script == "web_app":
        return ROLE_DASHBOARD, ""
    if script == "save_flow_auth":
        return ROLE_LOGIN, ""
    modes = [m for m in _MODE_RE.findall(cmd[match.end():]) if m != "external-telegram-listener"]
    if "telegram-poll" in modes:
        return ROLE_LISTENER, "telegram-poll"
    return ROLE_WORKER, modes[0] if modes else ""


def ancestors(procs: Iterable[ProcInfo], pid: int) -> list[int]:
    by_pid = {p.pid: p for p in procs}
    chain: list[int] = []
    current = by_pid.get(pid)
    while current is not None and current.ppid and current.ppid not in chain and current.ppid != current.pid:
        chain.append(current.ppid)
        current = by_pid.get(current.ppid)
    return chain


def descendants(procs: Iterable[ProcInfo], roots: Iterable[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    for proc in procs:
        children.setdefault(proc.ppid, []).append(proc.pid)
    found: set[int] = set()
    stack = list(roots)
    while stack:
        pid = stack.pop()
        for child in children.get(pid, []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def project_processes(procs: list[ProcInfo], self_pid: int | None = None) -> list[ProjectProcess]:
    """Python processes running this project's web_app.py / main.py / save_flow_auth.py.

    A process counts when its command line or executable (or its parent's executable, for the
    venv launcher -> base interpreter pair) is inside the project folder, or when it is an
    ancestor of self_pid (covers `python web_app.py` started from the project dir).
    """
    root = _norm(str(ROOT_DIR)).rstrip("\\") + "\\"
    by_pid = {p.pid: p for p in procs}
    own_chain = set(ancestors(procs, self_pid)) | {self_pid} if self_pid else set()
    found: list[ProjectProcess] = []
    for proc in procs:
        if not proc.name.lower().startswith("python"):
            continue
        role = _classify(proc.cmd)
        if role is None:
            continue
        parent = by_pid.get(proc.ppid)
        inside = (
            root in _norm(proc.cmd)
            or _norm(proc.exe).startswith(root)
            or (parent is not None and _norm(parent.exe).startswith(root))
            or proc.pid in own_chain
        )
        if inside:
            found.append(ProjectProcess(proc, role[0], role[1]))
    return found


def collapse_launchers(items: list[ProjectProcess]) -> list[ProjectProcess]:
    """Hide the venv launcher half of each launcher -> interpreter pair (same command)."""
    pids = {item.proc.pid: item for item in items}
    hidden = {
        item.proc.ppid
        for item in items
        if item.proc.ppid in pids and pids[item.proc.ppid].role == item.role
    }
    return [item for item in items if item.proc.pid not in hidden]


def _kill(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            timeout=15,
            creationflags=_NO_WINDOW,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def kill_set(procs: list[ProcInfo], targets: list[ProjectProcess], keep: set[int]) -> list[int]:
    """Targets plus everything they spawned (Playwright driver, Chrome), minus `keep` and any
    Flow-login process tree (a manual login in progress must survive)."""
    project = project_processes(procs)
    login_roots = [item.proc.pid for item in project if item.role == ROLE_LOGIN]
    protected = set(keep) | set(login_roots) | descendants(procs, login_roots)
    roots = [item.proc.pid for item in targets]
    pids = (set(roots) | descendants(procs, roots)) - protected
    # children first so nothing gets re-parented mid-way
    order = sorted(pids, key=lambda pid: pid in roots)
    return order


def _mark_workflow_stopped(reason: str) -> None:
    try:
        status = json.loads(WORKFLOW_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(status, dict) and status.get("running"):
        status.update({"running": False, "current_node": None, "error": reason})
        try:
            WORKFLOW_STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError:
            pass


def stop_workers(self_pid: int | None = None) -> list[ProjectProcess]:
    """Kill background workers (not the dashboard, the listener or a Flow login). Returns what was
    targeted (one entry per worker)."""
    self_pid = self_pid or os.getpid()
    procs = list_processes()
    keep = {self_pid, *ancestors(procs, self_pid)}
    workers = [
        item
        for item in project_processes(procs, self_pid)
        if item.role == ROLE_WORKER and item.proc.pid not in keep
    ]
    for pid in kill_set(procs, workers, keep):
        _kill(pid)
    if workers:
        _mark_workflow_stopped("Đã dừng từ Telegram.")
    return collapse_launchers(workers)


def worker_lock_busy() -> bool:
    from filelock import FileLock, Timeout

    try:
        with FileLock(str(PROCESS_LOCK_FILE), timeout=0):
            return False
    except Timeout:
        return True


# ---------------------------------------------------------------- restart

def relaunch_command(procs: list[ProcInfo], self_pid: int | None = None) -> list[str]:
    """Restart the dashboard when it is running (it starts the listener); otherwise re-run this
    process's own main.py command."""
    self_pid = self_pid or os.getpid()
    items = project_processes(procs, self_pid)
    if any(item.role == ROLE_DASHBOARD for item in items):
        return [python_executable(), str(ROOT_DIR / "web_app.py")]
    return [python_executable(), str(ROOT_DIR / "main.py"), *sys.argv[1:]]


def _popen_detached(cmd: list[str], log_path: Path, env: dict[str, str] | None = None) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as log:
        kwargs = dict(cwd=ROOT_DIR, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        for flags in (_NO_WINDOW | _NEW_GROUP | _BREAKAWAY, _NO_WINDOW | _NEW_GROUP):
            try:
                return subprocess.Popen(cmd, creationflags=flags, **kwargs)
            except OSError:
                if flags == _NO_WINDOW | _NEW_GROUP:
                    raise
    raise RuntimeError("unreachable")


def request_restart(chat_id: str | int, self_pid: int | None = None) -> list[str]:
    """Spawn the detached restart helper. Returns the command that will be relaunched."""
    self_pid = self_pid or os.getpid()
    procs = list_processes()
    relaunch = relaunch_command(procs, self_pid)
    RESTART_MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESTART_MARKER_FILE.write_text(
        json.dumps(
            {"chat_id": str(chat_id), "requested_at": int(time.time()), "relaunch": relaunch},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    helper = [python_executable(), "-m", "core.server_control", "restart", "--cmd-json", json.dumps(relaunch)]
    _popen_detached(helper, LOG_FILE)
    return relaunch


def pop_restart_marker(max_age_seconds: int = 900) -> dict | None:
    try:
        data = json.loads(RESTART_MARKER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        RESTART_MARKER_FILE.unlink()
    except OSError:
        pass
    if not isinstance(data, dict) or time.time() - int(data.get("requested_at") or 0) > max_age_seconds:
        return None
    return data


def _wait_gone(pids: set[int], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = {p.pid for p in list_processes()} & pids
        if not alive:
            return
        time.sleep(1.0)


def _wait_poll_lock(timeout: float) -> bool:
    from filelock import FileLock, Timeout

    try:
        with FileLock(str(TELEGRAM_POLL_LOCK_FILE), timeout=timeout):
            return True
    except Timeout:
        return False


def _fresh_env() -> dict[str, str]:
    """Environment for the relaunched server. Variables that come from .env are dropped so the new
    process re-reads .env (load_dotenv never overrides inherited values) — a restart therefore
    picks up .env edits."""
    env = os.environ.copy()
    try:
        from dotenv import dotenv_values

        for key in dotenv_values(ROOT_DIR / ".env"):
            env.pop(key, None)
    except Exception:
        pass
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _restart_main(relaunch: list[str], delay: float) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    time.sleep(delay)  # let the listener finish answering Telegram
    me = os.getpid()
    procs = list_processes()
    # self_pid=me: the helper's parent chain (listener -> dashboard) counts as project processes
    # even when the dashboard was started with a Python outside the project folder.
    targets = [
        item
        for item in project_processes(procs, me)
        if item.role in {ROLE_DASHBOARD, ROLE_LISTENER, ROLE_WORKER} and item.proc.pid != me
    ]
    pids = kill_set(procs, targets, keep={me})
    LOGGER.info("🔄 Restart server: dừng %d tiến trình (%s).", len(pids), ", ".join(map(str, pids)))
    for pid in pids:
        _kill(pid)
    _wait_gone(set(pids), timeout=20)
    if not _wait_poll_lock(timeout=20):
        LOGGER.warning("Khóa Telegram polling vẫn bị giữ sau 20s; vẫn khởi động lại.")
    # Update the marker before relaunching: the new listener deletes it once it has announced.
    try:
        data = json.loads(RESTART_MARKER_FILE.read_text(encoding="utf-8"))
        data["stopped"] = len(collapse_launchers(targets))
        RESTART_MARKER_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        pass
    process = _popen_detached(relaunch, LOG_FILE, env=_fresh_env())
    LOGGER.info("✅ Restart server: đã chạy lại %s (PID %s).", " ".join(relaunch[1:]), process.pid)
    return 0


def format_started(created: str) -> str:
    """'2026-11-14T10:20:30.1234567+07:00' (.NET 'o' format, local time) -> '14/11 10:20'."""
    match = re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d", created or "")
    if not match:
        return ""
    return datetime.strptime(match.group(0), "%Y-%m-%dT%H:%M").strftime("%d/%m %H:%M")


def main() -> int:
    parser = argparse.ArgumentParser(description="Restart helper for the short-video server.")
    sub = parser.add_subparsers(dest="action", required=True)
    restart = sub.add_parser("restart")
    restart.add_argument("--cmd-json", required=True)
    restart.add_argument("--delay", type=float, default=3.0)
    args = parser.parse_args()
    if args.action == "restart":
        relaunch = json.loads(args.cmd_json)
        if not isinstance(relaunch, list) or not all(isinstance(part, str) for part in relaunch):
            raise SystemExit("--cmd-json must be a JSON list of strings")
        return _restart_main(relaunch, args.delay)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
