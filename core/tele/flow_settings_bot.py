"""Telegram /flow — view and change the Google Flow settings (same as dashboard "Cấu hình › Google Flow").

Values are saved to storage/runtime_prefs.json and apply to the next video, no restart needed.
Callback data: "flowcfg:menu" | "flowcfg:edit:<KEY>" | "flowcfg:set:<KEY>:<choice index>"
               | "flowcfg:prof:<profile name>"
"""
from __future__ import annotations

import html
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

from core.config import STORAGE_DIR, set_runtime_pref
from core.flow_ui import (
    FLOW_SETTING_CHOICES,
    FLOW_SETTINGS_FORM,
    flow_setting,
    flow_setting_display,
    format_reset,
    load_profile_credits,
)

CALLBACK_PREFIX = "flowcfg:"
_FORM = {key: (label, help_text, labels) for key, label, help_text, labels in FLOW_SETTINGS_FORM}


def _available_profiles() -> list[str]:
    root = STORAGE_DIR / "flow_profiles"
    if not root.is_dir():
        return []
    return sorted(
        child.name.lower()
        for child in root.iterdir()
        if child.is_dir() and (child / "auth.json").is_file()
    )


def _choices(key: str) -> list[str]:
    if key == "FLOW_PROFILES":
        return _available_profiles()
    return FLOW_SETTING_CHOICES.get(key, [])


def menu_text() -> str:
    lines = ["🎬 <b>Cấu hình Google Flow</b>", "<i>Áp dụng cho video tiếp theo, không cần khởi động lại.</i>", ""]
    for key, label, _help, _labels in FLOW_SETTINGS_FORM:
        value = flow_setting(key)
        lines.append(f"• {html.escape(label)}: <b>{html.escape(flow_setting_display(key, value))}</b>")
    credits = load_profile_credits()
    if credits:
        lines += ["", "💳 <b>Credit đọc lần gần nhất</b> (cập nhật mỗi lần tạo cảnh):"]
        for name in sorted(credits):
            info = credits[name]
            when = time.strftime("%d/%m %H:%M", time.localtime(int(info.get("checked_at", 0))))
            reset = format_reset(info)
            lines.append(
                f"• Profile {html.escape(name)}: <b>{int(info.get('credits', 0))}</b> credit ({when})"
                + (f" · {html.escape(reset)}" if reset else "")
            )
    lines += ["", "Bấm vào mục muốn đổi 👇"]
    return "\n".join(lines)


def menu_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, label, _help, _labels in FLOW_SETTINGS_FORM:
        value = flow_setting_display(key, flow_setting(key))
        short_value = value if len(value) <= 22 else value[:21] + "…"
        rows.append([InlineKeyboardButton(f"✏️ {label}: {short_value}", callback_data=f"{CALLBACK_PREFIX}edit:{key}")])
    rows.append([InlineKeyboardButton("✖️ Đóng", callback_data=f"{CALLBACK_PREFIX}close")])
    return InlineKeyboardMarkup(rows)


def edit_text(key: str) -> str:
    label, help_text, _labels = _FORM[key]
    current = flow_setting(key)
    lines = [
        f"🎬 <b>{html.escape(label)}</b>",
        f"Hiện tại: <b>{html.escape(flow_setting_display(key, current))}</b>",
    ]
    if help_text:
        lines.append(f"<i>{html.escape(help_text)}</i>")
    if key == "FLOW_PROFILES":
        lines.append("Chọn profile dùng TRƯỚC; các profile còn lại xếp sau làm dự phòng khi hết credit.")
    return "\n".join(lines)


def edit_keyboard(key: str) -> InlineKeyboardMarkup:
    current = flow_setting(key)
    rows: list[list[InlineKeyboardButton]] = []
    if key == "FLOW_PROFILES":
        first = current.split(",")[0].strip().lower() if current else ""
        for name in _available_profiles():
            mark = "✅ " if name == first else ""
            rows.append([InlineKeyboardButton(f"{mark}Dùng profile {name} trước", callback_data=f"{CALLBACK_PREFIX}prof:{name}")])
    else:
        buttons = [
            InlineKeyboardButton(
                ("✅ " if choice == current else "") + flow_setting_display(key, choice),
                callback_data=f"{CALLBACK_PREFIX}set:{key}:{index}",
            )
            for index, choice in enumerate(_choices(key))
        ]
        per_row = 1 if any(len(b.text) > 18 for b in buttons) else 2
        rows = [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]
    rows.append([InlineKeyboardButton("⬅️ Quay lại", callback_data=f"{CALLBACK_PREFIX}menu")])
    return InlineKeyboardMarkup(rows)


async def send_menu(message: Any) -> None:
    await message.reply_text(menu_text(), parse_mode=ParseMode.HTML, reply_markup=menu_keyboard())


async def _show(query: Any, text: str, keyboard: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except BadRequest as exc:  # "message is not modified" when the same option is tapped again
        if "not modified" not in str(exc).lower():
            raise


async def handle_callback(query: Any) -> None:
    data = (query.data or "")[len(CALLBACK_PREFIX):]
    action, _, rest = data.partition(":")

    if action == "close":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        return
    if action == "menu":
        await query.answer()
        await _show(query, menu_text(), menu_keyboard())
        return
    if action == "edit" and rest in _FORM:
        await query.answer()
        await _show(query, edit_text(rest), edit_keyboard(rest))
        return
    if action == "set":
        key, _, index_text = rest.rpartition(":")
        choices = _choices(key)
        if key not in _FORM or not index_text.isdigit() or int(index_text) >= len(choices):
            await query.answer("Lựa chọn không hợp lệ.", show_alert=True)
            return
        value = choices[int(index_text)]
        set_runtime_pref(key, value)
        await query.answer(f"Đã lưu: {_FORM[key][0]} = {flow_setting_display(key, value)}")
        await _show(query, menu_text(), menu_keyboard())
        return
    if action == "prof":
        profiles = _available_profiles()
        if rest not in profiles:
            await query.answer("Không tìm thấy profile này.", show_alert=True)
            return
        order = [rest] + [name for name in profiles if name != rest]
        set_runtime_pref("FLOW_PROFILES", ",".join(order))
        await query.answer(f"Đã lưu thứ tự profile: {','.join(order)}")
        await _show(query, menu_text(), menu_keyboard())
        return
    await query.answer()
