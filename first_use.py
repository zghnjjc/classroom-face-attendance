"""首次使用进度与名单预览，和业务数据一起备份。"""

import hashlib
import json
from datetime import datetime


SETUP_KEY = 'first_use_v1'


class FirstUseService:
    def __init__(self, store):
        self.store = store

    def _read(self, connection):
        raw = self.store._get(connection, SETUP_KEY)
        if raw is None:
            return None
        try:
            state = json.loads(raw)
            if (type(state['version']) is not int or state['version'] != 1 or type(state['completed']) is not bool
                    or state['camera'] not in {'pending', 'tested', 'skipped'}
                    or state['roster'] not in {'pending', 'imported', 'existing', 'skipped'}
                    or not isinstance(state['directory'], str)):
                raise ValueError()
            if state['completed'] and ('pending' in (state['camera'], state['roster'])):
                raise ValueError()
            return state
        except (ValueError, TypeError, KeyError):
            raise ValueError('首次使用进度无法读取，请先备份数据库并检查数据；不会自动覆盖进度。') from None

    def _save(self, connection, state):
        state['updated_at'] = datetime.now().replace(microsecond=0).isoformat()
        self.store._set(connection, SETUP_KEY, json.dumps(state, ensure_ascii=False, sort_keys=True))

    def prepare(self, manual=False):
        directory = str(self.store.path.parent.resolve())
        # 已有/已完成/同目录续办仅查询；确需写入时在写事务中重新判定。
        with self.store.connect() as connection:
            connection.execute('BEGIN')
            state, changed = self._prepared_state(connection, manual, directory)
            if not changed:
                return state
        with self.store.connect(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            state, changed = self._prepared_state(connection, manual, directory)
            if changed:
                self._save(connection, state)
            return state

    def _prepared_state(self, connection, manual, directory):
        state = self._read(connection)
        if state is None:
            existing = bool(self.store._get(connection, 'admin_auth')) or any(
                connection.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
                for table in ('students', 'sessions', 'audit_log'))
            if existing and not manual:
                return None, False
        if state is None or (manual and state['completed']):
            return dict(version=1, completed=False, camera='pending', roster='pending', directory=directory), True
        if not state['completed'] and state['directory'] != directory:
            state.update(camera='pending', directory=directory)
            return state, True
        return state, False

    def snapshot(self):
        with self.store.connect() as connection:
            connection.execute('BEGIN')
            state = self._read(connection)
            active = connection.execute('SELECT COUNT(*) FROM students WHERE active=1').fetchone()[0]
            return dict(state=state, active_students=active, password_set=bool(self.store._get(connection, 'admin_auth')),
                        camera_index=int(self.store._get(connection, 'camera_index', '0')),
                        directory=str(self.store.path.parent.resolve()))

    def _editable(self, connection):
        state = self._read(connection)
        if not state or state['completed']:
            raise ValueError('首次使用进度已变更，请重新打开向导。')
        if not self.store._get(connection, 'admin_auth'):
            raise ValueError('请先设置管理密码并登录教师端。')
        return state

    def record_camera(self, result, index):
        if result not in {'tested', 'skipped'} or type(index) is not int or not 0 <= index <= 9:
            raise ValueError('请选择摄像头 0 至 9，并确认测试结果或明确跳过。')
        with self.store.connect(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self._editable(connection)
            state['camera'] = result
            if result == 'tested':
                self.store._set(connection, 'camera_index', str(index))
            self._save(connection, state)
            self.store._audit(connection, '首次使用摄像头测试', detail=f'编号 {index}；{result}')

    def record_roster(self, result):
        if result not in {'existing', 'skipped'}:
            raise ValueError('请选择沿用已有名单或稍后导入。')
        with self.store.connect(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self._editable(connection)
            if result == 'existing' and not connection.execute('SELECT 1 FROM students WHERE active=1 LIMIT 1').fetchone():
                raise ValueError('尚无启用的学生，请导入名单或明确选择稍后导入。')
            state['roster'] = result
            self._save(connection, state)

    def _roster_plan(self, connection, rows):
        prepared = self.store._prepare_import(rows)
        existing = {row['student_id']: dict(row) for row in connection.execute('SELECT * FROM students')}
        preview = []
        for student_id, name, group in prepared:
            original = existing.get(student_id)
            action = '新增' if original is None else '保持不变' if (name, group) == (original['name'], original['group_name']) else '更新姓名/班级'
            preview.append(dict(student_id=student_id, name=name, group_name=group, action=action,
                                active=original['active'] if original else 1, original=original))
        token = hashlib.sha256(json.dumps(preview, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return dict(rows=prepared, preview=preview, token=token)

    def preview_roster(self, rows):
        with self.store.connect() as connection:
            self._editable(connection)
            return self._roster_plan(connection, rows)

    def import_roster(self, rows, token):
        with self.store.connect(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self._editable(connection)
            plan = self._roster_plan(connection, rows)
            if plan['token'] != token:
                raise ValueError('预览后的名单已变化，请重新读取文件并预览。')
            self.store._import_students(connection, plan['rows'])
            state['roster'] = 'imported'
            self._save(connection, state)
            return len(plan['rows'])

    def finish(self):
        with self.store.connect(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self._read(connection)
            if state and state['completed']:
                return False
            state = self._editable(connection)
            if 'pending' in (state['camera'], state['roster']):
                raise ValueError('请先确认摄像头测试与名单步骤，或明确选择跳过。')
            state['completed'] = True
            self._save(connection, state)
            self.store._audit(connection, '完成首次使用向导', detail=f"摄像头：{state['camera']}；名单：{state['roster']}")
            return True
