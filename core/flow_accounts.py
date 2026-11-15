"""Google Flow account profiles: who is signed in, and is the session still able to open the project.

Used by the Telegram /account menu. Results of live checks are kept in
storage/flow_account_status.json so the list can show the last known state without a browser.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from core import flow_ui
from core.config import STORAGE_DIR, get_setting
from core.flow_runner import FlowProfile, list_flow_profiles

LOGGER = logging.getLogger("short_video_distributor")

STATUS_FILE = STORAGE_DIR / "flow_account_status.json"
GOOGLE_SESSION_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "HSID", "SSID", "APISID"}
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

STATUS_OK = "ok"
STATUS_LOGIN = "login_required"
STATUS_NO_PROJECT = "no_project"
STATUS_ERROR = "error"
STATUS_LABELS = {
    STATUS_OK: "✅ Vào được project",
    STATUS_LOGIN: "❌ Hết phiên — cần đăng nhập lại",
    STATUS_NO_PROJECT: "⚠️ Không mở được project",
    STATUS_ERROR: "⚠️ Lỗi khi kiểm tra",
}

_check_lock = asyncio.Lock()


@dataclass
class AccountInfo:
    profile: str
    email: str = ""
    name: str = ""
    project_url: str = ""
    has_auth_file: bool = False
    has_session_cookie: bool = False
    session_expires_at: int = 0  # earliest expiry of the Google session cookies (0 = unknown/session)


@dataclass
class CheckResult:
    profile: str
    status: str
    detail: str = ""
    credits: int | None = None
    email: str = ""
    reset_at: int | None = None
    reset_text: str = ""
    checked_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def read_chrome_identity(profile_dir: Path) -> tuple[str, str]:
    """(email, full name) of the Google account signed in to this Chrome user-data dir."""
    prefs = profile_dir / "Default" / "Preferences"
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    accounts = data.get("account_info") if isinstance(data, dict) else None
    if not isinstance(accounts, list):
        return "", ""
    for account in accounts:
        if isinstance(account, dict) and account.get("email"):
            return str(account.get("email") or ""), str(account.get("full_name") or "")
    return "", ""


def read_cookie_summary(auth_file: Path) -> tuple[bool, int]:
    """(has Google session cookie, earliest expiry epoch) from a Playwright storage_state file."""
    try:
        state = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, 0
    cookies = state.get("cookies") if isinstance(state, dict) else None
    if not isinstance(cookies, list):
        return False, 0
    expiries: list[int] = []
    found = False
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        if cookie.get("name") in GOOGLE_SESSION_COOKIES and "google" in str(cookie.get("domain", "")):
            found = True
            expires = cookie.get("expires")
            if isinstance(expires, (int, float)) and expires > 0:
                expiries.append(int(expires))
    return found, min(expiries) if expiries else 0


def account_info(profile: FlowProfile) -> AccountInfo:
    email, name = read_chrome_identity(profile.profile_dir)
    has_cookie, expires_at = read_cookie_summary(profile.auth_file)
    return AccountInfo(
        profile=profile.name,
        email=email,
        name=name,
        project_url=profile.project_url,
        has_auth_file=profile.auth_file.is_file(),
        has_session_cookie=has_cookie,
        session_expires_at=expires_at,
    )


def list_accounts() -> list[AccountInfo]:
    return [account_info(profile) for profile in list_flow_profiles() if profile.auth_file.is_file()]


def load_status() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_status(results: list[CheckResult]) -> None:
    data = load_status()
    for result in results:
        data[result.profile] = asdict(result)
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        LOGGER.warning("Không ghi được %s", STATUS_FILE)


def classify_url(url: str) -> str | None:
    """STATUS_LOGIN when Flow bounced us to Google sign-in / the public landing page."""
    lowered = (url or "").lower()
    if "accounts.google.com" in lowered or lowered.rstrip("/").endswith("/about"):
        return STATUS_LOGIN
    return None


def _check_headless() -> bool:
    return get_setting("FLOW_CHECK_HEADLESS", "true").lower() not in {"0", "false", "no"}


async def _open_context(playwright: Any, profile: FlowProfile, headless: bool) -> tuple[Any, Any]:
    """Prefer the profile's own Chrome dir (same as the worker); fall back to auth.json when the
    dir is locked by a running worker."""
    if profile.profile_dir.is_dir():
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile.profile_dir),
                channel="chrome",
                headless=headless,
                chromium_sandbox=True,
                no_viewport=not headless,
            )
            try:
                state = json.loads(profile.auth_file.read_text(encoding="utf-8"))
                if isinstance(state, dict) and isinstance(state.get("cookies"), list):
                    await context.add_cookies(state["cookies"])
            except Exception:
                pass
            return None, context
        except Exception as exc:
            LOGGER.info("[Profile: %s] Không mở được Chrome profile (%s); dùng auth.json.", profile.name, exc)
    try:
        browser = await playwright.chromium.launch(channel="chrome", headless=headless, chromium_sandbox=True)
    except Exception:
        browser = await playwright.chromium.launch(headless=headless, chromium_sandbox=True)
    context = await browser.new_context(storage_state=str(profile.auth_file))
    return browser, context


async def _page_email(page: Any) -> str:
    try:
        labels = await page.evaluate(
            "() => Array.from(document.querySelectorAll('[aria-label],[title],img[alt]'))"
            ".map(e => e.getAttribute('aria-label') || e.getAttribute('title') || e.getAttribute('alt') || '')"
            ".filter(t => t.includes('@'))"
        )
    except Exception:
        return ""
    for label in labels or []:
        match = _EMAIL_RE.search(str(label))
        if match:
            return match.group(0)
    return ""


async def check_profile(playwright: Any, profile: FlowProfile, headless: bool | None = None) -> CheckResult:
    if not profile.auth_file.is_file():
        return CheckResult(profile.name, STATUS_LOGIN, "Chưa có auth.json")
    if not profile.project_url:
        return CheckResult(profile.name, STATUS_NO_PROJECT, "Chưa cấu hình project URL")
    headless = _check_headless() if headless is None else headless
    browser = context = None
    try:
        browser, context = await _open_context(playwright, profile, headless)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(profile.project_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            if classify_url(page.url) is None:
                first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                return CheckResult(profile.name, STATUS_NO_PROJECT, f"Không mở được URL: {first_line[:150]}")
        await asyncio.sleep(3.0)
        if classify_url(page.url):
            return CheckResult(profile.name, STATUS_LOGIN, "Bị chuyển về trang đăng nhập Google")
        prompt = get_setting("FLOW_PROMPT_SELECTOR", '[contenteditable="true"]')
        try:
            await page.locator(f'{prompt}, [contenteditable="true"], textarea').first.wait_for(
                state="visible", timeout=45_000
            )
        except Exception:
            if classify_url(page.url):
                return CheckResult(profile.name, STATUS_LOGIN, "Bị chuyển về trang đăng nhập Google")
            title = ""
            try:
                title = await page.title()
            except Exception:
                pass
            return CheckResult(
                profile.name,
                STATUS_NO_PROJECT,
                f"Đã đăng nhập nhưng không thấy khung prompt của project (trang: {title or page.url})"[:300],
            )
        email = await _page_email(page)
        menu = await flow_ui.read_account_menu(page)
        if menu.credits is not None:
            flow_ui.save_profile_credits(profile.name, menu.credits, menu.reset_at, menu.reset_text)
        try:
            await context.storage_state(path=str(profile.auth_file))  # keep auth.json fresh
        except Exception:
            pass
        return CheckResult(
            profile.name,
            STATUS_OK,
            "",
            credits=menu.credits,
            email=email,
            reset_at=menu.reset_at,
            reset_text=menu.reset_text,
        )
    except Exception as exc:
        LOGGER.exception("[Profile: %s] Kiểm tra tài khoản Flow lỗi.", profile.name)
        return CheckResult(profile.name, STATUS_ERROR, str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__)
    finally:
        for closer in (context, browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass


ProgressCallback = Callable[[FlowProfile, int, int], Awaitable[None]]


def is_checking() -> bool:
    return _check_lock.locked()


async def check_profiles(
    names: list[str] | None = None,
    progress: ProgressCallback | None = None,
) -> list[CheckResult]:
    """Open each profile's project in turn and record whether it is usable."""
    from playwright.async_api import async_playwright

    profiles = [p for p in list_flow_profiles() if p.auth_file.is_file()]
    if names:
        wanted = {name.lower() for name in names}
        profiles = [p for p in profiles if p.name in wanted]
    results: list[CheckResult] = []
    async with _check_lock:
        async with async_playwright() as playwright:
            for index, profile in enumerate(profiles, start=1):
                if progress is not None:
                    try:
                        await progress(profile, index, len(profiles))
                    except Exception:
                        pass
                result = await check_profile(playwright, profile)
                LOGGER.info(
                    "[Profile: %s] Kiểm tra tài khoản Flow: %s %s", profile.name, result.status, result.detail
                )
                results.append(result)
                save_status([result])
    return results
