"""合成名单验证无模型人工开课、批量原子性、预览并发与教师兜底界面。"""

import json
import sqlite3
import tempfile
import threading
import time
import tkinter as tk
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from admin_app import AdminApp
from attendance_store import AttendanceStore
from _face_test_support import authorize_faces
from manual_attendance import ManualAttendanceDialog
from task_runtime import TaskControl, checkpoint, current_control
from app_errors import TaskCancelled


class ManualFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='manual-attendance-')
        self.addCleanup(self.temporary.cleanup)
        self.store = AttendanceStore(Path(self.temporary.name) / 'test.db', seed_path=None)
        for sid, name in [('001', '合成甲'), ('002', '合成乙'), ('003', '合成丙')]:
            self.store.save_student(sid, name, '测试班')
        self.now = datetime.now().replace(microsecond=0)
        authorize_faces(self.store)
        self.sid = self.store.create_session('人工点名测试', '测试班', self.now - timedelta(minutes=5), self.now + timedelta(hours=1))

    def open(self):
        self.store.open_manual_session(self.sid, '相机暂不可用', now=self.now)

    def rows(self):
        return {r['student_id']: r for r in self.store.session_rows(self.sid)}

    def preview(self, ids=('001', '002'), status='present', reason='教师核实到课', **kwargs):
        return self.store.preview_manual_attendance(self.sid, ids, status, reason, **kwargs)

    def apply(self, plan, ids=('001', '002'), status='present', reason='教师核实到课', **kwargs):
        return self.store.apply_manual_attendance(self.sid, ids, status, reason, plan['token'], **kwargs)


class ManualAttendanceTests(ManualFixture):
    def test_manual_open_without_model_is_audited_with_reason(self):
        self.store.open_manual_session(self.sid, '选择人工考勤', actor='教师署名', now=self.now)
        self.assertEqual(self.store.session(self.sid)['state'], 'open')
        event = self.store.audit_rows(self.sid)[0]
        self.assertEqual(event['action'], '开启签到')
        self.assertIn('选择人工考勤', event['detail'])
        self.assertEqual(event['actor_kind'], 'management_signature')

    def test_manual_open_keeps_time_and_unique_session_rules(self):
        for moment in (self.now - timedelta(days=1), self.now + timedelta(days=1)):
            with self.assertRaisesRegex(ValueError, '时间范围'):
                self.store.open_manual_session(self.sid, '人工开课', now=moment)
        self.open()
        other = self.store.create_session('另一课次', '测试班', self.now - timedelta(minutes=1), self.now + timedelta(hours=1))
        with self.assertRaisesRegex(ValueError, '已有课次'):
            self.store.open_manual_session(other, '人工开课', now=self.now)

    def test_manual_open_reason_and_audit_are_atomic(self):
        with self.assertRaises(ValueError):
            self.store.open_manual_session(self.sid, ' ')
        with patch.object(self.store, '_audit', side_effect=sqlite3.OperationalError('failed')):
            with self.assertRaises(sqlite3.Error):
                self.open()
        self.assertEqual(self.store.session(self.sid)['state'], 'draft')

    def test_preview_does_not_write_checkins_or_corrections(self):
        self.open()
        plan = self.preview(time_unconfirmed=True)
        self.assertEqual((plan['total'], plan['changed']), (2, 2))
        self.assertEqual({r['status'] for r in self.rows().values()}, {'pending'})
        self.assertEqual(self.store.attendance_history(self.sid, '001')['corrections'], [])

    def test_batch_unknown_time_keeps_snapshot_notes_and_history(self):
        self.open()
        self.store.correct_attendance(self.sid, '001', 'leave', '先前请假', note='保留备注')
        self.store.save_student('001', '新档案姓名', '新班级')
        plan = self.preview(time_unconfirmed=True, actor='教师甲')
        self.assertEqual(plan['rows'][0]['name'], '合成甲')
        self.assertEqual(self.apply(plan, time_unconfirmed=True, actor='教师甲'), {'changed': 2, 'unchanged': 0})
        rows = self.rows()
        self.assertEqual((rows['001']['status'], rows['001']['note'], rows['001']['signed_at']), ('present', '保留备注', None))
        self.assertEqual(rows['003']['status'], 'pending')
        events = self.store.attendance_history(self.sid, '002')['corrections']
        self.assertEqual(events[0]['actor_kind'], 'management_signature')
        self.assertIn('教师核实到课', events[0]['detail'])
        self.assertEqual(self.store.audit_rows(self.sid)[0]['action'], '批量人工点名')

    def test_batch_explicit_time_and_late_are_kept(self):
        self.open()
        plan = self.preview(status='late', arrival_time=self.now)
        self.apply(plan, status='late', arrival_time=self.now)
        for sid in ('001', '002'):
            self.assertEqual((self.rows()[sid]['status'], self.rows()[sid]['signed_at']), ('late', self.now.isoformat()))

    def test_preserve_individual_times_and_original_face_evidence(self):
        self.open()
        times = [self.now - timedelta(minutes=2), self.now - timedelta(minutes=1)]
        for sid, moment in zip(('001', '002'), times):
            self.store.record_sign_in(sid, self.sid, moment)
        plan = self.preview(status='leave')
        self.apply(plan, status='leave')
        for sid, moment in zip(('001', '002'), times):
            self.assertEqual(self.rows()[sid]['signed_at'], moment.isoformat())
            self.assertEqual(self.store.attendance_history(self.sid, sid)['evidence']['signed_at'], moment.isoformat())

    def test_batch_new_arrivals_need_explicit_time(self):
        self.open()
        self.store.record_sign_in('001', self.sid, self.now)
        with self.assertRaisesRegex(ValueError, '实际到课时间'):
            self.preview()
        self.assertEqual(self.rows()['002']['status'], 'pending')

    def test_invalid_input_and_removed_member_never_write_partial_batch(self):
        self.open()
        roster = self.store.preview_roster_change(self.sid, remove_ids=['002'])
        self.store.apply_roster_change(self.sid, roster['token'], '移出名单', remove_ids=['002'])
        for ids, status, reason, extra in [([], 'leave', '原因', {}), (['001', '001'], 'leave', '原因', {}),
                (['001', '002'], 'leave', '原因', {}), (['001', '999'], 'leave', '原因', {}),
                (['001'], 'bad', '原因', {}), (['001'], 'leave', '', {}),
                (['001'], 'leave', 'a' * 301, {}), (['001'], 'leave', '原因', {'actor': 'a' * 81}),
                (['001'], 'present', '原因', {'arrival_time': self.now.replace(tzinfo=timezone.utc)}),
                (['001'], 'present', '原因', {'arrival_time': self.now, 'time_unconfirmed': True})]:
            with self.subTest(ids=ids, status=status, extra=extra), self.assertRaises(ValueError):
                self.preview(ids, status, reason, **extra)
        self.assertEqual(self.rows()['001']['status'], 'pending')

    def test_draft_cancelled_and_missing_sessions_reject_batch(self):
        with self.assertRaises(ValueError):
            self.preview(status='leave')
        self.store.cancel_session(self.sid, '取消')
        with self.assertRaises(ValueError):
            self.preview(status='leave')
        with self.assertRaises(ValueError):
            self.store.preview_manual_attendance(999, ['001'], 'leave', '原因')

    def test_closed_and_disabled_roster_members_can_be_manually_corrected(self):
        self.open()
        self.store.close_session(self.sid)
        self.store.set_student_active('001', False)
        plan = self.preview(status='leave')
        self.apply(plan, status='leave')
        self.assertEqual(self.rows()['001']['status'], 'leave')

    def test_stale_preview_rejected_after_face_or_manual_record(self):
        self.open()
        plan = self.preview(status='leave')
        self.store.record_sign_in('001', self.sid, self.now)
        with self.assertRaisesRegex(ValueError, '重新预览'):
            self.apply(plan, status='leave')
        self.assertEqual(self.rows()['002']['status'], 'pending')
        plan = self.preview(status='leave')
        self.store.correct_attendance(self.sid, '002', 'absent', '另一窗口登记')
        with self.assertRaisesRegex(ValueError, '重新预览'):
            self.apply(plan, status='leave')

    def test_stale_preview_rejected_after_evidence_only_change(self):
        self.open()
        self.store.correct_attendance(self.sid, '001', 'leave', '请假')
        plan = self.preview(status='absent')
        self.store.record_sign_in('001', self.sid, self.now)
        with self.assertRaisesRegex(ValueError, '重新预览'):
            self.apply(plan, status='absent')

    def test_token_binds_selection_reason_actor_time_and_session_state(self):
        self.open()
        plan = self.preview(time_unconfirmed=True)
        for kwargs in ({'ids': ['001']}, {'reason': '变更原因'}, {'actor': '署名'}, {'arrival_time': self.now}):
            extra = {'time_unconfirmed': True}
            extra.update(kwargs)
            if 'arrival_time' in extra:
                extra.pop('time_unconfirmed')
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, '重新预览'):
                self.apply(plan, **extra)
        self.store.close_session(self.sid)
        with self.assertRaisesRegex(ValueError, '重新预览'):
            self.apply(plan, time_unconfirmed=True)

    def test_removed_selection_rejects_and_unrelated_checkin_can_proceed(self):
        self.open()
        plan = self.preview(status='leave')
        self.store.record_sign_in('003', self.sid, self.now)
        self.apply(plan, status='leave')
        plan = self.preview(status='absent')
        roster = self.store.preview_roster_change(self.sid, remove_ids=['002'])
        self.store.apply_roster_change(self.sid, roster['token'], '移出', remove_ids=['002'])
        with self.assertRaises(ValueError):
            self.apply(plan, status='absent')
        self.assertEqual(self.rows()['001']['status'], 'leave')

    def test_batch_audit_failure_rolls_back_every_student(self):
        self.open()
        plan = self.preview(status='leave')
        original = self.store._audit
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise sqlite3.OperationalError('failed')
            return original(*args, **kwargs)
        with patch.object(self.store, '_audit', side_effect=fail_second), self.assertRaises(sqlite3.Error):
            self.apply(plan, status='leave')
        self.assertEqual({r['status'] for r in self.rows().values()}, {'pending'})
        self.assertEqual(self.store.attendance_history(self.sid, '001')['corrections'], [])

    def test_cancelled_batch_rolls_back_every_student(self):
        self.open()
        plan = self.preview(status='leave')
        control = TaskControl()
        original = self.store._apply_correction
        def cancel_after_first(*args):
            result = original(*args)
            control.cancelled.set()
            return result
        token = current_control.set(control)
        try:
            with patch.object(self.store, '_apply_correction', side_effect=cancel_after_first), self.assertRaises(TaskCancelled):
                self.apply(plan, status='leave')
        finally:
            current_control.reset(token)
        self.assertEqual({r['status'] for r in self.rows().values()}, {'pending'})

    def test_repeat_and_concurrent_confirmation_never_duplicate_events(self):
        self.open()
        plan = self.preview(time_unconfirmed=True)
        def submit(_):
            try:
                return self.apply(plan, time_unconfirmed=True)['changed']
            except ValueError:
                return 0
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(sum(pool.map(submit, range(3))), 2)
        fresh = self.preview(time_unconfirmed=True)
        self.assertEqual(fresh['changed'], 0)
        self.assertEqual(self.apply(fresh, time_unconfirmed=True)['unchanged'], 2)
        self.assertEqual(len(self.store.attendance_history(self.sid, '001')['corrections']), 1)


class ManualAttendanceGuiTests(ManualFixture):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.errors = patch('manual_attendance.messagebox.showerror').start()
        self.confirm = patch('manual_attendance.messagebox.askyesno', return_value=True).start()
        self.addCleanup(patch.stopall)
        with patch('admin_app.model_health', return_value=(False, '模型未就绪', None)):
            self.app = AdminApp(self.root, self.store, authenticated=True, async_io=False)

    def tearDown(self):
        self.app.close()
        try:
            for timer in self.root.tk.call('after', 'info'):
                self.root.after_cancel(timer)
            self.root.destroy()
        except tk.TclError:
            pass

    def dialog(self):
        self.app.sessions_tree.selection_set(str(self.sid))
        self.app.manual_roll_call()
        self.root.update()
        return self.app.manual_dialog

    def prepare(self, dialog):
        dialog.roster_tree.selection_set(['001', '002'])
        dialog.reason.set('老师核实')
        dialog.time_mode.set('时间未确认')

    def test_draft_entry_opens_without_model_and_opens_class_with_reason(self):
        dialog = self.dialog()
        self.assertEqual(str(dialog.preview_button['state']), 'disabled')
        self.assertEqual(str(dialog.open_button['state']), 'normal')
        dialog.open_manual()
        self.assertIn('填写', dialog.message.get())
        self.assertEqual(self.store.session(self.sid)['state'], 'draft')
        dialog.reason.set('未采集人脸，人工点名')
        dialog.open_manual()
        self.assertEqual(self.store.session(self.sid)['state'], 'open')
        self.assertEqual(str(dialog.preview_button['state']), 'normal')
        self.errors.assert_not_called()

    def test_preview_required_edit_invalidates_and_decline_does_not_write(self):
        self.open()
        dialog = self.dialog()
        self.prepare(dialog)
        self.assertEqual(str(dialog.save_button['state']), 'disabled')
        dialog.save()
        dialog.preview()
        self.assertEqual(len(dialog.preview_tree.get_children()), 2)
        self.root.update()
        self.assertGreater(dialog.form_scroll.canvas.yview()[0], 0)
        self.assertEqual(str(dialog.save_button['state']), 'normal')
        dialog.reason.set('改变原因')
        self.assertIsNone(dialog.plan)
        dialog.preview()
        self.confirm.return_value = False
        dialog.save()
        self.assertEqual(self.rows()['001']['status'], 'pending')

    def test_batch_save_refreshes_records_and_supports_more_students(self):
        self.open()
        dialog = self.dialog()
        self.prepare(dialog)
        dialog.preview()
        dialog.save()
        self.assertEqual(self.rows()['001']['status'], 'present')
        self.assertEqual(len([r for r in self.app.record_rows if r['status'] == 'present']), 2)
        self.assertIsNone(dialog.plan)
        self.assertIn('已登记 2', dialog.message.get())
        self.errors.assert_not_called()

    def test_search_and_filtered_select_all_never_select_hidden_rows(self):
        self.open()
        dialog = self.dialog()
        dialog.search.set('合成甲')
        dialog.select_visible()
        self.assertEqual(dialog.roster_tree.selection(), ('001',))
        dialog.search.set('合成')
        self.assertEqual(dialog.roster_tree.selection(), ())
        dialog.filter.set('已到')
        self.assertEqual(dialog.roster_tree.get_children(), ())

    def test_stale_confirmation_keeps_new_face_result_and_requires_preview(self):
        self.open()
        dialog = self.dialog()
        self.prepare(dialog)
        dialog.preview()
        self.store.record_sign_in('001', self.sid, self.now)
        dialog.save()
        self.assertEqual(self.rows()['001']['method'], 'face')
        self.assertEqual(self.rows()['002']['status'], 'pending')
        self.assertIsNone(dialog.plan)
        self.errors.assert_called_once()

    def test_small_window_keeps_fixed_actions_and_scrollable_preview(self):
        self.open()
        dialog = self.dialog()
        dialog.root.geometry('800x550')
        self.root.update()
        for widget in (dialog.preview_button, dialog.save_button, dialog.close_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), dialog.root.winfo_rooty() + dialog.root.winfo_height())
        self.assertTrue(dialog.roster_hint.winfo_ismapped())
        self.assertLessEqual(dialog.roster_hint.winfo_rooty() + dialog.roster_hint.winfo_height(),
                             dialog.tabs.winfo_rooty() + dialog.tabs.winfo_height())
        dialog.tabs.select(dialog.form_page)
        self.root.update()
        dialog.form_scroll.canvas.yview_moveto(1)
        self.root.update()
        self.assertGreater(dialog.form_scroll.canvas.yview()[0], 0)

    def test_slow_preview_keeps_tk_responsive_and_blocks_close(self):
        self.open()
        dialog = self.dialog()
        self.prepare(dialog)
        dialog.async_io = True
        original = self.store.preview_manual_attendance
        def slow(*args, **kwargs):
            time.sleep(.2)
            return original(*args, **kwargs)
        heartbeat = []
        self.root.after(30, lambda: heartbeat.append(True))
        with patch.object(self.store, 'preview_manual_attendance', side_effect=slow):
            dialog.preview()
            dialog.close()
            self.app.close()
            self.assertFalse(dialog.closed)
            deadline = time.monotonic() + 3
            while dialog.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
        self.assertFalse(dialog.busy)
        self.assertTrue(heartbeat)
        self.assertIsNotNone(dialog.plan)
        self.errors.assert_not_called()


if __name__ == '__main__':
    unittest.main()
