"""中文错误分类与不包含个人数据的运行诊断。"""

import logging
import logging.handlers
import sqlite3
import threading
import traceback
from pathlib import Path

from classroom_attendance.core.app_config import LOG_DIR


class ValidationError(ValueError):
    """可直接展示的业务校验错误。"""


class BusyError(RuntimeError):
    """数据库或共享资源繁忙。"""


class CameraError(RuntimeError):
    """相机连接或读取失败。"""


class TaskCancelled(RuntimeError):
    """任务取消，不代表保存成功。"""


class TaskTimeout(RuntimeError):
    """后台任务超过允许时长。"""


_lock = threading.Lock()
logger = logging.getLogger("classroom")
logger.setLevel(logging.INFO)
logger.propagate = False


def configure_logging(directory=LOG_DIR, *, max_bytes=1_000_000, backup_count=3):
    """日志只接受固定事件和不含异常值、局部变量的调用栈。"""
    with _lock:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        destination = (path / "classroom.log").resolve()
        for handler in logger.handlers:
            if getattr(handler, "baseFilename", None) == str(destination):
                handler.maxBytes=max_bytes
                handler.backupCount=backup_count
                return logger
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        handler = logging.handlers.RotatingFileHandler(
            destination, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def log_error(event, error):
    """保留 traceback 的每个调用位置，去掉可能包含姓名/路径/SQL/口令的源代码与异常值。"""
    try:
        if not logger.handlers:
            configure_logging()
        frames = traceback.extract_tb(error.__traceback__)
        stack = "\n".join(
            f'  File "{Path(frame.filename).name}", line {frame.lineno}, in {frame.name}'
            for frame in frames
        )
        # 不传 exc_info：默认 formatter 会输出异常值以及源代码，可能包含个人信息。
        logger.error("%s\nTraceback (most recent call last):\n%s\n%s [异常内容已脱敏]",
                     event, stack, type(error).__name__)
    except OSError:
        pass  # 无法写日志时仍须允许显示错误、退出，不递归报错。


def user_message(error):
    """将底层错误转成可读中文；未知异常不直接展示其文本。"""
    log_error("operation_error",error)
    if isinstance(error, TaskCancelled):
        return "操作已取消；请刷新核对结果，未完成的操作不会显示为成功。"
    if isinstance(error, TaskTimeout):
        return "操作超时，已请求取消。设备或磁盘可能无响应，请稍后重试。"
    if isinstance(error, sqlite3.Error):
        code = getattr(error, "sqlite_errorcode", 0) or 0
        if code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or "locked" in str(error).lower():
            return "数据忙，正在重试。请等待占用数据库的操作结束。"
        return "数据暂时不可用，请检查 attendance.db 的权限、完整性或占用情况。"
    if isinstance(error, PermissionError):
        return "没有文件访问权限，请检查保存位置和文件是否被占用。"
    if isinstance(error, OSError):
        return "文件或设备暂时不可用，请检查连接、保存位置和磁盘空间。"
    if isinstance(error, CameraError):
        return "摄像头无响应或连接中断，请检查设备后重新开始。"
    if isinstance(error, BusyError):
        return str(error) if any("\u4e00" <= char <= "\u9fff" for char in str(error)) else "资源正在使用，请停止另一个窗口的采集、训练或签到后重试。"
    if isinstance(error, ValueError) and any("\u4e00" <= char <= "\u9fff" for char in str(error)):
        return str(error)
    return "操作未完成，请稍后重试；诊断信息已保存到运行日志。"
