"""周期日期、冲突、名单快照、批量事务、模板往返与小屏教师流程。"""

import json
import sqlite3
import tempfile
import time
import tkinter as tk
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from classroom_attendance.ui.admin_app import AdminApp
from classroom_attendance.storage.attendance_store import AttendanceStore
from classroom_attendance.storage.backup_service import create_backup, inspect_backup, restore_backup
from classroom_attendance.courses.course_schedule import TEMPLATE_PREFIX, expand_course, normalize_course
from classroom_attendance.core.task_runtime import TaskControl, current_control
from classroom_attendance.core.app_errors import TaskCancelled


class ScheduleFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='course-schedule-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.store = AttendanceStore(self.folder / 'source' / 'attendance.db', seed_path=None)
        for sid, name, group in [('001', '合成甲', '一班'), ('002', '合成乙', '一班'), ('003', '合成丙', '二班')]:
            self.store.save_student(sid, name, group)
        self.definition = dict(name='数学周课', course_name='数学', group_name='一班', start_time='08:00', end_time='09:30',
                               next_day=False, late_minutes=10, weekdays=[0, 2], interval_weeks=1, student_ids=None)
        self.first, self.last = '2027-03-01', '2027-03-10'

    def preview(self, **kwargs):
        return self.store.preview_course_schedule(self.definition, self.first, self.last, **kwargs)

    def create(self, plan, **kwargs):
        return self.store.create_course_schedule(self.definition, self.first, self.last, plan['token'], **kwargs)


class CourseScheduleTests(ScheduleFixture):
    def test_sixteen_weeks_monday_wednesday_and_inclusive_dates(self):
        rows = expand_course(self.definition, '2027-03-01', '2027-06-20')
        self.assertEqual(len(rows), 32)
        self.assertEqual((rows[0]['date'], rows[-1]['date']), ('2027-03-01', '2027-06-16'))
        self.assertEqual(rows[0]['late_at'], '2027-03-01T08:10:00')

    def test_every_other_week_anchors_first_monday_even_partial_week(self):
        self.definition.update(interval_weeks=2)
        rows = expand_course(self.definition, '2027-03-03', '2027-03-22')
        self.assertEqual([r['date'] for r in rows], ['2027-03-03', '2027-03-15', '2027-03-17'])

    def test_cross_midnight_leap_day_year_end_and_late_cap(self):
        self.definition.update(weekdays=list(range(7)), start_time='23:30', end_time='00:20', next_day=True, late_minutes=180)
        rows = expand_course(self.definition, '2028-02-28', '2028-03-01')
        self.assertEqual([r['date'] for r in rows], ['2028-02-28', '2028-02-29', '2028-03-01'])
        self.assertEqual(rows[1]['ends_at'], '2028-03-01T00:20:00')
        self.assertEqual(rows[1]['late_at'], rows[1]['ends_at'])
        self.assertEqual(expand_course(self.definition, '2027-12-31', '2027-12-31')[0]['ends_at'], '2028-01-01T00:20:00')

    def test_invalid_course_and_date_input_is_rejected(self):
        for values in [dict(course_name=''), dict(group_name=''), dict(name='a'*81), dict(start_time='8:00'), dict(end_time='24:00'),
                       dict(end_time='07:00'), dict(next_day=True), dict(late_minutes=-1), dict(late_minutes=181),
                       dict(interval_weeks=0), dict(interval_weeks=9), dict(weekdays=[]), dict(weekdays=[True]),
                       dict(weekdays=[7]), dict(student_ids=[]), dict(student_ids=['001', '001']), dict(student_ids=['x'])]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                normalize_course(dict(self.definition, **values))
        for first, last in [('2027-3-1', self.last), ('2027-02-30', self.last), (self.last, self.first), ('2027-01-01', '2028-01-02')]:
            with self.subTest(first=first), self.assertRaises(ValueError):
                expand_course(self.definition, first, last)
        with self.assertRaisesRegex(ValueError, '没有符合'):
            expand_course(self.definition, '2027-03-02', '2027-03-02')
        with self.assertRaisesRegex(ValueError, '跳过日期'):
            expand_course(self.definition, self.first, self.last, ['2027-03-02'])

    def test_preview_read_only_and_creation_produces_draft_snapshots(self):
        plan = self.preview()
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual((plan['included'], len(plan['members'])), (4, 2))
        ids = self.create(plan, actor='课程署名')
        self.assertEqual(len(ids), 4)
        self.assertEqual({r['state'] for r in self.store.sessions()}, {'draft'})
        self.assertEqual({r['status'] for r in self.store.session_rows(ids[0])}, {'not_started'})
        self.assertEqual(self.store.audit_rows()[0]['actor_kind'], 'management_signature')

    def test_any_course_overlap_conflicts_but_adjacent_and_cancelled_do_not(self):
        for start, end in [('07:00', '08:00'), ('09:30', '10:00')]:
            self.store.create_session('相邻', '二班', datetime.fromisoformat(self.first+'T'+start), datetime.fromisoformat(self.first+'T'+end))
        cancelled = self.store.create_session('已停课', '二班', datetime(2027, 3, 1, 8), datetime(2027, 3, 1, 9))
        self.store.cancel_session(cancelled, '停课')
        self.assertEqual(self.preview()['conflict_count'], 0)
        conflict = self.store.create_session('另一课程', '二班', datetime(2027, 3, 1, 9, 29), datetime(2027, 3, 1, 10))
        plan = self.preview()
        self.assertEqual(plan['conflict_count'], 1)
        self.assertEqual(plan['rows'][0]['conflicts'][0]['id'], conflict)
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.create(plan)

    def test_skip_holiday_and_conflict_dates_and_reject_all_skipped(self):
        self.store.create_session('占用', '二班', datetime(2027, 3, 1, 8), datetime(2027, 3, 1, 10))
        exclusions = ['2027-03-01', '2027-03-03']
        plan = self.preview(excluded_dates=exclusions)
        ids = self.create(plan, excluded_dates=exclusions)
        self.assertEqual([self.store.session(i)['starts_at'][:10] for i in ids], ['2027-03-08', '2027-03-10'])
        exclusions = [r['date'] for r in self.preview()['rows']]
        with self.assertRaisesRegex(ValueError, '至少保留'):
            self.create(self.preview(excluded_dates=exclusions), excluded_dates=exclusions)

    def test_conflict_across_previous_midnight_is_found(self):
        previous = self.store.create_session('夜间课', '二班', datetime(2027, 2, 28, 23), datetime(2027, 3, 1, 8, 1))
        self.assertEqual(self.preview()['rows'][0]['conflicts'][0]['id'], previous)

    def test_fixed_enrollment_and_historical_snapshot_stay_independent(self):
        self.definition.update(group_name='全部学生', student_ids=['001', '003'])
        ids = self.create(self.preview())
        self.store.save_student('001', '后来改名', '二班')
        self.store.set_student_active('003', False)
        self.store.save_student('004', '新加入', '一班')
        rows = self.store.session_rows(ids[0])
        self.assertEqual([(r['student_id'], r['name']) for r in rows], [('001', '合成甲'), ('003', '合成丙')])
        with self.assertRaisesRegex(ValueError, '停用'):
            self.preview()

    def test_dynamic_class_uses_current_active_members_and_detects_change(self):
        plan = self.preview()
        self.store.save_student('004', '新成员', '一班')
        with self.assertRaisesRegex(ValueError, '重新预览'):
            self.create(plan)
        plan = self.preview()
        self.assertEqual(len(plan['members']), 3)
        self.store.save_student('001', '新姓名', '一班')
        with self.assertRaisesRegex(ValueError, '重新预览'):
            self.create(plan)

    def test_stale_plan_rejects_new_overlap_and_changed_parameters(self):
        plan = self.preview()
        self.definition['late_minutes'] = 15
        with self.assertRaisesRegex(ValueError, '重新预览'):
            self.create(plan)
        plan = self.preview()
        self.store.create_session('新占用', '二班', datetime(2027, 3, 3, 8), datetime(2027, 3, 3, 9))
        with self.assertRaisesRegex(ValueError, '重新预览'):
            self.create(plan)

    def test_unrelated_course_and_student_changes_do_not_invalidate_fixed_plan(self):
        self.definition['student_ids'] = ['001']
        plan = self.preview()
        self.store.save_student('003', '新名字', '二班')
        self.store.create_session('不重叠', '二班', datetime(2027, 3, 1, 12), datetime(2027, 3, 1, 13))
        self.assertEqual(len(self.create(plan)), 4)

    def test_batch_audit_failure_rolls_back_all_sessions_and_snapshots(self):
        plan = self.preview()
        original, calls = self.store._audit, []
        def fail_second(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise sqlite3.OperationalError('failed')
            return original(*args, **kwargs)
        with patch.object(self.store, '_audit', side_effect=fail_second), self.assertRaises(sqlite3.Error):
            self.create(plan)
        self.assertEqual(self.store.sessions(), [])
        with self.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM session_students').fetchone()[0], 0)

    def test_cancelled_generation_rolls_back_all_sessions(self):
        plan = self.preview()
        control = TaskControl()
        original = self.store._insert_session
        def cancel_first(*args, **kwargs):
            result = original(*args, **kwargs)
            control.cancelled.set()
            return result
        token = current_control.set(control)
        try:
            with patch.object(self.store, '_insert_session', side_effect=cancel_first), self.assertRaises(TaskCancelled):
                self.create(plan)
        finally:
            current_control.reset(token)
        self.assertEqual(self.store.sessions(), [])

    def test_concurrent_and_repeated_generation_never_duplicates(self):
        plan = self.preview()
        def submit(_):
            try:
                return len(self.create(plan))
            except ValueError:
                return 0
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(sum(pool.map(submit, range(3))), 4)
        self.assertEqual(len(self.store.sessions()), 4)
        self.assertEqual(self.preview()['conflict_count'], 4)

    def test_template_roundtrip_revision_and_existing_sessions_unchanged(self):
        template = self.store.save_course_template(self.definition, actor='教师署名')
        other = AttendanceStore(self.store.path, seed_path=None)
        self.assertEqual(other.course_templates()[0], template)
        ids = self.create(self.preview())
        changed = dict(self.definition, course_name='新数学', start_time='10:00', end_time='11:30')
        updated = other.save_course_template(changed, template_id=template['id'], revision=template['revision'])
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(self.store.session(ids[0])['course_name'], '数学')
        with self.assertRaisesRegex(ValueError, '重新加载'):
            self.store.save_course_template(self.definition, template_id=template['id'], revision=template['revision'])

    def test_template_duplicate_name_invalid_roster_and_no_change_are_handled(self):
        template = self.store.save_course_template(self.definition)
        count = len(self.store.audit_rows())
        self.assertEqual(self.store.save_course_template(self.definition, template_id=template['id'], revision=1), template)
        self.assertEqual(len(self.store.audit_rows()), count)
        for definition in [self.definition, dict(self.definition, name=''), dict(self.definition, name='新模板', student_ids=['999'])]:
            with self.assertRaises(ValueError):
                self.store.save_course_template(definition)
        self.assertEqual(len(self.store.course_templates()), 1)

    def test_template_audit_failure_rolls_back_and_corruption_is_not_silent(self):
        with patch.object(self.store, '_audit', side_effect=sqlite3.OperationalError('failed')), self.assertRaises(sqlite3.Error):
            self.store.save_course_template(self.definition)
        self.assertEqual(self.store.course_templates(), [])
        with self.store.connect(write=True) as connection:
            self.store._set(connection, TEMPLATE_PREFIX + '0123456789abcdef', '{bad')
        with self.assertRaisesRegex(ValueError, '损坏'):
            self.store.course_templates()

    def test_template_backup_restore_preserves_revision_and_schedule(self):
        template = self.store.save_course_template(self.definition)
        self.create(self.preview())
        folder = self.store.path.parent
        archive = self.folder / 'backup.zip'
        create_backup(self.store, archive, model=folder/'face_model.yml', manifest=folder/'model_manifest.json', dataset=folder/'dataset')
        summary = inspect_backup(archive)
        restore_backup(archive, self.folder/'restored', expected_sha256=summary['archive_sha256'])
        restored = AttendanceStore(self.folder/'restored'/'attendance.db', seed_path=None)
        self.assertEqual(restored.course_templates(), [template])
        self.assertEqual(len(restored.sessions()), 4)


class CourseScheduleGuiTests(ScheduleFixture):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.errors = patch('classroom_attendance.courses.course_schedule_ui.messagebox.showerror').start()
        self.confirm = patch('classroom_attendance.courses.course_schedule_ui.messagebox.askyesno', return_value=True).start()
        self.addCleanup(patch.stopall)
        with patch('classroom_attendance.ui.admin_app.model_health', return_value=(False, '模型未就绪', None)):
            self.app = AdminApp(self.root, self.store, authenticated=True, async_io=False)
        self.app.course.set('数学')
        self.app.session_group.set('一班')
        self.app.course_schedule()
        self.dialog = self.app.schedule_dialog
        self.root.update()
        self.dialog.variables['first_date'].set(self.first)
        self.dialog.variables['last_date'].set(self.last)
        self.dialog.variables['name'].set('数学周课')
        for i, variable in enumerate(self.dialog.days):
            variable.set(i in [0, 2])

    def tearDown(self):
        self.app.close()
        try:
            for timer in self.root.tk.call('after', 'info'):
                self.root.after_cancel(timer)
            self.root.destroy()
        except tk.TclError:
            pass

    def test_preview_confirm_and_teacher_refresh(self):
        dialog = self.dialog
        self.assertEqual(str(dialog.create_button['state']), 'disabled')
        dialog.preview()
        self.assertEqual(len(dialog.preview_tree.get_children()), 4)
        self.assertEqual(str(dialog.create_button['state']), 'normal')
        dialog.create()
        self.assertEqual(len(self.app.sessions_data), 4)
        self.assertIsNone(dialog.plan)
        self.assertIn('已生成 4', dialog.message.get())
        self.errors.assert_not_called()

    def test_edit_invalidates_preview_and_decline_keeps_database_empty(self):
        dialog = self.dialog
        dialog.preview()
        dialog.variables['late_minutes'].set('15')
        self.assertIsNone(dialog.plan)
        dialog.preview()
        self.confirm.return_value = False
        dialog.create()
        self.assertEqual(self.store.sessions(), [])

    def test_skip_holiday_conflict_and_restore_all_dates(self):
        dialog = self.dialog
        self.store.create_session('占用', '二班', datetime(2027, 3, 1, 8), datetime(2027, 3, 1, 9))
        dialog.preview()
        self.assertEqual(str(dialog.create_button['state']), 'disabled')
        dialog.skip_conflicts()
        self.assertEqual(dialog.plan['included'], 3)
        self.assertEqual(dialog.plan['conflict_count'], 0)
        dialog.preview_tree.selection_set('2027-03-03')
        dialog.skip_selected()
        self.assertEqual(dialog.plan['included'], 2)
        dialog.reset_exclusions()
        self.assertEqual(dialog.plan['included'], 4)

    def test_fixed_selection_template_reload_and_members_are_preserved_on_save(self):
        dialog = self.dialog
        dialog.variables['roster_mode'].set('固定选课名单')
        dialog.member_tree.selection_set('001')
        dialog.save_template()
        self.assertEqual(dialog.member_tree.selection(), ('001',))
        dialog.new_template()
        dialog.variables['template_choice'].set(next(iter(dialog.template_choices)))
        dialog.load_selected()
        self.assertEqual(dialog.definition()['student_ids'], ['001'])
        dialog.preview()
        self.assertEqual(len(dialog.plan['members']), 1)
        self.errors.assert_not_called()

    def test_stale_overlap_confirmation_never_creates_partial_schedule(self):
        dialog = self.dialog
        dialog.preview()
        self.store.create_session('后来占用', '二班', datetime(2027, 3, 1, 8), datetime(2027, 3, 1, 9))
        dialog.create()
        self.assertEqual(len(self.store.sessions()), 1)
        self.assertIsNone(dialog.plan)
        self.errors.assert_called_once()

    def test_small_screen_fixed_actions_and_scrollable_fields(self):
        dialog = self.dialog
        dialog.root.geometry('800x550')
        self.root.update()
        for widget in (dialog.close_button, dialog.preview_button, dialog.create_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(), dialog.root.winfo_rooty()+dialog.root.winfo_height())
        dialog.edit_scroll.canvas.yview_moveto(1)
        self.root.update()
        self.assertGreater(dialog.edit_scroll.canvas.yview()[0], 0)

    def test_slow_preview_allows_tk_heartbeat_and_blocks_close(self):
        dialog = self.dialog
        original = self.store.preview_course_schedule
        dialog.async_io = True
        def slow(*args, **kwargs):
            time.sleep(.2)
            return original(*args, **kwargs)
        heartbeat = []
        self.root.after(20, lambda: heartbeat.append(True))
        with patch.object(self.store, 'preview_course_schedule', side_effect=slow):
            dialog.preview()
            dialog.close()
            self.app.close()
            self.assertFalse(dialog.closed)
            deadline = time.monotonic()+3
            while dialog.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
        self.assertTrue(heartbeat)
        self.assertIsNotNone(dialog.plan)
        self.errors.assert_not_called()


if __name__ == '__main__':
    unittest.main()
