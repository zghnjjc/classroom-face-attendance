"""今日课堂、核对标记失效、并发确认、备份及首页导航验收。"""

import json
import sqlite3
import tempfile
import time
import tkinter as tk
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from classroom_attendance.ui.admin_app import AdminApp
from classroom_attendance.storage.attendance_store import AttendanceStore
from _face_test_support import authorize_faces
from classroom_attendance.storage.backup_service import create_backup, inspect_backup, restore_backup
from classroom_attendance.dashboard.teacher_dashboard import BACKUP_KEY, REVIEW_PREFIX


class DashboardFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='teacher-dashboard-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.store = AttendanceStore(self.folder/'source'/'attendance.db', seed_path=None)
        for sid, name in [('001', '合成甲'), ('002', '合成乙'), ('003', '合成丙')]:
            self.store.save_student(sid, name, '测试班')
        self.now = datetime.now().replace(microsecond=0)
        authorize_faces(self.store)

    def session(self, state='draft', start=None, end=None):
        start = start or self.now-timedelta(minutes=5)
        end = end or self.now+timedelta(hours=1)
        sid = self.store.create_session('首页合成课程', '测试班', start, end)
        if state in {'open', 'closed'}:
            self.store.open_session(sid, start)
        if state == 'closed':
            self.store.close_session(sid)
        elif state == 'cancelled':
            self.store.cancel_session(sid, '停课')
        return sid

    def mark(self, sid, **kwargs):
        plan = self.store.preview_session_review(sid)
        self.store.mark_session_reviewed(sid, plan['token'], **kwargs)
        return plan


class TeacherDashboardTests(DashboardFixture):
    def test_empty_dashboard_has_no_invented_records_or_backup(self):
        result = self.store.teacher_dashboard(self.now)
        self.assertEqual(result['today'], [])
        self.assertIsNone(result['active'])
        self.assertEqual(result['pending_review_count'], 0)
        self.assertIsNone(result['last_backup'])
        self.assertEqual(result['totals']['expected'], 0)

    def test_today_totals_exclude_drafts_cancellations_and_removed_members(self):
        self.session()
        self.session('cancelled')
        sid = self.session('closed')
        plan = self.store.preview_roster_change(sid, remove_ids=['003'])
        self.store.apply_roster_change(sid, plan['token'], '移出', remove_ids=['003'])
        self.store.correct_attendance(sid, '001', 'present', '核实', time_unconfirmed=True)
        data = self.store.teacher_dashboard(self.now)
        self.assertEqual(len(data['today']), 3)
        self.assertEqual((data['totals']['expected'], data['totals']['arrived'], data['totals']['absent']), (2, 1, 1))
        self.assertEqual(data['pending_review_count'], 1)

    def test_current_missing_list_uses_snapshot_and_only_pending(self):
        sid = self.session('open')
        self.store.correct_attendance(sid, '001', 'leave', '请假')
        self.store.record_sign_in('002', sid, self.now)
        self.store.save_student('003', '档案新姓名', '新班级')
        data = self.store.teacher_dashboard(self.now)
        self.assertEqual([(r['student_id'], r['name']) for r in data['missing']], [('003', '合成丙')])
        self.assertEqual((data['active']['leave'], data['active']['arrived']), (1, 1))
        self.assertEqual(data['totals']['absent'], 0)

    def test_cross_midnight_active_is_visible_without_changing_day_attribution(self):
        now = self.now.replace(hour=0, minute=10)
        sid = self.session('open', start=now-timedelta(minutes=20), end=now+timedelta(minutes=30))
        data = self.store.teacher_dashboard(now)
        self.assertEqual(data['active']['id'], sid)
        self.assertEqual(len(data['missing']), 3)
        self.assertEqual(data['today'], [])
        self.assertEqual(data['totals']['expected'], 0)

    def test_expiry_closes_open_once_and_expired_draft_never_becomes_absent(self):
        sid = self.session('open')
        draft = self.session(start=self.now-timedelta(days=1), end=self.now-timedelta(hours=23))
        future = self.now+timedelta(hours=2)
        self.assertEqual(self.store.teacher_dashboard(future)['pending_review_count'], 1)
        self.assertIsNone(self.store.teacher_dashboard(future)['active'])
        self.assertEqual(self.store.session(draft)['state'], 'draft')
        events = [r for r in self.store.audit_rows(sid) if r['action'] == '课次到时关闭']
        self.assertEqual(len(events), 1)

    def test_pending_queue_limit_keeps_total_count_and_recent_order(self):
        ids = [self.session('closed', start=self.now-timedelta(days=i+1), end=self.now-timedelta(days=i+1)+timedelta(hours=1)) for i in range(32)]
        data = self.store.teacher_dashboard(self.now)
        self.assertEqual((data['pending_review_count'], len(data['pending_reviews'])), (32, 30))
        self.assertEqual(data['pending_reviews'][0]['id'], ids[0])

    def test_review_only_closed_and_requires_current_snapshot(self):
        sid = self.session('open')
        with self.assertRaisesRegex(ValueError, '已结束'):
            self.store.preview_session_review(sid)
        self.store.close_session(sid)
        plan = self.store.preview_session_review(sid)
        self.assertFalse(plan['reviewed'])
        with self.assertRaisesRegex(ValueError, '刷新'):
            self.store.mark_session_reviewed(sid, 'invalid')
        self.assertEqual(self.store.teacher_dashboard()['pending_review_count'], 1)

    def test_review_preserves_absence_evidence_and_counts(self):
        sid = self.session('open')
        self.store.record_sign_in('001', sid, self.now)
        self.store.correct_attendance(sid, '002', 'late', '核实', time_unconfirmed=True)
        self.store.close_session(sid)
        before = self.store.session_rows(sid)
        self.mark(sid, actor='教师署名', note='已核对未到与时间未知人员')
        after = self.store.session_rows(sid)
        self.assertEqual(before, after)
        self.assertEqual(self.store.teacher_dashboard()['pending_review_count'], 0)
        event = self.store.audit_rows(sid)[0]
        self.assertEqual((event['action'], event['actor_kind']), ('确认课次核对完成', 'management_signature'))
        self.assertEqual(json.loads(event['after_json'])['absent'], 1)
        self.assertNotIn('token', json.loads(event['after_json']))

    def test_manual_change_invalidates_review_but_identical_repeat_does_not(self):
        sid = self.session('closed')
        self.store.correct_attendance(sid, '001', 'leave', '请假')
        self.mark(sid)
        self.assertFalse(self.store.correct_attendance(sid, '001', 'leave', '请假'))
        self.assertEqual(self.store.teacher_dashboard()['pending_review_count'], 0)
        self.store.correct_attendance(sid, '001', 'absent', '更正')
        row = self.store.teacher_dashboard()['pending_reviews'][0]
        self.assertEqual(row['review_text'], '变更后待复核')

    def test_roster_remove_and_readd_require_review_even_if_values_return(self):
        sid = self.session('closed')
        self.mark(sid)
        for kwargs in ({'remove_ids': ['003']}, {'add_ids': ['003']}):
            plan = self.store.preview_roster_change(sid, **kwargs)
            self.store.apply_roster_change(sid, plan['token'], '调整名单', **kwargs)
        self.assertEqual(self.store.teacher_dashboard()['pending_review_count'], 1)

    def test_profile_and_other_session_changes_leave_review_valid(self):
        sid = self.session('closed')
        self.mark(sid)
        self.store.save_student('001', '新档案名', '新班级')
        self.store.set_student_active('002', False)
        self.session()
        self.assertEqual(self.store.teacher_dashboard()['pending_review_count'], 0)
        self.assertTrue(self.store.preview_session_review(sid)['reviewed'])

    def test_stale_review_rejects_result_change_and_raw_row_change(self):
        sid = self.session('closed')
        plan = self.store.preview_session_review(sid)
        self.store.correct_attendance(sid, '001', 'leave', '变化')
        with self.assertRaisesRegex(ValueError, '刷新'):
            self.store.mark_session_reviewed(sid, plan['token'])
        plan = self.store.preview_session_review(sid)
        with self.store.connect(write=True) as connection:
            connection.execute("UPDATE checkins SET note='新的备注' WHERE session_id=? AND student_id='001'", (sid,))
        with self.assertRaisesRegex(ValueError, '刷新'):
            self.store.mark_session_reviewed(sid, plan['token'])

    def test_concurrent_identical_confirmation_is_one_event(self):
        sid = self.session('closed')
        plan = self.store.preview_session_review(sid)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.store.mark_session_reviewed(sid, plan['token']), range(3)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len([e for e in self.store.audit_rows(sid) if e['action'] == '确认课次核对完成']), 1)

    def test_failed_review_audit_rolls_back_marker(self):
        sid = self.session('closed')
        plan = self.store.preview_session_review(sid)
        with patch.object(self.store, '_audit', side_effect=sqlite3.OperationalError('failed')), self.assertRaises(sqlite3.Error):
            self.store.mark_session_reviewed(sid, plan['token'])
        self.assertEqual(self.store.teacher_dashboard()['pending_review_count'], 1)
        self.assertIsNone(self.store.preview_session_review(sid)['previous'])

    def test_invalid_signature_and_note_never_mark_reviewed(self):
        sid = self.session('closed')
        plan = self.store.preview_session_review(sid)
        for extra in ({'actor': 'a'*81}, {'note': 'a'*301}):
            with self.assertRaises(ValueError):
                self.store.mark_session_reviewed(sid, plan['token'], **extra)
        self.assertEqual(self.store.teacher_dashboard()['pending_review_count'], 1)

    def test_malformed_review_and_backup_are_visible_as_unknown(self):
        sid = self.session('closed')
        with self.store.connect(write=True) as connection:
            self.store._set(connection, REVIEW_PREFIX+str(sid), '{bad')
            self.store._set(connection, BACKUP_KEY, '{bad')
        data = self.store.teacher_dashboard()
        self.assertEqual(data['pending_review_count'], 1)
        self.assertIn('不可读', data['pending_reviews'][0]['review_text'])
        self.assertTrue(data['last_backup']['invalid'])
        self.mark(sid)
        self.assertEqual(self.store.teacher_dashboard()['pending_review_count'], 0)

    def test_backup_success_only_after_archive_created(self):
        with patch('classroom_attendance.storage.backup_service.create_backup', side_effect=OSError('failed')), self.assertRaises(OSError):
            self.store.backup_archive(self.folder/'failed.zip')
        self.assertIsNone(self.store.last_backup())
        self.store.backup_archive(self.folder/'success.zip')
        self.assertIn('completed_at', self.store.teacher_dashboard()['last_backup'])

    def test_review_marker_survives_backup_and_restore(self):
        sid = self.session('closed')
        self.mark(sid, note='备份核对测试')
        folder = self.store.path.parent
        archive = self.folder/'backup.zip'
        create_backup(self.store, archive, model=folder/'face_model.yml', manifest=folder/'model_manifest.json', dataset=folder/'dataset')
        info = inspect_backup(archive)
        restore_backup(archive, self.folder/'restored', expected_sha256=info['archive_sha256'])
        restored = AttendanceStore(self.folder/'restored'/'attendance.db', seed_path=None)
        self.assertTrue(restored.preview_session_review(sid)['reviewed'])
        self.assertEqual(restored.teacher_dashboard()['pending_review_count'], 0)


class TeacherDashboardGuiTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.errors = patch('classroom_attendance.dashboard.dashboard_ui.messagebox.showerror').start()
        self.addCleanup(patch.stopall)

    def app(self):
        with patch('classroom_attendance.ui.admin_app.model_health', return_value=(False, '模型未训练', None)):
            self.admin = AdminApp(self.root, self.store, authenticated=True, async_io=False)
        self.root.update()
        return self.admin

    def tearDown(self):
        if hasattr(self, 'admin'):
            self.admin.close()
        try:
            for timer in self.root.tk.call('after', 'info'):
                self.root.after_cancel(timer)
            self.root.destroy()
        except tk.TclError:
            pass

    def test_home_is_default_and_model_fallback_and_counts_are_visible(self):
        self.session('open')
        app = self.app()
        self.assertEqual(app.notebook.select(), str(app.pages['home']))
        self.assertIn('当前未签到 3', app.dashboard.stats.get())
        self.assertIn('人工点名', app.dashboard.face_text.get())
        self.assertEqual(len(app.dashboard.today_tree.get_children()), 1)

    def test_home_navigation_selects_correct_session_and_student(self):
        sid = self.session('open')
        app = self.app()
        app.dashboard.manage()
        self.assertEqual(app.notebook.select(), str(app.pages['sessions']))
        self.assertEqual(app.sessions_tree.selection(), (str(sid),))
        app.dashboard.tabs.select(app.dashboard.missing_page)
        app.dashboard.missing_tree.selection_set('002')
        app.dashboard.records()
        self.assertEqual(app.notebook.select(), str(app.pages['records']))
        self.assertEqual(app.record_session_id(), sid)
        self.assertEqual(app.records_tree.selection(), ('002',))

    def test_old_review_navigation_uses_exact_date_and_keeps_export_target(self):
        sid = self.session('closed', start=self.now-timedelta(days=90), end=self.now-timedelta(days=90)+timedelta(hours=1))
        self.session('closed')
        app = self.app()
        app.dashboard.tabs.select(app.dashboard.review_page)
        app.dashboard.review_tree.selection_set(str(sid))
        app.dashboard.records()
        self.assertEqual(app.record_period.get(), '选定课次日期')
        self.assertEqual(tuple(app.record_combo['values']), (app.record_choice.get(),))
        self.assertEqual({r['session_id'] for r in app.record_rows}, {sid})
        self.assertEqual(app.record_session_id(), sid)
        app.record_period.set('当天')
        app.refresh_records()
        self.assertNotIn(sid, {r['session_id'] for r in app.record_rows})

    def test_review_requires_check_and_refreshes_home_after_confirmation(self):
        sid = self.session('closed')
        app = self.app()
        app.show_review(sid)
        dialog = app.review_dialog
        self.assertEqual(str(dialog.save_button['state']), 'disabled')
        dialog.save()
        self.assertEqual(self.store.teacher_dashboard()['pending_review_count'], 1)
        dialog.confirmed.set(True)
        dialog.sync_actions()
        dialog.save()
        self.assertTrue(dialog.saved)
        self.assertEqual(app.dashboard.snapshot['pending_review_count'], 0)
        self.errors.assert_not_called()

    def test_review_stale_confirmation_is_rejected_and_plan_cleared(self):
        sid = self.session('closed')
        app = self.app()
        app.show_review(sid)
        dialog = app.review_dialog
        dialog.confirmed.set(True)
        self.store.correct_attendance(sid, '001', 'leave', '新的核实')
        dialog.save()
        self.assertIsNone(dialog.plan)
        self.assertEqual(self.store.teacher_dashboard()['pending_review_count'], 1)
        self.errors.assert_called_once()

    def test_home_query_error_disables_stale_actions_and_retry_recovers(self):
        self.session('open')
        app = self.app()
        with patch.object(self.store, 'teacher_dashboard', side_effect=sqlite3.OperationalError('busy')):
            app.dashboard.refresh()
        self.assertTrue(app.dashboard.failed)
        self.assertEqual(str(app.dashboard.manual_button['state']), 'disabled')
        app.dashboard.refresh()
        self.assertFalse(app.dashboard.failed)
        self.assertEqual(str(app.dashboard.manual_button['state']), 'normal')

    def test_small_home_and_review_keep_actions_inside_window(self):
        sid = self.session('closed')
        self.mark(sid, note='上次核对说明')
        self.store.correct_attendance(sid, '001', 'leave', '核对后的变更')
        app = self.app()
        self.root.geometry('940x600')
        self.root.update()
        for widget in (app.dashboard.manage_button, app.dashboard.review_button, app.dashboard.refresh_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(), self.root.winfo_rooty()+self.root.winfo_height())
        self.assertGreaterEqual(app.dashboard.today_tree.winfo_height(), 100, '小屏首页仍须显示考勤列表')
        app.show_review(sid)
        dialog = app.review_dialog
        dialog.tabs.select(1)
        self.root.update()
        self.assertTrue(dialog.previous.winfo_ismapped())
        self.assertGreater(dialog.previous.winfo_rooty()+dialog.previous.winfo_height(), dialog.confirmation_form.winfo_rooty())
        dialog.root.geometry('800x550')
        self.root.update()
        self.assertLessEqual(dialog.save_button.winfo_rooty()+dialog.save_button.winfo_height(), dialog.root.winfo_rooty()+dialog.root.winfo_height())
        dialog.tabs.select(1)
        self.root.update()
        self.assertTrue(dialog.confirm_box.winfo_ismapped())
        self.assertLess(dialog.confirm_box.winfo_rooty()+dialog.confirm_box.winfo_height(),
                        dialog.confirmation_form.winfo_rooty()+dialog.confirmation_form.winfo_height())

    def test_slow_home_refresh_keeps_tk_heartbeat(self):
        self.session('open')
        app = self.app()
        dashboard = app.dashboard
        dashboard.async_io = True
        original = self.store.teacher_dashboard
        def slow():
            time.sleep(.2)
            return original()
        heartbeat = []
        self.root.after(20, lambda: heartbeat.append(True))
        with patch.object(self.store, 'teacher_dashboard', side_effect=slow):
            dashboard.refresh()
            deadline = time.monotonic()+3
            while dashboard.loading and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
        self.assertTrue(heartbeat)
        self.assertFalse(dashboard.loading)
        self.errors.assert_not_called()


if __name__ == '__main__':
    unittest.main()
