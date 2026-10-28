"""教师今日首页和课后核对窗口，所有数据库读取通过后台流程完成。"""

import tkinter as tk
from functools import partial
from tkinter import messagebox, ttk

from app_errors import log_error, user_message
from attendance_store import STATUS_TEXT
from task_runtime import FlowHost, IO, ui_flow
from ui_common import MUTED, WHITE, button, entry_field, fit_window, label, replace_rows, tree

STATE_TEXT = {'draft': '待开启', 'open': '签到中', 'closed': '已结束', 'cancelled': '已取消'}


class TeacherDashboard(tk.Frame, FlowHost):
    def __init__(self, owner, parent):
        tk.Frame.__init__(self, parent, bg=WHITE)
        self.owner, self.store = owner, owner.store
        self.init_flows(owner.root, owner.async_io)
        self.snapshot, self.loading, self.failed = None, False, False
        self.message = tk.StringVar(value='正在读取今日课次…')
        self.stats = tk.StringVar(value='今日课次 0 · 已到 0 / 应到 0 · 当前未签到 0 · 待核对课次 0')
        self.face_text = tk.StringVar(value='人脸资料状态读取中…')
        self.backup_text = tk.StringVar(value='最近成功备份：尚无记录')
        self.health_text = tk.StringVar(value='人脸与备份状态读取中…')
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        summary = label(self, textvariable=self.stats, size=12, bold=True, justify='left')
        summary.grid(row=0, column=0, sticky='ew', pady=(0, 4))
        summary.bind('<Configure>', lambda e: summary.configure(wraplength=max(1, e.width)))
        state = label(self, textvariable=self.health_text, size=9, color=MUTED, justify='left', cursor='hand2')
        state.grid(row=1, column=0, sticky='ew', pady=(0, 6))
        state.bind('<Configure>', lambda e: state.configure(wraplength=max(1, e.width)))
        state.bind('<Button-1>', lambda e: self.owner.text_details('人脸模型与备份状态', self.face_text.get()+'\n\n'+self.backup_text.get()
                                                                  +'\n\n'+self.automatic_status()
                                                                  +'\n备份时间来自本地记录，不代表备份文件仍存在；可在数据与设置中校验恢复。'))
        self.tabs = ttk.Notebook(self)
        self.tabs.grid(row=2, column=0, sticky='nsew')
        self.today_page, self.missing_page, self.review_page = [tk.Frame(self.tabs, bg=WHITE) for _ in range(3)]
        for page, title in ((self.today_page, '今日课次'), (self.missing_page, '当前未签到'), (self.review_page, '待核对课次')):
            self.tabs.add(page, text=title)
        self.current_label = label(self.missing_page, '当前没有开放课次', size=9, color=MUTED, justify='left')
        self.current_label.pack(fill='x', pady=(4, 6))
        self.current_label.bind('<Configure>', lambda e: self.current_label.configure(wraplength=max(1, e.width)))
        frame, self.today_tree = tree(self.today_page, [('course', '课程 / 班级', 200), ('start', '正式上课', 150), ('state', '状态', 80),
                                                     ('arrived', '已到 / 应到', 95), ('review', '核对进度', 150)], empty_text='今天暂无课次\n可到课次管理创建或批量排课')
        frame.pack(fill='both', expand=True)
        frame, self.missing_tree = tree(self.missing_page, [('id', '学号', 130), ('name', '姓名', 120), ('group', '班级', 150),
                                                         ('course', '当前课次', 220)], empty_text='当前没有未签到学生\n未开放课次时，请先到课次管理开课')
        frame.pack(fill='both', expand=True)
        frame, self.review_tree = tree(self.review_page, [('course', '课程 / 班级', 200), ('start', '正式上课', 155),
                                                       ('arrived', '已到 / 应到', 95), ('absent', '缺勤', 65), ('review', '核对进度', 150)],
                                       empty_text='暂无待核对的已结束课次')
        frame.pack(fill='both', expand=True)
        for widget in (self.today_tree, self.missing_tree, self.review_tree):
            widget.configure(selectmode='browse')
            widget.bind('<<TreeviewSelect>>', self.sync_actions)
        self.tabs.bind('<<NotebookTabChanged>>', self.sync_actions)
        actions = tk.Frame(self, bg=WHITE)
        actions.grid(row=3, column=0, sticky='ew', pady=(8, 0))
        self.manage_button = button(actions, '课次管理', self.manage)
        self.manage_button.pack(side='left')
        self.manual_button = button(actions, '人工点名', self.manual)
        self.manual_button.pack(side='left', padx=8)
        self.records_button = button(actions, '查看考勤', self.records, plain=True)
        self.records_button.pack(side='left')
        self.review_button = button(actions, '课后核对', self.review, primary=True)
        self.review_button.pack(side='right')
        self.refresh_button = button(actions, '刷新', self.refresh, plain=True)
        self.refresh_button.pack(side='right')
        button(actions, '备份', owner.backup, plain=True).pack(side='right')
        button(actions, '设置', lambda: owner.notebook.select(owner.pages['settings']), plain=True).pack(side='right')
        feedback = label(self, textvariable=self.message, size=9, color=MUTED, justify='left')
        feedback.grid(row=4, column=0, sticky='ew', pady=(6, 0))
        feedback.bind('<Configure>', lambda e: feedback.configure(wraplength=max(1, e.width)))
        self.sync_actions()

    def schedule(self, milliseconds, callback):
        return self.owner.schedule(milliseconds, callback) if not self.closed else None

    def say(self, message):
        self.message.set(message)

    def handle_error(self, error):
        log_error('teacher_dashboard_failed', error)
        self.failed = True
        self.say('首页数据暂不可用，请刷新后再操作。' + user_message(error))
        self.sync_actions()

    def selected(self):
        if not self.snapshot or self.failed:
            return None
        page = self.tabs.select()
        if page == str(self.missing_page):
            return self.snapshot['active']
        widget = self.review_tree if page == str(self.review_page) else self.today_tree
        selection = widget.selection()
        candidates = self.snapshot['pending_reviews'] if page == str(self.review_page) else self.snapshot['today']
        return next((row for row in candidates if selection and str(row['id']) == selection[0]), None)

    def sync_actions(self, event=None):
        row = self.selected()
        enabled = bool(row and not self.loading and not self.failed)
        self.manage_button.configure(state='normal' if enabled else 'disabled')
        self.manual_button.configure(state='normal' if enabled and row['state'] in {'draft', 'open', 'closed'} else 'disabled')
        self.records_button.configure(state='normal' if enabled and row['state'] in {'open', 'closed'} else 'disabled')
        self.review_button.configure(state='normal' if enabled and row['state'] == 'closed' else 'disabled')
        self.refresh_button.configure(state='disabled' if self.loading else 'normal')

    def render_face_status(self):
        health = getattr(self.owner, 'face_summary', None)
        if health is None:
            self.face_text.set('人脸资料状态读取中…')
        elif not health['healthy']:
            self.face_text.set(f"人脸模型未就绪 · {health['active']} 位启用学生可通过人工点名登记。{health['message']}")
        else:
            self.face_text.set(f"人脸资料：启用 {health['active']} 人 · 模型覆盖 {health['covered']} 人 · 尚无人脸模型 {health['missing']} 人")
        face = ('人脸状态读取中' if health is None else '人脸模型未就绪，可人工点名' if not health['healthy'] else
                f"人脸模型覆盖 {health['covered']}/{health['active']} 人，缺 {health['missing']} 人")
        backup = self.snapshot['last_backup'] if self.snapshot else None
        backup_text = ('备份记录异常' if backup and backup.get('invalid') else '最近备份 '+backup['completed_at'].replace('T', ' ') if backup else '备份尚无记录')
        automatic = getattr(self.owner, 'automatic_backup', None)
        snapshot = automatic.snapshot if automatic else None
        state = snapshot['state'] if snapshot else {}
        warning = ' · 自动备份需处理' if state.get('status') in {'error', 'warning'} or state.get('invalid') or (snapshot and snapshot['settings'].get('invalid')) else ''
        self.health_text.set(face+' · '+backup_text+warning+'（点击详情）')

    def automatic_status(self):
        automatic = getattr(self.owner, 'automatic_backup', None)
        return automatic.status.get() if automatic else '自动备份状态读取中'

    @ui_flow
    def refresh(self):
        if self.loading or self.closed:
            return
        self.loading = True
        self.sync_actions()
        try:
            data = yield IO(self.store.teacher_dashboard)
            self.snapshot, self.failed = data, False
            self.stats.set(f"今日课次 {len(data['today'])} · 已到 {data['totals']['arrived']} / 应到 {data['totals']['expected']} 人次"
                           f" · 当前未签到 {len(data['missing'])} 人 · 待核对课次 {data['pending_review_count']}")
            current = data['active']
            self.tabs.tab(self.missing_page, text='当前未签到' + (' · '+current['course_name'][:10] if current else ''))
            self.current_label.configure(text=f"当前 #{current['id']} {current['course_name']} / {current['group_name']} · 已到 {current['arrived']} / 应到 {current['expected']} · "
                                              f"迟到 {current['late']} · 请假 {current['leave']} · 未签到 {current['pending']}" if current else '当前没有开放课次，可在课次管理开启签到或人工点名。')
            replace_rows(self.today_tree, [(r['id'], (f"{r['course_name']} / {r['group_name']}", r['starts_at'].replace('T', ' ')[:16], STATE_TEXT[r['state']],
                                                     f"{r['arrived']}/{r['expected']}" if r['state'] in {'open', 'closed'} else '—', r['review_text']),
                                            'good' if r['state'] == 'open' else '') for r in data['today']])
            if not self.today_tree.selection() and data['today']:
                self.today_tree.selection_set(str((current or data['today'][0])['id']) if current and self.today_tree.exists(str(current['id'])) else str(data['today'][0]['id']))
            replace_rows(self.missing_tree, [(r['student_id'], (r['student_id'], r['name'], r['group_name'], current['course_name']), '') for r in data['missing']])
            replace_rows(self.review_tree, [(r['id'], (f"{r['course_name']} / {r['group_name']}", r['starts_at'].replace('T', ' ')[:16], f"{r['arrived']}/{r['expected']}", r['absent'], r['review_text']), '')
                                            for r in data['pending_reviews']])
            if not self.review_tree.selection() and data['pending_reviews']:
                self.review_tree.selection_set(str(data['pending_reviews'][0]['id']))
            backup = data['last_backup']
            self.backup_text.set('最近成功备份：' + ('状态记录不可读，请重新备份' if backup and backup.get('invalid') else
                                                  backup['completed_at'].replace('T', ' ') if backup else '尚无记录') + '（本地记录）')
            self.render_face_status()
            self.say(f"{data['date']} · 今日按正式上课日期统计 · 待核对列表显示最近 {data['review_limit']} 个")
        finally:
            self.loading = False
            self.sync_actions()

    def manage(self):
        row = self.selected()
        if row:
            self.owner.show_dashboard_session(row['id'])

    def manual(self):
        row = self.selected()
        if row:
            self.owner.manual_roll_call(session_id=row['id'])

    def records(self):
        row = self.selected()
        if row:
            selected = self.missing_tree.selection() if self.tabs.select() == str(self.missing_page) else ()
            self.owner.show_dashboard_records(row['id'], selected[0] if selected else None)

    def review(self):
        row = self.selected()
        if row:
            self.owner.show_review(row['id'])

    def close(self):
        self.closed = True
        self.cancel_flows()


class SessionReviewDialog(FlowHost):
    def __init__(self, owner, session_id):
        self.owner, self.store, self.session_id = owner, owner.store, session_id
        window = tk.Toplevel(owner.root)
        window.title('课后核对')
        fit_window(window, '980x720', minimum=(740, 500))
        window.configure(bg=WHITE)
        window.transient(owner.root)
        window.grab_set()
        self.init_flows(window, owner.async_io)
        self.plan, self.busy, self.saved = None, False, False
        self.after_ids = []
        self.message = tk.StringVar(value='正在读取核对明细…')
        self.confirmed = tk.BooleanVar()
        self.actor, self.note = tk.StringVar(), tk.StringVar()
        window.protocol('WM_DELETE_WINDOW', self.close)
        actions = tk.Frame(window, bg=WHITE)
        actions.pack(side='bottom', fill='x', padx=20, pady=12)
        self.close_button = button(actions, '返回', self.close, plain=True)
        self.close_button.pack(side='left')
        self.reload_button = button(actions, '刷新明细', self.reload, plain=True)
        self.reload_button.pack(side='left', padx=8)
        self.save_button = button(actions, '确认核对完成', self.save, primary=True)
        self.save_button.pack(side='right')
        status = label(window, textvariable=self.message, size=9, color=MUTED, justify='left')
        status.pack(side='bottom', fill='x', padx=20, pady=(4, 0))
        status.bind('<Configure>', lambda e: status.configure(wraplength=max(1, e.width)))
        self.header = label(window, '课后核对', size=13, bold=True, justify='left')
        self.header.pack(fill='x', padx=20, pady=(16, 8))
        self.header.bind('<Configure>', lambda e: self.header.configure(wraplength=max(1, e.width)))
        self.tabs = ttk.Notebook(window)
        self.tabs.pack(fill='both', expand=True, padx=20)
        details, completion = tk.Frame(self.tabs, bg=WHITE), tk.Frame(self.tabs, bg=WHITE)
        self.tabs.add(details, text='1. 考勤明细')
        self.tabs.add(completion, text='2. 确认核对')
        frame, self.rows_tree = tree(details, [('id', '学号', 110), ('name', '姓名', 90), ('status', '当前状态', 90), ('arrival', '实际到课时间', 175),
                                             ('original', '首次人脸时间', 175), ('note', '备注', 160)], empty_text='此课次没有名单成员')
        frame.pack(fill='both', expand=True, pady=8)
        self.completion_page = completion
        form = tk.Frame(completion, bg=WHITE, padx=12, pady=8)
        form.pack(fill='both', expand=True)
        self.confirmation_form = form
        form.columnconfigure(0, weight=1)
        self.previous = label(form, '', color=MUTED, size=9, justify='left')
        self.previous.grid(row=0, column=0, sticky='ew', pady=12)
        self.previous.bind('<Configure>', lambda e: self.previous.configure(wraplength=max(1, e.width)))
        self.previous.configure(cursor='hand2')
        self.previous.bind('<Button-1>', self.previous_details)
        entry_field(form, '核对说明（选填，不超过 300 字）', self.note, 1)
        entry_field(form, '操作署名（选填，未经账号认证）', self.actor, 3)
        self.confirm_box = ttk.Checkbutton(form, text='我已核对考勤状态、到课时间和未到人员，确认完成本次核对', variable=self.confirmed, command=self.sync_actions)
        self.confirm_box.grid(row=5, column=0, sticky='w', pady=20)
        tip = label(form, '确认只记录核对进度，不改变任何考勤结果。之后若有更正、名单调整或原始人脸证据变化，需要重新核对。\n缺勤、请假和时间未确认仍按原有规则统计；核对标记不代表这些结果已被修改。', size=9, color=MUTED, justify='left')
        tip.grid(row=6, column=0, sticky='ew', pady=(0, 20))
        tip.bind('<Configure>', lambda e: tip.configure(wraplength=max(1, e.width)))
        self.sync_actions()
        self.reload()

    def previous_details(self, event=None):
        previous = self.plan['previous'] if self.plan else None
        if previous and not previous.get('invalid'):
            self.owner.text_details('上次核对记录', f"核对时间：{previous['reviewed_at'].replace('T', ' ')}\n"
                                    f"操作署名：{previous['actor'] or '管理入口未署名'}（未经认证）\n说明：{previous['note'] or '无'}")

    def schedule(self, milliseconds, callback):
        if not self.closed:
            self.after_ids.append(self.root.after(milliseconds, callback))

    def say(self, message):
        self.message.set(message)

    def handle_error(self, error):
        log_error('session_review_failed', error)
        self.say(user_message(error))
        messagebox.showerror('课后核对未完成', user_message(error), parent=self.root)

    def sync_actions(self):
        for widget in (self.close_button, self.reload_button):
            widget.configure(state='disabled' if self.busy else 'normal')
        self.save_button.configure(state='normal' if self.plan and self.confirmed.get() and not self.busy and not self.saved else 'disabled')

    @ui_flow
    def reload(self):
        if self.busy:
            return
        self.busy, self.plan, self.saved = True, None, False
        self.confirmed.set(False)
        self.sync_actions()
        try:
            self.plan = yield IO(partial(self.store.preview_session_review, self.session_id))
            plan = self.plan
            counts, session = plan['counts'], plan['session']
            self.header.configure(text=f"#{self.session_id} {session['course_name']} / {session['group_name']} · 已结束\n"
                                       f"应到 {counts['expected']} · 已到 {counts['arrived']} · 迟到 {counts['late']} · 请假 {counts['leave']} · 缺勤 {counts['absent']} · 时间未确认 {counts['unknown_time']}")
            replace_rows(self.rows_tree, [(r['student_id'], (r['student_id'], r['name'], STATUS_TEXT[r['status']],
                                                          (r['signed_at'] or ('时间未确认' if r['status'] in {'present', 'late'} else '—')).replace('T', ' '),
                                                          (r['original_signed_at'] or '—').replace('T', ' '), r['note']), 'muted' if not r['included'] else '') for r in plan['rows']])
            previous = plan['previous']
            if previous and not previous.get('invalid'):
                self.previous.configure(text=('当前结果已核对。' if plan['reviewed'] else '上次核对后有变更，需重新确认。')
                                        + f"\n上次核对：{previous['reviewed_at'].replace('T', ' ')}（点击查看署名与说明）")
            else:
                self.previous.configure(text='旧核对标记不可读，请核对后重新确认。' if previous else '此课次尚未核对，请先查看考勤明细。')
            self.say('请查看考勤明细，再到“确认核对”填写说明并勾选确认。')
        finally:
            self.busy = False
            self.sync_actions()

    @ui_flow
    def save(self):
        if self.busy or not self.plan or not self.confirmed.get() or self.saved:
            return
        token, actor, note = self.plan['token'], self.actor.get().strip(), self.note.get().strip()
        self.busy = True
        self.sync_actions()
        try:
            yield IO(partial(self.store.mark_session_reviewed, self.session_id, token, actor=actor, note=note))
            self.saved = True
            self.say('本次核对已完成；之后有考勤或名单变化会重新进入待核对。')
            self.owner.refresh_sessions()
        except Exception:
            self.plan = None
            self.confirmed.set(False)
            raise
        finally:
            self.busy = False
            self.sync_actions()

    def close(self):
        if self.busy:
            self.say('正在处理数据，请等待完成后返回。')
            return
        self.closed = True
        self.cancel_flows()
        for token in self.after_ids:
            try:
                self.root.after_cancel(token)
            except tk.TclError:
                pass
        self.root.destroy()
