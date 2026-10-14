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
    history_uids,
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


def reminder_execute_at(subject: str, content: str, todo: str, received_at: str) -> str:
    """当天来信里写了几点要做事，就排到那天的这个钟点。没有钟点则不排。"""
    text = " ".join(part for part in (todo, subject, (content or "")[:240]) if part)
    text = text.replace("：", ":")
    clock = re.search(r"(?<!\d)(\d{1,2})\s*[:点时]\s*(\d{1,2})?(?!\d)", text)
    if not clock:
        return ""
    hour = int(clock.group(1))
    minute = int(clock.group(2) or 0)
    if minute > 59:
        return ""
    hour = _apply_day_period(text, hour)
    if hour > 23:
        return ""
    today = datetime.now(CST).replace(tzinfo=None).date()
    anchor = _anchor_date(received_at) or today
    day_mark = re.search(r"(?<!\d)(\d{1,2})\s*号", text)
    if day_mark:
        number = int(day_mark.group(1))
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
            row["subject"], row["content"], parsed.get("todo_items", ""), row["received_at"]
        )
        action = (parsed.get("todo_items") or "").strip()
        if moment and action:
            status = _status_for_time(moment)
            task = insert_customer_task(
                customer_name=action[:40],
                execute_time=moment,
                execute_at=moment,
                scope_rule=action[:200],
                output_format="reminder",
                next_action="时间已过，请确认是否还要执行" if status == "need_time" else "到点提醒，不自动发信",
                source_mail_id=mail_id,
                reply_to="",
                reply_subject="",
                status=status,
            )
            if task and status == "pending":
                logger.info("当天事项已排入任务 id=%s 时间=%s", task["id"], moment)
                notify_reminder(task)
            elif task:
                logger.info("当天事项时间已过，等待确认 id=%s 时间=%s", task["id"], moment)
    elif parsed["type"] != "customer_task":
        logger.info("普通邮件已解析 id=%s 待办=%s", mail_id, parsed["todo_items"] or "无")


def ingest_mail(mail: dict, history: bool = False) -> int:
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
    else:
        logger.info("邮件已存在，跳过重复入库 id=%s", mail_id)
    return mail_id


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


def job_poll_mail() -> None:
    """每个已接入的邮箱各扫各的。新邮箱先收最近 40 封，再扫未读；扫完一批再看，还有未读才扫下一批。"""
    global _scan_note
    from db_init import mailbox_accounts
    from operator_rule import active_fetch_limit

    batch = active_fetch_limit()
    accounts = mailbox_accounts()
    pulled = 0
    batches = 0
    ceiling = 400
    try:
        for account in accounts:
            if pulled >= ceiling or should_stop():
                break
            label = str(account.get("address") or "")
            failures = _fetch_failures.setdefault(int(account.get("id") or 0), {})
            if not account.get("history_done"):
                got = _pull_history(account, failures)
                if got:
                    batches += 1
                    pulled += got
            rounds = 0
            while pulled < ceiling and not should_stop() and rounds < 40:
                rounds += 1
                try:
                    mails = fetch_unseen_mails(
                        limit=batch,
                        account=account,
                        failures=failures,
                        skip_uids=history_uids(int(account.get("id") or 0)),
                    )
                except Exception as exc:
                    logger.exception("拉取未读失败")
                    notify_error("拉取未读", exc)
                    if _scan_note.startswith("正在"):
                        _scan_note = "拉取中断，下一轮会继续"
                    break
                if not mails:
                    break
                batches += 1
                _scan_note = f"正在扫 {label} 第 {batches} 批" if label else f"正在扫第 {batches} 批"
                seen_uids = []
                for mail in mails:
                    if should_stop():
                        break
                    try:
                        mail_id = ingest_mail(mail)
                        if not mail_from_history(mail_id):
                            seen_uids.append(mail["uid"])
                    except Exception as exc:
                        uid = str(mail.get("uid") or "")
                        failures[uid] = failures.get(uid, 0) + 1
                        logger.exception("邮件入库失败 uid=%s", uid)
                        notify_error("邮件入库", exc)
                if not seen_uids:
                    logger.warning("本批没有入库，先停下，下一轮再试")
                    break
                try:
                    mark_seen(seen_uids, account)
                except Exception as exc:
                    logger.exception("标记已读失败")
                    notify_error("标记已读", exc)
                    break
                pulled += len(seen_uids)
                if len(mails) < batch or should_stop():
                    break
                _scan_note = f"{label} 这一批已扫完，还有未读，开始下一批" if label else "这一批已扫完，还有未读，开始下一批"
        if pulled:
            logger.info("扫完 %s 批，共 %s 封，每批 %s 封", batches, pulled, batch)
            _scan_note = f"未读已扫完，本次 {batches} 批，共 {pulled} 封"
        elif _scan_note.startswith("正在"):
            _scan_note = ""
    finally:
        if should_stop():
            _scan_note = ""

    if should_stop():
        return
    parsed = _parse_backlog()
    if parsed and not should_stop():
        if pulled:
            _scan_note = f"未读已扫完，并解析 {parsed} 封"
        else:
            _scan_note = f"已解析 {parsed} 封"


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


def retry_parse(mail_id: int) -> bool:
    """页面上点「重新识别」：清掉失败次数，马上在后台再识别一次。"""
    if not reset_mail_parse(mail_id):
        return False
    threading.Thread(target=_parse_one, args=(mail_id,), daemon=True, name=f"reparse-{mail_id}").start()
    return True


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


def _execute_task(task: dict) -> None:
    if (task.get("output_format") or "") == "reminder":
        text = (task.get("scope_rule") or task.get("customer_name") or "当天事项").strip()
        report = f"到点提醒：{text}\n\n{DISCLAIMER}"
        finish_task(task["id"], report, "")
        notify_reminder_due(task)
        logger.info("当天事项已提醒 id=%s", task["id"])
        return

    lookback_days = _int_env("SUMMARY_LOOKBACK_DAYS", 30, 1, 3650)
    since = (datetime.now(CST) - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    source = get_mail(int(task.get("source_mail_id") or 0)) if task.get("source_mail_id") else None
    mailbox_id = int((source or {}).get("mailbox_id") or 0)
    mails = list(reversed(query_recent_mails(since, limit=30, mailbox_id=mailbox_id)))
    total = count_recent_mails(since, mailbox_id)
    if not mails:
        report = (
            f"最近 {lookback_days} 天没有检索到历史邮件，本次未生成业务汇总。\n\n{DISCLAIMER}"
        )
    else:
        report = generate_summary(task, mails)
        if total > len(mails):
            scope = "这个邮箱" if mailbox_id else "已入库邮件"
            report = f"{scope}在范围内有 {total} 封非垃圾邮件，本次只看了最近 {len(mails)} 封。\n\n{report}"

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
