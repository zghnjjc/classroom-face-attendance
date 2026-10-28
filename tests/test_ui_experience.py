"""使用临时数据验证小屏操作可达性和后台任务反馈。"""

import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from admin_app import AdminApp
from GUI import Launcher
from app_errors import TaskTimeout
from attendance_store import AttendanceStore
from student_app import StudentApp
from task_runtime import checkpoint
from ui_common import BaseWindow, CameraView, ScrollableFrame, fit_window


class UiExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="classroom-ui-")
        self.store = AttendanceStore(Path(self.temporary.name) / "test.db", seed_path=None)
        self.store.save_student("001", "测试同学", "测试班")
        self.root = tk.Tk()
        self.errors = patch("ui_common.messagebox.showerror").start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        try:
            for timer in self.root.tk.call("after", "info"):
                self.root.after_cancel(timer)
            self.root.update_idletasks()
            self.root.destroy()
        except tk.TclError:
            pass
        self.temporary.cleanup()

    def admin(self, width=940, height=600):
        with patch("admin_app.model_health", return_value=(False, "请先采集并训练", None)):
            app = AdminApp(self.root, self.store, authenticated=True, async_io=False)
        self.root.geometry(f"{width}x{height}")
        self.root.update()
        return app

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            threading.Event().wait(.005)
        self.assertTrue(predicate(), "界面任务没有在限时内完成")

    def assert_inside(self, widget, container):
        self.assertTrue(widget.winfo_ismapped())
        self.assertGreaterEqual(widget.winfo_rooty(), container.winfo_rooty())
        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                             container.winfo_rooty() + container.winfo_height())

    def make_open_session(self):
        now = datetime.now().replace(microsecond=0)
        sid = self.store.create_session("测试课程", "测试班", now - timedelta(minutes=1),
                                        now + timedelta(hours=1), 10)
        self.store.open_session(sid, now)
        return sid

    def test_initial_window_and_minimum_fit_1366_by_768_workarea(self):
        with patch("ui_common.screen_workarea", return_value=(0, 0, 1366, 728)):
            app = AdminApp(self.root, self.store, authenticated=True, async_io=False)
        self.root.update()
        self.assertLessEqual(self.root.winfo_height() + self.root.winfo_y() + 32, 728)
        self.assertLessEqual(self.root.minsize()[1], 664)
        self.assert_inside(app.cancel_button, self.root)

    def test_window_minimum_also_shrinks_for_short_workareas(self):
        with patch("ui_common.screen_workarea", return_value=(0, 0, 1024, 600)):
            fit_window(self.root, "1180x860", minimum=(940, 600))
        self.root.update()
        self.assertLessEqual(self.root.winfo_height(), 536)
        self.assertEqual(self.root.minsize(), (940, 536))

    def test_student_form_can_scroll_and_keyboard_focus_reveals_training(self):
        app = self.admin()
        app.notebook.select(app.pages['students'])
        self.root.update()
        scroll = app.student_form_scroll
        self.assertLess(scroll.canvas.yview()[1], 1)
        training = app.task_buttons[1]
        training.event_generate("<FocusIn>")
        self.root.update()
        self.assertGreater(scroll.canvas.yview()[0], 0)
        self.assert_inside(training, scroll.canvas)
        app.notebook.select(app.pages["sessions"])
        self.root.update()
        self.assert_inside(app.create_session_button, app.pages["sessions"])

    def test_maintenance_page_is_visible_on_first_selection(self):
        app = self.admin(width=940, height=600)
        app.notebook.select(app.pages['maintenance'])
        self.root.update()
        scroll = next(widget for widget in app.maintenance.winfo_children() if isinstance(widget, ScrollableFrame))
        self.assertTrue(scroll.canvas.winfo_ismapped(), '维护页首次进入时不能显示为空白')
        camera = next(widget for widget in scroll.body.winfo_children()
                      if isinstance(widget, tk.Button) and widget.cget('text') == '独立摄像头检测')
        self.assertTrue(camera.winfo_viewable())
        self.assert_inside(camera, scroll.canvas)

    def test_short_scrollable_content_is_visible_on_first_map(self):
        self.root.geometry('640x480')
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill='both', expand=True)
        notebook.add(tk.Frame(notebook), text='Initial')
        scroll = ScrollableFrame(notebook)
        action = tk.Button(scroll.body, text='Short form action')
        action.pack(anchor='w')
        notebook.add(scroll, text='Short form')
        self.root.update()
        notebook.select(scroll)
        self.root.update()
        self.assertLessEqual(scroll.body.winfo_reqheight(), scroll.canvas.winfo_height())
        self.assertTrue(scroll.canvas.winfo_ismapped(), '短表单首次进入时不能显示为空白')
        self.assert_inside(action, scroll.canvas)

    def test_settings_last_action_is_reachable_and_scroll_bindings_are_released(self):
        app = self.admin()
        app.notebook.select(app.pages["settings"])
        self.root.update()
        scroll = app.settings_scroll
        scroll.canvas.yview_moveto(1)
        self.root.update()
        exit_button = next(w for w in scroll.body.winfo_children()
                           if isinstance(w, tk.Button) and w.cget("text") == "退出管理")
        self.assert_inside(exit_button, scroll.canvas)
        existing = self.root.bind("<MouseWheel>")
        extra = ScrollableFrame(self.root)
        extra.destroy()
        self.assertEqual(self.root.bind("<MouseWheel>").strip(), existing.strip())

    def test_wide_attendance_table_can_reach_note_column(self):
        self.make_open_session()
        app = self.admin()
        app.notebook.select(app.pages["records"])
        self.root.update()
        widget = app.records_tree
        self.assertLess(widget.xview()[1], 1)
        widget.xview_moveto(1)
        self.root.update()
        x, y, width, height = widget.bbox(widget.get_children()[0], "note")
        self.assertGreaterEqual(x, 0)
        self.assertLessEqual(x + width, widget.winfo_width())

    def test_correction_keeps_save_action_visible_on_short_screen(self):
        self.make_open_session()
        app = self.admin()
        app.notebook.select(app.pages["records"])
        app.records_tree.selection_set(app.records_tree.get_children()[0])
        with patch("ui_common.screen_workarea", return_value=(0, 0, 1024, 640)):
            app.correct_record()
        self.root.update()
        dialog = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        actions = next(w for w in dialog.winfo_children() if isinstance(w, tk.Frame)
                       and not isinstance(w, ScrollableFrame))
        save = next(w for w in actions.winfo_children() if w.cget("text") == "保存更正")
        self.assert_inside(save, dialog)
        self.assertLessEqual(dialog.winfo_height(), 576)
        form_scroll = next(w for w in dialog.winfo_children() if isinstance(w, ScrollableFrame))
        self.assertLess(form_scroll.canvas.yview()[1], 1)

    def test_student_sidebar_and_preview_fit_small_window(self):
        app = StudentApp(self.root, self.store, async_io=False)
        self.root.geometry("900x560")
        self.root.update()
        app.info_scroll.canvas.yview_moveto(1)
        self.root.update()
        self.assert_inside(app.receipt_detail, app.info_scroll.canvas)
        self.assert_inside(app.cancel_button, self.root)

    def test_launcher_entry_buttons_remain_visible_at_900_by_560(self):
        app = Launcher(self.root)
        self.root.geometry("900x560")
        self.root.update()

        def buttons(parent):
            for widget in parent.winfo_children():
                if isinstance(widget, tk.Button) and widget.cget("text").startswith("进入"):
                    yield widget
                yield from buttons(widget)

        entries = list(buttons(app.content))
        self.assertEqual(len(entries), 2)
        for widget in entries:
            self.assert_inside(widget, self.root)
            self.assertGreater(widget.winfo_height(), 30)

    def test_camera_preview_scales_to_viewport_without_cropping(self):
        import cv2
        import numpy as np
        self.root.geometry("500x320")
        view = CameraView(self.root)
        view.pack(fill="both", expand=True)
        self.root.update()
        success, encoded = cv2.imencode(".png", np.zeros((420, 640, 3), dtype=np.uint8))
        self.assertTrue(success)
        view.render(encoded.tobytes())
        self.assertLessEqual(view.photo.width(), view.picture.winfo_width())
        self.assertLessEqual(view.photo.height(), view.picture.winfo_height())
        self.assertAlmostEqual(view.photo.width() / view.photo.height(), 640 / 420, places=2)

    def test_job_progress_and_cancel_remain_active_until_worker_stops(self):
        app = BaseWindow(self.root, "界面测试", "临时任务", async_io=False)
        released = threading.Event()
        self.addCleanup(released.set)
        completed = []

        def work():
            released.wait(2)
            checkpoint()
            return "完成"

        app.run_job("测试任务", work, completed.append)
        self.root.update()
        self.assertTrue(app.task_box.winfo_ismapped())
        self.assertEqual(str(app.cancel_button.cget("state")), "normal")
        app.cancel_job()
        self.root.update()
        self.assertTrue(app.busy)
        self.assertEqual(str(app.cancel_button.cget("state")), "disabled")
        self.assertIn("取消", app.status.get())
        released.set()
        self.wait_for(lambda: not app.busy)
        self.assertEqual(completed, [])
        self.assertIn("已取消", app.status.get())
        self.assertFalse(app.task_box.winfo_ismapped())
        self.errors.assert_not_called()

    def test_timeout_feedback_is_not_replaced_by_cancel_feedback(self):
        app = BaseWindow(self.root, "界面测试", "临时任务", async_io=False)
        released = threading.Event()
        self.addCleanup(released.set)

        def work():
            released.wait(2)
            checkpoint()

        app.run_job("超时任务", work, timeout=.03)
        self.wait_for(lambda: isinstance(app.job_interrupted, TaskTimeout))
        released.set()
        self.wait_for(lambda: not app.busy)
        self.assertIn("超时", app.status.get())
        self.errors.assert_called_once()

    def test_completed_job_restores_idle_controls_and_keeps_full_status(self):
        app = BaseWindow(self.root, "界面测试", "临时任务", async_io=False)
        results = []
        app.run_job("测试任务", lambda: "完成", results.append)
        self.wait_for(lambda: not app.busy)
        self.assertEqual(results, ["完成"])
        self.assertFalse(app.task_box.winfo_ismapped())
        self.assertEqual(str(app.cancel_button.cget("state")), "disabled")
        message = "操作提示" * 30
        app.say(message)
        self.assertEqual(app.status.get(), message)
        with patch("ui_common.messagebox.showinfo") as details:
            app.show_status()
        self.assertEqual(details.call_args.args[1], message)


if __name__ == "__main__":
    unittest.main()
