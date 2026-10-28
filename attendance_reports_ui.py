"""教师统计报表页，查询与导出共用最近生成的快照。"""

import tkinter as tk
from functools import partial
from tkinter import filedialog, ttk

from app_errors import log_error, user_message
from attendance_reports import REPORT_RULES, REPORT_STATUSES, SUMMARY_FIELDS, SUMMARY_HEADERS, rate_text, report_context
from task_runtime import FlowHost, IO, ui_flow
from ui_common import MUTED, WHITE, button, label, replace_rows, tree


class AttendanceReportsPanel(tk.Frame, FlowHost):
    def __init__(self, owner, parent):
        tk.Frame.__init__(self, parent, bg=WHITE)
        self.owner, self.store = owner, owner.store
        self.init_flows(owner.root, owner.async_io)
        self.snapshot = None
        self.loading = False
        self.starts_on, self.ends_on = tk.StringVar(), tk.StringVar()
        self.course, self.group = tk.StringVar(value='全部课程'), tk.StringVar(value='全部班级')
        self.stats = tk.StringVar(value='选择学期或自定义日期，点击“生成报表”。')
        self.message = tk.StringVar(value='按正式上课日期归属；只计算已结束课次。')
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        dates = tk.Frame(self, bg=WHITE)
        dates.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        label(dates, '日期（含首尾）', size=9, color=MUTED).pack(side='left')
        self.start_entry = ttk.Entry(dates, textvariable=self.starts_on, width=13)
        self.start_entry.pack(side='left', padx=(10, 4))
        label(dates, '至', size=9).pack(side='left')
        self.end_entry = ttk.Entry(dates, textvariable=self.ends_on, width=13)
        self.end_entry.pack(side='left', padx=(4, 10))
        self.semester_button = button(dates, '使用当前学期', self.use_semester, plain=True)
        self.semester_button.pack(side='left')
        button(dates, '学期设置', owner.configure_semester, plain=True).pack(side='right')
        button(dates, '统计口径', self.show_rules, plain=True).pack(side='right')
        filters = tk.Frame(self, bg=WHITE)
        filters.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        self.course_combo = ttk.Combobox(filters, textvariable=self.course, values=['全部课程'], state='readonly', width=25)
        self.course_combo.pack(side='left', padx=(0, 10))
        self.group_combo = ttk.Combobox(filters, textvariable=self.group, values=['全部班级'], state='readonly', width=22)
        self.group_combo.pack(side='left')
        self.generate_button = button(filters, '生成报表', self.refresh, primary=True)
        self.generate_button.pack(side='right')
        self.export_button = button(filters, '导出 Excel', self.export)
        self.export_button.pack(side='right', padx=8)
        summary = label(self, textvariable=self.stats, size=10, bold=True, justify='left')
        summary.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        summary.bind('<Configure>', lambda event: summary.configure(wraplength=max(1, event.width)))
        self.tabs = ttk.Notebook(self)
        self.tabs.grid(row=3, column=0, sticky='nsew')
        self.student_page, self.course_page, self.matrix_page = [tk.Frame(self.tabs, bg=WHITE) for _ in range(3)]
        for page, title in ((self.student_page, '学生学期汇总'), (self.course_page, '课程汇总'), (self.matrix_page, '学生 × 课次矩阵')):
            self.tabs.add(page, text=title)
        summary_columns = [(key, title, 120 if 'rate' in key else 100) for key, title in zip(SUMMARY_FIELDS, SUMMARY_HEADERS)]
        frame, self.student_tree = tree(self.student_page, [('id', '学号', 140), ('name', '姓名（最近快照）', 135),
                                                           ('group', '班级（最近快照）', 140), *summary_columns],
                                        empty_text='暂无统计数据\n选择日期后生成报表')
        frame.pack(fill='both', expand=True)
        frame, self.course_tree = tree(self.course_page, [('course', '课程', 180), ('group', '课次班级', 140),
                                                         ('sessions', '已结束课次数', 110), *summary_columns], empty_text='暂无已结束课次')
        frame.pack(fill='both', expand=True)
        frame, self.matrix_tree = tree(self.matrix_page, [('id', '学号', 140), ('name', '姓名（最近快照）', 135),
                                                         ('group', '班级（最近快照）', 140)], empty_text='暂无应到学生\n“—”表示不在该次名单中')
        frame.pack(fill='both', expand=True)
        self.matrix_tree.bind('<Double-1>', self.show_cell)
        self.matrix_tree.bind('<Return>', self.show_student)
        footer = label(self, textvariable=self.message, size=9, color=MUTED, justify='left')
        footer.grid(row=4, column=0, sticky='ew', pady=(8, 0))
        footer.bind('<Configure>', lambda event: footer.configure(wraplength=max(1, event.width)))
        for variable in (self.starts_on, self.ends_on, self.course, self.group):
            variable.trace_add('write', self.filters_changed)
        self.sync_actions()

    def schedule(self, milliseconds, callback):
        return self.owner.schedule(milliseconds, callback) if not self.closed else None

    def say(self, message):
        self.message.set(message)

    def handle_error(self, error):
        log_error('attendance_reports_failed', error)
        self.say(user_message(error))

    def parameters(self):
        return (self.starts_on.get().strip(), self.ends_on.get().strip(),
                self.course.get().removeprefix('课程：') if self.course.get() != '全部课程' else None,
                self.group.get().removeprefix('班级：') if self.group.get() != '全部班级' else None)

    def matches_snapshot(self):
        return bool(self.snapshot and self.parameters() == tuple(self.snapshot[key] for key in
                    ('starts_on', 'ends_on', 'course_name', 'group_name')))

    def sync_actions(self):
        self.generate_button.configure(state='disabled' if self.loading else 'normal')
        self.semester_button.configure(state='disabled' if self.loading else 'normal')
        self.export_button.configure(state='normal' if self.matches_snapshot() and not self.loading else 'disabled')

    def filters_changed(self, *args):
        if self.snapshot and not self.matches_snapshot():
            self.say('筛选已改变，表格仍为上次快照；请点击“生成报表”后导出。')
        self.sync_actions()

    def on_selected(self, event=None):
        if self.owner.notebook.select() == str(self.owner.pages['reports']) and self.snapshot is None and not self.loading:
            if not self.starts_on.get() and not self.ends_on.get():
                self.use_semester()

    @ui_flow
    def use_semester(self):
        if self.loading or self.closed:
            return
        self.loading = True
        self.sync_actions()
        try:
            start, end = yield IO(self.store.semester_dates)
            self.starts_on.set(start.isoformat())
            self.ends_on.set(end.isoformat())
        finally:
            self.loading = False
            self.sync_actions()
        yield from self.refresh()

    @ui_flow
    def refresh(self):
        if self.loading or self.closed:
            return
        requested = self.parameters()
        self.loading = True
        self.sync_actions()
        self.say('正在生成报表…')
        try:
            start, end, course, group = requested
            result = yield IO(partial(self.store.attendance_report, start, end, course_name=course, group_name=group))
            if requested != self.parameters():
                self.say('筛选在查询期间已改变，请重新生成报表。')
                return
            self.starts_on.set(result['starts_on'])
            self.ends_on.set(result['ends_on'])
            self.snapshot = result
            self.render()
        except Exception:
            self.snapshot = None
            self.stats.set('生成失败；请检查日期或重试。表格保留上次显示，不可导出。')
            raise
        finally:
            self.loading = False
            self.sync_actions()

    def render(self):
        report = self.snapshot
        self.course_combo.configure(values=list(dict.fromkeys(['全部课程', *['课程：'+value for value in report['options']['courses']], self.course.get()])))
        self.group_combo.configure(values=list(dict.fromkeys(['全部班级', *['班级：'+value for value in report['options']['groups']], self.group.get()])))
        totals = report['totals']
        self.stats.set(f"已结束 {len(report['sessions'])} 课次 · 学生 {len(report['students'])} 人 · 应到 {totals['expected']} 人次 · "
                       f"已到 {totals['arrived']} · 迟到 {totals['late']} · 请假 {totals['leave']} · 缺勤 {totals['absent']} · 出勤率 {rate_text(totals['attendance_rate'])}")

        def values(row):
            return [rate_text(row[key]) if 'rate' in key else row[key] for key in SUMMARY_FIELDS]

        replace_rows(self.student_tree, [(row['student_id'], [row['student_id'], row['name'], row['group_name'], *values(row)], '')
                                         for row in report['students']])
        replace_rows(self.course_tree, [(index, [row['course_name'], row['group_name'], len(row['session_ids']), *values(row)], '')
                                        for index, row in enumerate(report['courses'])])
        columns = [('id', '学号', 140), ('name', '姓名（最近快照）', 135), ('group', '班级（最近快照）', 140)]
        columns.extend((f"session_{session['id']}", f"#{session['id']} {session['course_name']} / {session['group_name']} / {session['starts_at'].replace('T', ' ')[:16]}", 320)
                       for session in report['sessions'])
        self.matrix_tree.configure(columns=[column[0] for column in columns])
        for key, title, width in columns:
            self.matrix_tree.heading(key, text=title, anchor='w')
            self.matrix_tree.column(key, width=width, minwidth=width, anchor='w')
        replace_rows(self.matrix_tree, [(row['student_id'], [row['student_id'], row['name'], row['group_name']] + [
            REPORT_STATUSES.get(row['cells'].get(session['id']), '—') for session in report['sessions']], '') for row in report['students']])
        self.say(f"出勤率含迟到，请假计应到；双击矩阵格查看明细。快照：{report['generated_at'].replace('T', ' ')}")

    def show_rules(self):
        context = report_context(self.snapshot) + '\n\n' if self.snapshot else ''
        self.owner.text_details('统计口径', context + '\n\n'.join(REPORT_RULES))

    def show_cell(self, event):
        column = self.matrix_tree.identify_column(event.x)
        item = self.matrix_tree.identify_row(event.y)
        if not item or not column or not self.snapshot:
            return
        index = int(column[1:]) - 4
        if index < 0:
            self.show_student(student_id=item)
            return
        session = self.snapshot['sessions'][index]
        row = next((row for row in self.snapshot['rows'] if row['student_id'] == item and row['session_id'] == session['id']), None)
        self.owner.text_details('课次快照明细', self.row_text(row) if row else f"课次 #{session['id']}：该学生不在本次应到名单中。")

    @staticmethod
    def row_text(row):
        return (f"#{row['session_id']} {row['course_name']} / {row['session_group_name']} · {row['session_starts_at'].replace('T', ' ')}\n"
                f"开放签到：{row['session_opens_at'].replace('T', ' ')}\n正式上课：{row['session_starts_at'].replace('T', ' ')}\n"
                f"迟到起点：{row['session_late_at'].replace('T', ' ')}\n关闭签到：{row['session_ends_at'].replace('T', ' ')}\n"
                f"{row['student_id']} {row['name']} / {row['group_name']} · {REPORT_STATUSES[row['status']]}\n"
                f"实际到课：{row['signed_at'] or ('时间未确认' if row['status'] in {'present', 'late'} else '—')}\n"
                f"首次人脸签到：{row['original_signed_at'] or '—'}\n备注：{row['note'] or '—'}")

    def show_student(self, event=None, student_id=None):
        selection = self.matrix_tree.selection()
        student_id = student_id or (selection[0] if selection else None)
        if not self.snapshot or not student_id:
            return
        self.owner.text_details('学生考勤快照明细', '\n\n'.join(self.row_text(row) for row in self.snapshot['rows'] if row['student_id'] == student_id))

    @ui_flow
    def export(self):
        if self.loading or not self.matches_snapshot():
            self.say('请先按当前筛选生成报表。')
            return
        report = self.snapshot
        path = filedialog.asksaveasfilename(parent=self.root, title='导出统计报表', defaultextension='.xlsx',
                                          initialfile=f"考勤统计_{report['starts_on']}_{report['ends_on']}.xlsx",
                                          filetypes=[('Excel 统计报表', '*.xlsx')])
        if path:
            from maintenance_tools import MaintenanceService
            timeouts = yield IO(MaintenanceService(self.store).timeouts)
            self.owner.run_job('导出统计报表', partial(self.store.export_attendance_report, path, report),
                               done=lambda result: self.say('已导出页面快照：学生汇总、课程汇总、考勤矩阵、明细与统计口径。'), timeout=timeouts['export'])
