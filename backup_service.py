"""可校验的完整备份与离线恢复；恢复前保留原数据，崩溃后按日志回退。"""

import hashlib
import json
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

from app_config import APP_VERSION, IMAGE_EXTENSIONS, sample_student_id
from file_integrity import file_sha256
from resource_lock import ResourceLease
from runtime_guard import MaintenanceLease
from task_runtime import checkpoint
from session_times import SCHEMA_VERSION, resolve_session_times
from maintenance_tools import require_disk_space, backup_space_required

FORMAT_VERSION = 2
SUPPORTED_SCHEMAS = (3, 4, SCHEMA_VERSION)
MANAGED_NAMES = ("attendance.db", "attendance.db-wal", "attendance.db-shm", "dataset",
                 "face_model.yml", "model_manifest.json", "restore_status.json")
JOURNAL_NAME = ".restore-journal.json"
MAX_FILES = 50000
MAX_UNPACKED_BYTES = 8 * 1024**3
MAX_INFO_BYTES = 32 * 1024**2


class SnapshotSource:
    def __init__(self, database):
        self.path = Path(database)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            yield connection


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _write_json(path, value):
    import os
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def database_schema(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
        row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if not row or row[0] not in {str(version) for version in SUPPORTED_SCHEMAS}:
            raise ValueError('此数据库版本暂不支持恢复，请使用对应版本的软件处理。')
        return int(row[0])


def database_summary(database):
    """只读检查备份库，不创建数据库，也不迁移未知版本。"""
    database = Path(database)
    if not database.is_file():
        raise ValueError("备份缺少考勤数据库。")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("备份数据库完整性检查未通过。")
        required = {"students", "sessions", "session_students", "checkins", "face_evidence", "metadata", "audit_log"}
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            raise ValueError("备份数据库缺少必要的业务表。")
        version = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if not version or version[0] not in {str(value) for value in SUPPORTED_SCHEMAS}:
            raise ValueError("此数据库版本暂不支持恢复，请使用对应版本的软件处理。")
        columns = {row[1] for row in connection.execute('PRAGMA table_info(sessions)')}
        if int(version[0]) >= 4 and 'opens_at' not in columns:
            raise ValueError('备份缺少开放签到时间，数据库结构与版本不符。')
        query = 'SELECT opens_at,starts_at,late_at,ends_at FROM sessions' if int(version[0]) >= 4 else 'SELECT starts_at,starts_at,late_at,ends_at FROM sessions'
        if int(version[0]) >= 5:
            from face_lifecycle import validate_lifecycle_database
            validate_lifecycle_database(connection)
        for opens_at, starts_at, late_at, ends_at in connection.execute(query):
            checkpoint()
            try:
                resolve_session_times(datetime.fromisoformat(starts_at), datetime.fromisoformat(ends_at),
                                      opens_at=datetime.fromisoformat(opens_at), late_at=datetime.fromisoformat(late_at))
            except (ValueError, TypeError):
                raise ValueError('备份中的课次时间无效或顺序错误。') from None
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("备份数据库的关联记录检查未通过。")
        return {"students": connection.execute("SELECT COUNT(*) FROM students").fetchone()[0],
                "sessions": connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "checkins": connection.execute("SELECT COUNT(*) FROM checkins").fetchone()[0]}


def _model_state(folder, *, require_authorization=True):
    from train import model_health
    from face_lifecycle import authorized_ids
    with closing(sqlite3.connect(folder/'attendance.db')) as connection:
        version = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        consented = authorized_ids(connection) if version and version[0] == '5' else set()
    ready, _, manifest = model_health(model=folder / "face_model.yml", dataset=folder / "dataset",
                                      manifest_file=folder / "model_manifest.json", fresh=True, consented_ids=consented if require_authorization else None)
    if ready:
        from face_utils import load_recognizer, cv2, np
        try:
            recognizer = load_recognizer(folder / 'face_model.yml')
            identities = {recognizer.getLabelInfo(int(value)) for value in np.unique(recognizer.getLabels())}
            ready = identities == set(manifest['student_ids'])
        except (OSError, ValueError, TypeError, RuntimeError, cv2.error):
            ready = False
    return ready, manifest


def create_backup(store, path, *, model, manifest, dataset):
    """生成 v2 清单；模型原件始终保留，只有有效模型才标记可直接使用。"""
    path, model, manifest, dataset = map(Path, (path, model, manifest, dataset))
    from face_delete_recovery import assert_no_face_deletion
    assert_no_face_deletion(dataset)
    if path.suffix.lower() != ".zip":
        raise ValueError("备份文件必须使用 .zip 扩展名。")
    if path.resolve() in {store.path.resolve(), model.resolve(), manifest.resolve()} or path.resolve().is_relative_to(dataset.resolve()):
        raise ValueError("备份位置不能覆盖运行数据或放在人脸样本目录内。")
    required = backup_space_required((store.path, Path(str(store.path) + '-wal'), model, manifest), dataset)
    require_disk_space(path.parent, required)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".backup-build-", dir=path.parent) as temporary:
        folder = Path(temporary)
        database = folder / "attendance.db"
        with store.connect() as source, closing(sqlite3.connect(database)) as target:
            source.backup(target, pages=128, progress=lambda *args: checkpoint(), sleep=.05)
        for file, name in ((model, "face_model.yml"), (manifest, "model_manifest.json")):
            if file.exists():
                shutil.copy2(file, folder / name)
        if dataset.exists():
            (folder / "dataset").mkdir()
            for file in sorted(dataset.iterdir()):
                checkpoint()
                if file.is_file() and not file.name.startswith("."):
                    _allowed_member("dataset/" + file.name)
                    shutil.copy2(file, folder / "dataset" / file.name)
        ready, model_info = _model_state(folder)
        if ready and model_info["version"] == 2:
            from train import dataset_signature, training_paths
            model_info.update(version=3, signature_algorithm="sha256-content-v1",
                              dataset_signature=dataset_signature(training_paths(folder / "dataset", set(model_info["student_ids"])), use_cache=False))
            _write_json(folder / "model_manifest.json", model_info)
        files = {file.relative_to(folder).as_posix(): {"size": file.stat().st_size,
                 "sha256": file_sha256(file, use_cache=False)} for file in sorted(folder.rglob("*")) if file.is_file()}
        info = {"format_version": FORMAT_VERSION, "app_version": APP_VERSION, "created_at": _now(),
                "schema_version": database_schema(database), "summary": database_summary(database), "files": files,
                "model_status": "ready" if ready else "needs_training"}
        _write_json(folder / "backup_info.json", info)
        output = folder / "output.zip"
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in list(files) + ["backup_info.json"]:
                checkpoint()
                archive.write(folder / name, name)
        checkpoint()
        output.replace(path)
    return info


def _allowed_member(name):
    if ("\\" in name or "\x00" in name or PurePosixPath(name).is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ValueError("备份含不安全的文件路径。")
    if name in {"attendance.db", "face_model.yml", "model_manifest.json", "backup_info.json"}:
        return
    parts = name.split("/")
    if len(parts) != 2 or parts[0] != "dataset":
        raise ValueError("备份含未允许的文件。")
    leaf = parts[1]
    reserved = {'CON','PRN','AUX','NUL'} | {f'{prefix}{number}' for prefix in ('COM','LPT') for number in range(1,10)}
    if (any(c in leaf for c in '<>:"|?*') or leaf.endswith((" ", "."))
            or leaf.split('.')[0].upper() in reserved
            or Path(leaf).suffix.lower() not in IMAGE_EXTENSIONS):
        raise ValueError("备份含不支持的样本文件名。")
    sample_student_id(Path(leaf))


def _extract_verified(path, folder):
    try:
        snapshot = folder / ".input.zip"
        digest = hashlib.sha256()
        if Path(path).stat().st_size > MAX_UNPACKED_BYTES:
            raise ValueError("备份压缩包超过恢复大小限制。")
        with Path(path).open("rb") as source, snapshot.open("xb") as destination:
            copied = 0
            while chunk := source.read(1024 * 1024):
                checkpoint()
                copied += len(chunk)
                if copied > MAX_UNPACKED_BYTES:
                    raise ValueError("备份压缩包超过恢复大小限制。")
                digest.update(chunk)
                destination.write(chunk)
        archive_sha256 = digest.hexdigest()
        with zipfile.ZipFile(snapshot) as archive:
            members = archive.infolist()
            if len(members) > MAX_FILES or sum(item.file_size for item in members) > MAX_UNPACKED_BYTES:
                raise ValueError("备份文件数量或解压后大小超过恢复限制。")
            if shutil.disk_usage(folder).free < sum(item.file_size for item in members) + 64 * 1024**2:
                raise ValueError("磁盘剩余空间不足，无法安全校验与恢复。")
            names, actual = set(), {}
            for item in members:
                name = item.filename
                if item.is_dir() and name == "dataset/":
                    continue
                _allowed_member(name)
                if (name.casefold() in names or item.flag_bits & 1
                        or stat.S_IFMT(item.external_attr >> 16) == stat.S_IFLNK):
                    raise ValueError("备份含重复、加密或链接文件，无法恢复。")
                names.add(name.casefold())
                if name == "backup_info.json" and item.file_size > MAX_INFO_BYTES:
                    raise ValueError("备份清单过大。")
                if name == 'model_manifest.json' and item.file_size > MAX_INFO_BYTES:
                    raise ValueError('模型清单过大。')
                if name == 'face_model.yml' and item.file_size > 512 * 1024**2:
                    raise ValueError('模型文件超过当前恢复大小限制。')
                target = folder / name
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with archive.open(item) as source, target.open("xb") as destination:
                    while chunk := source.read(1024 * 1024):
                        checkpoint()
                        size += len(chunk)
                        if size > item.file_size:
                            raise ValueError("备份解压大小与清单不符。")
                        digest.update(chunk)
                        destination.write(chunk)
                if size != item.file_size:
                    raise ValueError("备份文件未完整解压。")
                actual[name] = {"size": size, "sha256": digest.hexdigest()}
        if "backup_info.json" not in actual or "attendance.db" not in actual:
            raise ValueError("备份缺少数据库或备份清单。")
        info = json.loads((folder / "backup_info.json").read_text(encoding="utf-8"))
        if not isinstance(info, dict):
            raise ValueError("备份清单格式无效。")
        version = info.get("format_version", 1)
        if type(version) is not int or version not in (1, FORMAT_VERSION):
            raise ValueError("备份格式版本暂不支持。")
        legacy = version == 1
        if not legacy:
            expected = info.get("files")
            actual.pop("backup_info.json")
            if (not isinstance(expected, dict) or expected != actual
                    or any(not isinstance(value, dict) or type(value.get('size')) is not int
                           or not isinstance(value.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}',value['sha256'])
                           for value in expected.values())):
                raise ValueError("备份文件清单、大小或 SHA-256 校验未通过。")
        summary = database_summary(folder / "attendance.db")
        schema = database_schema(folder / 'attendance.db')
        if type(info.get('schema_version')) is not int or info['schema_version'] != schema or (not legacy and info.get("summary") != summary):
            raise ValueError("备份中的数据库版本或数据概要与清单不符。")
        created = info.get("created_at")
        if not isinstance(created, str):
            raise ValueError("备份创建时间无效。")
        datetime.fromisoformat(created)
        ready = False
        if not legacy and info.get("model_status") == "ready":
            ready, manifest = _model_state(folder)
            if not ready or manifest.get("version") != 3:
                raise ValueError("备份中的模型或样本内容校验未通过。")
        sample_count = len(list((folder / "dataset").glob("*")))
        return {"format_version": version, "schema_version": schema, "created_at": created, **summary, "samples": sample_count,
                "legacy": legacy, "model_ready": ready,
                "warning": "旧备份缺少逐文件哈希，恢复后须重新训练模型。" if legacy else
                           "备份中的模型未就绪，恢复后须重新训练模型。" if not ready else "",
                "archive_sha256": archive_sha256}
    except (zipfile.BadZipFile, UnicodeError, KeyError, TypeError, sqlite3.Error) as error:
        raise ValueError("备份损坏、数据库无效或清单格式不正确，未改动当前数据。") from error


def inspect_backup(path):
    path = Path(path)
    before = (path.stat().st_size, path.stat().st_mtime_ns)
    with tempfile.TemporaryDirectory(prefix="classroom-backup-check-") as temporary:
        summary = _extract_verified(path, Path(temporary))
    if before != (path.stat().st_size, path.stat().st_mtime_ns):
        raise ValueError("校验期间备份发生变化，请重新选择。")
    return summary


def _journal_paths(directory, journal):
    stage, previous = journal.get("stage", ""), journal.get("previous", "")
    if (journal.get("version") != 1 or journal.get("phase") not in {"moving", "committed"}
            or not re.fullmatch(r"\.restore-stage-[0-9a-f]{32}", stage)
            or not re.fullmatch(r"backups/pre_restore_[0-9a-f]{32}", previous)
            or not isinstance(journal.get("original"), list)
            or not set(journal["original"]) <= set(MANAGED_NAMES)):
        raise ValueError("恢复日志无效，请保留数据目录并联系维护人员。")
    paths = directory / stage, directory / previous
    if any(not path.resolve().is_relative_to(directory.resolve()) for path in paths):
        raise ValueError("恢复日志指向了数据目录以外的位置。")
    return paths


def _remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _recover_journal(directory):
    path = directory / JOURNAL_NAME
    if not path.exists():
        return False
    journal = json.loads(path.read_text(encoding="utf-8"))
    stage, previous = _journal_paths(directory, journal)
    if journal["phase"] == "moving":
        for name in MANAGED_NAMES:
            saved, current = previous / name, directory / name
            if name not in journal["original"]:
                _remove(current)
            elif saved.exists() or saved.is_symlink():
                _remove(current)
                saved.replace(current)
    else:
        database_summary(directory / "attendance.db")
    _remove(stage)
    path.unlink()
    return True


def recover_interrupted(directory):
    directory = Path(directory).resolve()
    with MaintenanceLease(directory):
        return _recover_journal(directory)


def restore_backup(path, directory, *, expected_sha256, allow_legacy=False, authorize=None, progress=None):
    """完整校验后进入维护状态；替换阶段不接受取消，中断由启动检查回退。"""
    directory, path = Path(directory).resolve(), Path(path).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    progress = progress or (lambda message: None)
    token = uuid.uuid4().hex
    stage = directory / (".restore-stage-" + token)
    previous = directory / "backups" / ("pre_restore_" + token)
    stage.mkdir()
    journal_written = False
    committed = False
    try:
        progress("正在重新校验备份，尚未替换当前数据…")
        summary = _extract_verified(path, stage)
        if summary["archive_sha256"] != expected_sha256:
            raise ValueError("备份已改变，请重新校验后再恢复。")
        if summary["legacy"] and not allow_legacy:
            raise ValueError("旧备份没有逐文件哈希，请明确确认限制后再恢复。")
        if not summary["model_ready"]:
            for name in ("face_model.yml", "model_manifest.json"):
                (stage / name).unlink(missing_ok=True)
        with closing(sqlite3.connect(stage / 'attendance.db')) as connection:
            with connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                cutoff = connection.execute('SELECT COALESCE(MAX(id),0) FROM face_authorizations').fetchone()[0] if 'face_authorizations' in tables else 0
                connection.execute("INSERT INTO metadata(key,value) VALUES('face_restore_review_after',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(cutoff),))
                connection.execute('INSERT INTO audit_log(created_at,action,detail,actor_label,actor_kind) VALUES(?,?,?,?,?)',
                                   (_now(),'恢复完整备份','备份文件与数据库已校验；人脸授权需逐人重新复核，避免旧备份复活已撤回授权。本机恢复入口操作，不代表独立教师身份。','','management_entry'))
        summary['source_model_ready'] = summary['model_ready']
        summary['authorization_review_required'] = True
        summary['model_ready'] = False
        with MaintenanceLease(directory), ResourceLease(directory / ".camera.lock"), ResourceLease(directory / ".samples.lock"):
            _recover_journal(directory)
            if authorize is not None:
                authorize(directory)
            checkpoint()
            _write_json(stage / "restore_status.json", {"verified_at": _now(), "archive_sha256": expected_sha256,
                        "format_version": summary["format_version"], "model_ready": summary["model_ready"]})
            if previous.resolve().is_relative_to(directory) is False:
                raise ValueError("恢复前快照位置不在数据目录内。")
            previous.mkdir(parents=True)
            original = [name for name in MANAGED_NAMES if (directory / name).exists() or (directory / name).is_symlink()]
            journal = {"version": 1, "phase": "moving", "stage": stage.name,
                       "previous": previous.relative_to(directory).as_posix(), "original": original}
            _write_json(directory / JOURNAL_NAME, journal)
            journal_written = True
            progress("正在保存恢复前快照并替换数据；请勿关闭电脑…")
            # 恢复窗口不提供中途取消；以下操作的异常统一回退，避免留下半套运行数据。
            for name in original:
                (directory / name).replace(previous / name)
            previous_backup = previous.with_suffix('.zip')
            if (previous / 'attendance.db').exists():
                try:
                    create_backup(SnapshotSource(previous / 'attendance.db'), previous_backup,
                                  model=previous / 'face_model.yml', manifest=previous / 'model_manifest.json',
                                  dataset=previous / 'dataset')
                except (ValueError, sqlite3.DatabaseError):
                    # 原库损坏时无法生成可导入的 ZIP；原始文件快照仍完整保留。
                    previous_backup = None
            else:
                previous_backup = None
            for name in MANAGED_NAMES:
                if (stage / name).exists():
                    (stage / name).replace(directory / name)
            database_summary(directory / "attendance.db")
            if summary['source_model_ready'] and not _model_state(directory, require_authorization=False)[0]:
                raise ValueError("恢复后的模型校验未通过。")
            journal["phase"] = "committed"
            _write_json(directory / JOURNAL_NAME, journal)
            committed = True
            try:
                _recover_journal(directory)
            except OSError:
                summary["warning"] = "数据已恢复；临时文件清理尚未完成，下次启动会继续处理。"
        return {**summary, "previous_snapshot": str(previous),
                "previous_backup": str(previous_backup) if previous_backup else None}
    except BaseException:
        if journal_written and not committed:
            # 失败回退仍在本进程内；新启动入口看到日志也会拒绝直接使用半套数据。
            with MaintenanceLease(directory):
                _recover_journal(directory)
        raise
    finally:
        if not (directory / JOURNAL_NAME).exists():
            _remove(stage)
