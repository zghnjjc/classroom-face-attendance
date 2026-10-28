"""本地维护：空间预检、独立任务时限与白名单诊断包。"""

import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from classroom_attendance.core.app_config import APP_VERSION
from classroom_attendance.core.app_errors import ValidationError
from classroom_attendance.core.task_runtime import checkpoint

MIB = 1024 * 1024
MIN_FREE_BYTES = 100 * MIB
WARN_FREE_BYTES = 512 * MIB
TIMEOUT_KEY = 'maintenance_timeouts_v1'
DEFAULT_TIMEOUTS = {'training': 180, 'export': 120}
LOG_EVENTS = frozenset({'camera_preview_failed', 'camera_worker_failed', 'camera_driver_failed',
                        'training_failed', 'background_job_failed', 'background_io_failed',
                        'ui_operation_failed', 'tk_callback_failed', 'operation_error',
                        'admin_refresh_failed', 'student_refresh_failed', 'automatic_backup_failed'})
ERROR_TYPES = frozenset({'CameraError', 'BusyError', 'TaskTimeout', 'TaskCancelled', 'ValidationError',
                         'ValueError', 'RuntimeError', 'OSError', 'PermissionError', 'OperationalError',
                         'DatabaseError', 'IntegrityError', 'FileNotFoundError', 'error'})


def disk_status(path):
    directory = Path(path).expanduser().resolve()
    while not directory.exists():
        directory = directory.parent
    usage = shutil.disk_usage(directory)
    return {'total_bytes': usage.total, 'free_bytes': usage.free,
            'state': 'critical' if usage.free < MIN_FREE_BYTES else 'warning' if usage.free < WARN_FREE_BYTES else 'ok'}


def require_disk_space(path, required=MIN_FREE_BYTES):
    checkpoint()
    status = disk_status(path)
    if status['free_bytes'] < required:
        raise ValidationError(f'目标磁盘空间不足，至少需保留 {(required + MIB - 1) // MIB} MiB。请释放空间或更换位置后重试。')
    return status


def backup_space_required(files, dataset):
    total = 0
    for file in files:
        checkpoint()
        file = Path(file)
        if file.is_file():
            total += file.stat().st_size
    directory = Path(dataset)
    if directory.is_dir():
        for file in directory.iterdir():
            checkpoint()
            if file.is_file() and not file.name.startswith('.'):
                total += file.stat().st_size
    return total * 2 + MIN_FREE_BYTES


def validate_timeouts(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_TIMEOUTS):
        raise ValidationError('维护超时设置损坏，请检查数据库或从备份恢复。')
    if any(type(seconds) is not int or not 30 <= seconds <= 3600 for seconds in value.values()):
        raise ValidationError('训练和导出超时分别使用 30–3600 的整数秒。')
    return dict(value)


class MaintenanceService:
    def __init__(self, store):
        self.store = store

    def timeouts(self):
        with self.store.connect() as connection:
            raw = self.store._get(connection, TIMEOUT_KEY)
        try:
            return validate_timeouts(json.loads(raw)) if raw is not None else dict(DEFAULT_TIMEOUTS)
        except (TypeError, ValueError) as error:
            raise ValidationError('维护超时设置无法读取，请检查数据库或从备份恢复。') from error

    def save_timeouts(self, training, export):
        value = validate_timeouts({'training': training, 'export': export})
        with self.store.connect(write=True) as connection:
            checkpoint()
            self.store._set(connection, TIMEOUT_KEY, json.dumps(value, sort_keys=True))
            self.store._audit(connection, '修改维护超时', after=value)
        return value

    def snapshot(self):
        return {'data_disk': disk_status(self.store.path.parent), 'timeouts': self.timeouts()}

    def diagnostics(self):
        import cv2
        from tkinter import TkVersion
        with self.store.connect() as connection:
            counts = {name: connection.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                      for name in ('students', 'sessions', 'checkins', 'face_evidence')}
            raw_schema = self.store._get(connection, 'schema_version')
        version = cv2.__version__
        return {'format_version': 1, 'created_at': datetime.now().isoformat(timespec='seconds'),
                'app_version': APP_VERSION, 'platform': 'Windows' if os.name == 'nt' else 'other',
                'architecture_bits': struct.calcsize('P') * 8, 'python': '.'.join(map(str, sys.version_info[:3])),
                'sqlite': sqlite3.sqlite_version, 'tk': TkVersion,
                'opencv': version if re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', version) else 'unknown',
                'face_module': hasattr(cv2, 'face'), 'schema_version': int(raw_schema) if raw_schema in {'3', '4', '5'} else 'unknown',
                'record_counts': counts, **self.snapshot(),
                'log_summary': summarize_logs(self.store.path.parent / 'logs')}

    def export_diagnostics(self, path):
        path = Path(path)
        if path.suffix.lower() != '.zip':
            raise ValidationError('诊断包必须使用 .zip 扩展名。')
        if any(path.resolve().is_relative_to((self.store.path.parent / name).resolve()) for name in ('dataset', 'logs')):
            raise ValidationError('诊断包不能保存在人脸样本或日志目录内。')
        require_disk_space(path.parent)
        report = self.diagnostics()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix='.diagnostics-', suffix='.zip', dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('diagnostics.json', json.dumps(report, ensure_ascii=False, indent=2))
                archive.writestr('说明.txt', '此包仅含版本、数量、磁盘容量、超时设置和白名单错误计数。\n'
                                 '不含姓名、学号、课程、路径、密码摘要、原始日志、数据库、图片或模型。\n'
                                 '数量和生成时间仍可能具有业务敏感性，请核对后再手动分享。程序不会上传。\n')
            checkpoint()
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return report


def summarize_logs(directory):
    events, errors = Counter(), Counter()
    unavailable = False
    for name in ('classroom.log', 'classroom.log.1', 'classroom.log.2', 'classroom.log.3'):
        checkpoint()
        file = Path(directory) / name
        try:
            if file.is_symlink() or not file.is_file():
                continue
            with file.open('rb') as stream:
                stream.seek(max(0, file.stat().st_size - 256 * 1024))
                lines = stream.read(256 * 1024).decode('utf-8', errors='replace').splitlines()
            for line in lines:
                match = re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ERROR ([a-z_]+)', line)
                if match and match[1] in LOG_EVENTS:
                    events[match[1]] += 1
                match = re.fullmatch(r'([A-Za-z]+) \[异常内容已脱敏\]', line)
                if match and match[1] in ERROR_TYPES:
                    errors[match[1]] += 1
        except OSError:
            unavailable = True
    return {'events': dict(events), 'error_types': dict(errors), 'unavailable': unavailable,
            'scope': '最多四份日志，各末尾256KiB；非原始日志，非完整历史'}
