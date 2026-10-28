"""自动备份设置及单个可取消的后台调度器；失败以持久状态提示。"""

import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, ttk

from classroom_attendance.core.app_config import BACKUP_TIMEOUT
from classroom_attendance.core.app_errors import TaskCancelled, log_error, user_message
from classroom_attendance.storage.automatic_backup import AutomaticBackup
from classroom_attendance.core.task_runtime import TaskControl, current_control
from classroom_attendance.ui.ui_common import MUTED, WHITE, button, label


def backup_status(snapshot):
    config, state = snapshot['settings'], snapshot['state']
    if config.get('invalid'):
        return '自动备份设置不可读，已暂停；请重新保存设置。'
    if config.get('relocated'):
        return '检测到数据目录迁移或换机恢复，自动备份已暂停；请确认保存位置并重新启用。'
    if not config['enabled']:
        return '自动备份未启用。'
    last = state.get('last_success', '').replace('T', ' ') or '尚无记录'
    if state.get('invalid'):
        return '自动备份状态不可读，将按当天计划重新检查。'
    if state.get('status') in {'error', 'deferred'}:
        return f"自动备份{'延后' if state['status'] == 'deferred' else '失败'}：{state['message']} 下次重试 {state['retry_at'].replace('T', ' ')}；最近成功 {last}"
    return f"每日 {config['time']} · 保留最近 {config['keep']} 份 · 最近成功 {last}" + ('\n'+state['message'] if state.get('status') == 'warning' else '')


class AutomaticBackupPanel:
    def __init__(self, owner, parent):
        self.owner, self.service = owner, AutomaticBackup(owner.store)
        self.running, self.closed, self.control = False, False, None
        self.kind = None
        self.results = queue.Queue()
        self.snapshot = None
        self.enabled = tk.BooleanVar(master=owner.root)
        self.time = tk.StringVar(master=owner.root, value='18:00')
        self.keep = tk.StringVar(master=owner.root, value='7')
        self.directory = tk.StringVar(master=owner.root)
        self.status = tk.StringVar(master=owner.root, value='正在读取自动备份设置…')
        self.location = tk.StringVar(master=owner.root)
        label(parent, '自动备份', size=13, bold=True).pack(anchor='w', pady=(8, 8))
        hint = label(parent, '启用后，仅在教师端打开期间每天备份一次；错过时间时补做当天备份。ZIP 未加密，请选择受保护的磁盘或移动硬盘。',
                     size=9, color=MUTED, justify='left', wraplength=800)
        hint.pack(fill='x', pady=(0, 8))
        hint.bind('<Configure>', lambda e: hint.configure(wraplength=max(1, e.width)))
        self.enable_check = ttk.Checkbutton(parent, text='启用每日完整备份', variable=self.enabled)
        self.enable_check.pack(anchor='w')
        row = tk.Frame(parent, bg=WHITE)
        row.pack(fill='x', pady=8)
        label(row, '每日时间', size=9).pack(side='left')
        self.time_entry = ttk.Entry(row, textvariable=self.time, width=7)
        self.time_entry.pack(side='left', padx=(8, 20))
        label(row, '保留份数（1–90）', size=9).pack(side='left')
        self.keep_entry = ttk.Spinbox(row, textvariable=self.keep, from_=1, to=90, width=6)
        self.keep_entry.pack(side='left', padx=8)
        directory_row = tk.Frame(parent, bg=WHITE)
        directory_row.pack(fill='x', pady=(0, 6))
        self.directory_entry = ttk.Entry(directory_row, textvariable=self.directory)
        self.directory_entry.pack(side='left', fill='x', expand=True)
        self.choose_button = button(directory_row, '选择位置', self.choose, plain=True)
        self.choose_button.pack(side='left', padx=(8, 0))
        actions = tk.Frame(parent, bg=WHITE)
        actions.pack(fill='x', pady=(0, 8))
        self.save_button = button(actions, '保存自动备份设置', self.save)
        self.save_button.pack(side='left')
        self.retry_button = button(actions, '立即备份 / 重试', lambda: self.start(force=True), plain=True)
        self.retry_button.pack(side='left', padx=8)
        self.cancel_button = button(actions, '取消自动备份', self.cancel, plain=True)
        self.cancel_button.pack(side='left')
        self.feedback_labels = []
        for variable in (self.status, self.location):
            widget = label(parent, textvariable=variable, size=9, color=MUTED, justify='left', wraplength=800, width=1)
            widget.pack(fill='x', pady=(0, 6))
            widget.bind('<Configure>', lambda e, w=widget: w.configure(wraplength=max(1, e.width-8)))
            self.feedback_labels.append(widget)
        label(parent, '只清理本程序索引中且哈希一致的旧自动备份；手动备份、恢复前快照和未知文件保留。',
              size=9, color=MUTED, wraplength=800, justify='left').pack(anchor='w', pady=(0, 20))
        self._submit('load', self.service.snapshot)
        owner.schedule(1000, self.tick)

    def choose(self):
        folder = filedialog.askdirectory(parent=self.owner.root, title='选择自动备份保存位置', initialdir=self.directory.get() or str(self.service.data))
        if folder:
            self.directory.set(folder)

    def render(self, snapshot, populate=False):
        self.snapshot = snapshot
        if populate:
            config = snapshot['settings']
            self.enabled.set(config['enabled'])
            self.time.set(config['time'])
            self.keep.set(str(config['keep']))
            self.directory.set(config['directory'])
        self.status.set(backup_status(snapshot))
        self.location.set('实际自动备份目录：\n' + snapshot['folder'])
        self.sync_buttons()
        dashboard = getattr(self.owner, 'dashboard', None)
        if dashboard:
            dashboard.render_face_status()

    def sync_buttons(self):
        for widget in (self.save_button, self.retry_button, self.choose_button, self.enable_check, self.time_entry, self.keep_entry, self.directory_entry):
            widget.configure(state='disabled' if self.running else 'normal')
        self.cancel_button.configure(state='normal' if self.running and self.control and not self.control.cancelled.is_set() else 'disabled')

    def _submit(self, kind, work):
        if self.running or self.closed:
            return
        self.running = True
        self.kind = kind
        self.control = TaskControl(BACKUP_TIMEOUT)
        self.sync_buttons()
        results, control = self.results, self.control
        def execute():
            token = current_control.set(control)
            try:
                control.check()
                results.put((kind, work(), None))
            except Exception as error:
                log_error('automatic_backup_task_failed', error)
                results.put((kind, None, error))
            finally:
                current_control.reset(token)
        if self.owner.async_io:
            threading.Thread(target=execute, daemon=True).start()
            self.owner.schedule(100, self.poll)
        else:
            execute()
            self.poll()

    def poll(self):
        if self.closed:
            return
        try:
            kind, result, error = self.results.get_nowait()
        except queue.Empty:
            # 底层工作真实结束前保持 running，避免超时后启动重复备份。
            self.owner.schedule(100, self.poll)
            return
        self.running, self.control, self.kind = False, None, None
        if error:
            self.status.set('自动备份已取消，可手动重试；10 分钟后再检查。' if isinstance(error, TaskCancelled) else '自动备份操作未完成：'+user_message(error))
        elif kind in {'load', 'save'}:
            self.render(result, populate=True)
        elif result.get('snapshot'):
            self.render(result['snapshot'])
            if result['outcome'] in {'success', 'warning'}:
                self.owner.dashboard.refresh()
        self.sync_buttons()

    def save(self):
        try:
            keep = int(self.keep.get())
        except ValueError:
            self.status.set('保留数量请输入 1–90 的整数。')
            return
        values = dict(enabled=self.enabled.get(), time=self.time.get(), keep=keep, directory=self.directory.get())
        service = self.service
        def work():
            service.save_settings(**values)
            return service.snapshot()
        self._submit('save', work)

    def start(self, force=False):
        if self.running or self.closed:
            return
        if self.owner.busy or (self.owner.capture_dialog and self.owner.capture_dialog.window.winfo_exists()):
            if force:
                self.owner.say('请等待当前任务或采集结束后再执行自动备份')
            return
        service = self.service
        if force or (self.snapshot and service.due(self.snapshot['settings'], self.snapshot['state'], datetime.now())):
            self.status.set('正在自动备份…界面可继续使用；取消会等待当前步骤结束。')
        self._submit('backup', lambda: service.run_due(force=force))

    def tick(self):
        if self.closed:
            return
        self.start()
        self.owner.schedule(15000, self.tick)

    def cancel(self):
        if self.control:
            self.control.cancelled.set()
            self.status.set('正在取消自动备份，请等待当前步骤结束…')
            self.sync_buttons()

    def close(self):
        self.closed = True
        if self.control:
            self.control.cancelled.set()
