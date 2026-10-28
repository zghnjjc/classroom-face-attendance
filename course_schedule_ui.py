"""课程模板和周期课次窗口：编辑、名单选择、日期及冲突预览。"""

import tkinter as tk
from datetime import date, timedelta
from functools import partial
from tkinter import messagebox, ttk

from app_errors import log_error, user_message
from course_schedule import WEEKDAY_TEXT, course_times
from session_times import session_time_text
from task_runtime import FlowHost, IO, ui_flow
from ui_common import MUTED, WHITE, ScrollableFrame, button, entry_field, fit_window, label, replace_rows, tree


class CourseScheduleDialog(FlowHost):
    def __init__(self, owner):
        self.owner, self.store = owner, owner.store
        window = tk.Toplevel(owner.root)
        window.title('课程模板与周期课次')
        window.configure(bg=WHITE)
        fit_window(window, '1000x760', minimum=(760, 520))
        window.transient(owner.root)
        window.grab_set()
        self.init_flows(window, owner.async_io)
        self.busy, self.plan, self.current, self.templates = False, None, None, []
        self.students, self.excluded, self.loaded_ids = [], set(), None
        self.rendering = False
        self.after_ids = []
        window.protocol('WM_DELETE_WINDOW', self.close)
        self.variables = {name: tk.StringVar(value=value) for name, value in {
            'name': '', 'course_name': owner.course.get(), 'group_name': owner.session_group.get(),
            'start_time': '08:00', 'opens_time': '07:50', 'late_time': '08:10', 'end_time': '09:30',
            'late_minutes': owner.grace.get(), 'interval_weeks': '1',
            'first_date': date.today().isoformat(), 'last_date': (date.today()+timedelta(weeks=16)-timedelta(days=1)).isoformat(),
            'roster_mode': '按启用班级', 'actor': '', 'template_choice': '新模板'}.items()}
        self.next_day = tk.BooleanVar()
        self.opens_previous_day = tk.BooleanVar()
        self.late_next_day = tk.BooleanVar()
        self.days = [tk.BooleanVar(value=i == date.today().weekday()) for i in range(7)]
        self.message = tk.StringVar(value='先填写课程与周期，再预览日期及冲突。生成后均为待开启，不会自动开课。')
        actions = tk.Frame(window, bg=WHITE)
        actions.pack(side='bottom', fill='x', padx=20, pady=12)
        self.close_button = button(actions, '返回', self.close, plain=True)
        self.close_button.pack(side='left')
        self.preview_button = button(actions, '预览课次', self.preview)
        self.preview_button.pack(side='right', padx=(8, 0))
        self.create_button = button(actions, '确认生成', self.create, primary=True)
        self.create_button.pack(side='right')
        feedback = label(window, textvariable=self.message, size=9, color=MUTED, justify='left')
        feedback.pack(side='bottom', fill='x', padx=20, pady=(0, 6))
        feedback.bind('<Configure>', lambda e: feedback.configure(wraplength=max(1, e.width)))
        self.tabs = ttk.Notebook(window)
        self.tabs.pack(fill='both', expand=True, padx=20, pady=(16, 0))
        self.edit_page = tk.Frame(self.tabs, bg=WHITE)
        self.members_page = tk.Frame(self.tabs, bg=WHITE, pady=12)
        self.preview_page = tk.Frame(self.tabs, bg=WHITE, pady=12)
        self.tabs.add(self.edit_page, text='1. 课程与周期')
        self.tabs.add(self.members_page, text='2. 选课名单')
        self.tabs.add(self.preview_page, text='3. 日期与冲突')
        scroll = ScrollableFrame(self.edit_page)
        scroll.pack(fill='both', expand=True)
        self.edit_scroll = scroll
        form = scroll.body
        form.columnconfigure(0, weight=1)
        template_bar = tk.Frame(form, bg=WHITE)
        template_bar.grid(row=0, column=0, sticky='ew', pady=10)
        self.template_combo = ttk.Combobox(template_bar, textvariable=self.variables['template_choice'], state='readonly', width=28)
        self.template_combo.pack(side='left', fill='x', expand=True)
        self.template_combo.bind('<<ComboboxSelected>>', self.load_selected)
        self.new_button = button(template_bar, '新建模板', self.new_template, plain=True)
        self.new_button.pack(side='left', padx=8)
        self.save_button = button(template_bar, '保存模板', self.save_template)
        self.save_button.pack(side='right')
        entry_field(form, '模板名称（保存模板时必填）', self.variables['name'], 1)
        entry_field(form, '课程名称', self.variables['course_name'], 3)
        label(form, '班级（固定名单也须属于此班级；跨班选课请选择全部学生）', size=9, color=MUTED).grid(row=5, column=0, sticky='w', pady=(10, 5))
        self.group_combo = ttk.Combobox(form, textvariable=self.variables['group_name'], state='readonly')
        self.group_combo.grid(row=6, column=0, sticky='ew')
        clock = tk.Frame(form, bg=WHITE)
        clock.grid(row=7, column=0, sticky='ew', pady=(8, 0))
        clock.columnconfigure(0, weight=1)
        clock.columnconfigure(1, weight=1)
        for index, (key, text) in enumerate((('opens_time', '开放签到（时:分）'), ('start_time', '正式上课（时:分）'),
                                           ('late_time', '迟到起点（时:分）'), ('end_time', '关闭签到（时:分）'))):
            box = tk.Frame(clock, bg=WHITE)
            box.grid(row=index//2, column=index%2, sticky='ew', padx=(0, 12) if index%2 == 0 else 0)
            box.columnconfigure(0, weight=1)
            entry_field(box, text, self.variables[key], 0)
        ttk.Checkbutton(form, text='关闭签到在次日（日期与星期按正式上课日）', variable=self.next_day).grid(row=8, column=0, sticky='w', pady=10)
        offsets = tk.Frame(form, bg=WHITE)
        offsets.grid(row=9, column=0, sticky='w')
        ttk.Checkbutton(offsets, text='开放签到在前日', variable=self.opens_previous_day).pack(side='left')
        ttk.Checkbutton(offsets, text='迟到起点在次日', variable=self.late_next_day).pack(side='left', padx=20)
        label(form, '开放 ≤ 上课 ≤ 迟到 ≤ 关闭；上课早于关闭；开放至关闭最多 24 小时。', size=9, color=MUTED).grid(row=10, column=0, sticky='w', pady=(8, 0))
        label(form, '每周上课日', size=9, color=MUTED).grid(row=11, column=0, sticky='w', pady=(12, 5))
        weekdays = tk.Frame(form, bg=WHITE)
        weekdays.grid(row=12, column=0, sticky='w')
        for i, text in enumerate(WEEKDAY_TEXT):
            ttk.Checkbutton(weekdays, text=text, variable=self.days[i]).grid(row=0, column=i, padx=(0, 8))
        entry_field(form, '每隔几周（1–8；首日期所在的周一为第 1 周起点）', self.variables['interval_weeks'], 13)
        entry_field(form, '生成首日期（年-月-日，包含当天）', self.variables['first_date'], 15)
        entry_field(form, '生成末日期（年-月-日，包含当天，最多 366 天）', self.variables['last_date'], 17)
        entry_field(form, '操作署名（选填，未经账号认证）', self.variables['actor'], 19)
        note = label(form, '模板不保存本次日期范围和跳过日期。修改模板不会改动已经生成的课次或历史名单。\n生成后仍需在课次管理逐次开启；时间重叠的课次需跳过或调整。', size=9, color=MUTED, justify='left')
        note.grid(row=21, column=0, sticky='ew', pady=16)
        note.bind('<Configure>', lambda e: note.configure(wraplength=max(1, e.width)))
        self.build_members()
        self.build_preview()
        for key, variable in self.variables.items():
            if key != 'template_choice':
                variable.trace_add('write', lambda *_, field=key: self.edited(field))
        for variable in [self.next_day, self.opens_previous_day, self.late_next_day, *self.days]:
            variable.trace_add('write', lambda *_: self.edited('schedule'))
        self.sync_actions()
        self.reload()

    def build_members(self):
        toolbar = tk.Frame(self.members_page, bg=WHITE)
        toolbar.pack(fill='x', pady=(0, 12))
        ttk.Combobox(toolbar, textvariable=self.variables['roster_mode'], values=['按启用班级', '固定选课名单'], state='readonly', width=18).pack(side='left')
        button(toolbar, '全选当前班级', self.select_members, plain=True).pack(side='right')
        self.member_summary = label(self.members_page, '读取启用学生…', color=MUTED, size=9)
        self.member_summary.pack(side='bottom', anchor='w', pady=(10, 0))
        frame, self.member_tree = tree(self.members_page, [('id', '学号', 130), ('name', '姓名', 130), ('group', '班级', 180)], empty_text='此班级暂无启用学生')
        frame.pack(fill='both', expand=True)
        self.member_tree.configure(selectmode='extended')
        self.member_tree.bind('<<TreeviewSelect>>', self.members_changed)

    def build_preview(self):
        toolbar = tk.Frame(self.preview_page, bg=WHITE)
        toolbar.pack(fill='x', pady=(0, 12))
        self.skip_button = button(toolbar, '跳过所选日期', self.skip_selected, plain=True)
        self.skip_button.pack(side='left')
        self.conflicts_button = button(toolbar, '跳过全部冲突', self.skip_conflicts, plain=True)
        self.conflicts_button.pack(side='left', padx=6)
        self.reset_button = button(toolbar, '恢复全部日期', self.reset_exclusions, plain=True)
        self.reset_button.pack(side='left')
        self.detail_button = button(toolbar, '日期详情', self.show_date_detail, plain=True)
        self.detail_button.pack(side='right')
        self.roster_detail_button = button(toolbar, '名单快照', self.show_roster, plain=True)
        self.roster_detail_button.pack(side='right')
        self.preview_summary = label(self.preview_page, '尚未预览。', color=MUTED, size=9, justify='left')
        self.preview_summary.pack(side='bottom', fill='x', pady=(10, 0))
        self.preview_summary.bind('<Configure>', lambda e: self.preview_summary.configure(wraplength=max(1, e.width)))
        frame, self.preview_tree = tree(self.preview_page, [('date', '上课日期 / 星期', 150), ('open', '开放签到', 165),
                                                        ('start', '正式上课', 165), ('late', '迟到起点', 165),
                                                        ('end', '关闭签到', 165), ('result', '处理', 90), ('conflict', '冲突课次', 240)],
                                        empty_text='填写课程与周期后点击底部“预览课次”')
        frame.pack(fill='both', expand=True)
        self.preview_tree.configure(selectmode='extended')
        self.preview_tree.bind('<Double-1>', lambda e: self.show_date_detail())

    def schedule(self, milliseconds, callback):
        if not self.closed:
            self.after_ids.append(self.root.after(milliseconds, callback))

    def say(self, message):
        self.message.set(message)

    def handle_error(self, error):
        log_error('course_schedule_failed', error)
        self.say(user_message(error))
        messagebox.showerror('周期课次未完成', user_message(error), parent=self.root)

    def sync_actions(self):
        self.template_combo.configure(state='disabled' if self.busy else 'readonly')
        for widget in (self.close_button, self.preview_button, self.save_button, self.new_button):
            widget.configure(state='disabled' if self.busy else 'normal')
        valid = bool(self.plan and self.plan['included'] and not self.plan['conflict_count'])
        self.create_button.configure(state='normal' if valid and not self.busy else 'disabled')
        for widget in (self.skip_button, self.conflicts_button, self.reset_button, self.detail_button, self.roster_detail_button):
            widget.configure(state='normal' if self.plan and not self.busy else 'disabled')

    def invalidate(self):
        self.plan = None
        replace_rows(self.preview_tree, [])
        self.preview_summary.configure(text='内容已变化，请重新预览日期、名单和冲突。')
        self.sync_actions()

    def edited(self, field):
        if self.rendering:
            return
        if field in {'first_date', 'last_date', 'schedule', 'interval_weeks'}:
            self.excluded.clear()
        self.invalidate()
        if field in {'group_name', 'roster_mode'}:
            self.loaded_ids = None
            self.render_members()

    def render_members(self):
        group = self.variables['group_name'].get()
        rows = [row for row in self.students if group == '全部学生' or row['group_name'] == group]
        self.rendering = True
        self.member_tree.selection_remove(*self.member_tree.selection())
        replace_rows(self.member_tree, [(r['student_id'], (r['student_id'], r['name'], r['group_name']), '') for r in rows])
        if self.loaded_ids is not None:
            visible = {r['student_id'] for r in rows}
            self.member_tree.selection_set([sid for sid in self.loaded_ids if sid in visible])
        self.rendering = False
        self.members_changed()

    def members_changed(self, event=None):
        if self.rendering:
            return
        fixed = self.variables['roster_mode'].get() == '固定选课名单'
        selected = list(self.member_tree.selection())
        missing = set(self.loaded_ids or ()) - set(self.member_tree.get_children()) if fixed else set()
        self.member_summary.configure(text=f"当前班级启用 {len(self.member_tree.get_children())} 人 · 固定选择 {len(selected)} 人"
                                      + (f"；模板中 {len(missing)} 人不存在、已停用或已转班，请明确重选名单。" if missing else '')
                                      + ('；Ctrl / Shift 多选。' if fixed else '；生成时取当前启用班级名单。'))
        if fixed and self.plan and sorted(selected) != self.plan['definition']['student_ids']:
            self.invalidate()

    def select_members(self):
        if not self.busy:
            self.loaded_ids = None
            self.member_tree.selection_set(self.member_tree.get_children())
            self.members_changed()

    def definition(self):
        value = {key: self.variables[key].get().strip() for key in ('name', 'course_name', 'group_name', 'opens_time', 'start_time', 'late_time', 'end_time')}
        try:
            value.update(late_minutes=int(self.variables['late_minutes'].get()), interval_weeks=int(self.variables['interval_weeks'].get()))
        except ValueError:
            raise ValueError('迟到宽限和周间隔请填写整数。') from None
        value.update(next_day=self.next_day.get(), opens_previous_day=self.opens_previous_day.get(),
                     late_next_day=self.late_next_day.get(), weekdays=[i for i, day in enumerate(self.days) if day.get()], student_ids=None)
        if self.variables['roster_mode'].get() == '固定选课名单':
            # 不悄悄丢弃已失效的模板成员；老师须先明确重新选择。
            if set(self.loaded_ids or ()) - set(self.member_tree.get_children()):
                raise ValueError('模板中的固定成员已停用、转班或不存在，请点击“全选当前班级”或切换名单方式后重新选择。')
            value['student_ids'] = sorted(self.member_tree.selection())
        return value

    def request(self):
        return (self.definition(), self.variables['first_date'].get().strip(), self.variables['last_date'].get().strip()), {
            'excluded_dates': sorted(self.excluded)}

    @ui_flow
    def reload(self):
        if self.variables['roster_mode'].get() == '固定选课名单' and self.loaded_ids is None:
            self.loaded_ids = list(self.member_tree.selection())
        self.busy = True
        self.sync_actions()
        try:
            self.templates = yield IO(self.store.course_templates)
            self.students = yield IO(partial(self.store.students, active_only=True))
            groups = ['全部学生'] + sorted({r['group_name'] for r in self.students})
            if self.variables['group_name'].get() not in groups:
                groups.append(self.variables['group_name'].get())
            self.group_combo.configure(values=groups)
            self.template_choices = {f"{r['definition']['name']} · {r['id']}": r for r in self.templates}
            self.template_combo.configure(values=['新模板', *self.template_choices])
            self.render_members()
        finally:
            self.busy = False
            self.sync_actions()

    def new_template(self):
        if self.busy:
            return
        self.current = None
        self.variables['template_choice'].set('新模板')
        self.variables['name'].set('')
        self.say('当前课程可另存为新模板，请填写不同的模板名称。')

    def load_selected(self, event=None):
        if self.busy:
            return
        item = self.template_choices.get(self.variables['template_choice'].get())
        if not item:
            self.new_template()
            return
        self.rendering = True
        self.current = item
        definition = item['definition']
        for key in ('name', 'course_name', 'group_name', 'start_time', 'end_time', 'late_minutes', 'interval_weeks'):
            self.variables[key].set(str(definition[key]))
        self.next_day.set(definition['next_day'])
        times = course_times(definition, date(2000, 1, 2))
        self.variables['opens_time'].set(times['opens_at'].strftime('%H:%M'))
        self.variables['late_time'].set(times['late_at'].strftime('%H:%M'))
        self.opens_previous_day.set(times['opens_at'].date() < times['starts_at'].date())
        self.late_next_day.set(times['late_at'].date() > times['starts_at'].date())
        for i, variable in enumerate(self.days):
            variable.set(i in definition['weekdays'])
        self.variables['roster_mode'].set('固定选课名单' if definition['student_ids'] is not None else '按启用班级')
        self.loaded_ids = definition['student_ids']
        self.rendering = False
        self.excluded.clear()
        self.invalidate()
        self.render_members()
        self.say('已加载模板；请核对本次日期范围与名单，保存模板只影响之后的生成操作。')

    @ui_flow
    def save_template(self):
        if self.busy:
            return
        definition, actor = self.definition(), self.variables['actor'].get().strip()
        current = self.current
        if current and not messagebox.askyesno('更新课程模板', '更新此模板的课程、星期与名单设置；已经生成的课次保持原样。', parent=self.root):
            return
        self.busy = True
        self.sync_actions()
        try:
            item = yield IO(partial(self.store.save_course_template, definition, template_id=current['id'] if current else None,
                                   revision=current['revision'] if current else None, actor=actor))
            self.current = item
        finally:
            self.busy = False
            self.sync_actions()
        # 保存期间的编辑保留在表单中，后续更新仍使用最新修订号。
        yield from self.reload()
        self.variables['template_choice'].set(f"{item['definition']['name']} · {item['id']}")
        self.say('课程模板已保存，已有课次与名单快照保持原样。')

    @ui_flow
    def preview(self):
        if self.busy:
            return
        self.invalidate()
        args, options = self.request()
        self.busy = True
        self.sync_actions()
        try:
            self.say('正在计算日期、应到名单与时段冲突…')
            plan = yield IO(partial(self.store.preview_course_schedule, *args, **options))
            if (args, options) != self.request():
                self.say('预览期间表单已变化，请重新预览。')
                return
            self.plan = plan
            replace_rows(self.preview_tree, [(r['date'], (r['date'] + ' ' + WEEKDAY_TEXT[r['weekday']],
                                                        r['opens_at'].replace('T', ' ')[:16], r['starts_at'].replace('T', ' ')[:16],
                                                        r['late_at'].replace('T', ' ')[:16], r['ends_at'].replace('T', ' ')[:16],
                                                        '已跳过' if not r['included'] else '有冲突' if r['conflicts'] else '待生成',
                                                        '；'.join(f"#{c['id']} {c['course_name']}" for c in r['conflicts']) or '无'),
                                             'muted' if not r['included'] else 'warning' if r['conflicts'] else 'good') for r in plan['rows']])
            self.preview_summary.configure(text=f"计划 {plan['total']} 次 · 将生成 {plan['included']} 次 · 跳过 {plan['total']-plan['included']} 次 · "
                                                f"待处理冲突 {plan['conflict_count']} 次 · 每次应到 {len(plan['members'])} 人\n名单："
                                                + '、'.join(r['student_id'] + ' ' + r['name'] for r in plan['members'][:8])
                                                + ('等；点击“名单快照”核对完整生成名单。' if len(plan['members']) > 8 else ''))
            self.tabs.select(self.preview_page)
            self.say('请核对日期与名单；可跳过停课日或冲突日期，重新预览后确认生成。' if plan['conflict_count'] else '预览完成，可选择停课日期跳过；核对无误后确认生成。')
        finally:
            self.busy = False
            self.sync_actions()

    def skip_selected(self):
        if self.busy or not self.plan:
            return
        self.excluded.update(self.preview_tree.selection())
        self.preview()

    def skip_conflicts(self):
        if self.busy or not self.plan:
            return
        self.excluded.update(r['date'] for r in self.plan['rows'] if r['conflicts'])
        self.preview()

    def reset_exclusions(self):
        if not self.busy:
            self.excluded.clear()
            self.preview()

    def show_date_detail(self):
        selected = self.preview_tree.selection()
        row = next((r for r in self.plan['rows'] if selected and r['date'] == selected[0]), None) if self.plan else None
        if not row:
            self.say('请先选择一个预览日期。')
            return
        lines = [f"{row['date']} {WEEKDAY_TEXT[row['weekday']]}", session_time_text(row),
                 '处理：' + ('保留生成' if row['included'] else '跳过'), '\n冲突课次（时段采用左闭右开）：']
        lines += [f"#{c['id']} {c['course_name']} / {c['group_name']}\n{session_time_text(c)}" for c in row['conflicts']]
        messagebox.showinfo('日期详情', '\n'.join(lines) + ('\n无' if not row['conflicts'] else ''), parent=self.root)

    def show_roster(self):
        if self.plan:
            self.owner.text_details('周期课次应到名单快照',
                                    f"每个课次应到 {len(self.plan['members'])} 人\n\n" + '\n'.join(
                                        f"{row['student_id']}  {row['name']}  {row['group_name']}" for row in self.plan['members']))

    @ui_flow
    def create(self):
        if self.busy or not self.plan or not self.plan['included'] or self.plan['conflict_count']:
            return
        args, options = self.request()
        actor, token = self.variables['actor'].get().strip(), self.plan['token']
        if not messagebox.askyesno('确认生成周期课次', f"将创建 {self.plan['included']} 个待开启课次，每次应到 {len(self.plan['members'])} 人。\n"
                                 '每次分别保存当前名单快照；生成后由老师逐次开启。', parent=self.root):
            return
        self.busy = True
        self.invalidate()
        try:
            ids = yield IO(partial(self.store.create_course_schedule, *args, token, **options, actor=actor))
            self.owner.refresh_sessions()
            self.say(f'已生成 {len(ids)} 个待开启课次，请返回课次管理核对并逐次开课。')
            self.preview_summary.configure(text=f'已生成 {len(ids)} 个课次；再次生成同一时段将提示冲突。')
        finally:
            self.busy = False
            self.sync_actions()

    def close(self):
        if self.busy:
            self.say('正在处理数据，请等待完成后返回。')
            return
        self.closed = True
        self.cancel_flows()
        for identifier in self.after_ids:
            try:
                self.root.after_cancel(identifier)
            except tk.TclError:
                pass
        self.root.destroy()
