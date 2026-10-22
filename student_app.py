"""学生专用终端：当前课次、相机预览和本人签到结果。"""

import queue
import sqlite3
import time
import tkinter as tk
from datetime import datetime
from functools import partial
from task_runtime import IO, ui_flow
from app_errors import log_error, user_message

from attendance_store import AttendanceStore, STATUS_TEXT
from camera_service import CameraWorker
from train import model_health
from ui_common import BG, BLUE, GREEN, INK, MUTED, PALE_BLUE, WHITE, BaseWindow, CameraView, button, label, panel


class StudentApp(BaseWindow):
    def __init__(self,root,store=None,worker_factory=CameraWorker,async_io=True):
        self.store=store or AttendanceStore()
        self.worker_factory=worker_factory
        self.worker=None; self.worker_session_id=None; self.current_session=None; self.healthy=False; self.receipt_expires=0
        self.stop_requested=False
        self.session_refresh_error=None
        self.pending_receipts={}; self.receipt_retry_at=0
        super().__init__(root,"学生签到","请在老师开启课次后，依次面对摄像头签到",geometry="1100x800",async_io=async_io)
        self.content.columnconfigure(0,weight=1)
        self.content.columnconfigure(1,minsize=300)
        self.content.rowconfigure(0,weight=1)
        camera=panel(self.content); camera.grid(row=0,column=0,sticky="nsew",padx=(0,20))
        heading=tk.Frame(camera,bg=WHITE); heading.pack(fill="x",padx=20,pady=18)
        label(heading,"人脸签到",size=13,bold=True).pack(side="left")
        label(heading,"一次一人 · 请保持稳定",size=9,color=MUTED).pack(side="right")
        self.view=CameraView(camera); self.view.pack(fill="both",expand=True,padx=16,pady=(0,14))
        self.hint=tk.StringVar(value="老师开启签到后，点击右侧按钮开始")
        label(camera,textvariable=self.hint,size=10,color=MUTED,wraplength=610,justify="left").pack(fill="x",padx=20,pady=(0,22))
        info=panel(self.content); info.grid(row=0,column=1,sticky="nsew")
        inner=tk.Frame(info,bg=WHITE); inner.pack(fill="both",expand=True,padx=23,pady=24)
        label(inner,"当前课次",size=10,color=MUTED).pack(anchor="w")
        self.course=label(inner,"等待老师开启",size=20,bold=True,wraplength=260,justify="left"); self.course.pack(fill="x",pady=(12,8))
        self.details=label(inner,"暂无开放的签到课次",size=10,color=MUTED,wraplength=260,justify="left"); self.details.pack(fill="x",pady=(0,14))
        self.count=label(inner,"",size=12,color=BLUE,bold=True); self.count.pack(anchor="w",pady=(4,18))
        self.start_button=button(inner,"开始识别",self.toggle_camera,primary=True); self.start_button.pack(fill="x")
        label(inner,"开启后可连续为多位同学签到，\n无需每人重新点击。",size=9,color=MUTED,justify="left").pack(fill="x",pady=(12,25))
        receipt=panel(inner,bg=PALE_BLUE); receipt.pack(fill="both",expand=True)
        content=tk.Frame(receipt,bg=PALE_BLUE); content.pack(fill="both",expand=True,padx=18,pady=21)
        label(content,"本次识别结果",size=9,color=MUTED,bg=PALE_BLUE).pack(anchor="w")
        self.receipt_name=label(content,"尚未识别",size=22,bold=True,bg=PALE_BLUE); self.receipt_name.pack(anchor="w",pady=(15,8))
        self.receipt_detail=label(content,"签到成功后会在这里显示",size=10,color=MUTED,bg=PALE_BLUE,wraplength=230,justify="left"); self.receipt_detail.pack(fill="x")
        self.refresh_session(force=True)
        self.schedule(50,self.poll_camera)
        self.schedule(2000,self.tick_session)

    @ui_flow
    def refresh_session(self,force=False):
        previous_id=self.current_session["id"] if self.current_session else None
        try:
            current=yield IO(partial(self.store.active_session))
            current_id=current["id"] if current else None
            rows=(yield IO(partial(self.store.session_rows,current_id))) if current else []
            check_model=bool(current and (force or current_id!=previous_id))
            if check_model:
                active=yield IO(partial(self.store.students,active_only=True))
                healthy,message,_=yield IO(partial(model_health,{row["student_id"] for row in active}))
        except sqlite3.Error as error:
            log_error("student_refresh_failed",error)
            self.session_refresh_error=user_message(error)
            self.say(self.session_refresh_error)
            if not self.worker:
                self.start_button.configure(state="disabled",text="等待数据恢复")
            return False
        # 查询均成功后才更新课次，避免半更新的页面允许开启识别。
        if self.session_refresh_error is not None:
            self.say("课次数据已恢复")
            self.session_refresh_error=None
            self.receipt_retry_at=0
        if current_id!=previous_id:
            self.receipt_name.configure(text="尚未识别",fg=INK)
            self.receipt_detail.configure(text="签到成功后会在这里显示")
            self.receipt_expires=0
        if self.worker and (not current or current["id"]!=previous_id):
            self.worker.stop()
            self.start_button.configure(state="disabled",text="正在停止…")
        self.current_session=current
        if current:
            self.course.configure(text=current["course_name"])
            times=f"签到 {current['starts_at'][11:16]} — {current['ends_at'][11:16]}"
            if current['starts_at'][:10] != current['ends_at'][:10]:
                times=f"签到 {current['starts_at'][5:16].replace('T',' ')}\n至 {current['ends_at'][5:16].replace('T',' ')}"
            self.details.configure(text=f"{current['group_name']}\n\n{times}\n{current['late_at'][11:16]} 起计为迟到")
            arrived=sum(row["status"] in {"present","late"} for row in rows)
            self.count.configure(text=f"已到 {arrived} / 应到 {len(rows)}")
            # 检查在开始识别时再次执行；刷新页面不反复扫描整个样本目录。
            if check_model:
                self.healthy=healthy
                self.say(message if not self.healthy else "课次已开启，可以开始签到")
            if not self.worker:
                self.start_button.configure(state="normal",text="开始识别")
        else:
            self.course.configure(text="等待老师开启")
            self.details.configure(text="暂无开放的签到课次\n\n请等待老师在管理端开启签到。")
            self.count.configure(text="")
            if not self.worker:
                self.start_button.configure(state="disabled",text="等待课次开启")
                self.view.clear("等待老师开启签到")
        return True

    @ui_flow
    def toggle_camera(self):
        if self.worker:
            self.stop_requested=True
            self.worker.stop(); self.start_button.configure(state="disabled",text="正在停止…")
            self.say("正在停止识别，请稍候")
            return
        if not (yield from self.refresh_session(force=True)):
            return
        if not self.current_session:
            return
        if not self.healthy:
            self.hint.set("模型尚未就绪，请联系老师在管理端更新模型")
            return
        self.worker=self.worker_factory(self.store,"sign",session_id=self.current_session["id"])
        self.stop_requested=False
        self.worker_session_id=self.current_session["id"]
        self.worker.start(); self.start_button.configure(text="停止识别",state="normal")
        self.hint.set("正在连接摄像头…")
        self.say("识别进行中，请依次签到")

    def poll_camera(self):
        try:
            worker=self.worker
            stopped=False
            if worker:
                self.view.render(worker.take_frame())
                while True:
                    try:
                        kind,value=worker.events.get_nowait()
                    except queue.Empty:
                        break
                    if kind!="stopped" and (not self.current_session or self.current_session["id"]!=self.worker_session_id):
                        continue
                    if kind=="hint": self.hint.set(value)
                    elif kind=="signed":
                        # 先保留已入库的结果，查询失败时也继续消费停止事件。
                        self.pending_receipts[(value["session_id"],value["student_id"])]=value
                    elif kind=="error":
                        self.stop_requested=False
                        self.hint.set(value); self.say("识别已停止，请按提示处理后重试")
                    elif kind=="session_closed":
                        self.stop_requested=False
                        self.hint.set("本课次已结束，请等待下一课次")
                    elif kind=="stopped":
                        if self.stop_requested:
                            self.hint.set("摄像头已停止，需要时可重新开始识别")
                            self.say("摄像头已停止")
                        self.stop_requested=False
                        self.worker=None; self.worker_session_id=None
                        self.view.clear()
                        self.start_button.configure(state="disabled",text="正在刷新课次…")
                        stopped=True
            if stopped: self.refresh_session()
            if self.pending_receipts and time.monotonic()>=self.receipt_retry_at:
                self.refresh_receipts()
        finally:
            self.schedule(50,self.poll_camera)

    @ui_flow
    def refresh_receipts(self):
        if (yield from self.refresh_session()):
            receipts=list(self.pending_receipts.values())
            self.pending_receipts.clear()
            for receipt in receipts:
                if self.current_session and receipt["session_id"]==self.current_session["id"]:
                    self.show_receipt(receipt)
        else:
            self.receipt_retry_at=time.monotonic()+2

    def show_receipt(self,value):
        self.receipt_expires=time.monotonic()+10
        self.receipt_name.configure(text=(value["name"][:1]+"*" if value["name"] else "*"),fg=GREEN)
        student_id=value["student_id"]
        visible=min(2,max(0,len(student_id)-2))
        masked="*"*(len(student_id)-visible)+(student_id[-visible:] if visible else "")
        result="签到成功" if value["inserted"] else "本课次已有记录"
        clock=datetime.fromisoformat(value["signed_at"]).strftime("%H:%M:%S") if value["signed_at"] else "时间未确认" if value["status"] in {"present","late"} else ""
        self.receipt_detail.configure(text=f"{result} · {STATUS_TEXT[value['status']]}\n\n学号 {masked}\n{clock}")

    def tick_session(self):
        try:
            if self.receipt_expires and time.monotonic()>self.receipt_expires:
                self.receipt_name.configure(text="尚未识别",fg=INK)
                self.receipt_detail.configure(text="签到成功后会在这里显示")
                self.receipt_expires=0
            self.refresh_session()
        finally:
            self.schedule(2000,self.tick_session)

    def close(self):
        if self.worker: self.worker.stop()
        super().close()


def main():
    from app_startup import launch_entry
    launch_entry(lambda root,store:StudentApp(root,store))


if __name__=="__main__":
    main()
