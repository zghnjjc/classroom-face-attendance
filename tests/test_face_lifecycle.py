import json
import sqlite3
import tempfile
import threading
import time
import tkinter as tk
import unittest
from tkinter import ttk
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from classroom_attendance.ui.admin_app import AdminApp
from classroom_attendance.core.app_errors import BusyError, TaskCancelled, ValidationError
from classroom_attendance.storage.attendance_store import AttendanceStore
from classroom_attendance.storage.backup_service import SnapshotSource, create_backup, database_schema, inspect_backup, restore_backup
from classroom_attendance.camera.camera_service import CameraWorker
from classroom_attendance.faces.face_lifecycle import FaceLifecycleService
from classroom_attendance.faces.face_utils import save_gray
from classroom_attendance.core.resource_lock import ResourceLease
from classroom_attendance.core.task_runtime import TaskControl, current_control
from classroom_attendance.faces.train import manifest_path, model_health, train_model


class LifecycleFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='face-lifecycle-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.directory = self.folder/'source'
        self.store = AttendanceStore(self.directory/'attendance.db', seed_path=None)
        self.store.save_student('001', '合成人物甲', '测试班')
        self.store.save_student('002', '合成人物乙', '测试班')
        self.service = FaceLifecycleService(self.store)
        self.now = datetime.now().replace(microsecond=0)
        self.password = 'Synthetic-cleanup-password'

    def notice(self, version='school-test-v1'):
        return self.service.publish_notice(version, '合成学校告知正文：本机人脸签到、资料范围、明确同意、保存期限、访问者、撤回渠道和人工点名替代方式。', confirmed=True, now=self.now)

    def grant(self, student_id='001', expires_on=None):
        if self.service.snapshot()['notice'] is None:
            self.notice()
        plan = self.service.authorization_plan(student_id, now=self.now)
        self.service.grant(student_id, plan['token'], expires_on or (self.now+timedelta(days=30)).date().isoformat(),
                           '本人明确同意', 'synthetic-document-001', confirmed=True, now=self.now)

    def session(self):
        sid = self.store.create_session('合成课', '测试班', self.now-timedelta(minutes=2), self.now+timedelta(hours=1))
        self.store.open_manual_session(sid, '合成测试')
        return sid

    def samples(self):
        dataset = self.directory/'dataset'
        dataset.mkdir(exist_ok=True)
        for student_id in ('001', '002', '009'):
            save_gray(dataset/f'{student_id}.0.jpg', np.full((160, 160), 100, dtype=np.uint8))
        save_gray(dataset/'无法归属.png', np.full((160, 160), 100, dtype=np.uint8))
        (self.directory/'face_model.yml').write_bytes(b'synthetic-pooled-model')
        (self.directory/'model_manifest.json').write_text(json.dumps({'student_ids': ['001', '002']}), encoding='utf-8')
        return dataset

    def prepare_cleanup(self):
        self.store.setup_admin(self.password)
        self.grant('002')
        return self.samples(), self.service.preview_cleanup()

    def backup(self, path=None, source=None):
        path = path or self.folder/'backup.zip'
        create_backup(source or self.store, path, model=self.directory/'face_model.yml',
                      manifest=self.directory/'model_manifest.json', dataset=self.directory/'dataset')
        return path


class FaceAuthorizationTests(LifecycleFixture):
    def test_new_and_old_unregistered_students_have_no_implied_authorization(self):
        self.assertEqual(self.service.eligible_ids(), set())
        self.assertEqual(self.service.snapshot()['blocked'], 2)
        with self.assertRaises(ValidationError):
            self.store.record_sign_in('001', self.session(), self.now)
        self.assertEqual(self.service.history('001'), [])

    def test_manual_attendance_does_not_require_face_authorization(self):
        sid = self.session()
        plan = self.store.preview_manual_attendance(sid, ['001'], 'present', '人工替代', time_unconfirmed=True)
        self.store.apply_manual_attendance(sid, ['001'], 'present', '人工替代', plan['token'], time_unconfirmed=True)
        self.assertEqual(self.store.session_rows(sid)[0]['status'], 'present')
        self.assertIsNone(self.store.session_rows(sid)[0]['original_signed_at'])

    def test_publish_requires_full_text_explicit_confirmation_and_unique_version(self):
        for version, text, confirmed in [('', '完整正文'*20, True), ('v1', '短', True), ('v1', '完整正文'*20, False)]:
            with self.subTest(version=version), self.assertRaises(ValidationError):
                self.service.publish_notice(version, text, confirmed=confirmed)
        self.notice()
        with self.assertRaises(ValidationError):
            self.notice()

    def test_notice_and_authorization_history_cannot_be_updated_or_deleted(self):
        self.grant()
        for table in ('face_notices', 'face_authorizations'):
            for statement in (f'DELETE FROM {table}', f'UPDATE {table} SET id=id'):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    with self.store.connect(write=True) as connection:
                        connection.execute(statement)
        self.assertEqual(len(self.service.history('001')), 1)

    def test_grant_requires_reference_basis_date_and_real_confirmation(self):
        self.notice()
        plan = self.service.authorization_plan('001')
        for expiry, basis, reference, confirmed in [('bad', '本人明确同意', 'ref', True),
                ((self.now-timedelta(days=1)).date().isoformat(), '本人明确同意', 'ref', True),
                ('2099-01-01', '默认同意', 'ref', True), ('2099-01-01', '本人明确同意', '', True),
                ('2099-01-01', '本人明确同意', 'ref', False)]:
            with self.subTest(expiry=expiry, basis=basis), self.assertRaises(ValidationError):
                self.service.grant('001', plan['token'], expiry, basis, reference, confirmed=confirmed)
        self.assertEqual(self.service.eligible_ids(), set())

    def test_expiry_includes_final_day_but_stops_at_next_midnight(self):
        self.grant(expires_on=self.now.date().isoformat())
        end = self.now.replace(hour=23, minute=59, second=59)
        self.assertEqual(self.service.eligible_ids(end), {'001'})
        self.assertEqual(self.service.eligible_ids(end+timedelta(seconds=1)), set())
        self.assertEqual(self.service.snapshot(end+timedelta(seconds=1))['students'][0]['state'], 'expired')

    def test_fourteen_day_reminder_and_future_grant_not_yet_valid(self):
        self.grant(expires_on=(self.now+timedelta(days=14)).date().isoformat())
        self.assertTrue(self.service.snapshot()['students'][0]['expiring_soon'])
        self.assertEqual(self.service.eligible_ids(self.now-timedelta(seconds=1)), set())
        self.assertEqual(self.service.snapshot(self.now-timedelta(seconds=1))['students'][0]['state'], 'not_yet')

    def test_new_notice_invalidates_prior_grants_without_rewriting_history(self):
        self.grant()
        before = self.service.history('001')
        self.notice('school-test-v2')
        self.assertEqual(self.service.eligible_ids(), set())
        self.assertEqual(self.service.snapshot()['students'][0]['state'], 'outdated')
        self.assertEqual(self.service.history('001'), before)
        self.grant()
        self.assertEqual(self.service.eligible_ids(), {'001'})
        self.assertEqual(len(self.service.history('001')), 2)

    def test_stale_authorization_plan_rejected_after_notice_or_revocation(self):
        self.notice()
        plan = self.service.authorization_plan('001')
        self.service.revoke('001', '合成撤回请求')
        with self.assertRaisesRegex(ValidationError, '变化'):
            self.service.grant('001', plan['token'], '2099-01-01', '本人明确同意', 'ref', confirmed=True)
        plan = self.service.authorization_plan('001')
        self.notice('school-test-v2')
        with self.assertRaisesRegex(ValidationError, '变化'):
            self.service.grant('001', plan['token'], '2099-01-01', '本人明确同意', 'ref', confirmed=True)

    def test_revocation_blocks_signin_but_preserves_past_evidence(self):
        self.grant()
        sid = self.session()
        self.store.record_sign_in('001', sid, self.now)
        original = self.store.session_rows(sid)[0]['original_signed_at']
        self.service.revoke('001', '合成撤回凭据')
        with self.assertRaises(ValidationError):
            self.store.record_sign_in('001', sid, self.now)
        self.assertEqual(self.store.session_rows(sid)[0]['original_signed_at'], original)
        self.assertEqual(self.service.history('001')[0]['action'], 'revoke')
        self.assertEqual(self.service.snapshot()['students'][0]['state'], 'revoked')

    def test_backdated_signin_cannot_bypass_current_expiry(self):
        self.grant(expires_on=self.now.date().isoformat())
        sid = self.session()
        tomorrow = self.now.replace(hour=23, minute=59, second=59)+timedelta(seconds=1)
        from classroom_attendance.faces.face_lifecycle import local_now
        def clock(value=None):
            return local_now(value) if value is not None else tomorrow
        with patch('classroom_attendance.faces.face_lifecycle.local_now', side_effect=clock), self.assertRaises(ValidationError):
            self.store.record_sign_in('001', sid, self.now)
        self.assertIsNone(self.store.session_rows(sid)[0]['original_signed_at'])

    def test_unauthorized_capture_never_opens_camera(self):
        worker = CameraWorker(self.store, 'capture', student_id='001')
        with patch('classroom_attendance.camera.camera_service.CAMERA_LOCK_PATH', self.directory/'.camera.lock'), \
                patch('classroom_attendance.camera.camera_service.DATA_LOCK_PATH', self.directory/'.samples.lock'), \
                patch('classroom_attendance.camera.camera_service.open_camera') as camera:
            worker.run()
        camera.assert_not_called()
        self.assertEqual([item[0] for item in worker.events.queue], ['error', 'stopped'])

    def test_revocation_during_capture_prevents_next_sample_save(self):
        self.grant()
        dataset = self.directory/'dataset'
        detector, capture = Mock(), Mock()
        detector.detectMultiScale.return_value = [(0, 0, 160, 160)]
        def frame():
            self.service.revoke('001', '采集中撤回')
            return True, np.full((200, 200, 3), 100, dtype=np.uint8)
        capture.read.side_effect = frame
        worker = CameraWorker(self.store, 'capture', student_id='001', target=1)
        with patch('classroom_attendance.camera.camera_service.CAMERA_LOCK_PATH', self.directory/'.camera.lock'), \
                patch('classroom_attendance.camera.camera_service.DATA_LOCK_PATH', self.directory/'.samples.lock'), \
                patch('classroom_attendance.camera.camera_service.DATASET_DIR', dataset), patch('classroom_attendance.camera.camera_service.sample_paths', return_value=[]), \
                patch('classroom_attendance.camera.camera_service.open_camera', return_value=capture), patch('classroom_attendance.camera.camera_service.load_classifier', return_value=detector), \
                patch('classroom_attendance.camera.camera_service.quality_hint', return_value=''):
            worker.run()
        self.assertEqual(worker.saved, 0)
        self.assertEqual(list(dataset.iterdir()), [])
        capture.release.assert_called_once()

    def test_training_filters_unconsented_students_and_model_health_blocks_revoked_identity(self):
        self.grant('001')
        dataset = self.samples()
        (dataset/'无法归属.png').unlink()
        model = self.directory/'face_model.yml'
        info = train_model(dataset, model, {'001', '002'}, _lease=False, store=self.store)
        self.assertEqual(info['student_ids'], ['001'])
        self.assertTrue(model_health(model=model, dataset=dataset, consented_ids=self.service.eligible_ids())[0])
        self.service.revoke('001', '合成撤回')
        self.assertFalse(model_health(model=model, dataset=dataset, consented_ids=self.service.eligible_ids())[0])

    def test_training_rechecks_authorization_before_publishing_model(self):
        self.grant()
        dataset = self.samples()
        (dataset/'无法归属.png').unlink()
        model = self.directory/'face_model.yml'
        original = model.read_bytes()
        recognizer = Mock()
        recognizer.train.side_effect = lambda *args: self.service.revoke('001', '训练期间撤回')
        with patch('classroom_attendance.faces.train.create_recognizer', return_value=recognizer), self.assertRaisesRegex(ValueError, '授权已变化'):
            train_model(dataset, model, {'001'}, _lease=False, store=self.store)
        self.assertEqual(model.read_bytes(), original)

    def test_grant_audit_failure_rolls_back_authorization(self):
        self.notice()
        plan = self.service.authorization_plan('001')
        with patch.object(self.store, '_audit', side_effect=sqlite3.OperationalError()), self.assertRaises(sqlite3.Error):
            self.service.grant('001', plan['token'], '2099-01-01', '监护人明确同意', 'ref', confirmed=True)
        self.assertEqual(self.service.history('001'), [])

    def test_schema4_upgrade_preserves_data_but_does_not_invent_consent(self):
        self.store.setup_admin(self.password)
        sid = self.session()
        with self.store.connect(write=True) as connection:
            for table in ('face_authorizations', 'face_notices', 'face_cleanup_jobs'):
                connection.execute(f'DROP TABLE {table}')
            self.store._set(connection, 'schema_version', '4')
        upgraded = AttendanceStore(self.store.path, seed_path=None)
        self.assertEqual(database_schema(upgraded.path), 5)
        self.assertEqual(upgraded.session(sid)['id'], sid)
        self.assertTrue(upgraded.authenticate(self.password))
        self.assertEqual(FaceLifecycleService(upgraded).eligible_ids(), set())

    def test_schema4_archive_restores_then_upgrades_without_authorization(self):
        self.session()
        with self.store.connect(write=True) as connection:
            for table in ('face_authorizations', 'face_notices', 'face_cleanup_jobs'):
                connection.execute(f'DROP TABLE {table}')
            self.store._set(connection, 'schema_version', '4')
        archive = self.backup(source=SnapshotSource(self.store.path))
        summary = inspect_backup(archive)
        self.assertEqual(summary['schema_version'], 4)
        self.assertFalse(summary['model_ready'])
        target = self.folder/'restored'
        restore_backup(archive, target, expected_sha256=summary['archive_sha256'])
        restored = AttendanceStore(target/'attendance.db', seed_path=None)
        self.assertEqual(FaceLifecycleService(restored).eligible_ids(), set())
        self.assertEqual(database_schema(restored.path), 5)

    def test_schema5_backup_preserves_notice_grant_revocation_and_expiry(self):
        self.grant('001')
        self.grant('002')
        self.service.revoke('002', '合成撤回')
        archive = self.backup()
        summary = inspect_backup(archive)
        target = self.folder/'restored'
        restore_backup(archive, target, expected_sha256=summary['archive_sha256'])
        restored = FaceLifecycleService(AttendanceStore(target/'attendance.db', seed_path=None))
        self.assertEqual(restored.eligible_ids(), set())
        self.assertEqual(restored.snapshot()['students'][0]['state'], 'restore_review')
        self.assertEqual(restored.history('002'), self.service.history('002'))
        self.assertEqual(restored.snapshot()['notice'], self.service.snapshot()['notice'])
        plan = restored.authorization_plan('001')
        restored.grant('001', plan['token'], (self.now+timedelta(days=30)).date().isoformat(), '本人明确同意', 'restored-review-reference', confirmed=True)
        self.assertEqual(restored.eligible_ids(), {'001'})

    def test_restoring_pre_revocation_backup_never_reactivates_old_consent(self):
        self.grant('001')
        archive = self.backup()
        self.service.revoke('001', '备份之后已撤回')
        summary = inspect_backup(archive)
        target = self.folder/'restored'
        restore_backup(archive, target, expected_sha256=summary['archive_sha256'])
        restored = FaceLifecycleService(AttendanceStore(target/'attendance.db', seed_path=None))
        self.assertEqual(restored.eligible_ids(), set())
        self.assertEqual(restored.snapshot()['students'][0]['state'], 'restore_review')


class FaceCleanupTests(LifecycleFixture):
    def test_preview_requires_device_lock_and_does_not_delete(self):
        dataset, plan = self.prepare_cleanup()
        self.assertEqual(plan['sample_count'], 3)
        self.assertTrue(plan['removes_model'])
        self.assertEqual(len(list(dataset.iterdir())), 4)
        with ResourceLease(self.directory/'.camera.lock'), self.assertRaises(BusyError):
            self.service.preview_cleanup()

    def test_cleanup_requires_correct_password_and_reason(self):
        dataset, plan = self.prepare_cleanup()
        for password, reason in [('wrong', '清理'), (self.password, '')]:
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                self.service.apply_cleanup(password, plan['token'], reason)
        self.assertEqual(len(list(dataset.iterdir())), 4)
        self.assertIsNone(self.service.snapshot()['pending_job'])

    def test_cleanup_only_removes_restricted_samples_and_pooled_model(self):
        dataset, plan = self.prepare_cleanup()
        self.assertEqual(self.service.apply_cleanup(self.password, plan['token'], '合成受控清理'), 3)
        self.assertEqual([file.name for file in dataset.iterdir()], ['002.0.jpg'])
        self.assertFalse((self.directory/'face_model.yml').exists())
        self.assertFalse((self.directory/'model_manifest.json').exists())
        self.assertEqual(len(self.store.students()), 2)
        self.assertEqual(self.service.eligible_ids(), {'002'})
        self.assertIsNone(self.service.snapshot()['pending_job'])
        self.assertTrue(any(row['action'] == '完成人脸资料清理' for row in self.store.audit_rows()))

    def test_cleanup_preserves_authorization_history_attendance_and_original_evidence(self):
        self.grant('001')
        sid = self.session()
        signed = datetime.now().replace(microsecond=0)
        self.store.record_sign_in('001', sid, signed)
        self.service.revoke('001', '合成撤回')
        history = self.service.history('001')
        self.store.setup_admin(self.password)
        self.grant('002')
        self.samples()
        plan = self.service.preview_cleanup()
        self.service.apply_cleanup(self.password, plan['token'], '撤回后清理')
        self.assertEqual(self.service.history('001'), history)
        self.assertEqual(self.store.session_rows(sid)[0]['original_signed_at'], signed.isoformat())
        self.assertEqual(self.store.session_rows(sid)[0]['status'], 'present')

    def test_long_legacy_filename_uses_bounded_staging_name(self):
        dataset, plan = self.prepare_cleanup()
        name = 'a'*232+'.001.7.jpg'
        (dataset/name).write_bytes(b'synthetic-long-name')
        plan = self.service.preview_cleanup()
        self.assertEqual(self.service.apply_cleanup(self.password, plan['token'], '合成长文件名清理'), 4)
        self.assertFalse((dataset/name).exists())

    def test_new_grant_invalidates_cleanup_preview(self):
        dataset, plan = self.prepare_cleanup()
        self.grant('001')
        with self.assertRaisesRegex(ValidationError, '变化'):
            self.service.apply_cleanup(self.password, plan['token'], '合成清理')
        self.assertTrue((dataset/'001.0.jpg').exists())

    def test_changed_sample_invalidates_cleanup_preview(self):
        dataset, plan = self.prepare_cleanup()
        (dataset/'001.0.jpg').write_bytes(b'changed')
        with self.assertRaises(ValidationError):
            self.service.apply_cleanup(self.password, plan['token'], '合成清理')
        self.assertEqual((dataset/'001.0.jpg').read_bytes(), b'changed')

    def test_cancel_before_prepared_intent_leaves_no_job_or_changes(self):
        dataset, plan = self.prepare_cleanup()
        control = TaskControl()
        control.cancelled.set()
        token = current_control.set(control)
        try:
            with self.assertRaises(TaskCancelled):
                self.service.apply_cleanup(self.password, plan['token'], '合成清理')
        finally:
            current_control.reset(token)
        self.assertIsNone(self.service.snapshot()['pending_job'])
        self.assertEqual(len(list(dataset.iterdir())), 4)

    def interrupt_cleanup(self):
        dataset, plan = self.prepare_cleanup()
        original = Path.unlink
        def fail_model(file, *args, **kwargs):
            if file.name == 'model-face_model.yml':
                raise PermissionError('synthetic-failure')
            return original(file, *args, **kwargs)
        with patch('classroom_attendance.faces.face_lifecycle.Path.unlink', new=fail_model), self.assertRaises(PermissionError):
            self.service.apply_cleanup(self.password, plan['token'], '合成中断')
        return dataset

    def test_interrupted_cleanup_blocks_faces_grants_and_new_backup_until_resumed(self):
        dataset = self.interrupt_cleanup()
        self.assertIsNotNone(self.service.snapshot()['pending_job'])
        self.assertEqual(self.service.eligible_ids(), set())
        with self.assertRaisesRegex(ValidationError, '续办'):
            self.service.authorization_plan('002')
        with self.assertRaisesRegex(ValidationError, '清理尚未完成'):
            self.backup()
        self.assertTrue((dataset/'002.0.jpg').exists())

    def test_resume_after_partial_deletion_finishes_once_and_keeps_valid_samples(self):
        dataset = self.interrupt_cleanup()
        reopened = FaceLifecycleService(AttendanceStore(self.store.path, seed_path=None))
        self.assertEqual(reopened.resume_cleanup(self.password), 3)
        self.assertIsNone(reopened.snapshot()['pending_job'])
        self.assertEqual(reopened.eligible_ids(), {'002'})
        self.assertEqual([file.name for file in dataset.iterdir()], ['002.0.jpg'])
        self.assertEqual(list(self.directory.glob('.privacy-cleanup-*')), [])
        with self.assertRaises(ValidationError):
            reopened.resume_cleanup(self.password)
        self.assertEqual(sum(row['action'] == '完成人脸资料清理' for row in self.store.audit_rows()), 1)

    def test_resume_rejects_tampered_staged_file_without_deleting_it(self):
        self.interrupt_cleanup()
        staged = next(self.directory.glob('.privacy-cleanup-*/model-face_model.yml'))
        staged.write_bytes(b'changed-after-confirmation')
        with self.assertRaisesRegex(ValidationError, '校验失败'):
            self.service.resume_cleanup(self.password)
        self.assertEqual(staged.read_bytes(), b'changed-after-confirmation')

    def test_resume_rejects_traversal_in_stored_job(self):
        self.interrupt_cleanup()
        job_id = self.service.snapshot()['pending_job']
        with self.store.connect(write=True) as connection:
            plan = json.loads(connection.execute('SELECT plan_json FROM face_cleanup_jobs WHERE id=?', (job_id,)).fetchone()[0])
            plan['files'][0]['name'] = '../outside.jpg'
            connection.execute('UPDATE face_cleanup_jobs SET plan_json=? WHERE id=?', (json.dumps(plan), job_id))
        with self.assertRaisesRegex(ValidationError, '清单无效'):
            self.service.resume_cleanup(self.password)

    def test_confirmation_audit_failure_prevents_cleanup_intent_and_deletion(self):
        dataset, plan = self.prepare_cleanup()
        with patch.object(self.store, '_audit', side_effect=sqlite3.OperationalError()), self.assertRaises(sqlite3.Error):
            self.service.apply_cleanup(self.password, plan['token'], '合成清理')
        self.assertIsNone(self.service.snapshot()['pending_job'])
        self.assertEqual(len(list(dataset.iterdir())), 4)

    def test_completion_audit_failure_keeps_resumable_intent_after_files_are_removed(self):
        dataset, plan = self.prepare_cleanup()
        original = self.store._audit
        def audit(connection, action, *args, **kwargs):
            if action == '完成人脸资料清理':
                raise sqlite3.OperationalError('synthetic-completion-failure')
            return original(connection, action, *args, **kwargs)
        with patch.object(self.store, '_audit', side_effect=audit), self.assertRaises(sqlite3.Error):
            self.service.apply_cleanup(self.password, plan['token'], '合成清理')
        self.assertIsNotNone(self.service.snapshot()['pending_job'])
        self.assertEqual([file.name for file in dataset.iterdir()], ['002.0.jpg'])
        self.assertEqual(self.service.resume_cleanup(self.password), 3)
        self.assertIsNone(self.service.snapshot()['pending_job'])


class FaceLifecycleGuiTests(LifecycleFixture):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.update()
        self.admin = AdminApp(self.root, self.store, authenticated=True, async_io=False)
        self.panel = self.admin.lifecycle
        self.addCleanup(self.cleanup_root)

    def cleanup_root(self):
        if not self.admin.closed:
            self.admin.close()

    def descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def test_page_and_home_banner_show_missing_expired_and_due_soon(self):
        self.grant('001', self.now.date().isoformat())
        self.panel.refresh()
        self.assertIn('14 日内到期 1 人', self.panel.summary.get())
        self.assertIn('受限 1 人', self.admin.privacy_hint.get())
        self.assertEqual(len(self.panel.roster.get_children()), 2)

    def test_grant_dialog_displays_current_notice_and_has_no_default_expiry(self):
        self.notice()
        self.panel.refresh()
        self.panel.roster.selection_set('001')
        self.panel.grant_dialog()
        dialog = self.panel.dialog
        text = next(widget for widget in self.descendants(dialog) if isinstance(widget, tk.Text))
        self.assertIn('撤回渠道', text.get('1.0', 'end'))
        entries = [widget for widget in self.descendants(dialog) if widget.winfo_class() == 'TEntry']
        self.assertEqual([entry.get() for entry in entries], ['', ''])
        dialog.destroy()

    def test_notice_editor_content_is_visible_on_first_open_when_it_fits(self):
        self.root.deiconify()
        self.root.geometry('1020x780')
        self.admin.notebook.select(self.admin.pages['privacy'])
        self.root.update()
        self.panel.publish_dialog()
        dialog = self.panel.dialog
        self.root.update()
        scroll = next(widget for widget in dialog.winfo_children() if hasattr(widget, 'canvas'))
        self.assertLessEqual(scroll.body.winfo_reqheight(), scroll.canvas.winfo_height())
        self.assertTrue(scroll.canvas.winfo_ismapped(), '首次打开短表单时画布不应保持隐藏')
        editor = next(widget for widget in self.descendants(dialog) if isinstance(widget, tk.Text))
        entry = next(widget for widget in self.descendants(dialog) if widget.winfo_class() == 'TEntry')
        self.assertTrue(editor.winfo_viewable())
        self.assertTrue(entry.winfo_viewable())
        editor.insert('1.0', '合成输入验证；不提交真实告知。')
        entry.insert(0, 'synthetic-draft')
        self.assertIn('合成输入验证', editor.get('1.0', 'end'))
        self.assertEqual(entry.get(), 'synthetic-draft')
        self.assertIsNone(self.service.snapshot()['notice'])
        dialog.destroy()

    def test_grant_dialog_requires_checkbox_then_commits_in_background(self):
        self.notice()
        self.panel.refresh()
        self.panel.roster.selection_set('001')
        self.panel.grant_dialog()
        dialog = self.panel.dialog
        entries = [widget for widget in self.descendants(dialog) if widget.winfo_class() == 'TEntry']
        entries[0].insert(0, (self.now+timedelta(days=20)).date().isoformat())
        entries[1].insert(0, 'synthetic-ui-reference')
        submit = next(widget for widget in self.descendants(dialog) if widget.winfo_class() == 'Button' and widget.cget('text') == '确认登记')
        submit.invoke()
        self.assertEqual(self.service.history('001'), [])
        check = next(widget for widget in self.descendants(dialog) if widget.winfo_class() == 'TCheckbutton')
        check.invoke()
        submit.invoke()
        deadline = time.monotonic()+4
        while self.admin.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
        self.assertFalse(self.admin.busy)
        self.assertEqual(self.service.eligible_ids(), {'001'})

    def test_revoke_and_cleanup_dialogs_explain_scope_and_require_password(self):
        self.panel.refresh()
        self.panel.roster.selection_set('001')
        self.panel.revoke_dialog()
        self.assertIn('撤回', self.panel.dialog.title())
        self.panel.dialog.destroy()
        self.prepare_cleanup()
        self.panel.cleanup_dialog()
        entries = [widget for widget in self.descendants(self.panel.dialog) if isinstance(widget, ttk.Entry)]
        self.assertEqual(entries[0].cget('show'), '●')
        self.panel.dialog.destroy()

    def test_small_page_keeps_actions_inside_window(self):
        self.root.deiconify()
        self.root.geometry('940x600')
        self.admin.notebook.select(self.admin.pages['privacy'])
        self.root.update()
        controls = [widget for widget in self.descendants(self.panel) if widget.winfo_class() == 'Button']
        for control in controls:
            self.assertTrue(control.winfo_ismapped())
            self.assertLessEqual(control.winfo_rootx()+control.winfo_width(), self.root.winfo_rootx()+self.root.winfo_width())
            self.assertLessEqual(control.winfo_rooty()+control.winfo_height(), self.root.winfo_rooty()+self.root.winfo_height())
        self.assertGreater(self.panel.roster.winfo_height(), 100)

    def test_slow_lifecycle_refresh_keeps_tk_responsive(self):
        self.panel.async_io = True
        started, release = threading.Event(), threading.Event()
        original = self.panel.service.snapshot
        def slow():
            started.set()
            release.wait(3)
            return original()
        ticks = []
        with patch.object(self.panel.service, 'snapshot', side_effect=slow):
            self.panel.refresh()
            self.assertTrue(started.wait(1))
            self.root.after(5, lambda: ticks.append(True))
            deadline = time.monotonic()+2
            while not ticks and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
            release.set()
            while self.panel._running_flows and time.monotonic() < deadline+2:
                self.root.update()
                time.sleep(.005)
        self.assertTrue(ticks)
        self.assertFalse(self.panel._running_flows)


if __name__ == '__main__':
    unittest.main()
