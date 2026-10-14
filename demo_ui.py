"""本机演示界面。只监听 127.0.0.1，不增加安装依赖。"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from configure import _read_env, apply_ui_config, detect_mailbox, sync_after_mailbox_change
from db_init import (
    complete_task,
    dashboard_snapshot,
    delete_mail,
    delete_mails_by_usefulness,
    delete_mailbox,
    delete_task,
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
    replace_draft_text,
    reschedule_task,
    search_mails,
    set_mail_usefulness,
)
from mail_client import send_mail

logger = logging.getLogger("youzhihui.ui")


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
    ask_for_advice,
    clear_conversation,
    confirm_pending,
    current_rule,
    is_default_rule,
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


def _records(days: int = 14) -> list[dict]:
    """最近两周的运行记录，按天归好。今天即使没有内容也会留下。"""
    today = now_str()[:10]
    start_day = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    activity = list_recent_activity(start_day)
    buckets: dict[str, dict] = {}

    def ensure(day: str) -> dict | None:
        if len(day) < 10:
            return None
        day = day[:10]
        if day < start_day or day > today:
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
        seen: set[str] = set()
        for key in ("created_at", "executed_at", "execute_at"):
            day = str(task.get(key) or "")[:10]
            if day in seen:
                continue
            bucket = ensure(day)
            if bucket is None:
                continue
            seen.add(day)
            bucket["tasks"].append(task)
    ensure(today)
    rows = [buckets[day] for day in sorted(buckets, reverse=True)]
    for row in rows:
        row["count"] = len(row["mails"]) + len(row["tasks"])
    return rows


def _scan_note() -> str:
    from scheduler_task import scan_note

    text = scan_note()
    return text if text.startswith("正在") else ""


def build_state() -> dict:
    init_db()
    current = _read_env()
    try:
        hour = int(current.get("DAILY_REPORT_HOUR", "21") or "21")
        minute = int(current.get("DAILY_REPORT_MINUTE", "0") or "0")
    except ValueError:
        hour, minute = 21, 0
    state = dashboard_snapshot(mail_limit=200)
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
            "records": _records(),
            "now": now_str(),
            "proposal": pending_summary(),
            "autostart": autostart_enabled(),
        }
    )
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
            self._json(200, build_state())
            return
        if path == "/api/backup":
            query = parse_qs(urlparse(self.path).query)
            start = (query.get("start") or [""])[0]
            end = (query.get("end") or [""])[0]
            self._send_backup(start, end)
            return
        self._json(404, {"error": "没有这个页面"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        raw = self._read_raw()
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

    def _read_raw(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > 100000:
            self.close_connection = True
            return None
        return self.rfile.read(length) if length else b""

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
        task = get_task(task_id)
        if not task or task.get("status") != "draft":
            self._json(404, {"error": "没有待发送的草稿"})
            return
        to_addr = str(task.get("reply_to") or "").strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to_addr):
            self._json(400, {"error": "没有可发送的收件人"})
            return
        body = str(payload.get("body") or "").strip()
        if body:
            try:
                saved = replace_draft_text(task_id, body)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            if not saved:
                self._json(404, {"error": "没有待发送的草稿"})
                return
        else:
            body = str(task.get("report") or "").strip()
        if not body:
            self._json(400, {"error": "请先写回复内容"})
            return
        try:
            account = None
            source = int(task.get("source_mail_id") or 0)
            if source:
                account = mailbox_for_mail(source)
            send_mail(to_addr, task.get("reply_subject") or "Re: 邮件", body, account)
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
  #view-board, #view-chat, #view-records { flex: 1; }
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
  .card.late { border-color: var(--rose); background: #f8ecea; }
  .card.unsent strong, .card.late strong { color: var(--rose); }
  textarea.reply {
    width: 100%; min-height: 76px; margin-top: 8px; padding: 8px 10px;
    border-radius: 10px; border: 1px solid var(--line); background: #fff;
    color: var(--ink); font: inherit; font-size: 13px; resize: vertical;
  }
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
  .stat { padding: 16px 16px 14px; border-radius: 16px; background: #fff; border: 1px solid var(--line); }
  .stat.useful { background: #e7f4f2; border-color: #cfe6e2; }
  .stat.spam { background: #f8ecea; border-color: #edd9d6; }
  .stat b { display: block; font-size: 32px; line-height: 1; letter-spacing: -0.03em; }
  .stat span { display: block; margin-top: 8px; font-weight: 700; }
  .stat em { display: block; margin-top: 4px; font-style: normal; color: var(--muted); font-size: 12px; line-height: 1.45; }
  .meta { margin: 0; color: var(--muted); font-size: 13px; }
  .split { display: grid; grid-template-columns: 1fr 1fr; gap: 0; align-items: stretch; }
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
  .tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .tag { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #fff; border: 1px solid var(--line); }
  .tag.useful { background: #e7f4f2; color: var(--teal-deep); border-color: transparent; }
  .tag.spam { background: #f8ecea; color: var(--rose); border-color: transparent; }
  .tag.ordinary { background: #f3efe6; color: var(--muted); border-color: transparent; }
  .why { margin: 6px 0 0; color: var(--ink); font-size: 13px; }
  .todo { margin: 6px 0 0; color: var(--teal-deep); font-size: 13px; }
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
  #clearChat[hidden], #clearChatConfirm[hidden] { display: none; }
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
  .thread {
    flex: 0 1 auto; min-height: 180px; max-height: 360px; overflow: auto;
    display: flex; flex-direction: column; gap: 8px;
    margin: 4px 0 12px; padding: 12px; border-radius: 14px;
    border: 1px solid var(--line); background: #fff;
  }
  .bubble { max-width: min(78%, 560px); padding: 10px 12px; border-radius: 14px; line-height: 1.5; white-space: pre-wrap; }
  .bubble.user { align-self: flex-end; background: #e7f4f2; }
  .bubble.assistant { align-self: flex-start; background: #fff; border: 1px solid #efe8dc; }
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
  .actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }
  button.ghost { background: transparent; color: var(--teal-deep); border: 1px solid var(--line); }
  button.ghost:hover { background: #eef8f6; }
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
        <p class="hint" id="scanNote" hidden></p>
      </header>
      <section class="panel board">
        <div class="stats">
          <div class="stat"><b id="mailCount">0</b><span>已入库</span><em>摘要已打码，点查看看全文</em></div>
          <div class="stat useful"><b id="usefulCount">0</b><span>有用</span><em>需要处理的业务邮件</em></div>
          <div class="stat spam"><b id="spamCount">0</b><span>垃圾</span><em>广告和营销，不进入汇总</em></div>
        </div>
        <div class="split">
          <section class="lane">
            <div class="lane-head"><h2>最近邮件</h2></div>
            <div class="lane-tools">
              <input id="mailSearch" type="text" placeholder="搜索全部已入库邮件" />
            </div>
            <div class="lane-tools">
              <input id="backupStart" type="date" aria-label="导出开始日期" />
              <span class="meta">到</span>
              <input id="backupEnd" type="date" aria-label="导出结束日期" />
              <button class="textbtn" type="button" id="exportDb">导出表格</button>
            </div>
            <div id="wipeBar"></div>
            <p class="msg" id="actMsg"></p>
            <div class="filters" id="mailFilters">
              <button class="chip active" type="button" data-filter="all">全部</button>
              <button class="chip" type="button" data-filter="useful">有用</button>
              <button class="chip" type="button" data-filter="ordinary">普通</button>
              <button class="chip" type="button" data-filter="spam">垃圾</button>
              <button class="textbtn" type="button" id="wipeMails">一键删除</button>
            </div>
            <div id="mails" class="lane-body"></div>
          </section>
          <section class="lane">
            <div class="lane-head">
              <h2>任务</h2>
              <p class="meta" id="taskMeta">待执行 0 · 已完成 0</p>
            </div>
            <div id="tasks" class="lane-body"></div>
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
        <p>可以问 AI 有什么好的建议，也可以让它添加规则。没点确认，规则不会改。</p>
      </header>
      <section class="panel chat">
        <p class="ruleline" id="ruleText">正在读取已保存的规则。</p>
        <div class="limitbox">
          <label for="fetchLimit">每批收取</label>
          <input id="fetchLimit" type="number" min="5" max="40" step="5" value="20">
          <span>封</span>
          <button type="button" id="saveLimit">保存限制</button>
          <p class="hint">这一批扫完后，如果还有未读，再扫下一批。</p>
        </div>
        <div class="thread" id="chat"></div>
        <div id="ruleConfirm" class="confirmbar" hidden></div>
        <form id="chatForm">
          <div class="composer">
            <textarea id="chatInput" placeholder="例如：有什么好的建议？　或：每 10 分钟拉取一次，每天 18:30 推送总结。"></textarea>
            <button class="sendbtn" type="submit">发送</button>
          </div>
          <div class="actions">
            <button class="ghost" id="resetRule" type="button">恢复默认</button>
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
let latestNow = "";
let backupDatesSet = false;
const draftText = {};
let shownLimit = {mails: 30, dayMails: 30};
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

function mailTags(mail) {
  const tags = node("div", "tags");
  if (!mail.parse_status || mail.parse_status === "pending") {
    tags.appendChild(tag("待解析"));
    return tags;
  }
  if (mail.parse_status === "failed") {
    tags.appendChild(tag("解析失败", "spam"));
    return tags;
  }
  tags.appendChild(tag(USE[mail.usefulness] || "待识别", mail.usefulness || ""));
  if (mail.mail_type === "customer_task") tags.appendChild(tag("客户汇总", "useful"));
  if (mail.urgency === "高" || mail.urgency === "中") tags.appendChild(tag(mail.urgency));
  if (mailboxCount > 1 && mail.account) tags.appendChild(tag(mail.account));
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
    all: ["删除全部", "确定删除全部已入库邮件和任务？不能恢复。", "已删除全部邮件和任务"],
    useful: ["删除有用", "确定删除所有「有用」邮件？相关任务一并删除，不能恢复。", "已删除有用邮件"],
    ordinary: ["删除普通", "确定删除所有「普通」邮件？相关任务一并删除，不能恢复。", "已删除普通邮件"],
    spam: ["删除垃圾", "确定删除所有「垃圾」邮件？相关任务一并删除，不能恢复。", "已删除垃圾邮件"]
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

function renderMails(rows, targetId, emptyText) {
  const box = document.getElementById(targetId || "mails");
  const id = targetId || "mails";
  const all = rows || [];
  const sig = [id, shownLimit[id] || 30, pendingMailId, mailQuery, all.map((mail) => mail.id + ":" + mail.usefulness + ":" + mail.parse_status + ":" + (mail.judge_note || "")).join(",")].join("|");
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.replaceChildren();
  if (!all.length) {
    if (emptyText) box.appendChild(emptyState(emptyText, "可以换一天，或等新邮件进来后再看。"));
    else box.appendChild(emptyState("收件箱还是空的", mailQuery ? "没有符合搜索的邮件。" : "未读邮件进来后，会标成有用、普通或垃圾。正文留在本机，这里只看摘要。"));
    return;
  }
  const limit = shownLimit[id] || 30;
  all.slice(0, limit).forEach((mail) => {
    const card = node("article", "card");
    card.appendChild(node("strong", "", mail.subject || "(无主题)"));
    card.appendChild(node("p", "", (mail.sender || mail.sender_email || "未知发件人") + (mail.received_at ? " · " + mail.received_at : "")));
    if (mail.judge_note && mail.judge_note !== mail.todo_items) card.appendChild(node("p", "why", mail.judge_note));
    if (mail.excerpt) card.appendChild(node("p", "", mail.excerpt));
    if (mail.todo_items) card.appendChild(node("p", "todo", "待办：" + mail.todo_items));
    if (mail.parse_error) card.appendChild(node("p", "fail", "解析失败：" + mail.parse_error));
    card.appendChild(mailTags(mail));
    const actions = node("div", "actions");
    if (pendingMailId === mail.id) {
      actions.appendChild(node("span", "fail", "确定删除这封？不能恢复。"));
      actions.appendChild(miniButton("确定删除", "danger", async () => {
        pendingMailId = 0;
        try {
          applyState(await postJson("/api/mail/delete", {id: mail.id, confirm: true}));
          showAct("已删除这封邮件");
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
    const more = node("button", "more", "还有 " + (all.length - limit) + " 封");
    more.type = "button";
    more.addEventListener("click", () => {
      shownLimit[id] = limit + 30;
      renderMails(all, id, emptyText);
    });
    box.appendChild(more);
  }
}

function taskLate(task) {
  return task.status === "draft" && !!task.execute_at && !!latestNow && task.execute_at <= latestNow;
}

function renderTasks(rows, targetId, emptyText) {
  const box = document.getElementById(targetId || "tasks");
  const list = rows || [];
  const active = document.activeElement;
  if (active && box.contains(active) && active.matches("textarea.reply, input.when")) return;
  const sig = [targetId || "tasks", pendingTaskId, pendingTaskKind, [...sendingTasks].join("."), list.map((task) => task.id + ":" + task.status + ":" + task.execute_at + ":" + (taskLate(task) ? "late" : "")).join(",")].join("|");
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.replaceChildren();
  if (!rows.length) {
    box.appendChild(emptyState(emptyText || "还没有任务", emptyText ? "" : "客户汇总会排在这里。当天来信写了几点要做什么，也会排进来。需要回复的，来了就能写。"));
    return;
  }
  rows.forEach((task) => {
    const late = taskLate(task);
    const unsent = task.status === "draft";
    const card = node("article", "card" + (late ? " late" : unsent ? " unsent" : ""));
    const reminder = task.output_format === "reminder";
    const title = reminder
      ? (task.scope_rule || task.customer_name || "当天事项")
      : (task.scope_rule || task.customer_name || "未识别客户");
    card.appendChild(node("strong", "", title));
    card.appendChild(node("p", "", (unsent ? "截止时间 " : "执行时间 ") + (task.execute_at || "未识别")));
    if (unsent) card.appendChild(node("p", "fail", late ? "到点了还没发送。" : "还没发送。请在到点前写好并确认发送。"));
    if (!unsent && task.scope_rule && task.scope_rule !== title) card.appendChild(node("p", "", task.scope_rule));
    if (!unsent && task.report) card.appendChild(node("p", "", task.report));
    if (task.last_error && task.last_error !== "到点未发送") card.appendChild(node("p", "", task.last_error));
    if (unsent) {
      const input = document.createElement("textarea");
      input.className = "reply";
      input.maxLength = 10000;
      input.dataset.id = String(task.id);
      input.value = draftText[task.id] != null ? draftText[task.id] : (task.report || "");
      input.placeholder = "在这里写回复。确认后才会发出。";
      input.addEventListener("input", () => { draftText[task.id] = input.value; });
      card.appendChild(input);
    }
    const tags = node("div", "tags");
    const label = late ? "到点未发送" : unsent ? "未发送" : (reminder && task.status === "pending" ? "等到点" : (TASK[task.status] || task.status || "等待"));
    tags.appendChild(tag(label, late || unsent ? "spam" : ""));
    card.appendChild(tags);
    const actions = node("div", "actions");
    if (pendingTaskId === task.id && pendingTaskKind === "delete") {
      actions.appendChild(node("span", "fail", "确定删除这条任务？"));
      actions.appendChild(miniButton("确定删除", "danger", async () => {
        pendingTaskId = 0;
        pendingTaskKind = "";
        try {
          applyState(await postJson("/api/task/delete", {id: task.id, confirm: true}));
          showAct("已删除这条任务");
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
      actions.appendChild(node("span", "fail", "确定发送这封回复？发出后不能撤回。"));
      actions.appendChild(miniButton("确定发送", "danger", async () => {
        if (sendingTasks.has(task.id)) return;
        sendingTasks.add(task.id);
        pendingTaskId = 0;
        pendingTaskKind = "";
        renderTasks(rows, targetId, emptyText);
        try {
          const written = (draftText[task.id] != null ? draftText[task.id] : (task.report || "")).trim();
          applyState(await postJson("/api/task/send", {id: task.id, confirm: true, body: written}));
          showAct("已发送");
        } catch (err) {
          showAct(err.message, true);
        } finally {
          sendingTasks.delete(task.id);
          refresh();
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
      if (task.status === "draft") {
        actions.appendChild(miniButton("发送", "mini", () => {
          pendingTaskId = task.id;
          pendingTaskKind = "send";
          renderTasks(rows, targetId, emptyText);
        }));
      }
      if (task.status !== "done") {
        actions.appendChild(miniButton("完成", "mini", async () => {
          try {
            applyState(await postJson("/api/task/done", {id: task.id}));
            showAct("已标记完成");
          } catch (err) {
            showAct(err.message, true);
          }
        }));
        actions.appendChild(miniButton("改时间", "textbtn", () => {
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
    box.appendChild(card);
  });
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
    if (data.todo_items) sheet.appendChild(node("p", "todo", "待办：" + data.todo_items));
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
  const draftNote = data.draft_count ? " · 待发送 " + data.draft_count : "";
  setText("taskMeta", "待执行 " + data.pending_count + draftNote + " · 已完成 " + data.done_count);
  setText("meta", data.rule_text || "");
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
  renderChat(data.messages || []);
  renderProposal(data.proposal || "");
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
  latestMails = data.mails || [];
  renderWipeBar();
  if ((mailQuery || "").trim()) runSearch();
  else {
    searchHits = null;
    renderBoardMails();
  }
  renderTasks(data.tasks || []);
  latestRecords = data.records || [];
  if (activeView === "records") renderDays();
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
  if (!rows.some((row) => row.date === selectedDay)) selectedDay = rows.length ? rows[0].date : "";
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
      button.appendChild(node("span", "day-sub", row.title));
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
    renderMails([], "dayMails", "这一天没有邮件");
    renderTasks([], "dayTasks", "这一天没有客户任务。");
    return;
  }
  title.textContent = day.label === day.title ? day.label : day.label + " · " + day.title;
  const visible = filteredMails(day.mails || [], dayFilter);
  meta.textContent = day.mails.length + " 封邮件 · " + day.tasks.length + " 条任务";
  renderMails(visible, "dayMails", dayFilter === "all" ? "这一天没有邮件" : "这一类没有邮件");
  renderTasks(day.tasks || [], "dayTasks", "这一天没有客户任务。");
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
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(String(res.status));
    applyState(await res.json());
  } catch (err) {
    document.getElementById("badge").textContent = "界面连接中断";
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
function renderProposal(text) {
  const bar = document.getElementById("ruleConfirm");
  const next = text || "";
  if (bar.dataset.text === next && (next || bar.hidden)) return;
  bar.dataset.text = next;
  bar.replaceChildren();
  if (!next) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  bar.appendChild(node("p", "", "建议改成：" + next));
  const ok = node("button", "", "确认添加");
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

document.getElementById("chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const msg = document.getElementById("chatMsg");
  const button = event.target.querySelector("button[type=submit]");
  msg.className = "msg";
  msg.textContent = "正在想…";
  button.disabled = true;
  try {
    const {ok, data} = await callApi("/api/chat", {message: document.getElementById("chatInput").value});
    if (!ok) {
      msg.className = "msg bad";
      msg.textContent = data.error || "没有问到";
      return;
    }
    document.getElementById("chatInput").value = "";
    msg.className = "msg ok";
    msg.textContent = data.proposal ? "这是建议的规则，确认后才会保存。" : "已回复。规则没有改动。";
    chatSignature = "";
    document.getElementById("ruleConfirm").dataset.text = "";
    applyState(data);
  } catch (err) {
    msg.className = "msg bad";
    msg.textContent = "没有连上演示页。";
  } finally {
    button.disabled = false;
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
document.getElementById("resetRule").addEventListener("click", async () => {
  const msg = document.getElementById("chatMsg");
  msg.className = "msg";
  msg.textContent = "正在恢复默认…";
  const {ok, data} = await callApi("/api/rule/reset", {});
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
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 250);
});
document.getElementById("mailView").addEventListener("click", (event) => {
  if (event.target.id === "mailView") closeMail();
});
document.getElementById("exportDb").addEventListener("click", () => {
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
refresh();
let pollTimer = setInterval(refresh, 3000);
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
