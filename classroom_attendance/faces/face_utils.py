"""OpenCV 图像读写与摄像头检查。"""

import os
from contextlib import chdir
from pathlib import Path
from uuid import uuid4
from threading import RLock

import cv2
import numpy as np

from classroom_attendance.core.app_config import CASCADE_PATH, MODEL_PATH
from classroom_attendance.core.task_runtime import checkpoint

MODEL_IO_LOCK = RLock()


def create_recognizer():
    if not hasattr(cv2, "face") or not hasattr(cv2.face, "LBPHFaceRecognizer_create"):
        raise RuntimeError("缺少人脸识别模块，请安装 requirements.txt 中的 opencv-contrib-python。")
    return cv2.face.LBPHFaceRecognizer_create()


def load_classifier():
    if not CASCADE_PATH.exists():
        raise FileNotFoundError(f"缺少人脸检测文件：{CASCADE_PATH.name}")
    # Windows OpenCV 的文件接口不能直接读取中文绝对路径。
    storage = cv2.FileStorage(CASCADE_PATH.read_text(encoding="utf-8"),
                              cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
    try:
        classifier = cv2.CascadeClassifier()
        if not classifier.read(storage.getFirstTopLevelNode()) or classifier.empty():
            raise RuntimeError("人脸检测器加载失败，请检查 OpenCV 安装。")
        return classifier
    finally:
        storage.release()


def load_recognizer(path: Path = MODEL_PATH):
    recognizer = create_recognizer()
    path = Path(path).resolve()
    # 锁保护进程级工作目录切换；相对文件名规避 OpenCV 的中文父目录问题。
    with MODEL_IO_LOCK, chdir(path.parent):
        recognizer.read(path.name)
    return recognizer


def save_recognizer(recognizer, path: Path):
    path = Path(path).resolve()
    temporary = path.with_name(f".face-model-{uuid4().hex}.tmp.yml")
    try:
        with MODEL_IO_LOCK, chdir(path.parent):
            recognizer.write(temporary.name)
        checkpoint()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def open_camera(index: int = 0):
    from classroom_attendance.camera.camera_device import ProcessCapture
    return ProcessCapture(index)


def read_gray(path: Path):
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"无法读取样本图片：{path.name}")
    return image


def save_gray(path: Path, image):
    success, buffer = cv2.imencode(".jpg", image)
    if not success:
        raise RuntimeError("人脸图像保存失败。")
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        buffer.tofile(temporary)
        checkpoint()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
