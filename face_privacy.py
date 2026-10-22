"""经过口令复核的样本删除。保留考勤时间证据，不保存生物图像副本。"""

import shutil
import tempfile
from pathlib import Path

from app_config import CAMERA_LOCK_PATH, DATA_LOCK_PATH, DATASET_DIR, MODEL_PATH, sample_paths, sample_student_id
from app_errors import ValidationError
from resource_lock import ResourceLease
from task_runtime import checkpoint
from train import manifest_path, train_model, training_paths


def delete_face_data(store, password, student_id=None, *, clear_all=False, dataset=DATASET_DIR,
                     model=MODEL_PATH, camera_lock=CAMERA_LOCK_PATH, data_lock=DATA_LOCK_PATH):
    """删除指定学生或全部样本并重建；文件变更失败时恢复旧模型和样本，写独立业务审计。"""
    if not clear_all and not student_id:
        raise ValidationError("请先选择学生。")
    if not store.authenticate(password):
        raise ValidationError("管理密码不正确。")
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
        old_model = model.read_bytes() if model.exists() else None
        info_path = manifest_path(model)
        old_manifest = info_path.read_bytes() if info_path.exists() else None
        # 仅在当前样本目录下创建已验证的临时位置，结束时不留下被删除样本副本。
        staging=Path(tempfile.mkdtemp(prefix=".face-delete-",dir=dataset)).resolve()
        if staging.parent != dataset:
            raise RuntimeError("无效临时目录")
        moved=[]
        try:
            for path in paths:
                checkpoint()
                destination=staging/path.name
                path.replace(destination)
                moved.append((path,destination))
            active={row["student_id"] for row in store.students(active_only=True)}
            if student_id: active.discard(student_id)
            checkpoint()
            if not clear_all and training_paths(dataset,active):
                train_model(dataset,model,active,_lease=False)
            else:
                model.unlink(missing_ok=True)
                info_path.unlink(missing_ok=True)
            with store.connect(write=True) as connection:
                connection.execute("BEGIN IMMEDIATE")
                store._audit(connection,"清空全部训练数据" if clear_all else "删除人脸样本并重建模型",
                             student_id=student_id,detail=f"移除 {len(moved)} 张样本；保留考勤时间证据")
            # 后续删除是已提交事务的清理，不因取消留下包含生物图像的临时目录。
        except BaseException:
            for path, destination in moved:
                destination.replace(path)
            if old_model is None: model.unlink(missing_ok=True)
            else: model.write_bytes(old_model)
            if old_manifest is None: info_path.unlink(missing_ok=True)
            else: info_path.write_bytes(old_manifest)
            raise
        finally:
            shutil.rmtree(staging)
        return len(moved)
