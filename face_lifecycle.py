"""告知、授权、撤回与可续办的人脸资料清理；旧资料不推定获得授权。"""

import hashlib
import json
import re
import uuid
from datetime import date, datetime
from pathlib import Path

from app_config import IMAGE_EXTENSIONS, sample_student_id
from app_errors import ValidationError
from file_integrity import file_sha256
from resource_lock import ResourceLease
from task_runtime import checkpoint

STATE_TEXT = {'missing': '未登记授权', 'valid': '授权有效', 'expired': '已到期',
              'revoked': '已撤回', 'outdated': '告知已更新，需重新授权', 'not_yet': '授权尚未生效',
              'restore_review': '恢复后待复核授权'}
MODEL_NAMES = ('face_model.yml', 'model_manifest.json')


def initialize_schema(connection):
    statements = [
        '''CREATE TABLE IF NOT EXISTS face_notices(id INTEGER PRIMARY KEY, version TEXT NOT NULL UNIQUE,
           text TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS face_authorizations(id INTEGER PRIMARY KEY,
           student_id TEXT NOT NULL REFERENCES students(student_id), action TEXT NOT NULL CHECK(action IN ('grant','revoke')),
           notice_id INTEGER REFERENCES face_notices(id), expires_on TEXT, basis TEXT NOT NULL,
           reference TEXT NOT NULL, created_at TEXT NOT NULL)''',
        '''CREATE INDEX IF NOT EXISTS idx_face_authorizations_student ON face_authorizations(student_id,id)''',
        '''CREATE TABLE IF NOT EXISTS face_cleanup_jobs(id TEXT PRIMARY KEY, state TEXT NOT NULL,
           plan_json TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT)''',
    ]
    for table in ('face_notices', 'face_authorizations'):
        for operation in ('UPDATE', 'DELETE'):
            statements.append(f'''CREATE TRIGGER IF NOT EXISTS preserve_{table}_{operation.lower()}
                BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'告知和授权记录不可覆盖或删除'); END''')
    for statement in statements:
        connection.execute(statement)


def local_now(now=None):
    value = now or datetime.now()
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValidationError('请使用不含时区的本地日期时间。')
    return value.replace(microsecond=0)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def current_notice(connection):
    row = connection.execute('SELECT id,version,text,sha256,created_at FROM face_notices ORDER BY id DESC LIMIT 1').fetchone()
    if not row:
        return None
    notice = dict(zip(('id', 'version', 'text', 'sha256', 'created_at'), row))
    if hashlib.sha256(notice['text'].encode('utf-8')).hexdigest() != notice['sha256']:
        raise ValidationError('告知内容校验失败，已停止人脸使用，请检查数据或恢复备份。')
    return notice


def pending_job(connection):
    return connection.execute("SELECT id FROM face_cleanup_jobs WHERE state!='completed' ORDER BY created_at,id LIMIT 1").fetchone()


def authorization_rows(connection, now=None):
    now = local_now(now)
    notice = current_notice(connection)
    cutoff_row = connection.execute("SELECT value FROM metadata WHERE key='face_restore_review_after'").fetchone()
    try:
        cutoff = int(cutoff_row[0]) if cutoff_row else 0
        if cutoff < 0:
            raise ValueError()
    except (ValueError, TypeError) as error:
        raise ValidationError('恢复后的授权复核标记不可读，已停止人脸使用。') from error
    cursor = connection.execute('''SELECT s.student_id,s.name,s.group_name,s.active,a.id AS event_id,a.action,
        a.notice_id,a.expires_on,a.basis,a.reference,a.created_at FROM students s
        LEFT JOIN face_authorizations a ON a.id=(SELECT MAX(latest.id) FROM face_authorizations latest
            WHERE latest.student_id=s.student_id) ORDER BY s.student_id''')
    rows = [dict(zip([column[0] for column in cursor.description], row)) for row in cursor]
    for row in rows:
        state = 'missing'
        if row['action'] == 'revoke':
            state = 'revoked'
        elif row['action'] == 'grant':
            try:
                expiry = date.fromisoformat(row['expires_on'])
                created = datetime.fromisoformat(row['created_at'])
                if created.tzinfo is not None:
                    raise ValueError()
            except (TypeError, ValueError) as error:
                raise ValidationError('授权记录不可读，已停止人脸使用，请检查数据或恢复备份。') from error
            state = ('outdated' if not notice or row['notice_id'] != notice['id'] else
                     'not_yet' if now < created else 'expired' if now.date() > expiry else
                     'restore_review' if row['event_id'] <= cutoff else 'valid')
        row['state'] = state
        row['expiring_soon'] = bool(state == 'valid' and 0 <= (date.fromisoformat(row['expires_on'])-now.date()).days <= 14)
    return notice, rows


def authorized_ids(connection, now=None):
    if pending_job(connection):
        return set()
    return {row['student_id'] for row in authorization_rows(connection, now)[1] if row['state'] == 'valid'}


def require_authorized(connection, student_id, now=None):
    if pending_job(connection):
        raise ValidationError('人脸资料清理尚未完成，请教师续办清理；当前可使用人工点名。')
    row = next((row for row in authorization_rows(connection, now)[1] if row['student_id'] == student_id), None)
    if row is None or row['state'] != 'valid':
        raise ValidationError('该学生没有当前有效的人脸授权，已停止采集和刷脸，请教师核对授权或使用人工点名。')


def validate_lifecycle_database(connection):
    required = {'face_notices', 'face_authorizations', 'face_cleanup_jobs'}
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= tables:
        raise ValidationError('备份缺少人脸生命周期记录，结构与版本不符。')
    for version, text, checksum, created_at in connection.execute('SELECT version,text,sha256,created_at FROM face_notices'):
        if not version or not text or hashlib.sha256(text.encode('utf-8')).hexdigest() != checksum:
            raise ValidationError('备份中的告知内容校验失败。')
        local_now(datetime.fromisoformat(created_at))
    authorization_rows(connection)
    if pending_job(connection):
        raise ValidationError('人脸资料清理尚未完成，请先在原数据目录续办清理，再备份或恢复。')


class FaceLifecycleService:
    def __init__(self, store):
        self.store = store
        self.directory = store.path.parent.resolve()

    def snapshot(self, now=None):
        with self.store.connect() as connection:
            connection.execute('BEGIN')
            notice, rows = authorization_rows(connection, now)
            pending = pending_job(connection)
        return {'notice': notice, 'students': rows, 'pending_job': pending[0] if pending else None,
                'blocked': sum(row['state'] != 'valid' for row in rows),
                'expiring_soon': sum(row['expiring_soon'] for row in rows)}

    def eligible_ids(self, now=None):
        with self.store.connect() as connection:
            return authorized_ids(connection, now)

    def require(self, student_id, now=None):
        with self.store.connect() as connection:
            require_authorized(connection, student_id, now)

    def publish_notice(self, version, text, *, confirmed=False, now=None):
        version, text = version.strip(), text.strip()
        if not confirmed or not 1 <= len(version) <= 60 or not 20 <= len(text) <= 12000:
            raise ValidationError('请填写告知版本（1–60 字）和完整正文（20–12000 字），并确认已由学校核定。')
        with self.store.connect(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            if pending_job(connection):
                raise ValidationError('请先续办人脸清理，再发布告知或登记授权。')
            if connection.execute('SELECT 1 FROM face_notices WHERE version=?', (version,)).fetchone():
                raise ValidationError('该告知版本已存在，请用新版本发布，历史正文不可覆盖。')
            checksum = hashlib.sha256(text.encode('utf-8')).hexdigest()
            connection.execute('INSERT INTO face_notices(version,text,sha256,created_at) VALUES(?,?,?,?)',
                               (version, text, checksum, local_now(now).isoformat()))
            self.store._audit(connection, '发布人脸告知', after={'version': version, 'sha256': checksum})
            return current_notice(connection)

    def authorization_plan(self, student_id, *, now=None):
        with self.store.connect() as connection:
            connection.execute('BEGIN')
            if pending_job(connection):
                raise ValidationError('请先续办人脸清理，再登记授权。')
            notice, rows = authorization_rows(connection, now)
            student = next((row for row in rows if row['student_id'] == student_id), None)
            if student is None or notice is None:
                raise ValidationError('请先发布学校核定的告知，并选择已保存的学生。')
            plan = {'student': student, 'notice': notice}
            return dict(plan, token=digest(plan))

    def grant(self, student_id, token, expires_on, basis, reference, *, confirmed=False, now=None):
        now = local_now(now)
        try:
            expiry = date.fromisoformat(expires_on)
        except (TypeError, ValueError) as error:
            raise ValidationError('保存期限请使用 YYYY-MM-DD 日期。') from error
        basis, reference = basis.strip(), reference.strip()
        if not confirmed or expiry < now.date() or basis not in {'本人明确同意', '监护人明确同意'} or not 1 <= len(reference) <= 300:
            raise ValidationError('请确认已取得本人或监护人明确同意，登记凭据引用，并填写今天或以后的保存截止日。')
        with self.store.connect(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            if pending_job(connection):
                raise ValidationError('请先续办人脸清理，再登记授权。')
            notice, rows = authorization_rows(connection, now)
            student = next((row for row in rows if row['student_id'] == student_id), None)
            if student is None or notice is None or digest({'student': student, 'notice': notice}) != token:
                raise ValidationError('告知或授权状态已变化，请重新查看告知与学生后再登记。')
            connection.execute('''INSERT INTO face_authorizations(student_id,action,notice_id,expires_on,basis,reference,created_at)
                VALUES(?,'grant',?,?,?,?,?)''', (student_id, notice['id'], expires_on, basis, reference, now.isoformat()))
            self.store._audit(connection, '登记人脸授权', student_id=student_id,
                              after={'notice_id': notice['id'], 'expires_on': expires_on, 'basis': basis, 'reference': reference})

    def revoke(self, student_id, reason, *, now=None):
        reason = reason.strip()
        if not 1 <= len(reason) <= 300:
            raise ValidationError('请填写撤回原因或请求凭据引用（1–300 字）。')
        with self.store.connect(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            if not connection.execute('SELECT 1 FROM students WHERE student_id=?', (student_id,)).fetchone():
                raise ValidationError('请选择已保存的学生。')
            connection.execute('''INSERT INTO face_authorizations(student_id,action,basis,reference,created_at)
                VALUES(?,'revoke','撤回请求',?,?)''', (student_id, reason, local_now(now).isoformat()))
            self.store._audit(connection, '撤回人脸授权', student_id=student_id, detail=reason)

    def history(self, student_id):
        with self.store.connect() as connection:
            cursor = connection.execute('''SELECT a.*,n.version,n.sha256 FROM face_authorizations a
                LEFT JOIN face_notices n ON n.id=a.notice_id WHERE student_id=? ORDER BY a.id DESC''', (student_id,))
            return [dict(zip([column[0] for column in cursor.description], row)) for row in cursor]

    def _locks(self):
        return (ResourceLease(self.directory/'.camera.lock', '请先停止所有相机，再预览或清理人脸资料。'),
                ResourceLease(self.directory/'.samples.lock', '正在采集、训练或备份，请稍后再清理。'))

    def _cleanup_plan(self, connection, now=None):
        from face_delete_recovery import assert_no_face_deletion
        assert_no_face_deletion(self.directory/'dataset')
        if pending_job(connection):
            raise ValidationError('已有未完成的人脸清理，请先续办。')
        notice, rows = authorization_rows(connection, now)
        allowed = {row['student_id'] for row in rows if row['state'] == 'valid'}
        dataset = self.directory/'dataset'
        if dataset.is_symlink() or dataset.resolve() != dataset:
            raise ValidationError('样本目录不能是链接，请人工核对数据位置。')
        if any((self.directory/name).is_symlink() for name in MODEL_NAMES):
            raise ValidationError('模型文件不能是链接，请人工核对。')
        files = []
        if dataset.exists():
            for file in sorted(dataset.iterdir()):
                checkpoint()
                if file.is_symlink():
                    raise ValidationError('样本目录存在链接，已停止清理，请人工核对。')
                if not file.is_file() or file.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                try:
                    student_id = sample_student_id(file)
                except ValueError:
                    student_id = None
                if student_id not in allowed:
                    files.append({'kind': 'sample', 'name': file.name, 'sha256': file_sha256(file, use_cache=False)})
        remove_model = bool(files)
        manifest = self.directory/'model_manifest.json'
        if (self.directory/'face_model.yml').exists() or manifest.exists():
            try:
                identities = json.loads(manifest.read_text(encoding='utf-8'))['student_ids']
                remove_model = remove_model or not isinstance(identities, list) or not identities or not set(identities) <= allowed
            except (OSError, ValueError, KeyError, TypeError):
                remove_model = True
        if remove_model:
            for name in MODEL_NAMES:
                file = self.directory/name
                if file.is_symlink():
                    raise ValidationError('模型文件不能是链接，请人工核对。')
                if file.exists():
                    files.append({'kind': 'model', 'name': name, 'sha256': file_sha256(file, use_cache=False)})
        plan = {'files': files, 'notice_id': notice['id'] if notice else None,
                'states': [{key: row[key] for key in ('student_id', 'event_id', 'state', 'expires_on')} for row in rows]}
        return dict(plan, token=digest(plan), sample_count=sum(file['kind'] == 'sample' for file in files),
                    removes_model=any(file['kind'] == 'model' for file in files))

    def preview_cleanup(self, now=None):
        camera_lock, data_lock = self._locks()
        with camera_lock, data_lock, self.store.connect() as connection:
            connection.execute('BEGIN')
            return self._cleanup_plan(connection, now)

    def apply_cleanup(self, password, token, reason, *, now=None):
        if not self.store.authenticate(password):
            raise ValidationError('管理密码不正确。')
        reason = reason.strip()
        if not 1 <= len(reason) <= 300:
            raise ValidationError('请填写清理原因（1–300 字）。')
        camera_lock, data_lock = self._locks()
        with camera_lock, data_lock:
            with self.store.connect(write=True) as connection:
                connection.execute('BEGIN IMMEDIATE')
                plan = self._cleanup_plan(connection, now)
                if not token or token != plan['token'] or not plan['files']:
                    raise ValidationError('资料、授权或期限已变化，或没有可清理文件，请重新预览。')
                job_id = uuid.uuid4().hex
                connection.execute("INSERT INTO face_cleanup_jobs VALUES(?,'prepared',?,?,NULL)",
                                   (job_id, json.dumps(plan, ensure_ascii=False), local_now(now).isoformat()))
                self.store._audit(connection, '确认人脸资料清理', detail=reason,
                                  after={'job_id': job_id, 'samples': plan['sample_count'], 'removes_model': plan['removes_model']})
            return self._execute_cleanup(job_id)

    def resume_cleanup(self, password):
        if not self.store.authenticate(password):
            raise ValidationError('管理密码不正确。')
        camera_lock, data_lock = self._locks()
        with camera_lock, data_lock:
            with self.store.connect() as connection:
                job = pending_job(connection)
            if job is None:
                raise ValidationError('没有需要续办的清理。')
            return self._execute_cleanup(job[0])

    def _execute_cleanup(self, job_id):
        if not re.fullmatch('[0-9a-f]{32}', job_id):
            raise ValidationError('清理任务标识无效。')
        with self.store.connect() as connection:
            row = connection.execute("SELECT plan_json FROM face_cleanup_jobs WHERE id=? AND state='prepared'", (job_id,)).fetchone()
        if not row:
            raise ValidationError('清理任务状态不可读，请人工核对。')
        try:
            plan = json.loads(row[0])
            files = plan['files']
            if not isinstance(files, list) or not files:
                raise ValueError()
            seen = set()
            for item in files:
                kind, name, checksum = item['kind'], item['name'], item['sha256']
                if (kind not in {'sample', 'model'} or not isinstance(name, str) or not name or name.startswith('.')
                        or any(character in name for character in '/\\:') or Path(name).name != name
                        or (kind == 'model' and name not in MODEL_NAMES)
                        or (kind == 'sample' and Path(name).suffix.lower() not in IMAGE_EXTENSIONS)
                        or not re.fullmatch('[0-9a-f]{64}', checksum) or (kind, name) in seen):
                    raise ValueError()
                seen.add((kind, name))
        except (ValueError, TypeError, KeyError) as error:
            raise ValidationError('清理清单无效，未删除文件，请人工核对或恢复备份。') from error
        staging = self.directory/('.privacy-cleanup-'+job_id)
        if (staging.is_symlink() or staging.resolve() != staging
                or (self.directory/'dataset').resolve() != self.directory/'dataset'):
            raise ValidationError('清理目录不能是链接，请人工核对。')
        staging.mkdir(exist_ok=True)
        for item in files:
            checkpoint()
            source = (self.directory/'dataset' if item['kind'] == 'sample' else self.directory)/item['name']
            staged_name = hashlib.sha256(item['name'].encode('utf-8')).hexdigest() if item['kind'] == 'sample' else item['name']
            destination = staging/(item['kind']+'-'+staged_name)
            if source.is_symlink() or destination.is_symlink():
                raise ValidationError('清理文件不能是链接，已停止。')
            if source.exists():
                if destination.exists() or file_sha256(source, use_cache=False) != item['sha256']:
                    raise ValidationError('清理期间文件已改变，未删除变化文件，请人工核对后续办。')
                source.replace(destination)
            if destination.exists():
                if file_sha256(destination, use_cache=False) != item['sha256']:
                    raise ValidationError('暂存清理文件校验失败，已停止，请人工核对。')
                destination.unlink()
        if list(staging.iterdir()):
            raise ValidationError('清理暂存目录出现未知文件，已停止，请人工核对。')
        staging.rmdir()
        with self.store.connect(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute("UPDATE face_cleanup_jobs SET state='completed',completed_at=? WHERE id=? AND state='prepared'",
                               (local_now().isoformat(), job_id))
            self.store._audit(connection, '完成人脸资料清理', after={'job_id': job_id, 'samples': plan['sample_count']})
        return plan['sample_count']
