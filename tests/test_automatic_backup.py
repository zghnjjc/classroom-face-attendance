"""每日备份、重试、跨进程所有权、受控保留与教师端反馈；仅使用临时合成数据。"""

import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from classroom_attendance.ui.admin_app import AdminApp
from classroom_attendance.core.app_errors import TaskCancelled, TaskTimeout
from classroom_attendance.storage.attendance_store import AttendanceStore
from classroom_attendance.storage.automatic_backup import AutomaticBackup, SETTINGS_KEY, STATE_KEY
from classroom_attendance.storage.automatic_backup_ui import backup_status
from classroom_attendance.storage.backup_service import inspect_backup, restore_backup
from classroom_attendance.core.resource_lock import ResourceLease
from classroom_attendance.core.task_runtime import TaskControl, current_control


class AutomaticBackupFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='classroom-auto-backup-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.store = AttendanceStore(self.folder/'source'/'attendance.db', seed_path=None)
        self.store.save_student('001', '合成备份学生', '测试班')
        self.service = AutomaticBackup(self.store)
        self.now = datetime(2026, 10, 23, 18, 0, 0)

    def configure(self, **changes):
        values = dict(enabled=True, time='18:00', keep=2, directory=str(self.folder/'archives'))
        values.update(changes)
        return self.service.save_settings(**values)

    def archives(self):
        return sorted(self.service.folder(self.service.settings()).glob('auto_*.zip'))

    def set_raw(self, key, value):
        with self.store.connect(write=True) as connection:
            self.store._set(connection, key, value)


class AutomaticBackupTests(AutomaticBackupFixture):
    def test_default_is_disabled_and_does_not_create_backup_directory(self):
        self.assertEqual(self.service.run_due(now=self.now)['outcome'], 'disabled')
        self.assertFalse((self.service.data/'backups').exists())
        self.assertIsNone(self.store.last_backup())

    def test_invalid_settings_and_sample_destination_are_rejected(self):
        for values in [dict(time='8:00'), dict(time='24:00'), dict(keep=0), dict(keep=91), dict(keep=True),
                       dict(enabled=1), dict(directory='relative'), dict(directory=str(self.service.data/'dataset'/'nested'))]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.configure(**values)
        self.assertFalse(self.service.settings()['enabled'])

    def test_boundary_catch_up_restart_and_next_day(self):
        self.configure()
        self.assertEqual(self.service.run_due(now=self.now-timedelta(seconds=1))['outcome'], 'idle')
        self.assertEqual(self.service.run_due(now=self.now+timedelta(hours=2))['outcome'], 'success')
        restarted = AutomaticBackup(AttendanceStore(self.store.path, seed_path=None))
        self.assertEqual(restarted.run_due(now=self.now+timedelta(hours=3))['outcome'], 'idle')
        self.assertEqual(restarted.run_due(now=self.now+timedelta(days=1))['outcome'], 'success')
        self.assertEqual(len(self.archives()), 2)

    def test_force_can_retry_today_and_before_daily_time(self):
        self.configure()
        self.assertEqual(self.service.run_due(now=self.now-timedelta(hours=2), force=True)['outcome'], 'success')
        self.assertEqual(self.service.run_due(now=self.now, force=True)['outcome'], 'success')
        self.assertEqual(len(self.archives()), 2)

    def test_saved_identical_settings_preserve_today_completion(self):
        self.configure()
        self.service.run_due(now=self.now)
        self.configure()
        self.assertEqual(self.service.run_due(now=self.now)['outcome'], 'idle')
        self.configure(directory=str(self.folder/'other'))
        self.assertEqual(self.service.run_due(now=self.now)['outcome'], 'success')
        self.assertTrue((self.folder/'archives').is_dir())

    def test_backup_can_be_verified_and_restored_and_relocation_pauses_schedule(self):
        self.configure()
        self.service.run_due(now=self.now)
        info = inspect_backup(self.archives()[0])
        self.assertEqual(info['students'], 1)
        target = self.folder/'restored'
        restore_backup(self.archives()[0], target, expected_sha256=info['archive_sha256'])
        restored = AutomaticBackup(AttendanceStore(target/'attendance.db', seed_path=None))
        self.assertTrue(restored.settings()['relocated'])
        self.assertFalse(restored.settings()['enabled'])
        self.assertEqual(restored.run_due(now=self.now+timedelta(days=1))['outcome'], 'disabled')

    def test_failures_back_off_ten_minutes_and_preserve_previous_success(self):
        self.configure()
        self.service.run_due(now=self.now-timedelta(days=1))
        with patch('classroom_attendance.storage.automatic_backup.create_backup', side_effect=OSError('private path')) as create:
            result = self.service.run_due(now=self.now)
            self.assertEqual(result['outcome'], 'error')
            self.assertEqual(self.service.run_due(now=self.now+timedelta(minutes=9))['outcome'], 'idle')
            self.assertEqual(self.service.run_due(now=self.now+timedelta(minutes=10))['outcome'], 'error')
            self.assertEqual(create.call_count, 2)
        self.assertNotIn('private path', backup_status(result['snapshot']))
        self.assertEqual(self.store.last_backup()['completed_at'][:10], '2026-10-22')
        self.assertEqual(self.service.run_due(now=self.now+timedelta(minutes=10), force=True)['outcome'], 'success')

    def test_sample_lock_defers_one_minute_without_interfering_with_capture(self):
        self.configure()
        with ResourceLease(self.service.data/'.samples.lock'):
            self.assertEqual(self.service.run_due(now=self.now)['outcome'], 'deferred')
        self.assertEqual(self.service.run_due(now=self.now+timedelta(seconds=59))['outcome'], 'idle')
        self.assertEqual(self.service.run_due(now=self.now+timedelta(minutes=1))['outcome'], 'success')

    def test_competing_window_cannot_overwrite_status_or_change_settings(self):
        self.configure()
        with ResourceLease(self.service.lock):
            self.assertEqual(self.service.run_due(now=self.now)['outcome'], 'locked')
            with self.assertRaises(RuntimeError):
                self.configure(keep=3)
        self.assertEqual(self.service.state()['status'], 'configured')

    def test_concurrent_scheduler_runs_publish_only_one_archive(self):
        self.configure()
        gate = threading.Barrier(2)
        def run():
            gate.wait()
            return AutomaticBackup(self.store).run_due(now=self.now)['outcome']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(results.count('success'), 1)
        self.assertEqual(len(self.archives()), 1)

    def test_retention_only_removes_owned_unchanged_older_archives(self):
        self.configure()
        self.service.run_due(now=self.now)
        first = self.archives()[0]
        folder = first.parent
        unknown = folder/'auto_20200101_000000_0123456789abcdef0123456789abcdef.zip'
        manual, snapshot = folder/'manual.zip', folder/'pre_restore.zip'
        for file in (unknown, manual, snapshot):
            file.write_bytes(b'unowned')
        for offset in (1, 2):
            self.service.run_due(now=self.now+timedelta(days=offset))
        self.assertFalse(first.exists())
        for file in (unknown, manual, snapshot):
            self.assertEqual(file.read_bytes(), b'unowned')
        self.assertEqual(len(list(folder.glob('auto_*.zip'))), 3)

    def test_modified_owned_archive_is_preserved_and_reported(self):
        self.configure(keep=1)
        self.service.run_due(now=self.now)
        first = self.archives()[0]
        first.write_bytes(b'changed')
        result = self.service.run_due(now=self.now+timedelta(days=1))
        self.assertEqual(result['outcome'], 'warning')
        self.assertEqual(first.read_bytes(), b'changed')
        self.assertEqual(self.service.run_due(now=self.now+timedelta(days=1))['outcome'], 'idle')

    def test_missing_newer_archive_does_not_displace_retained_good_archive(self):
        self.configure(keep=2)
        self.service.run_due(now=self.now)
        first = self.archives()[0]
        self.service.run_due(now=self.now+timedelta(days=1))
        next(path for path in self.archives() if path != first).unlink()
        self.service.run_due(now=self.now+timedelta(days=2))
        self.assertTrue(first.exists())
        self.assertEqual(len(self.archives()), 2)

    def test_cleanup_access_error_reports_warning_after_success(self):
        self.configure(keep=1)
        self.service.run_due(now=self.now)
        first = self.archives()[0]
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == first:
                raise PermissionError('occupied')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', unlink):
            result = self.service.run_due(now=self.now+timedelta(days=1))
        self.assertEqual(result['outcome'], 'warning')
        self.assertTrue(first.exists())
        self.assertEqual(self.service.state()['last_success'][:10], '2026-10-24')

    def test_failed_new_backup_does_not_prune_old_files(self):
        self.configure(keep=1)
        self.service.run_due(now=self.now)
        first = self.archives()[0]
        with patch('classroom_attendance.storage.automatic_backup.create_backup', side_effect=OSError('disk')):
            self.assertEqual(self.service.run_due(now=self.now+timedelta(days=1))['outcome'], 'error')
        self.assertTrue(first.exists())

    def test_index_commit_failure_keeps_old_and_unclaimed_new_archive(self):
        self.configure(keep=1)
        self.service.run_due(now=self.now)
        first = self.archives()[0]
        with patch.object(self.service, '_write_index', side_effect=OSError('disk')):
            self.assertEqual(self.service.run_due(now=self.now+timedelta(days=1))['outcome'], 'error')
        self.assertTrue(first.exists())
        self.assertEqual(len(self.archives()), 2)
        self.assertEqual(self.service.state()['last_success'][:10], '2026-10-23')

    def test_corrupt_index_prevents_creation_and_deletion(self):
        self.configure()
        self.service.run_due(now=self.now)
        first = self.archives()[0]
        (first.parent/'index.json').write_text('[]', encoding='utf-8')
        self.assertEqual(self.service.run_due(now=self.now+timedelta(days=1))['outcome'], 'error')
        self.assertEqual(self.archives(), [first])
        self.assertEqual((first.parent/'index.json').read_text(), '[]')

    def test_index_cannot_name_paths_outside_owned_directory(self):
        self.configure()
        self.service.run_due(now=self.now)
        folder = self.archives()[0].parent
        path = folder/'index.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['entries'][0]['name'] = '../attendance.db'
        path.write_text(json.dumps(value), encoding='utf-8')
        self.assertEqual(self.service.run_due(now=self.now+timedelta(days=1))['outcome'], 'error')
        self.assertEqual(len(self.store.students()), 1)

    def test_linked_old_file_is_not_deleted(self):
        self.configure(keep=1)
        self.service.run_due(now=self.now)
        first = self.archives()[0]
        from classroom_attendance.storage.automatic_backup import _linked
        with patch('classroom_attendance.storage.automatic_backup._linked', side_effect=lambda path: path == first or _linked(path)):
            result = self.service.run_due(now=self.now+timedelta(days=1))
        self.assertEqual(result['outcome'], 'warning')
        self.assertTrue(first.exists())

    def test_corrupt_settings_are_disabled_and_corrupt_state_can_recover(self):
        self.set_raw(SETTINGS_KEY, '{bad')
        self.assertTrue(self.service.settings()['invalid'])
        self.assertEqual(self.service.run_due(now=self.now)['outcome'], 'disabled')
        self.configure()
        self.set_raw(STATE_KEY, json.dumps(dict(version=1, source=self.service.source, status='error')))
        self.assertTrue(self.service.state()['invalid'])
        self.assertEqual(self.service.run_due(now=self.now)['outcome'], 'success')

    def test_cancellation_does_not_claim_success_or_remove_old_backup(self):
        self.configure()
        control = TaskControl(300)
        def cancel(*args, **kwargs):
            control.cancelled.set()
            raise TaskCancelled()
        token = current_control.set(control)
        try:
            with patch('classroom_attendance.storage.automatic_backup.create_backup', side_effect=cancel), self.assertRaises(TaskCancelled):
                self.service.run_due(now=self.now)
        finally:
            current_control.reset(token)
        self.assertFalse(self.archives())
        self.assertIsNone(self.store.last_backup())
        self.assertEqual(self.service.run_due(now=self.now+timedelta(minutes=1))['outcome'], 'idle')
        self.assertEqual(self.service.run_due(now=self.now+timedelta(minutes=10))['outcome'], 'success')

    def test_timeout_failure_is_persisted_after_work_has_stopped(self):
        self.configure()
        control = TaskControl(300)
        def timeout(*args, **kwargs):
            control.deadline = 0
            raise TaskTimeout()
        token = current_control.set(control)
        try:
            with patch('classroom_attendance.storage.automatic_backup.create_backup', side_effect=timeout):
                result = self.service.run_due(now=self.now)
        finally:
            current_control.reset(token)
        self.assertEqual(self.service.state()['status'], 'error')
        self.assertEqual(result['outcome'], 'error')
        self.assertIsNone(self.store.last_backup())


class AutomaticBackupGuiTests(AutomaticBackupFixture):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        with patch('classroom_attendance.ui.admin_app.model_health', return_value=(False, '模型未训练', None)):
            self.admin = AdminApp(self.root, self.store, authenticated=True, async_io=False)
        self.panel = self.admin.automatic_backup
        self.root.update_idletasks()

    def tearDown(self):
        self.panel.close()
        try:
            for timer in self.root.tk.call('after', 'info'):
                self.root.after_cancel(timer)
            self.root.destroy()
        except tk.TclError:
            pass

    def configure_panel(self):
        self.panel.enabled.set(True)
        self.panel.directory.set(str(self.folder/'ui-archives'))
        self.panel.save()

    def test_settings_save_validation_and_immediate_backup(self):
        self.panel.keep.set('bad')
        self.panel.save()
        self.assertIn('整数', self.panel.status.get())
        self.panel.keep.set('2')
        self.configure_panel()
        self.panel.start(force=True)
        self.assertEqual(self.panel.snapshot['state']['status'], 'success')
        self.assertIn('最近成功', self.panel.status.get())
        self.assertIsNotNone(self.admin.dashboard.snapshot['last_backup'])

    def test_failure_and_cleanup_warning_are_visible_on_home(self):
        self.configure_panel()
        with patch('classroom_attendance.storage.automatic_backup.create_backup', side_effect=PermissionError('private')):
            self.panel.start(force=True)
        self.assertIn('失败', self.panel.status.get())
        self.assertIn('自动备份需处理', self.admin.dashboard.health_text.get())
        self.assertIn('权限', self.admin.dashboard.automatic_status())
        self.panel.start(force=True)
        self.assertNotIn('自动备份需处理', self.admin.dashboard.health_text.get())

    def test_foreground_job_defers_automatic_backup(self):
        self.configure_panel()
        self.admin.busy = True
        self.panel.start(force=True)
        self.assertFalse(self.panel.running)
        self.assertIsNone(self.store.last_backup())
        self.admin.busy = False

    def test_settings_scroll_makes_buttons_accessible_on_small_window(self):
        self.admin.root.geometry('940x600')
        self.admin.notebook.select(self.admin.pages['settings'])
        self.root.update()
        self.admin.settings_scroll.canvas.yview_moveto(0.45)
        self.root.update()
        self.assertTrue(self.panel.save_button.winfo_ismapped())
        self.assertGreater(self.admin.settings_scroll.canvas.winfo_height(), 100)
        self.panel.location.set('实际自动备份目录：\nC:\\' + '非常长的合成目录名\\'*25)
        self.root.update()
        self.assertGreater(self.panel.feedback_labels[1].winfo_height(), 40)

    def test_worker_keeps_tk_responsive_and_close_cancels_without_duplicate_work(self):
        self.configure_panel()
        self.admin.async_io = True
        started, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            started.set()
            release.wait(3)
            from classroom_attendance.core.task_runtime import checkpoint
            checkpoint()
        with patch.object(self.panel.service, 'run_due', side_effect=slow) as run:
            self.panel.start(force=True)
            self.assertTrue(started.wait(2))
            heartbeat = []
            self.root.after(10, lambda: heartbeat.append(True))
            end = time.monotonic()+.12
            while time.monotonic() < end:
                self.root.update()
                time.sleep(.005)
            self.assertTrue(heartbeat)
            self.panel.start(force=True)
            self.assertEqual(run.call_count, 1)
            self.assertFalse(self.admin.close())
            release.set()
            end = time.monotonic()+2
            while self.panel.running and time.monotonic() < end:
                self.root.update()
                time.sleep(.01)
        self.assertFalse(self.panel.running)
        self.assertIn('取消', self.panel.status.get())


if __name__ == '__main__':
    unittest.main()
