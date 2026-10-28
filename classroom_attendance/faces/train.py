"""训练可核对版本的人脸模型，不将停用学生纳入新模型。"""

import argparse
import hashlib
import json
import sys
import tempfile
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from classroom_attendance.core.app_config import DATA_LOCK_PATH, DATASET_DIR, MODEL_MANIFEST_PATH, MODEL_PATH, sample_paths, sample_student_id
from classroom_attendance.storage.attendance_store import AttendanceStore
from classroom_attendance.faces.face_utils import create_recognizer, cv2, np, read_gray, save_recognizer
from classroom_attendance.core.resource_lock import ResourceLease
from classroom_attendance.core.task_runtime import checkpoint
from classroom_attendance.core.app_errors import log_error, user_message
from classroom_attendance.core.file_integrity import file_sha256
from classroom_attendance.maintenance.maintenance_tools import require_disk_space
from classroom_attendance.faces.face_delete_recovery import assert_no_face_deletion


def preprocess(image):
    return cv2.equalizeHist(cv2.resize(image, (160, 160), interpolation=cv2.INTER_AREA))


def dataset_signature(paths, *, version=3, use_cache=True):
    digest = hashlib.sha256()
    for path in sorted(paths):
        if version == 2:
            stat = path.stat()
            digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
        else:
            digest.update(f"{path.name}:{file_sha256(path, use_cache=use_cache)}\n".encode())
    return digest.hexdigest()


def manifest_path(model):
    return MODEL_MANIFEST_PATH if Path(model).resolve() == MODEL_PATH else Path(model).with_suffix(".manifest.json")


def training_paths(dataset=DATASET_DIR, allowed_ids=None):
    paths = []
    for file in sample_paths(Path(dataset)):
        student_id = sample_student_id(file)
        if allowed_ids is None or student_id in allowed_ids:
            paths.append(file)
    return paths


def data_translate(path=DATASET_DIR, allowed_ids=None):
    images, student_ids = [], []
    for file in training_paths(path, allowed_ids):
        checkpoint()
        images.append(preprocess(read_gray(file)))
        student_ids.append(sample_student_id(file))
    if not images:
        raise ValueError("没有可训练的人脸样本，请先为启用的学生采集人脸。")
    return images, student_ids


def train_model(dataset=DATASET_DIR, output=MODEL_PATH, allowed_ids=None, *, _lease=True, store=None):
    with (ResourceLease(DATA_LOCK_PATH, "人脸采集或模型训练正在进行，请先结束当前操作。") if _lease else nullcontext()):
        if _lease:
            assert_no_face_deletion(dataset)
        if store is not None:
            from classroom_attendance.faces.face_lifecycle import FaceLifecycleService
            consented = FaceLifecycleService(store).eligible_ids() & {row['student_id'] for row in store.students(active_only=True)}
            allowed_ids = consented if allowed_ids is None else set(allowed_ids) & consented
            if not allowed_ids:
                raise ValueError('没有当前有效人脸授权的启用学生，请先登记授权；也可使用人工点名。')
        require_disk_space(Path(output).parent)
        checkpoint()
        files = training_paths(dataset, allowed_ids)
        signature = dataset_signature(files, use_cache=False)
        images, student_ids = data_translate(dataset, allowed_ids)
        label_to_id = dict(enumerate(sorted(set(student_ids))))
        id_to_label = {student_id: label for label, student_id in label_to_id.items()}
        recognizer = create_recognizer()
        recognizer.train(images, np.asarray([id_to_label[sid] for sid in student_ids], dtype=np.int32))
        for label, student_id in label_to_id.items():
            recognizer.setLabelInfo(label, student_id)
        if signature != dataset_signature(training_paths(dataset, allowed_ids), use_cache=False):
            raise RuntimeError("训练期间样本发生变化，请重试。")
        if store is not None and not set(student_ids) <= FaceLifecycleService(store).eligible_ids():
            raise ValueError('训练期间授权已变化，请重新检查授权后训练。')
        output=Path(output)
        with tempfile.TemporaryDirectory(prefix=".model-build-",dir=output.parent) as staging:
            staged=Path(staging)/"model.yml"
            checkpoint()
            save_recognizer(recognizer, staged)
            manifest = {"version": 3, "signature_algorithm": "sha256-content-v1", "trained_at": datetime.now().isoformat(timespec="seconds"),
                        "student_ids": sorted(label_to_id.values()), "sample_count": len(images),
                        "dataset_signature": signature, "model_sha256": hashlib.sha256(staged.read_bytes()).hexdigest()}
            destination=manifest_path(output)
            staged_manifest=Path(staging)/"manifest.json"
            staged_manifest.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
            previous_model=output.read_bytes() if output.exists() else None
            previous_manifest=destination.read_bytes() if destination.exists() else None
            checkpoint()
            try:
                staged.replace(output)
                staged_manifest.replace(destination)
            except BaseException:
                if previous_model is not None: output.write_bytes(previous_model)
                else: output.unlink(missing_ok=True)
                if previous_manifest is not None: destination.write_bytes(previous_manifest)
                else: destination.unlink(missing_ok=True)
                raise
    if sys.stdout:
        print(f"训练完成：{len(label_to_id)} 名学生，{len(images)} 张样本。", flush=True)
    return manifest


def model_health(model=MODEL_PATH, dataset=DATASET_DIR, *, fresh=False, manifest_file=None, consented_ids=None):
    """检查模型完整性和授权；当前启用名单由覆盖提示及签到事务独立校验。"""
    info_path = Path(manifest_file) if manifest_file is not None else manifest_path(model)
    if any(Path(dataset).glob('.face-delete-*')):
        return False, '人脸删除尚未恢复，请关闭其他窗口并重启程序核对资料。', None
    if not Path(model).exists() or not info_path.exists():
        return False, "请先到管理端训练模型", None
    try:
        manifest = json.loads(info_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("模型信息应为对象")
        student_ids = manifest["student_ids"]
        if (not isinstance(student_ids, list) or not student_ids
                or any(not isinstance(sid, str) or not sid.isascii() or not sid.isdigit() or len(sid)>24 for sid in student_ids)
                or len(student_ids) != len(set(student_ids))
                or type(manifest["sample_count"]) is not int or manifest["sample_count"] < len(student_ids)):
            raise ValueError("模型学生或样本信息无效")
        for key in ("model_sha256", "dataset_signature"):
            value = manifest[key]
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("模型签名无效")
        if consented_ids is not None and not set(student_ids) <= set(consented_ids):
            return False, '模型含无当前有效授权的人脸，请在“人脸授权”受控清理并重新训练；可先人工点名。', manifest
        version = manifest.get("version")
        if version not in (2, 3) or (version == 3 and manifest.get("signature_algorithm") != "sha256-content-v1") or file_sha256(model, use_cache=not fresh) != manifest["model_sha256"]:
            return False, "模型版本不一致，请重新训练", manifest
        # 验证训练时的身份集合。当前停用、新建或重新启用的档案不改变旧模型的完整性。
        # 未训练身份仅在覆盖提示中列出，资格由识别线程与签到事务再次检查。
        files = training_paths(dataset, set(student_ids))
        if (dataset_signature(files, version=version, use_cache=not fresh) != manifest["dataset_signature"]
                or len(files) != manifest["sample_count"]
                or {sample_student_id(file) for file in files} != set(student_ids)):
            return False, "样本已更新，请重新训练模型", manifest
        return True, "模型已就绪", manifest
    except (OSError, ValueError, KeyError, TypeError):
        return False, "模型信息损坏，请重新训练", None


def main():
    parser = argparse.ArgumentParser(description="训练课堂人脸模型")
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR)
    parser.add_argument("--output", type=Path, default=MODEL_PATH)
    args = parser.parse_args()
    store = None
    try:
        from classroom_attendance.core.app_startup import checked_store
        store = checked_store()
        allowed = {row["student_id"] for row in store.students(active_only=True)}
        train_model(args.dataset, args.output, allowed, store=store)
        return 0
    except (OSError, ValueError, RuntimeError, cv2.error) as error:
        log_error("training_failed",error)
        if sys.stderr:
            print(f"训练失败：{user_message(error)}", file=sys.stderr, flush=True)
        return 1
    finally:
        if store is not None:
            store._instance_lease.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
