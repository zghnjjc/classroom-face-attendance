"""临时口令库及可控时钟验证次数提示、倒计时与后台结果顺序。"""
import gc
import json
import logging
import tempfile
import threading
import time
import tkinter as tk
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from admin_app import AdminLogin
from app_errors import configure_logging, logger
from attendance_store import AttendanceStore


class PasswordCase(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temporary = tempfile.TemporaryDirectory(prefix="login-feedback-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        configure_logging(self.folder/"logs")
        self.addCleanup(self.close_logs)
        self.store = AttendanceStore(self.folder/"attendance.db", seed_path=None)
        self.password = "temporary-test-password"
        self.store.setup_admin(self.password)
        self.now = datetime(2026, 10, 22, 12, 0, 0)

    def close_logs(self):
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.addHandler(logging.NullHandler())

    def fail_attempts(self, count):
        result = None
        for _ in range(count):
            result = self.store.authenticate_with_status("wrong-password", self.now)
        return result


class LoginStatusTests(PasswordCase):
    def test_each_failure_reports_remaining_attempts_and_fifth_locks(self):
        self.assertEqual(self.store.admin_auth_status(self.now)["attempts_remaining"], 5)
        for remaining in (4, 3, 2, 1, 0):
            result = self.fail_attempts(1)
            self.assertFalse(result["authenticated"])
            self.assertTrue(result["attempted"])
            self.assertEqual(result["attempts_remaining"], remaining)
        self.assertEqual(result["retry_after_seconds"], 60)
        self.assertEqual(result["failures"], 5)
        with patch("attendance_store.hashlib.pbkdf2_hmac") as hashing:
            blocked = self.store.authenticate_with_status(self.password, self.now)
        hashing.assert_not_called()
        self.assertFalse(blocked["attempted"])
        self.assertEqual(blocked["failures"], 5)
        self.assertNotIn(self.password, json.dumps(blocked))
        self.assertTrue({"hash", "salt"}.isdisjoint(blocked))

    def test_expiry_keeps_failures_and_next_failure_reports_longer_lock(self):
        self.fail_attempts(5)
        later = self.now+timedelta(seconds=60)
        status = self.store.admin_auth_status(later)
        self.assertEqual((status["failures"], status["attempts_remaining"], status["next_lock_seconds"]), (5, 1, 120))
        result = self.store.authenticate_with_status("wrong-password", later)
        self.assertEqual((result["failures"], result["retry_after_seconds"]), (6, 120))
        with self.assertRaisesRegex(ValueError, "还需等待 120 秒"):
            self.store.authenticate(self.password, later)
        valid = self.store.authenticate_with_status(self.password, later+timedelta(seconds=120))
        self.assertTrue(valid["authenticated"])
        self.assertEqual((valid["failures"], valid["attempts_remaining"], valid["locked_until"]), (0, 5, ""))

    def test_restarted_readonly_status_rounds_up_and_does_not_reset_counter(self):
        self.fail_attempts(5)
        restarted = AttendanceStore(self.store.path, seed_path=None)
        with restarted.connect() as connection:
            before = restarted._get(connection, "admin_auth")
        self.assertEqual(restarted.admin_auth_status(self.now+timedelta(seconds=59, microseconds=100000))["retry_after_seconds"], 1)
        self.assertEqual(restarted.admin_auth_status(self.now+timedelta(seconds=60))["retry_after_seconds"], 0)
        with restarted.connect() as connection:
            self.assertEqual(restarted._get(connection, "admin_auth"), before)


class LoginFeedbackGuiTests(PasswordCase):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.root.withdraw()
        self.login = None
        self.addCleanup(self.close_root)

    def close_root(self):
        if self.login:
            self.login.stop_login_feedback()
            self.login.cancel_flows()
            self.login.closed = True
        try:
            for timer in self.root.tk.call("after", "info"):
                self.root.after_cancel(timer)
            self.root.update_idletasks()
            self.root.destroy()
        except tk.TclError:
            pass
        gc.collect()

    def open_login(self, async_io=False):
        self.login = AdminLogin(self.root, self.store, async_io=async_io, clock=lambda: self.now)
        return self.login

    def wait(self, predicate):
        deadline = time.perf_counter()+5
        while time.perf_counter()<deadline and not predicate():
            self.root.update()
            threading.Event().wait(.01)
        self.assertTrue(predicate(), "后台登录状态没有按时更新")

    def test_wrong_password_updates_remaining_attempts_on_page(self):
        login = self.open_login()
        login.password.set("wrong-password")
        for remaining in (4, 3, 2, 1):
            login.submit()
            self.assertIn("密码不正确", login.error.get())
            self.assertIn(f"还可尝试 {remaining} 次", login.security_hint.get())
            self.assertEqual(str(login.login_button["state"]), "normal")
        self.assertFalse(hasattr(login, "application"))

    def test_countdown_blocks_button_and_return_then_enables_at_expiry(self):
        login = self.open_login()
        login.password.set("wrong-password")
        for _ in range(5):
            login.submit()
        self.assertIn("01分00秒", login.security_hint.get())
        self.assertEqual(str(login.login_button["state"]), "disabled")
        with patch.object(self.store, "authenticate_with_status") as authenticate:
            login.submit()  # Return 路径也不能绕过禁用按钮。
        authenticate.assert_not_called()
        self.now += timedelta(seconds=59)
        login.tick_login_feedback()
        self.assertIn("00分01秒", login.security_hint.get())
        self.now += timedelta(seconds=1)
        login.tick_login_feedback()
        self.assertEqual(str(login.login_button["state"]), "normal")
        self.assertIn("还可尝试 1 次", login.security_hint.get())
        self.assertIn("120 秒", login.security_hint.get())
        self.assertIn("锁定时间已到", login.error.get())
        login.submit()
        self.assertIn("02分00秒", login.security_hint.get())

    def test_reopen_locked_page_shows_countdown_without_password_attempt(self):
        self.fail_attempts(5)
        self.now += timedelta(seconds=10)
        with patch.object(self.store, "authenticate_with_status", wraps=self.store.authenticate_with_status) as authenticate:
            login = self.open_login()
        authenticate.assert_not_called()
        self.assertIn("00分50秒", login.security_hint.get())
        self.assertEqual(str(login.login_button["state"]), "disabled")
        self.assertEqual(self.store.admin_auth_status(self.now)["failures"], 5)
        self.now += timedelta(seconds=50)
        self.wait(lambda: str(login.login_button["state"]) == "normal")
        self.assertIn("还可尝试 1 次", login.security_hint.get())
        self.assertEqual(self.store.admin_auth_status(self.now)["failures"], 5)

    def test_success_resets_count_and_cancels_login_timers(self):
        self.fail_attempts(2)
        login = self.open_login()
        login.password.set(self.password)
        with patch("admin_app.AdminApp", return_value=Mock(pages={'home': 'home-page'})) as management:
            login.submit()
        management.assert_called_once()
        self.assertTrue(login.login_complete)
        self.assertIsNone(login.feedback_timer)
        self.assertIsNone(login.status_timer)
        self.assertEqual(self.store.admin_auth_status(self.now)["attempts_remaining"], 5)

    def test_async_stale_status_cannot_overwrite_new_failure_and_tk_responds(self):
        captured = self.store.admin_auth_status(self.now)
        entered, release = threading.Event(), threading.Event()
        def slow_status(now):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            return captured
        with patch.object(self.store, "admin_auth_status", side_effect=slow_status):
            login = self.open_login(async_io=True)
            try:
                self.wait(entered.is_set)
                heartbeat = []
                self.root.after(1, lambda: heartbeat.append(True))
                login.password.set("wrong-password")
                login.submit()
                self.wait(lambda: login.auth_status is not None and login.auth_status["failures"]==1 and not login.submitting)
                self.assertTrue(heartbeat)
                self.assertIn("还可尝试 4 次", login.security_hint.get())
            finally:
                release.set()
            self.wait(lambda: not login._running_flows)
        self.assertIn("还可尝试 4 次", login.security_hint.get())


if __name__ == "__main__":
    unittest.main()
