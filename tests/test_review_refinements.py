"""审查建议回归：稳定动作、资源归属、公共导出字段及真实异步界面流程。"""

import gc
import queue
import tempfile
import threading
import time
import tkinter as tk
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from _face_test_support import authorize_faces
from _flow_test_support import pump_flows
from classroom_attendance.ui.admin_app import AdminApp, CaptureDialog
from classroom_attendance.core.app_errors import BusyError, TaskTimeout
from classroom_attendance.attendance.attendance_fields import METHOD_TEXT, arrival_time_note
from classroom_attendance.attendance.attendance_reports import summarize
from classroom_attendance.storage.attendance_store import AttendanceStore
from classroom_attendance.attendance.audit_actions import ACTION_TEXT, CORRECT_ATTENDANCE, action_text
from classroom_attendance.core.resource_lock import ResourceLease
from classroom_attendance.core.task_runtime import FlowHost, IO
from classroom_attendance.dashboard.teacher_dashboard import attendance_counts


class ReviewFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='review-refinements-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.store = AttendanceStore(self.folder / 'source' / 'attendance.db', seed_path=None)
        self.store.save_student('001', '合成学生', '测试班')
        authorize_faces(self.store)
        self.now = datetime.now().replace(microsecond=0)

    def session(self):
        sid = self.store.create_session('合成课程', '测试班', self.now - timedelta(minutes=5), self.now + timedelta(hours=1))
        self.store.open_session(sid)
        self.store.close_session(sid)
        return sid


class ReviewDataTests(ReviewFixture):
    def test_display_rename_preserves_history_idempotency_and_review_invalidation(self):
        sid = self.session()
        self.store.correct_attendance(sid, '001', 'present', '合成原因；原因：保留全文', time_unconfirmed=True)
        plan = self.store.preview_session_review(sid)
        self.store.mark_session_reviewed(sid, plan['token'])
        with patch.dict(ACTION_TEXT, {CORRECT_ATTENDANCE: '人工登记'}):
            self.assertEqual(action_text(CORRECT_ATTENDANCE), '人工登记')
            self.assertFalse(self.store.correct_attendance(sid, '001', 'present', '合成原因；原因：保留全文'))
            history = self.store.attendance_history(sid, '001')['corrections']
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]['action'], CORRECT_ATTENDANCE)
            self.assertTrue(self.store.preview_session_review(sid)['reviewed'])
            self.assertEqual(self.store.teacher_dashboard()['pending_review_count'], 0)
            self.store.correct_attendance(sid, '001', 'leave', '变更原因')
            self.assertFalse(self.store.preview_session_review(sid)['reviewed'])
            self.assertEqual(self.store.teacher_dashboard()['pending_review_count'], 1)

    def test_sql_python_and_three_exports_share_unknown_time_and_method_semantics(self):
        sid = self.session()
        self.store.correct_attendance(sid, '001', 'late', '合成补签', time_unconfirmed=True)
        rows = self.store.session_rows(sid)
        self.assertEqual(attendance_counts(rows)['arrived'], 1)
        self.assertEqual(summarize(rows)['time_unconfirmed'], 1)
        self.assertEqual(self.store.sessions()[0]['arrived'], 1)
        self.assertEqual(self.store.teacher_dashboard()['today'][0]['unknown_time'], 1)
        self.assertEqual(arrival_time_note(dict(status='leave', signed_at=None)), '')
        self.assertEqual(arrival_time_note(dict(status='future_status', signed_at=None)), '')
        start = self.now.replace(hour=0, minute=0, second=0)
        outputs = [self.folder / name for name in ('single.xlsx', 'range.xlsx', 'report.xlsx')]
        with patch.dict(METHOD_TEXT, manual=('人工登记', '登记')):
            self.store.export_excel(outputs[0], sid)
            self.store.export_attendance(outputs[1], start, start + timedelta(days=1))
            report = self.store.attendance_report(start.date(), start.date())
            self.store.export_attendance_report(outputs[2], report)
        for path, expected in zip(outputs, ('人工登记', '人工登记', '登记')):
            with self.subTest(path=path.name):
                book = load_workbook(path)
                try:
                    sheet = book['考勤明细'] if path == outputs[2] else book.active
                    values = dict(zip([cell.value for cell in sheet[4]], [cell.value for cell in sheet[5]]))
                    self.assertEqual(values['到课时间说明'], '时间未确认')
                    self.assertEqual(values['记录方式'], expected)
                    self.assertIsNone(values['实际到课时间'])
                finally:
                    book.close()

    def test_backup_uses_database_sibling_resources_without_global_path_patches(self):
        source = self.store.path.parent
        (source / 'face_model.yml').write_bytes(b'synthetic local model')
        (source / 'model_manifest.json').write_text('{}', encoding='utf-8')
        (source / 'dataset').mkdir()
        (source / 'dataset' / '001.0.jpg').write_bytes(b'synthetic local sample')
        output = self.folder / 'backup.zip'
        self.store.backup_archive(output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read('face_model.yml'), b'synthetic local model')
            self.assertEqual(archive.read('model_manifest.json'), b'{}')
            self.assertEqual(archive.read('dataset/001.0.jpg'), b'synthetic local sample')
        self.assertIsNotNone(self.store.last_backup())

    def test_backup_respects_the_lock_in_the_resource_directory(self):
        output = self.folder / 'blocked.zip'
        with ResourceLease(self.store.path.parent / '.samples.lock'):
            with self.assertRaises(BusyError):
                self.store.backup_archive(output)
        self.assertFalse(output.exists())
        self.assertIsNone(self.store.last_backup())


class ReviewGuiTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.close_root)
        self.health = patch('classroom_attendance.ui.admin_app.model_health', return_value=(False, '请先训练', None))
        self.health.start()
        self.addCleanup(self.health.stop)
        self.errors = patch('tkinter.messagebox.showerror').start()
        self.addCleanup(patch.stopall)
        self.app = AdminApp(self.root, self.store, authenticated=True, async_io=True)
        pump_flows(self, self.root, self.app)

    def close_root(self):
        if hasattr(self, 'app') and not self.app.closed:
            self.app.close()
        try:
            for timer in self.root.tk.call('after', 'info'):
                self.root.after_cancel(timer)
            self.root.destroy()
        except tk.TclError:
            pass
        gc.collect()

    def widgets(self, parent):
        for child in parent.winfo_children():
            yield child
            yield from self.widgets(child)

    def test_save_student_uses_worker_thread_and_refreshes_ui(self):
        self.app.student_id.set('002')
        self.app.student_name.set('异步合成学生')
        self.app.student_group.set('测试班')
        threads = []
        original = self.store.save_student

        def save(*args):
            threads.append(threading.get_ident())
            return original(*args)

        with patch.object(self.store, 'save_student', side_effect=save):
            self.app.save_student()
            pump_flows(self, self.root, self.app)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertEqual(self.app.students_tree.selection(), ('002',))
        self.assertIn('异步合成学生', self.app.students_tree.item('002', 'values'))
        self.errors.assert_not_called()

    def test_correct_record_dialog_saves_on_worker_and_refreshes_records(self):
        sid = self.session()
        self.app.refresh_sessions()
        pump_flows(self, self.root, self.app)
        key = next(key for key, row in self.app.record_row_lookup.items() if row['student_id'] == '001')
        self.app.records_tree.selection_set(key)
        self.app.correct_record()
        pump_flows(self, self.root, self.app)
        dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel))
        widgets = list(self.widgets(dialog))
        next(widget for widget in widgets if isinstance(widget, tk.Text)).insert('1.0', '异步更正原因')
        threads = []
        original = self.store.correct_attendance

        def correct(*args, **kwargs):
            threads.append(threading.get_ident())
            return original(*args, **kwargs)

        with patch.object(self.store, 'correct_attendance', side_effect=correct):
            next(widget for widget in widgets if widget.winfo_class() in ('Button', 'TButton')
                 and widget.cget('text') == '保存更正').invoke()
            pump_flows(self, self.root, self.app)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(len(self.store.attendance_history(sid, '001')['corrections']), 1)
        self.assertEqual(self.app.record_row_lookup[key]['method'], 'manual')
        self.errors.assert_not_called()

    def test_periodic_refresh_skips_hidden_records_and_loads_when_selected(self):
        with patch.object(self.store, 'attendance_records', wraps=self.store.attendance_records) as records:
            self.app.notebook.select(self.app.pages['students'])
            pump_flows(self, self.root, self.app)
            self.app.tick()
            pump_flows(self, self.root, self.app)
            records.assert_not_called()
            self.app.notebook.select(self.app.pages['records'])
            pump_flows(self, self.root, self.app)
            self.assertGreater(records.call_count, 0)
            records.reset_mock()
            self.app.tick()
            pump_flows(self, self.root, self.app)
            self.assertGreater(records.call_count, 0)

    def test_capture_target_is_shared_by_progress_initial_capture_and_retry(self):
        workers = []

        class FakeWorker:
            def __init__(self, *args, **kwargs):
                self.target = kwargs['target']
                self.events = queue.Queue()
                workers.append(self)

            def start(self): pass
            def stop(self): pass
            def take_frame(self): return None

        completed = []
        with patch('classroom_attendance.ui.admin_app.CameraWorker', FakeWorker):
            dialog = CaptureDialog(self.root, self.store, self.store.students()[0], completed.append, target=3)
            self.assertEqual(float(dialog.progress['maximum']), 3)
            self.assertEqual(workers[0].target, 3)
            workers[0].events.put(('error', '合成断线'))
            workers[0].events.put(('stopped', 1))
            dialog.poll()
            dialog.retry()
            self.assertEqual(workers[1].target, 2)
            workers[1].events.put(('stopped', 2))
            dialog.poll()
        self.assertEqual(completed, [3])


class ScheduledHost(FlowHost):
    """确定性的主线程调度器，后台任务仍使用真实线程和控制令牌。"""
    def __init__(self):
        self.init_flows(self)
        self.callbacks, self.errors = [], []

    def schedule(self, milliseconds, callback):
        self.callbacks.append(callback)

    def update(self):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()

    def say(self, message): pass

    def handle_error(self, error):
        self.errors.append(error)


class FlowRunnerReviewTests(unittest.TestCase):
    def test_timeout_discards_late_result(self):
        self.check_late_result(timeout=True)

    def test_cancellation_discards_late_result_and_cleans_flow_key(self):
        self.check_late_result(timeout=False)

    def check_late_result(self, *, timeout):
        host, release, finished = ScheduledHost(), threading.Event(), threading.Event()
        callbacks = []

        def work():
            release.wait(2)
            finished.set()
            return 'late'

        def flow():
            result = yield IO(work, timeout=.02 if timeout else 5)
            callbacks.append(result)

        self.addCleanup(release.set)
        host.start_flow(flow(), key='one')
        if not timeout:
            host.cancel_flows()
        if timeout:
            deadline = time.monotonic()+1
            while not next(iter(host._running_flows)).interrupted and time.monotonic()<deadline:
                host.update()
                threading.Event().wait(.005)
        self.assertIn('one', host._flows)
        release.set()
        self.assertTrue(finished.wait(1))
        pump_flows(self, host, host)
        self.assertEqual(callbacks, [])
        self.assertEqual(host._flows, {})
        if timeout:
            self.assertEqual(len(host.errors), 1)
            self.assertIsInstance(host.errors[0], TaskTimeout)
        else:
            self.assertEqual(host.errors, [])

    def test_duplicate_flow_key_runs_work_once(self):
        host, release = ScheduledHost(), threading.Event()
        called, callbacks = [], []

        def work():
            called.append(threading.get_ident())
            release.wait(2)
            return 'done'

        def flow():
            callbacks.append((yield IO(work)))

        self.addCleanup(release.set)
        host.start_flow(flow(), key='same')
        host.start_flow(flow(), key='same')
        release.set()
        pump_flows(self, host, host)
        self.assertEqual(len(called), 1)
        self.assertEqual(callbacks, ['done'])
        self.assertEqual(host.errors, [])
