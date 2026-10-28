"""首次启动确认数据位置；目录切换通过新进程生效，避免混用旧路径常量。"""

import os
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox

from app_config import DATA_DIR, PROGRAM_DIR, save_data_location
from app_entry import role_command
from ui_common import BG, MUTED, WHITE, button, fit_window, label, setup_theme


class StorageWelcome:
    def __init__(self, root, role, start):
        setup_theme(root)
        root.title("课堂签到 · 首次使用向导 · 1/5 数据位置" if role in {'launcher', 'admin'} else "课堂签到 · 确认数据位置")
        fit_window(root, "760x450", minimum=(620, 400))
        self.root, self.role, self.start = root, role, start
        self.frame = tk.Frame(root, bg=WHITE, padx=28, pady=24)
        self.frame.pack(fill="both", expand=True, padx=18, pady=18)
        label(self.frame, "首次使用向导 · 1/5 数据位置" if role in {'launcher', 'admin'} else "欢迎使用课堂签到", size=19, bold=True).pack(anchor="w")
        label(self.frame, "程序与数据分开保存，更新程序时无需复制数据库。", color=MUTED).pack(anchor="w", pady=(12,18))
        label(self.frame, "默认数据目录", bold=True).pack(anchor="w")
        label(self.frame, str(DATA_DIR), color=MUTED, wraplength=630, justify="left").pack(fill="x", pady=10)
        label(self.frame, "已有数据可选择原目录继续使用；已有 ZIP 备份可进入恢复。\n新用户下一步设置管理密码，再测试摄像头和导入名单。", color=MUTED, justify="left").pack(anchor="w", pady=12)
        actions = tk.Frame(self.frame, bg=WHITE)
        actions.pack(side="bottom", fill="x", pady=8)
        button(actions, "使用默认位置开始", self.accept, primary=True).pack(side="left")
        button(actions, "选择数据目录", self.choose).pack(side="left", padx=8)
        button(actions, "恢复备份", lambda: self.restart("restore", DATA_DIR), plain=True).pack(side="right")

    def accept(self):
        try:
            save_data_location(DATA_DIR)
        except (OSError, ValueError):
            messagebox.showerror('数据位置未保存', '无法保存数据位置，请检查权限或选择其他目录。', parent=self.root)
            return
        self.frame.destroy()
        self.start()

    def choose(self):
        directory = filedialog.askdirectory(parent=self.root, title="选择受保护的运行数据目录")
        if directory:
            self.restart(self.role, directory)

    def restart(self, role, directory):
        try:
            if role != 'restore':
                save_data_location(directory)
            subprocess.Popen(role_command(role, directory), cwd=PROGRAM_DIR,
                             env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1"),
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, ValueError):
            messagebox.showerror("无法打开入口", "请检查程序是否完整，重试或重新解压发行包。", parent=self.root)
            return
        self.root.destroy()
