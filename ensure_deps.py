"""启动前核对依赖。缺的从项目里的 wheels 安装，已经符合版本的跳过。"""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import distributions
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOCK_PATH = BASE_DIR / "requirements.lock.txt"
WHEELS_DIR = BASE_DIR / "wheels"


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def _required() -> list[tuple[str, str]]:
    packages: list[tuple[str, str]] = []
    for line in LOCK_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "==" not in stripped:
            continue
        name, version = stripped.split("==", 1)
        packages.append((_norm(name), version.strip()))
    return packages


def _installed() -> dict[str, str]:
    found: dict[str, str] = {}
    for dist in distributions():
        name = dist.metadata["Name"]
        if name:
            found[_norm(name)] = dist.version
    return found


def _missing() -> list[str]:
    have = _installed()
    return [name for name, version in _required() if have.get(name) != version]


def _bundled_ready() -> bool:
    poppler = any((BASE_DIR / "tools" / "poppler").rglob("pdftoppm.exe"))
    models = BASE_DIR / "tools" / "paddlex" / "official_models"
    det = models / "PP-OCRv6_medium_det" / "inference.pdiparams"
    rec = models / "PP-OCRv6_medium_rec" / "inference.pdiparams"
    ok = True
    if not poppler:
        print("缺少 poppler。请确认拷贝的是完整 youzhihui 文件夹。")
        ok = False
    if not det.is_file() or not rec.is_file():
        print("缺少 OCR 模型。请确认拷贝的是完整 youzhihui 文件夹。")
        ok = False
    return ok


def main() -> int:
    if sys.version_info < (3, 9):
        print("请安装 64 位 Python 3.9 或 3.10，然后重新双击 启动.bat。")
        return 1
    if not LOCK_PATH.is_file() or not WHEELS_DIR.is_dir():
        print("安装包不完整。请确认 wheels 和 requirements.lock.txt 都在项目里。")
        return 1

    missing = _missing()
    if missing:
        preview = "、".join(missing[:6])
        extra = f" 等 {len(missing)} 个" if len(missing) > 6 else ""
        print(f"缺少组件：{preview}{extra}")
        print("正在从本机安装，不用联网下载。已经装好的会跳过。")
        result = subprocess.call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                f"--find-links={WHEELS_DIR}",
                "-r",
                str(LOCK_PATH),
            ]
        )
        if result != 0:
            print("安装没有完成。请确认这台电脑是 64 位 Windows，并且安装了 Python 3.9 或 3.10。")
            return 1
        still = _missing()
        if still:
            print("这些组件仍未装好：" + "、".join(still[:8]))
            return 1
        print("依赖已装好。")
    else:
        print("依赖已齐，跳过安装。")

    import ocr_local  # noqa: F401  创建模型目录联接，不会加载 OCR 引擎

    if not _bundled_ready():
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
