"""教师端人脸授权、期限提醒及需口令确认的清理入口。"""

import tkinter as tk
from functools import partial
from tkinter import messagebox, ttk
from classroom_attendance.core.app_errors import user_message

from classroom_attendance.faces.face_lifecycle import FaceLifecycleService, STATE_TEXT
from classroom_attendance.maintenance.maintenance_tools import MaintenanceService
from classroom_attendance.core.task_runtime import FlowHost, IO, ui_flow
from classroom_attendance.ui.ui_common import MUTED, WHITE, ScrollableFrame, button, fit_window, label, replace_rows, tree


class FaceLifecyclePanel(tk.Frame, FlowHost):
    def __init__(self, owner, parent):
        tk.Frame.__init__(self, parent, bg=WHITE)
        self.init_flows(owner.root, owner.async_io)
        self.owner, self.store = owner, owner.store
        self.service = FaceLifecycleService(self.store)
        self.snapshot, self.dialog = None, None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        heading = tk.Frame(self, bg=WHITE)
        heading.grid(row=0, column=0, sticky='ew')
        label(heading, '人脸授权与保存期限', size=17, bold=True).pack(side='left')
        button(heading, '刷新', self.refresh, plain=True).pack(side='right')
        button(heading, '发布新告知', self.publish_dialog).pack(side='right', padx=8)
        button(heading, '查看当前告知', self.show_notice, plain=True).pack(side='right')
        label(self, '本机只登记教师已核验的授权，不代替本人/监护人的真实同意。未登记、撤回、过期或告知更新后停止刷脸，可继续人工点名。',
              size=9, color=MUTED, wraplength=800, justify='left').grid(row=1, column=0, sticky='ew', pady=8)
        self.summary = tk.StringVar(value='正在读取授权与到期提醒…')
        label(self, textvariable=self.summary, size=10, color=MUTED, wraplength=800, justify='left').grid(row=2, column=0, sticky='ew', pady=(0, 8))
        roster_frame, self.roster = tree(self, [('id', '学号', 130), ('name', '姓名', 110), ('group', '班级', 120),
                                 ('status', '人脸授权状态', 210), ('expiry', '保存截止日（含当天）', 180), ('notice', '告知记录编号', 115)], height=10)
        roster_frame.grid(row=3, column=0, sticky='nsew')
        actions = tk.Frame(self, bg=WHITE)
        actions.grid(row=4, column=0, sticky='ew', pady=(12, 0))
        button(actions, '登记 / 续期授权', self.grant_dialog, primary=True).pack(side='left')
        button(actions, '登记撤回', self.revoke_dialog).pack(side='left', padx=8)
        button(actions, '授权历史', self.show_history, plain=True).pack(side='left')
        button(actions, '预览受控清理', self.cleanup_dialog).pack(side='right')
        button(actions, '续办未完成清理', lambda: self.cleanup_dialog(resume=True), plain=True).pack(side='right', padx=8)
        self.refresh()
        self.schedule(30000, self.tick)

    def schedule(self, delay, callback):
        return self.owner.schedule(delay, callback) if not self.closed else None

    def say(self, message):
        self.owner.say(message)

    def handle_error(self, error):
        if self.dialog and self.dialog.winfo_exists():
            self.dialog.feedback.set(user_message(error))
            return
        self.owner.handle_error(error)

    @ui_flow
    def refresh(self):
        snapshot = yield IO(self.service.snapshot)
        if self.closed:
            return
        self.snapshot = snapshot
        self.owner.privacy_hint.set(f"人脸授权：受限 {snapshot['blocked']} 人 · 14 日内到期 {snapshot['expiring_soon']} 人 · "
                                    + ('有清理待续办，请进入“人脸授权”处理' if snapshot['pending_job'] else '点击查看授权与期限；人工点名不受影响'))
        notice = snapshot['notice']
        self.summary.set(f"当前告知：{notice['version'] if notice else '尚未发布'} · 受限 {snapshot['blocked']} 人 · 14 日内到期 {snapshot['expiring_soon']} 人"
                         + ('\n有清理任务尚未完成：全部人脸使用与新备份暂停，请续办；人工点名可用。' if snapshot['pending_job'] else '\n截止日当天有效，次日零点起停止使用；不会静默自动删除。'))
        replace_rows(self.roster, [(row['student_id'], [row['student_id'], row['name'], row['group_name'],
                      STATE_TEXT[row['state']]+(' · 即将到期' if row['expiring_soon'] else ''), row['expires_on'] or '—', row['notice_id'] or '—'], '')
                     for row in snapshot['students']])

    def tick(self):
        if self.closed:
            return
        if not self.owner.busy:
            self.refresh()
        self.schedule(30000, self.tick)

    def selected_id(self):
        selection = self.roster.selection()
        if not selection:
            raise ValueError('请先在人脸授权列表中选择学生。')
        return selection[0]

    def new_dialog(self, title, geometry='760x620'):
        if self.owner.busy:
            self.say('请先等待当前任务完成。')
            return None
        if self.dialog and self.dialog.winfo_exists():
            self.dialog.lift()
            return None
        dialog = tk.Toplevel(self.root)
        self.dialog = dialog
        dialog.title(title)
        fit_window(dialog, geometry, minimum=(680, 500))
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg=WHITE)
        dialog.submitting = False
        dialog.cancel_requested = False
        dialog.submit_buttons = []
        dialog.feedback = tk.StringVar(master=dialog)
        feedback = tk.Frame(dialog, bg=WHITE)
        feedback.pack(side='bottom', fill='x', padx=20, pady=(0, 12))
        label(feedback, textvariable=dialog.feedback, color=MUTED, wraplength=620,
              justify='left').pack(fill='x')
        dialog.protocol('WM_DELETE_WINDOW', lambda: self.close_dialog(dialog))
        return dialog

    def close_dialog(self, dialog):
        if dialog.submitting:
            dialog.cancel_requested = True
            if self.owner.job_parent is dialog:
                self.owner.cancel_job()
            dialog.feedback.set('已请求停止，正在等待当前步骤结束；结束后可关闭或重试。')
            return
        if dialog.grab_current() is dialog:
            dialog.grab_release()
        dialog.destroy()

    def submitting(self, dialog, busy):
        if dialog.winfo_exists():
            dialog.submitting = busy
            for control in dialog.submit_buttons:
                control.configure(state='disabled' if busy else 'normal')

    @ui_flow
    def show_notice(self):
        snapshot = yield IO(self.service.snapshot)
        notice = snapshot['notice']
        self.owner.text_details('当前人脸告知', f"版本：{notice['version']}\n发布：{notice['created_at']}\nSHA-256：{notice['sha256']}\n\n{notice['text']}" if notice else '尚未发布告知。请由学校核定正文后发布，已有样本不自动视为授权。')

    @ui_flow
    def show_history(self):
        rows = yield IO(partial(self.service.history, self.selected_id()))
        self.owner.text_details('人脸授权 / 撤回历史', '\n\n'.join(
            f"#{row['id']} {row['created_at']} · {'授权' if row['action'] == 'grant' else '撤回'}\n"
            f"告知版本：{row['version'] or '—'}\n保存截止：{row['expires_on'] or '—'}\n方式：{row['basis']}\n凭据引用 / 原因：{row['reference']}"
            for row in rows) or '尚无授权或撤回记录。')

    def publish_dialog(self):
        dialog = self.new_dialog('发布学校核定的人脸告知')
        if dialog is None:
            return
        footer = tk.Frame(dialog, bg=WHITE)
        footer.pack(side='bottom', fill='x', padx=20, pady=12)
        scroll = ScrollableFrame(dialog)
        scroll.pack(fill='both', expand=True, padx=20, pady=15)
        body = scroll.body
        label(body, '新版本发布后，既有授权需重新登记；不能覆盖旧正文。', color=MUTED, wraplength=640).pack(anchor='w')
        label(body, '告知版本（1–60 字）').pack(anchor='w', pady=(12, 4))
        version = tk.StringVar()
        ttk.Entry(body, textvariable=version).pack(fill='x')
        label(body, '完整正文（20–12000 字）').pack(anchor='w', pady=(12, 4))
        text = tk.Text(body, height=12, wrap='word', font=('Microsoft YaHei UI', 10))
        text.pack(fill='x')
        label(body, '请填写学校核定的目的、资料范围、保存期限原则、访问者、撤回渠道及人工点名替代方式。\n此界面不生成或认证法律文件，不自动取得学生或监护人同意。', size=9, color=MUTED, wraplength=640, justify='left').pack(anchor='w', pady=10)
        confirmed = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text='确认正文已由学校核定，理解更新后需重新登记授权', variable=confirmed).pack(anchor='w')
        def publish():
            if not confirmed.get():
                dialog.feedback.set('请先确认告知正文已由学校核定。')
                return
            self.submit('发布人脸告知', partial(self.service.publish_notice, version.get(), text.get('1.0', 'end-1c'), confirmed=True), dialog)
        submit = button(footer, '发布新版本', publish, primary=True)
        submit.pack(side='right')
        dialog.submit_buttons.append(submit)
        button(footer, '取消', lambda: self.close_dialog(dialog), plain=True).pack(side='left')

    @ui_flow
    def grant_dialog(self):
        student_id = self.selected_id()
        plan = yield IO(partial(self.service.authorization_plan, student_id))
        dialog = self.new_dialog('登记已核验的人脸授权')
        if dialog is None:
            return
        footer = tk.Frame(dialog, bg=WHITE)
        footer.pack(side='bottom', fill='x', padx=20, pady=12)
        scroll = ScrollableFrame(dialog)
        scroll.pack(fill='both', expand=True, padx=20, pady=15)
        body = scroll.body
        label(body, f"{student_id} {plan['student']['name']} · 告知版本 {plan['notice']['version']}", size=14, bold=True).pack(anchor='w')
        text = tk.Text(body, height=8, wrap='word', font=('Microsoft YaHei UI', 10))
        text.insert('1.0', plan['notice']['text'])
        text.configure(state='disabled')
        text.pack(fill='x', pady=10)
        expiry, basis, reference = tk.StringVar(), tk.StringVar(value='本人明确同意'), tk.StringVar()
        for title, variable in [('保存截止日（YYYY-MM-DD，包含当天，不预填期限）', expiry), ('授权凭据引用（仅编号或受保护存放位置，不上传原件）', reference)]:
            label(body, title, size=10).pack(anchor='w', pady=(8, 3))
            ttk.Entry(body, textvariable=variable).pack(fill='x')
        ttk.Combobox(body, textvariable=basis, values=['本人明确同意', '监护人明确同意'], state='readonly').pack(anchor='w', pady=12)
        confirmed = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text='已展示此版本完整告知，核验明确同意与保存期限；可选择人工点名', variable=confirmed).pack(anchor='w')
        def grant():
            if not confirmed.get():
                dialog.feedback.set('必须先核验真实授权；未同意时请使用人工点名。')
                return
            self.submit('登记人脸授权', partial(self.service.grant, student_id, plan['token'], expiry.get(), basis.get(), reference.get(), confirmed=True), dialog)
        submit = button(footer, '确认登记', grant, primary=True)
        submit.pack(side='right')
        dialog.submit_buttons.append(submit)
        button(footer, '取消', lambda: self.close_dialog(dialog), plain=True).pack(side='left')

    def revoke_dialog(self):
        try:
            student_id = self.selected_id()
        except ValueError as error:
            self.handle_error(error)
            return
        dialog = self.new_dialog('登记撤回人脸授权', '700x500')
        if dialog is None:
            return
        body = tk.Frame(dialog, bg=WHITE, padx=20, pady=20)
        body.pack(fill='both', expand=True)
        label(body, f'撤回学号 {student_id} 的人脸授权', size=15, bold=True).pack(anchor='w')
        label(body, '记录提交后阻止该生采集和刷脸，含其模板的模型暂停使用。\n名单、考勤和原始到课证据保留；原样本需另行受控清理，历史备份需单独处理。', color=MUTED, justify='left', wraplength=620).pack(anchor='w', pady=18)
        reason = tk.StringVar()
        label(body, '撤回原因 / 请求凭据引用（1–300 字）').pack(anchor='w')
        ttk.Entry(body, textvariable=reason).pack(fill='x', pady=10)
        submit = button(body, '确认登记撤回', lambda: self.submit('撤回人脸授权', partial(self.service.revoke, student_id, reason.get()), dialog), primary=True)
        submit.pack(anchor='e', pady=14)
        dialog.submit_buttons.append(submit)

    @ui_flow
    def cleanup_dialog(self, resume=False):
        if self.owner.busy:
            return
        plan = None if resume else (yield IO(self.service.preview_cleanup))
        snapshot = yield IO(self.service.snapshot)
        if resume and not snapshot['pending_job']:
            self.say('没有需要续办的人脸清理。')
            return
        dialog = self.new_dialog('续办人脸清理' if resume else '受控清理预览')
        if dialog is None:
            return
        footer = tk.Frame(dialog, bg=WHITE)
        footer.pack(side='bottom', fill='x', padx=20, pady=12)
        scroll = ScrollableFrame(dialog)
        scroll.pack(fill='both', expand=True, padx=20, pady=16)
        body = scroll.body
        title = ('此前已确认的清理尚未完成，将继续删除清单中的资料。' if resume else
                 f"将移除 {plan['sample_count']} 张未授权/撤回/过期/旧告知或无法归属的样本；{'同时移除合并模型与清单，需另行训练。' if plan['removes_model'] else '不移除当前模型。'}")
        label(body, title, size=12, bold=True, wraplength=640, justify='left').pack(anchor='w')
        label(body, '不会删除有效授权者的原始样本、名单、考勤或到课时间证据。\n历史备份/导出/手工副本不在本次清理范围；文件删除不承诺不可恢复擦除。\n确认后清理意图先落库；失败或中断保留待续办状态，期间全部人脸与新备份暂停。', size=9, color=MUTED, wraplength=640, justify='left').pack(anchor='w', pady=12)
        if plan:
            files = tk.Text(body, height=6, wrap='word')
            files.insert('1.0', '\n'.join(item['kind']+' / '+item['name'] for item in plan['files']) or '没有可清理文件')
            files.configure(state='disabled')
            files.pack(fill='x', pady=8)
        password, reason = tk.StringVar(), tk.StringVar()
        label(body, '管理密码复核').pack(anchor='w')
        ttk.Entry(body, textvariable=password, show='●').pack(fill='x', pady=5)
        if not resume:
            label(body, '本次清理原因（1–300 字）').pack(anchor='w', pady=(8, 0))
            ttk.Entry(body, textvariable=reason).pack(fill='x', pady=5)
        def clean():
            if self.owner.busy or not messagebox.askyesno('确认删除人脸资料', '确认执行上述清理？确认后不提供界面撤销，请自行处理历史备份中的副本。', parent=dialog):
                return
            credentials = password.get()
            password.set('')
            work = partial(self.service.resume_cleanup, credentials) if resume else partial(self.service.apply_cleanup, credentials, plan['token'], reason.get())
            self.submit('受控清理人脸资料', work, dialog)
        submit = button(footer, '确认续办' if resume else '确认清理', clean, primary=True)
        submit.pack(side='right')
        dialog.submit_buttons.append(submit)
        if plan is not None and not plan['files']:
            submit.configure(state='disabled')
        button(footer, '取消', lambda: self.close_dialog(dialog), plain=True).pack(side='left')

    @ui_flow
    def submit(self, title, work, dialog):
        if self.owner.busy or dialog.submitting:
            return
        self.submitting(dialog, True)
        dialog.cancel_requested = False
        dialog.feedback.set('正在处理，请稍候；取消会等待当前步骤结束。')
        try:
            limits = yield IO(MaintenanceService(self.store).timeouts)
        except Exception as error:
            self.submitting(dialog, False)
            if dialog.winfo_exists():
                dialog.feedback.set(user_message(error))
            return
        if not dialog.winfo_exists():
            return
        if dialog.cancel_requested:
            self.submitting(dialog, False)
            dialog.feedback.set('操作已取消，可以关闭或重新提交。')
            return
        def completed(result):
            self.submitting(dialog, False)
            if dialog.winfo_exists():
                self.close_dialog(dialog)
            self.refresh()
            self.owner.refresh_students()
            self.say(title+'完成；授权、模型与历史副本需分别核对。')
        def failed(error):
            self.submitting(dialog, False)
            if dialog.winfo_exists():
                dialog.feedback.set(user_message(error))
        # 读设置期间别的页面可能启动了任务；保持表单可重试。
        if self.owner.busy:
            self.submitting(dialog, False)
            dialog.feedback.set('请等待当前任务结束后再提交。')
            return
        self.owner.run_job(title, work, done=completed, timeout=limits['training'], on_error=failed, parent=dialog)
