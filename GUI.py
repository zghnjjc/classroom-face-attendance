"""只提供角色入口，采集训练不会出现在学生签到界面。"""

import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox
from app_errors import log_error,user_message

from app_config import DATA_DIR, PROGRAM_DIR
from app_entry import role_command
from ui_common import BG, BLUE, MUTED, WHITE, BaseWindow, button, label, panel, switch_view
from task_runtime import FlowHost, IO, ui_flow
from first_use import FirstUseService


def launch_script(script):
    roles = {"student_app.py": "student", "admin_app.py": "admin", "recovery_app.py": "restore"}
    if script not in roles:
        raise ValueError("不支持的程序入口。")
    environment=dict(os.environ,PYTHONUTF8="1",PYTHONIOENCODING="utf-8",PYINSTALLER_RESET_ENVIRONMENT="1")
    return subprocess.Popen(role_command(roles[script], DATA_DIR),cwd=PROGRAM_DIR,env=environment,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)


class Launcher(BaseWindow):
    def __init__(self,root):
        super().__init__(root,"课堂签到","请选择使用入口",geometry="1000x740")
        self.content.columnconfigure((0,1),weight=1,uniform="roles")
        self.content.rowconfigure(0,weight=1)
        for column,title,subtitle,description,script in [
            (0,"学生签到","面向课堂现场","查看当前课次，开启摄像头后依次签到。\n此入口不提供名单修改、人脸采集与模型训练。","student_app.py"),
            (1,"教师管理","面向老师与管理员","维护班级名单、采集训练、开启课次，\n查询考勤、人工补签与导出记录。","admin_app.py"),
        ]:
            card=panel(self.content); card.grid(row=0,column=column,sticky="nsew",padx=(0,10) if column==0 else (10,0),pady=(12,20))
            inner=tk.Frame(card,bg=WHITE); inner.pack(fill="both",expand=True,padx=24,pady=24)
            label(inner,subtitle,size=10,color=BLUE).pack(anchor="w")
            label(inner,title,size=23,bold=True).pack(anchor="w",pady=(10,16))
            description_label=label(inner,description,size=10,color=MUTED,justify="left",wraplength=350)
            description_label.pack(fill="x")
            description_label.bind('<Configure>',lambda event, widget=description_label:widget.configure(wraplength=max(1,event.width)))
            button(inner,"进入学生端  →" if column==0 else "进入管理端  →",lambda item=script:self.open_role(item),primary=column==0).pack(fill="x",side="bottom",pady=(20,0))
        self.say("学生端与管理端独立运行；管理端首次使用需设置密码。")

    def open_role(self,script):
        try:
            launch_script(script)
            self.close()
        except OSError as error:
            log_error("role_launch_failed",error)
            messagebox.showerror("无法打开入口",user_message(error),parent=self.root)


class InitialLauncher(FlowHost):
    def __init__(self, root, store, async_io=True):
        self.init_flows(root, async_io)
        self.store = store
        self.frame = tk.Frame(root, bg=WHITE, padx=24, pady=24)
        self.frame.pack(fill='both', expand=True)
        self.message = tk.StringVar(value='正在检查首次使用进度…')
        label(self.frame, textvariable=self.message, wraplength=600, justify='left').pack(pady=20)
        button(self.frame, '重试', self.prepare).pack(anchor='w')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.prepare()

    def schedule(self, milliseconds, callback):
        return self.root.after(milliseconds, callback) if not self.closed else None

    def say(self, message):
        self.message.set(message)

    def handle_error(self, error):
        log_error('first_use_entry_failed', error)
        self.say(user_message(error))

    @ui_flow
    def prepare(self):
        progress = yield IO(FirstUseService(self.store).prepare)
        def create():
            if progress and not progress['completed']:
                from admin_app import AdminLogin
                return AdminLogin(self.root, self.store, async_io=self.async_io, onboarding=True, setup_progress=progress)
            return Launcher(self.root)
        self.application = switch_view(self.root, self.frame, create)
        self.closed = True

    def close(self):
        self.cancel_flows()
        self.closed = True
        self.root.destroy()


def main():
    from app_startup import launch_entry
    launch_entry(lambda root,store:InitialLauncher(root, store))


if __name__=="__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
