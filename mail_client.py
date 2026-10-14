"""IMAP 收信与 SMTP 发信。

读取未读邮件时使用 BODY.PEEK，避免还没入库就被服务器标成已读。
图片和 PDF 附件会交给本地 OCR；普通文本邮件不经过 OCR。
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
import re
import smtplib
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime, parseaddr
from html import unescape
from pathlib import Path

from dotenv import load_dotenv

from ocr_local import extract_attachment_text, invoice_brief

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger("youzhihui.mail")
CST = timezone(timedelta(hours=8))
MAX_CONTENT_CHARS = 80000


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _clean(text: str) -> str:
    """去掉空字符和解不开的半个字符。后者会让 SQLite 存不进去。"""
    value = (text or "").replace("\x00", "")
    value = value.encode("utf-8", errors="replace").decode("utf-8")
    return value.strip()


def _has_surrogate(text: str) -> bool:
    return any("\ud800" <= ch <= "\udfff" for ch in text)


def _decode_mime(value: str) -> str:
    chunks = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            chunks.append(_decode_bytes(part, charset))
        else:
            chunks.append(str(part))
    return _clean("".join(chunks))


def _header(message, name: str) -> str:
    value = message.get(name)
    if value is None:
        return ""
    text = str(value)
    if _has_surrogate(text) or "=?" in text or "\ufffd" in text:
        raw = next((str(item) for key, item in message.raw_items() if key.lower() == name.lower()), "")
        if raw and raw.isascii() and "=?" in raw:
            return _decode_mime(" ".join(raw.split()))
    return _clean(text)


def _decode_bytes(payload: bytes, charset: str | None) -> str:
    for encoding in (charset, "utf-8", "gb18030"):
        if not encoding:
            continue
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _clean(text)


def _part_text(part) -> str:
    content = ""
    try:
        value = part.get_content()
        if isinstance(value, str):
            content = value
            if "\ufffd" not in value:
                return value
    except Exception:
        pass
    payload = part.get_payload(decode=True) or b""
    if not isinstance(payload, (bytes, bytearray)):
        return content
    decoded = _decode_bytes(bytes(payload), part.get_content_charset())
    if content and decoded.count("\ufffd") >= content.count("\ufffd"):
        return content
    return decoded


def _part_bytes(part) -> bytes:
    try:
        value = part.get_content()
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode(part.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        pass
    payload = part.get_payload(decode=True) or b""
    return bytes(payload) if isinstance(payload, (bytes, bytearray)) else b""


def _append_ocr(content: str, filename: str, text: str) -> str:
    body = text.strip()
    brief = invoice_brief(body)
    if brief:
        body = f"{brief}\n{body}"
    block = (
        "\n\n----- 附件OCR识别开始（仅供辅助，需人工复核）-----\n"
        f"【附件】{filename}\n"
        f"{body}\n"
        "----- 附件OCR识别结束 -----"
    )
    return content + block


def _account_from(account: dict | None) -> tuple[str, str, str, int]:
    if account:
        username = str(account.get("address") or "").strip()
        password = str(account.get("password") or "").strip()
        host = str(account.get("imap_host") or "").strip()
        try:
            port = int(account.get("imap_port") or 993)
        except (TypeError, ValueError):
            port = 993
    else:
        username = os.getenv("MAIL_USERNAME", "").strip()
        password = os.getenv("MAIL_PASSWORD", "").strip()
        host = os.getenv("MAIL_IMAP_HOST", "").strip()
        port = _int_env("MAIL_IMAP_PORT", 993)
    if not username or not password:
        raise RuntimeError("未配置邮箱或授权码")
    if not host:
        raise RuntimeError("还没有识别出邮箱服务器，请先在页面保存邮箱")
    return username, password, host, port


@contextmanager
def _imap_session(account: dict | None = None):
    username, password, host, port = _account_from(account)
    client = imaplib.IMAP4_SSL(host, port, timeout=30)
    try:
        client.login(username, password)
        status, _ = client.select("INBOX")
        if status != "OK":
            raise RuntimeError(f"打开收件箱失败: {status}")
        yield client
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _rfc822(fetched) -> bytes:
    if not fetched:
        return b""
    for part in fetched:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
            return bytes(part[1])
    return b""


def _internal_date(fetched) -> str:
    for part in fetched or []:
        meta = part[0] if isinstance(part, tuple) else part
        if not isinstance(meta, (bytes, bytearray)):
            continue
        stamp = imaplib.Internaldate2tuple(bytes(meta))
        if stamp:
            return datetime.fromtimestamp(time.mktime(stamp), CST).strftime("%Y-%m-%d %H:%M:%S")
    return ""


def _received_at(message, fallback: str = "") -> str:
    """邮件自带的 Date 优先；有些通知信没有 Date，就用邮箱服务器记的收信时间。"""
    try:
        raw = message.get("Date")
    except Exception:
        raw = None
    if raw:
        try:
            moment = parsedate_to_datetime(str(raw))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=CST)
            return moment.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            logger.warning("邮件时间无法解析，改用服务器收信时间")
    if fallback:
        return fallback
    logger.warning("邮件没有时间，改用当前时间")
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _build_mail(uid: str, raw: bytes, internal_date: str = "") -> dict:
    message = email.message_from_bytes(raw, policy=policy.default)
    subject = _header(message, "Subject") or "(无主题)"
    name, address = parseaddr(message.get("From") or "")
    name = _decode_mime(name) if "=?" in name else _clean(name)
    address = _clean(address)
    sender = f"{name} <{address}>" if name and address else (address or name or "(未知发件人)")
    message_id = _header(message, "Message-ID") or f"imap-uid-{uid}"

    plain_parts = []
    html_parts = []
    attachments = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if filename and "=?" in filename:
            filename = _decode_mime(filename)
        filename = _clean(filename or "")
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        is_text = content_type in {"text/plain", "text/html"}
        looks_like_file = (
            disposition == "attachment"
            or (filename and not is_text)
            or content_type.startswith("image/")
            or content_type == "application/pdf"
        )
        if looks_like_file:
            data = _part_bytes(part)
            if data:
                attachments.append((filename or _default_name(content_type), content_type, data))
            if not is_text:
                continue
        if content_type == "text/plain":
            plain_parts.append(_part_text(part))
        elif content_type == "text/html":
            html_parts.append(_part_text(part))

    content = _clean("\n".join(part for part in plain_parts if part.strip()))
    if not content:
        content = _clean("\n".join(_html_to_text(part) for part in html_parts if part.strip()))

    for filename, content_type, data in attachments:
        text = extract_attachment_text(filename, content_type, data)
        if not text:
            continue
        logger.info("附件 OCR 完成: %s，识别 %s 字", filename, len(text))
        content = _append_ocr(content, filename, text)

    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "\n...(正文过长已截断)"

    return {
        "uid": uid,
        "message_id": message_id[:500],
        "subject": subject[:500],
        "sender": sender[:300],
        "sender_email": address[:200],
        "content": content,
        "received_at": _received_at(message, internal_date),
    }


def _default_name(content_type: str) -> str:
    if content_type == "application/pdf":
        return "attachment.pdf"
    if content_type == "image/png":
        return "attachment.png"
    return "attachment.jpg"


SKIP_AFTER = 3


def _search_uids(client, criterion: str) -> list[str]:
    status, data = client.uid("SEARCH", None, criterion)
    if status != "OK" or not data or not data[0]:
        return []
    return [
        uid.decode("ascii", errors="ignore") if isinstance(uid, bytes) else str(uid)
        for uid in data[0].split()
        if uid
    ]


def _fetch_selected(
    criterion: str,
    limit: int,
    account: dict | None,
    failures: dict,
    newest: bool,
    empty_note: str,
    found_note: str,
) -> list[dict]:
    """先按条件选出邮件，断开连接后再解析正文。"""
    raw_mails = []
    with _imap_session(account) as client:
        all_uids = _search_uids(client, criterion)
        if not all_uids:
            logger.info(empty_note)
            return []
        pending = [uid for uid in all_uids if failures.get(uid, 0) < SKIP_AFTER]
        uids = (pending[-limit:] if newest else pending[:limit])
        logger.info(found_note, len(all_uids), len(uids))
        for uid_text in uids:
            try:
                status, fetched = client.uid("FETCH", uid_text, "(INTERNALDATE BODY.PEEK[])")
                if status != "OK":
                    logger.error("读取邮件失败 uid=%s status=%s", uid_text, status)
                    failures[uid_text] = failures.get(uid_text, 0) + 1
                    continue
                raw = _rfc822(fetched)
                if not raw:
                    logger.error("邮件正文为空 uid=%s", uid_text)
                    failures[uid_text] = failures.get(uid_text, 0) + 1
                    continue
                raw_mails.append((uid_text, raw, _internal_date(fetched)))
            except Exception:
                logger.exception("读取邮件失败 uid=%s，本封跳过", uid_text)
                failures[uid_text] = failures.get(uid_text, 0) + 1

    mails = []
    for uid_text, raw, internal_date in raw_mails:
        try:
            mails.append(_build_mail(uid_text, raw, internal_date))
        except Exception:
            logger.exception("解析邮件失败 uid=%s，本封跳过", uid_text)
            failures[uid_text] = failures.get(uid_text, 0) + 1
    mailbox_id = int(account.get("id") or 0) if account else 0
    address = str(account.get("address") or "") if account else ""
    for mail in mails:
        mail["mailbox_id"] = mailbox_id
        mail["account"] = address
    return mails


def fetch_unseen_mails(
    limit: int | None = None,
    account: dict | None = None,
    failures: dict | None = None,
) -> list[dict]:
    """抓取未读邮件。先断开 IMAP，再做 OCR，避免识别图片时邮箱连接超时。

    failures 记着这次运行中每封未读读不出来或存不进去的次数。连错 3 次的先跳过，
    后面的新邮件才不会被同一封堵住。程序重启后会重新尝试。
    """
    if limit is None:
        limit = max(1, min(_int_env("MAIL_FETCH_LIMIT", 40), 80))
    if failures is None:
        failures = {}
    return _fetch_selected(
        "UNSEEN",
        max(1, limit),
        account,
        failures,
        False,
        "收件箱没有未读邮件",
        "收件箱未读 %s 封，本轮处理 %s 封",
    )


def fetch_recent_mails(
    limit: int = 40,
    account: dict | None = None,
    failures: dict | None = None,
) -> list[dict]:
    """补拉最近邮件，包含已经读过的。只在这个邮箱还没有入库记录时用。"""
    if failures is None:
        failures = {}
    return _fetch_selected(
        "ALL",
        max(1, min(limit, 40)),
        account,
        failures,
        True,
        "收件箱是空的",
        "收件箱共 %s 封，补拉最近 %s 封",
    )


def mark_seen(uids: list[str], account: dict | None = None) -> None:
    """处理成功后再标记已读。"""
    if not uids:
        return
    with _imap_session(account) as client:
        for uid in uids:
            status, _ = client.uid("STORE", uid, "+FLAGS", r"(\Seen)")
            if status != "OK":
                logger.error("标记已读失败 uid=%s status=%s", uid, status)
    logger.info("已标记已读 %s 封", len(uids))


def send_mail(to_addr: str, subject: str, body: str, account: dict | None = None) -> None:
    """用指定邮箱的 SMTP 发送纯文本邮件。没指定时用当前保存的第一个邮箱。"""
    if account:
        username = str(account.get("address") or "").strip()
        password = str(account.get("password") or "").strip()
        host = str(account.get("smtp_host") or "").strip()
        try:
            port = int(account.get("smtp_port") or 465)
        except (TypeError, ValueError):
            port = 465
        mode = str(account.get("smtp_mode") or "ssl").strip().lower()
    else:
        username = os.getenv("MAIL_USERNAME", "").strip()
        password = os.getenv("MAIL_PASSWORD", "").strip()
        host = os.getenv("MAIL_SMTP_HOST", "").strip()
        port = _int_env("MAIL_SMTP_PORT", 465)
        mode = os.getenv("MAIL_SMTP_MODE", "ssl").strip().lower()
    if not username or not password:
        raise RuntimeError("未配置邮箱或授权码")
    if not host:
        raise RuntimeError("还没有识别出发信服务器")
    if not to_addr:
        raise RuntimeError("收件人邮箱为空")
    message = MIMEText(body or "", "plain", "utf-8")
    message["Subject"] = subject or "邮件汇总"
    message["From"] = username
    message["To"] = to_addr
    if mode == "starttls" or port == 587:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(username, password)
            smtp.sendmail(username, [to_addr], message.as_string())
    else:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.sendmail(username, [to_addr], message.as_string())
    logger.info("SMTP 已发送至 %s", to_addr)
