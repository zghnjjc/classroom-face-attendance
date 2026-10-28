"""教师端存活期间的每日完整备份；跨进程去重及有归属校验的数量保留。"""

import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from classroom_attendance.core.app_errors import BusyError, TaskCancelled, TaskTimeout, log_error, user_message
from classroom_attendance.storage.backup_service import create_backup
from classroom_attendance.core.file_integrity import file_sha256
from classroom_attendance.core.resource_lock import ResourceLease
from classroom_attendance.core.task_runtime import TaskControl, checkpoint, current_control

SETTINGS_KEY = 'automatic_backup_settings_v1'
STATE_KEY = 'automatic_backup_state_v1'
NAME = re.compile(r'auto_\d{8}_\d{6}_[0-9a-f]{32}\.zip\Z')


def _json_write(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _linked(path):
    return path.is_symlink() or (os.name == 'nt' and bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def _local_time(value):
    if not isinstance(value, str):
        raise ValueError()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise ValueError()
    return parsed


class AutomaticBackup:
    def __init__(self, store):
        self.store = store
        self.data = store.path.resolve().parent
        self.source = hashlib.sha256(os.path.normcase(str(store.path.resolve())).encode('utf-8')).hexdigest()[:24]
        self.lock = self.data / '.automatic-backup.lock'

    def defaults(self):
        return dict(version=1, source=self.source, enabled=False, time='18:00', keep=7,
                    directory=str(self.data / 'backups'))

    def settings(self):
        with self.store.connect() as connection:
            raw = self.store._get(connection, SETTINGS_KEY)
        if raw is None:
            return self.defaults()
        try:
            value = json.loads(raw)
            self._validate(value)
            if value['source'] != self.source:
                return dict(self.defaults(), relocated=True)
            return value
        except (ValueError, TypeError, KeyError):
            return dict(self.defaults(), invalid=True)

    @staticmethod
    def _validate(value):
        if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
                or not isinstance(value.get('source'), str) or not re.fullmatch('[0-9a-f]{24}', value['source'])
                or type(value.get('enabled')) is not bool or type(value.get('keep')) is not int or not 1 <= value['keep'] <= 90
                or not isinstance(value.get('time'), str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value['time'])
                or not isinstance(value.get('directory'), str) or not value['directory'].strip()
                or not Path(value['directory']).is_absolute()):
            raise ValueError('自动备份设置无效；时间使用 HH:MM，保留数量为 1–90。')

    def save_settings(self, *, enabled, time, keep, directory):
        value = dict(version=1, source=self.source, enabled=enabled, time=time.strip(), keep=keep, directory=str(directory).strip())
        self._validate(value)
        destination = Path(value['directory']).resolve()
        if destination == (self.data / 'dataset') or destination.is_relative_to(self.data / 'dataset'):
            raise ValueError('自动备份不能存入人脸样本目录。')
        if destination.exists() and not destination.is_dir():
            raise ValueError('请选择文件夹作为自动备份保存位置。')
        value['directory'] = str(destination)
        with ResourceLease(self.lock, '自动备份正在执行，请完成后再保存设置。'):
            with self.store.connect(write=True) as connection:
                old = self.store._get(connection, SETTINGS_KEY)
                self.store._set(connection, SETTINGS_KEY, json.dumps(value, ensure_ascii=False, sort_keys=True))
                # 更换目录后当天也应补做；原目录的历史文件由用户保留。
                if old != json.dumps(value, ensure_ascii=False, sort_keys=True):
                    self.store._set(connection, STATE_KEY, json.dumps({'version': 1, 'source': self.source, 'status': 'configured'}))
        return value

    def state(self):
        with self.store.connect() as connection:
            raw = self.store._get(connection, STATE_KEY)
        if raw is None:
            return {}
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get('version') != 1 or value.get('source') != self.source
                    or value.get('status') not in {'configured', 'success', 'warning', 'error', 'deferred'}):
                raise ValueError()
            for key in ('last_success', 'last_attempt', 'retry_at'):
                if key in value:
                    _local_time(value[key])
            if not isinstance(value.get('message', ''), str) or len(value.get('message', '')) > 1000:
                raise ValueError()
            if value['status'] in {'error', 'deferred'} and not {'message', 'retry_at'} <= value.keys():
                raise ValueError()
            return value
        except (ValueError, TypeError):
            return {'invalid': True}

    def snapshot(self):
        settings = self.settings()
        return dict(settings=settings, state=self.state(), folder=str(self.folder(settings)))

    def folder(self, settings):
        return Path(settings['directory']) / ('classroom-auto-' + self.source)

    @staticmethod
    def due(settings, state, now):
        return (settings['enabled'] and not settings.get('invalid') and not settings.get('relocated')
                and now.strftime('%H:%M') >= settings['time']
                and state.get('last_success', '')[:10] != now.date().isoformat()
                and (not state.get('retry_at') or _local_time(state['retry_at']) <= now))

    def _state_write(self, value):
        with self.store.connect(write=True) as connection:
            self.store._set(connection, STATE_KEY, json.dumps(dict(value, version=1, source=self.source), ensure_ascii=False, sort_keys=True))

    def _finish_state(self, state):
        # 数据备份已停止，单独给状态记账 5 秒，避免已超时令牌吞掉失败提示。
        token = current_control.set(TaskControl(5))
        try:
            self._state_write(state)
            return self.snapshot()
        finally:
            current_control.reset(token)

    def _index(self, folder):
        folder.parent.mkdir(parents=True, exist_ok=True)
        if folder.exists() and (_linked(folder) or folder.resolve() != folder.absolute()):
            raise ValueError('自动备份子目录不能是链接，请选择其他保存位置。')
        folder.mkdir(exist_ok=True)
        path = folder / 'index.json'
        if not path.exists():
            return []
        try:
            if _linked(path) or path.stat().st_size > 2 * 1024**2:
                raise ValueError()
            value = json.loads(path.read_text(encoding='utf-8'))
            if value.get('version') != 1 or value.get('source') != self.source or not isinstance(value.get('entries'), list) or len(value['entries']) > 10000:
                raise ValueError()
            names = set()
            for entry in value['entries']:
                if (not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not NAME.fullmatch(entry['name'])
                        or entry['name'] in names or type(entry.get('size')) is not int or entry['size'] < 0
                        or not isinstance(entry.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
                    raise ValueError()
                _local_time(entry.get('created_at'))
                names.add(entry['name'])
            return value['entries']
        except (ValueError, TypeError, AttributeError, KeyError):
            raise ValueError('自动备份索引不可读，已停止自动写入与清理；请更换保存位置，或检查原目录。') from None

    def _write_index(self, folder, entries):
        _json_write(folder / 'index.json', dict(version=1, source=self.source, entries=entries))

    def _prune(self, folder, entries, keep):
        existing = [item for item in entries if (folder / item['name']).exists() or (folder / item['name']).is_symlink()]
        ordered = sorted(existing, key=lambda item: (item['created_at'], item['name']), reverse=True)
        retained, warnings = ordered[:keep], 0
        for entry in ordered[keep:]:
            checkpoint()
            path = folder / entry['name']
            try:
                if not path.exists() and not path.is_symlink():
                    continue
                if (_linked(path) or not path.is_file() or path.resolve().parent != folder.resolve()
                        or path.stat().st_size != entry['size'] or file_sha256(path, use_cache=False) != entry['sha256']):
                    retained.append(entry)
                    warnings += 1
                    continue
                checkpoint()
                path.unlink()
            except OSError as error:
                log_error('automatic_backup_cleanup_failed', error)
                retained.append(entry)
                warnings += 1
        self._write_index(folder, retained)
        return warnings

    def run_due(self, *, now=None, force=False):
        now = (now or datetime.now()).replace(microsecond=0)
        try:
            with ResourceLease(self.lock, '另一个教师窗口正在执行自动备份。'):
                settings, previous = self.settings(), self.state()
                if settings.get('invalid') or settings.get('relocated') or not settings['enabled']:
                    return dict(outcome='disabled', snapshot=self.snapshot())
                if not force and not self.due(settings, previous, now):
                    return dict(outcome='idle', snapshot=self.snapshot())
                state = {key: previous[key] for key in ('last_success',) if key in previous}
                state['last_attempt'] = now.isoformat()
                try:
                    with ResourceLease(self.data / '.samples.lock', '采集、训练或人脸资料操作尚未结束，自动备份将在 1 分钟后重试。'):
                        folder = self.folder(settings)
                        entries = self._index(folder)
                        archive = folder / ('auto_' + now.strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex + '.zip')
                        create_backup(self.store, archive, model=self.data / 'face_model.yml',
                                      manifest=self.data / 'model_manifest.json', dataset=self.data / 'dataset')
                        entry = dict(name=archive.name, created_at=now.isoformat(), size=archive.stat().st_size,
                                     sha256=file_sha256(archive, use_cache=False))
                        entries.append(entry)
                        # 先持久化新归属，再清理旧文件；崩溃留下的未知 ZIP 不参与删除。
                        self._write_index(folder, entries)
                        self.store.record_backup_success(completed_at=now)
                        state.update(status='success', last_success=now.isoformat(), message='当天自动备份已完成。')
                        self._state_write(state)
                        try:
                            warnings = self._prune(folder, entries, settings['keep'])
                            if warnings:
                                state.update(status='warning', message=f'备份已完成；{warnings} 个旧文件未清理（已变化、被占用或无权限），请检查自动备份目录。')
                        except (OSError, ValueError, TaskTimeout, TaskCancelled) as error:
                            log_error('automatic_backup_cleanup_failed', error)
                            state.update(status='warning', message='备份已完成，但旧备份清理未完成；请检查自动备份目录。')
                except TaskCancelled:
                    state.update(status='deferred', message='自动备份已取消，可手动重试；10 分钟后再检查。',
                                 retry_at=(now + timedelta(minutes=10)).isoformat())
                    self._finish_state(state)
                    raise
                except Exception as error:
                    log_error('automatic_backup_failed', error)
                    deferred = isinstance(error, BusyError)
                    state.update(status='deferred' if deferred else 'error', message=user_message(error),
                                 retry_at=(now + timedelta(minutes=1 if deferred else 10)).isoformat())
                return dict(outcome=state['status'], snapshot=self._finish_state(state))
        except BusyError:
            # 未获得跨进程所有权的窗口不能覆盖正在工作的窗口状态。
            return dict(outcome='locked')
