"""Telegram /account and /server menus.

/account — list Google Flow profiles with the signed-in Google account, check whether each one
           can still open its Flow project, and open a login window for the ones that cannot.
/server  — show the dashboard / listener / worker processes, stop background workers, restart
           the whole server.

Callback data (prefix "sys:"):
  acc | chk:all | chk:<profile> | login:<profile>
  srv | stop | stop! | rst | rst! | run | close
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import subprocess
import time
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

from core import flow_accounts, server_control
from core.config import ROOT_DIR, STORAGE_DIR
from core.flow_ui import format_reset, load_profile_credits

LOGGER = logging.getLogger("short_video_distributor")

CALLBACK_PREFIX = "sys:"
ACCOUNT_COMMANDS = ["account", "accounts", "tai_khoan"]
SERVER_COMMANDS = ["server", "he_thong"]
CONTROL_STATE_FILE = STORAGE_DIR / "tele_control_state.json"

_background_tasks: set[asyncio.Task] = set()


def _when(epoch: int | float | None) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(int(epoch))) if epoch else ""


async def _edit(query: Any, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True
        )
    except BadRequest as exc:  # "message is not modified" when refreshed with no change
        if "not modified" not in str(exc).lower():
            raise


def claim_update(update_id: int | None) -> bool:
    """False when this Telegram update was already handled (re-delivered after a restart), so a
    confirmed stop/restart is never executed twice."""
    if update_id is None:
        return True
    try:
        state = json.loads(CONTROL_STATE_FILE.read_text(encoding="utf-8"))
        last = int(state.get("last_update_id", 0)) if isinstance(state, dict) else 0
    except (OSError, ValueError):
        last = 0
    if update_id <= last:
        return False
    try:
        CONTROL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONTROL_STATE_FILE.write_text(json.dumps({"last_update_id": update_id}), encoding="utf-8")
    except OSError:
        pass
    return True


# ---------------------------------------------------------------- accounts

def accounts_text() -> str:
    accounts = flow_accounts.list_accounts()
    if not accounts:
        return (
            "👤 <b>Tài khoản Google Flow</b>\n\nChưa có profile nào có auth.json.\n"
            "Chạy <code>python scripts\\save_flow_auth.py --profile a</code> trên máy chủ để thêm."
        )
    statuses = flow_accounts.load_status()
    credits = load_profile_credits()
    order = " → ".join(account.profile for account in accounts)
    lines = [f"👤 <b>Tài khoản Google Flow</b> ({len(accounts)} profile)", f"Thứ tự dùng: <b>{html.escape(order)}</b>", ""]
    for index, account in enumerate(accounts, start=1):
        status = statuses.get(account.profile) or {}
        email = account.email or str(status.get("email") or "")
        who = html.escape(account.name) if account.name else ""
        who = f"{who} &lt;{html.escape(email)}&gt;" if email and who else (html.escape(email) or who or "(chưa rõ tên tài khoản)")
        marker = " ▶️" if index == 1 else ""
        lines.append(f"<b>{index}. {html.escape(account.profile)}</b>{marker} — {who}")
        parts: list[str] = []
        if status:
            parts.append(flow_accounts.STATUS_LABELS.get(str(status.get("status")), str(status.get("status"))))
            if status.get("detail") and status.get("status") != flow_accounts.STATUS_OK:
                parts.append(html.escape(str(status["detail"]))[:160])
            parts.append(f"kiểm tra {_when(status.get('checked_at'))}")
        else:
            parts.append("❔ Chưa kiểm tra")
        credit = credits.get(account.profile)
        if credit:
            parts.append(f"{int(credit.get('credits', 0))} credit ({_when(credit.get('checked_at'))})")
            reset = format_reset(credit)
            if reset:
                parts.append(html.escape(reset))
        if not account.has_session_cookie:
            parts.append("⚠️ auth.json không có cookie đăng nhập Google")
        elif account.session_expires_at and account.session_expires_at < time.time():
            parts.append("⚠️ cookie đã hết hạn")
        lines.append("    " + " · ".join(parts))
    if flow_accounts.is_checking():
        lines += ["", "⏳ Đang kiểm tra…"]
    return "\n".join(lines)


def accounts_keyboard(failed: list[str] | None = None) -> InlineKeyboardMarkup:
    names = [account.profile for account in flow_accounts.list_accounts()]
    statuses = flow_accounts.load_status()
    if failed is None:
        failed = [
            name for name in names
            if (statuses.get(name) or {}).get("status") in {flow_accounts.STATUS_LOGIN, flow_accounts.STATUS_NO_PROJECT}
        ]
    rows = [[InlineKeyboardButton("🔍 Kiểm tra tất cả", callback_data=f"{CALLBACK_PREFIX}chk:all")]]
    check_buttons = [InlineKeyboardButton(f"🔍 {name}", callback_data=f"{CALLBACK_PREFIX}chk:{name}") for name in names]
    rows += [check_buttons[i : i + 4] for i in range(0, len(check_buttons), 4)]
    for name in failed:
        rows.append([InlineKeyboardButton(f"🔑 Đăng nhập lại profile {name}", callback_data=f"{CALLBACK_PREFIX}login:{name}")])
    rows.append(
        [
            InlineKeyboardButton("🔄 Làm mới", callback_data=f"{CALLBACK_PREFIX}acc"),
            InlineKeyboardButton("✖️ Đóng", callback_data=f"{CALLBACK_PREFIX}close"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def check_summary(results: list[flow_accounts.CheckResult]) -> str:
    if not results:
        return "Không có profile nào để kiểm tra."
    bad = [r for r in results if not r.ok]
    lines = ["🔍 <b>Kết quả kiểm tra tài khoản Flow</b>", ""]
    for result in results:
        line = f"• <b>{html.escape(result.profile)}</b>: {flow_accounts.STATUS_LABELS.get(result.status, result.status)}"
        if result.ok and result.credits is not None:
            line += f" · <b>{result.credits}</b> credit"
            reset = format_reset({"reset_at": result.reset_at, "reset_text": result.reset_text})
            if reset:
                line += f" · {html.escape(reset)}"
        if result.detail and not result.ok:
            line += f"\n    <i>{html.escape(result.detail)[:200]}</i>"
        lines.append(line)
    if bad:
        names = ", ".join(r.profile for r in bad)
        lines += [
            "",
            f"⛔ <b>Cần đăng nhập thủ công:</b> {html.escape(names)}",
            "Bấm nút 🔑 bên dưới → Chrome mở trên <b>máy chủ</b> → đăng nhập Google và mở project Flow; "
            "phiên được tự lưu khi vào được project. Sau đó bấm 🔍 để kiểm tra lại.",
        ]
    else:
        lines += ["", "✅ Tất cả profile đều vào được project."]
    return "\n".join(lines)


async def _run_check(bot: Bot, chat_id: int | str, message_id: int, names: list[str] | None) -> None:
    async def progress(profile: Any, index: int, total: int) -> None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"🔍 Đang kiểm tra profile <b>{html.escape(profile.name)}</b> ({index}/{total})…",
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            pass

    try:
        results = await flow_accounts.check_profiles(names, progress=progress)
        text = check_summary(results)
        failed = [r.profile for r in results if r.status in {flow_accounts.STATUS_LOGIN, flow_accounts.STATUS_NO_PROJECT}]
        keyboard = accounts_keyboard(failed)
    except Exception as exc:
        LOGGER.exception("Kiểm tra tài khoản Flow thất bại.")
        text = f"⚠️ Kiểm tra tài khoản Flow lỗi: {html.escape(str(exc))[:300]}"
        keyboard = accounts_keyboard()
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    except BadRequest:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


def _launch_login(profile: str) -> None:
    log_file = STORAGE_DIR / "dashboard.log"
    with log_file.open("ab", buffering=0) as out:
        subprocess.Popen(
            [
                server_control.python_executable(),
                str(ROOT_DIR / "scripts" / "save_flow_auth.py"),
                "--auto-save",
                "--profile",
                profile,
            ],
            cwd=ROOT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
        )


# ---------------------------------------------------------------- server

def _queue_count() -> int:
    try:
        from core.flow_runner import read_approved_queue

        return len(read_approved_queue())
    except Exception:
        return 0


def server_text() -> tuple[str, bool, bool]:
    """(text, has workers, can start queue)."""
    me = os.getpid()
    try:
        procs = server_control.list_processes()
        items = server_control.collapse_launchers(server_control.project_processes(procs, me))
    except Exception as exc:
        return f"🖥 <b>Server</b>\n⚠️ Không đọc được danh sách tiến trình: {html.escape(str(exc))[:200]}", False, False
    own_chain = {me, *server_control.ancestors(procs, me)}
    lines = ["🖥 <b>Trạng thái server</b>", ""]
    for item in sorted(items, key=lambda i: list(server_control.ROLE_LABELS).index(i.role)):
        started = server_control.format_started(item.proc.created)
        line = f"• {html.escape(item.label)} — PID {item.proc.pid}"
        if started:
            line += f" (từ {started})"
        if item.proc.pid in own_chain and item.role != server_control.ROLE_DASHBOARD:
            line += " ← bot này"
        lines.append(line)
    if not items:
        lines.append("(không thấy tiến trình nào của dự án)")
    workers = [i for i in items if i.role == server_control.ROLE_WORKER and i.proc.pid not in own_chain]
    busy = server_control.worker_lock_busy()
    queue = _queue_count()
    lines += [
        "",
        f"⚙️ Worker ngầm: <b>{len(workers)}</b>" + (" · đang render video" if busy else ""),
        f"📋 Hàng đợi đã duyệt: <b>{queue}</b> kịch bản",
    ]
    return "\n".join(lines), bool(workers), queue > 0 and not busy


def server_keyboard(has_workers: bool, can_run: bool) -> InlineKeyboardMarkup:
    first_row = []
    if has_workers:
        first_row.append(InlineKeyboardButton("⏹ Dừng worker ngầm", callback_data=f"{CALLBACK_PREFIX}stop"))
    if can_run:
        first_row.append(InlineKeyboardButton("▶️ Chạy hàng đợi", callback_data=f"{CALLBACK_PREFIX}run"))
    rows = [first_row] if first_row else []
    rows.append([InlineKeyboardButton("🔄 Restart server", callback_data=f"{CALLBACK_PREFIX}rst")])
    rows.append(
        [
            InlineKeyboardButton("🔄 Làm mới", callback_data=f"{CALLBACK_PREFIX}srv"),
            InlineKeyboardButton("✖️ Đóng", callback_data=f"{CALLBACK_PREFIX}close"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard(action: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=f"{CALLBACK_PREFIX}{action}!"),
                InlineKeyboardButton("⬅️ Thôi", callback_data=f"{CALLBACK_PREFIX}srv"),
            ]
        ]
    )


async def announce_restart(bot: Bot) -> None:
    """Called once the listener is up: tell the chat that a Telegram-requested restart finished."""
    marker = server_control.pop_restart_marker()
    if not marker or not marker.get("chat_id"):
        return
    took = int(time.time()) - int(marker.get("requested_at") or time.time())
    text = f"✅ <b>Server đã khởi động lại</b> (sau {took} giây"
    if marker.get("stopped") is not None:
        text += f", dừng {int(marker['stopped'])} tiến trình cũ"
    text += ").\nDashboard/bot giờ chạy ngầm, log ở <code>storage/dashboard.log</code>."
    try:
        await bot.send_message(chat_id=marker["chat_id"], text=text, parse_mode=ParseMode.HTML)
    except Exception:
        LOGGER.exception("Không gửi được thông báo restart xong.")


# ---------------------------------------------------------------- entry points

async def send_accounts(message: Any) -> None:
    text = await asyncio.to_thread(accounts_text)
    await message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=accounts_keyboard(), disable_web_page_preview=True
    )


async def send_server(message: Any) -> None:
    text, has_workers, can_run = await asyncio.to_thread(server_text)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=server_keyboard(has_workers, can_run))


async def _show_server(query: Any, note: str = "") -> None:
    text, has_workers, can_run = await asyncio.to_thread(server_text)
    if note:
        text = f"{note}\n\n{text}"
    await _edit(query, text, server_keyboard(has_workers, can_run))


async def handle_callback(query: Any, update_id: int | None = None) -> None:
    data = (query.data or "")[len(CALLBACK_PREFIX):]
    action, _, arg = data.partition(":")

    if action == "close":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if action == "acc":
        await query.answer()
        await _edit(query, await asyncio.to_thread(accounts_text), accounts_keyboard())
        return

    if action == "chk":
        if flow_accounts.is_checking():
            await query.answer("Đang có lượt kiểm tra khác, chờ xong đã.", show_alert=True)
            return
        names = None if arg == "all" else [arg]
        await query.answer("Đang kiểm tra…")
        label = "tất cả profile" if names is None else f"profile {arg}"
        busy_note = (
            "\n<i>Worker đang render — profile đang dùng sẽ được kiểm tra bằng auth.json.</i>"
            if server_control.worker_lock_busy()
            else ""
        )
        status_message = await query.message.reply_text(
            f"🔍 Đang mở Flow để kiểm tra {html.escape(label)}… (mỗi profile ~15–60 giây){busy_note}",
            parse_mode=ParseMode.HTML,
        )
        task = asyncio.create_task(
            _run_check(query.get_bot(), status_message.chat_id, status_message.message_id, names)
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return

    if action == "login":
        names = [account.profile for account in flow_accounts.list_accounts()]
        if arg not in names:
            await query.answer("Không tìm thấy profile này.", show_alert=True)
            return
        try:
            await asyncio.to_thread(_launch_login, arg)
        except Exception as exc:
            LOGGER.exception("Không mở được save_flow_auth.py cho profile %s.", arg)
            await query.answer(f"Không mở được trình duyệt: {exc}", show_alert=True)
            return
        await query.answer(
            f"🔑 Đã mở Chrome profile '{arg}' trên máy chủ. Đăng nhập Google và mở project Flow — "
            "phiên tự lưu khi vào được project (tối đa 10 phút).",
            show_alert=True,
        )
        return

    if action == "srv":
        await query.answer()
        await _show_server(query)
        return

    if action == "run":
        from core.tele.approval_bot import _auto_start_video_worker_if_needed

        await asyncio.to_thread(_auto_start_video_worker_if_needed, False)
        await query.answer("▶️ Đã khởi động worker tạo video cho hàng đợi.")
        await asyncio.sleep(2)
        await _show_server(query)
        return

    if action == "stop":
        await query.answer()
        await _edit(
            query,
            "⏹ <b>Dừng tất cả worker chạy ngầm?</b>\n"
            "Video đang render sẽ bị hủy (Chrome của worker cũng đóng). Kịch bản vẫn giữ trong hàng đợi; "
            "dashboard và bot Telegram vẫn chạy.",
            _confirm_keyboard("stop", "✅ Dừng ngay"),
        )
        return

    if action == "stop!":
        if not claim_update(update_id):
            await query.answer()
            return
        await query.answer("Đang dừng…")
        try:
            stopped = await asyncio.to_thread(server_control.stop_workers)
        except Exception as exc:
            LOGGER.exception("Không dừng được worker từ Telegram.")
            await _show_server(query, f"⚠️ Không dừng được worker: {html.escape(str(exc))[:200]}")
            return
        LOGGER.info("⏹ Telegram: đã dừng %d worker ngầm.", len(stopped))
        if stopped:
            names = ", ".join(f"--{item.mode} (PID {item.proc.pid})" for item in stopped)
            note = f"⏹ Đã dừng {len(stopped)} worker: {html.escape(names)}"
        else:
            note = "ℹ️ Không có worker nào đang chạy."
        await asyncio.sleep(1)
        await _show_server(query, note)
        return

    if action == "rst":
        await query.answer()
        await _edit(
            query,
            "🔄 <b>Khởi động lại server?</b>\n"
            "Dừng dashboard, bot Telegram và mọi worker (video đang render sẽ bị hủy), rồi chạy lại. "
            "Bot offline khoảng 10–30 giây và sẽ nhắn khi chạy lại xong.\n"
            "<i>Sau restart server chạy ngầm (không có cửa sổ terminal); log ở storage/dashboard.log.</i>",
            _confirm_keyboard("rst", "✅ Restart"),
        )
        return

    if action == "rst!":
        if not claim_update(update_id):
            await query.answer()
            return
        try:
            relaunch = await asyncio.to_thread(server_control.request_restart, query.message.chat_id)
        except Exception as exc:
            LOGGER.exception("Không khởi động được restart helper.")
            await query.answer(f"Không restart được: {exc}", show_alert=True)
            return
        LOGGER.info("🔄 Telegram yêu cầu restart server; sẽ chạy lại: %s", " ".join(relaunch[1:]))
        await query.answer("Đang khởi động lại…")
        await _edit(query, "🔄 Đang khởi động lại server… bot sẽ nhắn khi chạy lại xong.", None)
        return

    await query.answer()
