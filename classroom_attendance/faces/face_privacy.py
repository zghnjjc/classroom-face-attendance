"""经过口令复核的样本删除。保留考勤时间证据，不保存生物图像副本。"""

from pathlib import Path

from classroom_attendance.core.app_config import CAMERA_LOCK_PATH, DATA_LOCK_PATH, DATASET_DIR, MODEL_PATH, sample_paths, sample_student_id
from classroom_attendance.core.app_errors import ValidationError
from classroom_attendance.core.resource_lock import ResourceLease
from classroom_attendance.core.task_runtime import checkpoint
from classroom_attendance.faces.train import manifest_path, train_model, training_paths
from classroom_attendance.faces.face_lifecycle import FaceLifecycleService
from classroom_attendance.faces.face_delete_recovery import (COMMIT_KEY, prepare_deletion, record_outputs, install_outputs,
                                  recover_deletion, recover_face_deletions)


def delete_face_data(store, password, student_id=None, *, clear_all=False, dataset=DATASET_DIR,
                     model=MODEL_PATH, camera_lock=CAMERA_LOCK_PATH, data_lock=DATA_LOCK_PATH):
    """删除指定学生或全部样本并重建；文件变更失败时恢复旧模型和样本，写独立业务审计。"""
    if not clear_all and not student_id:
        raise ValidationError("请先选择学生。")
    if not store.authenticate(password):
        raise ValidationError("管理密码不正确。")
    recover_face_deletions(store)
    lifecycle = FaceLifecycleService(store)
    if lifecycle.snapshot()['pending_job']:
        raise ValidationError('请先在人脸授权页续办未完成的人脸资料清理。')
    dataset, model = Path(dataset).resolve(), Path(model).resolve()
    dataset.mkdir(parents=True, exist_ok=True)
    with ResourceLease(camera_lock), ResourceLease(data_lock):
        paths = []
        for path in sample_paths(dataset):
            try:
                matches = sample_student_id(path) == student_id
            except ValueError:
                matches = False
            if clear_all or matches:
                paths.append(path)
        info_path = manifest_path(model)
        staging, plan = prepare_deletion(store, dataset, model, info_path, paths)
        moved=[]
        try:
            for path in paths:
                checkpoint()
                destination=staging/path.name
                path.replace(destination)
                moved.append((path,destination))
            active={row["student_id"] for row in store.students(active_only=True)} & lifecycle.eligible_ids()
            if student_id: active.discard(student_id)
            checkpoint()
            next_model = staging/'next-model.yml'
            next_manifest = manifest_path(next_model)
            if not clear_all and training_paths(dataset,active):
                train_model(dataset,next_model,active,_lease=False,store=store)
            record_outputs(staging, plan, next_model, next_manifest)
            checkpoint()
            install_outputs(store.path.parent.resolve(), staging, plan)
            with store.connect(write=True) as connection:
                connection.execute("BEGIN IMMEDIATE")
                store._audit(connection,"清空全部训练数据" if clear_all else "删除人脸样本并重建模型",
                             student_id=student_id,detail=f"移除 {len(moved)} 张样本；保留考勤时间证据",
                             after={'deletion_id': plan['id']})
                store._set(connection, COMMIT_KEY, plan['id'])
            # 后续删除是已提交事务的清理，不因取消留下包含生物图像的临时目录。
        except BaseException:
            recover_deletion(store, staging)
            raise
        recover_deletion(store, staging)
        return len(moved)
