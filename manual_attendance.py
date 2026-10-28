"""教师人工点名：选择名单、登记预览、事务提交，不依赖相机或模型。"""

import tkinter as tk
from datetime import datetime
from functools import partial
from tkinter import messagebox, ttk

from app_errors import log_error, user_message
from attendance_fields import ARRIVED_STATUSES, arrival_time_note
from attendance_store import STATUS_TEXT
from task_runtime import FlowHost, IO, ui_flow
from ui_common import MUTED, WHITE, ScrollableFrame, button, entry_field, fit_window, label, replace_rows, tree


def arrival_text(row):
    return (row['signed_at'] or (arrival_time_note(row) or '—')).replace('T', ' ')


class ManualAttendanceDialog(FlowHost):
    def __init__(self, owner, session_id):
        self.owner, self.store, self.session_id = owner, owner.store, session_id
        window = tk.Toplevel(owner.root)
        window.title('人工点名')
        fit_window(window, '980x720', minimum=(740, 500))
        window.configure(bg=WHITE)
        window.transient(owner.root)
        window.grab_set()
        self.init_flows(window, owner.async_io)
        self.busy, self.plan, self.rows, self.session = False, None, [], None
        self.after_ids = []
        window.protocol('WM_DELETE_WINDOW', self.close)
        self.search = tk.StringVar()
        self.filter = tk.StringVar(value='全部')
        self.choice = tk.StringVar(value='已签到')
        self.time_mode = tk.StringVar(value='保留各自原时间')
        self.actual, self.actor = tk.StringVar(), tk.StringVar()
        self.reason = tk.StringVar()
        self.message = tk.StringVar(value='选择学生后，在“登记与预览”填写状态与原因。')
        self.selection_message = tk.StringVar(value='尚未选择学生')
        self.header = label(window, '正在读取课次…', size=13, bold=True, justify='left')
        self.header.pack(fill='x', padx=20, pady=(16, 8))
        self.header.bind('<Configure>', lambda e: self.header.configure(wraplength=max(1, e.width)))
        actions = tk.Frame(window, bg=WHITE)
        actions.pack(side='bottom', fill='x', padx=20, pady=12)
        self.close_button = button(actions, '返回', self.close, plain=True)
        self.close_button.pack(side='left')
        self.reload_button = button(actions, '刷新名单', self.reload, plain=True)
        self.reload_button.pack(side='left', padx=8)
        self.save_button = button(actions, '确认登记', self.save, primary=True)
        self.save_button.pack(side='right')
        self.preview_button = button(actions, '预览登记', self.preview)
        self.preview_button.pack(side='right', padx=8)
        self.open_button = button(actions, '开启人工点名', self.open_manual)
        self.open_button.pack(side='right')
        self.feedback = label(window, textvariable=self.message, size=9, color=MUTED, justify='left')
        self.feedback.pack(side='bottom', fill='x', padx=20, pady=(4, 0))
        self.feedback.bind('<Configure>', lambda e: self.feedback.configure(wraplength=max(1, e.width)))
        label(window, textvariable=self.selection_message, size=10, color=MUTED).pack(side='bottom', anchor='w', padx=20)
        self.tabs = ttk.Notebook(window)
        self.tabs.pack(fill='both', expand=True, padx=20)
        roster = tk.Frame(self.tabs, bg=WHITE, pady=10)
        self.tabs.add(roster, text='1. 选择学生')
        self.form_page = tk.Frame(self.tabs, bg=WHITE)
        self.tabs.add(self.form_page, text='2. 登记与预览')
        filters = tk.Frame(roster, bg=WHITE)
        filters.pack(fill='x', pady=(0, 10))
        label(filters, '搜索学号 / 姓名 / 班级', size=9, color=MUTED).pack(side='left')
        ttk.Entry(filters, textvariable=self.search, width=20).pack(side='left', padx=8)
        ttk.Combobox(filters, textvariable=self.filter, values=['全部', '待处理（未签到 / 缺勤）', '已到', '请假'], width=23, state='readonly').pack(side='left')
        self.select_button = button(filters, '全选当前结果', self.select_visible, plain=True)
        self.select_button.pack(side='right')
        self.roster_hint = label(roster, '按住 Ctrl 可多选，Shift 可连续选；全选只选当前搜索结果。切换筛选会清空选择。', size=9, color=MUTED, justify='left')
        self.roster_hint.pack(side='bottom', fill='x', pady=(8, 0))
        self.roster_hint.bind('<Configure>', lambda e: self.roster_hint.configure(wraplength=max(1, e.width)))
        frame, self.roster_tree = tree(roster, [('id', '学号', 110), ('name', '姓名', 90), ('group', '班级', 100),
                                               ('status', '当前状态', 90), ('time', '实际到课时间', 170), ('method', '方式', 65)],
                                       empty_text='当前条件下没有应到学生')
        frame.pack(fill='both', expand=True)
        self.roster_tree.configure(selectmode='extended')
        self.roster_tree.bind('<<TreeviewSelect>>', self.selection_changed)
        scroll = ScrollableFrame(self.form_page)
        scroll.pack(fill='both', expand=True)
        self.form_scroll = scroll
        form = scroll.body
        form.columnconfigure(0, weight=1)
        label(form, '登记状态', size=9, color=MUTED).grid(row=0, column=0, sticky='w', pady=(12, 5))
        ttk.Combobox(form, textvariable=self.choice, values=['已签到', '迟到', '请假', '缺勤'], state='readonly').grid(row=1, column=0, sticky='ew')
        label(form, '实际到课时间处理', size=9, color=MUTED).grid(row=2, column=0, sticky='w', pady=(12, 5))
        ttk.Combobox(form, textvariable=self.time_mode, values=['保留各自原时间', '统一实际到课时间', '时间未确认'], state='readonly').grid(row=3, column=0, sticky='ew')
        self.actual_entry = entry_field(form, '统一实际到课时间（年-月-日 时:分[:秒]）', self.actual, 4)
        hint = label(form, '默认保留各自已有时间；新补签须填写统一时间或明确选“时间未确认”。不会自动填当前时间。\n请假、缺勤默认也保留已有到课时间；原始人脸证据始终保留。', size=9, color=MUTED, justify='left')
        hint.grid(row=6, column=0, sticky='ew', pady=(10, 0))
        hint.bind('<Configure>', lambda e: hint.configure(wraplength=max(1, e.width)))
        entry_field(form, '登记原因 / 人工开课原因（必填，1–300 字）', self.reason, 7)
        entry_field(form, '操作署名（选填，未经账号认证）', self.actor, 9)
        label(form, '预览结果：逐人核对状态、时间和覆盖范围后，再确认登记。', size=10, bold=True).grid(row=11, column=0, sticky='w', pady=(18, 10))
        frame, self.preview_tree = tree(form, [('id', '学号', 110), ('name', '姓名', 90), ('before', '原状态', 80),
                                              ('after', '登记状态', 90), ('oldtime', '原到课时间', 170),
                                              ('time', '登记到课时间', 170), ('result', '处理', 90)], height=8,
                                       empty_text='填写完成后点击底部“预览登记”')
        frame.grid(row=12, column=0, sticky='ew', pady=(0, 16))
        for variable in (self.choice, self.time_mode, self.actual, self.reason, self.actor):
            variable.trace_add('write', self.edited)
        self.search.trace_add('write', self.filter_changed)
        self.filter.trace_add('write', self.filter_changed)
        self.sync_actions()
        self.reload()

    def schedule(self, milliseconds, callback):
        if not self.closed:
            self.after_ids.append(self.root.after(milliseconds, callback))

    def say(self, message):
        self.message.set(message)

    def handle_error(self, error):
        log_error('manual_attendance_failed', error)
        self.say(user_message(error))
        messagebox.showerror('人工点名未完成', user_message(error), parent=self.root)

    def sync_actions(self):
        state = self.session['state'] if self.session else None
        for control in (self.close_button, self.reload_button, self.select_button):
            control.configure(state='disabled' if self.busy else 'normal')
        self.open_button.configure(state='normal' if state == 'draft' and not self.busy else 'disabled')
        self.preview_button.configure(state='normal' if state in {'open', 'closed'} and not self.busy else 'disabled')
        self.save_button.configure(state='normal' if self.plan and self.plan['changed'] and not self.busy else 'disabled')
        self.actual_entry.configure(state='normal' if self.time_mode.get() == '统一实际到课时间' else 'disabled')

    def invalidate(self):
        self.plan = None
        replace_rows(self.preview_tree, [])
        self.sync_actions()

    def edited(self, *_):
        had_plan = self.plan is not None
        self.invalidate()
        if had_plan:
            self.say('登记内容已改变，请重新预览。')

    def selection_changed(self, event=None):
        selected = self.roster_tree.selection()
        self.selection_message.set(f'当前显示 {len(self.roster_tree.get_children())} 人 · 已选 {len(selected)} 人')
        if self.plan and sorted(selected) != [row['student_id'] for row in self.plan['rows']]:
            self.invalidate()
            self.say('所选学生已改变，请重新预览。')

    def filter_changed(self, *_):
        self.invalidate()
        self.render_roster()

    def render_roster(self):
        keyword, category = self.search.get().strip().casefold(), self.filter.get()
        rows = [r for r in self.rows if (not keyword or any(keyword in r[k].casefold() for k in ('student_id', 'name', 'group_name')))
                and (category == '全部' or category == '待处理（未签到 / 缺勤）' and r['status'] in {'pending', 'absent'}
                     or category == '已到' and r['status'] in ARRIVED_STATUSES or category == '请假' and r['status'] == 'leave')]
        self.roster_tree.selection_remove(*self.roster_tree.selection())
        replace_rows(self.roster_tree, [(r['student_id'], (r['student_id'], r['name'], r['group_name'], STATUS_TEXT[r['status']], arrival_text(r),
                                                        {'face': '人脸', 'manual': '人工', 'legacy': '迁移'}.get(r['method'], '')), '') for r in rows])
        self.selection_changed()

    def select_visible(self):
        if not self.busy:
            self.roster_tree.selection_set(self.roster_tree.get_children())
            self.selection_changed()

    @ui_flow
    def reload(self):
        if self.busy:
            return
        self.busy = True
        self.invalidate()
        try:
            self.rows = yield IO(partial(self.store.session_rows, self.session_id))
            self.session = yield IO(partial(self.store.session, self.session_id))
            state = {'draft': '待开启', 'open': '签到中', 'closed': '已结束', 'cancelled': '已取消'}[self.session['state']]
            self.header.configure(text=f"#{self.session_id}  {self.session['course_name']} / {self.session['group_name']} · {state}\n"
                                       f"开放 {self.session['opens_at'].replace('T', ' ')} — 关闭 {self.session['ends_at'].replace('T', ' ')}\n"
                                       f"上课 {self.session['starts_at'].replace('T', ' ')} · 迟到 {self.session['late_at'].replace('T', ' ')}")
            self.render_roster()
            self.say('待开启课次：先填写原因，再点击“开启人工点名”；遵守原签到时段。' if self.session['state'] == 'draft' else
                     '课次可人工登记。选择学生后，到“登记与预览”填写并预览。' if self.session['state'] in {'open', 'closed'} else '已取消课次只能查询。')
        finally:
            self.busy = False
            self.sync_actions()

    def request(self):
        ids = sorted(self.roster_tree.selection())
        options = {'actor': self.actor.get().strip()}
        mode = self.time_mode.get()
        if mode == '统一实际到课时间':
            try:
                options['arrival_time'] = datetime.fromisoformat(self.actual.get().strip())
            except ValueError:
                raise ValueError('实际到课时间请使用 年-月-日 时:分[:秒]。') from None
        elif mode == '时间未确认':
            options['time_unconfirmed'] = True
        statuses = {STATUS_TEXT[k]: k for k in ('present', 'late', 'leave', 'absent')}
        return (self.session_id, ids, statuses[self.choice.get()], self.reason.get().strip()), options

    @ui_flow
    def preview(self):
        if self.busy:
            return
        self.tabs.select(self.form_page)
        self.invalidate()
        self.busy = True
        self.sync_actions()
        try:
            args, options = self.request()
            self.say('正在核对所选人员的考勤与原始证据…')
            plan = yield IO(partial(self.store.preview_manual_attendance, *args, **options))
            if (args, options) != self.request():
                self.say('预览期间登记内容已变化，请重新预览。')
                return
            self.plan = plan
            replace_rows(self.preview_tree, [(r['student_id'], (r['student_id'], r['name'], STATUS_TEXT[r['before']['status']],
                                                              STATUS_TEXT[r['after']['status']], arrival_text(r['before']), arrival_text(r['after']),
                                                              '登记' if r['changed'] else '无需重复'), '') for r in plan['rows']])
            self.say(f"预览 {plan['total']} 人：将登记 {plan['changed']} 人，{plan['total'] - plan['changed']} 人无需重复。请核对下方预览表，再点击确认登记。")
            self.form_scroll.update_idletasks()
            self.form_scroll.canvas.yview_moveto(1)
        finally:
            self.busy = False
            self.sync_actions()

    @ui_flow
    def save(self):
        if self.busy or not self.plan or not self.plan['changed']:
            return
        args, options = self.request()
        token = self.plan['token']
        if not messagebox.askyesno('确认人工点名', f"将为所选 {self.plan['total']} 人登记，其中 {self.plan['changed']} 人写入更正历史。\n"
                                 '请确认预览中的状态及实际到课时间；原始人脸证据与各自备注保留。', parent=self.root):
            return
        self.busy = True
        self.invalidate()
        try:
            result = yield IO(partial(self.store.apply_manual_attendance, *args, token, **options))
            self.owner.say(f"人工点名完成：登记 {result['changed']} 人，未重复写入 {result['unchanged']} 人")
            self.owner.refresh_sessions()
        finally:
            self.busy = False
            self.sync_actions()
        yield from self.reload()
        self.say(f"已登记 {result['changed']} 人，逐人修改原因和历史已保存。可继续选择其他学生。")
        self.tabs.select(0)

    @ui_flow
    def open_manual(self):
        if self.busy or not self.session or self.session['state'] != 'draft':
            return
        self.tabs.select(self.form_page)
        reason, actor = self.reason.get().strip(), self.actor.get().strip()
        if not reason:
            self.say('请先在“登记与预览”填写人工开课原因（1–300 字）。')
            return
        if not messagebox.askyesno('开启人工点名', '按当前课次签到时段开课，未登记人员在结束后计为缺勤。\n'
                                 '此入口无需模型或相机；同一时刻仍只允许一个开放课次。学生端可用时仍可刷脸。', parent=self.root):
            return
        self.busy = True
        self.sync_actions()
        try:
            yield IO(partial(self.store.open_manual_session, self.session_id, reason, actor=actor))
            self.owner.refresh_sessions()
        finally:
            self.busy = False
            self.sync_actions()
        yield from self.reload()
        self.tabs.select(0)

    def close(self):
        if self.busy:
            self.say('正在处理数据，请等待完成后再返回。')
            return
        self.closed = True
        self.cancel_flows()
        for identifier in self.after_ids:
            try:
                self.root.after_cancel(identifier)
            except tk.TclError:
                pass
        self.root.destroy()
