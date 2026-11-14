from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import flow_accounts, server_control
from core.server_control import ProcInfo
from core.tele import system_bot

ROOT = str(server_control.ROOT_DIR)
VENV_PY = f"{ROOT}\\.venv\\Scripts\\python.exe"
BASE_PY = "C:\\Python314\\python.exe"


def _procs() -> list[ProcInfo]:
    return [
        ProcInfo(1, 0, "explorer.exe", "C:\\Windows\\explorer.exe", "explorer"),
        # dashboard started as `python web_app.py` from the venv (launcher -> interpreter)
        ProcInfo(10, 1, "python.exe", VENV_PY, f"{VENV_PY} web_app.py"),
        ProcInfo(11, 10, "python.exe", BASE_PY, f"{VENV_PY} web_app.py"),
        # listener spawned by the dashboard
        ProcInfo(20, 11, "python.exe", VENV_PY, f'"{VENV_PY}" "{ROOT}\\main.py" --telegram-poll'),
        ProcInfo(21, 20, "python.exe", BASE_PY, f'"{VENV_PY}" "{ROOT}\\main.py" --telegram-poll'),
        # worker spawned by the listener, driving Chrome through Playwright
        ProcInfo(30, 21, "python.exe", VENV_PY, f'"{VENV_PY}" "{ROOT}\\main.py" --produce-telegram'),
        ProcInfo(31, 30, "python.exe", BASE_PY, f'"{VENV_PY}" "{ROOT}\\main.py" --produce-telegram'),
        ProcInfo(32, 31, "node.exe", "node.exe", "node playwright run-driver"),
        ProcInfo(33, 32, "chrome.exe", "chrome.exe", "chrome --user-data-dir=x"),
        # manual Flow login in progress, spawned by the listener
        ProcInfo(40, 21, "python.exe", VENV_PY, f'"{VENV_PY}" "{ROOT}\\scripts\\save_flow_auth.py" --auto-save'),
        ProcInfo(41, 40, "chrome.exe", "chrome.exe", "chrome --remote-debugging-port=9222"),
        # another project's main.py must be ignored
        ProcInfo(50, 1, "python.exe", "D:\\other\\.venv\\Scripts\\python.exe", "python D:\\other\\main.py --auto"),
    ]


class ServerControlTests(unittest.TestCase):
    def test_classify_roles(self) -> None:
        self.assertEqual(server_control._classify("python web_app.py"), ("dashboard", ""))
        self.assertEqual(server_control._classify('"py" "x\\main.py" --telegram-poll'), ("listener", "telegram-poll"))
        self.assertEqual(
            server_control._classify("py main.py --approval-video --external-telegram-listener"),
            ("worker", "approval-video"),
        )
        self.assertEqual(server_control._classify("py scripts\\save_flow_auth.py --profile a"), ("login", ""))
        self.assertIsNone(server_control._classify("py -m core.server_control restart"))
        self.assertIsNone(server_control._classify("py domain.py"))

    def test_project_processes_ignores_other_projects_and_collapses_launchers(self) -> None:
        items = server_control.collapse_launchers(server_control.project_processes(_procs()))
        # the venv launcher half is hidden; the real interpreter is shown
        self.assertEqual(sorted(i.proc.pid for i in items), [11, 21, 31, 40])
        self.assertEqual({i.proc.pid: i.role for i in items}[31], "worker")

    def test_global_python_dashboard_found_through_own_ancestors(self) -> None:
        procs = [
            ProcInfo(10, 1, "python.exe", BASE_PY, "python web_app.py"),
            ProcInfo(20, 10, "python.exe", VENV_PY, f"{VENV_PY} {ROOT}\\main.py --telegram-poll"),
        ]
        self.assertEqual([i.proc.pid for i in server_control.project_processes(procs)], [20])
        self.assertEqual(sorted(i.proc.pid for i in server_control.project_processes(procs, 20)), [10, 20])

    def test_stop_kills_worker_tree_but_not_listener_or_login(self) -> None:
        procs = _procs()
        workers = [i for i in server_control.project_processes(procs, 21) if i.role == "worker"]
        keep = {21, *server_control.ancestors(procs, 21)}
        pids = server_control.kill_set(procs, workers, keep)
        self.assertEqual(set(pids), {30, 31, 32, 33})
        self.assertIn(pids[-1], {30, 31})  # roots last

    def test_restart_kill_set_spares_helper_and_login(self) -> None:
        procs = _procs() + [ProcInfo(60, 21, "python.exe", VENV_PY, f"{VENV_PY} -m core.server_control restart")]
        targets = [
            i for i in server_control.project_processes(procs, 60) if i.role in {"dashboard", "listener", "worker"}
        ]
        pids = set(server_control.kill_set(procs, targets, keep={60}))
        self.assertEqual(pids, {10, 11, 20, 21, 30, 31, 32, 33})

    def test_relaunch_prefers_dashboard(self) -> None:
        self.assertTrue(server_control.relaunch_command(_procs(), 21)[-1].endswith("web_app.py"))
        no_dashboard = [p for p in _procs() if p.pid not in {10, 11}]
        with patch.object(server_control.sys, "argv", ["main.py", "--auto"]):
            command = server_control.relaunch_command(no_dashboard, 21)
        self.assertEqual(command[-2:], [str(server_control.ROOT_DIR / "main.py"), "--auto"])

    def test_format_started(self) -> None:
        self.assertEqual(server_control.format_started("2026-11-14T09:05:30.1234567+07:00"), "14/11 09:05")
        self.assertEqual(server_control.format_started(""), "")

    def test_restart_marker_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "m.json"
            with patch.object(server_control, "RESTART_MARKER_FILE", marker):
                marker.write_text(json.dumps({"chat_id": "1", "requested_at": 1}), encoding="utf-8")
                self.assertIsNone(server_control.pop_restart_marker())
                self.assertFalse(marker.exists())
                server_control.RESTART_MARKER_FILE.write_text(
                    json.dumps({"chat_id": "1", "requested_at": int(__import__("time").time())}), encoding="utf-8"
                )
                self.assertEqual(server_control.pop_restart_marker()["chat_id"], "1")


class FlowAccountTests(unittest.TestCase):
    def test_reads_chrome_identity_and_cookie_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "chrome_profile"
            (profile_dir / "Default").mkdir(parents=True)
            (profile_dir / "Default" / "Preferences").write_text(
                json.dumps({"account_info": [{"email": "a@example.com", "full_name": "An"}]}), encoding="utf-8"
            )
            self.assertEqual(flow_accounts.read_chrome_identity(profile_dir), ("a@example.com", "An"))
            self.assertEqual(flow_accounts.read_chrome_identity(Path(tmp) / "missing"), ("", ""))

            auth = Path(tmp) / "auth.json"
            auth.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {"name": "SID", "domain": ".google.com", "expires": 2000},
                            {"name": "__Secure-1PSID", "domain": ".google.com", "expires": 1500},
                            {"name": "NID", "domain": ".google.com", "expires": 10},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(flow_accounts.read_cookie_summary(auth), (True, 1500))
            auth.write_text(json.dumps({"cookies": [{"name": "NID", "domain": ".google.com"}]}), encoding="utf-8")
            self.assertEqual(flow_accounts.read_cookie_summary(auth), (False, 0))

    def test_classify_url(self) -> None:
        self.assertEqual(flow_accounts.classify_url("https://accounts.google.com/v3/signin"), "login_required")
        self.assertEqual(flow_accounts.classify_url("https://flow.google.com/about/"), "login_required")
        self.assertIsNone(flow_accounts.classify_url("https://flow.google.com/project/abc"))

    def test_check_summary_asks_for_manual_login(self) -> None:
        text = system_bot.check_summary(
            [
                flow_accounts.CheckResult("a", "ok", credits=120),
                flow_accounts.CheckResult("b", "login_required", "Bị chuyển về trang đăng nhập Google"),
            ]
        )
        self.assertIn("120 credit", text)
        self.assertIn("Cần đăng nhập thủ công:</b> b", text)


class ControlStateTests(unittest.TestCase):
    def test_claim_update_runs_each_update_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(system_bot, "CONTROL_STATE_FILE", Path(tmp) / "state.json"):
                self.assertTrue(system_bot.claim_update(100))
                self.assertFalse(system_bot.claim_update(100))
                self.assertFalse(system_bot.claim_update(99))
                self.assertTrue(system_bot.claim_update(101))


if __name__ == "__main__":
    unittest.main()
