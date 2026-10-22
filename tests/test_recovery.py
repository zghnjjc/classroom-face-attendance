"""数据库故障解除后，实际 Tk 定时器须恢复页面与相机事件处理。"""

import queue
import sqlite3
import tempfile
import time
import tkinter as tk
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from admin_app import AdminApp
from attendance_store import AttendanceStore
from student_app import StudentApp


class FakeCamera:
    def __init__(self, *args, **kwargs):
        self.events = queue.Queue()
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def take_frame(self):
        return None


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="polling-recovery-")
        self.addCleanup(self.temporary.cleanup)
        self.store = AttendanceStore(Path(self.temporary.name) / "test.db", seed_path=None)
        self.store.save_student("001", "恢复测试学生", "测试班")
        self.now = datetime.now().replace(microsecond=0)
        self.sid = self.create_session()
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(str(args[1]))
        self.addCleanup(self.cleanup_root)

    def create_session(self, name="恢复测试课"):
        return self.store.create_session(name, "测试班", self.now-timedelta(minutes=1),
                                         self.now+timedelta(hours=1))

    def cancel_timers(self):
        for token in self.root.tk.call("after", "info"):
            self.root.after_cancel(token)

    def cleanup_root(self):
        try:
            self.cancel_timers()
            self.root.update_idletasks()
            self.root.destroy()
        except tk.TclError:
            pass

    def fast_timer(self, app, callback):
        self.cancel_timers()
        original = app.schedule
        return patch.object(app, "schedule", side_effect=lambda ms, fn: original(10 if fn == callback else ms, fn))

    def fire(self, callback):
        self.root.after(0, callback)
        self.root.update()

    def wait_for(self, condition, timeout=3):
        deadline = time.monotonic()+timeout
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
        self.assertTrue(condition(), "数据库恢复后，实际定时器未完成预期恢复")
        self.assertFalse(self.errors)

    def student(self, opened=False):
        if opened:
            self.store.open_session(self.sid)
        app = StudentApp(self.root, self.store, worker_factory=FakeCamera, async_io=False)
        if opened:
            app.toggle_camera()
        return app

    def test_admin_timer_refreshes_new_session_after_failure(self):
        app = AdminApp(self.root, self.store, authenticated=True, async_io=False)
        with self.fast_timer(app, app.tick):
            with patch.object(self.store, "sessions", side_effect=sqlite3.OperationalError("database is locked")):
                self.fire(app.tick)
            second = self.create_session("数据库恢复后创建")
            self.wait_for(lambda: app.sessions_tree.exists(str(second)))
        self.assertNotIn("失败", app.status.get())

    def test_waiting_student_timer_discovers_session_after_failure(self):
        with patch("student_app.model_health", return_value=(True, "就绪", {})):
            app = self.student()
            with self.fast_timer(app, app.tick_session):
                with patch.object(self.store, "active_session", side_effect=sqlite3.OperationalError("database is locked")):
                    self.fire(app.tick_session)
                self.store.open_session(self.sid)
                self.wait_for(lambda: app.current_session is not None)
        self.assertEqual(app.current_session["id"], self.sid)
        self.assertEqual(app.start_button["state"], "normal")

    def test_signed_result_retries_and_stopped_event_is_consumed(self):
        with patch("student_app.model_health", return_value=(True, "就绪", {})):
            app = self.student(opened=True)
            record = self.store.record_sign_in("001", self.sid)
            worker = app.worker
            worker.events.put(("signed", record))
            worker.events.put(("stopped", 0))
            with self.fast_timer(app, app.poll_camera):
                with patch.object(self.store, "active_session", side_effect=sqlite3.OperationalError("database is locked")):
                    self.fire(app.poll_camera)
                self.assertIsNone(app.worker, "查询失败不能阻塞停止事件")
                self.assertNotEqual(app.start_button.cget("text"), "正在停止…")
                self.assertEqual(app.receipt_name.cget("text"), "尚未识别")
                self.wait_for(lambda: app.receipt_name.cget("text") == "恢*")
        self.assertEqual(app.start_button["state"], "normal")
        self.assertEqual(sum(row["action"] == "人脸签到" for row in self.store.audit_rows(self.sid)), 1)
        self.assertEqual(self.store.session_rows(self.sid)[0]["signed_at"], record["signed_at"])

    def test_pending_result_is_dropped_if_session_changes_during_outage(self):
        with patch("student_app.model_health", return_value=(True, "就绪", {})):
            app = self.student(opened=True)
            app.worker.events.put(("signed", self.store.record_sign_in("001", self.sid)))
            with self.fast_timer(app, app.poll_camera):
                with patch.object(self.store, "active_session", side_effect=sqlite3.OperationalError("database is locked")):
                    self.fire(app.poll_camera)
                self.store.close_session(self.sid)
                second = self.create_session("下一课")
                self.store.open_session(second)
                self.wait_for(lambda: app.current_session["id"] == second)
        self.assertEqual(app.receipt_name.cget("text"), "尚未识别")
        self.assertTrue(app.worker.stopped)

    def test_session_rows_failure_does_not_commit_partial_session_or_lose_timer(self):
        with patch("student_app.model_health", return_value=(True, "就绪", {})):
            app = self.student()
            self.store.open_session(self.sid)
            with self.fast_timer(app, app.tick_session):
                with patch.object(self.store, "session_rows", side_effect=sqlite3.OperationalError("database is locked")):
                    self.fire(app.tick_session)
                    self.assertIsNone(app.current_session)
                    self.assertEqual(app.start_button["state"], "disabled")
                self.wait_for(lambda: app.current_session is not None)
        self.assertEqual(app.start_button["state"], "normal")

    def test_camera_start_query_error_keeps_start_disabled_until_recovery(self):
        with patch("student_app.model_health", return_value=(True, "就绪", {})):
            app = self.student()
            self.store.open_session(self.sid)
            with self.fast_timer(app, app.tick_session):
                with patch.object(self.store, "active_session", side_effect=sqlite3.OperationalError("database is locked")):
                    self.fire(app.toggle_camera)
                self.assertIsNone(app.worker)
                self.assertEqual(app.start_button["state"], "disabled")
                self.fire(app.tick_session)
                self.wait_for(lambda: app.start_button["state"] == "normal")

    def test_database_failure_does_not_prevent_later_hint_or_stop(self):
        with patch("student_app.model_health", return_value=(True, "就绪", {})):
            app = self.student(opened=True)
            worker = app.worker
            worker.events.put(("signed", self.store.record_sign_in("001", self.sid)))
            with self.fast_timer(app, app.poll_camera):
                with patch.object(self.store, "active_session", side_effect=sqlite3.OperationalError("database is locked")):
                    self.fire(app.poll_camera)
                    worker.events.put(("hint", "相机事件继续处理"))
                    self.wait_for(lambda: app.hint.get() == "相机事件继续处理")
                    worker.events.put(("stopped", 0))
                    self.wait_for(lambda: app.worker is None)

    def test_closing_window_during_callback_does_not_schedule_again(self):
        app = AdminApp(self.root, self.store, authenticated=True, async_io=False)
        self.cancel_timers()
        self.root.update_idletasks()
        with patch.object(app, "refresh_sessions", side_effect=app.close):
            self.fire(app.tick)
        self.assertFalse(self.errors)
        self.assertFalse(self.root.tk.call("after", "info"))


if __name__ == "__main__":
    unittest.main()
