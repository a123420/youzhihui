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
        poll = f"每 {int(rule['poll_minutes'])} 分钟拉未读"
    elif poll_mode == "daily" and rule.get("poll_time"):
        poll = f"每天 {rule['poll_time']} 拉未读"
    elif poll_mode == "once" and rule.get("poll_time"):
        poll = f"{rule['poll_time']} 拉一次"
    elif poll_mode == "paused":
        poll = "暂停拉取"
    else:
        poll = f"每 {default_poll_minutes()} 分钟拉未读"

    reply_mode = rule.get("reply_mode") or "default"
    if reply_mode == "always":
        reply = "汇总后准备回复"
    elif reply_mode == "never":
        reply = "不准备回信"
    else:
        reply = "回复先写草稿"

    if rule.get("report_mode") == "daily" and rule.get("report_time"):
        report = f"每天 {rule['report_time']} 总结"
    else:
        report = f"每天 {default_report_time()} 总结"
    return f"{poll}，每批 {active_fetch_limit(rule)} 封；{reply}；{report}"


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

    fetch_action = str(parsed.get("fetch_action") or "keep").strip().lower()
    if fetch_action == "default":
        rule["fetch_limit"] = 0
    elif fetch_action == "set":
        try:
            number = int(parsed.get("fetch_limit") or 0)
        except (TypeError, ValueError):
            number = 0
        if 5 <= number <= 40:
            rule["fetch_limit"] = number

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
_pending_fetch = False

_RULE_KEYS = ("poll_mode", "poll_minutes", "poll_time", "reply_mode", "report_mode", "report_time", "fetch_limit")


def _same_rule(left: dict, right: dict) -> bool:
    return all(left.get(key) == right.get(key) for key in _RULE_KEYS)


def pending_is_fetch() -> bool:
    """只有马上拉一次、规则本身不变时，确认按钮写成确认拉取。"""
    return bool(_pending_fetch) and (_pending_rule is None or _same_rule(_pending_rule, current_rule()))


def pending_summary() -> str:
    if pending_is_fetch():
        return "马上拉一次未读"
    if not _pending_rule:
        return ""
    summary = str(_pending_rule.get("summary") or "")
    if _pending_fetch:
        return summary + "，并马上拉一次"
    return summary


def _resume_requested(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return any(word in compact for word in ("继续拉取", "恢复拉取", "开始拉取", "继续收信", "恢复收信"))


def _spoken_hhmm(hour: str, minute: str | None) -> str:
    try:
        value_hour = int(hour)
        value_minute = int(minute or 0)
    except (TypeError, ValueError):
        return ""
    if value_hour > 23 or value_minute > 59:
        return ""
    return f"{value_hour:02d}:{value_minute:02d}"


_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM = r"(?:\d{1,4}|[零〇一二两三四五六七八九十]{1,3})"


def _cn_int(text: str) -> int:
    raw = (text or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    if any(char not in _CN_DIGIT and char != "十" for char in raw):
        return 0
    if raw == "十":
        return 10
    if "十" in raw:
        left, _, right = raw.partition("十")
        tens = _CN_DIGIT[left] if left else 1
        ones = _CN_DIGIT[right] if right else 0
        return tens * 10 + ones
    if len(raw) == 1 and raw in _CN_DIGIT:
        return _CN_DIGIT[raw]
    return 0


def _clock_from_words(period: str, hour_text: str, minute_text: str, half: bool) -> str:
    hour = _cn_int(hour_text)
    minute = 30 if half else _cn_int(minute_text)
    if hour > 23 or minute > 59:
        return ""
    if period in {"下午", "傍晚", "晚上", "夜里", "晚间"} and 1 <= hour <= 11:
        hour += 12
    elif period in {"晚上", "夜里", "晚间", "凌晨", "半夜"} and hour == 12:
        hour = 0
    elif period == "中午" and 1 <= hour <= 4:
        hour += 12
    if hour > 23:
        return ""
    return f"{hour:02d}:{minute:02d}"


def _find_clocks(compact: str) -> list[tuple[int, str]]:
    found = []
    pattern = re.compile(
        rf"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|晚间|半夜|清晨)?({_CN_NUM})(?:[:：]|点|时)(?:半|({_CN_NUM})分?)?"
    )
    for match in pattern.finditer(compact):
        clock = _clock_from_words(
            match.group(1) or "",
            match.group(2),
            match.group(3) or "",
            match.group(0).endswith("半"),
        )
        if clock:
            found.append((match.start(), clock))
    return found


def _clauses(compact: str) -> list[tuple[int, str]]:
    parts = []
    cursor = 0
    for match in re.finditer(r"并且|还有|同时|[，,。；;]|并", compact):
        parts.append((cursor, compact[cursor:match.start()]))
        cursor = match.end()
    parts.append((cursor, compact[cursor:]))
    return [(pos, part) for pos, part in parts if part]


def _keep_reply(text: str) -> dict:
    return {
        "poll_action": "keep",
        "poll_minutes": 0,
        "poll_time": "",
        "reply_action": "keep",
        "report_action": "keep",
        "report_time": "",
        "fetch_action": "keep",
        "fetch_limit": 0,
        "reply": text,
        "note": "",
    }


def _asks_current(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return any(word in compact for word in ("现在的规则", "当前规则", "现在什么规则", "规则是什么", "现在怎么收"))


def _asks_suggestion(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if re.search(r"每\d+分钟|每批\d+|暂停|马上拉取|每天\d+", compact):
        return False
    return any(word in compact for word in ("有什么建议", "什么建议", "怎么更好", "有什么好的"))


def _asks_capabilities(text: str) -> bool:
    """问能加什么规则时，本机直接回答，不必再等模型。"""
    compact = re.sub(r"\s+", "", text or "")
    if not any(
        word in compact
        for word in ("什么规则", "哪些规则", "能做什么", "可以做什么", "怎么添加", "还能添加", "如何添加", "有什么功能")
    ):
        return False
    return re.search(r"每\d+分钟|每批\d+|暂停|马上拉取|每天\d+", compact) is None


def _active_minutes(rule: dict) -> int:
    if (rule.get("poll_mode") or "") == "interval" and int(rule.get("poll_minutes") or 0) > 0:
        return int(rule["poll_minutes"])
    if (rule.get("poll_mode") or "") in {"paused", "daily", "once"}:
        return 0
    return default_poll_minutes()


def _asks_link(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if _looks_like_rule(text):
        return False
    return any(word in compact for word in ("连上了", "连不上", "连得上", "断了", "断线"))


def _small_talk(text: str) -> str:
    """打招呼和确认在不在，本机立刻回答，不等模型。"""
    compact = re.sub(r"[\s?？!！。，,~～]", "", text or "")
    if not compact or _looks_like_rule(text):
        return ""
    if compact in {"还在吗", "在吗", "在不在", "你在吗", "在么", "还在么", "你好吗"}:
        return "在。改规则直接说，其他问题也可以问。"
    if compact.lower() in {"你好", "您好", "嗨", "hi", "hello"}:
        return "在。可以直接说规则，也可以问别的。"
    if compact in {"你是谁", "你叫什么", "你是什么", "你哪位"}:
        return "我是邮智汇的调度助手。改规则会先让你确认，确认后才保存。"
    if compact in {"谢谢", "谢谢你", "感谢", "多谢"}:
        return "不客气。"
    if compact in {"再见", "拜拜"}:
        return "好。规则保持原样。"
    return ""


def _looks_like_rule(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return re.search(r"拉取|收信|提醒|总结|每批|分钟|小时|暂停|封", compact) is not None


_CAPABILITY_REPLY = (
    "可以直接说这些，确认后才会保存：\n"
    "每 10 分钟拉取。每批 15 封。\n"
    "暂停拉取、继续拉取，或马上拉取一次。\n"
    "每天 21:00 总结。\n"
    "客户要求回复才写草稿，或都不准备回信。"
)

def _direct_schedule(text: str, rule: dict) -> dict | None:
    """说法明确时直接定规则。一句话里的拉取、封数、回复和总结可以一起改。"""
    compact = re.sub(r"\s+", "", text or "")
    parsed = {
        "poll_action": "keep",
        "poll_minutes": 0,
        "poll_time": "",
        "reply_action": "keep",
        "report_action": "keep",
        "report_time": "",
        "fetch_action": "keep",
        "fetch_limit": 0,
        "reply": "",
        "note": "",
    }
    touched = False
    if any(word in compact for word in ("恢复默认规则", "全部恢复默认", "规则恢复默认")):
        return {
            **parsed,
            "poll_action": "default",
            "reply_action": "default",
            "report_action": "default",
            "fetch_action": "default",
        }
    if any(word in compact for word in ("不准备回信", "不要回信", "不用回信", "不回复", "只生成汇总", "只做汇总")):
        parsed["reply_action"] = "never"
        touched = True
    elif any(word in compact for word in ("都准备回复", "都要回复", "每封都回复", "汇总后都回复", "汇总完成后准备回复")):
        parsed["reply_action"] = "always"
        touched = True
    elif any(word in compact for word in ("客户要求回复", "要求回复才", "恢复默认回复")):
        parsed["reply_action"] = "default"
        touched = True

    batch = re.search(rf"每批({_CN_NUM})封", compact)
    if batch:
        number = _cn_int(batch.group(1))
        if 5 <= number <= 40:
            parsed["fetch_action"] = "set"
            parsed["fetch_limit"] = number
        else:
            parsed["note"] = "每批只能设在 5 到 40 封之间。"
        touched = True

    poll_pos = -1
    report_pos = -1

    def nearer(origin: int, points: list[int]) -> int:
        if not points:
            return 10**9
        return min(abs(origin - point) for point in points)

    def set_poll(pos: int, action: str, minutes: int = 0, when: str = "") -> None:
        nonlocal poll_pos, touched
        if pos < poll_pos:
            return
        poll_pos = pos
        parsed["poll_action"] = action
        parsed["poll_minutes"] = minutes
        parsed["poll_time"] = when
        touched = True

    def take_report(pos: int, clock: str) -> None:
        nonlocal report_pos, touched
        if not clock or pos < report_pos:
            return
        report_pos = pos
        parsed["report_action"] = "daily"
        parsed["report_time"] = clock
        touched = True

    def add_interval(pos: int, minutes: int) -> None:
        nonlocal touched
        valid = _minutes(minutes)
        if valid:
            set_poll(pos, "interval", minutes=valid)
        elif minutes:
            parsed["note"] = "拉取间隔要在 1 分钟到 1 天之间。"
            touched = True

    for offset, clause in _clauses(compact):
        report_words = [match.start() for match in re.finditer(r"提醒|通知|总结|汇报|日报|叫我", clause)]
        poll_words = [match.start() for match in re.finditer(r"拉取|收信|检查|审核", clause)]
        for match in re.finditer(r"每半个?小时", clause):
            add_interval(offset + match.start(), 30)
        for match in re.finditer(rf"每(?:隔)?({_CN_NUM})分钟", clause):
            add_interval(offset + match.start(), _cn_int(match.group(1)))
        for match in re.finditer(r"每一?分钟", clause):
            add_interval(offset + match.start(), 1)
        for match in re.finditer(rf"每(?:隔)?({_CN_NUM})个?小时", clause):
            add_interval(offset + match.start(), _cn_int(match.group(1)) * 60)
        for match in re.finditer(r"每一?个小时|每小时", clause):
            add_interval(offset + match.start(), 60)
        for match in re.finditer(rf"(?:间隔|每隔)(?:改成|调成|调到|设为)?({_CN_NUM})分钟", clause):
            add_interval(offset + match.start(), _cn_int(match.group(1)))
        for match in re.finditer(rf"(?:改成|调成|调到|设为)({_CN_NUM})分钟", clause):
            add_interval(offset + match.start(), _cn_int(match.group(1)))
        for match in re.finditer(rf"({_CN_NUM})分钟(?:一次|拉取|收信)", clause):
            add_interval(offset + match.start(), _cn_int(match.group(1)))
        for pos, clock in _find_clocks(clause):
            origin = offset + pos
            if report_words and nearer(pos, report_words) <= nearer(pos, poll_words):
                take_report(origin, clock)
            elif poll_words and parsed["poll_action"] in {"keep", "daily"}:
                set_poll(origin, "daily", when=clock)

    for match in re.finditer(r"每天(\d{1,2})(?:[:：点时](\d{1,2}))?(?:分)?(?:拉取|收信|检查)", compact):
        clock = _spoken_hhmm(match.group(1), match.group(2))
        if clock:
            set_poll(match.start(), "daily", when=clock)
    for match in re.finditer(
        r"(\d{4}-\d{2}-\d{2})(\d{1,2})[:：](\d{2}).{0,8}(?:拉取一次|只拉一次|收一次|只收一次)",
        compact,
    ):
        moment = _once_moment(f"{match.group(1)} {int(match.group(2)):02d}:{match.group(3)}")
        if moment:
            set_poll(match.start(), "once", when=moment)

    if any(word in compact for word in ("暂停拉取", "停止拉取", "先暂停", "先别拉")) and parsed["poll_action"] == "keep":
        if (rule.get("poll_mode") or "default") == "paused" and not touched:
            return {**parsed, "reply": "现在已经暂停拉取，不用再改。"}
        set_poll(0, "paused")
    if (
        any(word in compact for word in ("马上拉取", "马上审核", "马上收", "立刻收", "立即收"))
        and parsed["poll_action"] == "keep"
    ):
        parsed["poll_action"] = "now"
        touched = True
    if parsed["poll_action"] == "keep" and parsed["fetch_action"] == "keep" and re.search(
        r"优化规则|规则优化|帮我优化|优化一下规则", compact
    ):
        base = _active_minutes(rule) or default_poll_minutes()
        batch = active_fetch_limit(rule)
        if base < 10:
            add_interval(len(compact), 10)
        if batch > 15:
            parsed["fetch_action"] = "set"
            parsed["fetch_limit"] = 15
            touched = True
        if touched:
            parsed["note"] = "间隔放宽一些，每批少拉一点。"
        else:
            return {**parsed, "reply": "现在这样就可以：" + describe_rule(rule)}
    elif parsed["poll_action"] == "keep" and re.search(r"快一点|拉快|频繁一点|间隔短一点|勤一点", compact):
        base = _active_minutes(rule) or default_poll_minutes()
        add_interval(len(compact), max(1, base // 2))
    elif parsed["poll_action"] == "keep" and re.search(r"慢一点|拉慢|间隔长一点|别那么勤", compact):
        base = _active_minutes(rule) or default_poll_minutes()
        add_interval(len(compact), min(240, base * 2))
    if parsed["fetch_action"] == "keep" and re.search(r"少拉一点|少收一点|每批少", compact):
        parsed["fetch_action"] = "set"
        parsed["fetch_limit"] = max(5, active_fetch_limit(rule) - 5)
        touched = True
    elif parsed["fetch_action"] == "keep" and re.search(r"多拉一点|多收一点|每批多", compact):
        parsed["fetch_action"] = "set"
        parsed["fetch_limit"] = min(40, active_fetch_limit(rule) + 5)
        touched = True
    if _resume_requested(text) and parsed["poll_action"] == "keep" and "分钟" not in compact:
        if (rule.get("poll_mode") or "default") == "paused":
            set_poll(0, "default")
        elif not touched:
            return {**parsed, "reply": "现在已经在按这个节奏收取：" + describe_rule(rule)}
    if not touched:
        return None
    return parsed


class AdviceUnavailable(Exception):
    """问到了对话，但模型这会儿没有给出可用结果。规则保持原样。"""


def ask_for_advice(text: str) -> str:
    """问建议或草拟规则。规则先停在待确认，点确认后才保存。"""
    global _pending_rule, _pending_fetch
    message = (text or "").strip()
    if not message:
        raise ValueError("请先写一句建议或要添加的规则")
    if len(message) > 1000:
        raise ValueError("这句话太长了，请缩短到 1000 字以内")
    init_db()
    add_chat("user", message)
    before = current_rule()
    direct = _direct_schedule(message, before)
    if direct is None and _asks_capabilities(message):
        direct = _keep_reply(_CAPABILITY_REPLY)
    elif direct is None and _asks_current(message):
        direct = _keep_reply("现在是：" + before["summary"] + "。要改直接说，确认后才保存。")
    elif direct is None and _asks_suggestion(message):
        direct = _keep_reply(
            "现在是：" + before["summary"] + "。\n可以试「马上拉取」看新邮件，或「每批 15 封」少拉一点。确认后才保存。"
        )
    if direct is None and _asks_link(message):
        direct = _keep_reply("DeepSeek 这会儿还没有回应。改规则不用等它，直接说即可。")
    if direct is None:
        spoken = _small_talk(message)
        if spoken:
            direct = _keep_reply(spoken)
    if direct is None and _looks_like_rule(message):
        direct = _keep_reply("可以说得再具体一点，例如：每 1 分钟拉取，下午 5 点提醒。确认后才会保存。")
    if direct is None:
        return _answer_other(before["summary"])
    try:
        parsed = direct
        fetch_now = str(parsed.get("poll_action") or "").strip().lower() == "now"
        if (
            before.get("poll_mode") == "paused"
            and _resume_requested(message)
            and str(parsed.get("poll_action") or "").strip().lower() in {"", "keep", "paused"}
        ):
            parsed = dict(parsed)
            parsed["poll_action"] = "default"
            fetch_now = False
        if fetch_now:
            parsed = dict(parsed)
            parsed["poll_action"] = "keep"
        rule = merge_rule(before, parsed)
        reply = str(parsed.get("reply") or "").strip()
        changed = not _same_rule(before, rule)
        note = str(parsed.get("note") or "").strip()
        if not changed and not fetch_now:
            _pending_rule = None
            _pending_fetch = False
            reply = note or reply or "这是建议，当前规则没有改动。"
        else:
            _pending_rule = rule
            _pending_fetch = fetch_now
            lines = []
            if changed:
                lines.append("改成：" + rule["summary"])
            if fetch_now and not changed:
                lines.append("马上拉一次未读。")
            elif fetch_now:
                lines.append("并马上拉一次未读。")
            if note:
                lines.append(note)
            reply = "\n".join(line for line in lines if line) + "\n确认后才执行。"
    except Exception as exc:
        logger.warning("调度对话没有生成建议: %s", type(exc).__name__)
        _pending_rule = None
        _pending_fetch = False
        reply = "这句话没改成规则，仍按原来的。"
        add_chat("assistant", reply)
        raise AdviceUnavailable(reply) from exc
    add_chat("assistant", reply)
    return reply


def _answer_other(summary: str) -> str:
    """规则以外的问题交给 DeepSeek。答完也不改规则。"""
    global _pending_rule, _pending_fetch
    from llm_parser import DeepSeekUnreachable, answer_question

    _pending_rule = None
    _pending_fetch = False
    try:
        reply = answer_question(list_chat(8), summary).strip() or "这条没有生成，仍按原来的规则。"
    except DeepSeekUnreachable as exc:
        logger.warning("调度对话没有答上: %s", type(exc).__name__)
        reply = str(exc)
        add_chat("assistant", reply)
        raise AdviceUnavailable(reply) from exc
    except Exception as exc:
        logger.warning("调度对话没有答上: %s", type(exc).__name__)
        reply = "这条没有生成，仍按原来的规则。"
        add_chat("assistant", reply)
        raise AdviceUnavailable(reply) from exc
    add_chat("assistant", reply)
    return reply


def confirm_pending(accept: bool) -> str:
    """确认后才写入规则。取消则丢掉这次建议。"""
    global _pending_rule, _pending_fetch
    init_db()
    if not _pending_rule and not _pending_fetch:
        raise ValueError("没有待确认的规则")
    if not accept:
        _pending_rule = None
        _pending_fetch = False
        reply = "已取消，规则保持原样。"
        add_chat("assistant", reply)
        return reply
    rule = _pending_rule
    fetch_now = _pending_fetch
    _pending_rule = None
    _pending_fetch = False
    changed = bool(rule) and not _same_rule(current_rule(), rule)
    if changed:
        _apply_saved(rule)
    if fetch_now:
        import threading

        from scheduler_task import job_poll_mail

        threading.Thread(target=job_poll_mail, daemon=True, name="poll-once").start()
    if changed and fetch_now:
        reply = f"已保存，并开始拉一次未读。{rule['summary']}"
    elif fetch_now:
        reply = "已开始拉一次未读。节奏不变。"
    else:
        reply = f"已保存。{rule['summary']}"
    add_chat("assistant", reply)
    return reply


def clear_conversation() -> int:
    """清掉调度对话。待确认的建议一并丢掉，已经保存的规则不动。"""
    global _pending_rule, _pending_fetch
    init_db()
    _pending_rule = None
    _pending_fetch = False
    return clear_chat()


def restore_default() -> str:
    global _pending_rule, _pending_fetch
    init_db()
    _pending_rule = None
    _pending_fetch = False
    rule = blank_rule()
    rule["summary"] = describe_rule(rule)
    _apply_saved(rule)
    reply = f"已恢复默认规则。{rule['summary']}"
    add_chat("assistant", reply)
    return reply
