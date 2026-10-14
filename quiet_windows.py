"""后台运行时，收信和识别附件不再弹出黑窗口。"""

from __future__ import annotations

import os

_installed = False


def install() -> None:
    """所有子进程都带上不创建控制台的标记。pdf2image 自己拿走过的 Popen 也会生效。"""
    global _installed
    if _installed or os.name != "nt":
        return
    import subprocess

    if not getattr(subprocess.Popen, "_youzhihui_hidden", False):
        original_popen = subprocess.Popen

        class _HiddenPopen(original_popen):
            _youzhihui_hidden = True

            def __init__(self, *args, **kwargs):
                kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | 0x08000000
                startup = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0
                kwargs["startupinfo"] = startup
                super().__init__(*args, **kwargs)

        subprocess.Popen = _HiddenPopen

    import _winapi

    if getattr(_winapi, "_youzhihui_quiet", False):
        _installed = True
        return
    original = _winapi.CreateProcess

    def create_process(*args, **kwargs):
        if len(args) >= 6:
            flags = int(args[5] or 0) | 0x08000000
            args = args[:5] + (flags,) + args[6:]
        else:
            kwargs["creationflags"] = int(kwargs.get("creationflags", 0) or 0) | 0x08000000
        return original(*args, **kwargs)

    _winapi.CreateProcess = create_process
    _winapi._youzhihui_quiet = True
    _installed = True
