from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from playwright.sync_api import sync_playwright

from core.config import STORAGE_DIR, get_setting, setting_path

LOGGER = logging.getLogger("short_video_distributor")

GOOGLE_SESSION_COOKIE_NAMES = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
}


def _find_chrome_executable() -> str | None:
    candidates = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _pick_free_port(preferred: int = 9222) -> int:
    for port in (preferred, 9223, 9224, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return int(sock.getsockname()[1])
            except OSError:
                continue
    raise RuntimeError("Could not allocate a local debugging port for Chrome.")


def _fetch_cdp_tabs(port: int) -> list[dict[str, Any]]:
    url = f"http://127.0.0.1:{port}/json/list"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
    except Exception:
        pass
    return []


def _is_flow_project_tab_ready(tabs: list[dict[str, Any]]) -> bool:
    for tab in tabs:
        if tab.get("type") != "page":
            continue
        url = str(tab.get("url") or "").lower()
        if not url or "accounts.google.com" in url:
            continue
        if "/project/" in url and ("flow.google" in url or "labs.google" in url):
            if "/about" not in url:
                return True
    return False


def _wait_for_login_without_playwright(
    port: int,
    proc: subprocess.Popen[Any],
    allow_enter: bool,
    require_confirmation: bool = False,
) -> None:
    enter_pressed = threading.Event()
    if allow_enter:
        if require_confirmation:
            print(
                "\n>>> Hãy chuyển sang tài khoản Google muốn dùng trong cửa sổ Chrome.\n"
                ">>> Sau đó mở lại đúng project Flow, kiểm tra avatar tài khoản và nhấn Enter tại đây để lưu auth.json.\n",
                flush=True,
            )
        else:
            print(
                "\n>>> Cửa sổ Google Chrome thật đã mở (không gắn Playwright khi đăng nhập để tránh bị Google chặn/treo).\n"
                ">>> Hãy đăng nhập tài khoản Google trên cửa sổ Chrome đó.\n"
                ">>> Hệ thống sẽ TỰ ĐỘNG lưu auth.json ngay khi vào được trang Project Flow (hoặc bạn có thể bấm Enter tại đây sau khi vào Project)...\n",
                flush=True,
            )

        def _read_enter() -> None:
            while True:
                try:
                    if not sys.stdin.readline():
                        return
                    enter_pressed.set()
                except Exception:
                    return

        threading.Thread(target=_read_enter, daemon=True).start()

    deadline = time.monotonic() + 600.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                "Cửa sổ trình duyệt đã bị đóng trước khi lưu được phiên đăng nhập."
            )
        if enter_pressed.is_set():
            if require_confirmation and not _is_flow_project_tab_ready(
                _fetch_cdp_tabs(port)
            ):
                print(
                    "Chưa phát hiện project Flow. Hãy mở đúng project trong Chrome rồi nhấn Enter lại.",
                    flush=True,
                )
                enter_pressed.clear()
                continue
            LOGGER.info("Đã nhận phím Enter từ người dùng, đang kiểm tra phiên Flow...")
            return

        if require_confirmation:
            time.sleep(1.5)
            continue

        tabs = _fetch_cdp_tabs(port)
        if _is_flow_project_tab_ready(tabs):
            LOGGER.info(
                "Phát hiện trình duyệt đã đăng nhập và mở trang Project Flow. Đang đợi giao diện tải xong..."
            )
            time.sleep(4.0)
            tabs_after = _fetch_cdp_tabs(port)
            if _is_flow_project_tab_ready(tabs_after):
                return

        time.sleep(1.5)
    raise TimeoutError("Hết thời gian chờ đăng nhập Google Flow (10 phút).")


def _normalize_profile_name(raw_name: str | None) -> str:
    cleaned = "".join(
        ch for ch in (raw_name or "").strip().lower() if ch.isalnum() or ch in {"-", "_"}
    )
    return cleaned or "default"


def _resolve_profile_paths(profile_name: str) -> tuple[Path, Path, Path]:
    normalized = _normalize_profile_name(profile_name)
    if normalized == "default":
        destination = setting_path("FLOW_AUTH_FILE", "auth.json")
        profile_dir = STORAGE_DIR / "chrome_flow_profile"
        meta_file = STORAGE_DIR / "flow_profiles" / "default" / "profile_meta.json"
    else:
        base_dir = STORAGE_DIR / "flow_profiles" / normalized
        destination = base_dir / "auth.json"
        profile_dir = base_dir / "chrome_profile"
        meta_file = base_dir / "profile_meta.json"
    return destination, profile_dir, meta_file


def save_flow_session(
    auto_save: bool = False,
    account_mode: str = "keep",
    profile_name: str | None = None,
    project_url_override: str | None = None,
) -> Path:
    if account_mode not in {"keep", "switch"}:
        raise ValueError("account_mode must be 'keep' or 'switch'.")
    if account_mode == "switch" and auto_save:
        raise ValueError(
            "Chế độ đổi tài khoản cần chạy tương tác trong terminal, không dùng --auto-save."
        )
    if account_mode == "switch" and not (sys.stdin and sys.stdin.isatty()):
        raise ValueError(
            "Chế độ đổi tài khoản cần terminal tương tác để xác nhận sau khi đăng nhập."
        )

    normalized_profile = _normalize_profile_name(profile_name)
    destination, profile_dir, meta_file = _resolve_profile_paths(normalized_profile)
    destination.parent.mkdir(parents=True, exist_ok=True)
    meta_file.parent.mkdir(parents=True, exist_ok=True)

    saved_meta: dict[str, Any] = {}
    if meta_file.is_file():
        try:
            saved_meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            saved_meta = {}

    env_profile_url = os.getenv(
        f"FLOW_PROJECT_URL_{normalized_profile.upper()}", ""
    ).strip()
    project_url = (
        (project_url_override or "").strip()
        or env_profile_url
        or str(saved_meta.get("project_url") or "").strip()
        or os.getenv("FLOW_PROJECT_URL", "").strip()
        or "https://labs.google/fx/tools/flow"
    )
    prompt_selector = get_setting(
        "FLOW_PROMPT_SELECTOR",
        '[contenteditable="true"][aria-label="What do you want to create?"]',
    )

    chrome_exe = _find_chrome_executable()
    if not chrome_exe:
        raise FileNotFoundError(
            "Không tìm thấy Google Chrome trên máy. Vui lòng cài đặt Google Chrome."
        )

    profile_dir.mkdir(parents=True, exist_ok=True)
    port = _pick_free_port(9222)

    chrome_cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        project_url,
    ]
    LOGGER.info(
        "Đang mở Google Chrome độc lập [Profile: %s] (port %s) để đăng nhập không bị Google chặn: %s",
        normalized_profile,
        port,
        project_url,
    )
    proc = subprocess.Popen(
        chrome_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    allow_enter = (not auto_save) and bool(sys.stdin and sys.stdin.isatty())
    try:
        _wait_for_login_without_playwright(
            port,
            proc,
            allow_enter=allow_enter,
            require_confirmation=(account_mode == "switch"),
        )

        with sync_playwright() as playwright:
            LOGGER.info(
                "Đang kết nối vào Chrome để trích xuất phiên đăng nhập cho Profile '%s' (%s)...",
                normalized_profile,
                destination,
            )
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )
            try:
                if not browser.contexts:
                    raise RuntimeError("Không tìm thấy context trình duyệt đang mở.")
                context = browser.contexts[0]

                flow_page = None
                for page in context.pages:
                    if "/project/" in page.url or "flow.google" in page.url or "labs.google" in page.url:
                        flow_page = page
                        break
                if flow_page is None and context.pages:
                    flow_page = context.pages[0]

                captured_project_url = project_url
                if flow_page is not None:
                    if "/project/" in (flow_page.url or "") and "accounts.google.com" not in (flow_page.url or ""):
                        captured_project_url = flow_page.url
                    try:
                        flow_page.locator(prompt_selector).first.wait_for(
                            state="visible", timeout=45_000
                        )
                    except Exception:
                        LOGGER.warning(
                            "Khung nhập prompt chưa hiển thị ngay, đang kiểm tra cookie phiên Google..."
                        )

                state = context.storage_state(path=str(destination))
                cookies = state.get("cookies", []) if isinstance(state, dict) else []
                has_google_session = any(
                    isinstance(c, dict)
                    and c.get("name") in GOOGLE_SESSION_COOKIE_NAMES
                    and "google" in str(c.get("domain", ""))
                    for c in cookies
                )
                if not has_google_session:
                    raise RuntimeError(
                        "Chưa tìm thấy cookie đăng nhập Google (SID/__Secure-1PSID). "
                        "Hãy hoàn tất đăng nhập tài khoản Google và vào trang Project Flow."
                    )

                meta_payload = {
                    "profile": normalized_profile,
                    "project_url": captured_project_url,
                    "auth_file": str(destination),
                    "profile_dir": str(profile_dir),
                    "updated_at": int(time.time()),
                }
                meta_file.write_text(
                    json.dumps(meta_payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                default_auth = setting_path("FLOW_AUTH_FILE", "auth.json")
                if not default_auth.is_file():
                    try:
                        shutil.copyfile(destination, default_auth)
                    except Exception:
                        pass

                LOGGER.info(
                    "Đã lưu phiên đăng nhập Google Flow [Profile: %s] thành công (%d cookies) vào: %s",
                    normalized_profile,
                    len(cookies),
                    destination,
                )
            finally:
                browser.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    print(f"Saved Playwright storage state [{normalized_profile}] to {destination}")
    return destination


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Open browser to sign in to Google Flow and save auth.json for one or multiple profiles."
    )
    parser.add_argument(
        "--auto-save",
        action="store_true",
        help="Automatically save auth.json as soon as the Flow project prompt is visible.",
    )
    parser.add_argument(
        "--account-mode",
        choices=("keep", "switch"),
        help="Use the current Google account or switch accounts before saving auth.json.",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Profile name to save (e.g., a, b, c, d, or default). Each profile has its own isolated Chrome session.",
    )
    parser.add_argument(
        "--project-url",
        type=str,
        default=None,
        help="Optional Flow project URL specific to this profile.",
    )
    args = parser.parse_args()
    profile_name = args.profile
    account_mode = args.account_mode or "keep"
    if not args.auto_save and sys.stdin and sys.stdin.isatty():
        if profile_name is None:
            raw_prof = input(
                "Nhập tên Profile muốn lưu (ví dụ: a, b, c, d — nhấn Enter để dùng 'default'): "
            ).strip()
            profile_name = raw_prof or "default"
        if args.account_mode is None:
            print(f"Chọn chế độ đăng nhập cho Profile '{_normalize_profile_name(profile_name)}':")
            print("  1. Tiếp tục dùng / đăng nhập tài khoản trên Profile này (Tự động lưu khi vào Project)")
            print("  2. Đổi sang tài khoản khác trên Profile này (Xác nhận bằng phím Enter)")
            choice = input("Nhập 1 hoặc 2 [1]: ").strip()
            if choice not in {"", "1", "2"}:
                parser.error("Lựa chọn không hợp lệ; hãy nhập 1 hoặc 2.")
            account_mode = "switch" if choice == "2" else "keep"
    save_flow_session(
        auto_save=args.auto_save,
        account_mode=account_mode,
        profile_name=profile_name,
        project_url_override=args.project_url,
    )


if __name__ == "__main__":
    main()
