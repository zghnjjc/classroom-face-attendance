"""错误模态、真实线程占位和进程中断后的资料恢复回归。"""

import gc
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

import test_A_fixes
import test_face_lifecycle
from _flow_test_support import pump_flows
from classroom_attendance.ui.admin_app import AdminApp
from classroom_attendance.core.app_errors import TaskTimeout, ValidationError
from classroom_attendance.storage.attendance_store import AttendanceStore
from classroom_attendance.faces.face_delete_recovery import prepare_deletion, recover_face_deletions
from classroom_attendance.faces.face_lifecycle import FaceLifecycleService
from classroom_attendance.faces.face_privacy import delete_face_data
from classroom_attendance.core.task_runtime import FlowHost, IO
from classroom_attendance.faces.train import manifest_path, model_health
from classroom_attendance.ui.ui_common import BaseWindow


class ScheduledHost(FlowHost):
    def __init__(self):
        self.init_flows(self)
        self.callbacks, self.errors = [], []
    def schedule(self, delay, callback): self.callbacks.append(callback)
    def say(self, message): pass
    def handle_error(self, error): self.errors.append(error)
    def update(self):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks: callback()


class FlowExecutionTests(unittest.TestCase):
    def test_timeout_keeps_key_until_worker_returns_then_allows_retry(self):
        self.check_occupancy(timeout=True)

    def test_cancel_keeps_key_until_worker_returns_then_allows_retry(self):
        self.check_occupancy(timeout=False)

    def check_occupancy(self, timeout):
        host, release, entered = ScheduledHost(), threading.Event(), threading.Event()
        calls, results = [], []
        def work():
            calls.append(True)
            entered.set()
            release.wait(3)
            return 'done'
        def flow():
            results.append((yield IO(work, timeout=.03 if timeout else 5)))
        self.addCleanup(release.set)
        host.start_flow(flow(), key='same')
        self.assertTrue(entered.wait(1))
        if timeout:
            deadline = time.monotonic()+1
            while not host._flows['same'].interrupted and time.monotonic()<deadline:
                host.update()
                time.sleep(.002)
            self.assertIsInstance(host._flows['same'].interrupted, TaskTimeout)
        else:
            host.cancel_flows()
        host.start_flow(flow(), key='same')
        self.assertEqual(len(calls),1)
        self.assertIn('same',host._flows)
        release.set()
        pump_flows(self,host,host)
        self.assertEqual(results,[])
        self.assertEqual(len(host.errors),1 if timeout else 0)
        host.start_flow(flow(),key='same')
        pump_flows(self,host,host)
        self.assertEqual(len(calls),2)
        self.assertEqual(results,['done'])

    def test_timeout_error_is_delivered_after_underlying_call_finishes(self):
        host, release = ScheduledHost(), threading.Event()
        def flow(): yield IO(lambda: release.wait(3),timeout=.01)
        host.start_flow(flow(),key='blocked')
        self.addCleanup(release.set)
        deadline=time.monotonic()+1
        while not host._flows['blocked'].interrupted and time.monotonic()<deadline:
            host.update()
            time.sleep(.002)
        self.assertEqual(host.errors,[])
        release.set()
        pump_flows(self,host,host)
        self.assertIsInstance(host.errors[0],TaskTimeout)


class DatabasePreflightTests(unittest.TestCase):
    def test_rejected_database_bytes_and_journal_mode_are_unchanged(self):
        for version in ('999','-1','broken'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'future.db'
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
                    connection.execute('INSERT INTO metadata VALUES(?,?)',('schema_version',version))
                before=path.read_bytes()
                with self.assertRaises(ValueError): AttendanceStore(path,seed_path=None)
                self.assertEqual(path.read_bytes(),before)
                self.assertFalse(Path(str(path)+'-wal').exists())
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0],'delete')
                    self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[('metadata',)])


class AuthorizationFeedbackTests(test_face_lifecycle.LifecycleFixture):
    def setUp(self):
        super().setUp()
        self.notice()
        self.root=tk.Tk()
        self.root.withdraw()
        self.app=AdminApp(self.root,self.store,authenticated=True)
        self.panel=self.app.lifecycle
        self.errors=patch('classroom_attendance.ui.ui_common.messagebox.showerror').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.cleanup_root)
        self.wait(lambda:self.panel.snapshot is not None and not self.app._running_flows)

    def cleanup_root(self):
        self.app.cancel_flows()
        self.panel.cancel_flows()
        self.app.closed=True
        for token in self.root.tk.call('after','info'):
            self.root.after_cancel(token)
        self.root.destroy()
        gc.collect()

    def wait(self,condition):
        deadline=time.monotonic()+4
        while not condition() and time.monotonic()<deadline:
            self.root.update()
            time.sleep(.003)
        self.assertTrue(condition())

    def descendants(self,widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def form(self,expiry='invalid-date',confirm=True):
        self.panel.roster.selection_set('001')
        self.panel.grant_dialog()
        self.wait(lambda:self.panel.dialog is not None)
        dialog=self.panel.dialog
        widgets=list(self.descendants(dialog))
        entries=[w for w in widgets if isinstance(w,ttk.Entry)]
        entries[0].insert(0,expiry)
        entries[1].insert(0,'synthetic-reference')
        if confirm:
            next(w for w in widgets if isinstance(w,ttk.Checkbutton)).invoke()
        submit=next(w for w in widgets if w.winfo_class()=='Button' and w.cget('text')=='确认登记')
        return dialog,entries,submit

    def test_invalid_authorization_keeps_form_editable_and_retry_succeeds(self):
        dialog,entries,submit=self.form()
        submit.invoke()
        self.wait(lambda:'YYYY-MM-DD' in dialog.feedback.get())
        self.errors.assert_not_called()
        self.assertIs(self.root.grab_current(),dialog)
        self.assertEqual(entries[1].get(),'synthetic-reference')
        self.assertEqual(submit.cget('state'),'normal')
        self.assertFalse(self.app.busy)
        entries[0].delete(0,'end')
        entries[0].insert(0,(self.now+timedelta(days=30)).date().isoformat())
        submit.invoke()
        self.wait(lambda:not dialog.winfo_exists())
        self.assertIsNone(self.root.grab_current())
        self.assertEqual(len(self.service.history('001')),1)

    def test_missing_confirmation_is_reported_inside_modal(self):
        dialog,entries,submit=self.form(confirm=False)
        submit.invoke()
        self.assertIn('核验真实授权',dialog.feedback.get())
        self.assertEqual(self.service.history('001'),[])
        self.errors.assert_not_called()

    def test_publish_validation_uses_inline_feedback_and_retains_input(self):
        self.panel.publish_dialog()
        dialog=self.panel.dialog
        widgets=list(self.descendants(dialog))
        editor=next(w for w in widgets if isinstance(w,tk.Text))
        entry=next(w for w in widgets if isinstance(w,ttk.Entry))
        entry.insert(0,'new-test-version')
        editor.insert('1.0','too short')
        next(w for w in widgets if isinstance(w,ttk.Checkbutton)).invoke()
        next(w for w in widgets if w.winfo_class()=='Button' and w.cget('text')=='发布新版本').invoke()
        self.wait(lambda:'20–12000' in dialog.feedback.get())
        self.assertEqual(editor.get('1.0','end-1c'),'too short')
        self.assertFalse(dialog.submitting)
        self.errors.assert_not_called()

    def test_close_during_submission_waits_for_cancelled_worker(self):
        dialog,entries,submit=self.form((self.now+timedelta(days=30)).date().isoformat())
        release,entered=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        original=self.panel.service.grant
        def slow(*args,**kwargs):
            entered.set()
            release.wait(3)
            return original(*args,**kwargs)
        with patch.object(self.panel.service,'grant',side_effect=slow):
            submit.invoke()
            self.wait(entered.is_set)
            self.panel.close_dialog(dialog)
            self.assertTrue(dialog.winfo_exists())
            self.assertTrue(self.app.busy)
            release.set()
            self.wait(lambda:not dialog.submitting)
        self.assertEqual(self.service.history('001'),[])
        self.assertIn('取消',dialog.feedback.get())
        self.panel.close_dialog(dialog)
        self.assertIsNone(self.root.grab_current())

    def test_cancel_while_loading_limits_never_starts_authorization(self):
        dialog,entries,submit=self.form((self.now+timedelta(days=30)).date().isoformat())
        entered,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        def limits():
            entered.set()
            release.wait(3)
            return {'training':180,'export':120}
        with patch('classroom_attendance.faces.face_lifecycle_ui.MaintenanceService.timeouts',side_effect=limits), \
                patch.object(self.panel.service,'grant') as grant:
            submit.invoke()
            self.wait(entered.is_set)
            self.panel.close_dialog(dialog)
            release.set()
            self.wait(lambda:not dialog.submitting)
            grant.assert_not_called()
        self.assertFalse(self.app.busy)
        self.assertIn('取消',dialog.feedback.get())

    def test_generic_job_failure_uses_current_modal_and_polling_continues(self):
        dialog,entries,submit=self.form()
        self.app.run_job('synthetic',lambda:(_ for _ in ()).throw(ValueError('测试失败')))
        self.wait(lambda:self.errors.called)
        self.assertIs(self.errors.call_args.kwargs['parent'],dialog)
        completed=[]
        self.app.run_job('next',lambda:'ok',done=completed.append)
        self.wait(lambda:bool(completed))
        self.assertEqual(completed,['ok'])

    def test_destroyed_preferred_parent_falls_back_to_live_root(self):
        dialog=tk.Toplevel(self.root)
        dialog.destroy()
        self.assertIs(self.app.message_parent(dialog),self.root)


class DeletionRecoveryTests(test_A_fixes.TemporaryCase):
    def delete(self,**kwargs):
        return delete_face_data(self.store,'test-password-2026',dataset=self.dataset,model=self.model,
                                camera_lock=self.folder/'.camera.lock',data_lock=self.folder/'.samples.lock',**kwargs)

    def seed_delete(self):
        self.seed()
        self.store.setup_admin('test-password-2026')
        return self.model.read_bytes(),manifest_path(self.model).read_bytes()

    def crash(self,phase):
        code='''
import os,sys
from pathlib import Path
from unittest.mock import patch
from classroom_attendance.storage.attendance_store import AttendanceStore
from classroom_attendance.faces.face_privacy import delete_face_data
folder,phase=Path(sys.argv[1]),sys.argv[2]
store=AttendanceStore(folder/'attendance.db',seed_path=None)
if phase=='move':
    calls=[0]
    def checkpoint():
        calls[0]+=1
        if calls[0]==2: os._exit(71)
    scope=patch('classroom_attendance.faces.face_privacy.checkpoint',checkpoint)
elif phase=='install':
    original=store._audit
    def audit(connection,action,*args,**kwargs):
        if action=='删除人脸样本并重建模型': os._exit(72)
        return original(connection,action,*args,**kwargs)
    scope=patch.object(store,'_audit',audit)
else:
    scope=patch('classroom_attendance.faces.face_privacy.recover_deletion',lambda *args:os._exit(73))
with scope:
    delete_face_data(store,'test-password-2026',student_id='001',clear_all=phase=='commit',
        dataset=folder/'dataset',model=folder/'face_model.yml',camera_lock=folder/'.camera.lock',data_lock=folder/'.samples.lock')
'''
        result=subprocess.run([sys.executable,'-c',code,str(self.folder),phase],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,{'move':71,'install':72,'commit':73}[phase],result.stderr.decode(errors='replace'))

    def test_startup_restores_sample_moved_before_hard_exit(self):
        model,manifest=self.seed_delete()
        self.crash('move')
        self.assertFalse((self.dataset/'001.0.jpg').exists())
        reopened=AttendanceStore(self.store.path,seed_path=None)
        self.assertTrue((self.dataset/'001.0.jpg').exists())
        self.assertEqual(self.model.read_bytes(),model)
        self.assertEqual(manifest_path(self.model).read_bytes(),manifest)
        self.assertEqual(len(reopened.students()),2)
        self.assertEqual(list(self.dataset.glob('.face-delete-*')),[])

    def test_startup_rolls_back_new_model_when_audit_transaction_was_not_committed(self):
        model,manifest=self.seed_delete()
        self.crash('install')
        AttendanceStore(self.store.path,seed_path=None)
        self.assertEqual(self.model.read_bytes(),model)
        self.assertEqual(manifest_path(self.model).read_bytes(),manifest)
        self.assertTrue((self.dataset/'001.0.jpg').exists())
        self.assertTrue(model_health(self.model,self.dataset)[0])

    def test_committed_delete_finishes_cleanup_without_restoring_deleted_samples(self):
        self.seed_delete()
        self.crash('commit')
        reopened=AttendanceStore(self.store.path,seed_path=None)
        self.assertFalse(self.model.exists())
        self.assertEqual(list(self.dataset.glob('*.jpg')),[])
        self.assertEqual(list(self.dataset.glob('.face-delete-*')),[])
        self.assertEqual(sum(r['action']=='清空全部训练数据' for r in reopened.audit_rows()),1)

    def test_failed_audit_restores_samples_and_old_model(self):
        model,manifest=self.seed_delete()
        original=self.store._audit
        def audit(connection,action,*args,**kwargs):
            if action=='删除人脸样本并重建模型': raise sqlite3.OperationalError('synthetic-audit-failure')
            return original(connection,action,*args,**kwargs)
        with patch.object(self.store,'_audit',audit),self.assertRaises(sqlite3.Error):
            self.delete(student_id='001')
        self.assertEqual(self.model.read_bytes(),model)
        self.assertEqual(manifest_path(self.model).read_bytes(),manifest)
        self.assertTrue((self.dataset/'001.0.jpg').exists())

    def test_legacy_unjournaled_directory_is_reported_and_preserved(self):
        self.seed_delete()
        legacy=self.dataset/'.face-delete-old'
        legacy.mkdir()
        sample=legacy/'001.9.jpg'
        sample.write_bytes(b'synthetic')
        with self.assertRaises(ValidationError): AttendanceStore(self.store.path,seed_path=None)
        with self.assertRaises(ValidationError): FaceLifecycleService(self.store).preview_cleanup()
        self.assertEqual(sample.read_bytes(),b'synthetic')

    def test_changed_staged_sample_is_not_overwritten_or_deleted(self):
        self.seed_delete()
        self.crash('move')
        staged=next(self.dataset.glob('.face-delete-*/001.0.jpg'))
        staged.write_bytes(b'changed-synthetic')
        with self.assertRaises(ValidationError): recover_face_deletions(self.store)
        self.assertEqual(staged.read_bytes(),b'changed-synthetic')

    def test_interrupted_deletion_blocks_backup_and_training(self):
        self.seed_delete()
        self.crash('move')
        with self.assertRaises(ValidationError): self.store.backup_archive(self.folder/'backup.zip')
        from classroom_attendance.faces.train import train_model
        with patch('classroom_attendance.faces.train.DATA_LOCK_PATH',self.folder/'.samples.lock'),self.assertRaises(ValidationError):
            train_model(self.dataset,self.model,{'002'})


if __name__=='__main__':
    unittest.main()
