"""管理密码验证后的首次使用向导。"""

import queue
import tkinter as tk
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from classroom_attendance.core.app_errors import log_error, user_message
from classroom_attendance.storage.attendance_store import read_student_import
from classroom_attendance.camera.camera_diagnostics import CameraPreviewWorker
from classroom_attendance.onboarding.first_use import FirstUseService
from classroom_attendance.core.task_runtime import FlowHost, IO, ui_flow
from classroom_attendance.ui.ui_common import MUTED, WHITE, CameraView, ScrollableFrame, button, fit_window, label, replace_rows, setup_theme, switch_view, tree


class FirstUseWizard(FlowHost):
    def __init__(self, root, store, on_finish, *, async_io=True, manual=False, worker_factory=None):
        self.init_flows(root, async_io)
        self.store, self.service, self.on_finish = store, FirstUseService(store), on_finish
        self.manual, self.worker_factory = manual, worker_factory or CameraPreviewWorker
        self.busy, self.closing = False, False
        self.reviewing = False
        self.worker, self.pending_camera, self.plan, self.data = None, None, None, None
        self.camera_good = False
        self.page = 'camera'
        self.after_ids = set()
        setup_theme(root)
        root.title('课堂签到 · 首次使用向导')
        fit_window(root, '960x720', minimum=(820, 550))
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = lambda kind, error, trace: self.handle_error(error.with_traceback(trace))
        self.frame = tk.Frame(root, bg=WHITE, padx=20, pady=16)
        self.frame.pack(fill='both', expand=True)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(3, weight=1)
        self.heading = tk.StringVar(value='首次使用向导 · 正在读取进度')
        self.message = tk.StringVar(value='正在读取进度…')
        self.camera_index = tk.StringVar(value='0')
        self.roster_hint = tk.StringVar()
        self.summary = tk.StringVar()
        label(self.frame, textvariable=self.heading, size=18, bold=True).grid(row=0, column=0, sticky='w')
        label(self.frame, '① 数据位置  →  ② 管理密码  →  ③ 摄像头测试  →  ④ 学生名单  →  ⑤ 完成',
              size=10, color=MUTED).grid(row=1, column=0, sticky='w', pady=(8, 4))
        self.location = label(self.frame, '', size=9, color=MUTED, justify='left')
        self.location.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        self.location.bind('<Configure>', lambda event: self.location.configure(wraplength=max(1, event.width)))
        self.body = tk.Frame(self.frame, bg=WHITE)
        self.body.grid(row=3, column=0, sticky='nsew')
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=1)
        self.pages = {key: tk.Frame(self.body, bg=WHITE) for key in ('camera', 'roster', 'finish')}
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky='nsew')
        self.build_camera()
        self.build_roster()
        self.build_finish()
        footer = tk.Frame(self.frame, bg=WHITE)
        footer.grid(row=4, column=0, sticky='ew', pady=(8, 0))
        self.back_button = button(footer, '上一步', self.back, plain=True)
        self.back_button.pack(side='left')
        self.close_button = button(footer, '稍后继续', self.close, plain=True)
        self.close_button.pack(side='left', padx=8)
        self.next_button = button(footer, '画面正常，继续', self.next, primary=True)
        self.next_button.pack(side='right')
        self.skip_button = button(footer, '跳过摄像头，使用人工点名', self.skip, plain=True)
        self.skip_button.pack(side='right', padx=8)
        self.faces_button = button(footer, '去采集与训练', lambda: self.finish('students'))
        self.restart_button = button(self.summary_label.master, '重新开始向导', self.restart, plain=True)
        feedback = label(self.frame, textvariable=self.message, size=9, color=MUTED, justify='left')
        feedback.grid(row=5, column=0, sticky='ew', pady=(8, 0))
        feedback.bind('<Configure>', lambda event: feedback.configure(wraplength=max(1, event.width)))
        self.camera_index.trace_add('write', self.camera_changed)
        self.go('camera')
        self.initialize()

    def schedule(self, milliseconds, callback):
        if not self.closed:
            def invoke():
                self.after_ids.discard(token)
                if not self.closed:
                    callback()
            token = self.root.after(milliseconds, invoke)
            self.after_ids.add(token)
            return token

    def say(self, message):
        self.message.set(message)

    def handle_error(self, error):
        log_error('first_use_failed', error)
        self.say(user_message(error))

    def build_camera(self):
        page = self.pages['camera']
        page.rowconfigure(2, weight=1)
        page.columnconfigure(0, weight=1)
        label(page, '请确认能看见实时画面。测试不保存照片，也不会识别或签到。', size=10, color=MUTED).grid(row=0, column=0, sticky='w')
        controls = tk.Frame(page, bg=WHITE)
        controls.grid(row=1, column=0, sticky='ew', pady=10)
        label(controls, '摄像头编号', size=10).pack(side='left')
        self.camera_combo = ttk.Combobox(controls, textvariable=self.camera_index, values=[str(index) for index in range(10)], state='readonly', width=5)
        self.camera_combo.pack(side='left', padx=10)
        self.start_button = button(controls, '开始测试 / 重试', self.start_camera)
        self.start_button.pack(side='left')
        self.stop_button = button(controls, '停止测试', self.stop_camera, plain=True)
        self.stop_button.pack(side='left', padx=8)
        button(controls, '重新读取进度', self.initialize, plain=True).pack(side='right')
        self.view = CameraView(page, height=300)
        self.view.grid(row=2, column=0, sticky='nsew')

    def build_roster(self):
        page = self.pages['roster']
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)
        hint = label(page, textvariable=self.roster_hint, size=10, color=MUTED, justify='left')
        hint.grid(row=0, column=0, sticky='ew')
        hint.bind('<Configure>', lambda event: hint.configure(wraplength=max(1, event.width)))
        controls = tk.Frame(page, bg=WHITE)
        controls.grid(row=1, column=0, sticky='ew', pady=10)
        self.template_button = button(controls, '下载 CSV 模板', self.save_template, plain=True)
        self.template_button.pack(side='left')
        self.read_button = button(controls, '读取名单并预览', self.load_roster)
        self.read_button.pack(side='left', padx=8)
        self.import_button = button(controls, '确认导入并继续', self.import_roster, primary=True)
        self.import_button.pack(side='right')
        frame, self.roster_tree = tree(page, [('id', '学号', 160), ('name', '姓名', 130), ('group', '班级', 150),
                                              ('action', '本次操作', 150), ('active', '导入后状态', 130)],
                                        empty_text='先读取名单并核对预览，再确认导入\n已有学号会更新姓名/班级，停用状态保留')
        frame.grid(row=2, column=0, sticky='nsew')

    def build_finish(self):
        scroll = ScrollableFrame(self.pages['finish'])
        scroll.pack(fill='both', expand=True)
        self.summary_label = label(scroll.body, textvariable=self.summary, size=12, justify='left', wraplength=700)
        self.summary_label.pack(anchor='w', padx=12, pady=12)
        scroll.body.bind('<Configure>', lambda event: self.summary_label.configure(wraplength=max(1, event.width-24)), add='+')

    def sync_actions(self):
        enabled = bool(self.data and not self.busy and not self.closing)
        free = enabled and self.worker is None and not self.reviewing
        self.camera_combo.configure(state='readonly' if free else 'disabled')
        self.start_button.configure(state='normal' if free else 'disabled')
        self.stop_button.configure(state='normal' if self.worker is not None and not self.closing else 'disabled')
        self.back_button.configure(state='normal' if free and self.page != 'camera' else 'disabled')
        self.skip_button.configure(state='normal' if enabled and self.pending_camera is None else 'disabled')
        self.next_button.configure(state='normal' if enabled and (self.camera_good if self.page == 'camera'
                                  else self.data['active_students'] > 0 if self.page == 'roster' else True) else 'disabled')
        for control in (self.template_button, self.read_button):
            control.configure(state='normal' if enabled and not self.reviewing else 'disabled')
        self.import_button.configure(state='normal' if enabled and self.plan else 'disabled')
        self.faces_button.configure(state='normal' if enabled else 'disabled')
        self.restart_button.configure(state='normal' if enabled else 'disabled')

    def go(self, page):
        self.page = page
        if page == 'roster' and self.data:
            self.roster_hint.set(f"当前启用 {self.data['active_students']} 名学生。支持 CSV/XLSX；首行包含学号、姓名，可选班级。\n"
                                 '导入前请核对新增与更新内容，已有学生停用状态保持。')
        for key, frame in self.pages.items():
            frame.grid() if key == page else frame.grid_remove()
        self.heading.set({'camera': '首次使用向导 · 3/5 摄像头测试', 'roster': '首次使用向导 · 4/5 学生名单',
                          'finish': '首次使用向导 · 5/5 完成检查'}[page])
        self.next_button.configure(text={'camera': '画面正常，继续', 'roster': '沿用已有名单，继续', 'finish': '完成并进入首页'}[page])
        self.skip_button.configure(text={'camera': '跳过摄像头，使用人工点名', 'roster': '稍后导入名单', 'finish': '去人工开课'}[page])
        if page == 'finish':
            self.faces_button.pack(side='right', padx=8)
            if self.reviewing:
                self.restart_button.pack(anchor='w', padx=12, pady=(8, 0), before=self.summary_label)
                self.heading.set('首次使用向导 · 设置复核')
                self.close_button.configure(text='关闭')
                self.next_button.configure(text='返回管理首页')
            if self.data:
                state = self.data['state']
                camera = ('已确认画面正常' if state and state['camera'] == 'tested' else
                          '已跳过，可使用人工点名' if state and state['camera'] == 'skipped' else '无向导测试记录')
                roster = ('稍后导入' if state and state['roster'] == 'skipped' else
                          '已导入或沿用' if state else '沿用现有业务名单')
                self.summary.set(f"✓ 数据位置：{self.data['directory']}\n\n✓ 管理密码：已设置并通过登录\n\n"
                                 f"摄像头：{camera}\n\n名单：{roster}；当前启用 {self.data['active_students']} 人\n\n"
                                 '下一步：先在“人脸授权”发布告知并登记同意与期限，再采集和训练；也可直接人工开课。\n'
                                 '人脸授权不会自动取得，旧资料不推定同意；摄像头测试不代表识别准确率已验证。\n'
                                 '没有启用学生时，请先维护名单再创建课次。')
        else:
            self.faces_button.pack_forget()
            self.restart_button.pack_forget()
            self.close_button.configure(text='稍后继续')
        self.sync_actions()

    @ui_flow
    def initialize(self):
        if self.busy or self.worker is not None or self.closing:
            return
        self.busy = True
        self.sync_actions()
        try:
            state = yield IO(self.service.prepare)
            if state is None and not self.manual:
                raise ValueError('已有用户无需自动初始化，请从教师端“数据与设置”重新打开向导。')
            self.data = yield IO(self.service.snapshot)
            state = self.data['state']
            self.reviewing = bool(self.manual and (state is None or state['completed']))
            self.camera_index.set(str(self.data['camera_index']))
            self.location.configure(text=f"数据位置已确认：{self.data['directory']} · 管理密码已验证")
            self.go('finish' if self.reviewing else 'camera' if state['camera'] == 'pending' else 'roster' if state['roster'] == 'pending' else 'finish')
            self.say('当前为设置复核，关闭不会重置进度；需要重做步骤时点击“重新开始向导”。' if self.reviewing else
                     '可继续上次未完成的步骤；“稍后继续”会保留已确认进度。')
        finally:
            self.busy = False
            self.settle()

    @ui_flow
    def restart(self):
        if self.busy or self.worker is not None or self.closing or not self.reviewing:
            return
        if not messagebox.askyesno('重新开始首次使用向导',
                '将把摄像头和名单确认步骤重置为未完成。\n下次登录会继续未完成的向导；已有密码、名单和考勤保留。是否继续？', parent=self.root):
            return
        self.busy = True
        self.sync_actions()
        try:
            yield IO(partial(self.service.prepare, manual=True))
            self.data = yield IO(self.service.snapshot)
            self.reviewing = False
            self.plan = None
            self.camera_good = False
            self.go('camera')
            self.say('向导已重新开始；原密码、名单和考勤保留，可逐步确认或明确跳过。')
        finally:
            self.busy = False
            self.settle()

    def camera_changed(self, *args):
        self.camera_good = False
        self.sync_actions()

    def start_camera(self):
        if self.worker is not None or self.busy or self.closing or not self.data:
            return
        self.camera_good = False
        self.pending_camera = None
        self.view.clear('正在连接摄像头…')
        self.worker = self.worker_factory(int(self.camera_index.get()), self.store.path.parent / '.camera.lock')
        self.worker.start()
        self.say('正在测试，看到稳定画面后点击“画面正常，继续”。')
        self.sync_actions()
        self.schedule(50, self.poll_camera)

    def stop_camera(self):
        if self.worker is not None:
            self.worker.stop()
            self.say('正在停止测试并释放摄像头，请稍候…')

    def poll_camera(self):
        if self.closed or self.worker is None:
            return
        self.view.render(self.worker.take_frame())
        try:
            while True:
                kind, value = self.worker.events.get_nowait()
                if kind == 'ready':
                    self.camera_good = True
                    self.say('已收到连续画面，请确认清晰且设备正确；确认后才保存测试结果。')
                elif kind == 'error':
                    self.camera_good = False
                    if self.pending_camera and self.pending_camera[0] == 'tested':
                        self.pending_camera = None
                    self.say(value + ' 可重试或跳过，改用人工点名。')
                elif kind == 'stopped':
                    self.worker = None
                    self.view.clear('摄像头已释放')
                    pending, self.pending_camera = self.pending_camera, None
                    if pending and not self.closing:
                        self.commit(partial(self.service.record_camera, *pending), 'roster')
                    elif not self.closing and self.message.get().startswith('正在停止测试'):
                        self.say('设备已释放；可确认刚才的正常画面。' if self.camera_good else '设备已释放；可重试或跳过摄像头。')
                    self.settle()
                    return
        except queue.Empty:
            pass
        self.sync_actions()
        self.schedule(50, self.poll_camera)

    def camera_decision(self, result):
        if self.busy or self.closing or (result == 'tested' and not self.camera_good):
            return
        decision = (result, int(self.camera_index.get()))
        if self.worker is None:
            self.commit(partial(self.service.record_camera, *decision), 'roster')
        else:
            self.pending_camera = decision
            self.stop_camera()
            self.sync_actions()

    @ui_flow
    def commit(self, work, page):
        if self.busy or self.closing:
            return
        self.busy = True
        self.sync_actions()
        try:
            yield IO(work)
            self.data = yield IO(self.service.snapshot)
            self.plan = None
            self.go(page)
            self.say('本步已保存。可继续，或关闭窗口后下次登录续办。')
        finally:
            self.busy = False
            self.settle()

    def next(self):
        if self.page == 'camera':
            self.camera_decision('tested')
        elif self.page == 'roster':
            self.commit(partial(self.service.record_roster, 'existing'), 'finish')
        else:
            self.finish('home')

    def skip(self):
        if self.page == 'camera':
            self.camera_decision('skipped')
        elif self.page == 'roster':
            self.commit(partial(self.service.record_roster, 'skipped'), 'finish')
        else:
            self.finish('sessions')

    def back(self):
        if not self.busy and self.worker is None and not self.reviewing:
            self.go('camera' if self.page == 'roster' else 'roster')

    @ui_flow
    def save_template(self):
        if self.busy:
            return
        path = filedialog.asksaveasfilename(parent=self.root, title='保存名单模板', initialfile='学生名单模板.csv',
                                          defaultextension='.csv', filetypes=[('CSV 名单', '*.csv')])
        if path:
            self.busy = True
            self.sync_actions()
            try:
                yield IO(partial(Path(path).write_text, '学号,姓名,班级\n', encoding='utf-8-sig'))
                self.say('模板已保存；学号请以文本保存，避免丢失前导零或长学号精度。')
            finally:
                self.busy = False
                self.settle()

    @ui_flow
    def load_roster(self):
        if self.busy or self.closing:
            return
        path = filedialog.askopenfilename(parent=self.root, title='读取学生名单并预览', filetypes=[('学生名单', '*.csv *.xlsx')])
        if not path:
            return
        self.plan = None
        replace_rows(self.roster_tree, [])
        self.busy = True
        self.sync_actions()
        try:
            rows = yield IO(partial(read_student_import, path))
            self.plan = yield IO(partial(self.service.preview_roster, rows))
            replace_rows(self.roster_tree, [(row['student_id'], (row['student_id'], row['name'], row['group_name'], row['action'],
                                                               '启用' if row['active'] else '保持停用'), '') for row in self.plan['preview']])
            self.say(f"预览 {len(self.plan['rows'])} 人，尚未写入；请核对后点击“确认导入并继续”。")
        finally:
            self.busy = False
            self.settle()

    def import_roster(self):
        if self.plan and not self.busy:
            plan, self.plan = self.plan, None
            self.commit(partial(self.service.import_roster, plan['rows'], plan['token']), 'finish')

    @ui_flow
    def finish(self, target):
        if self.busy or self.worker is not None or self.closing:
            return
        self.busy = True
        self.sync_actions()
        try:
            if not self.reviewing:
                yield IO(self.service.finish)
            try:
                switch_view(self.root, self.frame, lambda: self.on_finish(target))
            except Exception:
                # 完成标记已经提交；保留可重试的检查页，不让后退再写已完成步骤。
                if self.data['state']:
                    self.data['state']['completed'] = True
                self.reviewing = True
                self.go('finish')
                raise
            self.closed = True
            self.clear_timers()
        finally:
            self.busy = False
            if not self.closed:
                self.settle()

    def settle(self):
        if self.closing and not self.busy and self.worker is None:
            self.schedule(0, self.close)
        elif not self.closed:
            self.sync_actions()

    def clear_timers(self):
        for token in tuple(self.after_ids):
            try:
                self.root.after_cancel(token)
            except tk.TclError:
                pass
        self.after_ids.clear()

    def close(self):
        if self.closed:
            return True
        self.closing = True
        if self.worker is not None:
            self.stop_camera()
        if self.busy or self.worker is not None:
            self.say('正在保存当前步骤或释放摄像头，完成后退出；下次登录可继续。')
            self.sync_actions()
            return False
        self.cancel_flows()
        self.clear_timers()
        self.closed = True
        self.root.destroy()
        return True
