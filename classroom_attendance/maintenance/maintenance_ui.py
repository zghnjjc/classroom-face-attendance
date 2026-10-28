"""教师现场维护页及不依赖名单或模型的摄像头检测窗口。"""

import queue
import tkinter as tk
from functools import partial
from tkinter import filedialog, messagebox, ttk

from classroom_attendance.camera.camera_diagnostics import CameraPreviewWorker
from classroom_attendance.maintenance.maintenance_tools import MaintenanceService, MIB, disk_status
from classroom_attendance.core.task_runtime import FlowHost, IO, ui_flow
from classroom_attendance.ui.ui_common import MUTED, WHITE, CameraView, ScrollableFrame, button, fit_window, label
from classroom_attendance.core.app_errors import user_message


def disk_text(value):
    state = {'ok': '充足', 'warning': '偏低，建议清理', 'critical': '不足，写入操作将受限'}[value['state']]
    return f"可用 {value['free_bytes'] / MIB:.0f} MiB / 总计 {value['total_bytes'] / MIB:.0f} MiB · {state}"


class CameraDiagnosticDialog(FlowHost):
    def __init__(self, owner, worker_factory=None):
        root = tk.Toplevel(owner.root)
        self.init_flows(root, owner.async_io)
        self.store, self.worker_factory = owner.store, worker_factory or CameraPreviewWorker
        self.worker, self.closing, self.failed = None, False, False
        self.busy, self.stopping = True, False
        self.after_ids = []
        root.title('独立摄像头检测')
        fit_window(root, '820x620', minimum=(680, 500))
        root.transient(owner.root)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.configure(bg=WHITE)
        self.index = tk.StringVar(value='0')
        self.message = tk.StringVar(value='正在读取摄像头编号…')
        label(root, '摄像头检测 · 不采集、不识别、不签到', size=16, bold=True).pack(anchor='w', padx=20, pady=15)
        actions = tk.Frame(root, bg=WHITE)
        actions.pack(side='bottom', fill='x', padx=20, pady=14)
        self.close_button = button(actions, '关闭', self.close, plain=True)
        self.close_button.pack(side='right')
        self.stop_button = button(actions, '停止检测', self.stop)
        self.stop_button.pack(side='right', padx=10)
        self.start_button = button(actions, '开始检测 / 重试', self.start, primary=True)
        self.start_button.pack(side='left')
        self.save_button = button(actions, '保存此编号', self.save)
        self.save_button.pack(side='left', padx=10)
        label(root, textvariable=self.message, color=MUTED, wraplength=640, justify='left').pack(side='bottom', fill='x', padx=20, pady=8)
        row = tk.Frame(root, bg=WHITE)
        row.pack(fill='x', padx=20)
        label(row, '设备编号（0–9）').pack(side='left')
        self.spin = ttk.Spinbox(row, textvariable=self.index, from_=0, to=9, width=6)
        self.spin.pack(side='left', padx=12)
        self.view = CameraView(root)
        self.view.pack(fill='both', expand=True, padx=20, pady=12)
        self.sync()
        self.load()

    def schedule(self, delay, callback):
        if not self.closed:
            self.after_ids.append(self.root.after(delay, callback))
            self.after_ids = self.after_ids[-100:]

    def say(self, message):
        self.message.set(message)

    def handle_error(self, error):
        self.say(user_message(error))

    def sync(self):
        idle = self.worker is None and not self.closing and not self.busy
        self.start_button.configure(state='normal' if idle else 'disabled')
        self.save_button.configure(state='normal' if idle else 'disabled')
        self.spin.configure(state='normal' if idle else 'disabled')
        self.stop_button.configure(state='normal' if self.worker and not self.closing and not self.stopping else 'disabled')

    @ui_flow
    def load(self):
        try:
            self.index.set(str((yield IO(self.store.camera_index))))
            self.say('检测独占摄像头；先停止其他窗口的采集或识别。画面仅保存在内存。')
        finally:
            self.busy = False
            self.sync()

    def camera_index(self):
        value = self.index.get().strip()
        if value not in tuple(map(str, range(10))):
            raise ValueError('摄像头编号必须为 0–9 的整数。')
        return int(value)

    def start(self):
        if self.worker or self.closing or self.busy or self._running_flows:
            return
        try:
            index = self.camera_index()
        except ValueError as error:
            self.handle_error(error)
            return
        self.failed = False
        self.stopping = False
        self.worker = self.worker_factory(index, self.store.path.parent / '.camera.lock')
        self.worker.start()
        self.say('正在连接；拔插设备后请等待“设备已释放”，再点击重试。')
        self.sync()
        self.schedule(50, self.poll)

    def stop(self):
        if self.worker:
            self.stopping = True
            self.worker.stop()
            self.say('正在停止并释放设备，请稍候…')
            self.stop_button.configure(state='disabled')

    def poll(self):
        worker = self.worker
        if self.closed or not worker:
            return
        self.view.render(worker.take_frame())
        while True:
            try:
                kind, value = worker.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'ready' and not self.closing and not self.stopping:
                self.say('已收到连续画面，请目视确认方向、亮度与清晰度。停止后可保存设备编号。')
            elif kind == 'error':
                self.failed = True
                self.say(value + '；正在释放设备，释放后可重试或使用人工点名。')
            elif kind == 'stopped':
                self.worker = None
                self.view.clear()
                self.sync()
                if self.closing:
                    self.close()
                else:
                    self.say('设备已释放。' + ('请检查连接、系统摄像头权限及占用程序，再重试；也可使用人工点名。' if self.failed else '可重新检测或保存编号。'))
                return
        self.schedule(50, self.poll)

    @ui_flow
    def save(self):
        if self.worker or self.closing or self.busy:
            return
        index = self.camera_index()
        self.busy = True
        self.sync()
        try:
            yield IO(partial(self.store.camera_index, index))
            self.say('摄像头编号已保存，下次开启相机生效；此操作不代表完成现场验收。')
        finally:
            self.busy = False
            self.sync()

    def close(self):
        if self.closed:
            return True
        self.closing = True
        if self.worker:
            self.stop()
            self.sync()
            return False
        if self._running_flows:
            self.say('正在等待设置读取或保存完成…')
            self.schedule(50, self.close)
            return False
        self.closed = True
        for token in self.after_ids:
            try:
                self.root.after_cancel(token)
            except tk.TclError:
                pass
        self.root.destroy()
        return True


class MaintenancePanel(tk.Frame, FlowHost):
    def __init__(self, owner, parent):
        tk.Frame.__init__(self, parent, bg=WHITE)
        self.init_flows(owner.root, owner.async_io)
        self.owner, self.store = owner, owner.store
        self.service = MaintenanceService(self.store)
        self.dialog = None
        scroll = ScrollableFrame(self)
        scroll.pack(fill='both', expand=True)
        body = scroll.body
        label(body, '现场维护', size=18, bold=True).pack(anchor='w', pady=(6, 12))
        label(body, '摄像头检测不依赖课次、名单或模型。断线不自动恢复签到，确认设备后手动重试。', color=MUTED, wraplength=760).pack(anchor='w')
        button(body, '独立摄像头检测', self.open_camera, primary=True).pack(anchor='w', pady=14)
        ttk.Separator(body).pack(fill='x', pady=12)
        label(body, '磁盘空间', size=14, bold=True).pack(anchor='w')
        self.space = tk.StringVar(value='尚未检查')
        label(body, textvariable=self.space, color=MUTED, wraplength=760, justify='left').pack(anchor='w', pady=8)
        row = tk.Frame(body, bg=WHITE)
        row.pack(anchor='w')
        button(row, '检查数据磁盘', self.refresh).pack(side='left')
        button(row, '检查其他保存位置', self.check_destination, plain=True).pack(side='left', padx=12)
        label(body, '低于 512 MiB 提醒；采集、训练和导出至少保留 100 MiB。备份还需预留约两倍源文件容量。\n这是预检而非空间锁定，后续磁盘写满仍可能失败。', color=MUTED, size=9, justify='left').pack(anchor='w', pady=10)
        ttk.Separator(body).pack(fill='x', pady=12)
        label(body, '独立任务超时（秒）', size=14, bold=True).pack(anchor='w')
        self.training, self.export_seconds = tk.StringVar(), tk.StringVar()
        row = tk.Frame(body, bg=WHITE)
        row.pack(anchor='w', pady=12)
        label(row, '训练').pack(side='left')
        ttk.Entry(row, textvariable=self.training, width=7).pack(side='left', padx=(8, 20))
        label(row, '导出').pack(side='left')
        ttk.Entry(row, textvariable=self.export_seconds, width=7).pack(side='left', padx=(8, 20))
        button(row, '保存超时', self.save).pack(side='left')
        label(body, '分别允许 30–3600 秒，默认训练 180 秒 / 导出 120 秒，下次任务生效。\n超时会请求取消；原生训练或文件保存需返回检查点后才释放任务，不会强杀写入。', color=MUTED, size=9, justify='left').pack(anchor='w')
        ttk.Separator(body).pack(fill='x', pady=18)
        label(body, '脱敏诊断包', size=14, bold=True).pack(anchor='w')
        label(body, '仅导出环境版本、记录数量、空间、超时与白名单错误计数；不含原始日志、路径或个人记录。\n不含数据库、模型、人脸、口令；不自动上传，分享前请自行核对数量与时间等业务信息。', color=MUTED, size=9, justify='left').pack(anchor='w', pady=8)
        button(body, '导出脱敏诊断 ZIP', self.export_diagnostics).pack(anchor='w', pady=10)
        self.load()

    def schedule(self, delay, callback):
        return self.owner.schedule(delay, callback)

    def say(self, message):
        self.owner.say(message)

    def handle_error(self, error):
        self.owner.handle_error(error)

    @ui_flow
    def load(self):
        value = yield IO(self.service.snapshot)
        self.training.set(str(value['timeouts']['training']))
        self.export_seconds.set(str(value['timeouts']['export']))
        self.space.set('数据磁盘：' + disk_text(value['data_disk']))

    @ui_flow
    def refresh(self):
        value = yield IO(partial(disk_status, self.store.path.parent))
        self.space.set('数据磁盘：' + disk_text(value))

    @ui_flow
    def check_destination(self):
        path = filedialog.askdirectory(parent=self.root, title='选择要检查的保存位置')
        if path:
            value = yield IO(partial(disk_status, path))
            self.space.set('所选位置：' + disk_text(value))

    @ui_flow
    def save(self):
        try:
            training, export = int(self.training.get()), int(self.export_seconds.get())
        except ValueError as error:
            raise ValueError('训练和导出超时分别使用 30–3600 的整数秒。') from error
        yield IO(partial(self.service.save_timeouts, training, export))
        self.say('训练与导出超时已分别保存；下次任务生效。')

    def open_camera(self):
        if self.dialog and not self.dialog.closed:
            self.dialog.root.lift()
            return
        if self.owner.busy or (self.owner.capture_dialog and self.owner.capture_dialog.window.winfo_exists()):
            self.say('请先完成后台任务或停止人脸采集。')
            return
        self.dialog = CameraDiagnosticDialog(self.owner)

    @ui_flow
    def export_diagnostics(self):
        if self.owner.busy:
            self.say('请先等待当前任务完成。')
            return
        if not messagebox.askyesno('导出诊断包', '不含个人记录或原始日志，但含记录数量与生成时间。\n保存后请核对内容，再自行决定是否分享。继续？', parent=self.root):
            return
        path = filedialog.asksaveasfilename(parent=self.root, title='导出脱敏诊断包', initialfile='课堂签到_脱敏诊断.zip',
                                          defaultextension='.zip', filetypes=[('脱敏诊断包', '*.zip')])
        if path:
            timeouts = yield IO(self.service.timeouts)
            self.owner.run_job('导出脱敏诊断包', partial(self.service.export_diagnostics, path), timeout=timeouts['export'])

    def close(self):
        if self.dialog and not self.dialog.closed and not self.dialog.close():
            return False
        self.cancel_flows()
        self.closed = True
        return True
