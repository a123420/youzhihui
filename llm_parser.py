"""DeepSeek 调用。

通过 OpenAI SDK 连接 https://api.deepseek.com ，模型名来自 DEEPSEEK_MODEL，默认 deepseek-flash。
邮件分类要求严格 JSON。汇总和日报是中文文本，末尾固定附上人工复核提示。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import getproxies

from dotenv import load_dotenv
from openai import OpenAI
import httpx

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger("youzhihui.llm")

DISCLAIMER = "【提示】以上内容由AI辅助生成，请人工复核后再发送或执行。"

# 实训指定的系统提示词，保持原文，不在这里追加别的指令。
SYSTEM_PARSE = """你是邮件分析助手，读取邮件内容，输出严格JSON，不要多余解释。
区分两类邮件：
1.普通业务邮件：提取待办事项、紧急等级【高/中/低】、截止时间。
2.客户汇总指令邮件：提取：客户名称、execute_time(客户要求汇总执行时间)、scope_rule(整合范围)、output_format(输出格式)、next_action(完成后续操作)。
如果是客户汇总任务，type="customer_task"；普通邮件type="normal_mail"。
字段缺失填空字符串，输出纯JSON，不要markdown、不要额外文字。"""

SYSTEM_SUMMARY = """你是邮件汇总助手。请严格根据提供的历史邮件，按客户给出的整合范围和输出格式生成汇总报告。
不要编造邮件里不存在的事实、数字、承诺或结论。范围之外的邮件不要写进报告。
正文里标注了“附件OCR识别”的段落来自本地识别，可能有错字，只能谨慎引用。
信息不足时写明“未在邮件中找到”。不要声称邮件已经发出，后续动作由程序另行执行。
报告最后单独一行写：【提示】以上内容由AI辅助生成，请人工复核后再发送或执行。"""

SYSTEM_DAILY = """你是工作总结助手。根据今天收到的邮件和客户汇总任务，写一份简洁的全天工作总结，并列出待办清单。
不要编造没有出现的事项。待办要尽量保留紧急等级和截止时间。
正文里标注了“附件OCR识别”的内容可能有识别误差。
最后单独一行写：【提示】以上内容由AI辅助生成，请人工复核后再发送或执行。"""

JSON_FIELDS = (
    "type",
    "todo_items",
    "urgency",
    "deadline",
    "customer_name",
    "execute_time",
    "scope_rule",
    "output_format",
    "next_action",
)

_client: OpenAI | None = None


class DeepSeekUnreachable(RuntimeError):
    """网络或代理没通。不是这封邮件的问题，不该算进解析重试次数。"""


def _normalize_proxy(value: str) -> str:
    """本机混合端口只接受 HTTP 代理。Windows 经常把它记成 https://。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").strip("[]").lower()
    if parsed.scheme == "https" and host in {"127.0.0.1", "localhost", "::1"}:
        return "http://" + raw.split("://", 1)[1]
    return raw


def _proxy_url() -> str:
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        found = _normalize_proxy(os.getenv(name, ""))
        if found:
            return found
    try:
        found = getproxies()
    except Exception:
        return ""
    return _normalize_proxy(found.get("https") or found.get("http") or "")


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
        if not api_key:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY")
        timeout = httpx.Timeout(40.0, connect=8.0)
        proxy = _proxy_url()
        http_client = httpx.Client(proxy=proxy or None, timeout=timeout, trust_env=False)
        _client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )
    return _client


def _link_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(word in text for word in ("connect", "connection", "proxy", "ssl", "timeout", "timed out"))


def _message_text(response) -> str:
    message = response.choices[0].message
    content = message.content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                chunks.append(str(part.get("text") or ""))
            else:
                chunks.append(str(getattr(part, "text", "") or ""))
        content = "".join(chunks)
    if content and str(content).strip():
        return str(content)
    reasoning = getattr(message, "reasoning_content", None)
    return str(reasoning or "")


def _chat(messages: list[dict], *, json_mode: bool, max_tokens: int, temperature: float) -> str:
    """关闭思考模式，让结果直接出现在 content 里。参数不被接受时自动降级重试。"""
    global _client
    client = _get_client()
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip() or "deepseek-flash"
    use_json = json_mode
    disable_thinking = True
    last_error: Exception | None = None

    for attempt in range(4):
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if use_json:
            kwargs["response_format"] = {"type": "json_object"}
        if disable_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        try:
            response = client.chat.completions.create(**kwargs)
            text = _message_text(response).strip()
            if text:
                return text
            last_error = RuntimeError("DeepSeek 返回空内容")
            logger.warning("DeepSeek 返回空内容，准备重试（第 %s 次）", attempt + 1)
        except Exception as exc:
            last_error = exc
            error_text = str(exc).lower()
            if use_json and ("response_format" in error_text or "json_object" in error_text):
                use_json = False
                logger.warning("接口不接受 JSON 模式，改为普通文本后重试")
                continue
            if disable_thinking and "thinking" in error_text:
                disable_thinking = False
                logger.warning("接口不接受 thinking 参数，去掉后重试")
                continue
            if _link_error(exc):
                logger.warning("DeepSeek 连不上: %s", type(exc).__name__)
                _client = None
                raise DeepSeekUnreachable("连不上 DeepSeek。请检查网络或代理设置，再问一次。") from exc
            logger.warning("DeepSeek 调用失败，准备重试: %s", type(exc).__name__)
        if attempt == 3:
            break
    raise RuntimeError(f"DeepSeek 调用失败: {last_error}")


def _load_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        matched = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not matched:
            raise
        data = json.loads(matched.group(0))
    if not isinstance(data, dict):
        raise ValueError("模型返回的 JSON 不是对象")
    return data


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "；".join(_as_text(item) for item in value if _as_text(item))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


SPAM_HINTS = (
    "退订",
    "unsubscribe",
    "点击退订",
    "【广告】",
    "促销",
    "营销邮件",
    "中奖",
    "博彩",
    "代开发票",
    "发票代开",
    "免费领取",
    "限时抢购",
)
AUTO_SENDERS = ("newsletter", "mailer-daemon", "edm@", "marketing@")


def obvious_spam(subject: str, sender: str, content: str) -> bool:
    """广告和自动营销信不必再调用模型。仅凭 noreply 不算垃圾，避免把登录和安全提醒标错。"""
    blob = f"{subject or ''}\n{(content or '')[:800]}"
    lowered = blob.lower()
    if any(hint.lower() in lowered for hint in SPAM_HINTS):
        return True
    sender_text = (sender or "").lower()
    return any(token in sender_text for token in AUTO_SENDERS)


def _usefulness_text(value) -> str:
    text = _as_text(value).lower()
    if text in {"useful", "spam", "ordinary"}:
        return text
    if any(word in text for word in ("垃圾", "广告", "spam")):
        return "spam"
    if any(word in text for word in ("有用", "重要", "useful")):
        return "useful"
    if "普通" in text:
        return "ordinary"
    return ""


def permission_todo(subject: str, content: str) -> str:
    """权限变更需要本人点进去确认，不能当成看过即可的通知。"""
    blob = f"{subject or ''}\n{content or ''}".lower()
    markers = (
        "requesting updated permissions",
        "requesting access",
        "requesting permission",
        "review the permissions",
        "权限变更",
        "更新权限",
        "申请授权",
        "请求授权",
    )
    if any(marker in blob for marker in markers):
        return "打开邮件里的设置链接，确认或拒绝这次权限变更"
    return ""


def attendance_todo(subject: str, content: str) -> str:
    """座位和参会码要本人去看，不是看过即可的通知。"""
    blob = f"{subject or ''}\n{content or ''}"
    markers = ("座位已安排", "已为您安排座位", "安排座位", "参会二维码", "参会码")
    if any(marker in blob for marker in markers):
        return "查看座位安排，并打开邮件里的个人中心保存参会二维码"
    return ""


def local_judge_note(subject: str, content: str, usefulness: str, todo: str) -> str:
    """没有模型原因时，用一句中文说明分类。不摘录正文。"""
    if permission_todo(subject, content):
        return "需要本人确认或拒绝这次权限变更"
    if attendance_todo(subject, content):
        return "座位已安排，需要本人查看并保存参会码"
    blob = f"{subject or ''}\n{(content or '')[:800]}"
    if usefulness == "spam":
        return "广告或营销，不进入汇总"
    if usefulness == "useful":
        if "截止" in blob or "到期" in blob or "过期" in blob:
            return "有截止时间，需要处理"
        if any(word in blob for word in ("发票", "价税合计", "账单")):
            return "账单或发票，需要核对"
        text = (todo or "").strip()
        return text[:30] if text else "需要本人处理"
    if any(word in blob for word in ("验证码", "verification code", "launch code")):
        return "验证码，本人使用即可"
    if any(word in blob.lower() for word in ("token", "signed in", "sign in", "登录成功", "安全提醒")):
        return "安全提醒，不必马上处理"
    if any(word in blob for word in ("发票", "价税合计")):
        return "发票已开出，按需要核对"
    return "通知，不必马上处理"


def _clean_reason(value) -> str:
    text = " ".join(_as_text(value).split())
    if not text or len(text) > 40 or "http" in text.lower():
        return ""
    return text


def judge_usefulness(model_value, parsed: dict, subject: str, sender: str, content: str) -> str:
    """模型给出的分类优先。没给出时，用待办和常见营销词兜底。"""
    guessed = _usefulness_text(model_value)
    if guessed:
        return guessed
    if obvious_spam(subject, sender, content):
        return "spam"
    task_fields = ("customer_name", "execute_time", "scope_rule", "output_format", "next_action")
    if parsed.get("type") == "customer_task" and any(parsed.get(field) for field in task_fields):
        return "useful"
    if parsed.get("todo_items") or parsed.get("deadline") or parsed.get("urgency") in {"高", "中"}:
        return "useful"
    return "ordinary"


def _blank_parse(usefulness: str) -> dict:
    parsed = {field: "" for field in JSON_FIELDS}
    parsed["type"] = "normal_mail"
    parsed["usefulness"] = usefulness
    parsed["judge_note"] = "广告或营销，不进入汇总" if usefulness == "spam" else ""
    return parsed


def _urgency(value) -> str:
    text = _as_text(value)
    for level in ("高", "中", "低"):
        if level in text:
            return level
    return {"high": "高", "medium": "中", "low": "低"}.get(text.lower(), "")


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...(已截断)"


def _with_disclaimer(text: str) -> str:
    text = (text or "").strip()
    if DISCLAIMER not in text:
        text = f"{text}\n\n{DISCLAIMER}" if text else DISCLAIMER
    return text


def parse_email(subject: str, sender: str, content: str, received_at: str = "") -> dict:
    """解析一封邮件，返回字段齐全的字典。明显的营销信不再调用模型。"""
    if obvious_spam(subject, sender, content):
        logger.info("明显垃圾邮件，跳过模型 主题=%s", subject or "-")
        return _blank_parse("spam")
    body = _clip(content, 8000)
    when = (received_at or "").strip()
    user_prompt = (
        "请只输出一个 JSON 对象，不要输出其他文字。字段必须齐全，缺失用空字符串。\n"
        "execute_time 若能确定，使用北京时间，格式 YYYY-MM-DD HH:MM:SS。\n"
        + (f"这封邮件的收到时间是北京时间 {when}。正文里的今天、明天、几号都按这个时间推算年和月。\n" if when else "")
        + 
        "urgency 只能是“高”“中”“低”或空字符串。\n"
        "type 只能是 customer_task 或 normal_mail。\n"
        "usefulness 只能是 useful、spam、ordinary。"
        "需要本人处理的账单、发票、截止事项或明确待办标 useful，并把要做的事写入 todo_items。"
        "需要本人去批准、拒绝或确认的权限变更、授权申请也标 useful，todo_items 写清要打开哪里、确认什么。"
        "座位安排、参会二维码也标 useful，todo_items 写明查看座位并保存参会码。"
        "当天来信写了几点要做什么，todo_items 写清这件事，type 仍用 normal_mail，不要当成客户汇总。"
        "验证码、已完成的登录通知，以及不必处理的安全提醒标 ordinary，不要标 spam。"
        "广告、促销、退订和无关营销标 spam。营销里的“紧急、限时”也标 spam，不要标 useful。"
        "通知但不必马上处理标 ordinary。拿不准时标 ordinary。\n"
        "另外给出 reason：一句中文，不超过 30 字，说明为什么是这一类。\n"
        "例子：请确认权限变更、座位已安排 → useful；令牌已添加或验证码 → ordinary；限时折扣、退订 → spam。\n\n"
        "JSON结构示例（只说明字段，不要照抄内容）：\n"
        '{"type":"normal_mail","todo_items":"","urgency":"","deadline":"",'
        '"customer_name":"","execute_time":"","scope_rule":"","output_format":"","next_action":"",'
        '"usefulness":"ordinary","reason":""}\n\n'
        f"发件人：{sender or ''}\n"
        f"主题：{subject or ''}\n"
        f"正文：\n{body}"
    )
    raw = _chat(
        [
            {"role": "system", "content": SYSTEM_PARSE},
            {"role": "user", "content": user_prompt},
        ],
        json_mode=True,
        max_tokens=1200,
        temperature=0.1,
    )
    data = _load_json(raw)
    mail_type = _as_text(data.get("type"))
    if mail_type not in {"normal_mail", "customer_task"}:
        mail_type = "customer_task" if ("客户" in mail_type and "汇总" in mail_type) else "normal_mail"
    parsed = {field: _as_text(data.get(field)) for field in JSON_FIELDS}
    parsed["type"] = mail_type
    parsed["urgency"] = _urgency(parsed["urgency"])
    parsed["usefulness"] = judge_usefulness(data.get("usefulness"), parsed, subject, sender, content)
    action = permission_todo(subject, content) or attendance_todo(subject, content)
    if action and parsed["usefulness"] != "spam":
        parsed["usefulness"] = "useful"
        if not parsed["todo_items"]:
            parsed["todo_items"] = action
        if not parsed["urgency"]:
            parsed["urgency"] = "中"
    if parsed["usefulness"] == "spam":
        parsed["type"] = "normal_mail"
    parsed["judge_note"] = _clean_reason(data.get("reason")) or local_judge_note(
        subject, content, parsed["usefulness"], parsed.get("todo_items", "")
    )
    logger.info(
        "邮件解析完成 type=%s usefulness=%s urgency=%s customer=%s",
        parsed["type"],
        parsed["usefulness"],
        parsed["urgency"] or "-",
        parsed["customer_name"] or "-",
    )
    return parsed


def _digest(mails: list[dict], per_mail: int = 700, total: int = 10000) -> str:
    parts = []
    used = 0
    for index, mail in enumerate(mails, start=1):
        body = _clip(mail.get("content") or "", per_mail)
        block = (
            f"[{index}] 时间：{mail.get('received_at', '')}\n"
            f"发件人：{mail.get('sender', '')}\n"
            f"主题：{mail.get('subject', '')}\n"
            f"待办：{mail.get('todo_items', '')}\n"
            f"紧急等级：{mail.get('urgency', '')}\n"
            f"截止时间：{mail.get('deadline', '')}\n"
            f"分类：{mail.get('usefulness') or ''}\n"
            f"正文：\n{body}"
        )
        if used + len(block) > total:
            parts.append("其余邮件因长度限制未送入模型。")
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def generate_summary(task: dict, mails: list[dict]) -> str:
    """按客户要求生成汇总报告。"""
    user_prompt = (
        f"客户名称：{task.get('customer_name') or '未提供'}\n"
        f"整合范围：{task.get('scope_rule') or '未提供，请只做客观摘录并说明范围不明确'}\n"
        f"输出格式：{task.get('output_format') or '条理清晰的中文条目'}\n"
        f"后续操作：{task.get('next_action') or '无'}\n\n"
        "历史邮件如下：\n"
        f"{_digest(mails)}"
    )
    text = _chat(
        [
            {"role": "system", "content": SYSTEM_SUMMARY},
            {"role": "user", "content": user_prompt},
        ],
        json_mode=False,
        max_tokens=2000,
        temperature=0.2,
    )
    logger.info("汇总报告已生成，长度 %s", len(text))
    return _with_disclaimer(text)


def interpret_schedule(history: list[dict], summary: str) -> dict:
    """把操作员的对话收成调度 JSON。没提到的项目必须保持 keep。"""
    system = (
        "你是邮智汇的调度助手。用户可能在问建议，也可能在要求添加或修改规则。\n"
        "如果用户是在问建议、问现在好不好、没有明确要求修改，所有 action 保持 keep，把建议写在 reply，不要改规则。\n"
        "reply 里说到现在的设置时，必须照下面给出的当前规则，不要把默认值说成正在执行的规则。\n"
        "只有用户明确要添加或修改时，才填写对应 action。reply 里的说法必须和 action 一致，不要说暂停却填 default。不要说已经保存。页面会再让用户确认。\n"
        "用户要马上审核、马上拉取、立刻收信或让新邮箱马上看一遍时，poll_action 填 now。这是现在收一次未读，之后的节奏不变。不要理解成等邮箱服务商审核，也不要因此改成 paused 或 default。\n"
        "当前规则是暂停时，用户说继续拉取、恢复拉取、开始拉取，poll_action 必须填 default。这时不要填 keep，也不要回复已经在拉取。\n"
        "更早的对话如果和当前规则不一致，以当前规则为准，不要跟着旧回复说。\n"
        "没有提到的项目保持不变，动作填 keep。用户明确说恢复默认时，把提到的项目设为 default。\n"
        f"当前规则：{summary}\n"
        f"默认拉取：每 {os.getenv('MAIL_POLL_MINUTES', '5')} 分钟。\n"
        "默认发信：客户要求回复时先写成草稿，在页面上确认后才发出。\n"
        f"默认总结：每天 {os.getenv('DAILY_REPORT_HOUR', '21')}:{str(os.getenv('DAILY_REPORT_MINUTE', '0')).zfill(2)} 推送全天总结。\n"
        "只输出一个 JSON 对象，不要其他文字。字段如下：\n"
        '{"reply":"一两句中文。问建议时只给建议；要改规则时说明将要改成什么，不要说已经保存","poll_action":"keep","poll_minutes":0,'
        '"poll_time":"","reply_action":"keep","report_action":"keep","report_time":""}\n'
        "poll_action 只能是 keep、default、interval、daily、paused、once、now。\n"
        "now 表示马上收一次，不修改之后的拉取节奏，poll_minutes 和 poll_time 留空。\n"
        "interval 时 poll_minutes 取 1 到 1440。\n"
        "daily 时 poll_time 用 24 小时制 HH:MM，表示每天这个时间拉取。\n"
        "once 时 poll_time 用 YYYY-MM-DD HH:MM，表示只在这个时间拉取一次，之后恢复默认拉取。\n"
        "paused 表示先不拉取。\n"
        "reply_action 只能是 keep、default、always、never。\n"
        "always 表示汇总完成后准备回复，确认后再发。never 表示不准备回信。default 表示客户要求回复时也只出草稿。\n"
        "report_action 只能是 keep、default、daily。daily 时 report_time 用 HH:MM，表示每天这个时间推送全天总结。\n"
        "单封邮件正文里写的执行时间不要在这里改。"
    )
    messages = [{"role": "system", "content": system}]
    for item in history[-8:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content[:2000]})
    raw = _chat(messages, json_mode=True, max_tokens=500, temperature=0.1)
    data = _load_json(raw)
    logger.info(
        "调度对话 poll=%s reply=%s report=%s",
        data.get("poll_action"),
        data.get("reply_action"),
        data.get("report_action"),
    )
    return data


def generate_daily_report(day: str, mails: list[dict], tasks: list[dict]) -> str:
    """生成 21 点日报。"""
    task_lines = []
    for task in tasks:
        report = _clip(task.get("report") or "", 300)
        task_lines.append(
            f"- 客户：{task.get('customer_name') or '未识别'}｜状态：{task.get('status') or ''}"
            f"｜计划：{task.get('execute_at') or task.get('execute_time') or '未识别'}"
            f"｜范围：{_clip(task.get('scope_rule') or '', 120)}"
            f"｜报告摘录：{report or '无'}"
        )
    user_prompt = (
        f"日期：{day}\n\n"
        "今日邮件：\n"
        f"{_digest(mails) if mails else '无'}\n\n"
        "今日客户汇总任务：\n"
        f"{chr(10).join(task_lines) if task_lines else '无'}\n\n"
        "请输出三部分：今日工作总结、待办清单、客户汇总任务进展。"
    )
    text = _chat(
        [
            {"role": "system", "content": SYSTEM_DAILY},
            {"role": "user", "content": user_prompt},
        ],
        json_mode=False,
        max_tokens=1800,
        temperature=0.2,
    )
    logger.info("每日总结已生成，长度 %s", len(text))
    return _with_disclaimer(text)
