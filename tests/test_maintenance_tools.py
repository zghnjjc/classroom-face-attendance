import json
import queue
import shutil
import tempfile
import threading
import time
import tkinter as tk
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from admin_app import AdminApp, CaptureDialog
from app_errors import BusyError, TaskCancelled, ValidationError
from attendance_store import AttendanceStore, _write_excel
from _face_test_support import authorize_faces
from backup_service import create_backup, inspect_backup, restore_backup
from camera_diagnostics import CameraPreviewWorker
from camera_service import CameraWorker
from maintenance_tools import (DEFAULT_TIMEOUTS, MIN_FREE_BYTES, TIMEOUT_KEY, WARN_FREE_BYTES,
                               MaintenanceService, backup_space_required, disk_status, require_disk_space, summarize_logs)
from maintenance_ui import CameraDiagnosticDialog
from resource_lock import ResourceLease
from student_app import StudentApp
from task_runtime import TaskControl, checkpoint, current_control
from train import train_model


class DeferredCamera:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.events = queue.Queue()
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def take_frame(self):
        return None


class MaintenanceFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.store = AttendanceStore(self.folder / 'data' / 'attendance.db', seed_path=None)
        self.service = MaintenanceService(self.store)
        self.addCleanup(self.temporary.cleanup)


class MaintenanceServiceTests(MaintenanceFixture):
    def test_defaults_and_independent_timeouts_are_persisted_and_audited(self):
        self.assertEqual(self.service.timeouts(), DEFAULT_TIMEOUTS)
        self.service.save_timeouts(3600, 30)
        self.assertEqual(MaintenanceService(self.store).timeouts(), {'training': 3600, 'export': 30})
        self.assertEqual(self.store.audit_rows()[0]['action'], '修改维护超时')

    def test_invalid_timeouts_never_replace_previous_settings(self):
        self.service.save_timeouts(180, 120)
        for bad in (True, 29, 3601, 30.5, '180', None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.service.save_timeouts(bad, 120)
            self.assertEqual(self.service.timeouts(), DEFAULT_TIMEOUTS)

    def test_corrupt_settings_are_not_silently_replaced(self):
        for raw in ('{', 'null', '{}', '{"training":30,"export":60,"unknown":100}'):
            with self.store.connect(write=True) as connection:
                self.store._set(connection, TIMEOUT_KEY, raw)
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                self.service.timeouts()
            with self.store.connect() as connection:
                self.assertEqual(self.store._get(connection, TIMEOUT_KEY), raw)

    def test_space_thresholds_and_exact_minimum(self):
        for free, state in ((MIN_FREE_BYTES-1, 'critical'), (MIN_FREE_BYTES, 'warning'),
                            (WARN_FREE_BYTES-1, 'warning'), (WARN_FREE_BYTES, 'ok')):
            with self.subTest(free=free), patch('maintenance_tools.shutil.disk_usage', return_value=shutil._ntuple_diskusage(10**10, 0, free)):
                self.assertEqual(disk_status(self.folder)['state'], state)
                if free < MIN_FREE_BYTES:
                    with self.assertRaisesRegex(ValidationError, '空间不足'):
                        require_disk_space(self.folder)
                else:
                    self.assertEqual(require_disk_space(self.folder)['free_bytes'], free)

    def test_nonexistent_destination_checks_nearest_existing_directory(self):
        with patch('maintenance_tools.shutil.disk_usage', return_value=shutil._ntuple_diskusage(10**10, 0, 10**9)) as usage:
            require_disk_space(self.folder / 'new' / 'deep')
        usage.assert_called_once_with(self.folder)

    def test_backup_estimate_includes_two_copies_and_reserve(self):
        sample = self.folder / 'samples'
        sample.mkdir()
        (sample / '001.0.jpg').write_bytes(b'a'*11)
        (sample / '.hidden').write_bytes(b'a'*100)
        (sample / 'subdirectory').mkdir()
        file = self.folder / 'model.yml'
        file.write_bytes(b'a'*23)
        self.assertEqual(backup_space_required([file, self.folder/'missing'], sample), MIN_FREE_BYTES+68)

    def test_low_space_prevents_excel_and_preserves_existing_destination(self):
        output = self.folder / 'existing.xlsx'
        output.write_bytes(b'original')
        with patch('maintenance_tools.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)), self.assertRaises(ValidationError):
            _write_excel(output, '测试', '', ['序号', '学号'], [])
        self.assertEqual(output.read_bytes(), b'original')

    def test_low_space_prevents_report_save(self):
        from attendance_reports import write_report_workbook
        with patch('maintenance_tools.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)), self.assertRaises(ValidationError):
            write_report_workbook(self.folder/'report.xlsx', {})
        self.assertFalse((self.folder/'report.xlsx').exists())

    def test_low_space_prevents_training_before_loading_or_replacing_model(self):
        output = self.folder / 'model.yml'
        output.write_bytes(b'original')
        with patch('maintenance_tools.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)), \
                patch('train.create_recognizer') as recognizer, self.assertRaises(ValidationError):
            train_model(self.folder/'samples', output, _lease=False)
        recognizer.assert_not_called()
        self.assertEqual(output.read_bytes(), b'original')

    def test_low_space_backup_preserves_existing_archive(self):
        output = self.folder / 'backup.zip'
        output.write_bytes(b'original')
        with patch('maintenance_tools.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)), self.assertRaises(ValidationError):
            create_backup(self.store, output, model=self.folder/'none.yml', manifest=self.folder/'none.json', dataset=self.folder/'samples')
        self.assertEqual(output.read_bytes(), b'original')
        self.assertEqual(list(self.folder.glob('.backup-build-*')), [])

    def test_low_space_capture_does_not_open_device_or_write_sample(self):
        self.store.save_student('001', '合成姓名', '合成班')
        authorize_faces(self.store)
        worker = CameraWorker(self.store, 'capture', student_id='001')
        with patch('camera_service.DATASET_DIR', self.folder/'samples'), \
                patch('camera_service.CAMERA_LOCK_PATH', self.folder/'camera.lock'), \
                patch('camera_service.DATA_LOCK_PATH', self.folder/'data.lock'), \
                patch('maintenance_tools.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)), \
                patch('camera_service.open_camera') as camera:
            worker.run()
        camera.assert_not_called()
        self.assertEqual([event[0] for event in worker.events.queue], ['error', 'stopped'])
        self.assertEqual(list((self.folder/'samples').iterdir()), [])

    def test_diagnostic_package_contains_only_whitelisted_summary(self):
        secret = 'PRIVATE_SECRET_学生_口令_绝对路径'
        self.store.save_student('00123456', secret, secret)
        self.store.setup_admin(secret)
        logs = self.store.path.parent/'logs'
        logs.mkdir()
        (logs/'classroom.log').write_text('2026-10-24 12:00:00,000 ERROR camera_worker_failed\n'
                                         f'{secret}\n  File "{secret}.py", line 12, in {secret}\n'
                                         'CameraError [异常内容已脱敏]\n'
                                         f'2026-10-24 12:00:00,000 ERROR {secret}\n', encoding='utf-8')
        output = self.folder/'diagnostic.zip'
        report = self.service.export_diagnostics(output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {'diagnostics.json', '说明.txt'})
            content = '\n'.join(archive.read(name).decode('utf-8') for name in archive.namelist())
        for value in (secret, '00123456', str(self.folder), 'password_hash', 'Traceback', 'model_sha256'):
            self.assertNotIn(value, content)
        self.assertEqual(report['record_counts']['students'], 1)
        self.assertEqual(report['log_summary']['events'], {'camera_worker_failed': 1})
        self.assertEqual(report['log_summary']['error_types'], {'CameraError': 1})

    def test_diagnostic_package_rejects_bad_extension_and_sensitive_directories(self):
        for path in (self.folder/'bad.xlsx', self.store.path.parent/'dataset'/'bad.zip', self.store.path.parent/'logs'/'bad.zip'):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                self.service.export_diagnostics(path)

    def test_failed_diagnostic_write_preserves_existing_file_and_cleans_temporary(self):
        output = self.folder/'diagnostic.zip'
        output.write_bytes(b'original')
        with patch('maintenance_tools.zipfile.ZipFile.writestr', side_effect=OSError()), self.assertRaises(OSError):
            self.service.export_diagnostics(output)
        self.assertEqual(output.read_bytes(), b'original')
        self.assertEqual(list(self.folder.glob('.diagnostics-*')), [])

    def test_cancelled_diagnostic_export_preserves_destination(self):
        output = self.folder/'diagnostic.zip'
        output.write_bytes(b'original')
        control = TaskControl()
        token = current_control.set(control)
        original = self.service.diagnostics
        def cancel_after_read():
            report = original()
            control.cancelled.set()
            return report
        try:
            with patch.object(self.service, 'diagnostics', side_effect=cancel_after_read), self.assertRaises(TaskCancelled):
                self.service.export_diagnostics(output)
        finally:
            current_control.reset(token)
        self.assertEqual(output.read_bytes(), b'original')
        self.assertEqual(list(self.folder.glob('.diagnostics-*')), [])

    def test_log_summary_read_is_bounded_and_ignores_unknown_lines(self):
        logs = self.folder/'logs'
        logs.mkdir()
        known = '2026-10-24 12:00:00,000 ERROR camera_preview_failed\n'
        (logs/'classroom.log').write_text(known+'x'*300000+'\n'+known+'ValueError [异常内容已脱敏]\nUNKNOWN [异常内容已脱敏]\n', encoding='utf-8')
        report = summarize_logs(logs)
        self.assertEqual(report['events'], {'camera_preview_failed': 1})
        self.assertEqual(report['error_types'], {'ValueError': 1})

    def test_missing_and_unreadable_logs_do_not_export_exception_values(self):
        self.assertEqual(summarize_logs(self.folder/'missing')['events'], {})
        (self.folder/'classroom.log').write_bytes(b'')
        with patch('maintenance_tools.Path.open', side_effect=PermissionError('PRIVATE_PATH')):
            report = summarize_logs(self.folder)
        self.assertTrue(report['unavailable'])
        self.assertNotIn('PRIVATE_PATH', json.dumps(report))

    def test_timeouts_survive_backup_and_restore(self):
        self.service.save_timeouts(300, 90)
        archive = self.folder/'backup.zip'
        create_backup(self.store, archive, model=self.folder/'none.yml', manifest=self.folder/'none.json', dataset=self.folder/'samples')
        summary = inspect_backup(archive)
        target = self.folder/'restored'
        restore_backup(archive, target, expected_sha256=summary['archive_sha256'])
        restored = AttendanceStore(target/'attendance.db', seed_path=None)
        self.assertEqual(MaintenanceService(restored).timeouts(), {'training': 300, 'export': 90})


class MaintenanceCameraTests(MaintenanceFixture):
    def test_device_lock_is_held_until_real_release_finishes(self):
        lock = self.folder/'camera.lock'
        releasing, release = threading.Event(), threading.Event()
        capture = Mock()
        capture.read.return_value = (False, None)
        def slow_release():
            releasing.set()
            release.wait(4)
        capture.release.side_effect = slow_release
        worker = CameraPreviewWorker(0, lock, capture_factory=lambda index: capture)
        worker.start()
        try:
            self.assertTrue(releasing.wait(2))
            with self.assertRaises(BusyError), ResourceLease(lock):
                pass
            self.assertFalse(any(event[0] == 'stopped' for event in worker.events.queue))
        finally:
            release.set()
            worker.join(3)
        with ResourceLease(lock):
            pass
        self.assertEqual([event[0] for event in worker.events.queue], ['error', 'stopped'])

    def test_signing_lock_remains_held_until_capture_is_released(self):
        lock = self.folder/'camera.lock'
        worker = CameraWorker(self.store, 'sign', session_id=1)
        releasing, release = threading.Event(), threading.Event()
        def loop():
            try:
                raise RuntimeError('模拟读取失败')
            finally:
                releasing.set()
                release.wait(4)
        with patch('camera_service.CAMERA_LOCK_PATH', lock), patch.object(worker, '_sign_loop', side_effect=loop):
            worker.start()
            try:
                self.assertTrue(releasing.wait(2))
                with self.assertRaises(BusyError), ResourceLease(lock):
                    pass
            finally:
                release.set()
                worker.join(3)
        self.assertEqual([event[0] for event in worker.events.queue], ['error', 'stopped'])


class MaintenanceGuiTests(MaintenanceFixture):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.update()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.addCleanup(self.cleanup_root)
        self.owner = Mock(root=self.root, store=self.store, async_io=False, busy=False, capture_dialog=None)

    def cleanup_root(self):
        try:
            for token in self.root.tk.call('after', 'info'):
                self.root.after_cancel(token)
            self.root.destroy()
        except tk.TclError:
            pass

    def dialog(self):
        return CameraDiagnosticDialog(self.owner, worker_factory=DeferredCamera)

    def admin(self):
        with patch('admin_app.model_health', return_value=(False, '未训练', None)):
            return AdminApp(self.root, self.store, authenticated=True, async_io=False)

    def test_standalone_diagnostic_needs_no_model_students_or_session(self):
        dialog = self.dialog()
        dialog.start()
        self.assertIsNotNone(dialog.worker)
        self.assertEqual(self.store.students(), [])
        self.assertIsNone(self.store.active_session())
        worker = dialog.worker
        dialog.stop()
        worker.events.put(('stopped', None))
        dialog.poll()
        dialog.close()

    def test_disconnect_disables_retry_until_stopped_then_uses_new_index(self):
        dialog = self.dialog()
        dialog.start()
        previous = dialog.worker
        previous.events.put(('error', '摄像头已断开'))
        dialog.poll()
        dialog.start()
        self.assertIs(dialog.worker, previous)
        self.assertEqual(str(dialog.start_button['state']), 'disabled')
        previous.events.put(('stopped', None))
        dialog.poll()
        self.assertIsNone(dialog.worker)
        self.assertIn('设备已释放', dialog.message.get())
        dialog.index.set('2')
        dialog.start()
        self.assertEqual(dialog.worker.args[0], 2)
        dialog.worker.events.put(('stopped', None))
        dialog.poll()
        dialog.close()

    def test_close_waits_for_actual_worker_stop(self):
        dialog = self.dialog()
        dialog.start()
        worker = dialog.worker
        self.assertFalse(dialog.close())
        self.assertTrue(worker.stopped)
        self.assertFalse(dialog.closed)
        worker.events.put(('stopped', None))
        dialog.poll()
        self.assertTrue(dialog.closed)

    def test_selected_camera_only_saved_explicitly_after_stop(self):
        dialog = self.dialog()
        dialog.index.set('3')
        dialog.start()
        dialog.save()
        self.assertEqual(self.store.camera_index(), 0)
        dialog.worker.events.put(('stopped', None))
        dialog.poll()
        dialog.save()
        self.assertEqual(self.store.camera_index(), 3)
        dialog.close()

    def test_invalid_camera_number_does_not_start_worker(self):
        dialog = self.dialog()
        for bad in ('-1', '10', '', 'camera', '0.1'):
            dialog.index.set(bad)
            dialog.start()
            self.assertIsNone(dialog.worker)
        dialog.close()

    def test_diagnostic_small_window_keeps_controls_visible(self):
        self.root.deiconify()
        dialog = self.dialog()
        dialog.root.geometry('680x500')
        dialog.root.deiconify()
        dialog.root.update()
        for control in (dialog.start_button, dialog.stop_button, dialog.save_button, dialog.close_button):
            self.assertTrue(control.winfo_ismapped())
            self.assertLessEqual(control.winfo_rootx()+control.winfo_width(), dialog.root.winfo_rootx()+dialog.root.winfo_width())
            self.assertLessEqual(control.winfo_rooty()+control.winfo_height(), dialog.root.winfo_rooty()+dialog.root.winfo_height())
        self.assertGreater(dialog.view.winfo_height(), 100)
        dialog.close()

    def test_maintenance_page_loads_saves_and_checks_other_destination(self):
        admin = self.admin()
        panel = admin.maintenance
        self.assertIn('maintenance', admin.pages)
        self.assertEqual(panel.training.get(), '180')
        panel.training.set('300')
        panel.export_seconds.set('60')
        panel.save()
        self.assertEqual(self.service.timeouts(), {'training': 300, 'export': 60})
        with patch('maintenance_ui.filedialog.askdirectory', return_value=str(self.folder)):
            panel.check_destination()
        self.assertIn('所选位置', panel.space.get())
        admin.close()

    def test_invalid_timeout_ui_does_not_claim_success(self):
        admin = self.admin()
        admin.maintenance.training.set('bad')
        with patch('maintenance_ui.MaintenancePanel.handle_error') as error:
            admin.maintenance.save()
        error.assert_called_once()
        self.assertEqual(self.service.timeouts(), DEFAULT_TIMEOUTS)
        admin.close()

    def test_training_and_excel_export_use_different_configured_limits(self):
        self.service.save_timeouts(300, 90)
        admin = self.admin()
        with patch.object(admin, 'run_job') as run:
            admin.train_faces()
        self.assertEqual(run.call_args.kwargs['timeout'], 300)
        with patch('admin_app.filedialog.asksaveasfilename', return_value=str(self.folder/'test.xlsx')), \
                patch.object(admin, 'run_job') as run:
            admin.export()
        self.assertEqual(run.call_args.kwargs['timeout'], 90)
        self.assertEqual(run.call_args.args[1].func, self.store.export_attendance)
        admin.reports.use_semester()
        with patch('attendance_reports_ui.filedialog.asksaveasfilename', return_value=str(self.folder/'reports.xlsx')), \
                patch.object(admin, 'run_job') as run:
            admin.reports.export()
        self.assertEqual(run.call_args.kwargs['timeout'], 90)
        started, release = threading.Event(), threading.Event()
        def slow_export(*args):
            started.set()
            release.wait(3)
            checkpoint()
        with patch('admin_app.filedialog.asksaveasfilename', return_value=str(self.folder/'slow.xlsx')), \
                patch.object(self.store, 'export_attendance', side_effect=slow_export), \
                patch.object(MaintenanceService, 'timeouts', return_value={'training': 300, 'export': .03}), \
                patch('ui_common.messagebox.showerror'):
            admin.export()
            self.assertTrue(started.wait(1))
            deadline = time.monotonic()+2
            while admin.job_interrupted is None and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
            self.assertIsNotNone(admin.job_interrupted)
            self.assertTrue(admin.busy)
            admin.export()
            self.assertEqual(self.store.export_attendance.call_count, 1)
            release.set()
            while admin.busy and time.monotonic() < deadline+2:
                self.root.update()
                time.sleep(.005)
        self.assertFalse(admin.busy)
        admin.close()

    def test_diagnostic_export_requests_confirmation_and_export_limit(self):
        self.service.save_timeouts(300, 90)
        admin = self.admin()
        with patch('maintenance_ui.messagebox.askyesno', return_value=False), patch.object(admin, 'run_job') as run:
            admin.maintenance.export_diagnostics()
        run.assert_not_called()
        with patch('maintenance_ui.messagebox.askyesno', return_value=True), \
                patch('maintenance_ui.filedialog.asksaveasfilename', return_value=str(self.folder/'diagnostic.zip')), \
                patch.object(admin, 'run_job') as run:
            admin.maintenance.export_diagnostics()
        self.assertEqual(run.call_args.kwargs['timeout'], 90)
        admin.close()

    def test_admin_close_waits_for_diagnostic_device_release(self):
        admin = self.admin()
        admin.maintenance.dialog = CameraDiagnosticDialog(admin, worker_factory=DeferredCamera)
        dialog = admin.maintenance.dialog
        dialog.start()
        self.assertFalse(admin.close())
        self.assertFalse(admin.closed)
        dialog.worker.events.put(('stopped', None))
        dialog.poll()
        self.assertTrue(admin.close())

    def test_capture_disconnect_preserves_samples_and_only_collects_remaining(self):
        completed = []
        self.store.save_student('001', '合成人物', '测试班')
        with patch('admin_app.CameraWorker', DeferredCamera):
            dialog = CaptureDialog(self.root, self.store, self.store.students()[0], completed.append)
            first = dialog.worker
            first.events.put(('progress', (7, 20)))
            first.events.put(('error', '设备断开'))
            dialog.poll()
            self.assertEqual(str(dialog.retry_button['state']), 'disabled')
            dialog.retry()
            self.assertIs(dialog.worker, first)
            first.events.put(('stopped', 7))
            dialog.poll()
            self.assertEqual(completed, [])
            self.assertEqual(dialog.saved, 7)
            dialog.retry()
            self.assertEqual(dialog.worker.kwargs['target'], 13)
            dialog.worker.events.put(('progress', (13, 13)))
            dialog.worker.events.put(('stopped', 13))
            dialog.poll()
        self.assertEqual(completed, [20])

    def test_capture_can_exit_after_disconnect_without_restart(self):
        completed = []
        with patch('admin_app.CameraWorker', DeferredCamera):
            dialog = CaptureDialog(self.root, self.store, {'student_id': '001', 'name': '合成'}, completed.append)
        dialog.worker.events.put(('error', '断开'))
        dialog.worker.events.put(('stopped', 2))
        dialog.poll()
        dialog.stop()
        self.assertEqual(completed, [2])

    def test_student_disconnect_requires_manual_retry_and_revalidates_session(self):
        now = datetime.now().replace(microsecond=0)
        self.store.save_student('001', '合成', '测试班')
        sid = self.store.create_session('测试课', '测试班', now-timedelta(minutes=1), now+timedelta(hours=1))
        self.store.open_session(sid)
        with patch('student_app.model_health', return_value=(True, '就绪', {})):
            app = StudentApp(self.root, self.store, worker_factory=DeferredCamera, async_io=False)
            app.toggle_camera()
        worker = app.worker
        worker.events.put(('error', '设备断开'))
        app.poll_camera()
        self.assertEqual(app.start_button['state'], 'disabled')
        worker.events.put(('stopped', 0))
        app.poll_camera()
        self.assertEqual(app.start_button['text'], '设备恢复后重试')
        self.assertIsNone(app.worker)
        self.store.close_session(sid)
        with patch('student_app.model_health', return_value=(True, '就绪', {})):
            app.toggle_camera()
        self.assertIsNone(app.worker)
        self.assertEqual(app.start_button['state'], 'disabled')
        app.close()

    def test_slow_disk_check_does_not_block_tk_events(self):
        admin = self.admin()
        panel = admin.maintenance
        panel.async_io = True
        started, release = threading.Event(), threading.Event()
        original = disk_status
        def slow(path):
            started.set()
            release.wait(3)
            return original(path)
        ticks = []
        with patch('maintenance_ui.disk_status', side_effect=slow):
            panel.refresh()
            self.assertTrue(started.wait(1))
            self.root.after(5, lambda: ticks.append(True))
            deadline = time.monotonic()+1
            while not ticks and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
            release.set()
            while panel._running_flows and time.monotonic() < deadline+2:
                self.root.update()
                time.sleep(.005)
        self.assertTrue(ticks)
        self.assertFalse(panel._running_flows)
        admin.close()


if __name__ == '__main__':
    unittest.main()
