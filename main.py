"""邮智汇入口。

打开本机演示界面，填写邮箱后启动调度器，直到从页面退出。
调度器里包含邮箱轮询、客户汇总到点执行、每天固定时刻的工作总结。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

from quiet_windows import install as _hide_child_consoles

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_PATH = BASE_DIR / "data" / "runtime.json"
_hide_child_consoles()
load_dotenv(BASE_DIR / ".env")

from configure import _read_env
from db_init import DB_PATH, init_db, list_mailboxes
from demo_ui import start_demo_server
from scheduler_task import request_stop, should_stop, start_scheduler


def setup_logging() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    if sys.stdout is not None:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)
    file_handler = logging.FileHandler(log_dir / "youzhihui.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def _existing_url() -> str | None:
    try:
        info = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        url = str(info.get("url", "")).rstrip("/") + "/"
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        return None
    if not url.startswith("http://127.0.0.1:"):
        return None
    try:
        with urllib.request.urlopen(url + "api/state", timeout=1.5) as resp:
            body = resp.read(300)
    except (OSError, urllib.error.URLError):
        return None
    if b"mail_count" not in body:
        return None
    return url


def _write_runtime(url: str) -> None:
    RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_PATH.write_text(
        json.dumps({"url": url, "pid": os.getpid()}, ensure_ascii=False),
        encoding="utf-8",
    )


def _clear_runtime() -> None:
    try:
        RUNTIME_PATH.unlink()
    except OSError:
        pass


def _alert(text: str) -> None:
    if os.name != "nt":
        return
    box = ctypes.windll.user32.MessageBoxW
    box.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    box.restype = ctypes.c_int
    box(None, text, "邮智汇", 0x10)


def main() -> None:
    if sys.version_info < (3, 9):
        raise SystemExit("请使用 Python 3.9 及以上版本。实训环境建议 Python 3.10。")
    existing = _existing_url()
    if existing:
        webbrowser.open(existing)
        return
    setup_logging()
    logger = logging.getLogger("youzhihui")
    logger.info("初始化数据库: %s", DB_PATH)
    init_db()

    ready = threading.Event()
    running = {"on": False}

    def on_saved() -> None:
        load_dotenv(BASE_DIR / ".env", override=True)
        ready.set()

    def on_stop() -> None:
        logger.info("已从页面退出")
        request_stop()

    url = start_demo_server(on_saved, lambda: running["on"], on_stop)
    _write_runtime(url)
    logger.info("演示界面: %s", url)
    webbrowser.open(url)

    try:
        current = _read_env()
        if list_mailboxes() or (current.get("MAIL_USERNAME", "").strip() and current.get("MAIL_PASSWORD", "").strip()):
            on_saved()
            if not os.getenv("DINGTALK_WEBHOOK", "").strip():
                logger.warning("未填写钉钉 Webhook。程序会继续跑，通知只写在日志里。")
        else:
            logger.info("邮箱还没填，请在演示界面里填写后保存。")

        try:
            while not ready.wait(timeout=0.5):
                if should_stop():
                    logger.info("已停止")
                    return
        except KeyboardInterrupt:
            logger.info("已停止")
            return
        if should_stop():
            logger.info("已停止")
            return

        running["on"] = True
        logger.info("本地 OCR 会在遇到 jpg/png/pdf 附件时才加载，普通文本邮件不依赖它")
        start_scheduler()
    finally:
        _clear_runtime()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        logging.getLogger("youzhihui").exception("启动失败")
        _alert("邮智汇没有启动成功。请查看 logs\\youzhihui.log。")
        raise
