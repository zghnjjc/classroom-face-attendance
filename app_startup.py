"""三个入口共用的异步预检、中文重试与退出页面。"""

import tkinter as tk
from functools import partial

from app_errors import log_error, user_message
from attendance_store import AttendanceStore
from task_runtime import FlowHost, IO, ui_flow
from ui_common import setup_theme, fit_window, switch_view
from app_config import DATABASE_PATH, MODEL_PATH, DATASET_DIR, PROGRAM_DIR, DATA_DIR, DATA_DIRECTORY_ERROR, HAS_DATA_LOCATION_PREFERENCE
from runtime_guard import InstanceLease
from data_migration import LegacyDataFound, migrate_legacy


def checked_store():
    from backup_service import JOURNAL_NAME, recover_interrupted
    import os
    if DATA_DIRECTORY_ERROR:
        raise ValueError(DATA_DIRECTORY_ERROR)
    directory = DATABASE_PATH.parent
    # 先提示已有数据，不能因默认目标目录不可写而掩盖旧数据的选择入口。
    if (not os.environ.get("CLASSROOM_DATA_DIR") and not HAS_DATA_LOCATION_PREFERENCE
            and PROGRAM_DIR.resolve() != directory.resolve()
            and any((PROGRAM_DIR / name).exists() for name in ("attendance.db", "dataset", "face_model.yml"))):
        raise LegacyDataFound(PROGRAM_DIR)
    if (directory / JOURNAL_NAME).exists():
        recover_interrupted(directory)
    lease = InstanceLease(directory)
    lease.__enter__()
    try:
        if not DATABASE_PATH.exists() and (MODEL_PATH.exists() or any(DATASET_DIR.glob("*"))):
            raise FileNotFoundError("attendance.db")
        store = AttendanceStore()
        store._instance_lease = lease
        return store
    except BaseException:
        lease.__exit__(None, None, None)
        raise


class StartupController(FlowHost):
    def __init__(self, root, factory, store_factory=checked_store, async_io=True, role='launcher'):
        self.init_flows(root, async_io)
        self.factory, self.store_factory = factory, store_factory
        self.role = role
        setup_theme(root)
        root.title("课堂签到 · 启动检查")
        fit_window(root,"760x380",minimum=(640,340))
        self.frame = tk.Frame(root, padx=28, pady=28, bg="white")
        self.frame.pack(fill="both", expand=True)
        self.message = tk.StringVar(value="正在检查数据，请稍候…")
        tk.Label(self.frame, textvariable=self.message, wraplength=580, justify="left",
                 font=("Microsoft YaHei UI", 12), bg="white").pack(fill="x", pady=20)
        self.retry = tk.Button(self.frame, text="重试", command=self.probe, state="disabled")
        self.retry.pack(side="left", padx=12)
        self.migrate_button = tk.Button(self.frame, text="迁移旧数据到用户目录", command=self.migrate)
        self.choose_button = tk.Button(self.frame, text="选择数据目录", command=self.choose_directory)
        self.recovery_button = tk.Button(self.frame, text="恢复备份", command=self.open_recovery)
        self.legacy_source = None
        tk.Button(self.frame, text="退出", command=self.close).pack(side="right", padx=12)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = lambda kind,error,tb:self.handle_error(error.with_traceback(tb))
        self.probe()

    def schedule(self, milliseconds, callback):
        if not self.closed:
            return self.root.after(milliseconds, callback)

    def say(self, message):
        self.message.set(message)

    def handle_error(self, error):
        log_error("startup_failed", error)
        self.choose_button.pack(side='left', padx=8)
        self.recovery_button.pack(side='left', padx=8)
        if isinstance(error, LegacyDataFound):
            self.legacy_source = error.source
            self.say(str(error))
            self.migrate_button.pack(side="left", padx=8)
            self.retry.configure(state="normal")
            return
        if isinstance(error, PermissionError):
            self.say(user_message(error) + f"\n当前数据目录：{DATABASE_PATH.parent}\n请选择可写的数据目录，或处理权限后点击重试。")
        else:
            self.say(user_message(error)+"\n请检查 attendance.db 是否被占用或损坏，处理后点击重试。")
        self.retry.configure(state="normal")

    def choose_directory(self):
        from storage_welcome import StorageWelcome
        from tkinter import filedialog
        directory = filedialog.askdirectory(parent=self.root, title='选择运行数据目录')
        if directory:
            # 复用已验证的进程切换；本窗口不会修改现有路径常量。
            chooser = StorageWelcome.__new__(StorageWelcome)
            chooser.root, chooser.role = self.root, self.role
            chooser.restart(self.role, directory)

    def open_recovery(self):
        from storage_welcome import StorageWelcome
        chooser = StorageWelcome.__new__(StorageWelcome)
        chooser.root, chooser.role = self.root, self.role
        chooser.restart('restore', DATA_DIR)

    @ui_flow
    def migrate(self):
        from tkinter import messagebox
        from app_config import BACKUP_TIMEOUT
        if not self.legacy_source or not messagebox.askyesno("迁移旧数据", "将复制旧目录中的名单、考勤、样本和可用模型到用户数据目录。\n原目录会保留。请先关闭所有旧版窗口。是否继续？", parent=self.root):
            return
        self.migrate_button.configure(state="disabled")
        try:
            yield IO(partial(migrate_legacy, self.legacy_source, DATABASE_PATH.parent), timeout=BACKUP_TIMEOUT)
            self.migrate_button.pack_forget()
            yield from self.probe()
        finally:
            if not self.closed:
                self.migrate_button.configure(state="normal")

    @ui_flow
    def probe(self):
        self.retry.configure(state="disabled")
        self.say("正在检查数据；数据忙时可退出，处理后再重试。")
        store = yield IO(self.store_factory)
        lease = getattr(store, "_instance_lease", None)
        try:
            application = switch_view(self.root, self.frame, lambda: self.factory(self.root, store))
        except Exception:
            if lease is not None:
                lease.__exit__(None, None, None)
            raise
        if lease is not None:
            self.root.bind("<Destroy>", lambda event: lease.__exit__(None, None, None) if event.widget is self.root else None, add="+")
        self.closed = True
        self.application = application

    def close(self):
        self.cancel_flows()
        self.closed = True
        self.root.destroy()


def launch_entry(factory, role="launcher"):
    root = tk.Tk()
    import os
    if not DATA_DIRECTORY_ERROR and not os.environ.get("CLASSROOM_DATA_DIR") and not DATABASE_PATH.exists() and not any((PROGRAM_DIR / name).exists() for name in ("attendance.db", "dataset", "face_model.yml")):
        from storage_welcome import StorageWelcome
        StorageWelcome(root, role, lambda: StartupController(root, factory, role=role))
    else:
        StartupController(root, factory, role=role)
    root.mainloop()
