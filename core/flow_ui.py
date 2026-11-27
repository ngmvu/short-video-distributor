"""Language-neutral Google Flow composer helpers (non-Agent, scene-by-scene mode).

The Flow UI follows the Google account language (vi / en ...), so controls are matched by
Material icon ligatures (``videocam``, ``crop_9_16``, ``accessibility_new``...), CSS classes and
regexes covering both languages — never by translated aria-labels alone.

Settings come from runtime prefs (storage/runtime_prefs.json, editable on the dashboard
"Flow" page) with .env fallbacks — see ``flow_settings()``.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from core.config import get_runtime_pref

LOGGER = logging.getLogger("short_video_distributor")

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

FLOW_SETTING_DEFAULTS: dict[str, str] = {
    "FLOW_GENERATION_MODE": "scene",      # scene = paste each scene, Agent OFF | agent = Flow Agent
    "FLOW_INPUT_MODE": "ingredients",     # ingredients (Thành phần) | frames (Khung hình)
    "FLOW_ASPECT_RATIO": "9:16",          # 9:16 | 16:9
    "FLOW_VIDEO_MODEL": "Omni 1.1 Flash",  # text of the model menu item
    "FLOW_RESOLUTION": "720p",            # 720p | 360p
    "FLOW_DURATION": "auto",              # fallback when the prompt has no "duration N seconds": auto | 4 | 6 | 8 | 10
    "FLOW_OUTPUT_COUNT": "1",             # 1..4
    "FLOW_DOWNLOAD_QUALITY": "1080",      # 720 | 1080 | 4k
    "FLOW_ATTACH_VOICE": "true",          # also attach @Voice_* ingredients
    "FLOW_PROFILES": "d",                 # account profile order (comma separated)
    "FLOW_MIN_CREDITS": "20",             # switch to the next profile when credits drop below this (0 = off)
    "FLOW_FREE_MAX_CREDITS": "60",        # accounts with fewer credits count as free and run first (0 = off)
}

FLOW_SETTING_CHOICES: dict[str, list[str]] = {
    "FLOW_GENERATION_MODE": ["scene", "agent"],
    "FLOW_INPUT_MODE": ["ingredients", "frames"],
    "FLOW_ASPECT_RATIO": ["9:16", "16:9"],
    "FLOW_VIDEO_MODEL": ["Omni 1.1 Flash", "Omni 1.2 Flash", "Veo 3.1 - Lite", "Veo 3.1 - Fast", "Veo 3.1 - Quality"],
    "FLOW_RESOLUTION": ["720p", "360p"],
    "FLOW_DURATION": ["auto", "10", "8", "6", "4"],
    "FLOW_OUTPUT_COUNT": ["1", "2", "3", "4"],
    "FLOW_DOWNLOAD_QUALITY": ["1080", "720", "4k"],
    "FLOW_ATTACH_VOICE": ["true", "false"],
    "FLOW_MIN_CREDITS": ["20", "30", "50", "100", "0"],
    "FLOW_FREE_MAX_CREDITS": ["60", "100", "200", "0"],
}

# (key, label, help, {value: display label}) — shared by the dashboard page and Telegram /flow.
FLOW_SETTINGS_FORM: tuple[tuple[str, str, str, dict[str, str]], ...] = (
    ("FLOW_GENERATION_MODE", "Chế độ tạo", "scene = tắt Agent, dán từng cảnh; agent = để Flow Agent tự làm cả kịch bản.",
     {"scene": "scene — tắt Agent, dán từng cảnh", "agent": "agent — Flow Agent"}),
    ("FLOW_INPUT_MODE", "Kiểu đầu vào", "", {"ingredients": "Thành phần (Ingredients)", "frames": "Khung hình (Frames)"}),
    ("FLOW_ASPECT_RATIO", "Tỉ lệ khung hình", "", {}),
    ("FLOW_VIDEO_MODEL", "Mô hình video", "Tên đúng như menu mô hình trên Flow.", {}),
    ("FLOW_RESOLUTION", "Độ phân giải tạo", "", {}),
    ("FLOW_DURATION", "Thời lượng mỗi cảnh (khi prompt không ghi)",
     "Prompt ghi 'duration 8 seconds' / '8s' / 'thời lượng 8 giây' thì luôn dùng số đó (làm tròn lên 4/6/8/10s). "
     "Mục này chỉ dùng khi prompt không ghi; auto = ước theo độ dài thoại.",
     {"auto": "auto (theo prompt / độ dài thoại)"}),
    ("FLOW_OUTPUT_COUNT", "Số video mỗi lần tạo", "", {"1": "x1", "2": "x2", "3": "x3", "4": "x4"}),
    ("FLOW_DOWNLOAD_QUALITY", "Chất lượng tải về", "1080p/4K: Flow tăng độ phân giải ~1 phút trước khi tải.",
     {"1080": "1080p (tăng độ phân giải)", "720": "720p gốc", "4k": "4K (tăng độ phân giải)"}),
    ("FLOW_ATTACH_VOICE", "Gắn cả Voice asset (@Voice_…)", "", {"true": "Có", "false": "Không"}),
    ("FLOW_PROFILES", "Thứ tự profile tài khoản", "Ví dụ: d,a,b,c — profile đầu tiên được dùng trước, hết credit tự chuyển.", {}),
    ("FLOW_MIN_CREDITS", "Chuyển tài khoản khi credit dưới",
     "Trước mỗi cảnh đọc số credit Flow; dưới mức này thì chuyển sang profile kế tiếp.",
     {"20": "20 credit", "30": "30 credit", "50": "50 credit", "100": "100 credit", "0": "Tắt kiểm tra"}),
    ("FLOW_FREE_MAX_CREDITS", "Tài khoản miễn phí = dưới",
     "Tài khoản còn ít credit hơn mức này (hoặc hồi credit hằng ngày) được coi là miễn phí và chạy TRƯỚC; "
     "lỗi / hết credit thì chuyển sang tài khoản miễn phí khác, cuối cùng mới tới tài khoản nhiều credit.",
     {"60": "60 credit", "100": "100 credit", "200": "200 credit", "0": "Tắt (chỉ theo hồi hằng ngày)"}),
)


def flow_setting_display(key: str, value: str) -> str:
    """Human label for a setting value (e.g. '1080' -> '1080p (tăng độ phân giải)')."""
    for form_key, _label, _help, labels in FLOW_SETTINGS_FORM:
        if form_key == key:
            if key == "FLOW_DURATION" and value.isdigit():
                return f"{value} giây"
            return labels.get(value, value)
    return value


DURATION_OPTIONS = (4, 6, 8, 10)


@dataclass(frozen=True)
class FlowSettings:
    mode: str
    input_mode: str
    aspect_ratio: str
    video_model: str
    resolution: str
    duration: str
    output_count: str
    download_quality: str
    attach_voice: bool


def flow_setting(name: str) -> str:
    return (get_runtime_pref(name, FLOW_SETTING_DEFAULTS[name]) or FLOW_SETTING_DEFAULTS[name]).strip()


def flow_settings() -> FlowSettings:
    mode = flow_setting("FLOW_GENERATION_MODE").lower()
    return FlowSettings(
        mode="agent" if mode == "agent" else "scene",
        input_mode="frames" if flow_setting("FLOW_INPUT_MODE").lower().startswith("frame") else "ingredients",
        aspect_ratio="16:9" if flow_setting("FLOW_ASPECT_RATIO") == "16:9" else "9:16",
        video_model=flow_setting("FLOW_VIDEO_MODEL"),
        resolution="360p" if flow_setting("FLOW_RESOLUTION").startswith("360") else "720p",
        duration=flow_setting("FLOW_DURATION").lower().rstrip("s"),
        output_count=flow_setting("FLOW_OUTPUT_COUNT").lstrip("xX") or "1",
        download_quality=flow_setting("FLOW_DOWNLOAD_QUALITY").lower().rstrip("p"),
        attach_voice=flow_setting("FLOW_ATTACH_VOICE").lower() in {"1", "true", "yes"},
    )


def _snap_to_option(seconds: int) -> int:
    """Flow only offers 4/6/8/10s: round UP to the nearest option (cap at the longest)."""
    for option in DURATION_OPTIONS:
        if option >= seconds:
            return option
    return DURATION_OPTIONS[-1]


# "duration 10 seconds", "duration: 8s", "8-second clip", "8 second long", "thời lượng 8 giây", "(6 giây)"
_PROMPT_DURATION_PATTERNS = (
    re.compile(r"\bduration\s*[:=]?\s*(?:of\s+|is\s+)?(\d{1,2})\s*(?:s|secs?|seconds?|giây)\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,2})\s*-?\s*seconds?\s+(?:long|clip|video|scene|duration)\b", re.IGNORECASE),
    re.compile(r"\bthời\s*lượng\s*[:=]?\s*(\d{1,2})\s*(?:s|giây)\b", re.IGNORECASE),
    re.compile(r"\(\s*(\d{1,2})\s*(?:s|giây)\s*\)", re.IGNORECASE),
)


def requested_duration_seconds(scene: dict[str, Any]) -> int | None:
    """Duration the script asks for: scene["duration"] ("8s", "8 giây") or text in the prompt
    ("duration 10 seconds"). None when the scene leaves it to auto."""
    raw = str(scene.get("duration") or "").strip().lower()
    match = re.fullmatch(r"(\d{1,2})\s*(?:s|sec|secs|seconds?|giây)?", raw)
    if match and int(match.group(1)) > 0:
        return int(match.group(1))
    prompt = str(scene.get("prompt") or "")
    for pattern in _PROMPT_DURATION_PATTERNS:
        found = pattern.search(prompt)
        if found and int(found.group(1)) > 0:
            return int(found.group(1))
    return None


def pick_duration_seconds(
    settings: FlowSettings, estimated_seconds: int, requested_seconds: int | None = None
) -> int:
    """Duration written in the scene/prompt wins; else the configured value; else (auto) the
    shortest option that fits the dialogue. Always snapped to an option Flow offers."""
    if requested_seconds:
        return _snap_to_option(requested_seconds)
    if settings.duration.isdigit():
        return _snap_to_option(int(settings.duration))
    return _snap_to_option(estimated_seconds)


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------

# Old UI: a chip; new UI (Nov 2026): a switch with spark / spark_off icon. Both carry aria-pressed.
AGENT_CHIP = "flow-agent-mode-toggle-chip button, flow-agent-mode-switch button"
SETTINGS_TRIGGER = (
    'button.settings-trigger-button, button[aria-label="Settings trigger"], '
    'button[aria-label="Điều kiện kích hoạt cài đặt"]'
)
OVERLAY = ".cdk-overlay-container"
PROMPT_BOX = (
    '[contenteditable="true"].ProseMirror:visible, '
    '[contenteditable="true"][aria-label="What do you want to create?"]:visible, '
    '[contenteditable="true"][aria-label^="Bạn muốn tạo"]:visible'
)
SUBMIT_BUTTON = (
    'flow-generate-icon-button button:visible, button[aria-label="Start generation"]:visible, '
    'button[aria-label="Bắt đầu tạo"]:visible'
)
ADD_INGREDIENT_BUTTON = (
    'button[aria-label="Add ingredients to the prompt box"], '
    'button[aria-label="Thêm thành phần vào ô nhập câu lệnh"]'
)
DOWNLOAD_BUTTON = (
    'button[aria-label="Download media"]:visible, '
    'button[aria-label="Tải nội dung nghe nhìn xuống"]:visible, '
    'button[aria-haspopup="menu"]:has(mat-icon:text-is("download")):visible, '
    'button[aria-haspopup="menu"]:has-text("download"):visible'
)


def _radio(page: Any, text_regex: str) -> Any:
    """Visible settings radio whose text matches the regex (panel is not in the CDK overlay)."""
    return (
        page.locator('button[role="radio"]')
        .filter(has_text=re.compile(text_regex, re.IGNORECASE))
        .locator("visible=true")
        .first
    )


def _duration_regex(seconds: int) -> str:
    return rf"^\s*{seconds}\s*(s|sec|giây)\s*$"


# ---------------------------------------------------------------------------
# Composer actions
# ---------------------------------------------------------------------------

OVERLAY_BACKDROP = ".cdk-overlay-backdrop.cdk-overlay-backdrop-showing"


async def close_overlays(page: Any, attempts: int = 4) -> None:
    """Close any open CDK menu/dialog (e.g. the PRO account menu) whose transparent backdrop
    would otherwise intercept clicks on the composer."""
    for _ in range(attempts):
        backdrop = page.locator(OVERLAY_BACKDROP)
        try:
            if not await backdrop.count():
                return
        except Exception:
            return
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            if not await backdrop.count():
                return
            close_button = page.locator(
                f'{OVERLAY} button:has-text("close"):visible, '
                f'{OVERLAY} button[aria-label="Close"]:visible, {OVERLAY} button[aria-label="Đóng"]:visible'
            ).first
            if await close_button.count():
                await close_button.click(timeout=2_000)
                await asyncio.sleep(0.5)
                if not await backdrop.count():
                    return
            # Clicking the backdrop itself dismisses CDK overlays.
            await backdrop.first.click(position={"x": 5, "y": 5}, timeout=2_000, force=True)
            await asyncio.sleep(0.5)
        except Exception:
            await asyncio.sleep(0.3)
    if await page.locator(OVERLAY_BACKDROP).count():
        LOGGER.warning("Vẫn còn lớp phủ (menu/hộp thoại) trên Flow sau khi thử đóng.")


async def ensure_agent_off(page: Any) -> None:
    """Close the Agent side panel (if docked) and switch the composer chip off Agent mode."""
    await close_overlays(page)
    chip = page.locator(AGENT_CHIP).first
    if not await chip.count():
        # Agent panel docked on the right hides the bottom composer: close the panel.
        closed = await page.evaluate(
            """() => {
                const newSession = Array.from(document.querySelectorAll('button'))
                    .find(b => (b.innerText || '').trim() === 'edit_square' && b.offsetParent);
                const scope = newSession ? newSession.parentElement : null;
                const close = scope && Array.from(scope.querySelectorAll('button'))
                    .find(b => (b.innerText || '').trim() === 'close');
                if (close) { close.click(); return true; }
                return false;
            }"""
        )
        if closed:
            LOGGER.info("Đã đóng khung Agent bên phải của Flow.")
            await asyncio.sleep(2.0)
        await chip.wait_for(state="visible", timeout=15_000)
    if await chip.get_attribute("aria-pressed") == "true":
        await chip.click(timeout=8_000)
        await asyncio.sleep(1.2)
        LOGGER.info("Đã TẮT chế độ Agent (Tác nhân) trên khung nhập Flow.")
    if await chip.get_attribute("aria-pressed") == "true":
        raise RuntimeError("Không tắt được chế độ Agent trên Google Flow.")


async def _click_radio(page: Any, text_regex: str, label: str, required: bool = True) -> bool:
    radio = _radio(page, text_regex)
    try:
        await radio.wait_for(state="visible", timeout=8_000)
    except Exception:
        if required:
            raise RuntimeError(f"Không tìm thấy lựa chọn '{label}' trong bảng cài đặt Flow.")
        LOGGER.warning("Không thấy lựa chọn '%s' trong bảng cài đặt Flow; bỏ qua.", label)
        return False
    if await radio.get_attribute("aria-checked") != "true":
        await radio.click(timeout=5_000)
        await asyncio.sleep(0.6)
    return True


async def _select_model(page: Any, model_name: str) -> None:
    trigger = page.locator(
        'button[aria-label="Chọn nhóm mô hình"]:visible, '
        'button[aria-label="Select model group"]:visible, '
        'button[aria-label*="model" i][aria-haspopup="menu"]:visible'
    ).last
    if not await trigger.count():
        trigger = page.locator(
            'button[aria-haspopup="menu"]:has-text("arrow_drop_down"):visible'
        ).last
    await trigger.wait_for(state="visible", timeout=8_000)
    current = re.sub(r"\s*arrow_drop_down\s*$", "", (await trigger.inner_text()).strip())
    if current.lower() == model_name.lower():
        return
    await trigger.click(timeout=5_000)
    await asyncio.sleep(0.8)
    item = page.locator('[role="menuitem"]:visible').filter(
        has_text=re.compile(rf"^\s*{re.escape(model_name)}", re.IGNORECASE)
    ).first
    try:
        await item.wait_for(state="visible", timeout=6_000)
    except Exception as exc:
        await page.keyboard.press("Escape")
        raise RuntimeError(
            f"Flow không còn mô hình '{model_name}' (hiện tại: {current}). "
            "Đổi FLOW_VIDEO_MODEL trên trang cài đặt Flow."
        ) from exc
    await item.click(timeout=5_000)
    await asyncio.sleep(1.0)
    LOGGER.info("Đã chọn mô hình video Flow: %s", model_name)


async def configure_composer(page: Any, settings: FlowSettings, duration_seconds: int) -> str:
    """Open the composer settings and apply: Video, input mode, ratio, model, res, duration, count.

    Returns the settings-trigger summary text (e.g. "Video · 720p · 10 giây crop_9_16 x1").
    """
    await close_overlays(page)
    trigger = page.locator(SETTINGS_TRIGGER).first
    await trigger.wait_for(state="visible", timeout=15_000)
    await trigger.click(timeout=8_000)
    await asyncio.sleep(1.2)
    try:
        await _apply_settings(page, settings, duration_seconds)
    except Exception:
        await save_debug_screenshot(page, "configure_composer")
        raise
    finally:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.8)
        await close_overlays(page)
    summary = re.sub(r"\s+", " ", (await trigger.inner_text()).strip())
    LOGGER.info("Cài đặt Flow hiện tại: %s", summary)
    return summary


async def save_debug_screenshot(page: Any, name: str) -> None:
    from core.config import STORAGE_DIR
    import time as _time

    try:
        folder = STORAGE_DIR / "flow_debug"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{int(_time.time())}-{name}.png"
        await page.screenshot(path=str(path))
        LOGGER.warning("Đã lưu ảnh chụp lỗi Flow: %s", path)
    except Exception:
        pass


async def _apply_settings(page: Any, settings: FlowSettings, duration_seconds: int) -> None:
    await _click_radio(page, r"videocam", "Video")
    await _click_radio(
        page,
        r"crop_free" if settings.input_mode == "frames" else r"chrome_extension",
        "Khung hình" if settings.input_mode == "frames" else "Thành phần",
    )
    await _click_radio(
        page,
        r"crop_9_16" if settings.aspect_ratio == "9:16" else r"crop_16_9",
        settings.aspect_ratio,
    )
    await _select_model(page, settings.video_model)
    await _click_radio(page, rf"^\s*{settings.resolution}", settings.resolution, required=False)
    has_duration = await _click_radio(
        page, _duration_regex(duration_seconds), f"{duration_seconds}s", required=False
    )
    if not has_duration:
        LOGGER.warning(
            "Mô hình '%s' không có lựa chọn thời lượng; Flow tự quyết định độ dài clip.",
            settings.video_model,
        )
    await _click_radio(page, rf"^\s*x{settings.output_count}\s*$", f"x{settings.output_count}")


async def attach_ingredients(page: Any, tags: list[str]) -> None:
    """Attach saved Character (@khi) / Voice (@Voice_Khi) ingredients to the prompt."""
    for tag in dict.fromkeys(t.strip() for t in tags if t.strip()):
        is_voice = tag.lower().startswith("@voice")
        tab_icon = "voice_selection" if is_voice else "accessibility_new"
        await close_overlays(page)
        await page.locator(ADD_INGREDIENT_BUTTON).first.click(timeout=8_000)
        await asyncio.sleep(1.2)
        tab = page.locator(f'{OVERLAY} [role="tab"]:has-text("{tab_icon}")').first
        if await tab.count():
            await tab.click(timeout=5_000)
            await asyncio.sleep(1.0)
        option = page.locator(f'{OVERLAY} [role="option"]').filter(
            has_text=re.compile(rf"^\s*{re.escape(tag)}(\s|$)", re.IGNORECASE)
        ).first
        try:
            await option.wait_for(state="visible", timeout=8_000)
        except Exception:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            if is_voice:
                LOGGER.warning("Không thấy Voice asset %s trong Flow; bỏ qua.", tag)
                continue
            raise RuntimeError(f"Không thấy Character asset {tag} trong project Flow.")
        await option.click(timeout=5_000)
        await asyncio.sleep(1.0)
        confirm = page.get_by_role(
            "button", name=re.compile(r"^(Add to prompt|Thêm vào câu lệnh|Thêm)$", re.IGNORECASE)
        )
        if await confirm.count() and await confirm.first.is_visible():
            await confirm.first.click(timeout=5_000)
            await asyncio.sleep(0.8)
        if await page.locator(f'{OVERLAY} [role="option"]').count():
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.6)
        LOGGER.info("Đã gắn thành phần %s vào prompt Flow.", tag)


async def fill_prompt_and_submit(page: Any, prompt_text: str, ingredient_tags: list[str]) -> None:
    await close_overlays(page)
    box = page.locator(PROMPT_BOX).last
    await box.click(timeout=10_000)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await page.keyboard.insert_text(prompt_text)
    await asyncio.sleep(1.5)
    if ingredient_tags:
        await attach_ingredients(page, ingredient_tags)
    await close_overlays(page)
    submit = page.locator(SUBMIT_BUTTON).last
    await submit.wait_for(state="visible", timeout=10_000)
    await submit.click(timeout=8_000)
    LOGGER.info("Đã gửi prompt cảnh cho Google Flow.")


ACCOUNT_MENU_BUTTON = (
    # new UI: the avatar opens an "account settings" panel holding the credits row
    'flow-header-user-icon [role="button"]:visible, '
    'button:has-text("PRO"):visible, button:has-text("ULTRA"):visible, '
    '[role="button"]:has-text("PRO"):visible'
)
# vi: "491 tín dụng Google Flow" · en: "1,250 Google Flow credits" / "12 AI credits"
_CREDITS_RE = re.compile(
    r"(\d[\d.,]*)\s*(?:Google\s+Flow\s+|AI\s+)?(?:tín dụng|credits?)\b", re.IGNORECASE
)
CREDITS_FILE_NAME = "flow_credits.json"


def min_credits() -> int:
    raw = flow_setting("FLOW_MIN_CREDITS")
    return int(raw) if raw.isdigit() else 20


def free_max_credits() -> int:
    raw = flow_setting("FLOW_FREE_MAX_CREDITS")
    return int(raw) if raw.isdigit() else 60


def parse_credits(text: str) -> int | None:
    """'491 tín dụng Google Flow' / '1,250 Google Flow credits' -> 491 / 1250."""
    for line in (text or "").splitlines():
        match = _CREDITS_RE.search(line)
        if match:
            digits = re.sub(r"\D", "", match.group(1))
            if digits:
                return int(digits)
    return None


# vi: "Đặt lại lúc 01:27 vào 7 thg 12" · en (best effort): "Resets at 1:27 AM on Dec 7"
# daily plans (new UI): "Số tín dụng được làm mới hằng ngày vào lúc 17:54" · "Credits refresh daily at 5:54 PM"
_RESET_LINE_RE = re.compile(
    r"^.*\b(?:đặt lại|làm mới|resets?|renews?|refresh(?:es|ed)?)\b.*$", re.IGNORECASE | re.MULTILINE
)
_DAILY_RE = re.compile(r"hằng ngày|hàng ngày|mỗi ngày|daily|every day", re.IGNORECASE)
_RESET_VI_RE = re.compile(
    r"(\d{1,2}):(\d{2}).*?(\d{1,2})\s*(?:thg|tháng)\s*(\d{1,2})(?:\D+(\d{4}))?", re.IGNORECASE
)
_MONTHS_EN = {
    m: i
    for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)
}
_RESET_EN_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AP]M)?", re.IGNORECASE)
_RESET_EN_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?", re.IGNORECASE
)


@dataclass(frozen=True)
class AccountMenuInfo:
    credits: int | None
    reset_at: int | None = None  # epoch seconds of the next credit reset
    reset_text: str = ""  # the raw line shown by Flow, kept even when the date cannot be parsed


def parse_reset(text: str, now: float | None = None) -> tuple[int | None, str]:
    """Credit-reset line of the account menu -> (epoch of the reset, raw line).

    Flow omits the year, so the next occurrence after `now` is used."""
    import time
    from datetime import datetime, timedelta

    match = _RESET_LINE_RE.search(text or "")
    if not match:
        return None, ""
    line = match.group(0).strip()
    hour = minute = day = month = None
    year: int | None = None
    vi = _RESET_VI_RE.search(line)
    if vi:
        hour, minute, day, month = (int(vi.group(i)) for i in range(1, 5))
        year = int(vi.group(5)) if vi.group(5) else None
    else:
        en_time, en_date = _RESET_EN_TIME_RE.search(line), _RESET_EN_DATE_RE.search(line)
        if en_time and en_date:
            hour, minute = int(en_time.group(1)), int(en_time.group(2))
            meridiem = (en_time.group(3) or "").upper()
            if meridiem == "PM" and hour < 12:
                hour += 12
            elif meridiem == "AM" and hour == 12:
                hour = 0
            month, day = _MONTHS_EN[en_date.group(1).lower()[:3]], int(en_date.group(2))
            year = int(en_date.group(3)) if en_date.group(3) else None
    current = datetime.fromtimestamp(time.time() if now is None else now)
    if hour is None and _DAILY_RE.search(line):
        daily = _RESET_EN_TIME_RE.search(line)
        if daily:
            hour, minute = int(daily.group(1)), int(daily.group(2))
            meridiem = (daily.group(3) or "").upper()
            if meridiem == "PM" and hour < 12:
                hour += 12
            elif meridiem == "AM" and hour == 12:
                hour = 0
            try:
                when = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except ValueError:
                return None, line
            if when <= current:
                when += timedelta(days=1)  # next daily refresh
            return int(when.timestamp()), line
    if hour is None:
        return None, line
    try:
        when = datetime(year or current.year, month, day, hour, minute)
        if year is None and when < current - timedelta(days=1):
            when = when.replace(year=current.year + 1)
    except ValueError:
        return None, line
    return int(when.timestamp()), line


def parse_account_menu(text: str, now: float | None = None) -> AccountMenuInfo:
    reset_at, reset_text = parse_reset(text, now)
    return AccountMenuInfo(parse_credits(text), reset_at, reset_text)


async def read_account_menu(page: Any) -> AccountMenuInfo:
    """Open the account (PRO) menu, read credits + credit-reset time, close it."""
    button = page.locator(ACCOUNT_MENU_BUTTON).first
    try:
        if not await button.count():
            return AccountMenuInfo(None)
        await button.click(timeout=5_000)
        await asyncio.sleep(1.5)
        text = await page.evaluate(
            "() => { const o = document.querySelector('.cdk-overlay-container'); return o ? o.innerText : ''; }"
        )
        return parse_account_menu(text)
    except Exception as exc:
        LOGGER.warning("Không đọc được số credit Flow: %s", exc)
        return AccountMenuInfo(None)
    finally:
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.6)
        except Exception:
            pass
        await close_overlays(page)


async def read_credits(page: Any) -> int | None:
    """Credits shown in the account menu ('N tín dụng Google Flow'). None when not found."""
    return (await read_account_menu(page)).credits


def save_profile_credits(
    profile_name: str, credits: int, reset_at: int | None = None, reset_text: str = ""
) -> None:
    import json
    import time

    from core.config import STORAGE_DIR

    path = STORAGE_DIR / CREDITS_FILE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    now = int(time.time())
    entry: dict[str, Any] = {"credits": credits, "checked_at": now}
    previous = data.get(profile_name) if isinstance(data.get(profile_name), dict) else {}
    if reset_at or reset_text:
        entry.update({"reset_at": reset_at, "reset_text": reset_text})
    elif previous.get("reset_at") and int(previous["reset_at"]) > now:
        # not read this time: keep the known reset moment while it is still ahead
        entry.update({"reset_at": previous["reset_at"], "reset_text": previous.get("reset_text", "")})
    data[profile_name] = entry
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def format_reset(info: dict[str, Any] | None, now: float | None = None) -> str:
    """'hồi credit 01:27 07/12 (còn 21 ngày)' from a flow_credits.json entry; '' when unknown."""
    import time

    if not info:
        return ""
    reset_at = info.get("reset_at")
    if not reset_at:
        return f"hồi credit: {info['reset_text']}" if info.get("reset_text") else ""
    now = time.time() if now is None else now
    stamp = time.strftime("%H:%M %d/%m", time.localtime(int(reset_at)))
    left = int(reset_at) - now
    if left <= 0:
        return f"đã tới mốc hồi credit {stamp} — kiểm tra lại"
    if left >= 86_400:
        remaining = f"còn {int(left // 86_400)} ngày"
    elif left >= 3_600:
        remaining = f"còn {int(left // 3_600)} giờ"
    else:
        remaining = f"còn {max(1, int(left // 60))} phút"
    return f"hồi credit {stamp} ({remaining})"


def load_profile_credits() -> dict[str, dict[str, int]]:
    import json

    from core.config import STORAGE_DIR

    try:
        data = json.loads((STORAGE_DIR / CREDITS_FILE_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# Account priority: spend free accounts first — daily-refresh ones, or any account holding fewer
# than FLOW_FREE_MAX_CREDITS (60) — and keep the accounts with many credits for when they run dry.
TIER_DAILY, TIER_UNKNOWN, TIER_PAID, TIER_EXHAUSTED = 0, 1, 2, 3
TIER_LABELS = {
    TIER_DAILY: "miễn phí",
    TIER_UNKNOWN: "chưa rõ credit",
    TIER_PAID: "nhiều credit",
    TIER_EXHAUSTED: "hết credit, chờ hồi",
}


def is_daily_plan(info: dict[str, Any] | None) -> bool:
    """Free accounts show 'làm mới hằng ngày' / 'refresh daily'; paid ones reset monthly."""
    return bool(info and _DAILY_RE.search(str(info.get("reset_text") or "")))


def profile_tier(
    info: dict[str, Any] | None, now: float | None = None, threshold: int | None = None,
    free_max: int | None = None,
) -> int:
    """Priority group of a profile from its last flow_credits.json entry (lower runs first):
    free (daily refresh, or fewer than FLOW_FREE_MAX_CREDITS) → unknown → many credits → exhausted."""
    import time

    if not info or info.get("credits") is None:
        return TIER_UNKNOWN
    now = time.time() if now is None else now
    threshold = min_credits() if threshold is None else threshold
    reset_at = info.get("reset_at")
    refreshed_since = bool(reset_at and int(reset_at) <= now)
    if threshold > 0 and int(info["credits"]) < threshold and not refreshed_since:
        return TIER_EXHAUSTED
    free_max = free_max_credits() if free_max is None else free_max
    if is_daily_plan(info) or (free_max > 0 and int(info["credits"]) < free_max):
        return TIER_DAILY
    return TIER_PAID


def profile_sort_key(info: dict[str, Any] | None) -> tuple[int, float]:
    """Tier first; among free accounts the one whose credits refresh soonest runs first
    (its leftover credits are lost at the refresh anyway)."""
    tier = profile_tier(info)
    reset_at = (info or {}).get("reset_at")
    soonest = float(reset_at) if tier == TIER_DAILY and reset_at else float("inf")
    return tier, soonest


def estimate_scene_seconds(scene: dict[str, Any]) -> int:
    dialogue = str(scene.get("dialogue") or "").strip()
    if not dialogue:
        match = re.search(r'["“]([^"”]{2,})["”]', str(scene.get("prompt") or ""))
        dialogue = match.group(1) if match else ""
    words = len(dialogue.split())
    return max(4, math.ceil(words / 3 + 2))


async def is_menu_item_disabled(item: Any) -> bool:
    """Flow greys out upscaled downloads (1080p / 4K) on plans that don't include them."""
    try:
        return bool(
            await item.get_attribute("disabled") is not None
            or await item.get_attribute("aria-disabled") == "true"
            or "mat-mdc-menu-item-disabled" in (await item.get_attribute("class") or "")
        )
    except Exception:
        return False


def quality_menu_regex(quality: str) -> re.Pattern[str]:
    if quality == "4k":
        return re.compile(r"^\s*4K", re.IGNORECASE)
    if quality == "720":
        return re.compile(r"^\s*720\s*p", re.IGNORECASE)
    return re.compile(r"^\s*1080\s*p", re.IGNORECASE)
