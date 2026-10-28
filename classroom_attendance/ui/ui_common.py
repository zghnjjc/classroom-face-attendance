"""两个独立端共用的轻量 Tkinter 样式与相机预览。"""

import base64
import gc
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from classroom_attendance.core.app_config import TASK_TIMEOUT
from classroom_attendance.core.app_errors import TaskCancelled, TaskTimeout, log_error, user_message
from classroom_attendance.core.task_runtime import FlowHost, TaskControl, current_control

BG, WHITE, INK, MUTED = "#F4F6FA", "#FFFFFF", "#17243C", "#5F6F86"
BLUE, PALE_BLUE, BORDER, GREEN = "#3267E3", "#EDF3FF", "#E4E9F1", "#18876A"
FONT = "Microsoft YaHei UI"


def switch_view(root, current, factory):
    """构造下一页成功后才销毁当前页；失败时恢复可见页面及其窗口回调。"""
    children = set(root.winfo_children())
    timers = set(root.tk.call('after', 'info'))
    layout = current.pack_info()
    title, geometry, minimum = root.title(), root.geometry(), root.minsize()
    close_handler = root.protocol('WM_DELETE_WINDOW')
    return_handler = root.bind('<Return>')
    error_handler = getattr(root, 'report_callback_exception', None)
    focus = root.focus_get()
    grab = root.grab_current()
    current.pack_forget()
    try:
        application = factory()
    except Exception:
        for timer in set(root.tk.call('after', 'info')) - timers:
            root.after_cancel(timer)
        for child in set(root.winfo_children()) - children:
            child.destroy()
        root.title(title)
        root.minsize(*minimum)
        root.geometry(geometry)
        root.protocol('WM_DELETE_WINDOW', close_handler)
        root.bind('<Return>', return_handler or '')
        if error_handler is not None:
            root.report_callback_exception = error_handler
        else:
            root.__dict__.pop('report_callback_exception', None)
        current.pack(**layout)
        if grab is not None and grab.winfo_exists():
            grab.grab_set()
        if focus is not None and focus.winfo_exists():
            focus.focus_set()
        raise
    if current.winfo_exists():
        current.destroy()
    return application


def screen_workarea(window):
    """返回可用桌面范围；Windows 下排除任务栏。"""
    if os.name == "nt":
        import ctypes
        from ctypes.wintypes import RECT
        area = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(area), 0):
            return area.left, area.top, area.right, area.bottom
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def fit_window(window, geometry, minimum=(900, 560)):
    """按工作区限制初始尺寸和最小尺寸，并为标题栏留出空间。"""
    left, top, right, bottom = screen_workarea(window)
    available_width = max(1, right - left - 32)
    available_height = max(1, bottom - top - 64)
    desired_width, desired_height = map(int, geometry.split("x"))
    width = min(max(desired_width, minimum[0]), available_width)
    height = min(max(desired_height, minimum[1]), available_height)
    window.minsize(min(minimum[0], available_width), min(minimum[1], available_height))
    x = left + (right - left - width) // 2
    y = top + max(8, (bottom - top - height - 32) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


class ScrollableFrame(tk.Frame):
    """可滚动表单；滚轮只作用于指针所在面板，Tab 自动带出隐藏字段。"""
    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=WHITE, **kwargs)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, bg=WHITE, highlightthickness=0, width=1, height=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = tk.Frame(self.canvas, bg=WHITE)
        self._item = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._sync_region)
        self.canvas.bind("<Configure>", self._sync_width)
        self._window = self.winfo_toplevel()
        self._bindings = [
            ("<MouseWheel>", self._window.bind("<MouseWheel>", self._wheel, add="+")),
            ("<Button-4>", self._window.bind("<Button-4>", self._wheel, add="+")),
            ("<Button-5>", self._window.bind("<Button-5>", self._wheel, add="+")),
            ("<FocusIn>", self._window.bind("<FocusIn>", self._focus, add="+")),
        ]
        self.bind("<Destroy>", self._cleanup, add="+")

    def _contains(self, widget):
        while widget is not None:
            if widget is self:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _sync_region(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        # 首次布局时移除滚动条会使 Windows Tk 的画布保持未映射，短表单变为空白。
        # 保留滚动条的布局；内容较短时只将视图复位，由 Tk 显示不可滚动状态。
        self.scrollbar.grid()
        if self.body.winfo_reqheight() <= self.canvas.winfo_height():
            self.canvas.yview_moveto(0)

    def _sync_width(self, event):
        self.canvas.itemconfigure(self._item, width=event.width)
        self._sync_region()

    def _wheel(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root)
        if not self._contains(widget):
            return
        if widget.winfo_class() in {"Text", "Listbox", "Treeview", "TCombobox", "TSpinbox"}:
            return
        if self.body.winfo_reqheight() <= self.canvas.winfo_height():
            return
        delta = -1 if getattr(event, "num", None) == 4 else 1
        if getattr(event, "delta", 0):
            delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta * 3, "units")
        return "break"

    def _focus(self, event):
        widget = event.widget
        if not self._contains(widget) or widget in (self, self.canvas, self.body):
            return
        top = widget.winfo_rooty() - self.body.winfo_rooty()
        bottom = top + widget.winfo_height()
        visible_top = self.canvas.canvasy(0)
        visible_bottom = visible_top + self.canvas.winfo_height()
        target = top - 12 if top < visible_top else bottom - self.canvas.winfo_height() + 12
        if top < visible_top or bottom > visible_bottom:
            self.canvas.yview_moveto(max(0, target) / max(1, self.body.winfo_height()))

    def _cleanup(self, event):
        if event.widget is self:
            for sequence, identifier in self._bindings:
                try:
                    self._window.unbind(sequence, identifier)
                except tk.TclError:
                    pass


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


def tree(parent, columns, height=8, empty_text="暂无记录"):
    frame = tk.Frame(parent, bg=WHITE)
    frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
    widget = ttk.Treeview(frame, columns=[item[0] for item in columns], show="headings", height=height)
    for key, title, width in columns:
        widget.heading(key, text=title, anchor="w")
        widget.column(key, width=width, minwidth=width, anchor="w")
    widget.tag_configure("good", foreground=GREEN)
    widget.tag_configure("muted", foreground=MUTED)
    widget.tag_configure("warning", foreground="#BF7C23")
    widget.grid(row=0,column=0,sticky="nsew")
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
    scrollbar.grid(row=0,column=1,sticky="ns"); widget.configure(yscrollcommand=scrollbar.set)
    horizontal = ttk.Scrollbar(frame, orient="horizontal", command=widget.xview)
    horizontal.grid(row=1, column=0, sticky="ew")
    widget.configure(xscrollcommand=horizontal.set)
    widget.empty_label = label(widget, empty_text, size=10, color=MUTED, justify="center")
    widget.empty_label.place(relx=.5, rely=.5, anchor="center")
    return frame, widget


def replace_rows(widget, rows):
    selected = widget.selection()
    position = widget.yview()[0]
    widget.delete(*widget.get_children())
    for item_id, values, tag in rows:
        widget.insert("", "end", iid=str(item_id), values=values, tags=(tag,) if tag else ())
    if hasattr(widget, "empty_label"):
        if widget.get_children():
            widget.empty_label.place_forget()
        else:
            widget.empty_label.place(relx=.5, rely=.5, anchor="center")
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
            width, height = self.picture.winfo_width(), self.picture.winfo_height()
            if width > 1 and height > 1 and (self.photo.width() > width or self.photo.height() > height):
                import cv2
                import numpy as np
                scale = min(width / self.photo.width(), height / self.photo.height())
                size = (max(1, int(self.photo.width() * scale)), max(1, int(self.photo.height() * scale)))
                frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    success, encoded = cv2.imencode(".png", cv2.resize(frame, size, interpolation=cv2.INTER_AREA))
                    if success:
                        self.photo = tk.PhotoImage(data=base64.b64encode(encoded.tobytes()), format="png")
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
        self.job_error = None
        self.job_parent = None
        self.job_started = None
        self.job_title = ""
        self.job_interrupted = None
        root.report_callback_exception = self.callback_error
        setup_theme(root)
        root.title(title)
        fit_window(root, geometry)
        root.protocol("WM_DELETE_WINDOW",self.close)
        header = tk.Frame(root,bg=BG); header.pack(fill="x",padx=22,pady=(14,12))
        mark = label(header,"◉",size=24,color=BLUE,bg=BG)
        mark.pack(side="left",padx=(0,14))
        titles = tk.Frame(header,bg=BG); titles.pack(side="left")
        label(titles,title,size=21,bold=True,bg=BG).pack(anchor="w")
        label(titles,subtitle,size=9,color=MUTED,bg=BG).pack(anchor="w",pady=(3,0))
        today = datetime.now()
        label(header,f"{today.year:04d}年{today.month:02d}月{today.day:02d}日",size=10,color=MUTED,bg=BG).pack(side="right")
        self.content = tk.Frame(root,bg=BG); self.content.pack(fill="both",expand=True,padx=22)
        self.status = tk.StringVar(value="准备就绪")
        footer = tk.Frame(root,bg=BG); footer.pack(side="bottom",fill="x",padx=22,pady=8,before=self.content)
        footer.columnconfigure(1, weight=1)
        self.status_mark = label(footer,"●",size=9,color=GREEN,bg=BG)
        self.status_mark.grid(row=0, column=0, padx=(0,8))
        self.status_label = label(footer,textvariable=self.status,size=9,color=MUTED,bg=BG,
                                  height=2, justify="left", wraplength=600, cursor="hand2", takefocus=1)
        self.status_label.grid(row=0, column=1, sticky="ew")
        self.status_label.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(1,event.width)))
        self.status_label.bind("<Button-1>", self.show_status)
        self.status_label.bind("<Return>", self.show_status)
        self.status_label.bind("<space>", self.show_status)
        self.task_box = tk.Frame(footer, bg=BG)
        self.task_box.grid(row=0, column=2, padx=12)
        self.task_state = tk.StringVar()
        label(self.task_box, textvariable=self.task_state, size=8, color=MUTED, bg=BG).pack()
        self.task_progress = ttk.Progressbar(self.task_box, mode="indeterminate", length=100)
        self.task_progress.pack(fill="x", pady=(3,0))
        self.task_box.grid_remove()
        self.cancel_button = button(footer,"取消任务",self.cancel_job,plain=True)
        self.cancel_button.grid(row=0, column=3)
        self.cancel_button.configure(state="disabled")
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
        self.status.set(message)

    def show_status(self, event=None):
        messagebox.showinfo("完整操作提示", self.status.get(), parent=self.message_parent())

    def message_parent(self, preferred=None):
        try:
            current = self.root.grab_current()
            if current is not None and current.winfo_exists():
                return current.winfo_toplevel()
            if preferred is not None and preferred.winfo_exists():
                return preferred
        except tk.TclError:
            pass
        return self.root

    def handle_error(self, error):
        log_error("ui_operation_failed", error)
        messagebox.showerror("操作未完成", user_message(error), parent=self.message_parent())

    def callback_error(self, kind, error, tb):
        log_error("tk_callback_failed", error.with_traceback(tb))
        self.say(user_message(error))

    def cancel_job(self):
        if self.job_control:
            self.job_control.cancelled.set()
            self.job_interrupted = self.job_interrupted or TaskCancelled()
            self.cancel_button.configure(state="disabled", text="正在取消…")
            self.say("已请求取消，正在等待当前步骤结束…")
        else:
            self.say("当前没有可取消的后台任务")

    def run_job(self,title,work,done=None,timeout=TASK_TIMEOUT, *, on_error=None, parent=None):
        if self.busy:
            self.say("请等待当前操作完成")
            return
        self.busy=True; self.say(f"正在{title}…")
        self.job_started = time.monotonic()
        self.job_title = title
        self.job_interrupted = None
        self.task_state.set(f"{title} · 0 秒")
        self.task_box.grid()
        self.task_progress.configure(value=0)
        self.status_mark.configure(fg=BLUE)
        self.cancel_button.configure(state="normal", text="取消任务")
        self.job_control = TaskControl(timeout)
        control = self.job_control
        self.job_done = done
        self.job_error = on_error
        self.job_parent = parent
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
            self._poll_jobs()
        finally:
            # 消息框/完成回调出错或进入嵌套事件循环后仍保留后续轮询。
            self.schedule(100, self.poll_jobs)

    def _poll_jobs(self):
        try:
            while True:
                ok,title,result,done,control = self.jobs.get_nowait()
                if self.closed or control is not self.job_control:
                    continue
                done = done or self.job_done
                on_error, parent = self.job_error, self.job_parent
                self.job_done=None
                self.job_error = self.job_parent = None
                self.busy=False
                self.job_control=None
                interrupted = self.job_interrupted
                self.job_interrupted = None
                self.task_progress.configure(value=0)
                self.task_box.grid_remove()
                self.cancel_button.configure(state="disabled", text="取消任务")
                self.status_mark.configure(fg=GREEN)
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
                            messagebox.showerror("页面刷新失败",user_message(error),parent=self.message_parent(parent))
                else:
                    result = interrupted or result
                    if isinstance(result, TaskCancelled):
                        self.say(f"{title}已取消")
                    else:
                        self.say(f"{title}未完成：{user_message(result)}")
                    if on_error:
                        on_error(result)
                    elif not isinstance(result, TaskCancelled):
                        messagebox.showerror(f"{title}失败",user_message(result),parent=self.message_parent(parent))
        except queue.Empty:
            pass
        if self.busy and self.job_control:
            self.task_progress.step(7)
            elapsed = int(time.monotonic() - self.job_started)
            self.task_state.set(f"{self.job_title} · {elapsed} 秒")
            try:
                self.job_control.check()
            except (TaskCancelled, TaskTimeout) as error:
                self.job_control.cancelled.set()
                if self.job_interrupted is None:
                    self.job_interrupted = error
                    self.cancel_button.configure(state="disabled", text="正在停止…")
                    self.say(f"{user_message(error)}，正在等待当前步骤结束…")

    def close(self):
        active = self.busy or bool(self._running_flows)
        if active and not messagebox.askyesno("退出当前操作",
                "当前操作尚未完成。是否请求取消并退出？\n已提交的数据会保留，未完成操作不会显示为成功。", parent=self.message_parent()):
            return False
        if self.job_control:
            self.job_control.cancelled.set()
        self.job_done=None
        self.job_error = self.job_parent = None
        self.cancel_flows()
        self.closed = True
        for token in self.after_ids:
            try:
                self.root.after_cancel(token)
            except tk.TclError:
                pass
        self.root.destroy()
        return True
