"""三个入口共用的异步预检、中文重试与退出页面。"""

import tkinter as tk
from functools import partial

from app_errors import log_error, user_message
from attendance_store import AttendanceStore
from task_runtime import FlowHost, IO, ui_flow
from ui_common import setup_theme
from app_config import DATABASE_PATH, MODEL_PATH, DATASET_DIR


def checked_store():
    if not DATABASE_PATH.exists() and (MODEL_PATH.exists() or any(DATASET_DIR.glob("*"))):
        raise FileNotFoundError("attendance.db")
    return AttendanceStore()


class StartupController(FlowHost):
    def __init__(self, root, factory, store_factory=checked_store, async_io=True):
        self.init_flows(root, async_io)
        self.factory, self.store_factory = factory, store_factory
        setup_theme(root)
        root.title("课堂签到 · 启动检查")
        root.geometry("650x340")
        self.frame = tk.Frame(root, padx=28, pady=28, bg="white")
        self.frame.pack(fill="both", expand=True)
        self.message = tk.StringVar(value="正在检查数据，请稍候…")
        tk.Label(self.frame, textvariable=self.message, wraplength=580, justify="left",
                 font=("Microsoft YaHei UI", 12), bg="white").pack(fill="x", pady=20)
        self.retry = tk.Button(self.frame, text="重试", command=self.probe, state="disabled")
        self.retry.pack(side="left", padx=12)
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
        self.say(user_message(error)+"\n请检查 attendance.db 是否被占用或损坏，处理后点击重试。")
        self.retry.configure(state="normal")

    @ui_flow
    def probe(self):
        self.retry.configure(state="disabled")
        self.say("正在检查数据；数据忙时可退出，处理后再重试。")
        store = yield IO(self.store_factory)
        self.frame.destroy()
        self.closed = True
        self.application = self.factory(self.root, store)

    def close(self):
        self.cancel_flows()
        self.closed = True
        self.root.destroy()


def launch_entry(factory):
    root = tk.Tk()
    StartupController(root, factory)
    root.mainloop()
