"""接回 Windows 无控制台打包进程由 QProcess 提供的管道。"""

import sys


def restore_worker_stdio() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    import msvcrt
    import os
    from ctypes import wintypes

    get_handle = ctypes.WinDLL("kernel32", use_last_error=True).GetStdHandle
    get_handle.argtypes = [wintypes.DWORD]
    get_handle.restype = wintypes.HANDLE
    invalid_handle = ctypes.c_void_p(-1).value
    for stream, number, mode in (("stdin", -10, "r"), ("stdout", -11, "w"), ("stderr", -12, "w")):
        existing = getattr(sys, stream)
        if existing is not None:
            existing.reconfigure(encoding="utf-8", errors="replace")
            continue
        handle = get_handle(number)
        if handle is None or handle == invalid_handle:
            raise OSError(f"监听进程缺少 {stream} 管道。")
        flags = os.O_RDONLY if mode == "r" else os.O_WRONLY
        descriptor = msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
        setattr(sys, stream, os.fdopen(descriptor, mode, buffering=1, encoding="utf-8", errors="replace"))
