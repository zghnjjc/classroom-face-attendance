"""训练可核对版本的人脸模型，不将停用学生纳入新模型。"""

import argparse
import hashlib
import json
import sys
import tempfile
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from app_config import DATA_LOCK_PATH, DATASET_DIR, MODEL_MANIFEST_PATH, MODEL_PATH, sample_paths, sample_student_id
from attendance_store import AttendanceStore
from face_utils import create_recognizer, cv2, np, read_gray, save_recognizer
from resource_lock import ResourceLease
from task_runtime import checkpoint
from app_errors import log_error, user_message


def preprocess(image):
    return cv2.equalizeHist(cv2.resize(image, (160, 160), interpolation=cv2.INTER_AREA))


def dataset_signature(paths):
    digest = hashlib.sha256()
    for path in sorted(paths):
        stat = path.stat()
        digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
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


def train_model(dataset=DATASET_DIR, output=MODEL_PATH, allowed_ids=None, *, _lease=True):
    with (ResourceLease(DATA_LOCK_PATH, "人脸采集或模型训练正在进行，请先结束当前操作。") if _lease else nullcontext()):
        checkpoint()
        files = training_paths(dataset, allowed_ids)
        signature = dataset_signature(files)
        images, student_ids = data_translate(dataset, allowed_ids)
        label_to_id = dict(enumerate(sorted(set(student_ids))))
        id_to_label = {student_id: label for label, student_id in label_to_id.items()}
        recognizer = create_recognizer()
        recognizer.train(images, np.asarray([id_to_label[sid] for sid in student_ids], dtype=np.int32))
        for label, student_id in label_to_id.items():
            recognizer.setLabelInfo(label, student_id)
        if signature != dataset_signature(training_paths(dataset, allowed_ids)):
            raise RuntimeError("训练期间样本发生变化，请重试。")
        output=Path(output)
        with tempfile.TemporaryDirectory(prefix=".model-build-",dir=output.parent) as staging:
            staged=Path(staging)/"model.yml"
            checkpoint()
            save_recognizer(recognizer, staged)
            manifest = {"version": 2, "trained_at": datetime.now().isoformat(timespec="seconds"),
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


def model_health(allowed_ids=None, model=MODEL_PATH, dataset=DATASET_DIR):
    if not Path(model).exists() or not manifest_path(model).exists():
        return False, "请先到管理端训练模型", None
    try:
        manifest = json.loads(manifest_path(model).read_text(encoding="utf-8"))
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
        if manifest.get("version") != 2 or hashlib.sha256(Path(model).read_bytes()).hexdigest() != manifest["model_sha256"]:
            return False, "模型版本不一致，请重新训练", manifest
        # 验证训练时的身份集合。当前停用、新建或重新启用的档案不改变旧模型的完整性。
        # 未训练身份仅在覆盖提示中列出，资格由识别线程与签到事务再次检查。
        files = training_paths(dataset, set(student_ids))
        if (dataset_signature(files) != manifest["dataset_signature"]
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
    try:
        allowed = {row["student_id"] for row in AttendanceStore().students(active_only=True)}
        train_model(args.dataset, args.output, allowed)
        return 0
    except (OSError, ValueError, RuntimeError, cv2.error) as error:
        log_error("training_failed",error)
        if sys.stderr:
            print(f"训练失败：{user_message(error)}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
