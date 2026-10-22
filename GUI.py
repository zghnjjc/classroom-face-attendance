"""只提供角色入口，采集训练不会出现在学生签到界面。"""

import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox
from app_errors import log_error,user_message

from app_config import BASE_DIR
from ui_common import BG, BLUE, MUTED, WHITE, BaseWindow, button, label, panel


def launch_script(script):
    environment=dict(os.environ,PYTHONUTF8="1",PYTHONIOENCODING="utf-8")
    return subprocess.Popen([sys.executable,str(BASE_DIR/script)],cwd=BASE_DIR,env=environment,
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
            card=panel(self.content); card.grid(row=0,column=column,sticky="nsew",padx=(0,10) if column==0 else (10,0),pady=(35,90))
            inner=tk.Frame(card,bg=WHITE); inner.pack(fill="both",expand=True,padx=32,pady=42)
            label(inner,subtitle,size=10,color=BLUE).pack(anchor="w")
            label(inner,title,size=25,bold=True).pack(anchor="w",pady=(12,22))
            label(inner,description,size=10,color=MUTED,justify="left",wraplength=350).pack(anchor="w")
            button(inner,"进入学生端  →" if column==0 else "进入管理端  →",lambda item=script:self.open_role(item),primary=column==0).pack(fill="x",side="bottom",pady=(30,0))
        self.say("学生端与管理端独立运行；管理端首次使用需设置密码。")

    def open_role(self,script):
        try:
            launch_script(script)
            self.close()
        except OSError as error:
            log_error("role_launch_failed",error)
            messagebox.showerror("无法打开入口",user_message(error),parent=self.root)


def main():
    from app_startup import launch_entry
    launch_entry(lambda root,store:Launcher(root))


if __name__=="__main__":
    main()
