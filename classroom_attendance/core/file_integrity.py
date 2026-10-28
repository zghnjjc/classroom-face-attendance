"""文件内容哈希；运行时按文件元数据缓存，恢复与训练可强制重新读取。"""

import hashlib
from functools import lru_cache
from pathlib import Path

from classroom_attendance.core.task_runtime import checkpoint


def _read_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            checkpoint()
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=4096)
def _cached_hash(path, size, modified, changed):
    return _read_hash(path)


def file_sha256(path, *, use_cache=True):
    path = Path(path)
    before = path.stat()
    if use_cache:
        value = _cached_hash(str(path.resolve()), before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    else:
        value = _read_hash(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("校验期间文件发生变化，请停止采集和训练后重试。")
    return value
