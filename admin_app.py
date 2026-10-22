"""教师管理端。登录后才展示学生、采集训练、课次和考勤管理。"""
import json
import queue
import sqlite3
import tkinter as tk
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from math import ceil
from tkinter import filedialog, messagebox, ttk
from functools import partial
from task_runtime import IO, ui_flow, FlowHost
from app_errors import log_error, user_message
from face_privacy import delete_face_data
from app_config import BASE_DIR, DATA_LOCK_PATH, sample_paths, sample_student_id
from attendance_store import AttendanceStore, STATUS_TEXT, read_student_import
from attendance_filters import PERIODS, record_date_range
from camera_service import CameraWorker
from GUI import launch_script
from resource_lock import ResourceLease
from train import model_health, train_model
from ui_common import BG, BLUE, GREEN, INK, MUTED, WHITE, BaseWindow, CameraView, button, entry_field, label, panel, replace_rows, setup_theme, tree
STATE_TEXT = {'draft': '待开启', 'open': '签到中', 'closed': '已结束', 'cancelled': '已取消'}

class CaptureDialog:

    def __init__(self, parent, store, student, on_done):
        self.window = tk.Toplevel(parent)
        self.window.title('采集人脸 · ' + student['name'])
        self.window.geometry('760x740')
        self.window.minsize(720, 680)
        self.window.configure(bg=WHITE)
        self.window.transient(parent)
        self.window.grab_set()
        self.window.protocol('WM_DELETE_WINDOW', self.stop)
        self.on_done = on_done
        self.closing = False
        label(self.window, f"为 {student['name']} 采集人脸", size=18, bold=True).pack(anchor='w', padx=24, pady=(22, 5))
        label(self.window, f"学号 {student['student_id']} · 自动采集 20 张，请缓慢转头、改变表情", size=9, color=MUTED).pack(anchor='w', padx=24, pady=(0, 15))
        self.view = CameraView(self.window)
        self.view.pack(fill='both', expand=True, padx=24)
        self.hint = tk.StringVar(value='正在连接摄像头…')
        label(self.window, textvariable=self.hint, color=MUTED, wraplength=700).pack(anchor='w', padx=24, pady=14)
        self.progress = ttk.Progressbar(self.window, maximum=20)
        self.progress.pack(fill='x', padx=24)
        self.stop_button = button(self.window, '结束采集', self.stop)
        self.stop_button.pack(anchor='e', padx=24, pady=18)
        self.worker = CameraWorker(store, 'capture', student_id=student['student_id'])
        self.worker.start()
        self.window.after(50, self.poll)

    def stop(self):
        self.closing = True
        self.worker.stop()
        self.stop_button.configure(state='disabled', text='正在结束…')

    def poll(self):
        self.view.render(self.worker.take_frame())
        try:
            while True:
                kind, value = self.worker.events.get_nowait()
                if kind == 'hint':
                    self.hint.set(value)
                elif kind == 'progress':
                    self.progress['value'] = value[0]
                elif kind == 'error':
                    messagebox.showerror('采集未完成', value, parent=self.window)
                elif kind == 'stopped':
                    self.window.grab_release()
                    self.window.destroy()
                    self.on_done(value)
                    return
        except queue.Empty:
            pass
        self.window.after(50, self.poll)

class AdminApp(BaseWindow):

    def __init__(self, root, store=None, authenticated=False, async_io=True):
        if not authenticated:
            raise PermissionError('请先通过管理员登录。')
        self.store = store or AttendanceStore()
        self.sessions_data = []
        self.record_lookup = {}
        self.students_data = []
        self.selected_student = None
        self.capture_dialog = None
        self.refresh_error = False
        super().__init__(root, '教师管理', '学生档案、课次安排与考勤记录', geometry='1180x860',async_io=async_io)
        self.root.minsize(1080, 840)
        self.notebook = ttk.Notebook(self.content)
        self.notebook.pack(fill='both', expand=True)
        self.pages = {}
        for key, title in [('students', '学生与人脸'), ('sessions', '课次管理'), ('records', '考勤记录'), ('settings', '数据与设置')]:
            frame = tk.Frame(self.notebook, bg=WHITE, padx=20, pady=18)
            self.notebook.add(frame, text=title)
            self.pages[key] = frame
        self.build_students()
        self.build_sessions()
        self.build_records()
        self.build_settings()
        self.refresh_students()
        self.refresh_sessions()
        self.load_camera_setting()
        self.schedule(3000, self.tick)

    def guarded(self, work):
        try:
            return work()
        except Exception as error:
            self.handle_error(error)
            return None

    def refresh_failed(self,error):
        log_error('admin_refresh_failed',error)
        self.refresh_error=True
        self.say(user_message(error))

    def build_students(self):
        page = self.pages['students']
        page.columnconfigure(0, weight=1)
        page.columnconfigure(1, minsize=300)
        page.rowconfigure(1, weight=1)
        toolbar = tk.Frame(page, bg=WHITE)
        toolbar.grid(row=0, column=0, sticky='ew', padx=(0, 20), pady=(0, 14))
        label(toolbar, '班级学生名单', size=13, bold=True).pack(side='left')
        button(toolbar, '导入名单', self.import_roster).pack(side='right')
        button(toolbar, '下载模板', self.roster_template, plain=True).pack(side='right', padx=8)
        frame, self.students_tree = tree(page, [('id', '学号', 130), ('name', '姓名', 90), ('group', '班级', 90), ('samples', '样本', 55), ('active', '状态', 60),('coverage','人脸覆盖',90)])
        frame.grid(row=1, column=0, sticky='nsew', padx=(0, 20))
        self.students_tree.bind('<<TreeviewSelect>>', self.select_student)
        form = tk.Frame(page, bg=WHITE)
        form.grid(row=0, column=1, rowspan=2, sticky='nsew')
        form.columnconfigure(0, weight=1)
        label(form, '学生信息', size=13, bold=True).grid(row=0, column=0, sticky='w', pady=(0, 3))
        self.student_id = tk.StringVar()
        self.student_name = tk.StringVar()
        self.student_group = tk.StringVar(value='未分班')
        self.id_entry = entry_field(form, '学号', self.student_id, 1)
        pair = tk.Frame(form, bg=WHITE)
        pair.grid(row=3, column=0, rowspan=3, sticky='ew', pady=(12, 0))
        pair.columnconfigure((0, 1), weight=1)
        label(pair, '姓名', size=9, color=MUTED).grid(row=0, column=0, sticky='w', pady=(0, 5))
        label(pair, '班级', size=9, color=MUTED).grid(row=0, column=1, sticky='w', padx=(10, 0), pady=(0, 5))
        ttk.Entry(pair, textvariable=self.student_name, width=12).grid(row=1, column=0, sticky='ew')
        ttk.Entry(pair, textvariable=self.student_group, width=12).grid(row=1, column=1, sticky='ew', padx=(10, 0))
        buttons = tk.Frame(form, bg=WHITE)
        buttons.grid(row=7, column=0, sticky='ew', pady=(17, 10))
        button(buttons, '保存学生', self.save_student, primary=True).pack(side='left')
        button(buttons, '新建', self.new_student, plain=True).pack(side='right')
        self.active_button = button(form, '停用 / 启用选中学生', self.toggle_active, plain=True)
        self.active_button.grid(row=8, column=0, sticky='ew', pady=(0, 16))
        ttk.Separator(form).grid(row=9, column=0, sticky='ew', pady=(0, 14))
        label(form, '人脸资料', size=13, bold=True).grid(row=10, column=0, sticky='w')
        capture = button(form, '采集人脸（20 张）', self.capture_faces)
        capture.grid(row=11, column=0, sticky='ew', pady=(12, 10))
        training = button(form, '更新识别模型', self.train_faces)
        training.grid(row=12, column=0, sticky='ew')
        self.task_buttons.extend([capture, training])
        self.model_label = label(form, '', size=9, color=MUTED, wraplength=290, justify='left')
        self.model_label.grid(row=13, column=0, sticky='ew', pady=(13, 0))
        label(page, '停用仅阻止后续刷脸及默认入选新课次；既有应到名单请在课次管理中单独调整。', size=9, color=MUTED).grid(row=2, column=0, columnspan=2, sticky='w', pady=(14, 0))

    @ui_flow
    def refresh_students(self):
        counts = Counter()
        for file in (yield IO(partial(sample_paths))):
            try:
                counts[sample_student_id(file)] += 1
            except ValueError:
                pass
        self.students_data = (yield IO(partial(self.store.students)))
        groups = sorted({row['group_name'] for row in self.students_data if row['active']})
        self.session_group_combo.configure(values=['全部学生'] + groups)
        healthy, message, manifest = (yield IO(partial(model_health, {row['student_id'] for row in self.students_data if row['active']})))
        trained = set(manifest['student_ids']) if manifest else set()
        active_ids={row['student_id'] for row in self.students_data if row['active']}
        missing = sum((row['active'] and row['student_id'] not in trained for row in self.students_data))
        replace_rows(self.students_tree, [(row['student_id'], (row['student_id'], row['name'], row['group_name'], counts[row['student_id']], '启用' if row['active'] else '停用',
            '停用' if not row['active'] else '已训练' if healthy and row['student_id'] in trained else '需训练'), '' if row['active'] else 'muted') for row in self.students_data])
        self.model_label.configure(text=f'{message}\n启用学生已覆盖 {len(trained & active_ids)} 人，{missing} 位启用学生尚无已训练的人脸。', fg=GREEN if healthy else MUTED)
        if missing:
            names = [row['name'] for row in self.students_data if row['active'] and row['student_id'] not in trained]
            self.model_label.configure(text=self.model_label.cget('text')+'\n需训练或人工补签：'+'、'.join(names))

    def select_student(self, event=None):
        selection = self.students_tree.selection()
        if selection:
            student = next((row for row in self.students_data if row['student_id'] == selection[0]), None)
            if student:
                self.selected_student = student
                self.student_id.set(student['student_id'])
                self.student_name.set(student['name'])
                self.student_group.set(student['group_name'])
                self.id_entry.configure(state='readonly')

    def new_student(self):
        self.selected_student = None
        self.students_tree.selection_remove(*self.students_tree.selection())
        self.id_entry.configure(state='normal')
        self.student_id.set('')
        self.student_name.set('')
        self.student_group.set('未分班')
        self.id_entry.focus_set()

    @ui_flow
    def save_student(self):

        @self.flow
        def save():
            yield IO(partial(self.store.save_student, self.student_id.get(), self.student_name.get(), self.student_group.get()))
            sid = self.student_id.get().strip()
            yield from self.refresh_students()
            self.students_tree.selection_set(sid)
            self.say('学生信息已保存，历史课次名单保持原样')
        yield from save()

    @ui_flow
    def toggle_active(self):
        if not self.selected_student:
            self.say('请先选择学生')
            return
        row = self.selected_student

        @self.flow
        def change():
            yield IO(partial(self.store.set_student_active, row['student_id'], not row['active']))
            yield from self.refresh_students()
            self.say('学生状态已更新；如模型提示待更新，请重新训练')
        yield from change()

    def capture_faces(self):
        if not self.selected_student or not self.selected_student['active']:
            self.say('请先保存并选择一位启用的学生')
            return
        if self.capture_dialog and self.capture_dialog.window.winfo_exists():
            return
        self.capture_dialog = CaptureDialog(self.root, self.store, self.selected_student, self.capture_done)

    @ui_flow
    def capture_done(self, count):
        self.capture_dialog = None
        yield from self.refresh_students()
        self.say(f'新增 {count} 张样本，请点击“更新识别模型”')

    @ui_flow
    def train_faces(self):
        ids = {row['student_id'] for row in (yield IO(partial(self.store.students, active_only=True)))}
        self.run_job('更新识别模型', lambda: train_model(allowed_ids=ids), lambda result: self.refresh_students())

    @ui_flow
    def roster_template(self):
        path = filedialog.asksaveasfilename(parent=self.root, title='保存名单模板', initialfile='学生名单模板.csv', defaultextension='.csv', filetypes=[('CSV 名单', '*.csv')])
        if path:

            @self.flow
            def save():
                yield IO(partial(Path(path).write_text, '学号,姓名,班级\n', encoding='utf-8-sig'))
                self.say('模板已保存，请按列填写；长学号请按文本保存')
            yield from save()

    @ui_flow
    def import_roster(self):
        path = filedialog.askopenfilename(parent=self.root, title='导入学生名单', filetypes=[('学生名单', '*.xlsx *.csv')])
        if not path:
            return

        @self.flow
        def load():
            rows = (yield IO(partial(read_student_import, path)))
            if messagebox.askyesno('确认导入', f'将导入 {len(rows)} 名学生，已有学号会更新姓名和班级。\n不会清空原名单与历史记录。', parent=self.root):
                yield IO(partial(self.store.import_students, rows))
                yield from self.refresh_students()
                self.say(f'已导入 {len(rows)} 名学生')
        yield from load()

    def build_sessions(self):
        page = self.pages['sessions']
        page.columnconfigure(1, weight=1)
        page.rowconfigure(0, weight=1)
        form = tk.Frame(page, bg=WHITE)
        form.grid(row=0, column=0, sticky='nsw', padx=(0, 24))
        form.columnconfigure(0, weight=1)
        label(form, '创建课次', size=14, bold=True).grid(row=0, column=0, sticky='w', pady=(0, 4))
        self.course = tk.StringVar()
        self.session_group = tk.StringVar(value='全部学生')
        now = datetime.now().replace(second=0, microsecond=0)
        self.starts = tk.StringVar(value=now.strftime('%Y-%m-%d %H:%M'))
        self.ends = tk.StringVar(value=(now + timedelta(minutes=90)).strftime('%Y-%m-%d %H:%M'))
        self.grace = tk.StringVar(value='10')
        entry_field(form, '课程名称', self.course, 1, 27)
        label(form, '班级', size=9, color=MUTED).grid(row=3, column=0, sticky='w', pady=(10, 5))
        self.session_group_combo = ttk.Combobox(form, textvariable=self.session_group, state='readonly', width=25)
        self.session_group_combo.grid(row=4, column=0, sticky='ew')
        entry_field(form, '签到开始（年-月-日 时:分）', self.starts, 5, 27)
        entry_field(form, '签到结束（年-月-日 时:分）', self.ends, 7, 27)
        label(form, '迟到宽限（分钟）', size=9, color=MUTED).grid(row=9, column=0, sticky='w', pady=(10, 5))
        ttk.Spinbox(form, textvariable=self.grace, from_=0, to=180, width=25).grid(row=10, column=0, sticky='ew')
        button(form, '创建课次', self.create_session, primary=True).grid(row=11, column=0, sticky='ew', pady=(20, 16))
        label(form, '创建后在右侧选中课次，再开启签到。\n同一时刻仅开放一个课次。', size=9, color=MUTED, justify='left', wraplength=270).grid(row=12, column=0, sticky='w')
        right = tk.Frame(page, bg=WHITE)
        right.grid(row=0, column=1, sticky='nsew')
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)
        toolbar = tk.Frame(right, bg=WHITE)
        toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 14))
        self.open_session_button = button(toolbar, '开启签到', self.open_session, primary=True)
        self.open_session_button.pack(side='left')
        self.close_session_button = button(toolbar, '结束签到', self.close_session)
        self.close_session_button.pack(side='left', padx=10)
        self.cancel_session_button = button(toolbar, '取消课次', self.cancel_session, plain=True)
        self.cancel_session_button.pack(side='left')
        button(toolbar, '打开学生端', lambda: launch_script('student_app.py'), plain=True).pack(side='right')
        actions = tk.Frame(right, bg=WHITE)
        actions.grid(row=1, column=0, sticky='ew', pady=(0, 12))
        self.roster_button = button(actions, '查看 / 调整应到名单', self.manage_roster)
        self.roster_button.pack(side='left')
        self.session_detail_button = button(actions, '课次详情 / 取消原因', self.show_session_detail, plain=True)
        self.session_detail_button.pack(side='left', padx=10)
        frame, self.sessions_tree = tree(right, [('course', '课程 / 班级', 180), ('time', '开始时间', 155), ('state', '状态', 80), ('count', '到课', 70)])
        frame.grid(row=2, column=0, sticky='nsew')
        self.sessions_tree.bind('<<TreeviewSelect>>', self.sync_session_actions)
        label(right, '待开启或已取消课次不计考勤；只有已结束课次的未签到者计缺勤。', size=9, color=MUTED, wraplength=660).grid(row=3, column=0, sticky='w', pady=(14, 0))

    @ui_flow
    def create_session(self):

        @self.flow
        def create():
            sid = (yield IO(partial(self.store.create_session, self.course.get(), self.session_group.get(), datetime.strptime(self.starts.get().strip(), '%Y-%m-%d %H:%M'), datetime.strptime(self.ends.get().strip(), '%Y-%m-%d %H:%M'), int(self.grace.get()))))
            yield from self.refresh_sessions()
            self.sessions_tree.selection_set(str(sid))
            self.say('课次已创建，确认时间后点击“开启签到”')
        yield from create()

    def selected_session_id(self):
        selection = self.sessions_tree.selection()
        if not selection:
            raise ValueError('请先在右侧选择课次。')
        return int(selection[0])

    @ui_flow
    def open_session(self):

        @self.flow
        def start():
            sid = self.selected_session_id()
            ids = {row['student_id'] for row in (yield IO(partial(self.store.students, active_only=True)))}
            healthy, message, manifest = (yield IO(partial(model_health, ids)))
            if not healthy:
                raise ValueError(message)
            missing = [row for row in (yield IO(partial(self.store.session_rows, sid))) if row['student_id'] not in set(manifest['student_ids'])]
            if missing and (not messagebox.askyesno('有学生尚无人脸模型', f'本课次有 {len(missing)} 位学生尚未训练人脸，需由老师补签。\n仍要开启本课次吗？', parent=self.root)):
                return
            yield IO(partial(self.store.open_session, sid))
            yield from self.refresh_sessions()
            self.say('签到已开启，学生端会自动显示当前课次')
        yield from start()

    @ui_flow
    def close_session(self):

        @self.flow
        def stop():
            sid = self.selected_session_id()
            if (yield IO(partial(self.store.session, sid)))['state'] != 'open':
                raise ValueError('只能结束签到中的课次；待开启课次请使用取消。')
            if messagebox.askyesno('结束签到', '结束后学生端将停止本课次签到，未签到学生计为缺勤。', parent=self.root):
                yield IO(partial(self.store.close_session, sid))
                yield from self.refresh_sessions()
                self.say('本课次签到已结束')
        yield from stop()

    def sync_session_actions(self, event=None):
        selected = self.sessions_tree.selection()
        item = next((row for row in self.sessions_data if selected and str(row['id']) == selected[0]), None)
        state = item['state'] if item else None
        for widget, allowed in [(self.open_session_button, state == 'draft'), (self.close_session_button, state == 'open'), (self.cancel_session_button, state == 'draft'), (self.roster_button, state in {'draft', 'open', 'closed'}), (self.session_detail_button, item is not None)]:
            widget.configure(state='normal' if allowed else 'disabled')

    @ui_flow
    def cancel_session(self):

        @self.flow
        def show():
            sid = self.selected_session_id()
            session = (yield IO(partial(self.store.session, sid)))
            if session['state'] != 'draft':
                raise ValueError('只能取消待开启课次。')
            dialog = tk.Toplevel(self.root)
            dialog.title('取消课次')
            dialog.geometry('570x440')
            dialog.configure(bg=WHITE)
            dialog.transient(self.root)
            dialog.grab_set()
            label(dialog, f"取消 {session['course_name']}", size=16, bold=True).pack(anchor='w', padx=24, pady=(22, 12))
            label(dialog, '取消后保留课次和名单，不计入应到或缺勤。', color=MUTED).pack(anchor='w', padx=24)
            form = tk.Frame(dialog, bg=WHITE)
            form.pack(fill='x', padx=24, pady=10)
            form.columnconfigure(0, weight=1)
            reason = tk.StringVar()
            actor = tk.StringVar()
            entry_field(form, '取消原因（必填）', reason, 0)
            entry_field(form, '操作署名（选填，未经账号认证）', actor, 2)

            @self.flow
            def save():
                try:
                    yield IO(partial(self.store.cancel_session, sid, reason.get(), actor.get()))
                except Exception as error:
                    messagebox.showerror('取消未完成', user_message(error), parent=dialog)
                    return
                dialog.destroy()
                self.say('课次已取消，不计入考勤')
                yield from self.refresh_sessions()
            button(dialog, '确认取消', save, primary=True).pack(anchor='e', padx=24, pady=18)
        yield from show()

    def text_details(self, title, text):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry('900x640')
        body = tk.Text(dialog, wrap='word', font=('Microsoft YaHei UI', 10), padx=16, pady=16)
        scroll = ttk.Scrollbar(dialog, command=body.yview)
        scroll.pack(side='right', fill='y')
        body.configure(yscrollcommand=scroll.set)
        body.pack(fill='both', expand=True)
        body.insert('1.0', text)
        body.configure(state='disabled')
        return dialog

    @ui_flow
    def show_session_detail(self):

        @self.flow
        def show():
            sid = self.selected_session_id()
            rows = (yield IO(partial(self.store.session_rows, sid, include_removed=True)))
            session = (yield IO(partial(self.store.session, sid)))
            content = [f"课次 #{sid}  {session['course_name']} / {session['group_name']}", f"状态：{STATE_TEXT[session['state']]}", f"时段：{session['starts_at']} 至 {session['ends_at']}"]
            if session['state'] == 'cancelled':
                content.extend([f"取消时间：{session['cancelled_at']}", f"取消原因：{session['cancel_reason']}", '该课次不参与考勤统计。'])
            content.append('\n保留名单（含已移出人员）：')
            content.extend((f"{r['student_id']}  {r['name']}  {r['group_name']}  {STATUS_TEXT[r['status']]}" for r in rows))
            content.append('\n课次与名单操作记录：')
            for item in (yield IO(partial(self.store.audit_rows, sid))):
                if item['action'] in {'取消课次', '调整课次名单', '结束签到', '开启签到', '课次到时关闭'}:
                    content.append(f"{item['created_at']}  {item['action']}  {self.actor_text(item)}\n{self.audit_detail(item)}")
                    if item['before_json']:
                        content.append(f"调整前：\n{self.snapshot_text(item['before_json'])}\n调整后：\n{self.snapshot_text(item['after_json'])}")
            self.text_details('课次详情 / 取消原因', '\n'.join(content))
        yield from show()

    @staticmethod
    def actor_text(item):
        return {'management_signature': f"管理署名：{item['actor_label']}（未认证）", 'management_entry': '管理入口（未署名）', 'system': '系统自动', 'face_recognition': '人脸识别', 'legacy_unknown': '旧版记录（身份未知）'}.get(item['actor_kind'], item['actor_kind'])

    @staticmethod
    def snapshot_text(raw):
        if not raw:
            return '旧版未保存完整原值'
        value = json.loads(raw)
        names = {'session_id': '课次编号', 'student_id': '学号', 'name': '姓名', 'group_name': '班级', 'status': '状态', 'signed_at': '实际到课时间', 'method': '记录方式', 'note': '备注', 'included': '计入应到', 'id': '课次编号', 'course_name': '课程', 'starts_at': '开始时间', 'late_at': '迟到起点', 'ends_at': '结束时间', 'state': '课次状态', 'created_at': '创建时间', 'cancel_reason': '取消原因', 'cancelled_at': '取消时间'}
        methods = {'face': '人脸识别', 'manual': '人工更正', 'legacy': '历史迁移'}

        def fields(row):
            output = []
            for key, item in row.items():
                if key == 'status':
                    item = STATUS_TEXT.get(item, item)
                elif key == 'state':
                    item = STATE_TEXT.get(item, item)
                elif key == 'method':
                    item = methods.get(item, item)
                elif key == 'included':
                    item = '是' if item else '否'
                elif key == 'signed_at' and item is None and (row.get('status') in {'present', 'late'}):
                    item = '时间未确认'
                if item is None or item == '':
                    item = '无'
                output.append(f'{names.get(key, key)}：{item}')
            return '；'.join(output)
        return '\n'.join((fields(row) for row in value)) if isinstance(value, list) else fields(value)

    @staticmethod
    def audit_detail(item):
        detail = item['detail']
        if item['action'] == '人工更正' and '；原因：' in detail:
            transition, reason = detail.split('；原因：', 1)
            states = transition.split(' → ')
            return ' → '.join((STATUS_TEXT.get(state, state) for state in states)) + f'；原因：{reason}'
        return detail

    @ui_flow
    def manage_roster(self):

        @self.flow
        def show():
            sid = self.selected_session_id()
            session = (yield IO(partial(self.store.session, sid)))
            if session['state'] == 'cancelled':
                raise ValueError('已取消课次只能查询。')
            dialog = tk.Toplevel(self.root)
            dialog.title('课次应到名单')
            dialog.geometry('1080x760')
            dialog.minsize(920, 700)
            dialog.configure(bg=WHITE)
            dialog.transient(self.root)
            dialog.grab_set()
            inner = tk.Frame(dialog, bg=WHITE)
            inner.pack(fill='both', expand=True, padx=20, pady=16)
            label(inner, f"{session['course_name']} · {STATE_TEXT[session['state']]}", size=15, bold=True).pack(anchor='w')
            label(inner, '移出仅改变应到资格，原成员、签到和人脸证据保留。已结束课次的调整会改变统计。', size=9, color=MUTED).pack(anchor='w', pady=(5, 10))
            body = tk.Frame(inner, bg=WHITE)
            body.pack(fill='both', expand=True)
            body.columnconfigure(0, weight=1)
            body.columnconfigure(1, minsize=340)
            body.rowconfigure(0, weight=1)
            frame, listing = tree(body, [('id', '学号', 100), ('name', '姓名', 90), ('group', '班级', 110), ('status', '当前状态', 120), ('time', '实际到课时间', 180)], height=6)
            frame.grid(row=0, column=0, sticky='nsew', padx=(0, 16))
            horizontal = ttk.Scrollbar(frame, orient='horizontal', command=listing.xview)
            horizontal.grid(row=1, column=0, sticky='ew')
            listing.configure(xscrollcommand=horizontal.set)
            rows = (yield IO(partial(self.store.session_rows, sid, include_removed=True)))
            replace_rows(listing, [(r['student_id'], (r['student_id'], r['name'], r['group_name'], STATUS_TEXT[r['status']], r['signed_at'] or '—'), '') for r in rows])
            row_lookup = {r['student_id']: r for r in rows}

            @self.flow
            def history():
                selected = listing.selection()
                if selected:
                    yield from self.show_attendance_history(sid, selected[0], row_lookup[selected[0]])
            button(body, '查看选中人员原始证据 / 更正历史', history, plain=True).grid(row=1, column=0, sticky='w', pady=5)
            form = tk.Frame(body, bg=WHITE)
            form.grid(row=0, column=1, rowspan=2, sticky='new')
            form.columnconfigure(0, weight=1)
            add = tk.StringVar()
            remove = tk.StringVar()
            reason = tk.StringVar()
            actor = tk.StringVar()
            entry_field(form, '增补学号（多个用逗号分隔）', add, 0)
            entry_field(form, '移出学号（多个用逗号分隔）', remove, 2)
            entry_field(form, '调整原因（必填）', reason, 4)
            entry_field(form, '操作署名（选填，未经账号认证）', actor, 6)
            preview_frame = tk.Frame(inner, bg=WHITE)
            preview_frame.pack(fill='x', pady=10)
            preview = tk.Text(preview_frame, height=4, wrap='word', font=('Microsoft YaHei UI', 9), state='disabled')
            scroll = ttk.Scrollbar(preview_frame, command=preview.yview)
            scroll.pack(side='right', fill='y')
            preview.configure(yscrollcommand=scroll.set)
            preview.pack(fill='x')

            def preview_text(text):
                preview.configure(state='normal')
                preview.delete('1.0', 'end')
                preview.insert('1.0', text)
                preview.configure(state='disabled')
            preview_text('请先预览变更，再确认保存。增补人员未训练人脸时，需由老师人工核对补签。')
            actions = tk.Frame(inner, bg=WHITE)
            actions.pack(fill='x')
            pending = {}

            @self.flow
            def prepare(refresh):
                try:
                    split = lambda value: tuple(value.replace('，', ',').replace('\n', ',').replace(' ', ',').strip(',').split(',')) if value.strip() else ()
                    args = {'refresh': refresh, 'add_ids': () if refresh else tuple(filter(None, split(add.get()))), 'remove_ids': () if refresh else tuple(filter(None, split(remove.get())))}
                    plan = (yield IO(partial(self.store.preview_roster_change, sid, **args)))
                    names = {r['student_id']: r['name'] for r in plan['after']}
                    describe = lambda ids: '、'.join((f'{x} {names[x]}' for x in ids)) or '无'
                    preview_text(f"将增补：{describe(plan['added'])}\n将移出：{describe(plan['removed'])}\n移出但保留既有记录：{', '.join(plan['recorded_removals']) or '无'}")
                    pending.update(plan=plan, args=args)
                    confirm.configure(state='normal' if plan['added'] or plan['removed'] else 'disabled')
                except Exception as error:
                    pending.clear()
                    confirm.configure(state='disabled')
                    messagebox.showerror('预览未完成', user_message(error), parent=dialog)

            @self.flow
            def save():
                if not pending:
                    return
                try:
                    if pending['inputs'] != (add.get(), remove.get()):
                        raise ValueError('输入已改变，请重新预览。')
                    yield IO(partial(self.store.apply_roster_change, sid, pending['plan']['token'], reason.get(), actor=actor.get(), **pending['args']))
                except Exception as error:
                    messagebox.showerror('名单未保存', user_message(error), parent=dialog)
                    return
                dialog.destroy()
                self.say('应到名单已调整，原始记录和变更历史已保留')
                yield from self.refresh_sessions()

            @self.flow
            def preview_mode(refresh):
                yield from prepare(refresh)
                if pending:
                    pending['inputs'] = (add.get(), remove.get())
            refresh_button = button(actions, '预览班级刷新', lambda: preview_mode(True))
            refresh_button.pack(side='left')
            refresh_button.configure(state='normal' if session['state'] == 'draft' else 'disabled')
            button(actions, '预览手工调整', lambda: preview_mode(False)).pack(side='left', padx=8)
            confirm = button(actions, '确认保存变更', save, primary=True)
            confirm.pack(side='right')
            confirm.configure(state='disabled')
        yield from show()

    @ui_flow
    def refresh_sessions(self):
        self.sessions_data = (yield IO(partial(self.store.sessions)))
        replace_rows(self.sessions_tree, [(row['id'], (f"{row['course_name']} / {row['group_name']}", row['starts_at'].replace('T', ' ')[:16], STATE_TEXT[row['state']], f"{row['arrived']}/{row['expected']}" if row['state'] in {'open', 'closed'} else '—'), 'good' if row['state'] == 'open' else 'muted' if row['state'] in {'closed', 'cancelled'} else '') for row in self.sessions_data])
        self.sync_session_actions()
        yield from self.refresh_records()

    def build_records(self):
        page = self.pages['records']
        page.rowconfigure(4, weight=1)
        page.columnconfigure(0, weight=1)
        filters = tk.Frame(page, bg=WHITE)
        filters.grid(row=0, column=0, sticky='ew', pady=(0, 12))
        self.record_period = tk.StringVar(value='当天')
        self.record_status = tk.StringVar(value='全部')
        label(filters, '时间范围', size=10, color=MUTED).pack(side='left', padx=(0, 10))
        self.record_period_combo = ttk.Combobox(filters, textvariable=self.record_period, values=PERIODS, state='readonly', width=12)
        self.record_period_combo.pack(side='left')
        label(filters, '查看', size=10, color=MUTED).pack(side='left', padx=(24, 10))
        self.record_status_combo = ttk.Combobox(filters, textvariable=self.record_status, values=['全部', '缺勤'], state='readonly', width=10)
        self.record_status_combo.pack(side='left')
        for combo in (self.record_period_combo, self.record_status_combo):
            combo.bind('<<ComboboxSelected>>', lambda event: self.guarded(self.refresh_records))
        button(filters, '学期设置', self.configure_semester, plain=True).pack(side='right')
        self.record_choice = tk.StringVar(value='全部课次')
        self.record_rows = []
        self.record_row_lookup = {}
        self.record_combo = ttk.Combobox(page, textvariable=self.record_choice, state='readonly')
        self.record_combo.grid(row=1, column=0, sticky='ew', pady=(0, 15))
        self.record_combo.bind('<<ComboboxSelected>>', lambda event: self.guarded(self.refresh_records))
        toolbar = tk.Frame(page, bg=WHITE)
        toolbar.grid(row=2, column=0, sticky='ew', pady=(0, 12))
        self.record_summary = label(page, '暂无课次', size=10, color=MUTED)
        self.record_summary.grid(row=3, column=0, sticky='w', pady=(0, 12))
        button(toolbar, '导出 Excel', self.export).pack(side='right')
        button(toolbar, '人工更正', self.correct_record, plain=True).pack(side='right', padx=8)
        button(toolbar, '原始证据 / 更正历史', self.show_selected_history, plain=True).pack(side='right')
        button(toolbar, '操作记录', self.show_audit, plain=True).pack(side='right')
        frame, self.records_tree = tree(page, [('id', '学号', 110), ('name', '姓名', 80), ('group', '班级', 100), ('status', '状态', 75), ('course', '课程', 165), ('start', '课次开始时间', 155), ('time', '实际到课时间', 130), ('method', '方式', 65), ('note', '备注', 140)])
        frame.grid(row=4, column=0, sticky='nsew')
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=self.records_tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.records_tree.configure(xscrollcommand=horizontal.set)
        self.record_range_label = label(page, '', size=9, color=MUTED)
        self.record_range_label.grid(row=5, column=0, sticky='w', pady=(12, 0))

    @ui_flow
    def record_window(self, now=None):
        now = now or datetime.now()
        semester = (yield IO(partial(self.store.semester_dates, now))) if self.record_period.get() == '当前学期' else None
        return record_date_range(self.record_period.get(), now, semester)

    def record_session_id(self):
        sid = self.record_lookup.get(self.record_choice.get())
        if sid is not None:
            return sid
        selected = self.records_tree.selection()
        if selected and selected[0] in self.record_row_lookup:
            return self.record_row_lookup[selected[0]]['session_id']
        raise ValueError('请先选择一条考勤记录或一个具体课次。')

    @ui_flow
    def refresh_records(self):
        now = datetime.now()
        requested=(self.record_period.get(),self.record_status.get(),self.record_choice.get())
        try:
            start, end = (yield from self.record_window(now))
        except ValueError as error:
            self.record_lookup = {}
            self.record_rows = []
            self.record_row_lookup = {}
            self.record_combo.configure(values=['全部课次'])
            self.record_choice.set('全部课次')
            replace_rows(self.records_tree, [])
            self.record_summary.configure(text='请设置有效的学期日期')
            self.record_range_label.configure(text=str(error))
            return
        rows = (yield IO(partial(self.store.attendance_records, start, end, now=now)))
        if requested!=(self.record_period.get(),self.record_status.get(),self.record_choice.get()):
            self.schedule(0,self.refresh_records)
            return
        self.record_lookup = {f"#{row['session_id']}  {row['course_name']} · {row['session_group_name']} · {row['session_starts_at'].replace('T', ' ')[:16]}": row['session_id'] for row in rows}
        choices = ['全部课次'] + list(self.record_lookup)
        old = self.record_choice.get()
        self.record_combo.configure(values=choices)
        self.record_combo.current(choices.index(old) if old in choices else 0)
        self.record_combo.xview_moveto(0)
        sid = self.record_lookup.get(self.record_choice.get())
        if sid is not None:
            rows = [row for row in rows if row['session_id'] == sid]
        counts = Counter((row['status'] for row in rows))
        self.record_summary.configure(text=f"应到 {len(rows)} 人次  ·  已到 {counts['present'] + counts['late']}  ·  迟到 {counts['late']}  ·  请假 {counts['leave']}  ·  缺勤 {counts['absent']}  ·  未签到 {counts['pending']}")
        self.record_rows = [row for row in rows if self.record_status.get() != '缺勤' or row['status'] == 'absent']
        self.record_row_lookup = {row['student_id'] if sid is not None else f"{row['session_id']}:{row['student_id']}": row for row in self.record_rows}
        replace_rows(self.records_tree, [(key, (row['student_id'], row['name'], row['group_name'], STATUS_TEXT[row['status']], row['course_name'], row['session_starts_at'].replace('T', ' ')[:16], row['signed_at'][11:19] if row['signed_at'] else '时间未确认' if row['status'] in {'present', 'late'} else '—', {'face': '人脸', 'manual': '人工', 'legacy': '迁移'}.get(row['method'], ''), row['note'] or ''), 'good' if row['status'] == 'present' else 'warning' if row['status'] == 'late' else 'muted') for key, row in self.record_row_lookup.items()])
        self.record_range_label.configure(text=f'{start:%Y-%m-%d} 至 {end - timedelta(days=1):%Y-%m-%d} · 按课次开始日期统计 · 显示 {len(self.record_rows)} 条')

    @ui_flow
    def correct_record(self):
        if not self.records_tree.selection():
            self.say('请先选中一名学生的考勤记录')
            return
        selected = self.record_row_lookup.get(self.records_tree.selection()[0])
        if not selected:
            return
        sid, student_id = (selected['session_id'], selected['student_id'])
        rows = yield IO(partial(self.store.session_rows,sid))
        row = next((item for item in rows if item['student_id']==student_id),None)
        if row is None:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('人工更正')
        dialog.geometry('620x740')
        dialog.configure(bg=WHITE)
        dialog.transient(self.root)
        dialog.grab_set()
        label(dialog, f"{row['name']} · {student_id}", size=14, bold=True).pack(anchor='w', padx=24, pady=(20, 5))
        label(dialog, f"{selected['course_name']} · {selected['session_starts_at'].replace('T', ' ')[:16]}", size=9, color=MUTED, wraplength=450, justify='left').pack(anchor='w', padx=24, pady=(0, 12))
        choice = tk.StringVar(value=STATUS_TEXT[row['status']] if row['status'] != 'pending' else '已签到')
        options = {STATUS_TEXT[key]: key for key in ['present', 'late', 'leave', 'absent']}
        ttk.Combobox(dialog, textvariable=choice, values=list(options), state='readonly').pack(fill='x', padx=24)
        form = tk.Frame(dialog, bg=WHITE)
        form.pack(fill='x', padx=24)
        form.columnconfigure(0, weight=1)
        actual = tk.StringVar(value=(row['signed_at'] or '').replace('T', ' '))
        unknown = tk.BooleanVar(value=row['status'] in {'present', 'late'} and (not row['signed_at']))
        note = tk.StringVar(value=row['note'] or '')
        actor = tk.StringVar()
        entry_field(form, '实际到课时间（年-月-日 时:分[:秒]；留空保留原值）', actual, 0)
        ttk.Checkbutton(form, text='时间未确认（不会自动填当前时间）', variable=unknown, command=lambda: actual.set('') if unknown.get() else None).grid(row=2, column=0, sticky='w', pady=10)
        entry_field(form, '当前备注（与修改原因分别保存）', note, 3)
        entry_field(form, '操作署名（选填，未经账号认证）', actor, 5)
        label(dialog, '原始刷脸证据会保留；实际到课时间与本次操作时间分别记录。', size=9, color=MUTED, wraplength=560).pack(anchor='w', padx=24, pady=(12, 0))
        label(dialog, '修改原因（必填，会写入操作记录）', size=9, color=MUTED).pack(anchor='w', padx=24, pady=(12, 7))
        reason = tk.Text(dialog, height=3, font=('Microsoft YaHei UI', 10), relief='solid', bd=1)
        reason.pack(fill='x', padx=24)

        @self.flow
        def save():
            try:
                extra = {'time_unconfirmed': unknown.get(), 'actor': actor.get(), 'note': note.get()}
                if actual.get().strip():
                    try:
                        extra['arrival_time'] = datetime.fromisoformat(actual.get().strip())
                    except ValueError:
                        raise ValueError('实际到课时间请使用 年-月-日 时:分[:秒]。') from None
                yield IO(partial(self.store.correct_attendance, sid, student_id, options[choice.get()], reason.get('1.0', 'end').strip(), **extra))
            except Exception as error:
                messagebox.showerror('更正未完成', user_message(error), parent=dialog)
                return
            dialog.destroy()
            self.say('考勤已更正，修改原因已记录')
            yield from self.refresh_sessions()
        button(dialog, '保存更正', save, primary=True).pack(anchor='e', padx=24, pady=20)

    @ui_flow
    def show_selected_history(self):
        selected = self.records_tree.selection()
        row = self.record_row_lookup.get(selected[0]) if selected else None
        if not row:
            self.say('请先选中一条考勤记录')
            return
        yield from self.show_attendance_history(row['session_id'], row['student_id'], row)

    @ui_flow
    def show_attendance_history(self, session_id, student_id, row):
        history = (yield IO(partial(self.store.attendance_history, session_id, student_id)))
        evidence = history['evidence']
        lines = [f"课次 #{session_id}  {student_id}  {row['name']}", f"当前状态：{STATUS_TEXT[row['status']]}", f"当前实际到课时间：{row['signed_at'] or ('时间未确认' if row['status'] in {'present', 'late'} else '无')}", f"当前备注：{row['note'] or '无'}", '\n原始人脸证据：']
        if evidence:
            lines.extend([f"首次人脸签到时间：{evidence['signed_at']}", f"识别方式：{evidence['method']}", '来源：' + ('升级时保留的现存原值' if evidence['origin'] == 'migrated_current' else '现场识别保存')])
        else:
            lines.append('无已保存的人脸证据；旧版已被覆盖的数据无法反推。')
        lines.append('\n逐次更正记录（修改时间与到课时间不同）：')
        for change in history['corrections']:
            lines.extend([f"\n{change['created_at']}  {self.actor_text(change)}", self.audit_detail(change), f"修改前：{self.snapshot_text(change['before_json'])}", f"修改后：{self.snapshot_text(change['after_json'])}"])
        if not history['corrections']:
            lines.append('暂无更正。')
        self.text_details('原始证据 / 更正历史', '\n'.join(lines))

    @ui_flow
    def export(self):

        @self.flow
        def save():
            start, end = (yield from self.record_window())
            sid = self.record_lookup.get(self.record_choice.get())
            status = 'absent' if self.record_status.get() == '缺勤' else 'all'
            title = f'考勤记录 · {self.record_period.get()} · {self.record_status.get()}'
            path = filedialog.asksaveasfilename(parent=self.root, title='导出筛选后的考勤', initialfile=f'考勤_{self.record_period.get()}_{self.record_status.get()}_{start:%Y%m%d}.xlsx', defaultextension='.xlsx', filetypes=[('Excel 考勤', '*.xlsx')])
            if path:
                yield IO(partial(self.store.export_attendance, path, start, end, status, sid, title))
                self.say('已按当前时间、状态和课次筛选导出')
        yield from save()

    @ui_flow
    def configure_semester(self):
        try:
            dates = yield IO(partial(self.store.semester_dates))
        except ValueError:
            dates = None
        if dates is None:
            from attendance_filters import default_semester_dates
            dates = default_semester_dates()
        dialog = tk.Toplevel(self.root)
        dialog.title('当前学期设置')
        dialog.geometry('510x440')
        dialog.configure(bg=WHITE)
        dialog.transient(self.root)
        dialog.grab_set()
        form = tk.Frame(dialog, bg=WHITE)
        form.pack(fill='both', expand=True, padx=26, pady=24)
        form.columnconfigure(0, weight=1)
        label(form, '当前学期起止日期', size=16, bold=True).grid(row=0, column=0, sticky='w', pady=(0, 8))
        label(form, '按学校校历设置，开始与结束当天均包含在内。', size=9, color=MUTED).grid(row=1, column=0, sticky='w')
        starts = tk.StringVar(value=dates[0].isoformat())
        ends = tk.StringVar(value=dates[1].isoformat())
        entry_field(form, '开始日期（年-月-日）', starts, 2)
        entry_field(form, '结束日期（年-月-日）', ends, 4)
        label(form, '默认：2月1日—8月31日、9月1日—次年1月31日。\n自定义后将沿用已保存日期，跨学期时可修改或恢复默认。', size=9, color=MUTED, justify='left', wraplength=450).grid(row=6, column=0, sticky='w', pady=16)
        actions = tk.Frame(form, bg=WHITE)
        actions.grid(row=7, column=0, sticky='ew')

        @self.flow
        def save():
            try:
                yield IO(partial(self.store.set_semester_dates, starts.get().strip(), ends.get().strip()))
            except Exception as error:
                messagebox.showerror('学期设置未保存', user_message(error), parent=dialog)
                return
            dialog.destroy()
            self.say('当前学期日期已保存')
            yield from self.refresh_records()

        @self.flow
        def reset():
            try:
                yield IO(partial(self.store.reset_semester_dates))
            except Exception as error:
                messagebox.showerror('学期设置未保存', user_message(error), parent=dialog)
                return
            dialog.destroy()
            self.say('已恢复默认学期划分')
            yield from self.refresh_records()
        button(actions, '恢复默认划分', reset, plain=True).pack(side='left')
        button(actions, '保存设置', save, primary=True).pack(side='right')

    @ui_flow
    def show_audit(self):

        @self.flow
        def show():
            sid = self.record_session_id()
            dialog = tk.Toplevel(self.root)
            dialog.title('课次操作记录')
            dialog.geometry('1000x520')
            frame, widget = tree(dialog, [('time', '时间', 170), ('action', '操作', 130), ('student', '学号', 110), ('actor', '操作来源 / 署名', 220), ('detail', '详情', 420)])
            frame.pack(fill='both', expand=True, padx=16, pady=16)
            rows = (yield IO(partial(self.store.audit_rows, sid)))
            replace_rows(widget, [(row['id'], (row['created_at'].replace('T', ' '), row['action'], row['student_id'] or '', self.actor_text(row), self.audit_detail(row)), '') for row in rows])
            lookup = {str(row['id']): row for row in rows}

            def details(event=None):
                selected = widget.selection()
                if selected:
                    item = lookup[selected[0]]
                    self.text_details('操作完整记录', f"操作时间：{item['created_at']}\n操作：{item['action']}\n课次：{item['session_id']}\n学号：{item['student_id'] or '无'}\n{self.actor_text(item)}\n{self.audit_detail(item)}\n\n修改前：\n{self.snapshot_text(item['before_json'])}\n\n修改后：\n{self.snapshot_text(item['after_json'])}")
            widget.bind('<Double-1>', details)
        yield from show()

    def build_settings(self):
        page = self.pages['settings']
        label(page, '数据备份', size=15, bold=True).pack(anchor='w', pady=(4, 10))
        label(page, '备份包含学生、课次、签到记录、操作记录、模型与人脸样本。', size=10, color=MUTED).pack(anchor='w')
        backup = button(page, '保存完整备份', self.backup, primary=True)
        backup.pack(anchor='w', pady=(18, 30))
        self.task_buttons.append(backup)
        ttk.Separator(page).pack(fill='x', pady=(0, 25))
        label(page, '摄像头设置', size=15, bold=True).pack(anchor='w', pady=(0, 8))
        label(page, '默认编号为 0；连接外置摄像头时可尝试 1 或 2。修改前请停止相机。', size=10, color=MUTED).pack(anchor='w')
        row = tk.Frame(page, bg=WHITE)
        row.pack(anchor='w', pady=(15, 30))
        self.camera = tk.StringVar(value='0')
        ttk.Spinbox(row, textvariable=self.camera, from_=0, to=9, width=8).pack(side='left')
        button(row, '保存设置', lambda: self.guarded(lambda: self.save_camera())).pack(side='left', padx=15)
        ttk.Separator(page).pack(fill='x', pady=(0, 25))
        label(page, '管理端', size=15, bold=True).pack(anchor='w', pady=(0, 8))
        label(page, '管理密码只用于本机管理入口；请同时保护电脑账户与备份文件。', size=10, color=MUTED).pack(anchor='w')
        tools=tk.Frame(page,bg=WHITE); tools.pack(anchor='w',pady=12)
        button(tools,'修改管理密码',self.change_management_password).pack(side='left',padx=(0,10))
        button(tools,'删除选中学生人脸',lambda:self.delete_faces(False)).pack(side='left',padx=(0,10))
        button(tools,'清空全部训练数据',lambda:self.delete_faces(True),plain=True).pack(side='left')
        label(page,'删除样本需复核管理密码并二次确认；不删除名单、考勤时间证据。\n历史备份中的人脸副本需另外清理。',size=9,color=MUTED).pack(anchor='w')
        button(page, '退出管理', self.close, plain=True).pack(anchor='w', pady=18)

    def change_management_password(self):
        dialog=tk.Toplevel(self.root); dialog.title('修改管理密码'); dialog.geometry('520x430')
        dialog.transient(self.root); dialog.grab_set()
        form=tk.Frame(dialog); form.pack(fill='both',expand=True,padx=24,pady=20); form.columnconfigure(0,weight=1)
        old=tk.StringVar(); new=tk.StringVar(); confirm=tk.StringVar()
        entry_field(form,'原管理密码',old,0,show='●')
        entry_field(form,'新密码（8 至 128 位）',new,2,show='●')
        entry_field(form,'再次输入新密码',confirm,4,show='●')
        @self.flow
        def save():
            if new.get()!=confirm.get():
                messagebox.showerror('未修改','两次新密码不一致。',parent=dialog); return
            try:
                yield IO(partial(self.store.change_password,old.get(),new.get()))
            except Exception as error:
                messagebox.showerror('未修改',user_message(error),parent=dialog); return
            old.set(''); new.set(''); confirm.set('')
            dialog.destroy(); self.say('管理密码已修改')
        button(form,'保存新密码',save,primary=True).grid(row=6,column=0,pady=20,sticky='e')

    def delete_faces(self,clear_all=False):
        student=self.selected_student
        if not clear_all and not student:
            self.say('请先在学生页选择学生'); return
        dialog=tk.Toplevel(self.root); dialog.title('删除人脸资料'); dialog.geometry('600x390')
        dialog.transient(self.root); dialog.grab_set()
        form=tk.Frame(dialog); form.pack(fill='both',expand=True,padx=24,pady=20); form.columnconfigure(0,weight=1)
        label(form,'清空所有人脸训练数据' if clear_all else f"删除 {student['name']} 的人脸样本",size=15,bold=True).grid(row=0,column=0,sticky='w')
        label(form,'操作会删除样本并重建或移除模型，无法在界面撤销。\n名单及考勤时间证据保留；历史备份不在本次删除范围。',size=10,color=MUTED).grid(row=1,column=0,sticky='w',pady=15)
        password=tk.StringVar()
        entry_field(form,'重新输入管理密码',password,2,show='●')
        @self.flow
        def remove():
            if not messagebox.askyesno('再次确认删除','确认删除上述人脸训练数据？\n请先停止所有相机、采集和训练。',parent=dialog): return
            credentials=password.get(); password.set('')
            try:
                count=yield IO(partial(delete_face_data,self.store,credentials,None if clear_all else student['student_id'],clear_all=clear_all))
            except Exception as error:
                messagebox.showerror('删除未完成',user_message(error),parent=dialog); return
            dialog.destroy(); self.say(f'已删除 {count} 张样本，模型已重建或清空')
            yield from self.refresh_students()
        button(form,'确认删除并更新模型',remove,primary=True).grid(row=4,column=0,pady=20,sticky='e')

    @ui_flow
    def load_camera_setting(self):
        value = yield IO(partial(self.store.camera_index))
        self.camera.set(str(value))

    @ui_flow
    def save_camera(self):
        yield IO(partial(self.store.camera_index, int(self.camera.get())))
        self.say('摄像头编号已保存，下次开启相机时生效')

    def backup(self):
        if not messagebox.askyesno('备份含敏感数据','备份包含名单、人脸样本、模型和口令摘要。\n请保存在受保护位置并加密保存。是否继续？',parent=self.root):
            return
        path = filedialog.asksaveasfilename(parent=self.root, title='保存完整备份', initialfile='课堂签到备份_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.zip', defaultextension='.zip', filetypes=[('备份压缩包', '*.zip')])
        if path:
            store=self.store
            def work():
                with ResourceLease(DATA_LOCK_PATH, '正在采集或训练，请完成后再备份。'):
                    store.backup_archive(path)
            self.run_job('备份数据', work)

    @ui_flow
    def tick(self):
        try:
            yield from self.refresh_sessions()
            if self.refresh_error:
                self.say('课次数据已恢复')
                self.refresh_error = False
        except sqlite3.Error as error:
            log_error('admin_refresh_failed',error)
            self.refresh_error = True
            self.say(user_message(error))
        finally:
            self.schedule(3000, self.tick)

    def close(self):
        if self.capture_dialog and self.capture_dialog.window.winfo_exists():
            self.capture_dialog.stop()
        return super().close()

class AdminLogin(FlowHost):

    def __init__(self, root, store=None, async_io=True, clock=None):
        self.init_flows(root,async_io)
        self.root, self.store = (root, store or AttendanceStore())
        self.clock = clock or datetime.now
        self.auth_status = None
        self.submitting = False
        self.login_complete = False
        self.auth_revision = 0
        self.lock_feedback = False
        self.feedback_timer = self.status_timer = None
        self.security_hint = tk.StringVar()
        setup_theme(root)
        root.title('教师管理 · 登录')
        root.geometry('520x550')
        root.minsize(500, 520)
        self.error=tk.StringVar(value='正在检查管理入口…')
        self.loader=tk.Frame(root); self.loader.pack(fill='both',expand=True,padx=25,pady=25)
        tk.Label(self.loader,textvariable=self.error,wraplength=430).pack(pady=25)
        tk.Button(self.loader,text='重试',command=self.check_login).pack()
        root.report_callback_exception=lambda kind,error,tb:self.handle_error(error.with_traceback(tb))
        root.protocol('WM_DELETE_WINDOW',self.close)
        self.check_login()

    def close(self):
        self.closed = True
        self.stop_login_feedback()
        self.cancel_flows(); self.root.destroy()

    def stop_login_feedback(self):
        for timer in (self.feedback_timer, self.status_timer):
            if timer:
                try: self.root.after_cancel(timer)
                except tk.TclError: pass
        self.feedback_timer = self.status_timer = None

    def schedule(self,milliseconds,callback):
        if not self.closed: return self.root.after(milliseconds,callback)

    def say(self,message):
        self.error.set(message)

    def handle_error(self,error):
        log_error('login_failed',error)
        self.error.set(user_message(error))

    @ui_flow
    def check_login(self):
        self.is_setup = not (yield IO(self.store.admin_exists))
        self.build_form()
        if not self.is_setup:
            yield from self.refresh_login_status()

    def build_form(self):
        root=self.root
        self.loader.destroy()
        self.frame = panel(root)
        self.frame.pack(fill='both', expand=True, padx=30, pady=30)
        inner = tk.Frame(self.frame, bg=WHITE)
        inner.pack(fill='both', expand=True, padx=30, pady=30)
        inner.columnconfigure(0, weight=1)
        label(inner, '设置管理密码' if self.is_setup else '登录教师管理', size=22, bold=True).grid(row=0, column=0, sticky='w', pady=(0, 10))
        label(inner, '首次使用请设置密码，学生端不需要此密码。' if self.is_setup else '采集、训练、课次与考勤管理仅在此入口提供。', size=9, color=MUTED, wraplength=360, justify='left').grid(row=1, column=0, sticky='w', pady=(0, 15))
        self.password = tk.StringVar()
        self.confirm = tk.StringVar()
        self.error = tk.StringVar()
        self.password_entry = entry_field(inner, '管理密码（至少 8 位）', self.password, 2, show='●')
        if self.is_setup:
            entry_field(inner, '确认密码', self.confirm, 4, show='●')
        label(inner, textvariable=self.error, size=9, color='#C55D3A', wraplength=350, justify='left').grid(row=6, column=0, sticky='ew', pady=(15, 5))
        label(inner, textvariable=self.security_hint, size=9, color='#C55D3A', wraplength=350, justify='left').grid(row=7, column=0, sticky='ew', pady=(0, 5))
        self.login_button = button(inner, '设置并进入' if self.is_setup else '登录管理端', self.submit, primary=True)
        self.login_button.grid(row=8, column=0, sticky='ew', pady=(10, 0))
        self.password_entry.focus_set()
        root.bind('<Return>', lambda event: self.submit())
        if not self.is_setup:
            self.security_hint.set('正在读取剩余次数及锁定状态…')
            self.feedback_timer = self.schedule(1000, self.tick_login_feedback)

    def render_login_feedback(self):
        """只更新本地倒计时；数据库校验及密码计算仍在后台执行。"""
        if self.login_complete or self.closed:
            return 0
        remaining_seconds = 0
        if not self.is_setup and self.auth_status:
            until = self.auth_status['locked_until']
            remaining_seconds = max(0, ceil((datetime.fromisoformat(until)-self.clock()).total_seconds())) if until else 0
            if remaining_seconds:
                minutes, seconds = divmod(remaining_seconds, 60)
                self.security_hint.set(f'登录已锁定，还需等待 {minutes:02d}分{seconds:02d}秒。\n预计 {until[11:19]} 可重新尝试。')
            elif self.auth_status['configured']:
                attempts = max(1, self.auth_status['attempts_remaining'])
                duration = self.auth_status['next_lock_seconds']
                self.security_hint.set(f'还可尝试 {attempts} 次；达到上限后将锁定 {duration} 秒。')
                if self.lock_feedback:
                    self.error.set('锁定时间已到，可以重新尝试登录。')
                    self.lock_feedback = False
            else:
                self.security_hint.set('管理密码尚未设置或已重置，请重新打开管理入口。')
        self.login_button.configure(
            state='disabled' if remaining_seconds or self.submitting else 'normal',
            text='正在验证…' if self.submitting else '锁定中' if remaining_seconds else '设置并进入' if self.is_setup else '登录管理端')
        return remaining_seconds

    def tick_login_feedback(self):
        if self.login_complete or self.closed:
            return
        self.render_login_feedback()
        self.feedback_timer = self.schedule(1000, self.tick_login_feedback)

    @ui_flow
    def refresh_login_status(self):
        if self.login_complete or self.closed or self.is_setup:
            return
        revision = self.auth_revision
        try:
            status = yield IO(partial(self.store.admin_auth_status, self.clock()))
            if not self.login_complete and revision == self.auth_revision and not self.submitting:
                self.auth_status = status
                self.render_login_feedback()
        except Exception as error:
            if not self.login_complete:
                self.handle_error(error)
        finally:
            if not self.login_complete and not self.closed:
                self.status_timer = self.schedule(3000, self.refresh_login_status)

    @ui_flow
    def submit(self):
        if self.login_complete or self.submitting or self.closed:
            return
        if self.render_login_feedback():
            self.error.set('登录已锁定，请等待倒计时结束。')
            self.lock_feedback = True
            return
        self.submitting = True
        self.auth_revision += 1
        self.render_login_feedback()
        try:
            if self.is_setup:
                if self.password.get() != self.confirm.get():
                    raise ValueError('两次输入的密码不一致。')
                yield IO(partial(self.store.setup_admin,self.password.get()))
            else:
                result = yield IO(partial(self.store.authenticate_with_status, self.password.get(), self.clock()))
                self.auth_status = result
                if not result['authenticated']:
                    self.lock_feedback = bool(result['retry_after_seconds'])
                    self.error.set('密码不正确，已达到尝试次数上限。' if self.lock_feedback and result['attempted'] else
                                   '登录已锁定，请等待倒计时结束。' if self.lock_feedback else '密码不正确，请重试。')
                    return
            self.password.set('')
            self.confirm.set('')
            self.root.unbind('<Return>')
            self.login_complete = True
            self.stop_login_feedback()
            self.frame.destroy()
            self.application = AdminApp(self.root, self.store, authenticated=True,async_io=self.async_io)
        except Exception as error:
            self.handle_error(error)
        finally:
            self.submitting = False
            if not self.login_complete:
                self.render_login_feedback()

def main():
    from app_startup import launch_entry
    launch_entry(lambda root,store:AdminLogin(root,store))
if __name__ == '__main__':
    main()
