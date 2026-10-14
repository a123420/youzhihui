"""SQLite 初始化与数据访问。

两张核心表：
- mail_info：一封邮件一行。message_id 唯一，重复拉取不会再插一行。
  正文放在 content，列表只读 excerpt，所以一两百封也不会把页面撑满。
- customer_task：客户汇总任务表，到达 execute_at 后由调度器执行。
"""

from __future__ import annotations

import html
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "youzhihui.db"
CST = timezone(timedelta(hours=8))

# 可重入锁：避免同一线程里的嵌套调用互相卡住。
_db_lock = threading.RLock()

MAX_PARSE_RETRY = 3
MAX_REPLY_CHARS = 10000


def now_str() -> str:
    """当前北京时间，格式固定，便于字符串比较。"""
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _rows(rows) -> list[dict]:
    return [dict(row) for row in rows]


def init_db() -> None:
    """创建数据表。重复执行是安全的。"""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mail_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL UNIQUE,
                subject TEXT DEFAULT '',
                sender TEXT DEFAULT '',
                sender_email TEXT DEFAULT '',
                content TEXT DEFAULT '',
                received_at TEXT DEFAULT '',
                mail_type TEXT DEFAULT '',
                todo_items TEXT DEFAULT '',
                urgency TEXT DEFAULT '',
                deadline TEXT DEFAULT '',
                usefulness TEXT DEFAULT '',
                excerpt TEXT DEFAULT '',
                judge_note TEXT DEFAULT '',
                parse_status TEXT DEFAULT 'pending',
                parse_retry INTEGER DEFAULT 0,
                raw_json TEXT DEFAULT '',
                created_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS customer_task (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT DEFAULT '',
                execute_time TEXT DEFAULT '',
                execute_at TEXT DEFAULT '',
                scope_rule TEXT DEFAULT '',
                output_format TEXT DEFAULT '',
                next_action TEXT DEFAULT '',
                source_mail_id INTEGER UNIQUE,
                reply_to TEXT DEFAULT '',
                reply_subject TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                report TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                executed_at TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_mail_received ON mail_info(received_at);
            CREATE INDEX IF NOT EXISTS idx_mail_parse ON mail_info(parse_status, parse_retry);
            CREATE INDEX IF NOT EXISTS idx_task_status_time ON customer_task(status, execute_at);

            CREATE TABLE IF NOT EXISTS operator_rule (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                poll_mode TEXT DEFAULT 'default',
                poll_minutes INTEGER DEFAULT 0,
                poll_time TEXT DEFAULT '',
                reply_mode TEXT DEFAULT 'default',
                report_mode TEXT DEFAULT 'default',
                report_time TEXT DEFAULT '',
                fetch_limit INTEGER DEFAULT 0,
                summary TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS ai_chat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT ''
            );
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(mail_info)")}
        if "usefulness" not in columns:
            conn.execute("ALTER TABLE mail_info ADD COLUMN usefulness TEXT DEFAULT ''")
        if "excerpt" not in columns:
            conn.execute("ALTER TABLE mail_info ADD COLUMN excerpt TEXT DEFAULT ''")
        if "judge_note" not in columns:
            conn.execute("ALTER TABLE mail_info ADD COLUMN judge_note TEXT DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mail_useful ON mail_info(usefulness, received_at)"
        )
        rule_columns = {row[1] for row in conn.execute("PRAGMA table_info(operator_rule)")}
        if "fetch_limit" not in rule_columns:
            conn.execute("ALTER TABLE operator_rule ADD COLUMN fetch_limit INTEGER DEFAULT 0")
        if "mailbox_id" not in columns:
            conn.execute("ALTER TABLE mail_info ADD COLUMN mailbox_id INTEGER DEFAULT 0")
        if "account" not in columns:
            conn.execute("ALTER TABLE mail_info ADD COLUMN account TEXT DEFAULT ''")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mailbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                label TEXT DEFAULT '',
                imap_host TEXT DEFAULT '',
                imap_port INTEGER DEFAULT 993,
                smtp_host TEXT DEFAULT '',
                smtp_port INTEGER DEFAULT 465,
                smtp_mode TEXT DEFAULT 'ssl',
                created_at TEXT DEFAULT ''
            )
            """
        )
        box_columns = {row[1] for row in conn.execute("PRAGMA table_info(mailbox)")}
        if "history_done" not in box_columns:
            conn.execute("ALTER TABLE mailbox ADD COLUMN history_done INTEGER DEFAULT 0")
            conn.execute(
                """
                UPDATE mailbox SET history_done=1
                 WHERE id IN (SELECT DISTINCT mailbox_id FROM mail_info)
                """
            )
        _import_env_mailbox(conn)


def _read_env_file() -> dict[str, str]:
    path = BASE_DIR / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        text = value.strip()
        if len(text) >= 2 and text[0] == text[-1] == '"':
            text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        else:
            text = text.strip('"')
        values[key.strip()] = text
    return values


def _import_env_mailbox(conn) -> None:
    """把 .env 里原来的那一个邮箱收进邮箱表，已有的邮件仍算这个邮箱的。"""
    current = _read_env_file()
    address = current.get("MAIL_USERNAME", "").strip()
    password = current.get("MAIL_PASSWORD", "").strip()
    host = current.get("MAIL_IMAP_HOST", "").strip()
    if address and password and host:
        exists = conn.execute("SELECT id FROM mailbox LIMIT 1").fetchone()
        if not exists:
            try:
                imap_port = int(current.get("MAIL_IMAP_PORT") or 993)
            except ValueError:
                imap_port = 993
            try:
                smtp_port = int(current.get("MAIL_SMTP_PORT") or 465)
            except ValueError:
                smtp_port = 465
            conn.execute(
                """
                INSERT INTO mailbox (
                    address, password, label, imap_host, imap_port,
                    smtp_host, smtp_port, smtp_mode, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    address,
                    password,
                    current.get("MAIL_LABEL", ""),
                    host,
                    imap_port,
                    current.get("MAIL_SMTP_HOST", ""),
                    smtp_port,
                    current.get("MAIL_SMTP_MODE", "ssl") or "ssl",
                    now_str(),
                ),
            )
    primary = conn.execute("SELECT id, address FROM mailbox ORDER BY id LIMIT 1").fetchone()
    if primary:
        conn.execute(
            "UPDATE mail_info SET mailbox_id=?, account=? WHERE IFNULL(mailbox_id, 0)=0",
            (primary["id"], primary["address"]),
        )


def list_mailboxes() -> list[dict]:
    """页面上看到的邮箱。不含授权码。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, address, label, imap_host, imap_port, smtp_host, smtp_port, smtp_mode
              FROM mailbox
             ORDER BY id
            """
        ).fetchall()
    return _rows(rows)


def mailbox_accounts() -> list[dict]:
    """收取和发送时用。带授权码，不能放到页面上。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, address, password, label, imap_host, imap_port,
                   smtp_host, smtp_port, smtp_mode, history_done
              FROM mailbox
             ORDER BY id
            """
        ).fetchall()
    return _rows(rows)


def get_mailbox_by_address(address: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM mailbox WHERE lower(address)=lower(?)",
            ((address or "").strip(),),
        ).fetchone()
    return dict(row) if row else None


def mailbox_for_mail(mail_id: int) -> dict | None:
    """回复从收到这封信的邮箱发出。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT mailbox_id FROM mail_info WHERE id=?",
            (mail_id,),
        ).fetchone()
        if row and row["mailbox_id"]:
            found = conn.execute("SELECT * FROM mailbox WHERE id=?", (row["mailbox_id"],)).fetchone()
            if found:
                return dict(found)
        found = conn.execute("SELECT * FROM mailbox ORDER BY id LIMIT 1").fetchone()
    return dict(found) if found else None


def save_mailbox(
    address: str,
    password: str,
    label: str,
    imap_host: str,
    imap_port: int,
    smtp_host: str,
    smtp_port: int,
    smtp_mode: str,
) -> int:
    address = (address or "").strip()
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM mailbox").fetchone()["n"]
        existing = conn.execute(
            "SELECT id FROM mailbox WHERE lower(address)=lower(?)",
            (address,),
        ).fetchone()
        if not existing and count >= 8:
            raise ValueError("最多接入 8 个邮箱")
        if existing:
            conn.execute(
                """
                UPDATE mailbox
                   SET password=?, label=?, imap_host=?, imap_port=?,
                       smtp_host=?, smtp_port=?, smtp_mode=?
                 WHERE id=?
                """,
                (password, label, imap_host, imap_port, smtp_host, smtp_port, smtp_mode, existing["id"]),
            )
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO mailbox (
                address, password, label, imap_host, imap_port,
                smtp_host, smtp_port, smtp_mode, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (address, password, label, imap_host, imap_port, smtp_host, smtp_port, smtp_mode, now_str()),
        )
        return int(cur.lastrowid)


def delete_mailbox(mailbox_id: int) -> bool:
    """只断开这个邮箱。已经收下来的邮件留着。"""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM mailbox WHERE id=?", (mailbox_id,))
        return cur.rowcount == 1


def reset_running_tasks() -> int:
    """程序异常退出后，把卡在 running 的任务放回队列。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE customer_task SET status='pending' WHERE status='running'"
        )
        return cur.rowcount


def _excerpt(content: str, limit: int = 120) -> str:
    text = " ".join((content or "").split())
    return text[:limit]


def mask_sensitive(text: str) -> str:
    """列表摘要里打掉验证码和密码。正文仍留在库里，点开后才看原文。"""
    value = text or ""
    if not value:
        return ""
    value = re.sub(
        r"((?:初始)?密码|授权码|password|passwd)(\s*[:：=]\s*)(\S+)",
        lambda matched: matched.group(1) + matched.group(2) + "****",
        value,
        count=4,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"((?:动态|初始|临时)?密码|授权码)(\s*(?:是|为)?\s*)([A-Za-z0-9!@#$%^&*._-]{4,})",
        lambda matched: matched.group(1) + matched.group(2) + "****",
        value,
        count=4,
    )
    value = re.sub(
        r"(code below:\s*)([A-Za-z0-9]{4,12})",
        r"\1****",
        value,
        count=2,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b((?:verification |security |login |one-time )?(?:code|otp|pin))(\s*(?:is|:)\s*)([A-Za-z0-9]{4,12})\b",
        lambda matched: matched.group(1) + matched.group(2) + "****",
        value,
        count=4,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"(验证码|校验码|动态码|确认码)([^\n]{0,16}?)([A-Za-z0-9]{4,12})",
        lambda matched: matched.group(1) + matched.group(2) + "****",
        value,
        count=4,
    )
    return value


def _mask_card(row: dict) -> dict:
    for key in ("subject", "excerpt", "todo_items", "judge_note", "parse_error", "report", "last_error"):
        if key == "report" and row.get("status") == "draft":
            continue
        if row.get(key):
            row[key] = mask_sensitive(str(row[key]))
    return row


_MAIL_CARD_SQL = """
SELECT id, subject, sender, sender_email, received_at, created_at, mail_type,
       urgency, deadline, usefulness, parse_status, account,
       substr(todo_items, 1, 80) AS todo_items,
       substr(judge_note, 1, 80) AS judge_note,
       CASE WHEN parse_status = 'failed' THEN substr(raw_json, 1, 80) ELSE '' END AS parse_error,
       CASE
         WHEN excerpt != '' THEN excerpt
         ELSE substr(replace(replace(content, char(10), ' '), char(13), ' '), 1, 120)
       END AS excerpt
  FROM mail_info
"""


def insert_mail(
    message_id: str,
    subject: str,
    sender: str,
    sender_email: str,
    content: str,
    received_at: str,
    mailbox_id: int = 0,
    account: str = "",
) -> tuple[int, bool]:
    """写入邮件。返回 (id, 是否新插入)。同一个邮箱里的重复 Message-ID 不会再插一行。

    新邮件的 message_id 前面带邮箱编号，同一封信发到两个邮箱各存一份。
    早先存的是不带编号的原样 ID，按邮箱地址一起比对，不会重复入库。
    """
    raw = (message_id or "")[:480]
    key = f"{mailbox_id}:{raw}" if mailbox_id else raw
    with get_conn() as conn:
        if account and raw:
            row = conn.execute(
                """
                SELECT id FROM mail_info
                 WHERE lower(account)=lower(?)
                   AND (
                        message_id IN (?, ?)
                        OR (message_id GLOB '[0-9]*:*'
                            AND substr(message_id, instr(message_id, ':') + 1)=?)
                   )
                 LIMIT 1
                """,
                (account, key, raw, raw),
            ).fetchone()
            if row:
                return int(row["id"]), False
        try:
            cur = conn.execute(
                """
                INSERT INTO mail_info (
                    message_id, subject, sender, sender_email, content,
                    received_at, excerpt, parse_status, parse_retry, created_at,
                    mailbox_id, account
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    key,
                    subject,
                    sender,
                    sender_email,
                    content,
                    received_at,
                    _excerpt(content),
                    now_str(),
                    int(mailbox_id or 0),
                    account or "",
                ),
            )
            return int(cur.lastrowid), True
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM mail_info WHERE message_id=?",
                (key,),
            ).fetchone()
            return (int(row["id"]) if row else 0), False


def get_mail(mail_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM mail_info WHERE id=?",
            (mail_id,),
        ).fetchone()
    return dict(row) if row else None


def bump_parse_retry(mail_id: int, step: int = 1) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE mail_info SET parse_retry = MAX(parse_retry + ?, 0) WHERE id=?",
            (step, mail_id),
        )


def set_mail_usefulness(mail_id: int, usefulness: str) -> bool:
    """页面上改分类。只改这一封，不重新调用模型。"""
    if usefulness not in {"useful", "ordinary", "spam"}:
        raise ValueError("分类无效")
    notes = {
        "useful": "已手动标为有用",
        "ordinary": "已手动标为普通",
        "spam": "已手动标为垃圾，不进入汇总",
    }
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE mail_info SET usefulness=?, judge_note=? WHERE id=?",
            (usefulness, notes[usefulness], mail_id),
        )
        return cur.rowcount == 1


def mark_history_done(mailbox_id: int) -> None:
    """新邮箱第一次补拉最近邮件做完了，以后只收未读。"""
    with get_conn() as conn:
        conn.execute("UPDATE mailbox SET history_done=1 WHERE id=?", (mailbox_id,))


def delete_mail(mail_id: int) -> bool:
    """删掉一封邮件，以及由它生成的任务。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM customer_task WHERE source_mail_id=?", (mail_id,))
        cur = conn.execute("DELETE FROM mail_info WHERE id=?", (mail_id,))
        return cur.rowcount == 1


def delete_all_stored() -> tuple[int, int]:
    """清空已入库邮件和任务。规则和对话保留。"""
    return delete_mails_by_usefulness("all")


def delete_mails_by_usefulness(kind: str) -> tuple[int, int]:
    """按分类删除已入库邮件。全部时连任务一起清掉。"""
    if kind == "all":
        with get_conn() as conn:
            tasks = conn.execute("DELETE FROM customer_task").rowcount
            mails = conn.execute("DELETE FROM mail_info").rowcount
        return mails, tasks
    if kind not in {"useful", "ordinary", "spam"}:
        raise ValueError("分类无效")
    with get_conn() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM mail_info WHERE usefulness=?", (kind,))]
        tasks = 0
        mails = 0
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" * len(chunk))
            tasks += conn.execute(
                f"DELETE FROM customer_task WHERE source_mail_id IN ({marks})",
                chunk,
            ).rowcount
            mails += conn.execute(
                f"DELETE FROM mail_info WHERE id IN ({marks})",
                chunk,
            ).rowcount
    return mails, tasks


def reschedule_task(task_id: int, moment: str) -> bool:
    """改任务时间。已完成的不再改。"""
    text = (moment or "").strip().replace("T", " ")
    if len(text) == 16:
        text += ":00"
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise ValueError("请选择一个有效时间") from None
    text = parsed.strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE customer_task
               SET execute_at=?,
                   status=CASE WHEN status IN ('need_time', 'failed') THEN 'pending' ELSE status END,
                   last_error=CASE
                       WHEN status IN ('need_time', 'failed') OR last_error='到点未发送' THEN ''
                       ELSE last_error
                   END
             WHERE id=? AND status NOT IN ('done', 'running')
            """,
            (text, task_id),
        )
        return cur.rowcount == 1


def complete_task(task_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE customer_task
               SET status='done', executed_at=?,
                   report=CASE WHEN report != '' THEN report ELSE '已在页面上标记完成' END
             WHERE id=? AND status != 'done'
            """,
            (now_str(), task_id),
        )
        return cur.rowcount == 1


def delete_task(task_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM customer_task WHERE id=?", (task_id,))
        return cur.rowcount == 1


def save_task_draft(task_id: int, report: str) -> None:
    """保存可发送的回复。来信后就能改、就能发，不改计划时间。"""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE customer_task
               SET status='draft', report=?, last_error=''
             WHERE id=?
            """,
            ((report or "")[:MAX_REPLY_CHARS], task_id),
        )


def replace_draft_text(task_id: int, body: str) -> bool:
    text = (body or "").strip()
    if not text:
        return False
    if len(text) > MAX_REPLY_CHARS:
        raise ValueError(f"回复太长，最多 {MAX_REPLY_CHARS} 字")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE customer_task SET report=? WHERE id=? AND status='draft'",
            (text, task_id),
        )
        return cur.rowcount == 1


def list_unsent_due(moment: str) -> list[dict]:
    """到点了还没发出去、并且还没提醒过的回复。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, customer_name, execute_at, scope_rule, reply_to
              FROM customer_task
             WHERE status='draft' AND execute_at != '' AND execute_at <= ?
               AND (last_error IS NULL OR last_error NOT LIKE '到点未发送%')
             ORDER BY execute_at ASC
             LIMIT 10
            """,
            (moment,),
        ).fetchall()
    return _rows(rows)


def mark_unsent_due(task_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE customer_task SET last_error='到点未发送' WHERE id=? AND status='draft'",
            (task_id,),
        )


def mark_draft_sent(task_id: int, note: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT report FROM customer_task WHERE id=? AND status='draft'",
            (task_id,),
        ).fetchone()
        if not row:
            return False
        stored = f"{(row['report'] or '')[:20000]}\n\n发信情况：{note}"
        cur = conn.execute(
            """
            UPDATE customer_task
               SET status='done', report=?, last_error='', executed_at=?
             WHERE id=? AND status='draft'
            """,
            (stored, now_str(), task_id),
        )
        return cur.rowcount == 1


def search_mails(query: str, limit: int = 100) -> list[dict]:
    """按发件人、主题、摘要和正文搜索全部已入库邮件。列表仍只返回打码后的摘要。"""
    text = (query or "").strip()
    if not text:
        return []
    needle = text.replace("%", "").replace("_", "").replace("\\", "")[:80]
    if not needle:
        return []
    like = f"%{needle}%"
    with get_conn() as conn:
        rows = conn.execute(
            _MAIL_CARD_SQL
            + """
             WHERE subject LIKE ? OR sender LIKE ? OR sender_email LIKE ?
                OR excerpt LIKE ? OR todo_items LIKE ? OR judge_note LIKE ?
                OR content LIKE ? OR account LIKE ?
             ORDER BY id DESC
             LIMIT ?
            """,
            (like, like, like, like, like, like, like, like, limit),
        ).fetchall()
    return [_mask_card(row) for row in _rows(rows)]


def mail_detail(mail_id: int) -> dict | None:
    """点开后看原文。摘要打码不影响这里。"""
    row = get_mail(mail_id)
    if not row:
        return None
    content = row.get("content") or ""
    truncated = len(content) > 16000
    return {
        "id": row["id"],
        "subject": row.get("subject") or "",
        "sender": row.get("sender") or "",
        "sender_email": row.get("sender_email") or "",
        "received_at": row.get("received_at") or "",
        "usefulness": row.get("usefulness") or "",
        "judge_note": row.get("judge_note") or "",
        "todo_items": row.get("todo_items") or "",
        "content": content[:16000],
        "truncated": truncated,
    }


_EXPORT_STATUS = {
    "useful": "有用",
    "ordinary": "普通",
    "spam": "垃圾",
    "pending": "待执行",
    "draft": "待发送",
    "done": "已完成",
    "running": "执行中",
    "failed": "失败",
    "need_time": "待确认时间",
}


def _sheet_text(value: str, limit: int = 180) -> str:
    text = mask_sensitive(value or "")
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or "—"


def _sheet_who(sender: str, email: str) -> str:
    name = re.sub(r"^[\s。\.·,，]+|[\s。\.·,，]+$", "", sender or "")
    address = (email or "").strip()
    if name and address and name.lower() != address.lower():
        return f"{name} <{address}>"
    return address or name or "—"


def export_period_sheet(start: str, end: str) -> bytes:
    """按日期导出 Excel 能按列打开的表格。时间按文字显示，一行一条。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start or "") or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end or ""):
        raise ValueError("请选择开始和结束日期")
    if start > end:
        raise ValueError("开始日期要早于结束日期")
    start_day = datetime.strptime(start, "%Y-%m-%d")
    end_day = datetime.strptime(end, "%Y-%m-%d")
    if (end_day - start_day).days > 366:
        raise ValueError("一次最多导出 366 天")
    start_at = start + " 00:00:00"
    end_at = end + " 23:59:59"
    with get_conn() as conn:
        mails = conn.execute(
            """
            SELECT received_at, sender, sender_email, subject, usefulness,
                   CASE
                     WHEN excerpt != '' THEN excerpt
                     ELSE substr(replace(replace(content, char(10), ' '), char(13), ' '), 1, 160)
                   END AS excerpt,
                   substr(todo_items, 1, 80) AS todo_items
              FROM mail_info
             WHERE received_at >= ? AND received_at <= ?
             ORDER BY received_at ASC
            """,
            (start_at, end_at),
        ).fetchall()
        tasks = conn.execute(
            """
            SELECT execute_at, created_at, customer_name, scope_rule, status,
                   substr(report, 1, 200) AS report
              FROM customer_task
             WHERE (execute_at >= ? AND execute_at <= ?)
                OR (created_at >= ? AND created_at <= ?)
             ORDER BY execute_at ASC, id ASC
            """,
            (start_at, end_at, start_at, end_at),
        ).fetchall()
    rows: list[list[str]] = []
    for row in mails:
        rows.append([
            "邮件",
            (row["received_at"] or "")[:16],
            _sheet_who(row["sender"] or "", row["sender_email"] or ""),
            _sheet_text(row["subject"] or "", 80) if (row["subject"] or "").strip() else "（无主题）",
            _sheet_text(row["excerpt"] or row["todo_items"] or ""),
            _EXPORT_STATUS.get(row["usefulness"] or "", row["usefulness"] or "—"),
        ])
    for row in tasks:
        rows.append([
            "任务",
            ((row["execute_at"] or row["created_at"] or ""))[:16],
            _sheet_text(row["customer_name"] or "", 40),
            _sheet_text(row["scope_rule"] or "", 80),
            _sheet_text(row["report"] or ""),
            _EXPORT_STATUS.get(row["status"] or "", row["status"] or "—"),
        ])
    widths = (72, 150, 220, 240, 360, 88)
    headers = ("种类", "时间", "对方", "标题", "内容", "状态")
    body = [
        "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in item) + "</tr>"
        for item in rows
    ]
    if not body:
        body = ["<tr><td colspan=\"6\">这段日期里没有邮件或任务。</td></tr>"]
    cols = "".join(f"<col width=\"{width}\">" for width in widths)
    head = "".join(f"<th>{html.escape(name)}</th>" for name in headers)
    title = html.escape(f"邮智汇导出  {start} 至 {end}")
    page = f"""<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
table {{ border-collapse: collapse; table-layout: fixed; width: 1130px; font-family: "Microsoft YaHei", sans-serif; font-size: 12px; }}
caption {{ caption-side: top; text-align: left; font-size: 16px; font-weight: 700; padding: 8px 4px 12px; }}
th, td {{ border: 1px solid #d9d3c7; padding: 8px 10px; vertical-align: top; text-align: left; mso-number-format:"\\@"; white-space: normal; word-wrap: break-word; }}
th {{ background: #0e6b62; color: #fff; font-weight: 700; }}
tr:nth-child(even) td {{ background: #f7f4ee; }}
</style>
</head>
<body>
<table>
<caption>{title}</caption>
<colgroup>{cols}</colgroup>
<thead><tr>{head}</tr></thead>
<tbody>
{"".join(body)}
</tbody>
</table>
</body>
</html>"""
    return page.encode("utf-8")


def update_mail_parse(
    mail_id: int,
    mail_type: str,
    todo_items: str,
    urgency: str,
    deadline: str,
    usefulness: str,
    parse_status: str,
    raw_json: str,
    judge_note: str = "",
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE mail_info
               SET mail_type=?, todo_items=?, urgency=?, deadline=?,
                   usefulness=?, parse_status=?, raw_json=?, judge_note=?
             WHERE id=?
            """,
            (
                mail_type,
                todo_items,
                urgency,
                deadline,
                usefulness,
                parse_status,
                raw_json,
                judge_note,
                mail_id,
            ),
        )


def reset_mail_parse(mail_id: int) -> bool:
    """识别失败的邮件重新排队，失败次数清零。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE mail_info SET parse_status='pending', parse_retry=0 WHERE id=? AND parse_status='failed'",
            (mail_id,),
        )
        return cur.rowcount == 1


def list_mails_need_parse(limit: int = 5) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM mail_info
             WHERE parse_status != 'ok' AND parse_retry < ?
             ORDER BY id DESC
             LIMIT ?
            """,
            (MAX_PARSE_RETRY, limit),
        ).fetchall()
    return _rows(rows)


def query_recent_mails(since: str, limit: int = 30) -> list[dict]:
    """取回溯窗口内的邮件，新邮件在前。调用方需要时间顺序时再自行反转。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, subject, sender, sender_email, content, received_at,
                   mail_type, todo_items, urgency, deadline, usefulness
              FROM mail_info
             WHERE received_at >= ? AND usefulness != 'spam'
             ORDER BY received_at DESC
             LIMIT ?
            """,
            (since, limit),
        ).fetchall()
    return _rows(rows)


def query_mails_between(start: str, end: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, subject, sender, sender_email, content, received_at,
                   mail_type, todo_items, urgency, deadline, usefulness, parse_status
              FROM mail_info
             WHERE received_at >= ? AND received_at <= ?
             ORDER BY received_at ASC
            """,
            (start, end),
        ).fetchall()
    return _rows(rows)


def insert_customer_task(
    customer_name: str,
    execute_time: str,
    execute_at: str,
    scope_rule: str,
    output_format: str,
    next_action: str,
    source_mail_id: int,
    reply_to: str,
    reply_subject: str,
    status: str,
) -> dict | None:
    """同一封来源邮件只生成一条任务。重复时返回 None。"""
    with get_conn() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO customer_task (
                    customer_name, execute_time, execute_at, scope_rule,
                    output_format, next_action, source_mail_id, reply_to,
                    reply_subject, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    customer_name,
                    execute_time,
                    execute_at,
                    scope_rule,
                    output_format,
                    next_action,
                    source_mail_id,
                    reply_to,
                    reply_subject,
                    status,
                    now_str(),
                ),
            )
            new_id = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None
    return get_task(new_id)


def get_task(task_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM customer_task WHERE id=?",
            (task_id,),
        ).fetchone()
    return dict(row) if row else None


def list_due_tasks(moment: str, limit: int = 5) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM customer_task
             WHERE status='pending' AND execute_at != '' AND execute_at <= ?
             ORDER BY execute_at ASC
             LIMIT ?
            """,
            (moment, limit),
        ).fetchall()
    return _rows(rows)


def claim_task(task_id: int) -> bool:
    """只有 pending 能被抢走，避免同一次扫描执行两遍。"""
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE customer_task
               SET status='running'
             WHERE id=? AND status='pending'
            """,
            (task_id,),
        )
        return cur.rowcount == 1


def finish_task(task_id: int, report: str, last_error: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE customer_task
               SET status='done', report=?, last_error=?, executed_at=?
             WHERE id=?
            """,
            (report, last_error, now_str(), task_id),
        )


def fail_task(task_id: int, error: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE customer_task
               SET status='failed', last_error=?, executed_at=?
             WHERE id=?
            """,
            (error[:2000], now_str(), task_id),
        )


def dashboard_snapshot(mail_limit: int = 40, task_limit: int = 12) -> dict:
    """演示界面用的轻量快照，不返回正文全文和密钥。"""
    with get_conn() as conn:
        mail_count = int(conn.execute("SELECT COUNT(*) FROM mail_info").fetchone()[0])
        useful_count = int(
            conn.execute("SELECT COUNT(*) FROM mail_info WHERE usefulness='useful'").fetchone()[0]
        )
        spam_count = int(
            conn.execute("SELECT COUNT(*) FROM mail_info WHERE usefulness='spam'").fetchone()[0]
        )
        pending = int(
            conn.execute(
                "SELECT COUNT(*) FROM customer_task WHERE status='pending'"
            ).fetchone()[0]
        )
        drafts = int(
            conn.execute(
                "SELECT COUNT(*) FROM customer_task WHERE status='draft'"
            ).fetchone()[0]
        )
        done = int(
            conn.execute(
                "SELECT COUNT(*) FROM customer_task WHERE status='done'"
            ).fetchone()[0]
        )
        mails = conn.execute(
            _MAIL_CARD_SQL + " ORDER BY id DESC LIMIT ?",
            (mail_limit,),
        ).fetchall()
        tasks = conn.execute(
            """
            SELECT id, customer_name, execute_at, status, scope_rule, output_format, next_action,
                   CASE WHEN status='draft' THEN substr(report, 1, 2000) ELSE substr(report, 1, 180) END AS report,
                   last_error, created_at
              FROM customer_task
             ORDER BY CASE WHEN execute_at = '' THEN 1 ELSE 0 END, execute_at ASC, id ASC
             LIMIT ?
            """,
            (task_limit,),
        ).fetchall()
    return {
        "mail_count": mail_count,
        "useful_count": useful_count,
        "spam_count": spam_count,
        "pending_count": pending,
        "draft_count": drafts,
        "done_count": done,
        "mails": [_mask_card(row) for row in _rows(mails)],
        "tasks": [_mask_card(row) for row in _rows(tasks)],
    }


def list_recent_activity(since: str) -> dict:
    """取某一天之后的邮件和任务，供运行记录按天归类。"""
    with get_conn() as conn:
        mails = conn.execute(
            _MAIL_CARD_SQL
            + """
             WHERE substr(received_at, 1, 10) >= ? OR substr(created_at, 1, 10) >= ?
             ORDER BY id DESC
            """,
            (since, since),
        ).fetchall()
        tasks = conn.execute(
            """
            SELECT id, customer_name, execute_at, status, scope_rule, output_format, created_at, executed_at,
                   CASE WHEN status='draft' THEN substr(report, 1, 2000) ELSE substr(report, 1, 180) END AS report,
                   substr(last_error, 1, 180) AS last_error
              FROM customer_task
             WHERE substr(created_at, 1, 10) >= ?
                OR substr(executed_at, 1, 10) >= ?
                OR substr(execute_at, 1, 10) >= ?
             ORDER BY CASE WHEN execute_at = '' THEN 1 ELSE 0 END, execute_at ASC, id ASC
            """,
            (since, since, since),
        ).fetchall()
    return {
        "mails": [_mask_card(row) for row in _rows(mails)],
        "tasks": [_mask_card(row) for row in _rows(tasks)],
    }


def get_operator_rule() -> dict | None:
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM operator_rule WHERE id=1").fetchone()
    return dict(row) if row else None


def save_operator_rule(rule: dict) -> None:
    init_db()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO operator_rule (
                id, poll_mode, poll_minutes, poll_time, reply_mode,
                report_mode, report_time, fetch_limit, summary, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                poll_mode=excluded.poll_mode,
                poll_minutes=excluded.poll_minutes,
                poll_time=excluded.poll_time,
                reply_mode=excluded.reply_mode,
                report_mode=excluded.report_mode,
                report_time=excluded.report_time,
                fetch_limit=excluded.fetch_limit,
                summary=excluded.summary,
                updated_at=excluded.updated_at
            """,
            (
                rule.get("poll_mode") or "default",
                int(rule.get("poll_minutes") or 0),
                rule.get("poll_time") or "",
                rule.get("reply_mode") or "default",
                rule.get("report_mode") or "default",
                rule.get("report_time") or "",
                int(rule.get("fetch_limit") or 0),
                rule.get("summary") or "",
                now_str(),
            ),
        )


def add_chat(role: str, content: str) -> None:
    text = (content or "").strip()
    if not text:
        return
    init_db()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ai_chat (role, content, created_at) VALUES (?, ?, ?)",
            (role, text[:4000], now_str()),
        )


def clear_chat() -> int:
    """只删对话记录，不改已保存的规则，也不动邮件。"""
    init_db()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM ai_chat")
    return cur.rowcount


def list_chat(limit: int = 40) -> list[dict]:
    init_db()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, role, content, created_at
              FROM ai_chat
             ORDER BY id DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return list(reversed(_rows(rows)))


def query_tasks_between(start: str, end: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, customer_name, execute_time, execute_at, scope_rule,
                   output_format, next_action, status, report, last_error,
                   created_at, executed_at
              FROM customer_task
             WHERE created_at >= ? AND created_at <= ?
             ORDER BY id ASC
            """,
            (start, end),
        ).fetchall()
    return _rows(rows)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    init_db()
    print(f"数据库已就绪: {DB_PATH}")
