import queue
import tempfile
import time
import tkinter as tk
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from classroom_attendance.ui.admin_app import AdminApp,AdminLogin
from classroom_attendance.storage.attendance_store import AttendanceStore
from classroom_attendance.ui.student_app import StudentApp
from classroom_attendance.ui.ui_common import BaseWindow, CameraView


class FakeCamera:
    def __init__(self,*args,**kwargs):
        self.events=queue.Queue(); self.stopped=False
    def start(self): pass
    def stop(self): self.stopped=True; self.events.put(("stopped",0))
    def take_frame(self): return None


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix="role-ui-test-")
        self.folder=Path(self.temporary.name)
        self.store=AttendanceStore(self.folder/"db.sqlite",seed_path=None)
        self.store.save_student("001","测试学生","一班")
        self.root=tk.Tk(); self.root.withdraw()

    def tearDown(self):
        try:
            self.root.update()
            for identifier in self.root.tk.call("after","info"):
                self.root.after_cancel(identifier)
            self.root.update_idletasks()
            self.root.destroy()
        except tk.TclError: pass
        self.temporary.cleanup()

    def make_open_session(self):
        now=datetime.now().replace(microsecond=0)
        sid=self.store.create_session("测试课程","一班",now-timedelta(minutes=1),now+timedelta(hours=1))
        self.store.open_session(sid)
        return sid

    def test_header_date_supports_ascii_only_time_locale(self):
        class AsciiLocaleDate(datetime):
            def strftime(self, pattern):
                pattern.encode("ascii")
                return super().strftime(pattern)

        with patch("classroom_attendance.ui.ui_common.datetime") as clock:
            clock.now.return_value = AsciiLocaleDate(2026, 1, 2)
            BaseWindow(self.root, "Test", "Date header", async_io=False)
        self.root.update_idletasks()
        header = self.root.winfo_children()[0]
        texts = [str(widget.cget("text")) for widget in header.winfo_children()
                 if widget.winfo_class() == "Label"]
        self.assertIn("2026年01月02日", texts)

    def test_student_has_no_management_controls_and_waits_for_session(self):
        app=StudentApp(self.root,self.store,worker_factory=FakeCamera, async_io=False)
        self.root.update()
        self.assertIsNone(app.current_session)
        self.assertEqual(app.start_button["state"],"disabled")
        labels=[]
        def walk(widget):
            try: labels.append(str(widget.cget("text")))
            except tk.TclError: pass
            for child in widget.winfo_children(): walk(child)
        walk(self.root)
        self.assertFalse(any("采集人脸" in text or "训练模型" in text or "导出 Excel" in text for text in labels))

    def test_student_refreshes_session_and_stops_when_teacher_closes(self):
        sid=self.make_open_session()
        with patch("classroom_attendance.ui.student_app.model_health",return_value=(True,"就绪",{})):
            app=StudentApp(self.root,self.store,worker_factory=FakeCamera, async_io=False)
            app.toggle_camera(); self.assertIsNotNone(app.worker)
            worker=app.worker
            self.store.close_session(sid); app.refresh_session()
            self.assertTrue(worker.stopped)
            app.poll_camera(); self.assertIsNone(app.worker)
            self.assertEqual(app.start_button["state"],"disabled")

    def test_admin_requires_login_and_initial_setup_does_not_expose_tools(self):
        with self.assertRaises(PermissionError): AdminApp(self.root,self.store, async_io=False)
        login=AdminLogin(self.root,self.store, async_io=False)
        self.assertTrue(login.is_setup)
        self.assertFalse(hasattr(login,"application"))

    def test_admin_setup_validates_confirmation_and_then_opens_management(self):
        login=AdminLogin(self.root,self.store, async_io=False)
        login.password.set("Example-password-2026"); login.confirm.set("different")
        login.submit()
        self.assertFalse(self.store.admin_exists()); self.assertFalse(hasattr(login,"application"))
        login.confirm.set("Example-password-2026"); login.submit()
        self.assertTrue(self.store.admin_exists()); self.assertIsInstance(login.application,AdminApp)
        self.assertEqual(login.password.get(),"")

    def test_wrong_admin_password_does_not_expose_management(self):
        self.store.setup_admin("Example-password-2026")
        login=AdminLogin(self.root,self.store, async_io=False); login.password.set("wrong"); login.submit()
        self.assertFalse(hasattr(login,"application")); self.assertTrue(login.error.get())

    def test_admin_training_job_keeps_tk_responsive(self):
        app=AdminApp(self.root,self.store,authenticated=True, async_io=False)
        heartbeat=[]; self.root.after(10,lambda:heartbeat.append(True))
        completed=[]
        def work(): time.sleep(.15); return "completed"
        with patch("classroom_attendance.ui.ui_common.messagebox.showerror") as error:
            app.run_job("测试后台任务",work,completed.append)
            deadline=time.monotonic()+3
            while app.busy and time.monotonic()<deadline:
                self.root.update(); time.sleep(.01)
            self.assertFalse(app.busy); self.assertTrue(heartbeat); self.assertEqual(completed,["completed"])
            error.assert_not_called()

    def test_camera_preview_accepts_png_from_worker(self):
        view=CameraView(self.root)
        success,encoded=cv2.imencode('.png',np.zeros((420,640,3),dtype=np.uint8))
        self.assertTrue(success)
        view.render(encoded.tobytes())
        self.assertEqual(view.photo.width(),640)
        self.assertEqual(view.photo.height(),420)
        view.clear()
        self.assertIsNone(view.photo)


if __name__=="__main__": unittest.main()
