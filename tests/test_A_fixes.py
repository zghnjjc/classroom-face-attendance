"""A1–A6 临时数据回归；真实 Tk 事件循环验证默认异步模式。"""

import hashlib
import gc
import json
import logging
import multiprocessing
import queue
import sqlite3
import tempfile
import threading
import time
import tkinter as tk
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from classroom_attendance.ui.admin_app import AdminApp, AdminLogin
from classroom_attendance.core.app_errors import CameraError, TaskTimeout, configure_logging, log_error, user_message
from classroom_attendance.core.app_startup import StartupController
from classroom_attendance.storage.attendance_store import AttendanceStore
from _face_test_support import authorize_faces
from _flow_test_support import pump_flows
from classroom_attendance.camera.camera_device import ProcessCapture, TimedCapture
from classroom_attendance.camera.camera_service import CameraWorker
from classroom_attendance.faces.face_privacy import delete_face_data
from classroom_attendance.faces.face_utils import load_recognizer, read_gray
from classroom_attendance.faces.face_utils import save_gray
from classroom_attendance.maintenance.reset_admin import reset_admin
from classroom_attendance.ui.student_app import StudentApp
from classroom_attendance.core.task_runtime import IO, TaskControl, checkpoint, current_control
from classroom_attendance.faces.train import manifest_path, model_health, preprocess, train_model
from classroom_attendance.ui.ui_common import BaseWindow


def hold_write_lock(path, pipe, seconds):
    connection=sqlite3.connect(path)
    connection.execute("BEGIN EXCLUSIVE")
    pipe.send(True)
    try:
        pipe.recv()  # 父进程确认 UI 已准备好后才开始计时。
        time.sleep(seconds)
    finally:
        connection.rollback(); connection.close(); pipe.close()


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temporary=tempfile.TemporaryDirectory(prefix="A-regression-")
        self.addCleanup(self.temporary.cleanup)
        self.folder=Path(self.temporary.name)
        self.store=AttendanceStore(self.folder/"attendance.db",seed_path=None)
        self.dataset=self.folder/"dataset"
        self.dataset.mkdir()
        self.model=self.folder/"face_model.yml"
        self.now=datetime.now().replace(microsecond=0)
        configure_logging(self.folder/"logs",max_bytes=4096,backup_count=2)
        def close_logs():
            from classroom_attendance.core.app_errors import logger
            for handler in list(logger.handlers):
                logger.removeHandler(handler); handler.close()
            logger.addHandler(logging.NullHandler())
        self.addCleanup(close_logs)

    def seed(self):
        for sid in ("001","002"):
            self.store.save_student(sid,"临时学生"+sid,"测试班")
            image=np.random.default_rng(int(sid)).integers(40,210,(160,160),dtype=np.uint8)
            save_gray(self.dataset/f"{sid}.0.jpg",image)
        authorize_faces(self.store)
        with patch("classroom_attendance.faces.train.DATA_LOCK_PATH",self.folder/"samples.lock"):
            train_model(self.dataset,self.model,{"001","002"})

    def open_session(self):
        sid=self.store.create_session("临时验收","测试班",self.now-timedelta(minutes=1),self.now+timedelta(hours=1))
        self.store.open_session(sid,self.now)
        return sid


class A1ModelTests(TemporaryCase):
    def test_disabling_trained_identity_does_not_invalidate_model(self):
        self.seed()
        self.store.set_student_active("002",False)
        self.assertTrue(model_health(self.model,self.dataset)[0])
        sid=self.open_session()
        self.assertTrue(self.store.record_sign_in("001",sid,self.now)["inserted"])
        with self.assertRaises(ValueError):
            self.store.record_sign_in("002",sid,self.now)
        matcher=load_recognizer(self.model)
        label,distance=matcher.predict(preprocess(read_gray(self.dataset/"001.0.jpg")))
        self.assertEqual(matcher.getLabelInfo(label),"001")
        self.assertLess(distance,60)

    def test_untrained_enabled_student_does_not_block_trained_students(self):
        self.seed()
        self.store.save_student("003","未训练","测试班")
        healthy,_,manifest=model_health(self.model,self.dataset)
        self.assertTrue(healthy)
        self.assertNotIn("003",manifest["student_ids"])
        self.store.set_student_active("003",False)
        self.store.set_student_active("003",True)
        self.assertTrue(model_health(self.model,self.dataset)[0])

    def test_disabled_samples_and_trained_sample_changes_are_distinct(self):
        self.seed()
        self.assertTrue(model_health(self.model,self.dataset)[0])
        (self.dataset/"001.0.jpg").write_bytes(b"changed")
        self.assertFalse(model_health(self.model,self.dataset)[0])


class A4CameraTests(unittest.TestCase):
    def test_driver_exit_during_open_is_chinese_camera_error_and_cleans_up(self):
        context=Mock()
        parent,child=Mock(),Mock()
        parent.poll.return_value=True
        parent.recv.side_effect=EOFError()
        context.Pipe.return_value=(parent,child)
        context.Process.return_value.is_alive.return_value=True
        with patch("classroom_attendance.camera.camera_device.multiprocessing.get_context",return_value=context):
            with self.assertRaises(CameraError) as raised:
                ProcessCapture(0)
        self.assertIn("摄像头",user_message(raised.exception))
        context.Process.return_value.terminate.assert_called_once()
        parent.close.assert_called_once()
        child.close.assert_called_once()

    def test_never_returning_read_times_out_and_releases(self):
        unblock=threading.Event()
        capture=Mock()
        capture.read.side_effect=lambda:unblock.wait(10)
        capture.release.side_effect=unblock.set
        bounded=TimedCapture(capture,threading.Event(),timeout=.1)
        start=time.perf_counter()
        with self.assertRaises(CameraError):
            bounded.read()
        bounded.release()
        self.assertLess(time.perf_counter()-start,1)
        capture.release.assert_called_once()

    def test_stop_interrupts_blocked_read_without_waiting_for_timeout(self):
        from classroom_attendance.core.app_errors import TaskCancelled
        unblock=threading.Event(); stop=threading.Event()
        capture=Mock(); capture.read.side_effect=lambda:unblock.wait(10)
        capture.release.side_effect=unblock.set
        bounded=TimedCapture(capture,stop,timeout=10)
        timer=threading.Timer(.05,stop.set); timer.start()
        start=time.perf_counter()
        with self.assertRaises(TaskCancelled):
            bounded.read()
        bounded.release(); timer.join()
        self.assertLess(time.perf_counter()-start,1)
        capture.release.assert_called_once()


class A5LoggingTests(TemporaryCase):
    def test_traceback_has_call_locations_without_exception_values_or_source(self):
        try:
            raise ValueError("隐私姓名 123456 图像base64 SECRET_PASSWORD")
        except ValueError as error:
            log_error("test_failure",error)
        text=(self.folder/"logs/classroom.log").read_text(encoding="utf-8")
        self.assertIn("Traceback",text)
        self.assertIn("test_traceback",text)
        for private in ("隐私姓名","123456","SECRET_PASSWORD","raise ValueError"):
            self.assertNotIn(private,text)

    def test_rotation_and_chinese_error_categories(self):
        logger=configure_logging(self.folder/"logs",max_bytes=220,backup_count=2)
        for _ in range(15):
            try: raise sqlite3.OperationalError("database is locked private 999999")
            except sqlite3.Error as error: log_error("db_failed",error)
        self.assertTrue((self.folder/"logs/classroom.log.1").exists())
        self.assertIn("数据忙",user_message(sqlite3.OperationalError("database is locked")))
        self.assertNotIn("locked",user_message(sqlite3.OperationalError("database is locked")))
        self.assertIn("摄像头",user_message(CameraError("private")))
        self.assertIn("超时",user_message(TaskTimeout()))
        for file in (self.folder/"logs").glob("*"):
            self.assertNotIn("999999",file.read_text(encoding="utf-8"))


class A6PasswordTests(TemporaryCase):
    def test_legacy_password_upgrades_only_after_successful_login(self):
        password="test-password-2026"
        salt="12"*16
        old={"salt":salt,"hash":hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(salt),260000).hex(),
             "failures":0,"locked_until":""}
        with self.store.connect(write=True) as connection:
            self.store._set(connection,"admin_auth",json.dumps(old))
        self.assertFalse(self.store.authenticate("wrong",self.now))
        with self.store.connect() as c:
            self.assertNotIn("iterations",json.loads(self.store._get(c,"admin_auth")))
        self.assertTrue(self.store.authenticate(password,self.now))
        with self.store.connect() as c:
            updated=json.loads(self.store._get(c,"admin_auth"))
        self.assertEqual(updated["iterations"],600000)
        self.assertEqual(updated["algorithm"],"pbkdf2_sha256")
        self.assertNotEqual(updated["salt"],salt)
        self.assertNotIn(password,json.dumps(updated))

    def test_lockout_retains_failures_and_increases_after_expiry(self):
        self.store.setup_admin("test-password-2026")
        for _ in range(5): self.assertFalse(self.store.authenticate("wrong",self.now))
        with self.store.connect() as c:
            state=json.loads(self.store._get(c,"admin_auth"))
        self.assertEqual(state["failures"],5)
        with self.assertRaises(ValueError): self.store.authenticate("test-password-2026",self.now)
        later=self.now+timedelta(seconds=61)
        self.assertFalse(self.store.authenticate("wrong",later))
        with self.store.connect() as c:
            state=json.loads(self.store._get(c,"admin_auth"))
        self.assertEqual(state["failures"],6)
        self.assertEqual(datetime.fromisoformat(state["locked_until"])-later,timedelta(seconds=120))
        self.assertTrue(self.store.authenticate("test-password-2026",later+timedelta(seconds=121)))

    def test_password_change_and_explicit_offline_reset(self):
        self.store.setup_admin("test-password-2026")
        with self.assertRaises(ValueError): self.store.change_password("wrong","new-password-2026")
        self.store.change_password("test-password-2026","new-password-2026")
        self.assertFalse(self.store.authenticate("test-password-2026"))
        self.assertTrue(self.store.authenticate("new-password-2026"))
        with self.assertRaises(ValueError): reset_admin(self.folder)
        backup=reset_admin(self.folder,confirmed=True)
        self.assertTrue(backup.is_file())
        self.assertFalse(self.store.admin_exists())
        self.assertTrue(any(r["action"]=="离线重置管理密码" for r in self.store.audit_rows()))
        self.store.setup_admin("after-reset-2026")
        self.assertTrue(self.store.authenticate("after-reset-2026"))


class A6FaceDeletionTests(TemporaryCase):
    def delete(self,**kwargs):
        return delete_face_data(self.store,"test-password-2026",dataset=self.dataset,model=self.model,
                                camera_lock=self.folder/"camera.lock",data_lock=self.folder/"samples.lock",**kwargs)

    def test_delete_one_rebuilds_other_students_and_preserves_evidence(self):
        self.seed(); self.store.setup_admin("test-password-2026")
        sid=self.open_session()
        record=self.store.record_sign_in("001",sid,self.now)
        self.store.close_session(sid)
        self.assertEqual(self.delete(student_id="001"),1)
        self.assertFalse((self.dataset/"001.0.jpg").exists())
        health=model_health(self.model,self.dataset)
        self.assertTrue(health[0])
        self.assertEqual(health[2]["student_ids"],["002"])
        self.assertEqual(self.store.attendance_history(sid,"001")["evidence"]["signed_at"],record["signed_at"])
        self.assertTrue(any(r["action"]=="删除人脸样本并重建模型" for r in self.store.audit_rows()))
        self.assertEqual(list(self.dataset.glob(".face-delete-*")),[])

    def test_clear_training_preserves_roster_and_has_no_model_or_samples(self):
        self.seed(); self.store.setup_admin("test-password-2026")
        self.assertEqual(self.delete(clear_all=True),2)
        self.assertFalse(self.model.exists())
        self.assertFalse(manifest_path(self.model).exists())
        self.assertEqual(list(self.dataset.glob("*.jpg")),[])
        self.assertEqual(len(self.store.students()),2)
        self.assertFalse(model_health(self.model,self.dataset)[0])

    def test_wrong_password_does_not_delete_and_rebuild_failure_rolls_back(self):
        self.seed(); self.store.setup_admin("test-password-2026")
        old_model=self.model.read_bytes()
        with self.assertRaises(ValueError):
            delete_face_data(self.store,"wrong","001",dataset=self.dataset,model=self.model)
        with patch("classroom_attendance.faces.face_privacy.train_model",side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.delete(student_id="001")
        self.assertTrue((self.dataset/"001.0.jpg").exists())
        self.assertEqual(self.model.read_bytes(),old_model)

    def test_cancelled_database_write_cannot_commit(self):
        control=TaskControl()
        token=current_control.set(control)
        try:
            with self.assertRaises(RuntimeError):
                with self.store.connect(write=True) as connection:
                    self.store._set(connection,"late_write","must rollback")
                    control.cancelled.set()
        finally:
            current_control.reset(token)
        with self.store.connect() as c:
            self.assertIsNone(self.store._get(c,"late_write"))


class AsyncGuiTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.root=tk.Tk(); self.root.withdraw()
        self.addCleanup(self.close_root)
        self.errors=[]
        self.dialog_patch=patch("tkinter.messagebox.showerror",side_effect=lambda *a,**k:self.errors.append(a))
        self.dialog_patch.start(); self.addCleanup(self.dialog_patch.stop)

    def close_root(self):
        try:
            for token in self.root.tk.call("after","info"): self.root.after_cancel(token)
            self.root.update_idletasks(); self.root.destroy()
        except tk.TclError: pass
        gc.collect()

    def wait(self,predicate,timeout=5):
        pump_flows(self, self.root, predicate=predicate, timeout=timeout)

    def admin(self):
        with patch("classroom_attendance.ui.admin_app.model_health",return_value=(False,"请先训练",None)):
            app=AdminApp(self.root,self.store,authenticated=True)
            self.wait(lambda:not app._running_flows)
        return app

    def test_manual_camera_stop_updates_status_without_hiding_camera_error(self):
        self.store.save_student("001","临时学生","测试班")
        sid=self.open_session()
        with patch("classroom_attendance.ui.student_app.model_health",return_value=(True,"模型已就绪",{})):
            app=StudentApp(self.root,self.store,async_io=False)
        def worker():
            value=Mock();value.events=queue.Queue();value.take_frame.return_value=None
            app.worker=value;app.worker_session_id=sid
            app.say("识别进行中，请依次签到")
            return value
        normal=worker();app.toggle_camera()
        normal.events.put(("stopped",0));app.poll_camera()
        self.assertIsNone(app.worker)
        self.assertEqual(app.status.get(),"摄像头已停止")
        self.assertIn("已停止",app.hint.get())
        failing=worker();app.toggle_camera()
        failing.events.put(("error","摄像头连接中断"));failing.events.put(("stopped",0))
        app.poll_camera()
        self.assertIn("摄像头连接中断",app.hint.get())
        self.assertIn("人工点名",app.hint.get())
        self.assertIn("人工登记",app.status.get())

    def test_default_async_admin_and_student_stay_responsive_under_db_lock(self):
        app=self.admin()
        root2=tk.Toplevel(self.root)
        student=StudentApp(root2,self.store)
        self.wait(lambda:not student._running_flows)
        self.store.save_student("001","临时学生","测试班")
        sid=self.open_session()
        with self.store.connect(write=True) as c:
            c.execute("UPDATE sessions SET ends_at=? WHERE id=?",((self.now-timedelta(seconds=1)).isoformat(),sid))
        locked=sqlite3.connect(self.store.path)
        self.addCleanup(locked.close)
        locked.execute("BEGIN EXCLUSIVE")
        heartbeat=[]
        def beat():
            heartbeat.append(time.perf_counter())
            if len(heartbeat)<60: self.root.after(25,beat)
        self.root.after(0,beat)
        app.refresh_sessions(); student.refresh_session()
        self.wait(lambda:app.refresh_error and student.session_refresh_error,timeout=3.2)
        self.assertGreater(len(heartbeat),20)
        self.assertIn("数据忙",app.status.get())
        self.assertFalse(self.errors)
        locked.rollback()
        self.wait(lambda:not app._running_flows and not student._running_flows)
        app.tick(); student.refresh_session()
        self.wait(lambda:not app.refresh_error and student.session_refresh_error is None)
        root2.destroy()

    def test_startup_corrupt_database_has_retry_and_recovers(self):
        bad=self.folder/"broken.db"; bad.write_bytes(b"not a database")
        ready=[]
        boot=StartupController(self.root,lambda root,store:ready.append(store),
                               store_factory=lambda:AttendanceStore(bad,seed_path=None))
        self.wait(lambda:boot.retry.cget("state")=="normal")
        self.assertIn("数据暂时不可用",boot.message.get())
        bad.unlink()
        boot.probe()
        self.wait(lambda:bool(ready))
        self.assertFalse(self.errors)

    def test_three_entrypoints_use_common_startup_guard(self):
        import classroom_attendance.ui.launcher as GUI, classroom_attendance.ui.admin_app as admin_app, classroom_attendance.ui.student_app as student_app
        factories=[]
        with patch("classroom_attendance.core.app_startup.launch_entry",side_effect=lambda factory, **kwargs: factories.append((factory, kwargs.get('role','launcher')))):
            for module in (GUI,admin_app,student_app): module.main()
        self.assertEqual(len(factories),3)
        self.assertEqual([role for factory, role in factories],['launcher','admin','student'])

    def test_startup_io_permission_error_is_chinese_and_retryable(self):
        ready=[]
        factory=Mock(side_effect=[PermissionError("readonly user path private"),self.store])
        boot=StartupController(self.root,lambda root,store:ready.append(store),store_factory=factory)
        self.wait(lambda:boot.retry.cget("state")=="normal")
        self.assertIn("权限",boot.message.get())
        self.assertIn("当前数据目录",boot.message.get())
        self.assertNotIn("损坏",boot.message.get())
        self.assertNotIn("private",boot.message.get())
        boot.probe(); self.wait(lambda:bool(ready))

    def test_fifteen_second_cross_process_lock_has_heartbeat_and_recovers(self):
        self.store.save_student("001","临时学生","测试班")
        sid=self.open_session()
        app=self.admin()
        root2=tk.Toplevel(self.root); student=StudentApp(root2,self.store)
        self.wait(lambda:not student._running_flows)
        with self.store.connect(write=True) as c:
            c.execute("UPDATE sessions SET ends_at=? WHERE id=?",((self.now-timedelta(seconds=1)).isoformat(),sid))
        context=multiprocessing.get_context("spawn")
        parent,child=context.Pipe()
        process=context.Process(target=hold_write_lock,args=(str(self.store.path),child,15))
        process.start(); child.close()
        self.addCleanup(lambda:process.terminate() if process.is_alive() else None)
        self.assertTrue(parent.poll(5)); self.assertTrue(parent.recv())
        beats=[]
        active=True
        def beat():
            beats.append(time.perf_counter())
            if active: self.root.after(25,beat)
        self.root.after(0,beat)
        start=time.perf_counter(); parent.send(True)
        app.refresh_sessions(); student.refresh_session()
        self.wait(lambda:app.refresh_error and student.session_refresh_error,timeout=3.2)
        first_busy=time.perf_counter()-start
        self.wait(lambda:not process.is_alive(),timeout=16)
        process.join(1); parent.close()
        self.wait(lambda:not app._running_flows and not student._running_flows)
        app.tick(); student.refresh_session()
        self.wait(lambda:not app.refresh_error and student.session_refresh_error is None)
        active=False
        gaps=[b-a for a,b in zip(beats,beats[1:])]
        self.assertLess(first_busy,3.2)
        self.assertGreater(len(beats),200)
        self.assertLess(max(gaps),.5)
        self.assertEqual(self.store.session(sid)["state"],"closed")
        root2.destroy()
        # 仅保存响应耗时和计数，不保存测试学生或图像。
        evidence={"first_busy_seconds":round(first_busy,3),"heartbeat_count":len(beats),
                  "maximum_heartbeat_gap":round(max(gaps),3),"lock_seconds":15,"recovered":True}
        (Path(self.folder)/"A3_lock_evidence.json").write_text(
            json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")

    def test_io_and_export_run_off_tk_thread_and_drop_stale_filter_results(self):
        self.store.save_student("001","测试","测试班")
        sid=self.open_session()
        app=self.admin()
        called=[]
        original=self.store.export_attendance
        def export(*args,**kwargs):
            called.append(threading.get_ident())
            return original(*args,**kwargs)
        output=self.folder/"export.xlsx"
        with patch("classroom_attendance.ui.admin_app.filedialog.asksaveasfilename",return_value=str(output)),patch.object(self.store,"export_attendance",side_effect=export):
            app.export()
            pump_flows(self, self.root, app, predicate=output.exists)
        self.assertNotEqual(called[0],threading.get_ident())
        self.assertTrue(output.exists())

    def test_busy_window_can_cancel_exit_without_late_completion(self):
        app=self.admin()
        release=threading.Event(); finished=[]
        def work():
            release.wait(2); checkpoint()
            finished.append(True)
        app.run_job("等待测试",work)
        with patch("classroom_attendance.ui.ui_common.messagebox.askyesno",return_value=True):
            self.assertTrue(app.close())
        release.set()
        self.assertTrue(app.closed)
        self.assertEqual(finished,[])

    def test_background_timeout_does_not_report_success_or_commit_late_write(self):
        app=self.admin()
        complete=[]
        def work():
            threading.Event().wait(.15)
            with self.store.connect(write=True) as c:self.store._set(c,"late_task","no")
        app.run_job("超时测试",work,lambda result:complete.append(True),timeout=.03)
        self.wait(lambda:not app.busy)
        with self.store.connect() as c:self.assertIsNone(self.store._get(c,"late_task"))
        self.assertEqual(complete,[])

    def test_student_receipt_and_worker_hint_are_masked(self):
        app=StudentApp(self.root,self.store,async_io=False)
        value={"name":"隐私测试姓名","student_id":"123456","inserted":True,"status":"present","signed_at":self.now.isoformat()}
        app.show_receipt(value)
        self.assertEqual(app.receipt_name.cget("text"),"隐*")
        self.assertIn("****56",app.receipt_detail.cget("text"))
        self.assertNotIn(value["name"],app.receipt_name.cget("text"))
        self.assertNotIn(value["student_id"],app.receipt_detail.cget("text"))


if __name__=="__main__":
    unittest.main()
