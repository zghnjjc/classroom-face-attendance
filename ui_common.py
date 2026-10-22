"""两个独立端共用的轻量 Tkinter 样式与相机预览。"""

import base64
import gc
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from app_config import TASK_TIMEOUT
from app_errors import TaskCancelled, TaskTimeout, log_error, user_message
from task_runtime import FlowHost, TaskControl, current_control

BG, WHITE, INK, MUTED = "#F4F6FA", "#FFFFFF", "#17243C", "#778399"
BLUE, PALE_BLUE, BORDER, GREEN = "#3267E3", "#EDF3FF", "#E4E9F1", "#18876A"
FONT = "Microsoft YaHei UI"


def label(parent, text="", size=10, color=INK, bold=False, bg=WHITE, **kwargs):
    return tk.Label(parent, text=text, bg=bg, fg=color, anchor="w",
                    font=(FONT, size, "bold" if bold else "normal"), **kwargs)


def panel(parent, bg=WHITE):
    return tk.Frame(parent, bg=bg, highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER)


def button(parent, text, command, primary=False, plain=False):
    background, foreground = (BLUE, WHITE) if primary else (WHITE, MUTED) if plain else (PALE_BLUE, BLUE)
    result = tk.Button(parent, text=text, command=command, relief="flat", bd=0, cursor="hand2",
                       font=(FONT,10,"bold"), bg=background, fg=foreground, padx=17, pady=10,
                       activebackground="#2457CD" if primary else "#E1E9F8",
                       activeforeground=foreground, disabledforeground="#A5AFC0")
    return result


def entry_field(parent, title, variable, row, width=24, show=None):
    label(parent, title, size=9, color=MUTED).grid(row=row, column=0, sticky="w", pady=(10,5))
    widget = ttk.Entry(parent, textvariable=variable, width=width, show=show or "")
    widget.grid(row=row+1, column=0, sticky="ew")
    return widget


def tree(parent, columns, height=8):
    frame = tk.Frame(parent, bg=WHITE)
    frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
    widget = ttk.Treeview(frame, columns=[item[0] for item in columns], show="headings", height=height)
    for key, title, width in columns:
        widget.heading(key, text=title, anchor="w")
        widget.column(key, width=width, minwidth=55, anchor="w")
    widget.tag_configure("good", foreground=GREEN)
    widget.tag_configure("muted", foreground=MUTED)
    widget.tag_configure("warning", foreground="#BF7C23")
    widget.grid(row=0,column=0,sticky="nsew")
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
    scrollbar.grid(row=0,column=1,sticky="ns"); widget.configure(yscrollcommand=scrollbar.set)
    return frame, widget


def replace_rows(widget, rows):
    selected = widget.selection()
    position = widget.yview()[0]
    widget.delete(*widget.get_children())
    for item_id, values, tag in rows:
        widget.insert("", "end", iid=str(item_id), values=values, tags=(tag,) if tag else ())
    if selected and widget.exists(selected[0]):
        widget.selection_set(selected[0])
    widget.yview_moveto(position)


def setup_theme(root):
    root.configure(bg=BG)
    root.option_add("*Font", (FONT,10))
    style = ttk.Style(root); style.theme_use("clam")
    style.configure("TEntry", fieldbackground=WHITE, foreground=INK, bordercolor=BORDER, padding=(9,7))
    style.configure("TCombobox", fieldbackground=WHITE, background=WHITE, foreground=INK,
                    bordercolor=BORDER, padding=(9,7))
    style.map("TCombobox", foreground=[("disabled", "#A5AFC0"),("readonly",INK)],
              fieldbackground=[("readonly",WHITE)], selectbackground=[("readonly",WHITE)],
              selectforeground=[("readonly",INK)])
    style.configure("Treeview", background=WHITE, fieldbackground=WHITE, foreground=INK,
                    rowheight=34, borderwidth=0, font=(FONT,10))
    style.configure("Treeview.Heading", background="#F7F9FC", foreground=MUTED, relief="flat",
                    padding=(10,9), font=(FONT,9))
    style.map("Treeview", background=[("selected",PALE_BLUE)], foreground=[("selected",INK)])
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0,0,0,8))
    style.configure("TNotebook.Tab", padding=(24,13), font=(FONT,10), background=BG, foreground=MUTED)
    style.map("TNotebook.Tab", background=[("selected",WHITE)], foreground=[("selected",BLUE)],
              padding=[("selected",(24,13)),("!selected",(24,13))])
    style.configure("TProgressbar", background=BLUE, troughcolor=PALE_BLUE, borderwidth=0)


class CameraView(tk.Frame):
    def __init__(self, parent, height=420):
        super().__init__(parent, bg="#17243C", height=height)
        self.pack_propagate(False)
        self.picture = tk.Label(self, text="摄像头尚未启动", bg="#17243C", fg="#AEBAD0", font=(FONT,13))
        self.picture.pack(fill="both",expand=True)
        self.photo = None

    def render(self, data):
        if data:
            self.photo = tk.PhotoImage(data=base64.b64encode(data), format="png")
            self.picture.configure(image=self.photo, text="")

    def clear(self, text="摄像头已停止"):
        self.picture.configure(image="", text=text)
        self.photo = None


class BaseWindow(FlowHost):
    def __init__(self, root, title, subtitle, geometry="1120x820", async_io=True):
        gc.collect()  # 已销毁 Tk 对象在主线程回收，避免后台分配触发其 __del__。
        self.init_flows(root, async_io)
        self.root, self.busy = root, False
        self.closed = False
        self.jobs = queue.Queue()
        self.task_buttons = []
        self.after_ids = []
        self.job_control = None
        self.job_done = None
        root.report_callback_exception = self.callback_error
        setup_theme(root)
        root.title(title); root.geometry(geometry); root.minsize(980,740)
        root.protocol("WM_DELETE_WINDOW",self.close)
        header = tk.Frame(root,bg=BG); header.pack(fill="x",padx=28,pady=(22,18))
        mark = label(header,"◉",size=24,color=BLUE,bg=BG)
        mark.pack(side="left",padx=(0,14))
        titles = tk.Frame(header,bg=BG); titles.pack(side="left")
        label(titles,title,size=21,bold=True,bg=BG).pack(anchor="w")
        label(titles,subtitle,size=9,color=MUTED,bg=BG).pack(anchor="w",pady=(3,0))
        today = datetime.now()
        label(header,f"{today.year:04d}年{today.month:02d}月{today.day:02d}日",size=10,color=MUTED,bg=BG).pack(side="right")
        self.content = tk.Frame(root,bg=BG); self.content.pack(fill="both",expand=True,padx=28)
        self.status = tk.StringVar(value="准备就绪")
        footer = tk.Frame(root,bg=BG); footer.pack(fill="x",padx=28,pady=14)
        label(footer,"●",size=9,color=GREEN,bg=BG).pack(side="left",padx=(0,8))
        label(footer,textvariable=self.status,size=9,color=MUTED,bg=BG).pack(side="left")
        button(footer,"取消后台任务",self.cancel_job,plain=True).pack(side="right")
        self.schedule(100,self.poll_jobs)

    def schedule(self, milliseconds, callback):
        if self.closed:
            return None
        try:
            if not self.root.winfo_exists():
                return None
        except tk.TclError:
            return None
        token = self.root.after(milliseconds,callback)
        self.after_ids.append(token)
        if len(self.after_ids)>100:
            self.after_ids=self.after_ids[-50:]
        return token

    def say(self, message):
        self.status.set(message[:68]+("…" if len(message)>68 else ""))

    def handle_error(self, error):
        log_error("ui_operation_failed", error)
        messagebox.showerror("操作未完成", user_message(error), parent=self.root)

    def callback_error(self, kind, error, tb):
        log_error("tk_callback_failed", error.with_traceback(tb))
        self.say(user_message(error))

    def cancel_job(self):
        if self.job_control:
            self.job_control.cancelled.set()
            self.say("已请求取消；正在等待底层操作返回，期间可选择退出")
        else:
            self.say("当前没有可取消的后台任务")

    def run_job(self,title,work,done=None,timeout=TASK_TIMEOUT):
        if self.busy:
            self.say("请等待当前操作完成")
            return
        self.busy=True; self.say(f"正在{title}…")
        self.job_control = TaskControl(timeout)
        control = self.job_control
        self.job_done = done
        jobs=self.jobs
        for widget in self.task_buttons:
            widget.configure(state="disabled")
        def run():
            token = current_control.set(control)
            try:
                control.check()
                result = work()
                control.check()
                jobs.put((True,title,result,None,control))
            except Exception as error:
                log_error("background_job_failed", error)
                jobs.put((False,title,error,None,control))
            finally:
                current_control.reset(token)
        threading.Thread(target=run,daemon=True).start()

    def poll_jobs(self):
        try:
            while True:
                ok,title,result,done,control = self.jobs.get_nowait()
                if self.closed or control is not self.job_control:
                    continue
                done = done or self.job_done
                self.job_done=None
                self.busy=False
                self.job_control=None
                for widget in self.task_buttons:
                    widget.configure(state="normal")
                if ok:
                    self.say(f"{title}完成")
                    if done:
                        try:
                            done(result)
                        except Exception as error:
                            log_error("completion_callback_failed", error)
                            self.say(f"{title}已完成，但页面刷新失败")
                            messagebox.showerror("页面刷新失败",user_message(error),parent=self.root)
                else:
                    self.say(f"{title}未完成")
                    messagebox.showerror(f"{title}失败",user_message(result),parent=self.root)
        except queue.Empty:
            pass
        if self.busy and self.job_control:
            try:
                self.job_control.check()
            except (TaskCancelled, TaskTimeout) as error:
                self.job_control.cancelled.set()
                self.say(user_message(error))
        self.schedule(100,self.poll_jobs)

    def close(self):
        active = self.busy or bool(self._running_flows)
        if active and not messagebox.askyesno("退出当前操作",
                "当前操作尚未完成。是否请求取消并退出？\n已提交的数据会保留，未完成操作不会显示为成功。", parent=self.root):
            return False
        if self.job_control:
            self.job_control.cancelled.set()
        self.job_done=None
        self.cancel_flows()
        self.closed = True
        for token in self.after_ids:
            try:
                self.root.after_cancel(token)
            except tk.TclError:
                pass
        self.root.destroy()
        return True
