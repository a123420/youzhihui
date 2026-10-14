"""钉钉自定义机器人推送。

未配置 Webhook 时只写日志，不中断主流程。推送失败也只记录日志，避免告警函数反过来把调度打崩。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger("youzhihui.push")

_recent_errors: dict[str, float] = {}
ERROR_SUPPRESS_SECONDS = 1800


def _clip(text: str, limit: int = 3500) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...(内容过长已截断，完整内容见本地数据库或日志)"


def _signed_url(webhook: str) -> str:
    secret = os.getenv("DINGTALK_SECRET", "").strip()
    if not secret:
        return webhook
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    joiner = "&" if "?" in webhook else "?"
    return f"{webhook}{joiner}timestamp={timestamp}&sign={sign}"


def send_text(content: str) -> bool:
    """发送一条文本消息。成功返回 True。"""
    keyword = os.getenv("DINGTALK_KEYWORD", "").strip()
    if keyword and keyword not in content:
        content = f"{keyword}\n{content}"
    content = _clip(content)
    webhook = os.getenv("DINGTALK_WEBHOOK", "").strip()
    if not webhook:
        logger.warning("未配置 DINGTALK_WEBHOOK，消息只写入日志：\n%s", content)
        return False
    payload = {"msgtype": "text", "text": {"content": content}}
    try:
        response = requests.post(_signed_url(webhook), json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception:
        logger.exception("钉钉推送失败")
        return False
    if data.get("errcode") not in (0, None):
        logger.error("钉钉返回错误: %s", data)
        return False
    logger.info("钉钉推送成功")
    return True


def notify_error(module: str, error: Exception | str) -> None:
    message = str(error).strip() or "未知错误"
    key = f"{module}:{message[:120]}"
    now = time.time()
    last = _recent_errors.get(key, 0)
    if now - last < ERROR_SUPPRESS_SECONDS:
        logger.info("相同告警 30 分钟内已推送过，本次只记录日志：%s", key)
        return
    _recent_errors[key] = now
    send_text(f"【邮智汇告警】\n模块：{module}\n错误：{message[:800]}\n请查看 logs/youzhihui.log")


def notify_new_task(task: dict, subject: str) -> None:
    time_line = task.get("execute_at") or "未能识别，任务不会自动执行"
    send_text(
        "\n".join(
            [
                "【邮智汇】捕获到新的客户汇总任务",
                f"任务编号：{task.get('id', '')}",
                f"客户：{task.get('customer_name') or '未识别'}",
                f"要求时间：{task.get('execute_time') or '空'}",
                f"计划执行：{time_line}",
                f"整合范围：{task.get('scope_rule') or '空'}",
                f"输出格式：{task.get('output_format') or '空'}",
                f"后续操作：{task.get('next_action') or '空'}",
                f"来源邮件：{subject or '无标题'}",
                "请人工复核客户名称、执行时间和汇总范围。AI 提取结果只作辅助。",
            ]
        )
    )


def notify_reminder(task: dict) -> None:
    send_text(
        "\n".join(
            [
                "【邮智汇】已排入当天任务",
                f"事项：{task.get('scope_rule') or task.get('customer_name') or '未识别'}",
                f"计划时间：{task.get('execute_at') or '空'}",
                "到点只提醒，不会自动发信。请人工复核。",
            ]
        )
    )


def notify_reply_ready(task: dict) -> None:
    send_text(
        "\n".join(
            [
                "【邮智汇】可以回复了",
                task.get("scope_rule") or task.get("customer_name") or "这封来信",
                f"截止时间：{task.get('execute_at') or '未识别'}",
                "现在就能在页面上写回复并发送。到点还没发出去会标红提醒。",
            ]
        )
    )


def notify_reply_late(task: dict) -> None:
    send_text(
        "\n".join(
            [
                "【邮智汇】到点还没发送",
                task.get("scope_rule") or task.get("customer_name") or "这封回复",
                f"截止时间：{task.get('execute_at') or '空'}",
                "页面上已标红。请尽快回复，或点完成后不再提醒。",
            ]
        )
    )


def notify_reminder_due(task: dict) -> None:
    send_text(
        "\n".join(
            [
                "【邮智汇】到点提醒",
                task.get("scope_rule") or task.get("customer_name") or "当天事项",
                f"计划时间：{task.get('execute_at') or '空'}",
                "请人工确认后再执行。",
            ]
        )
    )


def notify_report_done(task: dict, report: str, reply_note: str) -> None:
    send_text(
        "\n".join(
            [
                "【邮智汇】客户汇总已完成",
                f"任务编号：{task.get('id', '')}",
                f"客户：{task.get('customer_name') or '未识别'}",
                f"发信情况：{reply_note or '未自动回复'}",
                "报告如下：",
                report,
            ]
        )
    )


def notify_daily(report: str) -> None:
    send_text(report)
