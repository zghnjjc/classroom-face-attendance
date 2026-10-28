"""窗口存活登记与数据维护互斥；锁由操作系统在进程退出时释放。"""

import uuid
from pathlib import Path

from app_errors import BusyError
from resource_lock import ResourceLease


class InstanceLease:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lease = None
        self.marker = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with ResourceLease(self.directory / ".maintenance.lock"):
            folder = self.directory / ".instances"
            folder.mkdir(exist_ok=True)
            self.marker = folder / (uuid.uuid4().hex + ".lock")
            self.lease = ResourceLease(self.marker)
            self.lease.__enter__()
        return self

    def __exit__(self, *args):
        if self.lease is not None:
            self.lease.__exit__(*args)
            self.lease = None
            self.marker.unlink(missing_ok=True)


class MaintenanceLease:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.gate = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.gate = ResourceLease(self.directory / ".maintenance.lock")
        self.gate.__enter__()
        try:
            for marker in (self.directory / ".instances").glob("*.lock"):
                try:
                    with ResourceLease(marker):
                        pass
                except BusyError:
                    raise BusyError("请关闭使用此数据目录的所有签到窗口，再重试恢复。") from None
                marker.unlink(missing_ok=True)
        except BaseException:
            self.gate.__exit__(None, None, None)
            self.gate = None
            raise
        return self

    def __exit__(self, *args):
        if self.gate is not None:
            self.gate.__exit__(*args)
            self.gate = None
