"""本机演示界面。只监听 127.0.0.1，不增加安装依赖。"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from configure import _read_env, apply_ui_config, detect_mailbox, sync_after_mailbox_change
from db_init import (
    complete_task,
    dashboard_snapshot,
    delete_mail,
    delete_mails_by_usefulness,
    delete_done_tasks,
    delete_mailbox,
    delete_task,
    undo_mail,
    undo_task,
    reopen_task,
    mail_file_for,
    bind_task_reply,
    get_task,
    init_db,
    list_chat,
    list_mailboxes,
    export_period_sheet,
    list_recent_activity,
    mail_detail,
    mailbox_for_mail,
    mark_draft_sent,
    now_str,
    save_reply_body,
    reschedule_task,
    retry_failed_task,
    search_mails,
    set_mail_usefulness,
    upload_file_for,
)
from mail_client import send_mail

logger = logging.getLogger("youzhihui.ui")
UPLOAD_BODY_LIMIT = 21 * 1024 * 1024


def _reply_attachments(payload: dict) -> list[tuple[str, str, bytes]]:
    """回复里拖进来的附件。合计不超过 15MB，确认发送时才随信出去。"""
    raw = payload.get("files") or []
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("附件格式不对")
    if len(raw) > 8:
        raise ValueError("一次最多 8 个附件")
    found: list[tuple[str, str, bytes]] = []
    total = 0
    limit = 15 * 1024 * 1024
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("附件格式不对")
        name = Path(str(item.get("name") or "附件")).name
        name = re.sub(r'[\\/:*?"<>|\r\n\x00]+', "_", name).strip(" .")[:80] or "附件"
        kind = str(item.get("type") or "application/octet-stream").split(";", 1)[0].strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.+-]{0,40}/[a-z0-9][a-z0-9.+-]{0,40}", kind):
            kind = "application/octet-stream"
        try:
            data = base64.b64decode(str(item.get("data") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("附件没有读出来，请重新拖入") from exc
        if not data:
            raise ValueError("有一个附件是空的")
        if len(data) > limit or total + len(data) > limit:
            raise ValueError("附件合计不能超过 15MB")
        total += len(data)
        found.append((name, kind, data))
    return found


def _startup_script() -> Path:
    root = os.environ.get("APPDATA", "").strip()
    if not root:
        return Path()
    return Path(root) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "邮智汇.vbs"


def autostart_enabled() -> bool:
    path = _startup_script()
    return bool(path.name) and path.is_file()


def set_autostart(enabled: bool) -> None:
    """在当前用户的开机启动目录里放或去掉一个隐藏启动脚本。"""
    path = _startup_script()
    if not path.name:
        raise ValueError("没有找到开机启动目录")
    if not enabled:
        if path.is_file():
            path.unlink()
        return
    base = Path(__file__).resolve().parent
    hide = base / "tools" / "hide_console.vbs"
    bat = base / "启动.bat"
    if not hide.is_file() or not bat.is_file():
        raise ValueError("启动文件不完整，没能设置开机运行")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        'Set sh = CreateObject("Wscript.Shell")\r\n'
        f'sh.Run "wscript //nologo ""{hide}"" ""{bat}""", 0, False\r\n'
    )
    path.write_text(body, encoding="gbk")

from operator_rule import (
    AdviceUnavailable,
    ask_for_advice,
    clear_conversation,
    confirm_pending,
    current_rule,
    is_default_rule,
    pending_is_fetch,
    pending_summary,
    restore_default,
    save_fetch_limit,
)

BASE_DIR = Path(__file__).resolve().parent


_sending: set = set()
_sending_lock = threading.Lock()


def _task_id(value) -> int:
    """页面传来的编号。布尔值、小数、超长数字都当成无效。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit() and len(value.strip()) <= 12:
        number = int(value.strip())
    else:
        return 0
    return number if 0 < number < 2 ** 62 else 0


class _Bridge:
    on_saved = None
    is_running = None
    on_stop = None


def _month_day(day: str) -> str:
    return f"{int(day[5:7])}月{int(day[8:10])}日"


def _day_label(day: str, today: str) -> str:
    if day == today:
        return "今天"
    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    if day == yesterday:
        return "昨天"
    return _month_day(day)


def _records() -> list[dict]:
    """有邮件或任务的日子都留下。今天即使没有内容也留着。"""
    today = now_str()[:10]
    activity = list_recent_activity()
    buckets: dict[str, dict] = {}

    def ensure(day: str) -> dict | None:
        if len(day) < 10:
            return None
        day = day[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return None
        if day not in buckets:
            buckets[day] = {
                "date": day,
                "label": _day_label(day, today),
                "title": _month_day(day),
                "mails": [],
                "tasks": [],
            }
        return buckets[day]

    for mail in activity["mails"]:
        bucket = ensure(str(mail.get("received_at") or mail.get("created_at") or ""))
        if bucket is not None:
            bucket["mails"].append(mail)
    for task in activity["tasks"]:
        # 按该做的那天归。今天才点完成的，仍留在原来那天，不挤进今天。
        day = str(task.get("execute_at") or task.get("created_at") or "")
        bucket = ensure(day)
        if bucket is not None:
            bucket["tasks"].append(task)
    ensure(today)
    rows = [buckets[day] for day in sorted(buckets, reverse=True)]
    for row in rows:
        row["mail_count"] = len(row["mails"])
        row["task_count"] = len(row["tasks"])
        row["count"] = row["mail_count"] + row["task_count"]
    return rows


def _scan_note() -> str:
    from scheduler_task import scan_note

    return scan_note()


def build_state(include_records: bool = True) -> dict:
    init_db()
    current = _read_env()
    try:
        hour = int(current.get("DAILY_REPORT_HOUR", "21") or "21")
        minute = int(current.get("DAILY_REPORT_MINUTE", "0") or "0")
    except ValueError:
        hour, minute = 21, 0
    state = dashboard_snapshot(mail_limit=300)
    rule = current_rule()
    boxes = list_mailboxes()
    state.update(
        {
            "configured": bool(boxes),
            "running": bool(_Bridge.is_running and _Bridge.is_running()),
            "username": boxes[0]["address"] if boxes else current.get("MAIL_USERNAME", ""),
            "mailbox_label": boxes[0]["label"] if boxes else current.get("MAIL_LABEL", ""),
            "mail_host": boxes[0]["imap_host"] if boxes else current.get("MAIL_IMAP_HOST", ""),
            "mailboxes": boxes,
            "password_set": bool(boxes or current.get("MAIL_PASSWORD", "").strip()),
            "dingtalk_set": bool(current.get("DINGTALK_WEBHOOK", "").strip()),
            "poll_minutes": current.get("MAIL_POLL_MINUTES", "5") or "5",
            "daily": f"{hour:02d}:{minute:02d}",
            "rule_text": rule["summary"],
            "rule_custom": not is_default_rule(rule),
            "fetch_limit": int(rule.get("fetch_limit") or 0) if 5 <= int(rule.get("fetch_limit") or 0) <= 40 else 20,
            "scan_note": _scan_note(),
            "messages": list_chat(30),
            "now": now_str(),
            "proposal": pending_summary(),
            "proposal_fetch": pending_is_fetch(),
            "autostart": autostart_enabled(),
        }
    )
    if include_records:
        state["records"] = _records()
    return state


class DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _local_hosts(self) -> set:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").strip().lower() in self._local_hosts()

    def _origin_ok(self) -> bool:
        """别的网页可以往本机端口发请求。只认本页自己发来的 JSON。"""
        if not self._host_ok():
            return False
        kind = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if kind != "application/json":
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin not in {"http://" + item for item in self._local_hosts()}:
            return False
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        return site in {"", "same-origin", "none"}

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if not self._host_ok():
            self._json(403, {"error": "只能从本机页面访问"})
            return
        if path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            query = parse_qs(urlparse(self.path).query)
            include_records = (query.get("records") or ["1"])[0] != "0"
            self._json(200, build_state(include_records=include_records))
            return
        if path == "/api/backup":
            query = parse_qs(urlparse(self.path).query)
            start = (query.get("start") or [""])[0]
            end = (query.get("end") or [""])[0]
            self._send_backup(start, end)
            return
        if path == "/api/scan/file":
            query = parse_qs(urlparse(self.path).query)
            self._send_upload((query.get("id") or [""])[0])
            return
        if path == "/api/mail/file":
            query = parse_qs(urlparse(self.path).query)
            self._send_mail_file((query.get("id") or [""])[0])
            return
        self._json(404, {"error": "没有这个页面"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/task/send":
            limit = 24 * 1024 * 1024
        elif path == "/api/scan/upload":
            limit = UPLOAD_BODY_LIMIT
        else:
            limit = 100000
        raw = self._read_raw(limit)
        if not self._origin_ok():
            self._json(403, {"error": "只能从本机页面操作"})
            return
        payload, error = self._read_body(raw)
        if error:
            self._json(400, {"error": error})
            return
        if "id" in payload:
            payload["id"] = _task_id(payload["id"])
        if path == "/api/config":
            self._save_config(payload)
            return
        if path == "/api/mailbox/delete":
            if payload.get("confirm") is not True:
                self._json(400, {"error": "请先确认再移出"})
                return
            try:
                ok = delete_mailbox(int(payload.get("id")))
            except (TypeError, ValueError):
                self._json(400, {"error": "没有这个邮箱"})
                return
            if not ok:
                self._json(404, {"error": "没有这个邮箱"})
                return
            sync_after_mailbox_change()
            if _Bridge.on_saved:
                _Bridge.on_saved()
            self._json(200, build_state())
            return
        if path == "/api/detect":
            found = detect_mailbox(str(payload.get("username", "")))
            self._json(
                200,
                {
                    "ok": bool(found["imap_host"]),
                    "label": found["label"],
                    "host": found["imap_host"],
                    "imap_host": found["imap_host"],
                    "imap_port": found["imap_port"],
                    "smtp_host": found["smtp_host"],
                    "smtp_port": found["smtp_port"],
                    "smtp_mode": found["smtp_mode"],
                },
            )
            return
        if path == "/api/chat":
            try:
                ask_for_advice(str(payload.get("message", "")))
            except AdviceUnavailable as exc:
                state = build_state()
                state["chat_error"] = str(exc)
                self._json(200, state)
                return
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, build_state())
            return
        if path == "/api/rule/confirm":
            try:
                confirm_pending(payload.get("confirm") is True)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, build_state())
            return
        if path == "/api/rule/reset":
            restore_default()
            self._json(200, build_state())
            return
        if path == "/api/chat/clear":
            if payload.get("confirm") is not True:
                self._json(400, {"error": "请先确认再清空对话"})
                return
            clear_conversation()
            self._json(200, build_state())
            return
        if path == "/api/rule/limit":
            try:
                save_fetch_limit(payload.get("fetch_limit"))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, build_state())
            return
        if path == "/api/mail/usefulness":
            try:
                ok = set_mail_usefulness(int(payload.get("id")), str(payload.get("usefulness", "")))
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc) or "分类无效"})
                return
            if not ok:
                self._json(404, {"error": "没有这封邮件"})
                return
            self._json(200, build_state())
            return
        if path == "/api/mail/reparse":
            from scheduler_task import retry_parse

            mail_id = _task_id(payload.get("id"))
            if not mail_id or not retry_parse(mail_id):
                self._json(404, {"error": "这封邮件现在不需要重新识别"})
                return
            self._json(200, build_state())
            return
        if path == "/api/mail/delete":
            if payload.get("confirm") is not True:
                self._json(400, {"error": "请先确认再删除"})
                return
            try:
                ok = delete_mail(int(payload.get("id")))
            except (TypeError, ValueError):
                self._json(400, {"error": "没有这封邮件"})
                return
            if not ok:
                self._json(404, {"error": "没有这封邮件"})
                return
            self._json(200, build_state())
            return
        if path == "/api/mail/delete-all":
            kind = str(payload.get("kind") or "all")
            expected = {"all": "删除全部", "useful": "删除有用", "ordinary": "删除普通", "spam": "删除垃圾"}
            if kind not in expected or payload.get("confirm") != expected[kind]:
                self._json(400, {"error": "请先确认再删除"})
                return
            try:
                delete_mails_by_usefulness(kind)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, build_state())
            return
        if path == "/api/scan/now":
            from scheduler_task import scan_now

            note = scan_now()
            state = build_state()
            state["note"] = note
            self._json(200, state)
            return
        if path == "/api/scan/upload":
            self._accept_upload(payload)
            return
        if path == "/api/task/retry":
            if payload.get("confirm") is not True:
                self._json(400, {"error": "请先确认再重试"})
                return
            try:
                ok = retry_failed_task(int(payload.get("id")))
            except (TypeError, ValueError):
                self._json(400, {"error": "没有这条任务"})
                return
            if not ok:
                self._json(404, {"error": "这条任务现在不能重试"})
                return
            self._json(200, build_state())
            return
        if path == "/api/task/done":
            try:
                ok = complete_task(int(payload.get("id")))
            except (TypeError, ValueError):
                self._json(400, {"error": "没有这条任务"})
                return
            if not ok:
                self._json(404, {"error": "没有这条任务"})
                return
            self._json(200, build_state())
            return
        if path == "/api/task/reopen":
            try:
                ok = reopen_task(int(payload.get("id")))
            except (TypeError, ValueError):
                self._json(400, {"error": "没有这条任务"})
                return
            if not ok:
                self._json(404, {"error": "这条任务现在不能恢复"})
                return
            self._json(200, build_state())
            return
        if path == "/api/task/undo":
            try:
                ok = undo_task(int(payload.get("id")))
            except (TypeError, ValueError):
                self._json(400, {"error": "没有这条任务"})
                return
            if not ok:
                self._json(404, {"error": "这条任务已经不能撤回"})
                return
            self._json(200, build_state())
            return
        if path == "/api/mail/undo":
            try:
                ok = undo_mail(int(payload.get("id")))
            except (TypeError, ValueError):
                self._json(400, {"error": "没有这封邮件"})
                return
            if not ok:
                self._json(404, {"error": "这封邮件已经不能撤回"})
                return
            self._json(200, build_state())
            return
        if path == "/api/task/reschedule":
            if payload.get("confirm") is not True:
                self._json(400, {"error": "请先确认再改时间"})
                return
            try:
                task_id = int(payload.get("id"))
            except (TypeError, ValueError):
                self._json(400, {"error": "没有这条任务"})
                return
            try:
                ok = reschedule_task(task_id, str(payload.get("execute_at") or ""))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            if not ok:
                self._json(404, {"error": "没有这条任务"})
                return
            self._json(200, build_state())
            return
        if path == "/api/autostart":
            try:
                set_autostart(payload.get("enabled") is True)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            except OSError:
                logger.exception("开机运行没有设置成功")
                self._json(500, {"error": "开机运行没有设置成功"})
                return
            self._json(200, build_state())
            return
        if path == "/api/mail/search":
            self._json(200, {"mails": search_mails(str(payload.get("q", "")))})
            return
        if path == "/api/mail/open":
            try:
                detail = mail_detail(int(payload.get("id")))
            except (TypeError, ValueError):
                self._json(400, {"error": "没有这封邮件"})
                return
            if not detail:
                self._json(404, {"error": "没有这封邮件"})
                return
            self._json(200, detail)
            return
        if path == "/api/task/send":
            self._send_draft(payload)
            return
        if path == "/api/task/clear_done":
            if payload.get("confirm") is not True:
                self._json(400, {"error": "请先确认再删除"})
                return
            removed = delete_done_tasks()
            state = build_state()
            state["note"] = f"已删除 {removed} 条已完成的任务"
            self._json(200, state)
            return
        if path == "/api/task/delete":
            if payload.get("confirm") is not True:
                self._json(400, {"error": "请先确认再删除"})
                return
            try:
                ok = delete_task(int(payload.get("id")))
            except (TypeError, ValueError):
                self._json(400, {"error": "没有这条任务"})
                return
            if not ok:
                self._json(404, {"error": "没有这条任务"})
                return
            self._json(200, build_state())
            return
        if path == "/api/shutdown":
            self._json(200, {"ok": True})
            if _Bridge.on_stop:
                threading.Thread(target=_Bridge.on_stop, name="youzhihui-stop", daemon=True).start()
            return
        self._json(404, {"error": "没有这个页面"})

    def _save_config(self, payload: dict) -> None:
        error = apply_ui_config(
            str(payload.get("username", "")),
            str(payload.get("password", "")),
            str(payload.get("webhook", "")),
            str(payload.get("imap_host", "")),
            str(payload.get("imap_port", "")),
            str(payload.get("smtp_host", "")),
            str(payload.get("smtp_port", "")),
            str(payload.get("smtp_mode", "")),
        )
        if error:
            self._json(400, {"error": error})
            return
        if _Bridge.on_saved:
            _Bridge.on_saved()
        from scheduler_task import first_scan_soon

        first_scan_soon()
        self._json(200, {"ok": True})

    def _read_raw(self, limit: int = 100000) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            self.close_connection = True
            return None
        return self.rfile.read(length) if length else b""

    def _accept_upload(self, payload: dict) -> None:
        from scheduler_task import MAX_UPLOAD_BYTES, ingest_upload, upload_kind

        name = str(payload.get("name") or "").strip()
        if not upload_kind(name):
            self._json(400, {"error": "只支持 jpg、png 和 pdf"})
            return
        try:
            data = base64.b64decode(str(payload.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            self._json(400, {"error": "文件没有读出来，请重新选择"})
            return
        if not data:
            self._json(400, {"error": "这个文件是空的"})
            return
        if len(data) > MAX_UPLOAD_BYTES:
            self._json(400, {"error": "文件超过 15MB"})
            return
        threading.Thread(
            target=ingest_upload,
            args=(name, data),
            name="youzhihui-upload",
            daemon=True,
        ).start()
        self._json(200, {"ok": True, "note": f"已收到 {name}，正在识别"})

    def _send_mail_file(self, raw_id: str) -> None:
        try:
            found = mail_file_for(int(raw_id))
        except (TypeError, ValueError):
            found = None
        if not found:
            self._json(404, {"error": "这个附件不在了"})
            return
        path, title, kind = found
        safe = {"image/jpeg", "image/png", "image/gif", "image/bmp", "image/webp", "application/pdf"}
        shown = kind if kind in safe else "application/octet-stream"
        mode = "inline" if kind in safe else "attachment"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", shown)
        self.send_header("Content-Disposition", f"{mode}; filename*=UTF-8''" + quote(title))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_upload(self, raw_id: str) -> None:
        try:
            found = upload_file_for(int(raw_id))
        except (TypeError, ValueError):
            found = None
        if not found:
            self._json(404, {"error": "原文件不在了"})
            return
        path, title = found
        data = path.read_bytes()
        kind = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".pdf": "application/pdf",
        }.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + quote(title))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self, raw: bytes | None) -> tuple[dict, str]:
        if not raw:
            return {}, "提交内容无效"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return {}, "提交内容无效"
        if not isinstance(payload, dict):
            return {}, "提交内容无效"
        return payload, ""

    def _send_backup(self, start: str, end: str) -> None:
        day = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
        if not day.fullmatch(start) or not day.fullmatch(end):
            self._json(400, {"error": "日期格式不对"})
            return
        try:
            data = export_period_sheet(start, end)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception:
            logger.exception("导出备份失败")
            self._json(500, {"error": "备份没有生成"})
            return
        filename = f"youzhihui-{start}-{end}.xls"
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.ms-excel; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_draft(self, payload: dict) -> None:
        if payload.get("confirm") is not True:
            self._json(400, {"error": "请先确认再发送"})
            return
        task_id = _task_id(payload.get("id"))
        if not task_id:
            self._json(400, {"error": "没有这条草稿"})
            return
        with _sending_lock:
            if task_id in _sending:
                self._json(409, {"error": "这封回复正在发送，请稍等"})
                return
            _sending.add(task_id)
        try:
            self._send_draft_once(task_id, payload)
        finally:
            with _sending_lock:
                _sending.discard(task_id)

    def _send_draft_once(self, task_id: int, payload: dict) -> None:
        task = bind_task_reply(task_id)
        if not task:
            self._json(404, {"error": "没有可回复的任务"})
            return
        to_addr = str(task.get("reply_to") or "").strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to_addr):
            self._json(400, {"error": "这封来信没有可回复的地址"})
            return
        try:
            attachments = _reply_attachments(payload)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        body = str(payload.get("body") or "").strip()
        if body:
            try:
                saved = save_reply_body(task_id, body)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            if not saved:
                self._json(404, {"error": "这条任务现在不能回复"})
                return
        else:
            body = str(task.get("report") or "").strip() if task.get("status") == "draft" else ""
        if not body and attachments:
            body = "见附件。"
        if not body:
            self._json(400, {"error": "请先写回复内容"})
            return
        try:
            account = None
            source = int(task.get("source_mail_id") or 0)
            if source:
                account = mailbox_for_mail(source)
            send_mail(to_addr, task.get("reply_subject") or "Re: 邮件", body, account, attachments)
        except Exception:
            logger.exception("草稿发送失败 task=%s", task_id)
            self._json(502, {"error": "没有发出去，请检查邮箱授权码和发信服务器"})
            return
        mark_draft_sent(task_id, f"已于 {now_str()} 发送至 {to_addr}")
        self._json(200, build_state())

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def start_demo_server(on_saved, is_running, on_stop=None) -> str:
    """启动演示页并返回本机地址。"""
    _Bridge.on_saved = on_saved
    _Bridge.is_running = is_running
    _Bridge.on_stop = on_stop
    last_error: OSError | None = None
    for port in range(8765, 8775):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), DemoHandler)
        except OSError as exc:
            last_error = exc
            continue
        thread = threading.Thread(target=server.serve_forever, name="youzhihui-demo", daemon=True)
        thread.start()
        return f"http://127.0.0.1:{port}/"
    raise RuntimeError(f"演示界面端口被占用: {last_error}")


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>邮智汇</title>
<style>
  :root {
    --paper: #f3efe6;
    --ink: #1c2824;
    --muted: #5e6b64;
    --line: #ddd4c6;
    --card: #fffdf8;
    --teal: #0e6b62;
    --teal-deep: #0a4f49;
    --amber: #9a5b16;
    --rose: #8d3d36;
    --shadow: 0 16px 40px rgba(48, 38, 22, 0.06);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    color: var(--ink);
    background:
      radial-gradient(900px 420px at 100% -8%, rgba(14, 107, 98, 0.08), transparent 55%),
      var(--paper);
    font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  .app { display: flex; min-height: 100vh; }
  .side {
    width: 240px; flex: none; position: sticky; top: 0; height: 100vh;
    display: flex; flex-direction: column; padding: 22px 14px 16px;
    background: var(--teal-deep); color: #f6f1e6; overflow: hidden;
  }
  .brand { display: flex; gap: 12px; align-items: center; padding: 0 8px 18px; }
  .seal {
    width: 42px; height: 42px; flex: none; display: grid; place-items: center;
    background: #f6f1e6; color: var(--teal-deep); font-size: 22px;
    border-radius: 8px 14px 8px 14px;
  }
  .eyebrow { margin: 0 0 2px; color: rgba(246, 241, 230, 0.72); font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; }
  h1 { margin: 0; font-size: 22px; font-weight: 680; letter-spacing: 0.06em; color: #f6f1e6; }
  .brand-line { margin: 0 8px 16px; color: rgba(246, 241, 230, 0.72); font-size: 12px; line-height: 1.45; }
  .lane-body { flex: 1; display: flex; flex-direction: column; }
  .nav { display: flex; flex-direction: column; gap: 4px; }
  button.navbtn, button.daybtn {
    width: 100%; margin: 0; text-align: left; background: transparent; color: #f6f1e6;
    border-radius: 12px; padding: 10px 12px; font-weight: 600;
  }
  button.navbtn:hover, button.daybtn:hover { background: rgba(246, 241, 230, 0.1); }
  button.navbtn.active, button.navbtn.active:hover { background: #f6f1e6; color: var(--teal-deep); }
  button:focus { outline: none; }
  button:focus-visible { outline: 2px solid rgba(14, 107, 98, 0.45); outline-offset: 2px; }
  .side button:focus-visible { outline-color: #f6f1e6; }
  .day-nav { margin-top: 16px; padding-top: 14px; border-top: 1px solid rgba(246, 241, 230, 0.16); overflow: auto; }
  .day-nav[hidden] { display: none; }
  .side-label { margin: 0 8px 8px; color: rgba(246, 241, 230, 0.7); font-size: 12px; }
  button.daybtn { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; font-weight: 560; }
  button.daybtn .day-sub { color: rgba(246, 241, 230, 0.66); font-size: 12px; font-weight: 500; }
  button.daybtn.active { background: #f6f1e6; color: var(--teal-deep); }
  button.daybtn.active .day-sub { color: var(--teal); }
  .side-foot { margin-top: auto; padding-top: 14px; border-top: 1px solid rgba(246, 241, 230, 0.16); }
  .side-note { margin: 10px 4px 0; color: rgba(246, 241, 230, 0.62); font-size: 12px; line-height: 1.5; }
  .badge {
    display: inline-block; padding: 6px 10px; border-radius: 999px;
    background: rgba(246, 241, 230, 0.12); color: #f6f1e6; font-size: 13px;
  }
  .badge.run { background: #e7f4f2; color: var(--teal-deep); }
  .badge.wait { background: #fff4e4; color: var(--amber); }
  main {
    flex: 1; min-width: 0; min-height: 100vh; padding: 28px 32px;
    display: flex; flex-direction: column;
  }
  .view { display: flex; flex-direction: column; gap: 16px; }
  .view[hidden] { display: none; }
  #view-board, #view-chat, #view-records { flex: 1; min-width: 0; }
  .page-head h2 { margin: 0 0 8px; font-size: 26px; letter-spacing: 0.02em; color: var(--ink); }
  .kicker { margin: 0 0 4px; color: var(--teal); font-size: 12px; font-weight: 700; letter-spacing: 0.12em; }
  .page-head p { margin: 0; color: var(--muted); font-size: 14px; line-height: 1.5; }
  .rulechip {
    display: inline-block; max-width: 100%; margin: 0; padding: 8px 12px;
    border-radius: 999px; background: #fff; border: 1px solid var(--line); color: var(--ink); line-height: 1.45;
  }
  .panel { background: var(--card); border: 1px solid var(--line); border-radius: 18px; box-shadow: var(--shadow); }
  .setup, .board, .chat, .record { padding: 18px 20px; }
  #view-chat .chat, #view-board .board, #view-records .record { flex: 0 0 auto; }
  .chat { display: flex; flex-direction: column; }
  h2 { margin: 0 0 12px; font-size: 16px; }
  .choices { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px; }
  .choice {
    display: flex; align-items: center; justify-content: center; gap: 6px;
    padding: 10px 6px; border-radius: 12px; border: 1px solid var(--line);
    background: #faf7f2; cursor: pointer;
  }
  .choice input { accent-color: var(--teal); }
  .choice:has(input:checked) { border-color: var(--teal); background: #eef8f6; color: var(--teal-deep); font-weight: 650; }
  label.field { display: block; margin: 10px 0 4px; color: var(--muted); font-size: 13px; }
  input[type="text"], input[type="password"] {
    width: 100%; padding: 11px 12px; border-radius: 12px; border: 1px solid var(--line);
    background: #fff; color: var(--ink); font: inherit;
  }
  input:focus { outline: 2px solid rgba(14, 107, 98, 0.25); border-color: var(--teal); }
  button {
    width: auto; margin: 0; padding: 11px 16px; border: 0; border-radius: 12px;
    background: var(--teal); color: white; font: inherit; font-weight: 650; cursor: pointer;
  }
  button:hover { background: var(--teal-deep); }
  button.quit {
    display: block; width: 100%; margin-top: 12px; background: transparent; color: #f6f1e6;
    border: 1px solid rgba(246, 241, 230, 0.35); font-weight: 600;
  }
  button.quit:hover { background: rgba(246, 241, 230, 0.12); color: #f6f1e6; }
  .setup button[type="submit"] { width: 100%; margin-top: 14px; }
  .hint, .msg { color: var(--muted); font-size: 13px; line-height: 1.55; }
  .msg { min-height: 1.2em; margin: 8px 0 0; }
  .msg.bad { color: var(--rose); }
  .msg.ok { color: var(--teal-deep); }
  .example { margin-top: 16px; padding-top: 14px; border-top: 1px dashed var(--line); }
  .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 14px; }
  .filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 0 0 10px; }
  button.chip {
    background: #f7f4ee; color: var(--ink); border: 1px solid var(--line);
    padding: 6px 10px; font-size: 13px; font-weight: 600;
  }
  button.chip:hover { background: #eef8f6; color: var(--teal-deep); }
  button.chip.active, button.chip.active:hover { background: var(--teal); color: white; border-color: var(--teal); }
  #wipeMails { margin-left: auto; }
  .headtools { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }
  .headtools button { padding: 8px 14px; font-size: 13px; }
  .headtools .meta { font-size: 12px; color: var(--muted); }
  #headMsg:empty { display: none; }
  .tag.upload { background: #eef3fb; color: #36507a; border-color: transparent; }
  a.textbtn {
    display: inline-block; margin: 6px 0; padding: 6px 12px; border-radius: 12px;
    border: 1px solid var(--line); color: var(--teal-deep); text-decoration: none; font-size: 13px; font-weight: 600;
  }
  .detect { margin: 8px 0 0; min-height: 1.2em; color: var(--teal-deep); font-size: 13px; }
  button.more { margin-top: 4px; background: transparent; color: var(--teal-deep); border: 1px solid var(--line); }
  button.more:hover { background: #eef8f6; }
  .lane-tools { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 0 0 10px; }
  .lane-tools input {
    flex: 1; min-width: 0; padding: 8px 10px; border-radius: 10px;
    border: 1px solid var(--line); background: #fff; color: var(--ink); font: inherit; font-size: 13px;
  }
  .lane-tools input[type="date"] { flex: none; width: auto; }
  .card.unsent { border-color: #e3b2ab; background: #fdf4f2; }
  .card.late, .card.overdue { border-color: var(--rose); background: #f8ecea; }
  .card.late strong, .card.overdue strong { color: var(--rose); }
  textarea.reply {
    width: 100%; min-height: 76px; margin-top: 8px; padding: 8px 10px;
    border-radius: 10px; border: 1px solid var(--line); background: #fff;
    color: var(--ink); font: inherit; font-size: 13px; resize: vertical;
  }
  textarea.reply.over { border-color: var(--teal); background: #eef8f6; }
  .exportbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 0 0 8px; }
  .exportbar[hidden] { display: none; }
  .exportbar input[type="date"] { padding: 6px 8px; border-radius: 10px; border: 1px solid var(--line); background: #fff; font: inherit; }
  .replybox .chips { margin: 8px 0 0; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .chip {
    display: flex; align-items: center; gap: 6px; max-width: 100%;
    padding: 4px 6px 4px 8px; border-radius: 10px; background: #fff; border: 1px solid var(--line); font-size: 12px;
  }
  .chip b { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 180px; }
  .chip img { width: 36px; height: 36px; object-fit: cover; border-radius: 6px; }
  .actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; align-items: center; }
  button.mini, button.textbtn, button.danger {
    padding: 4px 8px; font-size: 12px; font-weight: 600; border-radius: 999px;
  }
  button.mini { background: #fff; color: var(--ink); border: 1px solid var(--line); }
  button.mini:hover, button.textbtn:hover { background: #eef8f6; color: var(--teal-deep); }
  button.mini.on { background: var(--teal); color: white; border-color: var(--teal); }
  button.textbtn { background: transparent; color: var(--muted); border: 1px solid var(--line); }
  button.danger { background: #f8ecea; color: var(--rose); border: 1px solid #edd9d6; }
  button.danger:hover { background: #f3ddd9; color: var(--rose); }
  .wipebar {
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    margin: 0 0 10px; padding: 8px 10px; border-radius: 12px; background: #f8ecea; color: var(--rose); font-size: 13px;
  }
  .fail { margin: 6px 0 0; color: var(--rose); font-weight: 650; font-size: 13px; }
  .card p.fail { color: var(--rose); }
  .viewer {
    position: fixed; inset: 0; z-index: 30; display: flex; justify-content: center;
    align-items: flex-start; padding: 48px 16px; background: rgba(28, 40, 36, 0.38);
  }
  .viewer[hidden] { display: none; }
  .sheet {
    width: min(720px, 100%); max-height: calc(100vh - 96px); overflow: auto;
    padding: 18px 20px; border-radius: 18px; background: var(--card); border: 1px solid var(--line);
    box-shadow: var(--shadow);
  }
  .sheet .body { white-space: pre-wrap; word-break: break-word; font-size: 14px; line-height: 1.6; margin: 12px 0; }
  .files { display: flex; flex-wrap: wrap; gap: 10px; margin: 8px 0 4px; }
  .files a.file {
    display: block; max-width: 100%; border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
    background: #fff; color: var(--teal-deep); text-decoration: none; font-size: 13px;
  }
  .files a.file img { display: block; max-width: 100%; max-height: 360px; object-fit: contain; }
  .files a.file span { display: block; padding: 8px 12px; }
  .split .lane-body {
    max-height: calc(100vh - 140px); overflow-y: auto; overflow-x: hidden; overscroll-behavior: contain; padding-right: 6px;
  }
  @media (min-width: 861px) and (min-height: 560px) {
    body:has(#view-board:not([hidden])),
    body:has(#view-chat:not([hidden])) { overflow: hidden; }
    main:has(#view-board:not([hidden])),
    main:has(#view-chat:not([hidden])) { height: 100vh; min-height: 0; overflow: hidden; padding-top: 20px; padding-bottom: 16px; }
    #view-board, #view-chat { min-height: 0; gap: 10px; }
    #view-board .page-head { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; }
    #view-board .page-head .kicker, #view-board .page-head #boxScope { display: none; }
    #view-board .page-head h2 { font-size: 22px; margin: 0 6px 0 0; }
    #view-board .page-head .rulechip { flex: 1 1 320px; padding: 6px 12px; border-radius: 12px; font-size: 13px; }
    #view-board .headtools { margin-top: 0; }
    #view-board .page-head #scanNote, #view-board .page-head #headMsg { flex-basis: 100%; font-size: 13px; }
    #view-board .board { flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 14px 18px 8px; }
    #view-board .stats { margin-bottom: 10px; gap: 8px; }
    #view-board .stat { display: flex; align-items: baseline; gap: 8px; padding: 8px 14px; border-radius: 12px; }
    #view-board .stat b { font-size: 20px; }
    #view-board .stat span { margin-top: 0; }
    #view-board .stat em { display: none; }
    #view-board .lane-tools { margin-bottom: 8px; }
    #view-board .lane-tools input[type="date"] { padding: 6px 8px; }
    #view-board .filters { margin-bottom: 8px; }
    #view-board .split { flex: 1; min-height: 0; grid-template-rows: minmax(0, 1fr); }
    #view-board .lane { min-height: 0; overflow: hidden; }
    #view-board .split .lane-body { flex: 1 1 0; min-height: 0; max-height: none; }
    #view-chat .page-head { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; }
    #view-chat .page-head .kicker, #view-chat .page-head .intro { display: none; }
    #view-chat .page-head h2 { font-size: 22px; margin: 0 6px 0 0; }
    #view-chat .page-head .rulechip { flex: 1 1 320px; padding: 6px 12px; border-radius: 12px; font-size: 13px; }
    #view-chat .chat { flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 14px 18px 8px; }
    #view-chat .thread { flex: 1 1 auto; min-height: 0; max-height: none; }
    #view-chat #chatForm { flex: none; }
  }
  .dayhead {
    position: sticky; top: 0; z-index: 2; margin: 0 0 6px; padding: 6px 2px;
    background: var(--card); color: var(--muted); font-size: 12px; font-weight: 700; letter-spacing: 0.04em;
  }
  .card.mrow { padding: 8px 12px; margin-bottom: 6px; }
  .card.mrow.quiet:not(.open) { background: transparent; border-color: #f0ebe2; }
  .card.mrow.quiet:not(.open) strong { color: var(--muted); font-weight: 600; }
  .mhead { display: flex; gap: 10px; align-items: baseline; cursor: pointer; }
  .mhead strong { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }
  .card.mrow.open .mhead strong { white-space: normal; }
  .mtime { flex: none; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
  .mline { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: center; margin-top: 4px; }
  .mline .tags { margin-top: 0; }
  .mline .tag { font-size: 11px; padding: 1px 7px; }
  .mline button.mini, .mline button.textbtn { min-height: 26px; padding: 3px 10px; }
  .card.mrow:not(.open) { cursor: pointer; }
  .msender { color: var(--muted); font-size: 12px; max-width: 14em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mline a.linked { margin-top: 0; font-size: 12px; }
  .card p.one { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 4px; font-size: 12px; }
  .split > .lane { min-width: 0; }
  .lane-body .card { min-width: 0; overflow-wrap: anywhere; }
  .lane-head .doneclear { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: -4px 0 10px; }
  .lane-head .doneclear[hidden] { display: none; }
  .stat { padding: 16px 16px 14px; border-radius: 16px; background: #fff; border: 1px solid var(--line); }
  .stat.useful { background: #e7f4f2; border-color: #cfe6e2; }
  .stat.spam { background: #f8ecea; border-color: #edd9d6; }
  .stat b { display: block; font-size: 32px; line-height: 1; letter-spacing: -0.03em; }
  .stat span { display: block; margin-top: 8px; font-weight: 700; }
  .stat em { display: block; margin-top: 4px; font-style: normal; color: var(--muted); font-size: 12px; line-height: 1.45; }
  .meta { margin: 0; color: var(--muted); font-size: 13px; }
  .split { display: grid; grid-template-columns: 1.5fr 1fr; gap: 0; align-items: stretch; }
  .lane {
    display: flex; flex-direction: column; min-height: 0; padding: 4px 8px 0 0;
    background: transparent; border: 0; border-radius: 0;
  }
  .lane + .lane { padding: 4px 0 0 22px; border-left: 1px solid var(--line); }
  .lane-head { display: block; }
  .lane h2 { margin: 0 0 4px; }
  .lane-head .meta { margin: 0 0 12px; }
  .card {
    padding: 12px; margin-bottom: 8px; border-radius: 14px; background: #fbf9f4; border: 1px solid #efe8dc;
  }
  .card strong { display: block; }
  .card p { margin: 4px 0 0; color: var(--muted); font-size: 13px; }
  .lane .undo {
    margin: 0 0 8px; padding: 8px 10px; border-radius: 10px;
    background: var(--card); border: 1px solid var(--line); color: var(--ink); font-size: 13px;
  }
  .tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .tag { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #fff; border: 1px solid var(--line); }
  .tag.useful { background: #e7f4f2; color: var(--teal-deep); border-color: transparent; }
  .tag.spam { background: #f8ecea; color: var(--rose); border-color: transparent; }
  .tag.fail { background: #f4efe2; color: #8a5a12; border-color: transparent; }
  .tag.ordinary { background: #f3efe6; color: var(--muted); border-color: transparent; }
  .why { margin: 6px 0 0; color: var(--ink); font-size: 13px; }
  .todo { margin: 6px 0 0; color: var(--teal-deep); font-size: 13px; }
  .tgroup { margin: 0 0 14px; }
  .tgroup > h3, .tgroup > summary {
    position: sticky; top: 0; z-index: 2; margin: 0 0 8px; padding: 6px 0;
    background: var(--card); font-size: 13px; font-weight: 700; color: var(--muted); letter-spacing: 0.02em;
  }
  .tgroup > summary { cursor: pointer; list-style: revert; }
  .tgroup.now > h3, .tgroup.overdue > h3 { color: var(--rose); }
  .card.done { opacity: 0.72; padding: 8px 12px; }
  .card.done:not(.open) p { display: none; }
  a.linked {
    display: inline-block; margin-top: 6px; color: var(--teal-deep); font-size: 13px; font-weight: 650;
    cursor: pointer; text-decoration: none;
  }
  a.linked:hover { text-decoration: underline; }
  .card.flash { outline: 3px solid rgba(14, 107, 98, 0.45); outline-offset: 1px; }
  .empty {
    display: block; margin: 8px 0 0; padding: 0; border: 0; background: none; color: var(--muted);
  }
  .empty b { display: block; margin-bottom: 4px; color: var(--ink); font-size: 15px; }
  .empty p { margin: 0; max-width: 36em; line-height: 1.55; }
  .thread .empty { margin: 28px 0; text-align: center; }
  .thread .empty p { margin-left: auto; margin-right: auto; text-align: center; }
  pre {
    margin: 0; max-height: 360px; overflow: auto; padding: 12px; border-radius: 14px;
    background: #17211e; color: #e7f2ee; font-size: 12px; line-height: 1.55;
    font-family: Consolas, "Sarasa Mono SC", monospace;
  }
  .ruleline {
    margin: 0 0 12px; padding: 12px 14px; border-radius: 14px; background: #f7f4ee;
    border: 1px solid #efe8dc; line-height: 1.55;
  }
  .confirmbar {
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    margin: 0 0 12px; padding: 12px; border-radius: 14px;
    background: #eef8f6; border: 1px solid #cfe6e2; color: var(--ink);
  }
  .confirmbar[hidden] { display: none; }
  #clearChat[hidden], #clearChatConfirm[hidden], #resetRule[hidden], #resetRuleConfirm[hidden] { display: none; }
  .confirmbar p { flex: 1 1 100%; margin: 0; line-height: 1.5; font-size: 14px; }
  .limitbox {
    display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 12px;
  }
  .limitbox label, .limitbox span { color: var(--muted); font-size: 13px; }
  #fetchLimit {
    width: 76px; padding: 8px 10px; border-radius: 12px; border: 1px solid var(--line);
    background: #fff; color: var(--ink); font: inherit;
  }
  .limitbox .hint { flex-basis: 100%; margin: 0; }
  .chat { min-width: 0; }
  .thread {
    flex: 0 1 auto; min-height: 120px; max-height: min(520px, calc(100vh - 300px)); min-width: 0;
    overflow-x: hidden; overflow-y: auto;
    display: flex; flex-direction: column; justify-content: flex-start; gap: 8px;
    margin: 4px 0 12px; padding: 12px; border-radius: 14px;
    border: 1px solid var(--line); background: #fff;
  }
  .thread .empty { margin: auto; }
  .bubble {
    max-width: min(72%, 640px); padding: 10px 12px; border-radius: 14px; line-height: 1.5;
    white-space: pre-wrap; overflow-wrap: anywhere;
  }
  .bubble.user { align-self: flex-end; background: #e7f4f2; }
  .bubble.pending { color: var(--muted); }
  .bubble.assistant { align-self: flex-start; background: #fff; border: 1px solid #efe8dc; }
  .quick { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 8px; }
  .quick button { background: #fff; color: var(--ink); border: 1px solid var(--line); border-radius: 999px; padding: 6px 12px; font-size: 13px; }
  .quick button:hover { background: #eef8f6; color: var(--teal-deep); }
  .composer { position: relative; }
  textarea {
    width: 100%; min-height: 76px; resize: vertical; padding: 11px 12px; border-radius: 12px;
    border: 1px solid var(--line); background: #fff; color: var(--ink); font: inherit;
  }
  #chatInput { padding-right: 76px; }
  textarea:focus { outline: 2px solid rgba(14, 107, 98, 0.25); border-color: var(--teal); }
  button.sendbtn {
    position: absolute; right: 8px; bottom: 8px; height: 32px; padding: 0 12px;
    border-radius: 8px; font-size: 13px;
  }
  button.sendbtn:disabled { opacity: 0.55; }
  input.when {
    width: 190px; padding: 8px 10px; border-radius: 12px; border: 1px solid var(--line);
    background: #fff; color: var(--ink); font: inherit;
  }
  .servers { display: grid; grid-template-columns: 1fr 88px; gap: 8px; margin-top: 8px; }
  .servers label { color: var(--muted); font-size: 12px; }
  select, input.narrow {
    width: 100%; padding: 11px 12px; border-radius: 12px; border: 1px solid var(--line);
    background: #fff; color: var(--ink); font: inherit;
  }
  .mbox {
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    margin: 0 0 8px; padding: 10px 12px; border-radius: 14px;
    border: 1px solid var(--line); background: #fff;
  }
  .mbox b { font-weight: 650; }
  .mbox span { color: var(--muted); font-size: 13px; }
  #mailboxList:empty { display: none; }
  .bootline {
    display: flex; align-items: center; gap: 8px; margin: 14px 0 4px;
    color: var(--ink); font-size: 14px;
  }
  .bootline input { accent-color: var(--teal); width: auto; }
  .actions.confirming { flex-wrap: nowrap; }
  .actions .ask { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink); font-size: 13px; }
  .toolrow { display: flex; gap: 10px; align-items: center; margin: 0 0 8px; }
  button.ghost { background: transparent; color: var(--teal-deep); border: 1px solid var(--line); }
  button.ghost:hover { background: #eef8f6; }
  .quietline { margin: 0 0 12px; color: var(--muted); font-size: 13px; }
  .day-title { margin: 0; font-size: 18px; font-weight: 680; }
  .day-title + .meta { margin: 6px 0 0; }
  .record h3 { margin: 16px 0 8px; font-size: 14px; }
  @media (max-width: 860px) {
    .app { flex-direction: column; }
    .side { position: static; width: auto; height: auto; }
    .nav { flex-direction: row; overflow: auto; }
    button.navbtn { width: auto; white-space: nowrap; }
    .day-nav { display: flex; gap: 8px; align-items: center; overflow: auto; }
    .side-foot { margin-top: 12px; }
    #dayList { display: flex; gap: 8px; }
    button.daybtn { width: auto; white-space: nowrap; }
    .split, .stats { grid-template-columns: 1fr; }
    .lane { padding-right: 0; }
    .lane + .lane { padding-left: 0; margin-top: 18px; border-left: 0; border-top: 1px solid var(--line); padding-top: 18px; }
    main { min-height: 0; padding: 16px; }
    .chat { min-height: 0; }
  }
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">
      <div class="seal">邮</div>
      <div>
        <p class="eyebrow">Youzhihui</p>
        <h1>邮智汇</h1>
      </div>
    </div>
    <p class="brand-line">收信、识别、到点汇总</p>
    <nav class="nav">
      <button class="navbtn active" type="button" data-view="board">工作台</button>
      <button class="navbtn" type="button" data-view="setup">接入邮箱</button>
      <button class="navbtn" type="button" data-view="chat">调度对话</button>
      <button class="navbtn" type="button" data-view="records">运行记录</button>
    </nav>
    <div class="day-nav" id="dayNav" hidden>
      <p class="side-label">按天查看</p>
      <div id="dayList"></div>
    </div>
    <div class="side-foot">
      <div class="badge" id="badge">正在连接</div>
      <p class="side-note">识别和汇总只作辅助，回复或执行前请人工复核。</p>
      <button class="quit" type="button" id="quit">退出程序</button>
    </div>
  </aside>
  <main>
    <section class="view" id="view-board">
      <header class="page-head">
        <p class="kicker">今天的收件</p>
        <h2>工作台</h2>
        <p class="rulechip" id="meta">正在读取当前规则。</p>
        <p class="hint" id="boxScope"></p>
        <div class="headtools">
          <button type="button" id="scanNow">立即扫描</button>
          <span class="meta">马上同时看所有邮箱的未读</span>
        </div>
        <p class="hint" id="scanNote" hidden></p>
        <p class="msg" id="headMsg"></p>
      </header>
      <section class="panel board">
        <div class="lane-tools boardsearch">
          <input id="mailSearch" type="text" placeholder="搜索任务和邮件" />
        </div>
        <div class="split">
          <section class="lane">
            <div class="lane-head">
              <h2>任务</h2>
              <p class="meta" id="taskMeta">已完成 0</p>
              <div class="doneclear" id="doneClear" hidden></div>
            </div>
            <div id="tasks" class="lane-body"></div>
          </section>
          <section class="lane">
            <div class="lane-head"><h2>最近邮件</h2></div>
            <div id="wipeBar"></div>
            <p class="msg" id="actMsg"></p>
            <div class="filters" id="mailFilters">
              <button class="chip active" type="button" data-filter="all">全部</button>
              <button class="chip" type="button" data-filter="useful">有用</button>
              <button class="chip" type="button" data-filter="ordinary">普通</button>
              <button class="chip" type="button" data-filter="spam">垃圾</button>
            </div>
            <div class="toolrow">
              <button class="textbtn" type="button" id="wipeMails">一键删除</button>
              <button class="textbtn" type="button" id="exportDb">导出表格</button>
            </div>
            <div id="exportBar" class="exportbar" hidden>
              <input id="backupStart" type="date" aria-label="导出开始日期" />
              <span class="meta">到</span>
              <input id="backupEnd" type="date" aria-label="导出结束日期" />
              <button type="button" id="exportGo">确定导出</button>
              <button class="textbtn" type="button" id="exportCancel">取消</button>
            </div>
            <div id="mails" class="lane-body"></div>
          </section>
        </div>
      </section>
    </section>
    <section class="view" id="view-setup" hidden>
      <header class="page-head">
        <p class="kicker">先接上邮箱</p>
        <h2>接入邮箱</h2>
        <p>可以添加多个邮箱。公共邮箱会自动填服务器，其他邮箱自己填收信和发信地址。</p>
      </header>
      <section class="panel setup">
        <div id="mailboxList"></div>
        <form id="form">
          <label class="field" for="username">邮箱地址</label>
          <input id="username" type="text" autocomplete="username" placeholder="name@example.com">
          <p class="detect" id="detect">输入完整邮箱后自动填服务器。识别不到就改下面两行。</p>
          <label class="field" for="password">授权码</label>
          <input id="password" type="password" autocomplete="current-password" placeholder="不是网页登录密码">
          <div class="servers">
            <label for="imapHost">收信服务器</label>
            <label for="imapPort">端口</label>
            <input id="imapHost" type="text" placeholder="imap.example.com" autocomplete="off">
            <input id="imapPort" class="narrow" type="text" inputmode="numeric" value="993">
            <label for="smtpHost">发信服务器</label>
            <label for="smtpPort">端口</label>
            <input id="smtpHost" type="text" placeholder="smtp.example.com" autocomplete="off">
            <input id="smtpPort" class="narrow" type="text" inputmode="numeric" value="465">
          </div>
          <label class="field" for="smtpMode">发信加密</label>
          <select id="smtpMode">
            <option value="ssl">SSL（常见端口 465）</option>
            <option value="starttls">STARTTLS（常见端口 587）</option>
          </select>
          <label class="field" for="webhook">钉钉 Webhook，可留空</label>
          <input id="webhook" type="text" placeholder="留空则不发钉钉">
          <button type="submit">添加邮箱</button>
          <p class="msg" id="msg"></p>
        </form>
        <div class="example hint">
          给这个邮箱发一封未读邮件，写清客户、时间和范围。例如：请于今天 18:30 汇总近 7 天关于对账的邮件，完成后回复本邮箱。到点后先写成草稿，确认后再发。
        </div>
        <label class="bootline" for="autoStart">
          <input id="autoStart" type="checkbox">
          <span>开机后自动在后台运行</span>
        </label>
        <p class="hint" id="autoStartHint">登录 Windows 后会自己打开，不用再双击启动。</p>
      </section>
    </section>
    <section class="view" id="view-chat" hidden>
      <header class="page-head">
        <p class="kicker">建议和规则</p>
        <h2>调度对话</h2>
        <p class="rulechip" id="ruleText">正在读取已保存的规则。</p>
        <p class="intro">可以问 AI 有什么好的建议，也可以让它添加规则。没点确认，规则不会改。</p>
      </header>
      <section class="panel chat">
        <div class="limitbox">
          <label for="fetchLimit">每批收取</label>
          <input id="fetchLimit" type="number" min="5" max="40" step="5" value="20">
          <span>封</span>
          <button type="button" id="saveLimit">保存限制</button>
          <p class="hint">这一批扫完后，如果还有未读，再扫下一批。也可以直接说「每批 15 封」。</p>
        </div>
        <div class="thread" id="chat"></div>
        <div id="ruleConfirm" class="confirmbar" hidden></div>
        <form id="chatForm">
          <div class="quick">
            <button type="button" data-say="现在的规则">现在的规则</button>
            <button type="button" data-say="还能添加什么规则">还能添加什么</button>
            <button type="button" data-say="马上拉取">马上拉取</button>
            <button type="button" data-say="暂停拉取">暂停拉取</button>
          </div>
          <div class="composer">
            <textarea id="chatInput" title="回车发送，Shift+回车换行" placeholder="例如：每 10 分钟拉取，每批 20 封，每天 21:00 总结。回车发送，Shift+回车换行。"></textarea>
            <button class="sendbtn" type="submit">发送</button>
          </div>
          <div class="actions">
            <button class="ghost" id="resetRule" type="button">恢复默认</button>
            <span id="resetRuleConfirm" hidden>
              <button id="resetRuleOk" type="button">确定恢复</button>
              <button class="ghost" id="resetRuleCancel" type="button">取消</button>
            </span>
            <button class="ghost" id="clearChat" type="button">清空对话</button>
            <span id="clearChatConfirm" hidden>
              <button id="clearChatOk" type="button">确定清空</button>
              <button class="ghost" id="clearChatCancel" type="button">取消</button>
            </span>
          </div>
          <p class="msg" id="chatMsg"></p>
        </form>
      </section>
    </section>
    <section class="view" id="view-records" hidden>
      <header class="page-head">
        <p class="kicker">按天回看</p>
        <h2>运行记录</h2>
        <p>在左侧点某一天，查看当天的邮件和客户任务。</p>
      </header>
      <section class="panel record">
        <p class="day-title" id="dayTitle">今天</p>
        <p class="meta" id="dayMeta"></p>
        <div class="filters" id="dayFilters">
          <button class="chip active" type="button" data-filter="all">全部</button>
          <button class="chip" type="button" data-filter="useful">有用</button>
          <button class="chip" type="button" data-filter="ordinary">普通</button>
          <button class="chip" type="button" data-filter="spam">垃圾</button>
        </div>
        <h3>邮件</h3>
        <div id="dayMails"></div>
        <h3>客户任务</h3>
        <div id="dayTasks"></div>
      </section>
    </section>
  </main>
</div>
<div id="mailView" class="viewer" hidden></div>
<script>
const TYPE = {customer_task: "客户汇总", normal_mail: "普通邮件"};
const PARSE = {pending: "待解析", ok: "已解析", failed: "解析失败"};
const USE = {useful: "有用", spam: "垃圾", ordinary: "普通"};
const TASK = {pending: "等待执行", running: "执行中", draft: "待发送", done: "已完成", failed: "失败", need_time: "待确认时间"};
const formTouched = {username: false};
let filledOnce = false;
let chatSignature = null;
let activeView = "board";
let selectedDay = "";
let daySignature = "";
let latestRecords = [];
let latestMails = [];
let latestTasks = [];
let mailFilter = "all";
let dayFilter = "all";
let mailQuery = "";
let pendingMailId = 0;
let pendingBox = 0;
let mailboxCount = 0;
let pendingTaskId = 0;
let pendingTaskKind = "";
let pendingWhen = "";
const sendingTasks = new Set();
let pendingWipe = false;
let searchHits = null;
let searchToken = 0;
let searchTimer = 0;
let searchStamp = "";
let quietPaint = false;
let refreshing = false;
let userBusyUntil = 0;
let latestNow = "";
let backupDatesSet = false;
const draftText = {};
const replyFiles = {};
const REPLY_FILE_MAX = 8;
const REPLY_FILE_BYTES = 15 * 1024 * 1024;
let shownLimit = {mails: 30, dayMails: 30};
let doneOpen = false;
const openMails = new Set();
const openTasks = new Set();

function recordDayName(day) {
  const short = dayLabel(day);
  if (short === "今天" || short === "昨天" || short === "前天") return short;
  if (!day || day.length < 10) return short;
  return Number(day.slice(5, 7)) + "月" + Number(day.slice(8, 10)) + "日";
}

function openRecordMail(day, mailId) {
  if (!day) return;
  selectedDay = day;
  if (mailId) openMails.add(Number(mailId));
  daySignature = "";
  renderDays();
  const lane = document.getElementById("dayMails");
  const card = lane && lane.querySelector('[data-mail="' + mailId + '"]');
  if (card) fitCard(lane, card);
}

function dayLabel(day) {
  const today = (latestNow || "").slice(0, 10);
  if (!today || day.length !== 10) return day;
  const ms = Date.parse(today + "T00:00:00") - Date.parse(day + "T00:00:00");
  const diff = Math.round(ms / 86400000);
  if (diff === 0) return "今天";
  if (diff === 1) return "昨天";
  if (diff === 2) return "前天";
  return day.slice(0, 4) === today.slice(0, 4) ? day.slice(5) : day;
}

function shortTime(value) {
  const text = value || "";
  return text.length >= 16 ? text.slice(11, 16) : text;
}

function senderName(mail) {
  const raw = (mail.sender || "").replace(/\s*<[^>]*>\s*$/, "").replace(/^["'\s。.]+|["'\s]+$/g, "");
  return raw || mail.sender_email || "未知发件人";
}
let pendingClearDone = false;
let latestDone = 0;

function renderDoneClear() {
  const bar = document.getElementById("doneClear");
  if (!bar) return;
  bar.hidden = !latestDone;
  bar.replaceChildren();
  if (!latestDone) {
    pendingClearDone = false;
    return;
  }
  if (!pendingClearDone) {
    bar.appendChild(miniButton("删除已完成（" + latestDone + "）", "textbtn", () => {
      pendingClearDone = true;
      renderDoneClear();
    }));
    return;
  }
  bar.appendChild(node("span", "fail", "确定删除 " + latestDone + " 条已完成的任务？删了不能撤回。邮件本身还在。"));
  bar.appendChild(miniButton("确定删除", "danger", async () => {
    pendingClearDone = false;
    try {
      const data = await postJson("/api/task/clear_done", {confirm: true});
      applyState(data);
      showAct(data.note || "已删除");
    } catch (err) {
      showAct(err.message, true);
      renderDoneClear();
    }
  }));
  bar.appendChild(miniButton("取消", "textbtn", () => {
    pendingClearDone = false;
    renderDoneClear();
  }));
}

function taskLabel(task) {
  if (task.status === "failed") return "失败";
  if (task.status === "draft" || taskLate(task)) return "未发送";
  if (!task.execute_at || task.status === "need_time") return "待安排";
  if (task.status === "done") return "";
  return "待处理";
}

function boxShort(value) {
  const text = String(value || "");
  const at = text.indexOf("@");
  return at > 1 ? text.slice(0, at) : text;
}

let undoTaskId = 0;
let undoMailId = 0;

function taskGroup(task) {
  if (task.status === "done") return "done";
  if (!task.execute_at) return "later";
  const today = (latestNow || "").slice(0, 10);
  const day = task.execute_at.slice(0, 10);
  if (today && day < today) return "overdue";
  if (!today || day === today) return "now";
  return "next";
}

function jumpToTask(taskId, boxId) {
  const box = document.getElementById(boxId || "tasks");
  const card = box && box.querySelector('[data-task="' + Number(taskId) + '"]');
  if (!card) {
    showAct("这条任务不在当前列表里", true);
    return;
  }
  const fold = card.closest("details");
  if (fold && !fold.open) {
    fold.open = true;
    doneOpen = true;
  }
  card.scrollIntoView({behavior: "smooth", block: "center"});
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1800);
}

function taskWhen(mail) {
  if (!mail.task_at) return mail.task_status === "done" ? "已完成" : "待安排";
  return mail.task_at.slice(0, 16) + (mail.task_status === "done" ? " · 已完成" : "");
}
let detectTimer = 0;

function renderChat(messages) {
  const rows = messages || [];
  const signature = rows.map((item) => item.id + ":" + item.content).join("|");
  if (signature === chatSignature) return;
  chatSignature = signature;
  const box = document.getElementById("chat");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
  box.replaceChildren();
  if (!rows.length) {
    box.appendChild(emptyState("还没有对话", "可以问有什么好的建议，也可以说要添加的规则。添加需要再点确认。"));
    return;
  }
  rows.forEach((item) => {
    const role = item.role === "user" ? "user" : "assistant";
    box.appendChild(node("div", "bubble " + role, item.content || ""));
  });
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text != null) el.textContent = text;
  return el;
}

function emptyState(title, detail) {
  const box = node("div", "empty");
  box.appendChild(node("b", "", title));
  if (detail) box.appendChild(node("p", "", detail));
  return box;
}

function tag(text, kind) {
  return node("span", kind ? "tag " + kind : "tag", text);
}

function mailTags(mail, withAccount) {
  const tags = node("div", "tags");
  if (!mail.parse_status || mail.parse_status === "pending") {
    tags.appendChild(tag("待解析"));
    return tags;
  }
  if (mail.parse_status === "failed") {
    tags.appendChild(tag("解析失败", "fail"));
    return tags;
  }
  const kind = mail.usefulness || "";
  tags.appendChild(tag(USE[kind] || "待识别", kind === "spam" ? "ordinary" : kind));
  if (mail.source === "upload") tags.appendChild(tag("扫描件", "upload"));
  if (mail.file_count) tags.appendChild(tag("附件 " + mail.file_count, "upload"));
  if (mail.mail_type === "customer_task") tags.appendChild(tag("客户汇总", "useful"));
  if (mail.urgency === "高") tags.appendChild(tag("紧急"));
  else if (mail.urgency === "中") tags.appendChild(tag("中"));
  if (withAccount && mailboxCount > 1 && mail.account) tags.appendChild(tag(boxShort(mail.account)));
  return tags;
}

function filteredMails(rows, filter, query) {
  const text = (query || "").trim().toLowerCase();
  return (rows || []).filter((mail) => {
    if (filter && filter !== "all" && (mail.usefulness || "ordinary") !== filter) return false;
    if (!text) return true;
    const blob = [mail.subject, mail.sender, mail.sender_email, mail.account, mail.excerpt, mail.todo_items, mail.judge_note, mail.parse_error].join(" ").toLowerCase();
    return blob.includes(text);
  });
}

function showAct(text, bad) {
  const el = document.getElementById("actMsg");
  if (!el) return;
  el.textContent = text || "";
  el.className = bad ? "msg bad" : "msg ok";
}

async function callApi(url, body) {
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body || {})
    });
  } catch (err) {
    return {ok: false, data: {error: "连不上程序，请确认它还在运行后再试。"}};
  }
  let data = {};
  try {
    data = await res.json();
  } catch (err) {
    data = {};
  }
  if (!res.ok && !data.error) data.error = "操作失败（" + res.status + "）";
  return {ok: res.ok, data};
}

async function postJson(url, body) {
  const {ok, data} = await callApi(url, body);
  if (!ok) throw new Error(data.error);
  return data;
}

function miniButton(label, className, onClick) {
  const button = node("button", className, label);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function renderWipeBar() {
  const bar = document.getElementById("wipeBar");
  if (!bar) return;
  const kind = mailFilter || "all";
  const sig = pendingWipe ? kind : "off";
  if (bar.dataset.sig === sig) return;
  bar.dataset.sig = sig;
  bar.replaceChildren();
  if (!pendingWipe) {
    bar.hidden = true;
    return;
  }
  const words = {
    all: ["删除全部", "确定删除全部本机邮件和任务？删了不能撤回。邮箱里的原信还在。", "已删除本机全部邮件和任务"],
    useful: ["删除有用", "确定删除本机所有「有用」记录？相关任务一并删除，删了不能撤回。邮箱里的原信还在。", "已删除本机有用记录"],
    ordinary: ["删除普通", "确定删除本机所有「普通」记录？相关任务一并删除，删了不能撤回。邮箱里的原信还在。", "已删除本机普通记录"],
    spam: ["删除垃圾", "确定删除本机所有「垃圾」记录？相关任务一并删除，删了不能撤回。邮箱里的原信还在。", "已删除本机垃圾记录"]
  };
  const chosen = words[kind] || words.all;
  bar.hidden = false;
  bar.className = "wipebar";
  bar.appendChild(node("span", "", chosen[1]));
  bar.appendChild(miniButton("确定删除", "danger", async () => {
    pendingWipe = false;
    try {
      const data = await postJson("/api/mail/delete-all", {confirm: chosen[0], kind: kind});
      showAct(chosen[2]);
      applyState(data);
    } catch (err) {
      showAct(err.message, true);
      renderWipeBar();
    }
  }));
  bar.appendChild(miniButton("取消", "textbtn", () => {
    pendingWipe = false;
    showAct("");
    renderWipeBar();
  }));
}

function ownReply(report, title) {
  let text = String(report || "");
  text = text.replace(/【提示】[\s\S]*$/g, " ");
  text = text.replace(/发信情况[:：][\s\S]*/g, " ");
  text = text.replace(/关于「[^」]*」[，,]?\s*回复如下[:：]\s*/g, "");
  text = text.replace(/到点提醒[:：]\s*/g, "");
  text = text.replace(/\s+/g, " ").trim();
  const name = String(title || "").replace(/\s+/g, " ").trim();
  if (!text || text === name) return "";
  if (name && text.indexOf(name) === 0) {
    return text.slice(name.length).replace(/^[\s，,。:：]+/, "").trim();
  }
  return text;
}

function appendUndo(box, kind) {
  if (!box) return;
  const id = kind === "mail" ? undoMailId : undoTaskId;
  if (!id) return;
  if (kind === "mail" && box.id !== "mails") return;
  if (kind === "task" && box.id !== "tasks") return;
  const line = node("p", "undo");
  line.appendChild(document.createTextNode("已从列表拿掉。刷新前可以撤回。"));
  line.appendChild(miniButton("撤回", "textbtn", async () => {
    const current = kind === "mail" ? undoMailId : undoTaskId;
    if (kind === "mail") undoMailId = 0;
    else undoTaskId = 0;
    try {
      applyState(await postJson(kind === "mail" ? "/api/mail/undo" : "/api/task/undo", {id: current}));
    } catch (err) {
      showAct(err.message, true);
    }
  }));
  box.appendChild(line);
}

function markLaneBusy() {
  userBusyUntil = Date.now() + 450;
}

function fitCard(box, card) {
  if (!box || !card) return;
  const boxRect = box.getBoundingClientRect();
  const cardRect = card.getBoundingClientRect();
  if (cardRect.height >= boxRect.height - 8) {
    box.scrollTop += cardRect.top - boxRect.top;
    return;
  }
  if (cardRect.top < boxRect.top + 4) box.scrollTop -= boxRect.top + 4 - cardRect.top;
  else if (cardRect.bottom > boxRect.bottom - 4) box.scrollTop += cardRect.bottom - (boxRect.bottom - 4);
}

function renderMails(rows, targetId, emptyText, options) {
  const box = document.getElementById(targetId || "mails");
  const id = targetId || "mails";
  const all = rows || [];
  const sig = [id, emptyText || "", options && options.compactEmpty ? "1" : "0", undoMailId, shownLimit[id] || 50, pendingMailId, [...openMails].join("."), (latestNow || "").slice(0, 10), mailQuery, all.map((mail) => mail.id + ":" + mail.usefulness + ":" + mail.urgency + ":" + (mail.deadline || "") + ":" + (mail.todo_items || "") + ":" + (mail.reply_report || "") + ":" + (mail.task_id || "") + ":" + (mail.task_at || "") + ":" + (mail.task_status || "") + ":" + (mail.file_count || 0) + ":" + mail.parse_status + ":" + (mail.judge_note || "")).join(",")].join("|");
  if (box.dataset.sig === sig) return;
  if (quietPaint && box.dataset.sig && Date.now() < userBusyUntil) return;
  const keptScroll = box.scrollTop;
  box.dataset.sig = sig;
  box.replaceChildren();
  if (id === "mails") appendUndo(box, "mail");
    if (!all.length) {
    if (options && options.compactEmpty) box.appendChild(node("p", "quietline", emptyText || "没有新信"));
    else if (emptyText) box.appendChild(emptyState(emptyText, "可以换一天，或等新邮件进来后再看。"));
    else box.appendChild(emptyState("收件箱还是空的", mailQuery ? "没有符合搜索的邮件。" : "未读邮件进来后，会标成有用、普通或垃圾。正文留在本机，这里只看摘要。"));
    if (box.scrollTop !== keptScroll) box.scrollTop = keptScroll;
    return;
  }
  const limit = shownLimit[id] || 50;
  const perDay = {};
  all.forEach((mail) => {
    const day = (mail.received_at || "").slice(0, 10) || "未知日期";
    perDay[day] = (perDay[day] || 0) + 1;
  });
  let lastDay = null;
  all.slice(0, limit).forEach((mail) => {
    const day = (mail.received_at || "").slice(0, 10) || "未知日期";
    if (id !== "dayMails" && day !== lastDay) {
      lastDay = day;
      box.appendChild(node("h3", "dayhead", dayLabel(day) + " · " + perDay[day] + " 封"));
    }
    const open = openMails.has(mail.id) || pendingMailId === mail.id;
    const quiet = mail.usefulness === "ordinary" || mail.usefulness === "spam";
    const card = node("article", "card mrow" + (open ? " open" : "") + (quiet ? " quiet" : "") + (mail.urgency === "高" ? " urgent" : ""));
    card.dataset.mail = String(mail.id);
    const head = node("div", "mhead");
    head.title = open ? "点一下收起" : "点一下展开";
    const named = mail.subject && mail.subject !== "(无主题)";
    const stand = (mail.todo_items || mail.excerpt || "").trim().slice(0, 60);
    head.appendChild(node("strong", "", named ? mail.subject : (stand ? "（无主题）" + stand : "(无主题)")));
    head.appendChild(node("span", "mtime", shortTime(mail.received_at)));
    card.addEventListener("click", (event) => {
      if (event.target.closest("button, a, textarea, input, select, .actions")) return;
      const willOpen = !openMails.has(mail.id);
      if (willOpen) openMails.add(mail.id);
      else openMails.delete(mail.id);
      renderMails(all, id, emptyText);
      if (willOpen) fitCard(document.getElementById(id), document.querySelector("#" + id + ' [data-mail="' + mail.id + '"]'));
    });
    card.appendChild(head);
    const line = node("div", "mline");
    line.appendChild(node("span", "msender", senderName(mail)));
    line.appendChild(mailTags(mail));
    if (mail.task_id) {
      const link = node("a", "linked", "任务 · " + taskWhen(mail) + " →");
      link.href = "#";
      link.addEventListener("click", (event) => {
        event.preventDefault();
        jumpToTask(mail.task_id, id === "dayMails" ? "dayTasks" : "tasks");
      });
      line.appendChild(link);
    }
    card.appendChild(line);
    if (!open) {
      if (mail.todo_items) card.appendChild(node("p", "todo one", "识别：" + mail.todo_items));
      const reply = ownReply(mail.reply_report, mail.todo_items || mail.subject);
      if (reply) card.appendChild(node("p", "todo one", "回复：" + reply));
      if (mail.parse_error) card.appendChild(node("p", "fail one", "解析失败：" + mail.parse_error));
      box.appendChild(card);
      return;
    }
    card.appendChild(node("p", "", (mail.sender || mail.sender_email || "未知发件人") + (mail.received_at ? " · " + mail.received_at : "") + (mailboxCount > 1 && mail.account ? " · " + boxShort(mail.account) : "")));
    if (mail.judge_note && mail.judge_note !== mail.todo_items) card.appendChild(node("p", "why", mail.judge_note));
    if (mail.excerpt) card.appendChild(node("p", "", mail.excerpt));
    if (mail.todo_items) card.appendChild(node("p", "todo", "识别：" + mail.todo_items));
    const reply = ownReply(mail.reply_report, mail.todo_items || mail.subject);
    if (reply) card.appendChild(node("p", "todo", "回复：" + reply));
    if (mail.deadline) card.appendChild(node("p", mail.urgency === "高" ? "fail" : "", "截止：" + mail.deadline));
    if (mail.parse_error) card.appendChild(node("p", "fail", "解析失败：" + mail.parse_error));
    const actions = node("div", pendingMailId === mail.id ? "actions confirming" : "actions");
    if (pendingMailId === mail.id) {
      actions.appendChild(node("span", "fail", "拿掉这封邮件？刷新前可撤回。"));
      actions.appendChild(miniButton("确定删除", "danger", async () => {
        pendingMailId = 0;
        try {
          const removed = await postJson("/api/mail/delete", {id: mail.id, confirm: true});
          undoMailId = mail.id;
          undoTaskId = 0;
          applyState(removed);
          showAct("已从列表拿掉，刷新前可以撤回");
        } catch (err) {
          showAct(err.message, true);
          refresh();
        }
      }));
      actions.appendChild(miniButton("取消", "textbtn", () => {
        pendingMailId = 0;
        renderMails(all, id, emptyText);
      }));
    } else {
      ["useful", "ordinary", "spam"].forEach((kind) => {
        const button = miniButton(USE[kind], "mini" + (mail.usefulness === kind ? " on" : ""), async () => {
          if (mail.usefulness === kind) return;
          try {
            applyState(await postJson("/api/mail/usefulness", {id: mail.id, usefulness: kind}));
            showAct("已改为" + USE[kind]);
          } catch (err) {
            showAct(err.message, true);
          }
        });
        actions.appendChild(button);
      });
      actions.appendChild(miniButton("查看", "mini", () => openMail(mail.id)));
      if (mail.parse_status === "failed") {
        actions.appendChild(miniButton("重新识别", "mini", async (event) => {
          event.currentTarget.disabled = true;
          try {
            applyState(await postJson("/api/mail/reparse", {id: mail.id}));
            showAct("已重新排队识别，稍等片刻");
          } catch (err) {
            showAct(err.message, true);
            refresh();
          }
        }));
      }
      actions.appendChild(miniButton("删除", "textbtn", () => {
        pendingMailId = mail.id;
        pendingWipe = false;
        renderWipeBar();
        renderMails(all, id, emptyText);
      }));
    }
    card.appendChild(actions);
    box.appendChild(card);
  });
  if (all.length > limit) {
    const more = node("button", "more", "再显示 " + Math.min(50, all.length - limit) + " 封（还有 " + (all.length - limit) + " 封）");
    more.type = "button";
    more.addEventListener("click", () => {
      shownLimit[id] = limit + 50;
      renderMails(all, id, emptyText);
    });
    box.appendChild(more);
  }
  if (box.scrollTop !== keptScroll) box.scrollTop = keptScroll;
}

function taskClock(task) {
  const value = task.execute_at || "";
  if (value.length < 16) return "待安排";
  return value.slice(5, 16);
}

function taskLate(task) {
  return task.status === "draft" && !!task.execute_at && !!latestNow && task.execute_at <= latestNow;
}

function replyFileSig() {
  return Object.keys(replyFiles).sort().map((id) => id + "=" + (replyFiles[id] || []).map((file) => file.name + ":" + file.size).join(",")).join(";");
}

function readReplyFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({
      name: file.name || "附件",
      type: file.type || "application/octet-stream",
      size: file.size || 0,
      url: String(reader.result || "")
    });
    reader.onerror = () => reject(new Error("没有读出来"));
    reader.readAsDataURL(file);
  });
}

function addReplyFiles(taskId, fileList, rows, targetId, emptyText) {
  const incoming = Array.from(fileList || []);
  if (!incoming.length) return;
  const kept = (replyFiles[taskId] || []).slice();
  let total = kept.reduce((sum, file) => sum + file.size, 0);
  const accepted = [];
  for (const file of incoming) {
    if (kept.length + accepted.length >= REPLY_FILE_MAX) {
      showAct("一次最多 8 个附件", true);
      break;
    }
    if (!file.size) {
      showAct("这个文件是空的", true);
      continue;
    }
    if (file.size > REPLY_FILE_BYTES || total + file.size > REPLY_FILE_BYTES) {
      showAct("附件合计不能超过 15MB", true);
      continue;
    }
    total += file.size;
    accepted.push(file);
  }
  if (!accepted.length) return;
  Promise.all(accepted.map(readReplyFile)).then((items) => {
    replyFiles[taskId] = kept.concat(items);
    renderTasks(rows, targetId, emptyText);
  }).catch(() => showAct("附件没有读出来", true));
}

function bindReplyDrop(card, task, rows, targetId, emptyText) {
  if (!task.can_reply) return;
  card.addEventListener("dragover", (event) => {
    const types = event.dataTransfer ? [...event.dataTransfer.types] : [];
    if (!types.includes("Files")) return;
    event.preventDefault();
    card.classList.add("drop");
  });
  card.addEventListener("dragleave", (event) => {
    if (event.relatedTarget && card.contains(event.relatedTarget)) return;
    card.classList.remove("drop");
  });
  card.addEventListener("drop", (event) => {
    const files = event.dataTransfer && event.dataTransfer.files;
    if (!files || !files.length) return;
    event.preventDefault();
    card.classList.remove("drop");
    if (task.status !== "draft") {
      pendingTaskId = task.id;
      pendingTaskKind = "write";
    }
    openTasks.add(task.id);
    renderTasks(rows, targetId, emptyText);
    addReplyFiles(task.id, files, rows, targetId, emptyText);
  });
}

function renderReplyFiles(task, rows, targetId, emptyText) {
  const files = replyFiles[task.id] || [];
  if (!files.length) return null;
  const wrap = node("div", "attach");
  const chips = node("div", "chips");
  files.forEach((file, index) => {
    const chip = node("span", "chip");
    if ((file.type || "").startsWith("image/") && file.url) {
      const img = document.createElement("img");
      img.src = file.url;
      img.alt = "";
      chip.appendChild(img);
    }
    chip.appendChild(node("b", "", file.name));
    chip.appendChild(miniButton("移除", "textbtn", () => {
      const list = (replyFiles[task.id] || []).slice();
      list.splice(index, 1);
      if (list.length) replyFiles[task.id] = list;
      else delete replyFiles[task.id];
      renderTasks(rows, targetId, emptyText);
    }));
    chips.appendChild(chip);
  });
  wrap.appendChild(chips);
  return wrap;
}

function renderTasks(rows, targetId, emptyText) {
  const box = document.getElementById(targetId || "tasks");
  const query = (!targetId || targetId === "tasks") ? (mailQuery || "").trim().toLowerCase() : "";
  const list = (rows || []).filter((task) => {
    if (!query) return true;
    return [task.scope_rule, task.customer_name, task.recognition, task.report, task.box_name].join(" ").toLowerCase().includes(query);
  });
  const active = document.activeElement;
  const typing = active && box.contains(active) && active.matches("textarea.reply, input.when");
  const typingId = typing && active.dataset ? active.dataset.id || "" : "";
  const files = replyFileSig();
  const sig = [targetId || "tasks", pendingTaskId, pendingTaskKind, undoTaskId, query, [...openTasks].join("."), [...sendingTasks].join("."), (latestNow || "").slice(0, 10), files, list.map((task) => task.id + ":" + task.status + ":" + task.execute_at + ":" + (task.box_name || "") + ":" + (task.mail_day || "") + ":" + (task.source_mail_id || "") + ":" + (task.urgency || "") + ":" + (task.can_reply || 0) + ":" + (task.recognition || "") + ":" + (task.report || "").slice(0, 80) + ":" + (taskLate(task) ? "late" : "")).join(",")].join("|");
  if (box.dataset.sig === sig) return;
  if (typing && box.dataset.files === files) return;
  if (quietPaint && box.dataset.sig && Date.now() < userBusyUntil) return;
  const keptScroll = box.scrollTop;
  box.dataset.sig = sig;
  box.dataset.files = files;
  box.replaceChildren();
  appendUndo(box, "task");
  if (!list.length) {
    box.appendChild(emptyState(query ? "没有符合搜索的任务" : (emptyText || "还没有任务"), query || emptyText ? "" : "客户汇总会排在这里。当天来信写了几点要做什么，也会排进来。需要回复的，来了就能写。"));
    if (box.scrollTop !== keptScroll) box.scrollTop = keptScroll;
    return;
  }
  const grouped = !targetId || targetId === "tasks";
  const groups = {};
  if (grouped) {
    const counts = {overdue: 0, now: 0, next: 0, later: 0, done: 0};
    list.forEach((task) => { counts[taskGroup(task)] += 1; });
    [["overdue", "还没完成"], ["now", "今天要做"], ["next", "之后"], ["later", "待安排（信里没写时间）"], ["done", "已完成"]].forEach(([key, title]) => {
      if (!counts[key]) return;
      if (key === "done") {
        const fold = node("details", "tgroup done");
        fold.open = doneOpen;
        fold.appendChild(node("summary", "", title + " " + counts[key] + " 条"));
        fold.addEventListener("toggle", () => { doneOpen = fold.open; });
        groups[key] = fold;
      } else {
        const section = node("section", "tgroup " + key);
        section.appendChild(node("h3", "", title + " · " + counts[key]));
        groups[key] = section;
      }
      box.appendChild(groups[key]);
    });
  }
  list.forEach((task) => {
    const late = taskLate(task);
    const unsent = task.status === "draft";
    const writing = !unsent && pendingTaskId === task.id && pendingTaskKind === "write";
    const busy = pendingTaskId === task.id && !!pendingTaskKind;
    const finished = task.status === "done";
    const overdue = taskGroup(task) === "overdue";
    const urgent = task.urgency === "高" && !finished;
    const open = openTasks.has(task.id) || unsent || writing || busy;
    const card = node("article", "card mrow" + (open ? " open" : "") + (finished ? " done" : "") + (overdue ? " overdue" : "") + (late ? " late" : unsent ? " unsent" : ""));
    card.dataset.task = String(task.id);
    bindReplyDrop(card, task, rows, targetId, emptyText);
    const reminder = task.output_format === "reminder";
    const title = reminder
      ? (task.scope_rule || task.customer_name || "当天事项")
      : (task.scope_rule || task.customer_name || "未识别客户");
    const head = node("div", "mhead");
    head.title = open ? "点一下收起" : "点一下展开";
    head.appendChild(node("strong", "", title));
    head.appendChild(node("span", "mtime", taskClock(task)));
    card.addEventListener("click", (event) => {
      if (unsent || writing) return;
      if (event.target.closest("button, a, textarea, input, select, summary, .actions")) return;
      const willOpen = !openTasks.has(task.id);
      if (willOpen) openTasks.add(task.id);
      else openTasks.delete(task.id);
      renderTasks(rows, targetId, emptyText);
      const lane = document.getElementById(targetId || "tasks");
      if (willOpen) fitCard(lane, lane && lane.querySelector('[data-task="' + task.id + '"]'));
    });
    card.appendChild(head);
    const line = node("div", "mline");
    const tags = node("div", "tags");
    const label = taskLabel(task);
    const tone = task.status === "failed" ? "fail" : (late ? "spam" : "");
    if (label) tags.appendChild(tag(label, tone));
    if (urgent) tags.appendChild(tag("紧急"));
    if (mailboxCount > 1 && task.box_name) line.appendChild(node("span", "msender", task.box_name));
    line.appendChild(tags);
    if (targetId === "dayTasks" && task.mail_day && task.source_mail_id) {
      const from = node("a", "linked", "来自" + recordDayName(task.mail_day) + "的信");
      from.href = "#";
      from.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        openRecordMail(task.mail_day, task.source_mail_id);
      });
      line.appendChild(from);
    }
    card.appendChild(line);
    const replyNow = ownReply(task.report, task.scope_rule || title);
    if (!open) {
      if (task.can_reply && !unsent) {
        if (replyNow) {
          line.appendChild(miniButton("已回复", "mini", () => {
            openTasks.add(task.id);
            renderTasks(rows, targetId, emptyText);
          }));
        } else {
          line.appendChild(miniButton("回复", "mini", () => {
            pendingTaskId = task.id;
            pendingTaskKind = "write";
            openTasks.add(task.id);
            renderTasks(rows, targetId, emptyText);
          }));
        }
      }
      if (finished) {
        line.appendChild(miniButton("恢复", "mini", async () => {
          try {
            applyState(await postJson("/api/task/reopen", {id: task.id}));
            showAct("已恢复为未完成");
          } catch (err) {
            showAct(err.message, true);
          }
        }));
      }
      if (overdue) {
        line.appendChild(miniButton("确认已完成", "mini", () => {
          pendingTaskId = task.id;
          pendingTaskKind = "finish";
          openTasks.add(task.id);
          renderTasks(rows, targetId, emptyText);
        }));
      }
      const seen = (task.recognition || "").trim();
      const showSeen = seen && seen !== title && (targetId === "dayTasks" || seen.length > title.length);
      if (showSeen) card.appendChild(node("p", "todo one", "识别：" + seen));
      if (replyNow) card.appendChild(node("p", "todo one", "回复：" + replyNow));
      (grouped ? groups[taskGroup(task)] : box).appendChild(card);
      return;
    }
    card.appendChild(node("p", "", task.execute_at ? (unsent ? "截止时间 " : "执行时间 ") + task.execute_at : "还没定时间"));
    if (unsent) card.appendChild(node("p", "fail", late ? "到点了还没发送。" : "还没发送。请在到点前写好并确认发送。"));
    const seen = (task.recognition || "").trim();
    if (seen && seen !== title) card.appendChild(node("p", "todo", "识别：" + seen));
    else if (!unsent && task.scope_rule && task.scope_rule !== title) card.appendChild(node("p", "", task.scope_rule));
    const reply = (!unsent && !writing) ? ownReply(task.report, task.scope_rule || title) : "";
    if (reply) card.appendChild(node("p", "todo", "回复：" + reply));
    if (task.last_error && task.last_error !== "到点未发送") card.appendChild(node("p", "", task.last_error));
    if (unsent || writing) {
      const input = document.createElement("textarea");
      input.className = "reply";
      input.maxLength = 10000;
      input.dataset.id = String(task.id);
      input.value = draftText[task.id] != null ? draftText[task.id] : (unsent ? (task.report || "") : "");
      input.placeholder = "在这里写回复，附件和图片拖到这里。确认后才会发出。";
      input.addEventListener("input", () => { draftText[task.id] = input.value; });
      input.addEventListener("dragover", (event) => {
        const types = event.dataTransfer ? [...event.dataTransfer.types] : [];
        if (!types.includes("Files")) return;
        event.preventDefault();
        event.stopPropagation();
        input.classList.add("over");
      });
      input.addEventListener("dragleave", () => input.classList.remove("over"));
      input.addEventListener("drop", (event) => {
        const dropped = event.dataTransfer && event.dataTransfer.files;
        if (!dropped || !dropped.length) return;
        event.preventDefault();
        event.stopPropagation();
        input.classList.remove("over");
        addReplyFiles(task.id, dropped, rows, targetId, emptyText);
      });
      input.addEventListener("paste", (event) => {
        const pasted = event.clipboardData && event.clipboardData.files;
        if (!pasted || !pasted.length) return;
        event.preventDefault();
        addReplyFiles(task.id, pasted, rows, targetId, emptyText);
      });
      const replyBox = node("div", "composer replybox");
      const picked = renderReplyFiles(task, rows, targetId, emptyText);
      if (picked) replyBox.appendChild(picked);
      replyBox.appendChild(input);
      card.appendChild(replyBox);
    }
    const confirming = pendingTaskId === task.id && ["delete", "send", "finish", "retry"].includes(pendingTaskKind);
    const actions = node("div", confirming ? "actions confirming" : "actions");
    if (pendingTaskId === task.id && pendingTaskKind === "delete") {
      actions.appendChild(node("span", "fail", "拿掉这条任务？刷新前可撤回。"));
      actions.appendChild(miniButton("确定删除", "danger", async () => {
        pendingTaskId = 0;
        pendingTaskKind = "";
        try {
          const removed = await postJson("/api/task/delete", {id: task.id, confirm: true});
          undoTaskId = task.id;
          undoMailId = 0;
          applyState(removed);
          showAct("已从列表拿掉，刷新前可以撤回");
        } catch (err) {
          showAct(err.message, true);
          refresh();
        }
      }));
      actions.appendChild(miniButton("取消", "textbtn", () => {
        pendingTaskId = 0;
        pendingTaskKind = "";
        renderTasks(rows, targetId, emptyText);
      }));
    } else if (pendingTaskId === task.id && pendingTaskKind === "retry") {
      actions.appendChild(node("span", "fail", "确定再试一次这条失败的任务？"));
      actions.appendChild(miniButton("确定再试", "mini", async () => {
        pendingTaskId = 0;
        pendingTaskKind = "";
        try {
          applyState(await postJson("/api/task/retry", {id: task.id, confirm: true}));
          showAct("已放回待执行");
        } catch (err) {
          showAct(err.message, true);
          refresh();
        }
      }));
      actions.appendChild(miniButton("取消", "textbtn", () => {
        pendingTaskId = 0;
        pendingTaskKind = "";
        renderTasks(rows, targetId, emptyText);
      }));
    } else if (pendingTaskId === task.id && pendingTaskKind === "send") {
      const fileCount = (replyFiles[task.id] || []).length;
      actions.appendChild(node("span", "fail", "确定发送这封回复？" + (fileCount ? "带上 " + fileCount + " 个附件。" : "") + "发出后不能撤回。"));
      actions.appendChild(miniButton("确定发送", "danger", async () => {
        if (sendingTasks.has(task.id)) return;
        sendingTasks.add(task.id);
        pendingTaskId = 0;
        pendingTaskKind = "";
        renderTasks(rows, targetId, emptyText);
        try {
          const written = (draftText[task.id] != null ? draftText[task.id] : (task.report || "")).trim();
          const packed = (replyFiles[task.id] || []).map((file) => ({
            name: file.name,
            type: file.type,
            data: (file.url.split(",")[1] || "")
          }));
          const sent = await postJson("/api/task/send", {id: task.id, confirm: true, body: written, files: packed});
          delete replyFiles[task.id];
          applyState(sent);
          showAct("已发送");
        } catch (err) {
          showAct(err.message, true);
        } finally {
          sendingTasks.delete(task.id);
          refresh();
        }
      }));
      actions.appendChild(miniButton("取消", "textbtn", () => {
        if (task.status === "draft") {
          pendingTaskId = 0;
          pendingTaskKind = "";
        } else {
          pendingTaskId = task.id;
          pendingTaskKind = "write";
        }
        renderTasks(rows, targetId, emptyText);
      }));
    } else if (pendingTaskId === task.id && pendingTaskKind === "finish") {
      actions.appendChild(node("span", "ask", "确定标成已完成？"));
      actions.appendChild(miniButton("确定完成", "mini", async () => {
        pendingTaskId = 0;
        pendingTaskKind = "";
        try {
          applyState(await postJson("/api/task/done", {id: task.id}));
          showAct("已标记完成");
        } catch (err) {
          showAct(err.message, true);
        }
      }));
      actions.appendChild(miniButton("取消", "textbtn", () => {
        pendingTaskId = 0;
        pendingTaskKind = "";
        renderTasks(rows, targetId, emptyText);
      }));
    } else if (pendingTaskId === task.id && pendingTaskKind === "time") {
      const when = document.createElement("input");
      when.type = "datetime-local";
      when.className = "when";
      when.value = pendingWhen || (task.execute_at || "").slice(0, 16).replace(" ", "T");
      when.addEventListener("input", () => { pendingWhen = when.value; });
      actions.appendChild(when);
      actions.appendChild(miniButton("确定改期", "mini", async () => {
        const value = when.value;
        pendingTaskId = 0;
        pendingTaskKind = "";
        try {
          applyState(await postJson("/api/task/reschedule", {id: task.id, confirm: true, execute_at: value}));
          showAct("已改到新的时间");
        } catch (err) {
          showAct(err.message, true);
          refresh();
        }
      }));
      actions.appendChild(miniButton("取消", "textbtn", () => {
        pendingTaskId = 0;
        pendingTaskKind = "";
        renderTasks(rows, targetId, emptyText);
      }));
    } else if (sendingTasks.has(task.id)) {
      actions.appendChild(node("span", "", "正在发送…"));
    } else {
      if (task.can_reply && !unsent && !writing) {
        actions.appendChild(miniButton("回复", "mini", () => {
          pendingTaskId = task.id;
          pendingTaskKind = "write";
          openTasks.add(task.id);
          renderTasks(rows, targetId, emptyText);
        }));
      }
      if (writing) {
        actions.appendChild(miniButton("取消", "textbtn", () => {
          pendingTaskId = 0;
          pendingTaskKind = "";
          renderTasks(rows, targetId, emptyText);
        }));
      }
      if (unsent || writing) {
        actions.appendChild(miniButton("发送", "mini", () => {
          pendingTaskId = task.id;
          pendingTaskKind = "send";
          renderTasks(rows, targetId, emptyText);
        }));
      }
      if (task.status === "failed") {
        actions.appendChild(miniButton("再试一次", "mini", () => {
          pendingTaskId = task.id;
          pendingTaskKind = "retry";
          renderTasks(rows, targetId, emptyText);
        }));
      }
      if (reply && task.can_reply && !writing) {
        actions.appendChild(miniButton("再写一封", "textbtn", () => {
          pendingTaskId = task.id;
          pendingTaskKind = "write";
          openTasks.add(task.id);
          renderTasks(rows, targetId, emptyText);
        }));
      }
      if (task.status !== "done") {
        actions.appendChild(miniButton("确认已完成", "mini", () => {
          pendingTaskId = task.id;
          pendingTaskKind = "finish";
          renderTasks(rows, targetId, emptyText);
        }));
        actions.appendChild(miniButton(task.execute_at ? "改时间" : "设时间", "textbtn", () => {
          pendingTaskId = task.id;
          pendingTaskKind = "time";
          pendingWhen = "";
          renderTasks(rows, targetId, emptyText);
        }));
      }
      actions.appendChild(miniButton("删除", "textbtn", () => {
        pendingTaskId = task.id;
        pendingTaskKind = "delete";
        renderTasks(rows, targetId, emptyText);
      }));
    }
    card.appendChild(actions);
    (grouped ? groups[taskGroup(task)] : box).appendChild(card);
  });
  if (typingId) {
    const again = box.querySelector('textarea.reply[data-id="' + typingId + '"]');
    if (again) again.focus();
  }
  if (box.scrollTop !== keptScroll) box.scrollTop = keptScroll;
}

function renderBoardMails() {
  const searching = (mailQuery || "").trim().length > 0;
  if (searching && searchHits === null) return;
  const source = searching ? searchHits : latestMails;
  renderMails(
    filteredMails(source, mailFilter, searching ? "" : mailQuery),
    "mails",
    mailFilter === "all" ? "" : "这一类还没有邮件"
  );
}

function runSearch() {
  const q = (mailQuery || "").trim();
  if (!q) {
    searchHits = null;
    renderBoardMails();
    return;
  }
  const token = ++searchToken;
  postJson("/api/mail/search", {q}).then((data) => {
    if (token !== searchToken) return;
    searchHits = data.mails || [];
    renderBoardMails();
  }).catch((err) => {
    if (token !== searchToken) return;
    showAct(err.message, true);
  });
}

function openMail(id) {
  const box = document.getElementById("mailView");
  box.hidden = false;
  box.replaceChildren();
  const sheet = node("div", "sheet");
  sheet.appendChild(node("p", "", "正在读取正文…"));
  box.appendChild(sheet);
  postJson("/api/mail/open", {id}).then((data) => {
    const who = data.sender_email ? ((data.sender && data.sender !== data.sender_email) ? data.sender + " <" + data.sender_email + ">" : data.sender_email) : (data.sender || "未知发件人");
    sheet.replaceChildren();
    sheet.appendChild(node("strong", "", data.subject || "(无主题)"));
    sheet.appendChild(node("p", "meta", who + (data.received_at ? " · " + data.received_at : "")));
    if (data.judge_note) sheet.appendChild(node("p", "why", data.judge_note));
    if (data.todo_items) sheet.appendChild(node("p", "todo", "识别：" + data.todo_items));
    if (data.has_file) {
      const link = node("a", "textbtn", "打开原文件");
      link.href = "/api/scan/file?id=" + encodeURIComponent(data.id);
      link.target = "_blank";
      link.rel = "noopener";
      sheet.appendChild(link);
    }
    const files = data.files || [];
    if (files.length) {
      const gallery = node("div", "files");
      files.forEach((file) => {
        const url = "/api/mail/file?id=" + encodeURIComponent(file.id);
        const link = node("a", "file");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener";
        if ((file.content_type || "").startsWith("image/")) {
          const img = document.createElement("img");
          img.src = url;
          img.alt = file.name || "图片";
          img.loading = "lazy";
          link.appendChild(img);
        } else {
          link.appendChild(node("span", "", "附件：" + (file.name || "未命名")));
        }
        gallery.appendChild(link);
      });
      sheet.appendChild(node("p", "meta", "附件 " + files.length + " 个，点开看原图"));
      sheet.appendChild(gallery);
    }
    sheet.appendChild(node("div", "body", data.content || "（这封没有正文）"));
    if (data.truncated) sheet.appendChild(node("p", "meta", "正文较长，这里只显示前面一部分。"));
    sheet.appendChild(miniButton("关闭", "textbtn", () => closeMail()));
  }).catch((err) => {
    sheet.replaceChildren();
    sheet.appendChild(node("p", "fail", err.message));
    sheet.appendChild(miniButton("关闭", "textbtn", () => closeMail()));
  });
}

function closeMail() {
  const box = document.getElementById("mailView");
  box.hidden = true;
  box.replaceChildren();
}

function setText(id, value) {
  const node = document.getElementById(id);
  const text = String(value);
  if (node && node.textContent !== text) node.textContent = text;
}

function applyState(data) {
  setText("mailCount", data.mail_count);
  setText("usefulCount", data.useful_count || 0);
  setText("spamCount", data.spam_count || 0);
  latestNow = data.now || latestNow;
  if (!backupDatesSet && data.now) {
    const day = data.now.slice(0, 10);
    const start = document.getElementById("backupStart");
    const end = document.getElementById("backupEnd");
    if (start && !start.value) start.value = day;
    if (end && !end.value) end.value = day;
    backupDatesSet = true;
  }
  const today = (data.now || "").slice(0, 10);
  let left = 0;
  let todayN = 0;
  (data.tasks || []).forEach((task) => {
    if (task.status !== "done" && task.execute_at && today && task.execute_at.slice(0, 10) < today) left += 1;
    if (task.status !== "done" && task.execute_at && today && task.execute_at.slice(0, 10) === today) todayN += 1;
  });
  setText("taskMeta", "已完成 " + (data.done_count || 0));
  if ((data.done_count || 0) !== latestDone) {
    latestDone = data.done_count || 0;
    renderDoneClear();
  }
  setText("meta", "还没完成 " + left + " · 今天要做 " + todayN);
  setText("ruleText", data.rule_text || "");
  const scanNote = document.getElementById("scanNote");
  if (scanNote) {
    scanNote.hidden = !data.scan_note;
    setText("scanNote", data.scan_note || "");
  }
  const limitInput = document.getElementById("fetchLimit");
  if (limitInput && document.activeElement !== limitInput) {
    limitInput.value = data.fetch_limit || 20;
  }
  const autoBox = document.getElementById("autoStart");
  if (autoBox && document.activeElement !== autoBox) autoBox.checked = !!data.autostart;
  mailboxCount = (data.mailboxes || []).length;
  renderMailboxes(data.mailboxes || []);
  const scope = document.getElementById("boxScope");
  if (scope) {
    const lines = (data.mailboxes || []).map((item) => (item.label || item.address || "邮箱") + "：" + (item.scope || ""));
    scope.textContent = lines.join(" ");
  }
  renderChat(data.messages || []);
  renderProposal(data.proposal || "", !!data.proposal_fetch);
  const badge = document.getElementById("badge");
  if (data.running) {
    badge.textContent = "运行中";
    badge.className = "badge run";
  } else if (data.configured) {
    badge.textContent = "正在启动";
    badge.className = "badge wait";
  } else {
    badge.textContent = "等待填写邮箱";
    badge.className = "badge wait";
  }
  if (!filledOnce) {
    const password = document.getElementById("password");
    password.placeholder = "授权码，不是网页登录密码";
    const webhook = document.getElementById("webhook");
    webhook.placeholder = data.dingtalk_set ? "已保存，留空则不修改" : "留空则不发钉钉";
    filledOnce = true;
  }
  const stamp = (data.mails || []).map((mail) => mail.id + ":" + (mail.usefulness || "") + ":" + (mail.parse_status || "")).join(",");
  latestMails = data.mails || [];
  latestTasks = data.tasks || [];
  const kinds = {all: latestMails.length, useful: 0, ordinary: 0, spam: 0};
  latestMails.forEach((mail) => { const key = mail.usefulness || "ordinary"; if (key in kinds) kinds[key] += 1; });
  document.querySelectorAll("#mailFilters [data-filter]").forEach((chip) => {
    const key = chip.dataset.filter;
    const text = (key === "all" ? "全部" : USE[key]) + " " + (kinds[key] || 0);
    if (chip.textContent !== text) chip.textContent = text;
  });
  renderWipeBar();
  if ((mailQuery || "").trim()) {
    if (stamp !== searchStamp) {
      searchStamp = stamp;
      runSearch();
    } else renderBoardMails();
  } else {
    searchStamp = stamp;
    searchHits = null;
    renderBoardMails();
  }
  renderTasks(data.tasks || []);
  if (Array.isArray(data.records)) {
    latestRecords = data.records;
    if (activeView === "records") renderDays();
  }
}

function showView(name) {
  activeView = name;
  document.querySelectorAll(".view").forEach((view) => {
    view.hidden = view.id !== "view-" + name;
  });
  document.querySelectorAll(".navbtn").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === name);
  });
  document.getElementById("dayNav").hidden = name !== "records";
  if (name === "records") renderDays();
}

function renderDays() {
  const rows = latestRecords;
  if (!rows.some((row) => row.date === selectedDay)) {
    const today = (latestNow || "").slice(0, 10);
    selectedDay = rows.some((row) => row.date === today) ? today : (rows.length ? rows[0].date : "");
  }
  const signature = rows.map((row) => row.date + ":" + row.count).join("|") + "|" + selectedDay;
  if (signature !== daySignature) {
    daySignature = signature;
    const box = document.getElementById("dayList");
    box.replaceChildren();
    rows.forEach((row) => {
      const button = node("button", "daybtn" + (row.date === selectedDay ? " active" : ""));
      button.type = "button";
      button.dataset.date = row.date;
      button.appendChild(node("span", "day-label", row.label));
      button.appendChild(node("span", "day-sub", (row.mail_count || 0) + " 封 · " + (row.task_count || 0) + " 条"));
      box.appendChild(button);
    });
  }
  renderDayDetail();
}

function renderDayDetail() {
  const day = latestRecords.find((row) => row.date === selectedDay);
  const title = document.getElementById("dayTitle");
  const meta = document.getElementById("dayMeta");
  if (!day) {
    title.textContent = "今天";
    meta.textContent = "这一天还没有运行记录。";
    paintDayFilters([]);
    renderMails([], "dayMails", "今天没有新信", {compactEmpty: true});
    renderTasks([], "dayTasks", "这一天没有客户任务。");
    return;
  }
  title.textContent = day.label === day.title ? day.label : day.label + " · " + day.title;
  const mails = day.mails || [];
  const tasks = day.tasks || [];
  paintDayFilters(mails);
  const visible = filteredMails(mails, dayFilter);
  meta.textContent = mails.length + " 封邮件 · " + tasks.length + " 条任务";
  const today = (latestNow || "").slice(0, 10);
  const noMail = day.date === today ? "今天没有新信" : "这一天没有新信";
  const compact = dayFilter === "all" && !mails.length && tasks.length > 0;
  renderMails(visible, "dayMails", dayFilter === "all" ? noMail : "这一类没有邮件", {compactEmpty: compact});
  renderTasks(tasks, "dayTasks", "这一天没有客户任务。");
}

function paintDayFilters(mails) {
  const counts = {all: mails.length, useful: 0, ordinary: 0, spam: 0};
  mails.forEach((mail) => {
    const kind = mail.usefulness || "ordinary";
    if (counts[kind] != null) counts[kind] += 1;
  });
  const names = {all: "全部", useful: "有用", ordinary: "普通", spam: "垃圾"};
  document.querySelectorAll("#dayFilters .chip").forEach((chip) => {
    const key = chip.dataset.filter;
    chip.textContent = names[key] + " " + (counts[key] || 0);
  });
}

function renderMailboxes(rows) {
  const box = document.getElementById("mailboxList");
  if (!box) return;
  const list = rows || [];
  const sig = list.map((item) => item.id + ":" + item.address + ":" + item.imap_host).join("|") + "|" + pendingBox;
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.replaceChildren();
  list.forEach((item) => {
    const row = node("div", "mbox");
    row.appendChild(node("b", "", item.address || ""));
    row.appendChild(node("span", "", (item.label || "自填服务器") + " · " + (item.imap_host || "")));
    if (item.scope) row.appendChild(node("span", "", item.scope));
    if (pendingBox === item.id) {
      row.appendChild(node("span", "fail", "确定移出？已收下来的邮件还留着。"));
      row.appendChild(miniButton("确定移出", "danger", async () => {
        pendingBox = 0;
        try {
          applyState(await postJson("/api/mailbox/delete", {id: item.id, confirm: true}));
          document.getElementById("msg").className = "msg ok";
          document.getElementById("msg").textContent = "已移出这个邮箱。";
        } catch (err) {
          document.getElementById("msg").className = "msg bad";
          document.getElementById("msg").textContent = err.message;
          renderMailboxes(list);
        }
      }));
      row.appendChild(miniButton("取消", "textbtn", () => {
        pendingBox = 0;
        renderMailboxes(list);
      }));
    } else {
      row.appendChild(miniButton("移出", "textbtn", () => {
        pendingBox = item.id;
        renderMailboxes(list);
      }));
    }
    box.appendChild(row);
  });
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const records = activeView === "records" ? "1" : "0";
    const res = await fetch("/api/state?records=" + records);
    if (!res.ok) throw new Error(String(res.status));
    quietPaint = true;
    applyState(await res.json());
  } catch (err) {
    document.getElementById("badge").textContent = "界面连接中断";
  } finally {
    quietPaint = false;
    refreshing = false;
  }
}

let hostTouched = false;
["imapHost", "imapPort", "smtpHost", "smtpPort", "smtpMode"].forEach((id) => {
  document.getElementById(id).addEventListener("input", () => { hostTouched = true; });
});
document.getElementById("username").addEventListener("input", () => {
  formTouched.username = true;
  hostTouched = false;
  const value = document.getElementById("username").value.trim();
  const box = document.getElementById("detect");
  clearTimeout(detectTimer);
  if (!value.includes("@") || !value.split("@")[1]) {
    box.textContent = "输入完整邮箱后自动填服务器。识别不到就改下面两行。";
    return;
  }
  box.textContent = "正在识别…";
  detectTimer = setTimeout(async () => {
    try {
      const res = await fetch("/api/detect", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({username: value})
      });
      const data = await res.json();
      if (!hostTouched && data.imap_host) {
        document.getElementById("imapHost").value = data.imap_host;
        document.getElementById("imapPort").value = data.imap_port || "993";
        document.getElementById("smtpHost").value = data.smtp_host || "";
        document.getElementById("smtpPort").value = data.smtp_port || "465";
        document.getElementById("smtpMode").value = data.smtp_mode || "ssl";
      }
      box.textContent = data.imap_host
        ? ("已填 " + (data.label || "服务器") + "。不对就改下面的地址。")
        : "没识别出来。请自己填写收信和发信服务器。";
    } catch (err) {
      box.textContent = "没识别出来。请自己填写收信和发信服务器。";
    }
  }, 400);
});
document.getElementById("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const msg = document.getElementById("msg");
  msg.className = "msg";
  msg.textContent = "正在保存…";
  const submit = event.target.querySelector("button[type=submit]");
  if (submit) submit.disabled = true;
  const {ok, data} = await callApi("/api/config", {
    username: document.getElementById("username").value,
    password: document.getElementById("password").value,
    webhook: document.getElementById("webhook").value,
    imap_host: document.getElementById("imapHost").value,
    imap_port: document.getElementById("imapPort").value,
    smtp_host: document.getElementById("smtpHost").value,
    smtp_port: document.getElementById("smtpPort").value,
    smtp_mode: document.getElementById("smtpMode").value
  });
  if (submit) submit.disabled = false;
  if (!ok) {
    msg.className = "msg bad";
    msg.textContent = data.error || "没有保存成功";
    return;
  }
  msg.className = "msg ok";
  msg.textContent = "已添加，开始从这个邮箱收取邮件。";
  document.getElementById("username").value = "";
  document.getElementById("password").value = "";
  document.getElementById("webhook").value = "";
  hostTouched = false;
  filledOnce = false;
  refresh();
});
function renderProposal(text, fetch) {
  const bar = document.getElementById("ruleConfirm");
  const next = text || "";
  const kind = fetch ? "fetch" : "rule";
  const sig = next + "|" + (next ? kind : "");
  if (bar.dataset.text === sig && (next || bar.hidden)) return;
  bar.dataset.text = sig;
  bar.replaceChildren();
  if (!next) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  bar.appendChild(node("p", "", fetch ? "马上拉一次未读。" : ("改成：" + next)));
  const ok = node("button", "", fetch ? "确认拉取" : "确认");
  ok.type = "button";
  ok.addEventListener("click", () => decideRule(true));
  const cancel = node("button", "ghost", "取消");
  cancel.type = "button";
  cancel.addEventListener("click", () => decideRule(false));
  bar.append(ok, cancel);
}

async function decideRule(accept) {
  const msg = document.getElementById("chatMsg");
  msg.className = "msg";
  msg.textContent = accept ? "正在保存这条规则…" : "正在取消…";
  const bar = document.getElementById("ruleConfirm");
  bar.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  const {ok, data} = await callApi("/api/rule/confirm", {confirm: accept});
  bar.querySelectorAll("button").forEach((button) => { button.disabled = false; });
  if (!ok) {
    msg.className = "msg bad";
    msg.textContent = data.error || "没有完成";
    return;
  }
  msg.className = "msg ok";
  msg.textContent = accept ? "已确认，规则已保存。" : "已取消，规则保持原样。";
  chatSignature = "";
  bar.dataset.text = "";
  applyState(data);
}

function showPendingChat(text) {
  const box = document.getElementById("chat");
  const empty = box.querySelector(".empty");
  if (empty) empty.remove();
  box.appendChild(node("div", "bubble user", text));
  box.appendChild(node("div", "bubble assistant pending", "正在想…"));
  box.scrollTop = box.scrollHeight;
}

document.querySelectorAll("#chatForm .quick button").forEach((button) => {
  button.addEventListener("click", () => {
    const send = document.querySelector("#chatForm button[type=submit]");
    if (send && send.disabled) return;
    const input = document.getElementById("chatInput");
    input.value = button.dataset.say || "";
    document.getElementById("chatForm").requestSubmit();
  });
});
document.getElementById("chatInput").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  const button = document.querySelector("#chatForm button[type=submit]");
  if (button && button.disabled) return;
  document.getElementById("chatForm").requestSubmit();
});
document.getElementById("chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const msg = document.getElementById("chatMsg");
  const input = document.getElementById("chatInput");
  const button = event.target.querySelector("button[type=submit]");
  const text = input.value.trim();
  if (!text) {
    msg.className = "msg bad";
    msg.textContent = "请先写一句。";
    return;
  }
  const draft = input.value;
  input.value = "";
  input.disabled = true;
  button.disabled = true;
  document.querySelectorAll("#chatForm .quick button").forEach((item) => { item.disabled = true; });
  showPendingChat(text);
  msg.className = "msg";
  msg.textContent = "正在想…";
  try {
    const {ok, data} = await callApi("/api/chat", {message: text});
    chatSignature = "";
    if (!ok) {
      input.value = draft;
      msg.className = "msg bad";
      msg.textContent = data.error || "没有问到";
      return;
    }
    if (data.chat_error) {
      msg.className = "msg bad";
      msg.textContent = "规则没有改。";
      applyState(data);
      return;
    }
    msg.className = "msg ok";
    msg.textContent = data.proposal ? (data.proposal_fetch ? "确认后才会拉。" : "确认后才会保存。") : "已回复。规则没有改动。";
    document.getElementById("ruleConfirm").dataset.text = "";
    applyState(data);
  } catch (err) {
    input.value = draft;
    msg.className = "msg bad";
    msg.textContent = "没有连上演示页。";
  } finally {
    input.disabled = false;
    button.disabled = false;
    document.querySelectorAll("#chatForm .quick button").forEach((item) => { item.disabled = false; });
    input.focus();
  }
});
document.getElementById("saveLimit").addEventListener("click", async () => {
  const msg = document.getElementById("chatMsg");
  msg.className = "msg";
  msg.textContent = "正在保存限制…";
  const {ok, data} = await callApi("/api/rule/limit", {fetch_limit: document.getElementById("fetchLimit").value});
  if (!ok) {
    msg.className = "msg bad";
    msg.textContent = data.error || "没有保存成功";
    return;
  }
  msg.className = "msg ok";
  msg.textContent = "已保存。这一批扫完后，还有未读就会再扫下一批。";
  applyState(data);
});
document.getElementById("autoStart").addEventListener("change", async () => {
  const box = document.getElementById("autoStart");
  const hint = document.getElementById("autoStartHint");
  hint.textContent = box.checked ? "正在设置开机运行…" : "正在取消开机运行…";
  try {
    const data = await postJson("/api/autostart", {enabled: box.checked});
    applyState(data);
    hint.textContent = data.autostart ? "已设置。下次登录 Windows 会在后台打开。" : "已取消开机运行。";
  } catch (err) {
    box.checked = !box.checked;
    hint.textContent = err.message || "没有设置成功";
  }
});
document.getElementById("resetRule").addEventListener("click", () => {
  document.getElementById("resetRule").hidden = true;
  document.getElementById("resetRuleConfirm").hidden = false;
});
document.getElementById("resetRuleCancel").addEventListener("click", () => {
  document.getElementById("resetRuleConfirm").hidden = true;
  document.getElementById("resetRule").hidden = false;
});
document.getElementById("resetRuleOk").addEventListener("click", async () => {
  const msg = document.getElementById("chatMsg");
  msg.className = "msg";
  msg.textContent = "正在恢复默认…";
  const {ok, data} = await callApi("/api/rule/reset", {});
  document.getElementById("resetRuleConfirm").hidden = true;
  document.getElementById("resetRule").hidden = false;
  if (!ok) {
    msg.className = "msg bad";
    msg.textContent = data.error || "没有恢复成功";
    return;
  }
  msg.className = "msg ok";
  msg.textContent = "已恢复默认规则。";
  chatSignature = null;
  applyState(data);
});
document.getElementById("clearChat").addEventListener("click", () => {
  document.getElementById("clearChat").hidden = true;
  document.getElementById("clearChatConfirm").hidden = false;
});
document.getElementById("clearChatCancel").addEventListener("click", () => {
  document.getElementById("clearChatConfirm").hidden = true;
  document.getElementById("clearChat").hidden = false;
});
document.getElementById("clearChatOk").addEventListener("click", async () => {
  const msg = document.getElementById("chatMsg");
  msg.className = "msg";
  msg.textContent = "正在清空对话…";
  const {ok, data} = await callApi("/api/chat/clear", {confirm: true});
  document.getElementById("clearChatConfirm").hidden = true;
  document.getElementById("clearChat").hidden = false;
  if (!ok) {
    msg.className = "msg bad";
    msg.textContent = data.error || "没有清空";
    return;
  }
  msg.className = "msg ok";
  msg.textContent = "对话已清空。规则还是原来的。";
  chatSignature = null;
  applyState(data);
});
document.querySelectorAll(".navbtn").forEach((button) => {
  button.addEventListener("click", () => showView(button.dataset.view));
});
function bindFilter(boxId, apply) {
  document.getElementById(boxId).addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter]");
    if (!button) return;
    apply(button.dataset.filter);
    button.parentElement.querySelectorAll(".chip").forEach((chip) => {
      chip.classList.toggle("active", chip === button);
    });
  });
}
bindFilter("mailFilters", (value) => {
  mailFilter = value;
  shownLimit.mails = 30;
  renderWipeBar();
  renderBoardMails();
});
bindFilter("dayFilters", (value) => {
  dayFilter = value;
  shownLimit.dayMails = 30;
  renderDays();
});
document.getElementById("dayList").addEventListener("click", (event) => {
  const button = event.target.closest("[data-date]");
  if (!button) return;
  selectedDay = button.dataset.date;
  daySignature = "";
  renderDays();
});
document.getElementById("mailSearch").addEventListener("input", (event) => {
  mailQuery = event.target.value || "";
  shownLimit.mails = 30;
  renderTasks(latestTasks);
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 250);
});
document.getElementById("mailView").addEventListener("click", (event) => {
  if (event.target.id === "mailView") closeMail();
});
function headNote(text, bad) {
  const el = document.getElementById("headMsg");
  if (!el) return;
  el.textContent = text || "";
  el.className = bad ? "msg bad" : "msg ok";
}

document.getElementById("scanNow").addEventListener("click", async () => {
  const button = document.getElementById("scanNow");
  button.disabled = true;
  try {
    const data = await postJson("/api/scan/now", {});
    applyState(data);
    headNote(data.note || "已开始扫描");
  } catch (err) {
    headNote(err.message, true);
  } finally {
    setTimeout(() => { button.disabled = false; }, 3000);
  }
});

document.getElementById("exportDb").addEventListener("click", () => {
  const bar = document.getElementById("exportBar");
  bar.hidden = !bar.hidden;
});
document.getElementById("exportCancel").addEventListener("click", () => {
  document.getElementById("exportBar").hidden = true;
});
document.getElementById("exportGo").addEventListener("click", () => {
  const start = document.getElementById("backupStart").value;
  const end = document.getElementById("backupEnd").value;
  if (!start || !end) {
    showAct("请先选择开始和结束日期", true);
    return;
  }
  if (start > end) {
    showAct("开始日期不能晚于结束日期", true);
    return;
  }
  if ((Date.parse(end) - Date.parse(start)) / 86400000 > 366) {
    showAct("一次最多导出 366 天", true);
    return;
  }
  const link = document.createElement("a");
  link.href = "/api/backup?start=" + encodeURIComponent(start) + "&end=" + encodeURIComponent(end);
  link.click();
});
document.getElementById("wipeMails").addEventListener("click", () => {
  pendingWipe = true;
  pendingMailId = 0;
  renderWipeBar();
  renderBoardMails();
});
["mails", "tasks"].forEach((id) => {
  const lane = document.getElementById(id);
  lane.addEventListener("scroll", markLaneBusy, {passive: true});
  lane.addEventListener("pointerdown", markLaneBusy);
});
refresh();
let pollTimer = setInterval(() => {
  if (!document.hidden) refresh();
}, 5000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
document.getElementById("quit").addEventListener("click", async () => {
  if (!window.confirm("退出后会停止收取邮件。下次双击启动即可再打开。")) return;
  clearInterval(pollTimer);
  const badge = document.getElementById("badge");
  badge.textContent = "正在退出";
  badge.className = "badge wait";
  try {
    await fetch("/api/shutdown", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: "{}"
    });
  } catch (err) {}
  badge.textContent = "已退出";
});
</script>
</body>
</html>
"""
