"""独立离线恢复窗口：先校验、确认与复核口令，再进入维护状态。"""

import os
import queue
import sqlite3
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from classroom_attendance.core.app_config import DATA_DIR, PROGRAM_DIR, RESTORE_CHECK_TIMEOUT
from app_entry import role_command
from classroom_attendance.core.app_errors import ValidationError, log_error, user_message
from classroom_attendance.storage.attendance_store import AttendanceStore
from classroom_attendance.storage.backup_service import inspect_backup, restore_backup
from classroom_attendance.ui.ui_common import BaseWindow, ScrollableFrame, WHITE, MUTED, button, label


def authorize_restore(directory, password, allow_damaged=False):
    database = Path(directory) / "attendance.db"
    if not database.exists():
        return
    try:
        from contextlib import closing
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            auth = connection.execute("SELECT value FROM metadata WHERE key='admin_auth'").fetchone()
    except sqlite3.DatabaseError as error:
        if allow_damaged:
            return
        raise ValidationError("当前数据库无法读取。若确定已损坏，请明确勾选离线恢复授权；原文件仍会保存。") from error
    if auth and auth[0]:
        # 使用原有锁定与限流逻辑，但不在验证之前迁移或初始化当前数据库。
        store = AttendanceStore.__new__(AttendanceStore)
        store.path = database
        if not store.authenticate(password):
            raise ValidationError("当前管理密码不正确，未恢复任何备份数据。")


class RecoveryApp(BaseWindow):
    def __init__(self, root):
        super().__init__(root, "备份校验与恢复", "关闭其他签到窗口后恢复；原数据会保留为恢复前快照", geometry="1000x740")
        self.summary = None
        self.restore_running = False
        self.restore_events = queue.Queue()
        self.source = tk.StringVar()
        self.password = tk.StringVar()
        self.confirmed = tk.BooleanVar()
        self.allow_legacy = tk.BooleanVar()
        self.allow_damaged = tk.BooleanVar()
        self.preview = tk.StringVar(value="请选择本软件生成的 ZIP 备份，完成校验后才能恢复。")
        scroll = ScrollableFrame(self.content)
        scroll.pack(fill="both", expand=True)
        body = scroll.body
        label(body, "恢复到以下数据目录", size=12, bold=True).pack(anchor="w", pady=(8,4))
        label(body, str(DATA_DIR), color=MUTED, wraplength=850, justify="left").pack(anchor="w", pady=(0,16))
        row = tk.Frame(body, bg=WHITE)
        row.pack(fill="x", pady=(0,16))
        self.choose_button = button(row, "选择并校验备份", self.choose)
        self.choose_button.pack(side="right", padx=(10,0))
        ttk.Entry(row, textvariable=self.source, state="readonly").pack(fill="x", expand=True, pady=6)
        label(body, textvariable=self.preview, color=MUTED, wraplength=840, justify="left").pack(anchor="w", pady=(0,20))
        label(body, "当前数据目录的管理密码（已有密码时必填）", size=10).pack(anchor="w")
        ttk.Entry(body, textvariable=self.password, show="•", width=38).pack(anchor="w", pady=(6,12))
        label(body, "恢复后使用备份时的管理密码；新备份中的文件哈希用于完整性校验，不证明备份来源。", color=MUTED, wraplength=840, justify="left").pack(anchor="w", pady=(0,12))
        for text, variable in [
            ("我已关闭所有其他签到窗口，并同意用备份替换此目录的运行数据", self.confirmed),
            ("若为旧格式备份，我理解缺少逐文件哈希，恢复后需要重新训练", self.allow_legacy),
            ("若当前数据库已损坏，授权按本机文件权限离线恢复并保留原文件", self.allow_damaged),
        ]:
            ttk.Checkbutton(body, text=text, variable=variable).pack(anchor="w", pady=5)
        actions = tk.Frame(self.content, bg=WHITE)
        actions.pack(side="bottom", fill="x", pady=(12,0), before=scroll)
        button(actions, "返回入口", self.return_to_launcher, plain=True).pack(side="left")
        self.restore_button = button(actions, "确认恢复", self.restore, primary=True)
        self.restore_button.pack(side="right")
        self.restore_button.configure(state="disabled")
        self.task_buttons.extend([self.choose_button, self.restore_button])
        self.schedule(100, self.poll_restore)

    def choose(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, title="选择课堂签到备份", filetypes=[("ZIP 备份", "*.zip")])
        if not path:
            return
        self.source.set(path)
        self.summary = None
        self.restore_button.configure(state="disabled")
        self.preview.set("正在校验文件清单、SHA-256、数据库版本、完整性与模型…")
        self.run_job("校验备份", lambda: inspect_backup(path), self.checked, timeout=RESTORE_CHECK_TIMEOUT)

    def checked(self, summary):
        self.summary = summary
        self.preview.set(f"备份时间：{summary['created_at'].replace('T',' ')}\n"
                         f"学生 {summary['students']} 人 · 课次 {summary['sessions']} 次 · 签到记录 {summary['checkins']} 条 · 样本 {summary['samples']} 张\n"
                         f"格式：{'旧版（仅结构与数据库检查）' if summary['legacy'] else '新版（文件哈希已校验）'}\n"
                         f"模型：{'备份内容校验通过' if summary['model_ready'] else '需重新训练'}\n恢复后需逐人复核人脸授权，不能直接恢复刷脸。\n{summary['warning']}")
        self.restore_button.configure(state="normal")

    def poll_jobs(self):
        super().poll_jobs()
        if hasattr(self, 'restore_button') and not self.busy and self.summary is None:
            self.restore_button.configure(state='disabled')

    def restore(self):
        if self.busy or self.summary is None:
            self.say("请先选择并校验备份")
            return
        if not self.confirmed.get() or (self.summary["legacy"] and not self.allow_legacy.get()):
            self.say("请先确认关闭其他窗口及相应的恢复限制")
            return
        path, expected = self.source.get(), self.summary["archive_sha256"]
        password, damaged, legacy = self.password.get(), self.allow_damaged.get(), self.allow_legacy.get()
        self.password.set("")
        self.busy = self.restore_running = True
        self.job_started = time.monotonic()
        self.task_box.grid()
        self.cancel_button.configure(state="disabled", text="恢复中")
        for widget in self.task_buttons:
            widget.configure(state="disabled")
        events = self.restore_events

        def run():
            try:
                result = restore_backup(path, DATA_DIR, expected_sha256=expected, allow_legacy=legacy,
                                        authorize=lambda directory: authorize_restore(directory, password, damaged),
                                        progress=lambda text: events.put(("progress", text)))
                events.put(("success", result))
            except Exception as error:
                log_error("restore_failed", error)
                events.put(("error", error))
        threading.Thread(target=run, daemon=True).start()

    def poll_restore(self):
        try:
            while True:
                kind, value = self.restore_events.get_nowait()
                if kind == "progress":
                    self.say(value)
                    continue
                self.busy = self.restore_running = False
                self.task_box.grid_remove()
                self.cancel_button.configure(state="disabled", text="取消任务")
                self.choose_button.configure(state="normal")
                if kind == "success":
                    self.summary = None
                    self.confirmed.set(False)
                    self.say("恢复完成，数据库与模型状态已复核；请返回入口重新登录")
                    self.preview.set("恢复完成。恢复前的数据已保存在：\n" + (value.get("previous_backup") or value["previous_snapshot"]) +
                                     "\n请按敏感数据保护该快照。\n请先在人脸授权页逐人复核授权；模型未就绪时再重新训练，人工点名可先使用。")
                    messagebox.showinfo("恢复完成", "数据已恢复。请返回入口，并使用备份时的管理密码登录。", parent=self.root)
                else:
                    self.restore_button.configure(state="normal" if self.summary else "disabled")
                    self.say("恢复未完成，请核对提示后重试")
                    messagebox.showerror("恢复未完成", user_message(value), parent=self.root)
        except queue.Empty:
            pass
        if self.restore_running:
            self.task_progress.step(7)
            self.task_state.set(f"恢复数据 · {int(time.monotonic()-self.job_started)} 秒")
        self.schedule(100, self.poll_restore)

    def return_to_launcher(self):
        if self.restore_running:
            self.say("正在恢复，请等待完成后返回")
            return
        if self.busy:
            self.say("请先取消校验并等待任务结束")
            return
        environment = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        try:
            subprocess.Popen(role_command("launcher", DATA_DIR), cwd=PROGRAM_DIR, env=environment,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as error:
            self.handle_error(error)
            return
        self.close()

    def close(self):
        if self.restore_running:
            messagebox.showinfo("正在恢复", "正在替换或回退数据，请等待完成。若进程意外中断，下次启动会先处理恢复日志。", parent=self.root)
            return False
        return super().close()


def main():
    root = tk.Tk()
    RecoveryApp(root)
    root.mainloop()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
