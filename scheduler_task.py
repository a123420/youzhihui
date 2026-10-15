"""APScheduler 调度。

三种节奏：
- 周期任务：按 MAIL_POLL_MINUTES 扫描未读邮件。
- 任务驱动：按 TASK_SCAN_SECONDS 检查客户汇总任务是否到点。
- 每日固定时刻：默认 21:00 生成全天总结并推送钉钉。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from db_init import (
    MAX_PARSE_RETRY,
    bump_parse_retry,
    reset_mail_parse,
    claim_task,
    fail_task,
    finish_task,
    count_imported_older,
    count_recent_mails,
    get_conn,
    get_mail,
    get_task,
    stored_uids,
    mails_ready_for_seen,
    mark_seen_done,
    save_task_draft,
    init_db,
    insert_customer_task,
    insert_mail,
    list_due_tasks,
    list_mails_need_parse,
    mail_from_history,
    mailbox_accounts,
    list_unsent_due,
    mark_unsent_due,
    now_str,
    query_imported_older,
    query_mails_between,
    query_recent_mails,
    query_tasks_between,
    reset_running_tasks,
    save_mail_files,
    update_mail_parse,
)
from llm_parser import DISCLAIMER, DeepSeekUnreachable, generate_daily_report, generate_summary, parse_email
from mail_client import fetch_recent_mails, fetch_unseen_mails, mark_seen
from operator_rule import current_rule, describe_rule, is_default_rule, reply_mode, save_operator_rule
from push_notify import (
    notify_daily,
    notify_error,
    notify_new_task,
    notify_reminder,
    notify_reminder_due,
    notify_reply_late,
    notify_reply_ready,
    notify_report_done,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger("youzhihui.scheduler")
CST = timezone(timedelta(hours=8))
_scheduler: BackgroundScheduler | None = None
_stop = threading.Event()
_scan_note = ""
_fetch_failures: dict = {}
_link_down = threading.Event()
_parsing: set[int] = set()
_parsing_lock = threading.Lock()


def request_stop() -> None:
    """让主循环退出。页面上的退出和手动停止都走这里。"""
    _stop.set()


def should_stop() -> bool:
    return _stop.is_set()


def scan_note() -> str:
    return _scan_note

REPLY_HINTS = ("回复", "回邮", "回复邮件", "发送邮件", "发邮件", "邮件回复", "reply")


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    if value < low or value > high:
        return default
    return value


def _apply_day_period(raw: str, hour: int) -> int:
    if any(flag in raw for flag in ("下午", "晚上")) and hour < 12:
        return hour + 12
    if "中午" in raw and hour < 11:
        return hour + 12
    return hour


def _anchor_date(received_at: str):
    """邮件原来的日期。没有日期时返回空。"""
    if received_at and len(received_at) >= 10:
        try:
            return datetime.strptime(received_at[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _run_now(text: str) -> bool:
    raw = (text or "").strip()
    return raw == "现在" or any(word in raw for word in ("立即", "马上", "即刻", "立刻"))


def _status_for_time(execute_at: str, run_now: bool = False) -> str:
    """已经过去的时间留给人确认。明确说立即执行的，仍然马上排队。"""
    if run_now and execute_at:
        return "pending"
    if not execute_at:
        return "need_time"
    if execute_at <= datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"):
        return "need_time"
    return "pending"


def normalize_execute_time(text: str, received_at: str = "") -> str:
    """把客户要求的执行时间尽量收成 YYYY-MM-DD HH:MM:SS。无法识别时返回空字符串。"""
    if text is None:
        return ""
    raw = str(text).strip().strip("。；;，,")
    if not raw:
        return ""
    raw = raw.replace("：", ":")

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    matched = re.search(
        r"(?P<y>\d{4})\s*[-/.年]\s*(?P<mo>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*日?"
        r"(?:\s*(?:上午|下午|晚上|中午|凌晨)?\s*(?P<h>\d{1,2})\s*[:点时]\s*(?P<mi>\d{1,2})?)?",
        raw,
    )
    if matched and matched.group("h") is not None:
        try:
            hour = _apply_day_period(raw, int(matched.group("h")))
            moment = datetime(
                int(matched.group("y")),
                int(matched.group("mo")),
                int(matched.group("d")),
                hour,
                int(matched.group("mi") or 0),
            )
            return moment.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    if any(word in raw for word in ("立即", "马上", "即刻", "立刻")) or raw == "现在":
        return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    has_today = ("今天" in raw) or ("今日" in raw)
    has_tomorrow = "明天" in raw
    if has_today == has_tomorrow:
        return ""
    clock = re.search(r"(\d{1,2})\s*[:点时]\s*(\d{1,2})?", raw)
    if not clock:
        return ""
    anchor = _anchor_date(received_at) or datetime.now(CST).replace(tzinfo=None).date()
    if has_tomorrow:
        anchor += timedelta(days=1)
    try:
        moment = datetime(
            anchor.year,
            anchor.month,
            anchor.day,
            _apply_day_period(raw, int(clock.group(1))),
            int(clock.group(2) or 0),
        )
    except ValueError:
        return ""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _date_with_day(today, day_num: int):
    """「15号」落到本月或下月的这一天。已经过去的号，算下个月。"""
    if not 1 <= day_num <= 31:
        return None
    year, month = today.year, today.month
    for _ in range(2):
        try:
            candidate = datetime(year, month, day_num).date()
        except ValueError:
            candidate = None
        if candidate and candidate >= today:
            return candidate
        month += 1
        if month == 13:
            month = 1
            year += 1
    return None


_NOT_A_DATE = re.compile(
    r"^(?:塔机|塔吊|起重机|挖掘机|设备|楼|栋|车间|生产线|机组|机|柜|车|仓|泵|阀|门|道)"
)


def _calendar_day_number(text: str) -> int | None:
    """「15号」是日期。「3号塔机」是设备编号，不能当成几号。"""
    for match in re.finditer(r"(?<!\d)(\d{1,2})\s*号", text):
        tail = text[match.end() :].lstrip()
        if _NOT_A_DATE.match(tail):
            continue
        number = int(match.group(1))
        if 1 <= number <= 31:
            return number
    return None


def _clock_value(match: re.Match, text: str) -> tuple[int, int] | None:
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if minute > 59:
        return None
    span = text[max(0, match.start() - 4) : match.end()]
    hour = _apply_day_period(span, hour)
    if hour > 23:
        return None
    return hour, minute


def _pick_clock(text: str) -> tuple[int, int] | None:
    """「今天18点前」是要完成的时间，优先于告警发生的钟点。"""
    deadline = re.search(
        r"(?<!\d)(\d{1,2})\s*[:点时]\s*(\d{1,2})?\s*(?:分)?\s*(?:前|之前|以前)",
        text,
    )
    if deadline:
        picked = _clock_value(deadline, text)
        if picked:
            return picked
    clock = re.search(r"(?<!\d)(\d{1,2})\s*[:点时]\s*(\d{1,2})?(?!\d)", text)
    if not clock:
        return None
    return _clock_value(clock, text)


_QUOTED_HEADER = re.compile(
    r"^\s*(?:发送时间|发件时间|发送日期|日期|时间|sent|date|发件人|from|收件人|to|抄\s*送|cc|主\s*题|subject)\s*[:：].*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_quoted_headers(content: str) -> str:
    """转发和引用里的「发送时间：…」是原信的时间，不是要做事的时间。"""
    return _QUOTED_HEADER.sub(" ", content or "")


_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}


def _weekday_date(text: str, anchor):
    """「本周五」「下周一」「周三」按来信那天推算。"""
    matched = re.search(r"(本周|这周|下周|下个星期|下星期|周|星期|礼拜)([一二三四五六日天1-7])", text)
    if not matched:
        return None
    target = _WEEKDAY[matched.group(2)]
    prefix = matched.group(1)
    monday = anchor - timedelta(days=anchor.weekday())
    if prefix.startswith("下"):
        return monday + timedelta(days=7 + target)
    day = monday + timedelta(days=target)
    if prefix in ("周", "星期", "礼拜") and day < anchor:
        day += timedelta(days=7)
    return day


def reminder_execute_at(
    subject: str,
    content: str,
    todo: str,
    received_at: str,
    extra: str = "",
) -> str:
    """当天来信里写了几点要做事，就排到那天的这个钟点。没有钟点则不排。"""
    exact = normalize_execute_time(extra, received_at) if extra else ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", exact or ""):
        return exact
    body = _strip_quoted_headers(content)
    text = " ".join(part for part in (extra, todo, subject, body[:240]) if part)
    text = text.replace("：", ":")
    picked = _pick_clock(text)
    if not picked:
        return ""
    hour, minute = picked
    today = datetime.now(CST).replace(tzinfo=None).date()
    anchor = _anchor_date(received_at) or today
    number = _calendar_day_number(text)
    weekday = _weekday_date(text, anchor)
    if weekday is not None:
        day = weekday
    elif number is not None:
        if anchor < today:
            try:
                day = datetime(anchor.year, anchor.month, number).date()
            except ValueError:
                return ""
        else:
            day = _date_with_day(today, number)
            if day is None:
                return ""
    elif "明天" in text:
        day = anchor + timedelta(days=1)
    elif any(word in text for word in ("今天", "今日", "今晚")):
        day = anchor
    elif anchor == today:
        day = today
    else:
        return ""
    try:
        moment = datetime(day.year, day.month, day.day, hour, minute, 0)
    except ValueError:
        return ""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0}


def _cn_number(text: str) -> int:
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + _CN_NUM.get(text[1:], 0)
    if text.endswith("十"):
        return _CN_NUM.get(text[:-1], 0) * 10
    if "十" in text:
        head, tail = text.split("十", 1)
        return _CN_NUM.get(head, 0) * 10 + _CN_NUM.get(tail, 0)
    return _CN_NUM.get(text, 0)


def requested_lookback_days(text: str) -> int:
    """客户写了「近3天」「最近一周」「近一个月」，就按这个范围汇总。没写返回 0。"""
    raw = re.sub(r"\s+", "", text or "")
    matched = re.search(r"(?:近|最近|过去|前)([0-9]{1,4}|[一两二三四五六七八九十]{1,3})(?:个)?(天|日|周|星期|礼拜|个月|月|年)", raw)
    if matched:
        number = _cn_number(matched.group(1))
        unit = matched.group(2)
        scale = {"天": 1, "日": 1, "周": 7, "星期": 7, "礼拜": 7, "个月": 30, "月": 30, "年": 365}[unit]
        return max(0, min(number * scale, 3650))
    if any(word in raw for word in ("今天的", "今日的", "当天的")):
        return 1
    if "本周" in raw or "这周" in raw:
        return 7
    if "本月" in raw or "这个月" in raw:
        return 31
    return 0


def _importance(execute_at: str, received_at: str, output_format: str = "") -> str:
    """模型没给紧急程度时补上：当天就要交的客户汇总算高，其余有时间的事项算中。"""
    if not execute_at:
        return ""
    anchor = _anchor_date(received_at)
    try:
        due = datetime.strptime(execute_at[:10], "%Y-%m-%d").date()
    except ValueError:
        return "中"
    if output_format != "reminder" and anchor and (due - anchor).days <= 0:
        return "高"
    return "中"


def _customer_todo(parsed: dict, execute_at: str, wants_reply: bool) -> str:
    scope = (parsed.get("scope_rule") or "").strip()
    if not scope:
        return ""
    when = f"{execute_at[:16]} 前" if execute_at else ""
    shape = (parsed.get("output_format") or "").strip()
    text = f"{when}汇总{scope}" + (f"，整理成{shape}" if shape else "")
    if wants_reply:
        text += "，并回复发件人"
    return text[:200]


def repair_mail_importance() -> int:
    """给已入库、还没做完的事项补上截止时间和紧急程度。"""
    from db_init import get_conn

    changed = 0
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.urgency, m.deadline, m.received_at, t.execute_at, t.output_format
              FROM mail_info m
              JOIN customer_task t ON t.source_mail_id = m.id
             WHERE t.status != 'done' AND t.execute_at != ''
               AND (IFNULL(m.urgency, '') = '' OR IFNULL(m.deadline, '') = '')
            """
        ).fetchall()
        for row in rows:
            urgency = row["urgency"] or _importance(row["execute_at"], row["received_at"] or "", row["output_format"] or "")
            reminder = (row["output_format"] or "") == "reminder"
            deadline = row["deadline"] or ("" if reminder else row["execute_at"])
            conn.execute(
                "UPDATE mail_info SET urgency=?, deadline=? WHERE id=?",
                (urgency, deadline, row["id"]),
            )
            changed += 1
        tasks = conn.execute(
            """
            SELECT m.id, t.scope_rule, t.output_format, t.execute_at, t.reply_to
              FROM mail_info m
              JOIN customer_task t ON t.source_mail_id = m.id
             WHERE m.mail_type = 'customer_task' AND IFNULL(m.todo_items, '') = ''
            """
        ).fetchall()
        for row in tasks:
            todo = _customer_todo(
                {"scope_rule": row["scope_rule"], "output_format": row["output_format"]},
                row["execute_at"] or "",
                bool((row["reply_to"] or "").strip()),
            )
            if todo:
                conn.execute("UPDATE mail_info SET todo_items=? WHERE id=?", (todo, row["id"]))
                changed += 1
    return changed


def _reminder_note(moment: str, status: str) -> str:
    if not moment:
        return "信里没写时间，可以设个时间，或做完后点完成"
    if status == "need_time":
        return "时间已过，请确认是否还要执行"
    return "到点提醒，不自动发信"


def backfill_todo_tasks(days: int = 7) -> int:
    """最近几天有用、有待办却没进任务的信，补成任务。更早的旧信不补。"""
    from db_init import get_conn

    since = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.subject, m.content, m.todo_items, m.deadline, m.received_at, m.sender_email
              FROM mail_info m
             WHERE m.usefulness = 'useful' AND m.parse_status = 'ok'
               AND trim(IFNULL(m.todo_items, '')) != ''
               AND m.received_at >= ?
               AND IFNULL(m.task_made, 0) = 0
               AND NOT EXISTS (SELECT 1 FROM customer_task t WHERE t.source_mail_id = m.id)
            """,
            (since,),
        ).fetchall()
    added = 0
    for row in rows:
        action = (row["todo_items"] or "").strip()
        moment = reminder_execute_at(
            row["subject"] or "", row["content"] or "", action, row["received_at"] or "", row["deadline"] or ""
        )
        status = _status_for_time(moment)
        sender = row["sender_email"] or ""
        can_reply = _looks_like_email(sender)
        task = insert_customer_task(
            customer_name=action[:40],
            execute_time=moment,
            execute_at=moment,
            scope_rule=action[:200],
            output_format="reminder",
            next_action=_reminder_note(moment, status),
            source_mail_id=int(row["id"]),
            reply_to=sender if can_reply else "",
            reply_subject=_reply_subject(row["subject"] or "") if can_reply else "",
            status=status,
        )
        if task:
            added += 1
    if added:
        logger.info("最近 %s 天有待办的信已补进任务 %s 条", days, added)
    return added


def repair_open_reminders() -> int:
    """改正还没执行的提醒：设备编号不再当成日期，截止时间优先。"""
    from db_init import get_conn

    changed = 0
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.execute_at, m.subject, m.content, m.todo_items, m.deadline, m.received_at
              FROM customer_task t
              JOIN mail_info m ON m.id = t.source_mail_id
             WHERE t.output_format = 'reminder'
               AND t.status IN ('pending', 'need_time')
            """
        ).fetchall()
        for row in rows:
            moment = reminder_execute_at(
                row["subject"] or "",
                row["content"] or "",
                row["todo_items"] or "",
                row["received_at"] or "",
                row["deadline"] or "",
            )
            if not moment or moment == (row["execute_at"] or ""):
                continue
            status = _status_for_time(moment)
            action = "时间已过，请确认是否还要执行" if status == "need_time" else "到点提醒，不自动发信"
            conn.execute(
                """
                UPDATE customer_task
                   SET execute_at=?, execute_time=?, status=?, next_action=?
                 WHERE id=? AND status IN ('pending', 'need_time')
                """,
                (moment, moment, status, action, row["id"]),
            )
            changed += 1
            logger.info("已更正事项时间 id=%s %s -> %s", row["id"], row["execute_at"], moment)
    return changed


def _reply_subject(subject: str) -> str:
    subject = (subject or "邮件汇总").strip()
    if subject.lower().startswith("re:"):
        return subject[:200]
    return f"Re: {subject}"[:200]


def _looks_like_email(address: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address or ""))


_REPLY_PREFIX = re.compile(r"(回复|答复|re|fw|fwd)\s*[:：]", re.IGNORECASE)
_REPLY_NEGATE = re.compile(r"(请勿|无需|不用|不要|勿|禁止|不必)\s*(回复|回邮|发邮件|发送邮件|邮件回复)")


def asks_for_reply(text: str) -> bool:
    """主题里原有的「回复：」和「请勿回复」都不算客户要求回信。"""
    cleaned = _REPLY_NEGATE.sub(" ", text or "")
    cleaned = _REPLY_PREFIX.sub(" ", cleaned)
    lowered = cleaned.lower()
    return any(hint in lowered for hint in REPLY_HINTS)


def _should_reply(next_action: str, reply_to: str) -> bool:
    mode = reply_mode()
    if mode == "never":
        return False
    if not _looks_like_email(reply_to):
        return False
    if mode == "always":
        return True
    text = (next_action or "").lower()
    return any(hint in text for hint in REPLY_HINTS)


def parse_saved_mail(mail_id: int) -> None:
    """解析已入库邮件。失败会留下 failed 状态，下一轮可重试，最多 3 次。"""
    row = get_mail(mail_id)
    if not row or row["parse_status"] == "ok":
        return
    if int(row["parse_retry"] or 0) >= MAX_PARSE_RETRY:
        logger.warning("邮件 id=%s 已达到解析重试上限", mail_id)
        return

    bump_parse_retry(mail_id)
    try:
        parsed = parse_email(row["subject"], row["sender"], row["content"], row.get("received_at") or "")
        raw_json = json.dumps(parsed, ensure_ascii=False)
        status = "ok"
    except DeepSeekUnreachable:
        bump_parse_retry(mail_id, -1)
        raise
    except Exception as exc:
        update_mail_parse(mail_id, "", "", "", "", "", "failed", str(exc)[:2000])
        raise

    task = None
    task_fields = ("customer_name", "execute_time", "scope_rule", "output_format", "next_action")
    if parsed["type"] == "customer_task" and not any(parsed[field] for field in task_fields):
        logger.warning("模型标记为客户任务但字段全空，按普通邮件保存 id=%s", mail_id)
        parsed["type"] = "normal_mail"
        raw_json = json.dumps(parsed, ensure_ascii=False)

    if parsed.get("usefulness") == "spam":
        parsed["type"] = "normal_mail"
        raw_json = json.dumps(parsed, ensure_ascii=False)

    if parsed["type"] == "customer_task":
        execute_at = normalize_execute_time(parsed["execute_time"], row.get("received_at") or "")
        reply_text = " ".join(
            [
                parsed.get("next_action") or "",
                parsed.get("todo_items") or "",
                parsed.get("scope_rule") or "",
                parsed.get("output_format") or "",
            ]
        )
        sender = row["sender_email"]
        wants_reply = reply_mode() != "never" and _looks_like_email(sender) and (
            reply_mode() == "always" or asks_for_reply(reply_text)
        )
        task = insert_customer_task(
            customer_name=parsed["customer_name"],
            execute_time=parsed["execute_time"],
            execute_at=execute_at,
            scope_rule=parsed["scope_rule"],
            output_format=parsed["output_format"],
            next_action=parsed["next_action"],
            source_mail_id=mail_id,
            reply_to=sender if wants_reply else "",
            reply_subject=_reply_subject(row["subject"]) if wants_reply else "",
            status=_status_for_time(execute_at, _run_now(parsed.get("execute_time") or "")),
        )
        if execute_at and not parsed.get("deadline"):
            parsed["deadline"] = execute_at
        if not parsed.get("urgency"):
            parsed["urgency"] = _importance(execute_at, row.get("received_at") or "", parsed.get("output_format") or "")
        if not parsed.get("todo_items"):
            parsed["todo_items"] = _customer_todo(parsed, execute_at, wants_reply)

    update_mail_parse(
        mail_id,
        parsed["type"],
        parsed["todo_items"],
        parsed["urgency"],
        parsed["deadline"],
        parsed.get("usefulness") or "ordinary",
        status,
        raw_json,
        parsed.get("judge_note") or "",
    )
    if task:
        if task.get("status") == "draft":
            logger.info("回复草稿已可发送 id=%s 截止=%s", task["id"], task["execute_at"] or "未识别")
            notify_reply_ready(task)
        else:
            logger.info("新客户任务 id=%s 计划执行=%s", task["id"], task["execute_at"] or "未识别")
            notify_new_task(task, row["subject"])
    elif parsed.get("usefulness") == "useful":
        moment = reminder_execute_at(
            row["subject"],
            row["content"],
            parsed.get("todo_items", ""),
            row["received_at"],
            parsed.get("deadline") or "",
        )
        action = (parsed.get("todo_items") or "").strip()
        if action:
            status = _status_for_time(moment)
            sender = row["sender_email"]
            can_reply = _looks_like_email(sender)
            task = insert_customer_task(
                customer_name=action[:40],
                execute_time=moment,
                execute_at=moment,
                scope_rule=action[:200],
                output_format="reminder",
                next_action=_reminder_note(moment, status),
                source_mail_id=mail_id,
                reply_to=sender if can_reply else "",
                reply_subject=_reply_subject(row["subject"]) if can_reply else "",
                status=status,
            )
            if task:
                repair_mail_importance()
            if task and status == "pending":
                logger.info("当天事项已排入任务 id=%s 时间=%s", task["id"], moment)
                notify_reminder(task)
            elif task and not moment:
                logger.info("待办已放入待安排 id=%s", task["id"])
            elif task:
                logger.info("当天事项时间已过，等待确认 id=%s 时间=%s", task["id"], moment)
    elif parsed["type"] != "customer_task":
        logger.info("普通邮件已解析 id=%s 待办=%s", mail_id, parsed["todo_items"] or "无")


def ingest_mail(mail: dict, history: bool = False) -> tuple[int, bool]:
    mail_id, created = insert_mail(
        message_id=mail["message_id"],
        subject=mail["subject"],
        sender=mail["sender"],
        sender_email=mail["sender_email"],
        content=mail["content"],
        received_at=mail["received_at"],
        mailbox_id=int(mail.get("mailbox_id") or 0),
        account=str(mail.get("account") or ""),
        imap_uid=str(mail.get("uid") or ""),
        history=history,
    )
    if created:
        logger.info("邮件已入库 id=%s 主题=%s", mail_id, mail["subject"])
        try:
            saved = save_mail_files(mail_id, mail.get("files") or [])
            if saved:
                logger.info("邮件附件已保存 id=%s %s 个", mail_id, saved)
        except Exception:
            logger.exception("邮件附件没有存下来 id=%s", mail_id)
    else:
        logger.info("邮件已存在，跳过重复入库 id=%s", mail_id)
    return mail_id, created


def backfill_mail_files(days: int = 7) -> int:
    """最近几天的信回邮箱只读一遍附件，补存图片，并把识别出的文字接到正文后面。"""
    from db_init import append_mail_ocr, mails_needing_files
    from mail_client import attachment_ocr, keep_file, mail_attachments

    since = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    rows = mails_needing_files(since)
    if not rows:
        return 0
    accounts = {int(a.get("id") or 0): a for a in mailbox_accounts()}
    total = 0
    for row in rows:
        if should_stop():
            break
        account = accounts.get(int(row["mailbox_id"] or 0))
        if not account:
            continue
        try:
            found = mail_attachments(str(row["imap_uid"]), account)
        except Exception:
            logger.warning("补读附件失败 id=%s，下次启动再试", row["id"])
            continue
        current = get_mail(int(row["id"])) or {}
        if found and "附件OCR识别开始" not in (current.get("content") or ""):
            append_mail_ocr(int(row["id"]), attachment_ocr(found))
        files = [{"name": n, "type": k, "data": d} for n, k, d in found if keep_file(k, d)]
        total += save_mail_files(int(row["id"]), files)
    if total:
        logger.info("最近 %s 天的信已补存附件 %s 个", days, total)
    return total


def _poll_entry() -> None:
    once = current_rule().get("poll_mode") == "once"
    try:
        job_poll_mail()
    finally:
        if once:
            rule = current_rule()
            rule["poll_mode"] = "default"
            rule["poll_minutes"] = 0
            rule["poll_time"] = ""
            rule["summary"] = describe_rule(rule)
            save_operator_rule(rule)
            reschedule_from_rule(immediate=False)


_history_lock = threading.Lock()


def _pull_history(account: dict, failures: dict) -> int:
    """新接入的邮箱先收最近 40 封（含已读），时间用邮箱里原来的时间。只做一次。"""
    global _scan_note
    from db_init import mark_history_done

    box_id = int(account.get("id") or 0)
    if not box_id or should_stop():
        return 0
    with _history_lock:
        fresh = next((a for a in mailbox_accounts() if int(a.get("id") or 0) == box_id), None)
        if not fresh or fresh.get("history_done"):
            return 0
        label = str(account.get("address") or "")
        _scan_note = f"正在收 {label} 最近的邮件" if label else "正在收新邮箱最近的邮件"
        try:
            older = fetch_recent_mails(limit=40, account=account, failures=failures)
        except Exception as exc:
            logger.exception("新邮箱首次收信失败，下一轮再试")
            notify_error("新邮箱首次收信", exc)
            if _scan_note.startswith("正在"):
                _scan_note = "首次收信中断，下一轮会继续"
            return 0
        stored = 0
        for mail in older:
            if should_stop():
                return 0
            try:
                ingest_mail(mail, history=True)
                stored += 1
            except Exception as exc:
                uid = str(mail.get("uid") or "")
                failures[uid] = failures.get(uid, 0) + 1
                logger.exception("邮件入库失败 uid=%s", uid)
                notify_error("邮件入库", exc)
        if older and not stored:
            logger.warning("新邮箱首次收信一封都没存进去，下一轮再试")
            return 0
        mark_history_done(box_id)
        logger.info("新邮箱 id=%s 首次收信入库 %s 封，未改邮箱未读状态", box_id, stored)
        return stored


def scan_new_mailboxes() -> None:
    """刚保存的邮箱马上收一次最近邮件并解析，不等下一轮拉取。暂停拉取时也照做，只这一次。"""
    global _scan_note
    pulled = 0
    for account in mailbox_accounts():
        if should_stop():
            return
        if account.get("history_done"):
            continue
        failures = _fetch_failures.setdefault(int(account.get("id") or 0), {})
        pulled += _pull_history(account, failures)
    if pulled and not should_stop():
        parsed = _parse_backlog()
        _scan_note = f"新邮箱已收 {pulled} 封" + (f"，并解析 {parsed} 封" if parsed else "")


def first_scan_soon() -> None:
    """调度器已经在跑时，排一次新邮箱首次收信。还没启动的，启动后的第一轮拉取会先做。"""
    if _scheduler is None or should_stop():
        return
    if not any(not a.get("history_done") for a in mailbox_accounts()):
        return
    _scheduler.add_job(
        lambda: _guard("新邮箱首次收信", scan_new_mailboxes),
        "date",
        run_date=datetime.now(CST),
        id="first_scan",
        max_instances=1,
        misfire_grace_time=120,
        replace_existing=True,
    )


_poll_lock = threading.Lock()
_seen_lock = threading.Lock()


def settle_seen() -> None:
    """识别完成的新信，同一邮箱一次标已读。首次回填的信不碰。"""
    if not _seen_lock.acquire(blocking=False):
        return
    try:
        rows = mails_ready_for_seen()
        if not rows:
            return
        accounts = {int(item.get("id") or 0): item for item in mailbox_accounts()}
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            grouped.setdefault(int(row["mailbox_id"]), []).append(row)
        for mailbox_id, items in grouped.items():
            account = accounts.get(mailbox_id)
            if not account:
                continue
            uids = [str(item["imap_uid"]) for item in items if item.get("imap_uid")]
            try:
                mark_seen(uids, account)
            except Exception as exc:
                logger.exception("标记已读失败")
                notify_error("标记已读", exc)
                continue
            mark_seen_done([int(item["id"]) for item in items])
    finally:
        _seen_lock.release()


def scan_now() -> str:
    """页面上的「立即扫描」：马上同时看所有邮箱，之后的节奏不变。暂停拉取时也只收这一次。"""
    global _scan_note
    if _scheduler is None or should_stop():
        return "程序还没开始收信。请先接入邮箱。"
    if not mailbox_accounts():
        return "还没有接入邮箱。"
    if _poll_lock.locked():
        return "正在收信，这一轮收完就能看到。"
    _scan_note = "正在连接邮箱"
    threading.Thread(target=lambda: _guard("立即扫描", job_poll_mail), name="scan-now", daemon=True).start()
    return "正在同时查看所有邮箱的未读。"


def job_poll_mail() -> None:
    """同一时间只收一轮。收完就放开，识别在后面进行，不挡住下一次扫描。"""
    global _scan_note
    if not _poll_lock.acquire(blocking=False):
        logger.info("上一轮收信还没结束，这次跳过")
        return
    pulled = 0
    try:
        pulled = _poll_once()
    finally:
        _poll_lock.release()
    if should_stop():
        return
    parsed = _parse_backlog()
    settle_seen()
    if should_stop():
        return
    if parsed:
        if pulled:
            _scan_note = f"未读已扫完，本次 {pulled} 封，并解析 {parsed} 封"
        else:
            _scan_note = f"没有新的未读邮件，并解析 {parsed} 封"


def _scan_account(account: dict, batch: int) -> int:
    """一个邮箱自己收自己的。新邮箱先收最近 40 封，再扫未读。"""
    label = str(account.get("address") or "")
    failures = _fetch_failures.setdefault(int(account.get("id") or 0), {})
    pulled = 0
    ceiling = 400
    if not account.get("history_done"):
        pulled += _pull_history(account, failures)
    rounds = 0
    while pulled < ceiling and not should_stop() and rounds < 40:
        rounds += 1
        try:
            mails = fetch_unseen_mails(
                limit=batch,
                account=account,
                failures=failures,
                skip_uids=stored_uids(int(account.get("id") or 0)),
            )
        except Exception as exc:
            logger.exception("拉取未读失败")
            notify_error("拉取未读", exc)
            break
        if not mails:
            break
        fresh = 0
        for mail in mails:
            if should_stop():
                break
            try:
                mail_id, created = ingest_mail(mail)
                if created and not mail_from_history(mail_id):
                    fresh += 1
            except Exception as exc:
                uid = str(mail.get("uid") or "")
                failures[uid] = failures.get(uid, 0) + 1
                logger.exception("邮件入库失败 uid=%s", uid)
                notify_error("邮件入库", exc)
        if not fresh:
            logger.warning("本批没有新邮件，先停下，下一轮再试 %s", label)
            break
        pulled += fresh
        if len(mails) < batch or should_stop():
            break
    return pulled


def _poll_once() -> int:
    """所有邮箱同时看未读。返回这次新入库的封数。"""
    global _scan_note
    from operator_rule import active_fetch_limit

    batch = active_fetch_limit()
    accounts = mailbox_accounts()
    _scan_note = "正在连接邮箱"
    pulled = 0
    try:
        workers = max(1, min(4, len(accounts)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_scan_account, account, batch) for account in accounts]
            for future in as_completed(futures):
                pulled += future.result()
        if pulled:
            logger.info("未读已扫完，本次 %s 封", pulled)
            _scan_note = f"未读已扫完，本次 {pulled} 封"
        else:
            _scan_note = "没有新的未读邮件"
    finally:
        if should_stop():
            _scan_note = ""
    return pulled


def _parse_one(mail_id: int) -> bool:
    with _parsing_lock:
        if mail_id in _parsing:
            return False
        _parsing.add(mail_id)
    try:
        parse_saved_mail(mail_id)
        return True
    except DeepSeekUnreachable:
        if not _link_down.is_set():
            logger.warning("连不上 DeepSeek，这一轮先不解析，下一轮再试")
        _link_down.set()
    except Exception as exc:
        logger.exception("邮件解析失败 id=%s", mail_id)
        notify_error("邮件解析", exc)
    finally:
        with _parsing_lock:
            _parsing.discard(mail_id)
    return False


UPLOAD_KINDS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
}
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
_upload_lock = threading.Lock()


def upload_kind(filename: str) -> str:
    return UPLOAD_KINDS.get(Path(filename or "").suffix.lower(), "")


def ingest_upload(filename: str, data: bytes) -> None:
    """同时选了几份文件时一份一份识别，本地 OCR 不并发。"""
    with _upload_lock:
        _ingest_upload(filename, data)


def _ingest_upload(filename: str, data: bytes) -> None:
    """页面上传的扫描件：本地识别文字，归档成一条记录，有文字时再交给 DeepSeek 识别待办。"""
    global _scan_note
    import hashlib

    from db_init import UPLOAD_DIR, insert_mail
    from ocr_local import extract_attachment_text, invoice_brief

    name = Path(filename or "").name[:120] or "扫描件"
    suffix = Path(name).suffix.lower()
    content_type = UPLOAD_KINDS.get(suffix, "")
    if not content_type or not data or len(data) > MAX_UPLOAD_BYTES:
        return
    digest = hashlib.sha1(data).hexdigest()
    stored = f"{digest}{suffix}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / stored
    if not target.exists():
        target.write_bytes(data)

    _scan_note = f"正在识别 {name}"
    try:
        text = extract_attachment_text(name, content_type, data)
    except Exception:
        logger.exception("扫描件识别失败: %s", name)
        text = ""
    brief = invoice_brief(text) if text else ""
    content = "\n\n".join(part for part in (brief, text) if part) or "（没有识别出文字）"
    if len(content) > 80000:
        content = content[:80000] + "\n...(正文过长已截断)"
    mail_id, created = insert_mail(
        message_id=f"upload:{digest}",
        subject=name,
        sender="本机上传",
        sender_email="",
        content=content,
        received_at=now_str(),
        source="upload",
        file_name=stored,
    )
    if not created:
        logger.info("扫描件已经上传过 id=%s", mail_id)
        _scan_note = ""
        return
    logger.info("扫描件已归档 id=%s 识别 %s 字", mail_id, len(text))
    if not text:
        update_mail_parse(mail_id, "normal_mail", "", "", "", "ordinary", "ok", "", "没有识别出文字，原文件已归档")
        _scan_note = ""
        return
    _scan_note = f"正在识别 {name} 里的待办"
    _parse_one(mail_id)
    _scan_note = ""


def retry_parse(mail_id: int) -> bool:
    """页面上点「重新识别」：清掉失败次数，马上在后台再识别一次。"""
    if not reset_mail_parse(mail_id):
        return False
    threading.Thread(target=_reparse_then_seen, args=(mail_id,), daemon=True, name=f"reparse-{mail_id}").start()
    return True


def _reparse_then_seen(mail_id: int) -> None:
    if _parse_one(mail_id):
        settle_seen()


def _parse_backlog() -> int:
    """解析完一批再看。还有待解析就继续，同时处理 3 封，不再干等 5 分钟。"""
    global _scan_note
    attempted: set[int] = set()
    done = 0
    _link_down.clear()
    with ThreadPoolExecutor(max_workers=3) as pool:
        while not should_stop() and not _link_down.is_set():
            rows = [
                row
                for row in list_mails_need_parse(limit=6 + len(attempted))
                if row["id"] not in attempted
            ][:6]
            if not rows:
                break
            for row in rows:
                attempted.add(row["id"])
            _scan_note = f"正在解析，已完成 {done} 封"
            futures = [pool.submit(_parse_one, row["id"]) for row in rows]
            for future in as_completed(futures):
                if future.result():
                    done += 1
                _scan_note = f"正在解析，已完成 {done} 封"
    return done


_TOPIC_STOP = {
    "邮件", "汇总", "所有", "全部", "最近", "过去", "今天", "今日", "当天", "本周", "这周", "本月", "这个月",
    "天", "日", "周", "星期", "礼拜", "个月", "月", "年", "的", "了", "并", "以及", "和", "与", "或",
    "在", "把", "将", "按", "对", "中", "内", "请", "要", "需要", "完成", "生成", "输出", "回复",
    "客户", "范围", "关于", "本次", "一下", "进行", "整理", "清单", "通知", "内部", "来信",
}


def _topic_terms(text: str) -> list[str]:
    """从客户写的范围里留下能用来对信的词。时间词和「邮件、汇总」不算。"""
    raw = re.sub(r"(?:近|最近|过去|前)\s*[0-9一两二三四五六七八九十]{1,4}\s*(?:个)?(?:天|日|周|星期|礼拜|个月|月|年)", " ", text or "")
    parts = re.split(r"[^\u4e00-\u9fffA-Za-z0-9]+", raw)
    terms: list[str] = []
    for part in parts:
        piece = part.strip()
        for stop in sorted(_TOPIC_STOP, key=len, reverse=True):
            piece = piece.replace(stop, " ")
        for word in piece.split():
            if len(word) >= 2 and word not in _TOPIC_STOP and word not in terms:
                terms.append(word)
    return terms[:8]


def _mail_hits(mail: dict, terms: list[str]) -> bool:
    blob = " ".join(
        [
            str(mail.get("subject") or ""),
            str(mail.get("todo_items") or ""),
            str(mail.get("content") or "")[:800],
        ]
    ).lower()
    return any(term.lower() in blob for term in terms)


def _summary_mails(task: dict, since: str, mailbox_id: int) -> tuple[list[dict], str]:
    """先按客户写的范围留下相关邮件，再最多取 30 封。对不上就不做成完整汇总。"""
    pool = query_recent_mails(since, limit=400, mailbox_id=mailbox_id)
    total = count_recent_mails(since, mailbox_id)
    terms = _topic_terms(" ".join([task.get("scope_rule") or "", task.get("customer_name") or ""]))
    scope = "这个邮箱" if mailbox_id else "已入库邮件"
    if terms:
        matched = [mail for mail in pool if _mail_hits(mail, terms)]
        label = "、".join(terms[:4])
        if not matched:
            return [], f"{scope}在这段时间里没有对上「{label}」的邮件，本次未生成业务汇总。"
        used = matched[:30]
        if len(matched) > len(used):
            note = f"{scope}在范围内有 {total} 封非垃圾邮件，对上「{label}」的有 {len(matched)} 封，本次看了最近 {len(used)} 封。"
        else:
            note = f"{scope}在范围内有 {total} 封非垃圾邮件，对上「{label}」的有 {len(matched)} 封，本次全部看过。"
        if total > len(pool):
            note += f"查找时先看了最近 {len(pool)} 封。"
        return list(reversed(used)), note
    used = pool[:30]
    if not used:
        return [], ""
    if total > len(used):
        note = f"{scope}在范围内有 {total} 封非垃圾邮件，本次只看了最近 {len(used)} 封。"
    else:
        note = ""
    return list(reversed(used)), note


def _execute_task(task: dict) -> None:
    if (task.get("output_format") or "") == "reminder":
        text = (task.get("scope_rule") or task.get("customer_name") or "当天事项").strip()
        report = f"到点提醒：{text}\n\n{DISCLAIMER}"
        finish_task(task["id"], report, "")
        notify_reminder_due(task)
        logger.info("当天事项已提醒 id=%s", task["id"])
        return

    asked_days = requested_lookback_days(
        " ".join([task.get("scope_rule") or "", task.get("customer_name") or "", task.get("next_action") or ""])
    )
    lookback_days = asked_days or _int_env("SUMMARY_LOOKBACK_DAYS", 30, 1, 3650)
    since = (datetime.now(CST) - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    source = get_mail(int(task.get("source_mail_id") or 0)) if task.get("source_mail_id") else None
    mailbox_id = int((source or {}).get("mailbox_id") or 0)
    mails, scope_note = _summary_mails(task, since, mailbox_id)
    if not mails:
        report = scope_note or f"最近 {lookback_days} 天没有检索到历史邮件，本次未生成业务汇总。"
        report = f"{report}\n\n{DISCLAIMER}"
    else:
        report = generate_summary(task, mails)
        if scope_note:
            report = f"{scope_note}\n\n{report}"
        if asked_days:
            report = f"按来信要求，只汇总最近 {lookback_days} 天（{since[:16]} 之后）的邮件。\n\n{report}"

    reply_text = " ".join(
        [
            task.get("next_action") or "",
            task.get("scope_rule") or "",
            task.get("output_format") or "",
        ]
    )
    reply_error = ""
    if _should_reply(reply_text, task.get("reply_to", "")):
        save_task_draft(task["id"], report)
        reply_note = "已写成草稿，尚未发送。请在页面上确认后再发。"
        notify_report_done(task, report, reply_note)
        logger.info("客户任务已写成草稿 id=%s", task["id"])
        return
    elif reply_mode() == "never":
        reply_note = "未准备回信（已保存的规则是不发信）"
    else:
        reply_note = "未准备回信（客户没有要求发信）"

    if len(report) > 20000:
        report = report[:20000] + "\n...(报告过长已截断)"
    stored = f"{report}\n\n发信情况：{reply_note}"
    finish_task(task["id"], stored, reply_error)
    notify_report_done(task, report, reply_note)
    logger.info("客户任务完成 id=%s %s", task["id"], reply_note)


def promote_pending_replies() -> int:
    """不再把待执行任务改成没有正文的草稿。汇总生成后才有可发送的内容。"""
    return 0


def _remind_unsent_replies() -> None:
    moment = now_str()
    for task in list_unsent_due(moment):
        mark_unsent_due(task["id"])
        notify_reply_late(task)
        logger.info("到点未发送已提醒 id=%s", task["id"])


def job_run_customer_tasks() -> None:
    _remind_unsent_replies()
    due = list_due_tasks(now_str(), limit=5)
    if not due:
        return
    logger.info("到点的客户汇总任务 %s 条", len(due))
    for task in due:
        if not claim_task(task["id"]):
            continue
        try:
            _execute_task(task)
        except Exception as exc:
            logger.exception("客户汇总失败 id=%s", task.get("id"))
            fail_task(task["id"], str(exc))
            notify_error("客户汇总", exc)
            logger.info("任务 id=%s 已标记 failed。确认修复后，可把 status 改回 pending 再试。", task.get("id"))


def _fallback_daily(day: str, mails: list[dict], tasks: list[dict]) -> str:
    lines = [f"【邮智汇每日工作总结】", f"日期：{day}", "", "今日邮件："]
    if not mails:
        lines.append("- 无")
    for mail in mails:
        lines.append(
            f"- {mail.get('received_at', '')} {mail.get('subject', '')} "
            f"紧急:{mail.get('urgency') or '-'} 待办:{mail.get('todo_items') or '-'}"
        )
    lines.append("")
    lines.append("客户汇总任务：")
    if not tasks:
        lines.append("- 无")
    for task in tasks:
        lines.append(
            f"- {task.get('customer_name') or '未识别'} 状态:{task.get('status') or '-'} "
            f"计划:{task.get('execute_at') or task.get('execute_time') or '-'}"
        )
    lines.append("")
    lines.append("AI 总结生成失败，以上为原始清单。")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _subject_lines(mails: list[dict], limit: int = 40) -> list[str]:
    lines = []
    for mail in mails[:limit]:
        subject = mail.get("subject") or "(无主题)"
        if mail.get("usefulness") == "spam":
            subject += "（垃圾，未进入汇总）"
        lines.append(f"- {mail.get('received_at', '')} {subject}")
    if len(mails) > limit:
        lines.append(f"- 其余 {len(mails) - limit} 封未列出")
    return lines


def _daily_extra(today: str, spam: list[dict]) -> str:
    """垃圾主题和今天补进来的旧信单独列出，不混进今日收信。"""
    imported_count = count_imported_older(today)
    imported = query_imported_older(today, limit=40) if imported_count else []
    lines = []
    if spam:
        lines.append(f"已排除垃圾 {len(spam)} 封，不进入上面的总结：")
        lines.extend(_subject_lines(spam))
    if imported_count:
        lines.append(f"今天新入库、日期在今天之前的邮件 {imported_count} 封，不计入今日收信：")
        lines.extend(_subject_lines(imported))
        if imported_count > len(imported):
            lines.append(f"- 其余 {imported_count - len(imported)} 封未列出")
    if not lines:
        return ""
    return "\n\n" + "\n".join(lines)


def job_daily_report() -> None:
    today = datetime.now(CST).strftime("%Y-%m-%d")
    start = f"{today} 00:00:00"
    end = f"{today} 23:59:59"
    all_mails = query_mails_between(start, end)
    mails = [mail for mail in all_mails if mail.get("usefulness") != "spam"]
    spam = [mail for mail in all_mails if mail.get("usefulness") == "spam"]
    tasks = query_tasks_between(start, end)
    logger.info("开始生成每日总结，邮件 %s 封，已排除垃圾 %s 封，任务 %s 条", len(mails), len(spam), len(tasks))
    if not mails and not tasks:
        if spam:
            report = (
                f"【邮智汇每日工作总结】\n日期：{today}\n"
                f"今日 {len(spam)} 封已标为垃圾，没有需要处理的邮件，也没有新的客户汇总任务。\n"
                f"待办清单：无\n\n{DISCLAIMER}"
            )
        else:
            report = f"【邮智汇每日工作总结】\n日期：{today}\n今日没有新邮件，也没有新的客户汇总任务。\n待办清单：无\n\n{DISCLAIMER}"
    else:
        try:
            report = generate_daily_report(today, mails, tasks)
        except Exception as exc:
            logger.exception("每日总结生成失败，改为推送原始清单")
            notify_error("每日总结", exc)
            report = _fallback_daily(today, mails, tasks)
    report += _daily_extra(today, spam)
    notify_daily(report)


def _clock(text: str) -> tuple[int, int]:
    matched = re.fullmatch(r"(\d{2}):(\d{2})", text or "")
    if not matched:
        return 9, 0
    return int(matched.group(1)), int(matched.group(2))


def _once_time(text: str):
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text.strip(), pattern).replace(tzinfo=CST)
        except ValueError:
            continue
    return None


def _add_poll_job(minutes: int, first_run) -> None:
    """只拉一次的任务跑完后，调度器会把它删掉。这里把常规拉取再装回去。"""
    extra = {"next_run_time": first_run} if first_run is not None else {}
    _scheduler.add_job(
        lambda: _guard("邮箱轮询", _poll_entry),
        "interval",
        minutes=minutes,
        id="poll_mail",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        replace_existing=True,
        **extra,
    )


def _keep_poll_job() -> None:
    if _scheduler is not None and _scheduler.get_job("poll_mail") is None:
        logger.warning("拉取任务不见了，按当前规则装回去")
        reschedule_from_rule(immediate=False)


def reschedule_from_rule(immediate: bool = True) -> None:
    """按已保存的规则改拉取和每日总结。调度器还没启动时只把规则留在数据库里。"""
    if _scheduler is None:
        return
    rule = current_rule()
    poll_mode = rule.get("poll_mode") or "default"
    if poll_mode == "once":
        run_at = _once_time(rule.get("poll_time") or "")
        if run_at is None or run_at <= datetime.now(CST):
            logger.info("只拉一次的时间已过，改回默认拉取")
            rule["poll_mode"] = "default"
            rule["poll_minutes"] = 0
            rule["poll_time"] = ""
            rule["summary"] = describe_rule(rule)
            save_operator_rule(rule)
            poll_mode = "default"
    if _scheduler.get_job("poll_mail") is None:
        _add_poll_job(_int_env("MAIL_POLL_MINUTES", 5, 1, 24 * 60), datetime.now(CST) if immediate else None)
    try:
        _scheduler.resume_job("poll_mail")
    except Exception:
        logger.exception("恢复拉取任务失败")

    if poll_mode == "paused":
        _scheduler.pause_job("poll_mail")
    elif poll_mode == "daily":
        hour, minute = _clock(rule.get("poll_time") or "09:00")
        _scheduler.reschedule_job(
            "poll_mail",
            trigger=CronTrigger(hour=hour, minute=minute, timezone=CST),
        )
    elif poll_mode == "once":
        _scheduler.reschedule_job("poll_mail", trigger="date", run_date=_once_time(rule["poll_time"]))
    else:
        minutes = int(rule.get("poll_minutes") or 0) if poll_mode == "interval" else _int_env("MAIL_POLL_MINUTES", 5, 1, 24 * 60)
        _scheduler.reschedule_job("poll_mail", trigger="interval", minutes=minutes)
        if immediate:
            _scheduler.modify_job("poll_mail", next_run_time=datetime.now(CST))

    if rule.get("report_mode") == "daily" and rule.get("report_time"):
        hour, minute = _clock(rule["report_time"])
    else:
        hour = _int_env("DAILY_REPORT_HOUR", 21, 0, 23)
        minute = _int_env("DAILY_REPORT_MINUTE", 0, 0, 59)
    if _scheduler.get_job("daily_report") is not None:
        _scheduler.reschedule_job(
            "daily_report",
            trigger=CronTrigger(hour=hour, minute=minute, timezone=CST),
        )
    logger.info("当前规则：%s", rule.get("summary") or "默认")
    if is_default_rule(rule):
        logger.info("拉取和发送没有单独保存过，按默认规则执行")


def _guard(name: str, func) -> None:
    try:
        func()
    except Exception as exc:
        logger.exception("%s失败", name)
        notify_error(name, exc)


def start_scheduler() -> None:
    global _scheduler
    init_db()
    recovered = reset_running_tasks()
    if recovered:
        logger.info("已恢复 %s 条中断的汇总任务", recovered)
    promote_pending_replies()
    repair_open_reminders()
    backfill_todo_tasks()
    repair_mail_importance()

    poll_minutes = _int_env("MAIL_POLL_MINUTES", 5, 1, 24 * 60)
    scan_seconds = _int_env("TASK_SCAN_SECONDS", 60, 15, 24 * 60 * 60)
    daily_hour = _int_env("DAILY_REPORT_HOUR", 21, 0, 23)
    daily_minute = _int_env("DAILY_REPORT_MINUTE", 0, 0, 59)

    scheduler = BackgroundScheduler(timezone=CST)
    _scheduler = scheduler
    scheduler.add_job(
        lambda: _guard("邮箱轮询", _poll_entry),
        "interval",
        minutes=poll_minutes,
        id="poll_mail",
        next_run_time=datetime.now(CST),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: (_guard("拉取任务检查", _keep_poll_job), _guard("客户汇总", job_run_customer_tasks)),
        "interval",
        seconds=scan_seconds,
        id="customer_task",
        next_run_time=datetime.now(CST) + timedelta(seconds=10),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _guard("每日总结", job_daily_report),
        CronTrigger(hour=daily_hour, minute=daily_minute, timezone=CST),
        id="daily_report",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 30,
        replace_existing=True,
    )
    reschedule_from_rule(immediate=True)
    scheduler.start()
    _log_startup(poll_minutes, scan_seconds, daily_hour, daily_minute)
    threading.Thread(
        target=lambda: _guard("补存附件", backfill_mail_files),
        name="mail-files",
        daemon=True,
    ).start()
    try:
        while not _stop.wait(1):
            pass
    except (KeyboardInterrupt, SystemExit):
        request_stop()
    logger.info("正在停止调度器")
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception:
        logger.exception("停止调度器时出错")


def _log_startup(poll_minutes: int, scan_seconds: int, daily_hour: int, daily_minute: int) -> None:
    def flag(name: str) -> str:
        return "已配置" if os.getenv(name, "").strip() else "未配置"

    logger.info(
        "邮智汇已启动 | 邮箱轮询每 %s 分钟 | 汇总扫描每 %s 秒 | 每日总结 %02d:%02d",
        poll_minutes,
        scan_seconds,
        daily_hour,
        daily_minute,
    )
    logger.info(
        "配置检查 | 邮箱账号%s | 授权码%s | DeepSeek%s | 模型%s | 钉钉%s",
        flag("MAIL_USERNAME"),
        flag("MAIL_PASSWORD"),
        flag("DEEPSEEK_API_KEY"),
        os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
        flag("DINGTALK_WEBHOOK"),
    )
    logger.info("按 Ctrl+C 停止")
