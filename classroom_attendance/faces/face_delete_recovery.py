"""人脸删除的持久化意图：提交前恢复原件，提交后清理已确认的暂存文件。"""

import json
import os
import re
import uuid
from pathlib import Path

from classroom_attendance.core.app_config import IMAGE_EXTENSIONS, sample_student_id
from classroom_attendance.core.app_errors import ValidationError
from classroom_attendance.core.file_integrity import file_sha256
from classroom_attendance.core.resource_lock import ResourceLease
from classroom_attendance.core.task_runtime import current_control

COMMIT_KEY = 'face_deletion_commit_v1'
INTENT = '.intent.json'
MODEL_NAMES = {'face_model.yml', 'model_manifest.json', 'face_model.manifest.json'}


def assert_no_face_deletion(dataset):
    if any(Path(dataset).glob('.face-delete-*')):
        raise ValidationError('发现未完成的人脸删除，请关闭其他窗口并重启程序恢复；旧版暂存资料需人工核对。')


def _checksum(path):
    return file_sha256(path, use_cache=False) if path.exists() else None


def _write_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _copy(source, target):
    # 原子安装所用的临时模型也留在有恢复意图的目录中，避免硬退出留下散落副本。
    scratch = source.parent if source.parent.name.startswith('.face-delete-') else target.parent
    temporary = scratch/('.copy-' + uuid.uuid4().hex + '.tmp')
    try:
        with source.open('rb') as reader, temporary.open('xb') as writer:
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_deletion(store, dataset, model, manifest, paths):
    directory = store.path.parent.resolve()
    if (dataset != directory/'dataset' or model.parent != directory or manifest.parent != directory
            or model.name not in MODEL_NAMES or manifest.name not in MODEL_NAMES
            or dataset.is_symlink() or dataset.resolve() != dataset
            or model.is_symlink() or manifest.is_symlink()):
        raise ValidationError('人脸删除范围必须是当前数据库同目录的普通样本与模型文件。')
    assert_no_face_deletion(dataset)
    for file in paths:
        if file.is_symlink() or file.parent != dataset:
            raise ValidationError('人脸样本必须是当前目录内的普通文件。')
        sample_student_id(file)
    identifier = uuid.uuid4().hex
    folder = dataset/('.face-delete-'+identifier)
    plan = {'version': 1, 'id': identifier,
            'samples': [{'name': file.name, 'sha256': _checksum(file)} for file in paths],
            'models': [{'name': file.name, 'backup': backup, 'sha256': _checksum(file)}
                       for file, backup in ((model, 'previous-model.bin'), (manifest, 'previous-manifest.bin'))],
            'outputs': {}}
    if any(file.is_symlink() for file in paths):
        raise ValidationError('人脸样本不能是链接，未开始删除。')
    folder.mkdir()
    try:
        for item in plan['models']:
            if item['sha256'] is not None:
                _copy(directory/item['name'], folder/item['backup'])
        _write_json(folder/INTENT, plan)  # 持久化后才能移动第一份样本。
    except BaseException:
        for file in folder.iterdir():
            file.unlink()
        folder.rmdir()
        raise
    return folder, plan


def record_outputs(folder, plan, model, manifest):
    plan['outputs'] = {item['name']: _checksum(file)
                       for item, file in zip(plan['models'], (model, manifest))}
    _write_json(folder/INTENT, plan)


def install_outputs(directory, folder, plan):
    for item, staged in zip(plan['models'], ('next-model.yml', 'next-model.manifest.json')):
        target = directory/item['name']
        if plan['outputs'][item['name']] is None:
            target.unlink(missing_ok=True)
        else:
            _copy(folder/staged, target)


def _load_plan(store, folder):
    directory = store.path.parent.resolve()
    if (folder.parent != directory/'dataset' or folder.resolve() != folder or folder.is_symlink()
            or not re.fullmatch(r'\.face-delete-[0-9a-f]{32}', folder.name)):
        raise ValidationError('删除暂存目录无效，已保留资料，请人工核对。')
    try:
        intent = folder/INTENT
        if intent.is_symlink() or intent.stat().st_size > 32*1024**2:
            raise ValueError()
        plan = json.loads(intent.read_text(encoding='utf-8'))
        if plan['version'] != 1 or folder.name != '.face-delete-'+plan['id']:
            raise ValueError()
        models, samples, outputs = plan['models'], plan['samples'], plan['outputs']
        if (not isinstance(samples, list) or len(models) != 2 or not isinstance(outputs, dict)
                or [item['backup'] for item in models] != ['previous-model.bin', 'previous-manifest.bin']
                or len({item['name'] for item in models}) != 2):
            raise ValueError()
        names = {INTENT, INTENT+'.tmp', 'next-model.yml', 'next-model.manifest.json'}
        for item in samples + models:
            name, checksum = item['name'], item['sha256']
            if (not isinstance(name, str) or not name or name.startswith('.')
                    or any(c in name for c in '/\\:') or Path(name).name != name
                    or (checksum is not None and not re.fullmatch('[0-9a-f]{64}', checksum))):
                raise ValueError()
        for item in models:
            if item['name'] not in MODEL_NAMES:
                raise ValueError()
            names.add(item['backup'])
        for item in samples:
            if item['name'] in names or item['sha256'] is None or Path(item['name']).suffix.lower() not in IMAGE_EXTENSIONS:
                raise ValueError()
            sample_student_id(Path(item['name']))
            names.add(item['name'])
        if set(outputs) not in (set(), {item['name'] for item in models}):
            raise ValueError()
        if any(value is not None and not re.fullmatch('[0-9a-f]{64}', value) for value in outputs.values()):
            raise ValueError()
        if any((file.name not in names and not re.fullmatch(r'\.copy-[0-9a-f]{32}\.tmp', file.name))
               or file.is_symlink() or not file.is_file() for file in folder.iterdir()):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValidationError('删除暂存清单缺失或不可读，已保留资料，请人工核对或恢复备份。') from error
    return plan


def recover_deletion(store, folder):
    """调用方持有相机/样本锁。恢复不因原任务的取消令牌再次中断。"""
    token = current_control.set(None)
    try:
        plan = _load_plan(store, folder)
        directory, dataset = store.path.parent.resolve(), folder.parent
        with store.connect() as connection:
            committed = store._get(connection, COMMIT_KEY) == plan['id']
        # 先完整校验全部文件，遇到变化时不覆盖或删除未知内容。
        for item in plan['samples']:
            staged, source = folder/item['name'], dataset/item['name']
            if staged.exists() and _checksum(staged) != item['sha256']:
                raise ValidationError('删除暂存样本校验失败，已停止恢复，请人工核对。')
            if not committed and (source.is_symlink() or (source.exists() and _checksum(source) != item['sha256'])
                                  or not (source.exists() or staged.exists())):
                raise ValidationError('原样本已变化或缺失，已停止恢复，请人工核对。')
        for item in plan['models']:
            source, backup = directory/item['name'], folder/item['backup']
            if source.is_symlink() or (backup.exists() and _checksum(backup) != item['sha256']):
                raise ValidationError('模型恢复文件校验失败，已保留资料。')
            if not committed:
                current = _checksum(source)
                allowed = {item['sha256'], plan['outputs'].get(item['name'])}
                if current not in allowed or (item['sha256'] is not None and not backup.exists() and current != item['sha256']):
                    raise ValidationError('原模型已变化或恢复副本缺失，请人工核对。')
        if not committed:
            for item in plan['samples']:
                staged, source = folder/item['name'], dataset/item['name']
                if not source.exists():
                    staged.replace(source)
            for item in plan['models']:
                target, backup = directory/item['name'], folder/item['backup']
                if item['sha256'] is None:
                    target.unlink(missing_ok=True)
                elif _checksum(target) != item['sha256']:
                    _copy(backup, target)
        # 只移除经过清单校验的普通文件，最后移除意图，允许清理中断后再次进入。
        for file in folder.iterdir():
            if file.name != INTENT:
                file.unlink()
        (folder/INTENT).unlink()
        folder.rmdir()
        return committed
    finally:
        current_control.reset(token)


def recover_face_deletions(store):
    directory = store.path.parent.resolve()
    dataset = directory/'dataset'
    folders = sorted(dataset.glob('.face-delete-*'))
    if not folders:
        return
    if dataset.is_symlink() or dataset.resolve() != dataset:
        raise ValidationError('删除恢复样本目录不能是链接，请人工核对。')
    with ResourceLease(directory/'.camera.lock'), ResourceLease(directory/'.samples.lock'):
        for folder in folders:
            if folder.resolve() != folder or folder.is_symlink():
                raise ValidationError('删除暂存目录不能是链接，请人工核对。')
            if folder.is_dir() and not folder.is_symlink() and not any(folder.iterdir()):
                folder.rmdir()
            else:
                recover_deletion(store, folder)
