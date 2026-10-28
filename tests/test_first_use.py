"""首次使用路由、续办、原子导入与不保存图片的相机测试。"""

import json
import queue
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from admin_app import AdminApp, AdminLogin
from app_errors import BusyError, TaskCancelled
from attendance_store import AttendanceStore
from camera_diagnostics import CameraPreviewWorker
from first_use import FirstUseService, SETUP_KEY
from first_use_ui import FirstUseWizard
from GUI import InitialLauncher, Launcher
from resource_lock import ResourceLease
from storage_welcome import StorageWelcome
from task_runtime import TaskControl, current_control


PASSWORD = 'Example-setup-password-2026'


class SetupFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='classroom-first-use-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.store = AttendanceStore(self.folder / 'data' / 'attendance.db', seed_path=None)
        self.service = FirstUseService(self.store)

    def begin(self):
        self.service.prepare()
        self.store.setup_admin(PASSWORD)


class FirstUseServiceTests(SetupFixture):
    def test_new_store_starts_once_and_password_is_required_for_writes(self):
        state = self.service.prepare()
        self.assertFalse(state['completed'])
        self.assertEqual(state['camera'], 'pending')
        self.assertEqual(self.service.prepare(), state)
        with self.assertRaises(ValueError):
            self.service.record_camera('skipped', 0)

    def test_existing_password_students_or_audit_do_not_start_automatic_setup(self):
        for condition in ('password', 'students', 'audit'):
            with self.subTest(condition=condition):
                store = AttendanceStore(self.folder / (condition + '.db'), seed_path=None)
                if condition == 'password':
                    store.setup_admin(PASSWORD)
                elif condition == 'students':
                    store.save_student('001', '合成学生')
                else:
                    with store.connect(write=True) as connection:
                        store._audit(connection, '历史操作')
                self.assertIsNone(FirstUseService(store).prepare())

    def test_partial_progress_survives_reopen_and_uses_existing_password(self):
        self.begin()
        self.service.record_camera('skipped', 0)
        reopened = FirstUseService(AttendanceStore(self.store.path, seed_path=None))
        state = reopened.prepare()
        self.assertEqual((state['camera'], state['roster']), ('skipped', 'pending'))
        self.assertTrue(reopened.snapshot()['password_set'])

    def test_camera_index_saves_only_after_test_and_invalid_results_fail(self):
        self.begin()
        self.service.record_camera('skipped', 3)
        self.assertEqual(self.store.camera_index(), 0)
        self.service.record_camera('tested', 2)
        self.assertEqual(self.store.camera_index(), 2)
        for result, index in [('pending', 1), ('tested', 10), ('tested', True)]:
            with self.assertRaises(ValueError):
                self.service.record_camera(result, index)

    def test_empty_roster_requires_explicit_skip(self):
        self.begin()
        self.service.record_camera('skipped', 0)
        with self.assertRaises(ValueError):
            self.service.record_roster('existing')
        with self.assertRaises(ValueError):
            self.service.finish()
        self.service.record_roster('skipped')
        self.assertTrue(self.service.finish())

    def test_preview_marks_new_and_updates_without_writing(self):
        self.begin()
        self.store.save_student('001', '原合成姓名', '甲班')
        self.store.set_student_active('001', False)
        plan = self.service.preview_roster([('001', '新合成姓名', '乙班'), ('002', '另一合成', '乙班')])
        self.assertEqual([row['action'] for row in plan['preview']], ['更新姓名/班级', '新增'])
        self.assertEqual(plan['preview'][0]['active'], 0)
        self.assertEqual(len(self.store.students()), 1)
        self.service.import_roster(plan['rows'], plan['token'])
        self.assertEqual([row['student_id'] for row in self.store.students(active_only=True)], ['002'])
        self.assertEqual(self.service.snapshot()['state']['roster'], 'imported')

    def test_duplicate_or_invalid_rows_do_not_write_partial_import(self):
        self.begin()
        for rows in [[('001', '合成甲'), ('001', '重复')], [('001', '合成甲'), ('bad', '非法')]]:
            with self.assertRaises(ValueError):
                self.service.preview_roster(rows)
        self.assertEqual(self.store.students(), [])

    def test_preview_becomes_stale_after_existing_profile_or_active_change(self):
        self.begin()
        self.store.save_student('001', '合成甲')
        plan = self.service.preview_roster([('001', '合成新名', '甲班')])
        self.store.set_student_active('001', False)
        with self.assertRaisesRegex(ValueError, '预览后的名单已变化'):
            self.service.import_roster(plan['rows'], plan['token'])
        self.assertEqual(self.store.students()[0]['name'], '合成甲')
        self.assertEqual(self.service.snapshot()['state']['roster'], 'pending')

    def test_import_and_progress_rollback_together_on_metadata_failure(self):
        self.begin()
        plan = self.service.preview_roster([('001', '合成甲', '甲班')])
        with patch.object(self.service, '_save', side_effect=OSError('synthetic')), self.assertRaises(OSError):
            self.service.import_roster(plan['rows'], plan['token'])
        self.assertEqual(self.store.students(), [])
        self.assertEqual(self.service.snapshot()['state']['roster'], 'pending')

    def test_cancelled_import_rolls_back_names_and_progress(self):
        self.begin()
        plan = self.service.preview_roster([('001', '合成甲', '甲班')])
        control = TaskControl()
        control.cancelled.set()
        token = current_control.set(control)
        try:
            with self.assertRaises(TaskCancelled):
                self.service.import_roster(plan['rows'], plan['token'])
        finally:
            current_control.reset(token)
        self.assertEqual(self.store.students(), [])
        self.assertEqual(self.service.snapshot()['state']['roster'], 'pending')

    def test_finish_is_idempotent_and_completed_user_does_not_restart(self):
        self.begin()
        self.service.record_camera('skipped', 0)
        self.service.record_roster('skipped')
        self.assertTrue(self.service.finish())
        self.assertFalse(self.service.finish())
        self.assertTrue(self.service.prepare()['completed'])
        self.assertEqual(sum(row['action'] == '完成首次使用向导' for row in self.store.audit_rows()), 1)
        self.assertNotIn(PASSWORD, json.dumps(self.store.audit_rows(), ensure_ascii=False))

    def test_manual_restart_is_explicit_and_preserves_students_and_password(self):
        self.store.setup_admin(PASSWORD)
        self.store.save_student('001', '合成甲')
        self.assertIsNone(self.service.prepare())
        state = self.service.prepare(manual=True)
        self.assertFalse(state['completed'])
        self.assertEqual(len(self.store.students()), 1)
        self.assertTrue(self.store.authenticate(PASSWORD))

    def test_relocated_partial_progress_requires_camera_retest(self):
        self.begin()
        self.service.record_camera('tested', 2)
        destination = self.folder / 'moved.db'
        with self.store.connect() as connection:
            import sqlite3
            target = sqlite3.connect(destination)
            try:
                connection.backup(target)
            finally:
                target.close()
        moved = FirstUseService(AttendanceStore(destination, seed_path=None))
        self.assertEqual(moved.prepare()['camera'], 'pending')
        self.assertTrue(moved.snapshot()['password_set'])

    def test_damaged_progress_is_not_silently_replaced(self):
        with self.store.connect(write=True) as connection:
            self.store._set(connection, SETUP_KEY, '{broken')
        with self.assertRaises(ValueError):
            self.service.prepare(manual=True)
        with self.store.connect() as connection:
            self.assertEqual(self.store._get(connection, SETUP_KEY), '{broken')


class FakeCapture:
    def __init__(self):
        self.released = False

    def read(self):
        return True, np.full((60, 80, 3), 100, dtype=np.uint8)

    def release(self):
        self.released = True


class CameraPreviewTests(SetupFixture):
    def worker(self, capture=None):
        self.capture = capture or FakeCapture()
        return CameraPreviewWorker(0, self.folder / '.camera.lock', lambda index: self.capture)

    def test_live_preview_has_png_and_ready_then_releases_device_and_lock(self):
        worker = self.worker()
        worker.start()
        kind, value = worker.events.get(timeout=2)
        self.assertEqual(kind, 'ready')
        self.assertTrue(worker.take_frame().startswith(b'\x89PNG'))
        worker.stop()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(self.capture.released)
        self.assertEqual(worker.events.get(timeout=1)[0], 'stopped')
        with ResourceLease(self.folder / '.camera.lock'):
            pass
        self.assertEqual(list(self.folder.rglob('*.jpg')), [])
        self.assertEqual(self.store.students(), [])

    def test_occupied_device_fails_without_opening_and_can_retry(self):
        factory = Mock(return_value=FakeCapture())
        with ResourceLease(self.folder / '.camera.lock'):
            worker = CameraPreviewWorker(0, self.folder / '.camera.lock', factory)
            worker.start()
            worker.join(2)
        self.assertEqual(worker.events.get()[0], 'error')
        factory.assert_not_called()
        retry = self.worker()
        retry.start()
        self.assertEqual(retry.events.get(timeout=2)[0], 'ready')
        retry.stop()
        retry.join(2)

    def test_read_failure_releases_capture_and_reports_error_before_stopped(self):
        capture = FakeCapture()
        capture.read = lambda: (False, None)
        releasing, released = threading.Event(), threading.Event()

        def release():
            releasing.set()
            released.wait(3)
            capture.released = True

        capture.release = release
        worker = self.worker(capture)
        worker.start()
        self.assertTrue(releasing.wait(1))
        worker.join(1.1)
        self.assertTrue(worker.is_alive())
        with self.assertRaises(BusyError):
            with ResourceLease(self.folder / '.camera.lock'):
                pass
        released.set()
        worker.join(2)
        self.assertTrue(capture.released)
        self.assertEqual([worker.events.get()[0] for index in range(2)], ['error', 'stopped'])

    def test_stop_before_start_does_not_open_device(self):
        worker = self.worker()
        worker.capture_factory = Mock()
        worker.stop()
        worker.start()
        worker.join(1)
        worker.capture_factory.assert_not_called()
        self.assertEqual(worker.events.get()[0], 'stopped')


class FakePreview:
    def __init__(self, index, lock_path):
        self.index = index
        self.events = queue.Queue()
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def take_frame(self):
        return None


class FirstUseGuiTests(SetupFixture):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.addCleanup(self.cleanup_root)

    def cleanup_root(self):
        try:
            self.root.update_idletasks()
            for timer in self.root.tk.call('after', 'info'):
                self.root.after_cancel(timer)
            self.root.destroy()
        except tk.TclError:
            pass

    def wizard(self):
        self.begin()
        self.targets = []
        self.app = FirstUseWizard(self.root, self.store, self.targets.append, async_io=False, worker_factory=FakePreview)
        self.root.update()
        return self.app

    def test_fresh_launcher_routes_to_password_and_then_wizard(self):
        gate = InitialLauncher(self.root, self.store, async_io=False)
        login = gate.application
        self.assertIsInstance(login, AdminLogin)
        self.assertTrue(login.is_setup)
        login.password.set(PASSWORD)
        login.confirm.set(PASSWORD)
        login.submit()
        self.assertIsInstance(login.application, FirstUseWizard)
        self.assertEqual(login.application.page, 'camera')
        self.assertEqual(login.password.get(), '')
        self.root.update()
        login.application.close()

    def test_existing_launcher_keeps_role_selection(self):
        self.store.setup_admin(PASSWORD)
        gate = InitialLauncher(self.root, self.store, async_io=False)
        self.assertIsInstance(gate.application, Launcher)
        self.root.update()
        gate.application.close()

    def test_existing_teacher_can_reopen_wizard_and_return_to_selected_page(self):
        self.store.setup_admin(PASSWORD)
        with patch('admin_app.model_health', return_value=(False, '未训练', None)):
            admin = AdminApp(self.root, self.store, authenticated=True, async_io=False)
            admin.open_setup()
            wizard = admin.setup_dialog
            self.root.update()
            self.assertTrue(wizard.reviewing)
            with patch('first_use_ui.messagebox.askyesno', return_value=True):
                wizard.restart()
            self.assertEqual(wizard.page, 'camera')
            wizard.skip()
            wizard.skip()
            wizard.finish('students')
        self.root.update()
        self.assertTrue(wizard.closed)
        self.assertEqual(admin.notebook.select(), str(admin.pages['students']))
        self.assertTrue(self.service.prepare()['completed'])
        admin.close()

    def test_partial_setup_requires_password_and_resumes_roster(self):
        self.begin()
        self.service.record_camera('skipped', 0)
        login = AdminLogin(self.root, self.store, async_io=False, onboarding=True)
        login.password.set('wrong')
        login.submit()
        self.assertFalse(hasattr(login, 'application'))
        login.password.set(PASSWORD)
        login.submit()
        self.assertEqual(login.application.page, 'roster')
        self.root.update()
        login.application.close()

    def test_explicit_skips_reach_checklist_and_finish(self):
        wizard = self.wizard()
        wizard.skip()
        self.assertEqual(wizard.page, 'roster')
        wizard.skip()
        self.assertEqual(wizard.page, 'finish')
        self.assertIn('已跳过', wizard.summary.get())
        self.assertFalse(self.service.snapshot()['state']['completed'])
        wizard.finish('sessions')
        self.assertEqual(self.targets, ['sessions'])
        self.assertTrue(self.service.snapshot()['state']['completed'])

    def test_camera_continuation_waits_for_actual_device_release(self):
        wizard = self.wizard()
        wizard.start_camera()
        worker = wizard.worker
        self.assertEqual(str(wizard.next_button['state']), 'disabled')
        worker.events.put(('ready', 0))
        wizard.poll_camera()
        wizard.next()
        self.assertTrue(worker.stopped)
        self.assertEqual(wizard.page, 'camera')
        self.assertEqual(self.service.snapshot()['state']['camera'], 'pending')
        worker.events.put(('stopped', None))
        wizard.poll_camera()
        self.assertEqual(wizard.page, 'roster')
        self.assertEqual(self.service.snapshot()['state']['camera'], 'tested')
        wizard.close()

    def test_disconnect_invalidates_ready_and_retry_uses_selected_camera(self):
        wizard = self.wizard()
        wizard.start_camera()
        wizard.worker.events.put(('ready', 0))
        wizard.worker.events.put(('error', '设备已断开'))
        wizard.worker.events.put(('stopped', None))
        wizard.poll_camera()
        self.assertFalse(wizard.camera_good)
        self.assertEqual(str(wizard.next_button['state']), 'disabled')
        wizard.camera_index.set('2')
        wizard.start_camera()
        self.assertEqual(wizard.worker.index, 2)
        wizard.skip()
        wizard.worker.events.put(('stopped', None))
        wizard.poll_camera()
        wizard.close()

    def test_closing_camera_waits_for_stopped_and_preserves_pending_step(self):
        wizard = self.wizard()
        wizard.start_camera()
        worker = wizard.worker
        self.assertFalse(wizard.close())
        self.assertTrue(worker.stopped)
        self.assertFalse(wizard.closed)
        worker.events.put(('stopped', None))
        wizard.poll_camera()
        self.root.update()
        self.assertTrue(wizard.closed)
        self.assertEqual(self.service.snapshot()['state']['camera'], 'pending')

    def test_roster_preview_confirm_and_invalid_file_clears_old_plan(self):
        wizard = self.wizard()
        wizard.skip()
        source = self.folder / '名单.csv'
        source.write_text('学号,姓名,班级\n001,合成甲,甲班\n', encoding='utf-8-sig')
        with patch('first_use_ui.filedialog.askopenfilename', return_value=str(source)):
            wizard.load_roster()
            self.assertEqual(self.store.students(), [])
            self.assertEqual(len(wizard.roster_tree.get_children()), 1)
            source.write_text('学号,姓名\nbad,非法\n', encoding='utf-8-sig')
            wizard.load_roster()
            self.assertIsNone(wizard.plan)
            self.assertEqual(str(wizard.import_button['state']), 'disabled')
            source.write_text('学号,姓名,班级\n001,合成甲,甲班\n', encoding='utf-8-sig')
            wizard.load_roster()
            wizard.import_roster()
        self.assertEqual(wizard.page, 'finish')
        self.assertEqual(self.store.students()[0]['student_id'], '001')
        wizard.back()
        self.assertIn('当前启用 1 名学生', wizard.roster_hint.get())
        wizard.close()

    def test_small_window_keeps_bottom_controls_and_preview_visible(self):
        wizard = self.wizard()
        self.root.geometry('820x550')
        self.root.update()
        for control in (wizard.next_button, wizard.skip_button, wizard.close_button):
            self.assertTrue(control.winfo_ismapped())
            self.assertLessEqual(control.winfo_rootx() + control.winfo_width(), self.root.winfo_rootx() + self.root.winfo_width())
            self.assertLessEqual(control.winfo_rooty() + control.winfo_height(), self.root.winfo_rooty() + self.root.winfo_height())
        self.assertGreater(wizard.view.winfo_height(), 100)
        wizard.skip()
        self.root.update()
        self.assertGreater(wizard.roster_tree.winfo_height(), 100)
        wizard.close()

    def test_slow_import_does_not_block_ui_and_close_waits_for_commit(self):
        wizard = self.wizard()
        wizard.skip()
        wizard.plan = self.service.preview_roster([('001', '合成甲', '甲班')])
        original = wizard.service.import_roster
        started, release = threading.Event(), threading.Event()
        wizard.async_io = True

        def slow(*args):
            started.set()
            release.wait(3)
            return original(*args)

        with patch.object(wizard.service, 'import_roster', side_effect=slow):
            wizard.import_roster()
            self.assertTrue(started.wait(1))
            heartbeat = []
            self.root.after(10, lambda: heartbeat.append(True))
            self.assertFalse(wizard.close())
            release.set()
            deadline = time.monotonic() + 3
            while not wizard.closed and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
        self.assertTrue(heartbeat)
        self.assertTrue(wizard.closed)
        self.assertEqual(self.service.snapshot()['state']['roster'], 'imported')
        self.assertFalse(self.service.snapshot()['state']['completed'])

    def test_default_location_persists_before_continuing_and_failure_keeps_screen(self):
        started = []
        screen = StorageWelcome(self.root, 'admin', lambda: started.append(True))
        with patch('storage_welcome.save_data_location', side_effect=PermissionError()), patch('storage_welcome.messagebox.showerror'):
            screen.accept()
        self.assertEqual(started, [])
        self.assertTrue(screen.frame.winfo_exists())
        with patch('storage_welcome.save_data_location') as save:
            screen.accept()
        save.assert_called_once()
        self.assertEqual(started, [True])


if __name__ == '__main__':
    unittest.main()
