"""跨进程资源锁；进程退出后操作系统自动释放，不删除锁文件。"""

import os
from pathlib import Path
from app_errors import BusyError


class ResourceLease:
    def __init__(self, path: Path, message="资源正在使用，请稍后重试。"):
        self.path, self.message, self.file = Path(path), message, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            self.file = None
            raise BusyError(self.message) from error
        return self

    def __exit__(self, *args):
        if self.file is not None:
            self.file.close()
            self.file = None
