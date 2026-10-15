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
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime, parseaddr
from pathlib import Path

import html2text
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


def _prefer_body(plain: str, html: str) -> str:
    """纯文本很短、网页里还有表格或链接时，用网页正文。正文已经写清楚时不动。"""
    plain = (plain or "").strip()
    html = (html or "").strip()
    if not html:
        return plain
    if not plain:
        return html
    plain_size = len(re.sub(r"\s+", "", plain))
    html_size = len(re.sub(r"\s+", "", html))
    if plain_size < 40 and html_size > plain_size:
        return html
    return plain


def _html_to_text(html: str) -> str:
    """网页邮件收成一行行正文。表格和链接留下来，样式和图片丢掉。"""
    parser = html2text.HTML2Text()
    parser.body_width = 0
    parser.ignore_images = True
    parser.ignore_emphasis = True
    parser.unicode_snob = True
    parser.protect_links = True
    parser.single_line_break = True
    try:
        text = parser.handle(html or "")
    except Exception:
        logger.warning("网页邮件转正文失败，改用去掉标签")
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html or "")
        text = re.sub(r"(?s)<[^>]+>", " ", text)
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


_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"BM", "image/bmp", ".bmp"),
    (b"%PDF", "application/pdf", ".pdf"),
)
MAX_FILE_BYTES = 15 * 1024 * 1024


def _real_type(filename: str, content_type: str, data: bytes) -> tuple[str, str]:
    """邮件里贴的图常被标成 application/octet-stream、名字也没有扩展名，按文件头认出来。"""
    if "@" in (filename or "") or filename in {"attachment.jpg", "attachment.png"}:
        filename = "正文图片"
    head = data[:16]
    for magic, kind, suffix in _MAGIC:
        if head.startswith(magic):
            name = filename or f"附件{suffix}"
            if not name.lower().endswith(suffix) and not (suffix == ".jpg" and name.lower().endswith(".jpeg")):
                name = f"{name[:80]}{suffix}"
            return name, kind
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return (filename if filename.lower().endswith(".webp") else f"{filename or '附件'}.webp"), "image/webp"
    return filename or "附件", content_type or "application/octet-stream"


def keep_file(content_type: str, data: bytes) -> bool:
    """签名小图标不存。其余图片和文件都存下来，页面上能看到。"""
    if not data or len(data) > MAX_FILE_BYTES:
        return False
    if content_type.startswith("image/") and len(data) < 4096:
        return False
    return True


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
                name, real = _real_type(filename or _default_name(content_type), content_type, data)
                attachments.append((name, real, data))
            if not is_text:
                continue
        if content_type == "text/plain":
            plain_parts.append(_part_text(part))
        elif content_type == "text/html":
            html_parts.append(_part_text(part))

    plain = _clean("\n".join(part for part in plain_parts if part.strip()))
    html = _clean("\n".join(_html_to_text(part) for part in html_parts if part.strip()))
    content = _prefer_body(plain, html)

    content = content + attachment_ocr(attachments)

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
        "files": [
            {"name": name, "type": kind, "data": data}
            for name, kind, data in attachments
            if keep_file(kind, data)
        ],
    }


def attachment_ocr(attachments: list[tuple[str, str, bytes]]) -> str:
    """图片和 PDF 附件识别出的文字，拼成可以接在正文后面的一段。"""
    block = ""
    for filename, content_type, data in attachments:
        text = extract_attachment_text(filename, content_type, data)
        if not text:
            continue
        logger.info("附件 OCR 完成: %s，识别 %s 字", filename, len(text))
        block = _append_ocr(block, filename, text)
    return block


def mail_attachments(uid: str, account: dict | None) -> list[tuple[str, str, bytes]]:
    """回到邮箱里只读取一封信的附件。用 BODY.PEEK，不改未读状态。"""
    with _imap_session(account) as client:
        status, fetched = client.uid("FETCH", uid, "(BODY.PEEK[])")
    if status != "OK":
        return []
    raw = _rfc822(fetched)
    if not raw:
        return []
    message = email.message_from_bytes(raw, policy=policy.default)
    found = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        filename = part.get_filename()
        if filename and "=?" in filename:
            filename = _decode_mime(filename)
        filename = _clean(filename or "")
        if content_type in {"text/plain", "text/html"} and part.get_content_disposition() != "attachment":
            continue
        data = _part_bytes(part)
        if not data:
            continue
        name, real = _real_type(filename or _default_name(content_type), content_type, data)
        found.append((name, real, data))
    return found


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
    skip_uids: set | None = None,
) -> list[dict]:
    """先按条件选出邮件，断开连接后再解析正文。"""
    raw_mails = []
    with _imap_session(account) as client:
        all_uids = _search_uids(client, criterion)
        if not all_uids:
            logger.info(empty_note)
            return []
        skipped = {str(uid) for uid in (skip_uids or set())}
        pending = [
            uid for uid in all_uids
            if uid not in skipped and failures.get(uid, 0) < SKIP_AFTER
        ]
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
    skip_uids: set | None = None,
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
        skip_uids,
    )


def fetch_recent_mails(
    limit: int = 40,
    account: dict | None = None,
    failures: dict | None = None,
) -> list[dict]:
    """新邮箱第一次接入时读取最近邮件，包含已读。不改邮箱里的未读状态。"""
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


def _outgoing_message(
    username: str,
    to_addr: str,
    subject: str,
    body: str,
    attachments: list[tuple[str, str, bytes]] | None = None,
):
    """没有附件时仍是纯文本。有附件时正文和文件放在同一封信里。"""
    files = attachments or []
    if files:
        message = MIMEMultipart()
        message.attach(MIMEText(body or "", "plain", "utf-8"))
        for name, kind, data in files:
            main, sub = "application", "octet-stream"
            if "/" in (kind or ""):
                left, right = kind.split("/", 1)
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,40}", left) and re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9.+-]{0,40}", right
                ):
                    main, sub = left, right
            part = MIMEBase(main, sub)
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", name))
            message.attach(part)
    else:
        message = MIMEText(body or "", "plain", "utf-8")
    message["Subject"] = subject or "邮件汇总"
    message["From"] = username
    message["To"] = to_addr
    return message


def send_mail(
    to_addr: str,
    subject: str,
    body: str,
    account: dict | None = None,
    attachments: list[tuple[str, str, bytes]] | None = None,
) -> None:
    """用指定邮箱的 SMTP 发送。没指定时用当前保存的第一个邮箱。附件随信发出。"""
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
    message = _outgoing_message(username, to_addr, subject, body, attachments)
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
    logger.info("SMTP 已发送，附件 %s 个", len(attachments or []))
