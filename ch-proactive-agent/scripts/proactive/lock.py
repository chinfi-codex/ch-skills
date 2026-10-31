"""状态锁：本机运行态目录下的 flock，进程退出由 OS 释放。"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import time
from pathlib import Path

from .context import CTX


@contextlib.contextmanager
def locked(path: Path, *, timeout: float = 20.0, exclusive: bool = True):
    lock_root = CTX.runtime / "locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / (hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest() + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"另一主动目标进程仍持有状态锁：{lock_path}")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
