"""本地离线 OCR。

使用 PaddleOCR 识别 jpg/png，使用内置 poppler 把扫描版 PDF 转成图片后再识别。
识别失败、依赖缺失、poppler 缺失都只记录日志并返回空字符串，不中断收信主流程。
识别文字只作为邮件正文的辅助材料，业务结论仍要人工复核。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

from dotenv import load_dotenv

from quiet_windows import install as _hide_child_consoles

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
_hide_child_consoles()

def _run_hidden(args: list[str]) -> subprocess.CompletedProcess:
    """调用控制台程序时不弹出黑窗口。"""
    kwargs = {"capture_output": True, "text": True, "check": False}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        kwargs["startupinfo"] = startup
    return subprocess.run(args, **kwargs)


def _call_hidden(func):
    if os.name != "nt":
        return func()
    original = subprocess.Popen

    def popen(*args, **kwargs):
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | 0x08000000
        startup = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        kwargs["startupinfo"] = startup
        return original(*args, **kwargs)

    subprocess.Popen = popen
    try:
        return func()
    finally:
        subprocess.Popen = original


def _install_hidden_popen() -> None:
    if os.name != "nt" or getattr(subprocess.Popen, "_youzhihui_hidden", False):
        return
    original = subprocess.Popen

    class _HiddenPopen(original):
        _youzhihui_hidden = True

        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | 0x08000000
            startup = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            kwargs["startupinfo"] = startup
            super().__init__(*args, **kwargs)

    subprocess.Popen = _HiddenPopen


_install_hidden_popen()

logger = logging.getLogger("youzhihui.ocr")

# Windows 上 Paddle 与其他科学计算库同时存在时，偶尔会因 OpenMP 冲突无法加载。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_enable_onednn", "0")
os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "False")
# 模型下载到项目目录，避免散落在用户主目录。必须在 import paddleocr 之前设置。
def _ascii_model_home() -> str:
    """Paddle 打不开含中文的模型路径。模型仍放在项目里，通过英文目录联接访问。"""
    target = BASE_DIR / "tools" / "paddlex"
    target.mkdir(parents=True, exist_ok=True)
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return str(target)
    link = Path(local) / "youzhihui" / "paddlex"
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            _run_hidden(["cmd", "/c", "mklink", "/J", str(link), str(target)])
        if link.exists():
            return str(link)
    except Exception as exc:
        logger.warning("创建模型目录联接失败，仍使用项目目录: %s", exc)
    return str(target)


os.environ.setdefault("PADDLE_PDX_CACHE_HOME", _ascii_model_home())
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024

_engine = None
_engine_error: Exception | None = None
_engine_lock = threading.Lock()
_pdf_disabled_reason = ""


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bundled_poppler() -> Path | None:
    root = BASE_DIR / "tools" / "poppler"
    if not root.exists():
        return None
    for exe in root.rglob("pdftoppm.exe"):
        return exe.parent
    return None


def _poppler_path() -> str | None:
    value = os.getenv("POPPLER_PATH", "").strip().strip('"')
    if value:
        folder = Path(value)
        if not folder.is_absolute():
            folder = BASE_DIR / folder
        if (folder / "pdftoppm.exe").exists() or (folder / "pdftoppm").exists():
            return str(folder)
        logger.warning("POPPLER_PATH 无效，改为使用项目内置 poppler: %s", value)
    bundled = _bundled_poppler()
    if bundled:
        return str(bundled)
    return None


def poppler_available() -> bool:
    """判断 PDF 转图片所需的 poppler 是否可用。"""
    if _poppler_path():
        return True
    return shutil.which("pdftoppm") is not None or shutil.which("pdfinfo") is not None


def _build_engine():
    from paddleocr import PaddleOCR

    # 当前环境是 PaddleOCR 3.x。旧参数只作为 2.x 的后备。
    attempts = (
        {
            "lang": "ch",
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
        },
        {"lang": "ch"},
        {},
        {"use_angle_cls": True, "lang": "ch", "show_log": False, "use_gpu": False},
        {"use_angle_cls": True, "lang": "ch"},
    )
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            return PaddleOCR(**kwargs)
        except TypeError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return PaddleOCR()


def _get_engine():
    global _engine, _engine_error
    with _engine_lock:
        if _engine_error is not None:
            raise _engine_error
        if _engine is None:
            try:
                logger.info("正在加载本地 PaddleOCR，首次运行会下载模型，请稍候")
                logging.getLogger("ppocr").setLevel(logging.ERROR)
                _engine = _build_engine()
                logger.info("本地 PaddleOCR 已就绪")
            except Exception as exc:
                _engine_error = exc
                raise
        return _engine


def _add_text(lines: list[str], value) -> None:
    if value is None:
        return
    text = str(value).strip()
    if text:
        lines.append(text)


def _collect_texts(result) -> list[str]:
    """兼容 PaddleOCR 2.x 的框+文本结构，以及 3.x 的 rec_texts。"""
    lines: list[str] = []

    def from_line(node) -> bool:
        if not isinstance(node, (list, tuple)) or len(node) < 2:
            return False
        text_part = node[1]
        if isinstance(text_part, (list, tuple)) and text_part and isinstance(text_part[0], str):
            _add_text(lines, text_part[0])
            return True
        return False

    def walk(node) -> None:
        if node is None or isinstance(node, (int, float, bytes)):
            return
        if isinstance(node, dict):
            rec = node.get("rec_texts", node.get("rec_text"))
            if isinstance(rec, str):
                _add_text(lines, rec)
            elif isinstance(rec, list):
                for item in rec:
                    _add_text(lines, item)
            return
        if not isinstance(node, (list, tuple, str)):
            rec_attr = getattr(node, "rec_texts", None)
            if rec_attr:
                for item in rec_attr:
                    _add_text(lines, item)
            return
        if isinstance(node, str):
            return
        if from_line(node):
            return
        for child in node:
            walk(child)

    walk(result)
    return lines


def _run_ocr(image_path: str) -> str:
    engine = _get_engine()
    with _engine_lock:
        if hasattr(engine, "predict"):
            result = engine.predict(image_path)
        else:
            try:
                result = engine.ocr(image_path, cls=True)
            except TypeError:
                result = engine.ocr(image_path)
    return "\n".join(_collect_texts(result)).strip()


def _ocr_file(data: bytes, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        with open(path, "wb") as file:
            file.write(data)
        return _run_ocr(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _ocr_pdf(data: bytes) -> str:
    """自己调用 pdftoppm，不经过会弹出控制台的 pdfinfo。"""
    global _pdf_disabled_reason
    if _pdf_disabled_reason:
        return ""
    if not poppler_available():
        _pdf_disabled_reason = "poppler 不存在"
        logger.warning(
            "未检测到 poppler，已跳过 PDF OCR。图片 OCR 仍可使用。可安装 poppler 并配置 POPPLER_PATH。"
        )
        return ""
    poppler = _poppler_path()
    exe = str(Path(poppler) / "pdftoppm.exe") if poppler else "pdftoppm"
    max_pages = max(1, _int_env("OCR_PDF_MAX_PAGES", 5))
    folder = tempfile.mkdtemp(prefix="youzhihui-pdf-")
    try:
        pdf_path = Path(folder) / "in.pdf"
        pdf_path.write_bytes(data)
        prefix = str(Path(folder) / "page")
        result = _run_hidden(
            [exe, "-png", "-r", "150", "-f", "1", "-l", str(max_pages), str(pdf_path), prefix]
        )
        if result.returncode != 0:
            logger.warning("PDF 转图片失败，已跳过该附件")
            return ""
        pages = []
        for index, image_path in enumerate(sorted(Path(folder).glob("page*.png")), start=1):
            try:
                text = _run_ocr(str(image_path))
            except Exception as exc:
                logger.warning("第 %s 页 OCR 失败，继续其余页面: %s", index, exc)
                text = ""
            if text:
                pages.append(f"[第{index}页]\n{text}")
        return "\n\n".join(pages).strip()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _kind(filename: str, content_type: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in {"image/jpeg", "image/jpg", "image/png"}:
        return "image"
    if ctype == "application/pdf":
        return "pdf"
    return ""


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xC0, 0xC1, 0xC2}:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        if marker in {0xD8, 0xD9}:
            index += 2
            continue
        if index + 4 > len(data):
            break
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if length < 2:
            break
        index += 2 + length
    return None


def _image_size(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data.startswith(b"\xff\xd8"):
        return _jpeg_size(data)
    return None


def invoice_brief(text: str) -> str:
    """从发票文字里抽出号码、日期和价税合计，方便后面分类时先看见重点。"""
    raw = text or ""
    compact = re.sub(r"\s+", "", raw)
    if not any(word in compact for word in ("发票", "价税合计", "发票号码")):
        return ""
    parts = []
    number = re.search(r"发票号码[:：]?([0-9A-Z]{8,30})", compact, re.I)
    if number:
        parts.append(f"发票号码 {number.group(1)}")
    date = re.search(r"(?:开票日期|开具日期)[:：]?(\d{4}[年./-]\d{1,2}[月./-]\d{1,2}日?)", compact)
    if date:
        parts.append(f"日期 {date.group(1)}")
    amount = re.search(r"价税合计[^\d]{0,8}[¥￥]?(\d{1,9}(?:\.\d{1,2})?)", compact)
    if not amount:
        amount = re.search(r"[（(]小写[）)][¥￥]?(\d{1,9}(?:\.\d{1,2})?)", compact)
    if amount:
        parts.append(f"价税合计 {amount.group(1)}")
    if not parts:
        return ""
    return "发票要点：" + "；".join(parts)


def extract_attachment_text(filename: str, content_type: str, data: bytes) -> str:
    """识别一个附件。任何失败都返回空字符串。"""
    try:
        if not data:
            return ""
        if len(data) > MAX_ATTACHMENT_BYTES:
            logger.warning("附件超过 15MB，跳过 OCR: %s", filename or "未命名附件")
            return ""
        if len(data) < 256:
            return ""
        kind = _kind(filename, content_type)
        if kind == "image" and len(data) < 4096:
            logger.info("图片小于 4KB，多半是签名图标，跳过 OCR: %s", filename or "未命名附件")
            return ""
        if kind == "image":
            size = _image_size(data)
            if size and (size[0] <= 48 or size[1] <= 48):
                logger.info(
                    "图片只有 %sx%s，多半是图标，跳过 OCR: %s",
                    size[0],
                    size[1],
                    filename or "未命名附件",
                )
                return ""
        if kind == "image":
            suffix = Path(filename or "").suffix.lower()
            if suffix not in IMAGE_EXTS:
                suffix = ".png" if "png" in (content_type or "").lower() else ".jpg"
            return _ocr_file(data, suffix)
        if kind == "pdf":
            return _ocr_pdf(data)
        return ""
    except Exception as exc:
        logger.warning("附件 OCR 失败，已忽略 %s: %s", filename or "未命名附件", exc)
        return ""
