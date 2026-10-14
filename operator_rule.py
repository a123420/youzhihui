"""界面里保存的拉取和发送规则。没改过的项目继续走默认值。"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from db_init import add_chat, clear_chat, get_operator_rule, init_db, list_chat, save_operator_rule

logger = logging.getLogger("youzhihui.rule")
CST = timezone(timedelta(hours=8))

POLL_ACTIONS = {"keep", "default", "interval", "daily", "paused", "once"}
REPLY_ACTIONS = {"keep", "default", "always", "never"}
REPORT_ACTIONS = {"keep", "default", "daily"}


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    if value < low or value > high:
        return default
    return value


def default_poll_minutes() -> int:
    return _int_env("MAIL_POLL_MINUTES", 5, 1, 24 * 60)


def active_fetch_limit(rule: dict | None = None) -> int:
    """每批收取封数。0 或越界都按 20 封，整次扫描会一批接一批直到扫完。"""
    raw = int((rule or current_rule()).get("fetch_limit") or 0)
    if raw < 5 or raw > 40:
        return 20
    return raw


def default_report_time() -> str:
    hour = _int_env("DAILY_REPORT_HOUR", 21, 0, 23)
    minute = _int_env("DAILY_REPORT_MINUTE", 0, 0, 59)
    return f"{hour:02d}:{minute:02d}"


def blank_rule() -> dict:
    return {
        "poll_mode": "default",
        "poll_minutes": 0,
        "poll_time": "",
        "reply_mode": "default",
        "report_mode": "default",
        "report_time": "",
        "fetch_limit": 0,
        "summary": "",
        "updated_at": "",
    }


def current_rule() -> dict:
    init_db()
    stored = get_operator_rule() or blank_rule()
    rule = blank_rule()
    rule.update({key: stored.get(key, rule[key]) for key in rule})
    rule["poll_minutes"] = int(rule.get("poll_minutes") or 0)
    rule["fetch_limit"] = int(rule.get("fetch_limit") or 0)
    rule["summary"] = describe_rule(rule)
    return rule


def is_default_rule(rule: dict) -> bool:
    return (
        rule.get("poll_mode") == "default"
        and rule.get("reply_mode") == "default"
        and rule.get("report_mode") == "default"
    )


def describe_rule(rule: dict) -> str:
    poll_mode = rule.get("poll_mode") or "default"
    if poll_mode == "interval" and int(rule.get("poll_minutes") or 0) > 0:
        poll = f"每 {int(rule['poll_minutes'])} 分钟拉取未读邮件"
    elif poll_mode == "daily" and rule.get("poll_time"):
        poll = f"每天 {rule['poll_time']} 拉取未读邮件"
    elif poll_mode == "once" and rule.get("poll_time"):
        poll = f"{rule['poll_time']} 拉取一次，之后恢复默认拉取"
    elif poll_mode == "paused":
        poll = "暂停拉取邮件"
    else:
        poll = f"每 {default_poll_minutes()} 分钟拉取未读邮件"

    reply_mode = rule.get("reply_mode") or "default"
    if reply_mode == "always":
        reply = "汇总完成后准备回复，确认后再发"
    elif reply_mode == "never":
        reply = "只生成汇总，不准备回信"
    else:
        reply = "客户要求回复时先写成草稿，确认后再发"

    if rule.get("report_mode") == "daily" and rule.get("report_time"):
        report = f"每天 {rule['report_time']} 推送全天总结"
    else:
        report = f"每天 {default_report_time()} 推送全天总结"
    return f"{poll}；每批 {active_fetch_limit(rule)} 封，扫完再看下一批；{reply}；{report}"


def _hhmm(text: str) -> str:
    matched = re.fullmatch(r"(\d{1,2}):(\d{2})", (text or "").strip())
    if not matched:
        return ""
    hour, minute = int(matched.group(1)), int(matched.group(2))
    if hour > 23 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def _minutes(value) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return 0
    if minutes < 1 or minutes > 24 * 60:
        return 0
    return minutes


def _once_moment(text: str) -> str:
    raw = (text or "").strip().replace("T", " ")
    matched = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})(?::(\d{2}))?",
        raw,
    )
    if matched:
        try:
            moment = datetime(
                int(matched.group(1)),
                int(matched.group(2)),
                int(matched.group(3)),
                int(matched.group(4)),
                int(matched.group(5)),
                int(matched.group(6) or 0),
            )
        except ValueError:
            return ""
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    clock = _hhmm(raw)
    if not clock:
        return ""
    hour, minute = [int(part) for part in clock.split(":")]
    moment = datetime.now(CST).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment <= datetime.now(CST):
        moment += timedelta(days=1)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def merge_rule(current: dict, parsed: dict) -> dict:
    """只改模型明确提到的项目，其余保持当前规则。"""
    rule = blank_rule()
    rule.update({key: current.get(key, rule[key]) for key in ("poll_mode", "poll_minutes", "poll_time", "reply_mode", "report_mode", "report_time", "fetch_limit")})
    rule["poll_minutes"] = int(rule.get("poll_minutes") or 0)
    rule["fetch_limit"] = int(rule.get("fetch_limit") or 0)

    poll_action = str(parsed.get("poll_action") or "keep").strip().lower()
    if poll_action not in POLL_ACTIONS:
        poll_action = "keep"
    if poll_action == "default":
        rule["poll_mode"] = "default"
        rule["poll_minutes"] = 0
        rule["poll_time"] = ""
    elif poll_action == "paused":
        rule["poll_mode"] = "paused"
        rule["poll_time"] = ""
    elif poll_action == "interval":
        minutes = _minutes(parsed.get("poll_minutes"))
        if minutes:
            rule["poll_mode"] = "interval"
            rule["poll_minutes"] = minutes
            rule["poll_time"] = ""
    elif poll_action == "daily":
        clock = _hhmm(str(parsed.get("poll_time") or ""))
        if clock:
            rule["poll_mode"] = "daily"
            rule["poll_time"] = clock
    elif poll_action == "once":
        moment = _once_moment(str(parsed.get("poll_time") or ""))
        if moment:
            rule["poll_mode"] = "once"
            rule["poll_time"] = moment

    reply_action = str(parsed.get("reply_action") or "keep").strip().lower()
    if reply_action in {"default", "always", "never"}:
        rule["reply_mode"] = reply_action

    report_action = str(parsed.get("report_action") or "keep").strip().lower()
    if report_action == "default":
        rule["report_mode"] = "default"
        rule["report_time"] = ""
    elif report_action == "daily":
        clock = _hhmm(str(parsed.get("report_time") or ""))
        if clock:
            rule["report_mode"] = "daily"
            rule["report_time"] = clock

    rule["summary"] = describe_rule(rule)
    return rule


def save_fetch_limit(value) -> str:
    """调度页直接保存每轮封数，不经过对话。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    if number < 5 or number > 40:
        raise ValueError("每轮只能设在 5 到 40 封之间")
    rule = current_rule()
    rule["fetch_limit"] = number
    rule["summary"] = describe_rule(rule)
    _apply_saved(rule)
    return rule["summary"]


def reply_mode() -> str:
    """default 时仍看环境变量 AUTO_REPLY。"""
    mode = current_rule().get("reply_mode") or "default"
    if mode in {"always", "never"}:
        return mode
    return os.getenv("AUTO_REPLY", "auto").strip().lower() or "auto"


def _apply_saved(rule: dict) -> None:
    save_operator_rule(rule)
    from scheduler_task import reschedule_from_rule

    reschedule_from_rule()


_pending_rule: dict | None = None

_RULE_KEYS = ("poll_mode", "poll_minutes", "poll_time", "reply_mode", "report_mode", "report_time", "fetch_limit")


def _same_rule(left: dict, right: dict) -> bool:
    return all(left.get(key) == right.get(key) for key in _RULE_KEYS)


def pending_summary() -> str:
    if not _pending_rule:
        return ""
    return str(_pending_rule.get("summary") or "")


def ask_for_advice(text: str) -> str:
    """问建议或草拟规则。规则先停在待确认，点确认后才保存。"""
    global _pending_rule
    message = (text or "").strip()
    if not message:
        raise ValueError("请先写一句建议或要添加的规则")
    if len(message) > 1000:
        raise ValueError("这句话太长了，请缩短到 1000 字以内")
    init_db()
    add_chat("user", message)
    before = current_rule()
    try:
        from llm_parser import interpret_schedule

        parsed = interpret_schedule(list_chat(8), before["summary"])
        rule = merge_rule(before, parsed)
        reply = str(parsed.get("reply") or "").strip()
        if _same_rule(before, rule):
            _pending_rule = None
            reply = reply or "这是建议，当前规则没有改动。"
        else:
            _pending_rule = rule
            reply = (reply or rule["summary"]) + "\n还没保存。确认添加后才会按这条执行。"
    except Exception as exc:
        logger.exception("调度对话没有生成建议")
        _pending_rule = None
        reply = f"这条没有生成，仍按原来的规则。{exc}"[:500]
    add_chat("assistant", reply)
    return reply


def confirm_pending(accept: bool) -> str:
    """确认后才写入规则。取消则丢掉这次建议。"""
    global _pending_rule
    init_db()
    if not _pending_rule:
        raise ValueError("没有待确认的规则")
    if not accept:
        _pending_rule = None
        reply = "已取消，规则保持原样。"
        add_chat("assistant", reply)
        return reply
    rule = _pending_rule
    _pending_rule = None
    _apply_saved(rule)
    reply = f"已确认并保存。{rule['summary']}"
    add_chat("assistant", reply)
    return reply


def clear_conversation() -> int:
    """清掉调度对话。待确认的建议一并丢掉，已经保存的规则不动。"""
    global _pending_rule
    init_db()
    _pending_rule = None
    return clear_chat()


def restore_default() -> str:
    global _pending_rule
    init_db()
    _pending_rule = None
    rule = blank_rule()
    rule["summary"] = describe_rule(rule)
    _apply_saved(rule)
    reply = f"已恢复默认规则。{rule['summary']}"
    add_chat("assistant", reply)
    return reply
