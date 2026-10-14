"""启动后在页面里填写邮箱、授权码和钉钉。服务器按邮箱自动识别。"""

from __future__ import annotations

import random
import re
import socket
import struct
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# 公共邮箱按域名直接确定。企业自定义域名再看 MX。
_DOMAIN_HOSTS = {
    "qq.com": ("QQ邮箱", "imap.qq.com", "smtp.qq.com", "465", "ssl"),
    "vip.qq.com": ("QQ邮箱", "imap.qq.com", "smtp.qq.com", "465", "ssl"),
    "foxmail.com": ("Foxmail", "imap.qq.com", "smtp.qq.com", "465", "ssl"),
    "163.com": ("网易163", "imap.163.com", "smtp.163.com", "465", "ssl"),
    "126.com": ("网易126", "imap.126.com", "smtp.126.com", "465", "ssl"),
    "yeah.net": ("网易Yeah", "imap.yeah.net", "smtp.yeah.net", "465", "ssl"),
    "188.com": ("网易188", "imap.188.com", "smtp.188.com", "465", "ssl"),
    "vip.163.com": ("网易VIP", "imap.vip.163.com", "smtp.vip.163.com", "465", "ssl"),
    "aliyun.com": ("阿里个人邮箱", "imap.aliyun.com", "smtp.aliyun.com", "465", "ssl"),
    "gmail.com": ("Gmail", "imap.gmail.com", "smtp.gmail.com", "465", "ssl"),
    "googlemail.com": ("Gmail", "imap.gmail.com", "smtp.gmail.com", "465", "ssl"),
    "outlook.com": ("Outlook", "outlook.office365.com", "smtp.office365.com", "587", "starttls"),
    "hotmail.com": ("Outlook", "outlook.office365.com", "smtp.office365.com", "587", "starttls"),
    "live.com": ("Outlook", "outlook.office365.com", "smtp.office365.com", "587", "starttls"),
    "msn.com": ("Outlook", "outlook.office365.com", "smtp.office365.com", "587", "starttls"),
    "icloud.com": ("iCloud", "imap.mail.me.com", "smtp.mail.me.com", "587", "starttls"),
    "me.com": ("iCloud", "imap.mail.me.com", "smtp.mail.me.com", "587", "starttls"),
    "sina.com": ("新浪邮箱", "imap.sina.com", "smtp.sina.com", "465", "ssl"),
    "sina.cn": ("新浪邮箱", "imap.sina.com", "smtp.sina.com", "465", "ssl"),
    "vip.sina.com": ("新浪VIP", "imap.vip.sina.com", "smtp.vip.sina.com", "465", "ssl"),
    "sohu.com": ("搜狐邮箱", "imap.sohu.com", "smtp.sohu.com", "465", "ssl"),
    "139.com": ("139邮箱", "imap.139.com", "smtp.139.com", "465", "ssl"),
    "189.cn": ("189邮箱", "imap.189.cn", "smtp.189.cn", "465", "ssl"),
    "21cn.com": ("21CN邮箱", "imap.21cn.com", "smtp.21cn.com", "465", "ssl"),
    "tom.com": ("TOM邮箱", "imap.tom.com", "smtp.tom.com", "465", "ssl"),
    "263.net": ("263邮箱", "imap.263.net", "smtp.263.net", "465", "ssl"),
    "yahoo.com": ("Yahoo", "imap.mail.yahoo.com", "smtp.mail.yahoo.com", "465", "ssl"),
}

# MX 主机名里出现这些片段时，对上企业邮服务器。
_MX_HOSTS = (
    ("mxhichina.com", "阿里企业邮箱", "imap.qiye.aliyun.com", "smtp.qiye.aliyun.com", "465", "ssl"),
    ("qiye.aliyun.com", "阿里企业邮箱", "imap.qiye.aliyun.com", "smtp.qiye.aliyun.com", "465", "ssl"),
    ("mx.aliyun.com", "阿里企业邮箱", "imap.qiye.aliyun.com", "smtp.qiye.aliyun.com", "465", "ssl"),
    ("exmail.qq.com", "腾讯企业邮", "imap.exmail.qq.com", "smtp.exmail.qq.com", "465", "ssl"),
    ("mxbiz.qq.com", "腾讯企业邮", "imap.exmail.qq.com", "smtp.exmail.qq.com", "465", "ssl"),
    ("qq.com", "QQ邮箱", "imap.qq.com", "smtp.qq.com", "465", "ssl"),
    ("protection.outlook.com", "Microsoft 365", "outlook.office365.com", "smtp.office365.com", "587", "starttls"),
    ("outlook.com", "Microsoft 365", "outlook.office365.com", "smtp.office365.com", "587", "starttls"),
    ("google.com", "Google Workspace", "imap.gmail.com", "smtp.gmail.com", "465", "ssl"),
    ("googlemail.com", "Google Workspace", "imap.gmail.com", "smtp.gmail.com", "465", "ssl"),
    ("qiye.163.com", "网易企业邮", "imap.qiye.163.com", "smtp.qiye.163.com", "465", "ssl"),
    ("ym.163.com", "网易企业邮", "imap.qiye.163.com", "smtp.qiye.163.com", "465", "ssl"),
    ("netease.com", "网易企业邮", "imap.qiye.163.com", "smtp.qiye.163.com", "465", "ssl"),
    ("feishu.cn", "飞书邮箱", "imap.feishu.cn", "smtp.feishu.cn", "465", "ssl"),
    ("larksuite.com", "飞书邮箱", "imap.feishu.cn", "smtp.feishu.cn", "465", "ssl"),
)

_mx_cache: dict[str, list[str]] = {}


def _read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _unquote(value)
    return values


def _unquote(value: str) -> str:
    """和 _format_value 配对：加了引号的值要把转义还原。"""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text.strip('"')


def _format_value(value: str) -> str:
    if any(char in value for char in " #\t"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _write_updates(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={_format_value(updates[key])}")
            seen.add(key)
        else:
            new_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={_format_value(value)}")
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _email_domain(username: str) -> str:
    text = (username or "").strip().lower()
    if "@" not in text:
        return ""
    return text.split("@", 1)[1].strip().strip(".")


def _known_domain(domain: str) -> tuple | None:
    parts = domain.split(".")
    for index in range(len(parts) - 1):
        candidate = ".".join(parts[index:])
        if candidate in _DOMAIN_HOSTS:
            return _DOMAIN_HOSTS[candidate]
    return None


def _pack_result(label: str, imap_host: str, smtp_host: str, smtp_port: str, smtp_mode: str) -> dict:
    return {
        "label": label,
        "imap_host": imap_host,
        "imap_port": "993",
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_mode": smtp_mode,
    }


def _system_dns() -> list[str]:
    servers: list[str] = []
    try:
        import winreg

        base = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces",
        )
        index = 0
        while True:
            try:
                name = winreg.EnumKey(base, index)
            except OSError:
                break
            index += 1
            with winreg.OpenKey(base, name) as sub:
                for field in ("NameServer", "DhcpNameServer"):
                    try:
                        value, _kind = winreg.QueryValueEx(sub, field)
                    except OSError:
                        continue
                    for part in str(value).replace(",", " ").split():
                        if part.count(".") == 3 and part not in servers:
                            servers.append(part)
    except OSError:
        pass
    for extra in ("223.5.5.5", "119.29.29.29"):
        if extra not in servers:
            servers.append(extra)
    return servers[:4]


def _read_dns_name(data: bytes, offset: int) -> tuple[str, int]:
    labels = []
    jumped = False
    end = offset
    guard = 0
    while guard < 20 and offset < len(data):
        guard += 1
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                end = offset
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                end = offset + 2
            offset = pointer
            jumped = True
            continue
        offset += 1
        labels.append(data[offset : offset + length].decode("ascii", "ignore"))
        offset += length
        if not jumped:
            end = offset
    return ".".join(labels).lower(), end


def _mx_from_packet(data: bytes) -> list[str]:
    if len(data) < 12:
        return []
    count = struct.unpack(">H", data[6:8])[0]
    offset = 12
    _name, offset = _read_dns_name(data, offset)
    offset += 4
    found = []
    for _ in range(count):
        if offset + 10 > len(data):
            break
        _name, offset = _read_dns_name(data, offset)
        rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", data[offset : offset + 10])
        offset += 10
        if offset + rdlength > len(data):
            break
        if rtype == 15 and rdlength > 2:
            host, _end = _read_dns_name(data, offset + 2)
            if host:
                found.append(host)
        offset += rdlength
    return found


def _lookup_mx(domain: str) -> list[str]:
    if domain in _mx_cache:
        return _mx_cache[domain]
    query_name = b"".join(bytes([len(part)]) + part.encode("ascii", "ignore") for part in domain.split(".") if part)
    query_name += b"\x00"
    question = query_name + struct.pack(">HH", 15, 1)
    hosts: list[str] = []
    for server in _system_dns():
        header = struct.pack(">HHHHHH", random.randint(0, 65535), 0x0100, 1, 0, 0, 0)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(1.5)
            sock.sendto(header + question, (server, 53))
            data, _addr = sock.recvfrom(2048)
            hosts = _mx_from_packet(data)
            if hosts:
                break
        except OSError:
            continue
        finally:
            sock.close()
    _mx_cache[domain] = hosts
    return hosts


def _from_mx(domain: str) -> dict | None:
    try:
        hosts = _lookup_mx(domain)
    except Exception:
        return None
    blob = " ".join(hosts)
    for needle, label, imap_host, smtp_host, smtp_port, smtp_mode in _MX_HOSTS:
        if needle in blob:
            return _pack_result(label, imap_host, smtp_host, smtp_port, smtp_mode)
    return None


def detect_mailbox(username: str) -> dict:
    """按邮箱域名识别 IMAP/SMTP。识别不到时按 imap.域名 尝试。"""
    domain = _email_domain(username)
    if not domain or "." not in domain:
        return _pack_result("", "", "", "465", "ssl")
    known = _known_domain(domain)
    if known:
        label, imap_host, smtp_host, smtp_port, smtp_mode = known
        return _pack_result(label, imap_host, smtp_host, smtp_port, smtp_mode)
    found = _from_mx(domain)
    if found:
        return found
    return _pack_result("按域名尝试", f"imap.{domain}", f"smtp.{domain}", "465", "ssl")


def apply_ui_config(
    username: str,
    password: str,
    webhook: str,
    imap_host: str = "",
    imap_port: str = "",
    smtp_host: str = "",
    smtp_port: str = "",
    smtp_mode: str = "",
) -> str | None:
    """保存一个邮箱。服务器可以自动识别，也可以自己填。已有的邮箱会更新，不会把别的邮箱清掉。"""
    from db_init import get_mailbox_by_address, init_db, mailbox_accounts, save_mailbox

    init_db()
    current = _read_env()
    username = (username or "").strip()
    if "@" not in username or "." not in username.split("@", 1)[1]:
        return "请填写完整邮箱地址"
    existing = get_mailbox_by_address(username)
    secret = (password or "").strip() or (existing or {}).get("password") or ""
    if not secret:
        return "请填写授权码"
    found = detect_mailbox(username)
    raw_imap = (imap_host or "").strip()
    raw_smtp = (smtp_host or "").strip()
    host = _clean_host(imap_host) or ("" if raw_imap else found["imap_host"])
    smtp = _clean_host(smtp_host) or ("" if raw_smtp else found["smtp_host"])
    if raw_imap and not _clean_host(imap_host):
        return "收信服务器格式不对"
    if raw_smtp and not _clean_host(smtp_host):
        return "发信服务器格式不对"
    if host.startswith("imap.") and not _clean_host(smtp_host):
        smtp = smtp or ("smtp." + host[5:])
    if not host or not smtp:
        return "请填写收信服务器和发信服务器"
    try:
        in_port = int(imap_port or found.get("imap_port") or 993)
        out_port = int(smtp_port or found.get("smtp_port") or 465)
    except ValueError:
        return "端口要填数字"
    if not 1 <= in_port <= 65535 or not 1 <= out_port <= 65535:
        return "端口要在 1 到 65535 之间"
    mode = (smtp_mode or found.get("smtp_mode") or "ssl").strip().lower()
    if mode not in {"ssl", "starttls"}:
        mode = "ssl"
    label = found["label"] or "自填服务器"
    try:
        save_mailbox(username, secret, label, host, in_port, smtp, out_port, mode)
    except ValueError as exc:
        return str(exc)
    hook = (webhook or "").strip() or current.get("DINGTALK_WEBHOOK", "")
    _sync_env_from_db(hook, mailbox_accounts())
    return None


def _clean_host(value: str) -> str:
    text = (value or "").strip().lower().rstrip(".")
    if not text:
        return ""
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,188}[a-z0-9])?", text):
        return ""
    return text


def _sync_env_from_db(webhook: str, accounts: list[dict]) -> None:
    """环境文件保留第一个邮箱，方便原来的启动判断。其余邮箱在数据库里。"""
    primary = accounts[0] if accounts else None
    updates = {"DINGTALK_WEBHOOK": webhook or ""}
    if primary:
        updates.update(
            {
                "MAIL_IMAP_HOST": primary.get("imap_host") or "",
                "MAIL_IMAP_PORT": str(primary.get("imap_port") or 993),
                "MAIL_SMTP_HOST": primary.get("smtp_host") or "",
                "MAIL_SMTP_PORT": str(primary.get("smtp_port") or 465),
                "MAIL_SMTP_MODE": primary.get("smtp_mode") or "ssl",
                "MAIL_LABEL": primary.get("label") or "",
                "MAIL_USERNAME": primary.get("address") or "",
                "MAIL_PASSWORD": primary.get("password") or "",
            }
        )
    else:
        updates.update({"MAIL_USERNAME": "", "MAIL_PASSWORD": ""})
    _write_updates(updates)


def sync_after_mailbox_change() -> None:
    from db_init import mailbox_accounts

    current = _read_env()
    _sync_env_from_db(current.get("DINGTALK_WEBHOOK", ""), mailbox_accounts())


def _ask(label: str, current: str, secret: bool = False) -> str:
    state = "已填写" if current else "未填写"
    entered = input(f"{label}（当前{state}，直接回车保持不变）：").strip()
    if not entered:
        return current
    if secret:
        print("已记录。")
    return entered


def ensure_ready() -> bool:
    """已经填过就直接开始；没填或选择更换时，在当前窗口里填写后继续运行。"""
    current = _read_env()
    username = current.get("MAIL_USERNAME", "")
    password = current.get("MAIL_PASSWORD", "")
    if username and password:
        print(f"当前邮箱：{username}")
        choice = input("直接回车开始运行。输入 1 更换邮箱或钉钉：").strip()
        if choice != "1":
            return True
        print()

    print("填写后会直接开始运行。DeepSeek 和模型不用再填。服务器按邮箱自动识别。")
    username = _ask("邮箱地址", username)
    password = _ask("授权码", password, secret=True)
    webhook = _ask("钉钉 Webhook，可留空", current.get("DINGTALK_WEBHOOK", ""))
    if not username or not password:
        print("邮箱和授权码都要填写。请重新双击 启动.bat。")
        return False
    if apply_ui_config(username, password, webhook):
        print("没能从邮箱地址识别服务器。")
        return False
    print("已保存，开始运行。")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if ensure_ready() else 1)
