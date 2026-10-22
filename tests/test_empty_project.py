"""空项目可直接启动；不依赖示例学生、人脸图片或已训练模型。"""

import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from admin_app import AdminApp
from app_config import SEED_PATH
from attendance_store import AttendanceStore
from student_app import StudentApp


class EmptyProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="empty-project-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/"empty.db"
        self.store = AttendanceStore(self.path, seed_path=None)
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(str(args[1]))
        self.addCleanup(self.close_root)

    def close_root(self):
        try:
            self.root.update_idletasks()
            for token in self.root.tk.call("after", "info"):
                self.root.after_cancel(token)
            self.root.destroy()
        except tk.TclError:
            pass

    def test_default_seed_does_not_restore_removed_demo_data(self):
        for _ in range(2):
            store = AttendanceStore(self.path, seed_path=SEED_PATH)
            self.assertEqual(store.students(), [])
            self.assertEqual(store.sessions(), [])
            with store.connect() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM attendance").fetchone()[0], 0)

    def test_empty_admin_can_switch_every_page(self):
        app = AdminApp(self.root, self.store, authenticated=True, async_io=False)
        for page in app.pages.values():
            app.notebook.select(page)
            app.tick()
            self.root.update()
        self.assertFalse(app.students_tree.get_children())
        self.assertFalse(app.sessions_tree.get_children())
        self.assertFalse(app.records_tree.get_children())
        self.assertFalse(self.errors)

    def test_empty_student_waits_without_starting_camera(self):
        app = StudentApp(self.root, self.store, async_io=False)
        app.toggle_camera()
        self.root.update()
        self.assertIsNone(app.worker)
        self.assertIsNone(app.current_session)
        self.assertEqual(app.start_button["state"], "disabled")
        self.assertFalse(self.errors)


if __name__ == "__main__":
    unittest.main()
