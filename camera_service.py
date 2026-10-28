"""相机在工作线程中处理；Tk 主线程只接收预览与结果，不被识别阻塞。"""

import queue
import threading
import time

from app_config import CAMERA_LOCK_PATH, DATA_LOCK_PATH, DATASET_DIR, sample_paths, sample_student_id
from app_config import CAPTURE_TARGET
from attendance_store import STATUS_TEXT
from face_utils import cv2, np, load_classifier, load_recognizer, open_camera, save_gray
from resource_lock import ResourceLease
from train import model_health, preprocess
from maintenance_tools import require_disk_space
from app_errors import CameraError, TaskCancelled, log_error, user_message
from face_lifecycle import FaceLifecycleService, require_authorized
from face_delete_recovery import assert_no_face_deletion


class RecognitionStreak:
    def __init__(self, required=8, minimum_seconds=1.0):
        self.required, self.minimum_seconds = required, minimum_seconds
        self.student_id, self.count, self.started = None, 0, 0.0

    def update(self, student_id, now=None):
        now = time.monotonic() if now is None else now
        if student_id is None:
            self.student_id, self.count, self.started = None, 0, now
        elif student_id != self.student_id:
            self.student_id, self.count, self.started = student_id, 1, now
        else:
            self.count += 1
        return self.count >= self.required and now - self.started >= self.minimum_seconds


def quality_hint(face):
    if min(face.shape[:2]) < 100:
        return "请靠近一些，让人脸完整出现在画面中"
    brightness = float(face.mean())
    if brightness < 45:
        return "光线偏暗，请面向光源"
    if brightness > 215:
        return "光线过强，请避免逆光或过曝"
    if cv2.Laplacian(face, cv2.CV_64F).var() < 45:
        return "画面不够清晰，请保持稳定"
    return ""


class CameraWorker(threading.Thread):
    def __init__(self, store, mode, student_id=None, session_id=None, target=CAPTURE_TARGET):
        super().__init__(daemon=True)
        self.store, self.mode = store, mode
        self.student_id, self.session_id, self.target = student_id, session_id, target
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.frame = None
        self.frame_lock = threading.Lock()
        self.saved = 0
        self.last_hint = None
        self.capture = None

    def hint(self, text):
        if text != self.last_hint:
            self.events.put(("hint", text))
            self.last_hint = text

    def take_frame(self):
        with self.frame_lock:
            frame, self.frame = self.frame, None
        return frame

    def stop(self):
        self.stop_event.set()

    def run(self):
        capture = None
        try:
            if self.stop_event.is_set():
                return
            with ResourceLease(CAMERA_LOCK_PATH, "摄像头正在使用，请先停止另一个窗口的采集或签到。"):
                if self.stop_event.is_set():
                    return
                if self.mode == "capture":
                    with ResourceLease(DATA_LOCK_PATH, "采集或训练正在进行，请稍后重试。"):
                        assert_no_face_deletion(DATASET_DIR)
                        capture = self._capture_loop()
                else:
                    capture = self._sign_loop()
        except TaskCancelled:
            pass
        except Exception as error:
            log_error("camera_worker_failed", error)
            self.events.put(("error", user_message(error)))
        finally:
            if capture is not None:
                capture.release()
            self.events.put(("stopped", self.saved))

    def _preview(self, frame, faces):
        for x, y, w, h in faces:
            cv2.rectangle(frame, (x, y), (x+w, y+h), (210, 140, 65), 2)
        h, w = frame.shape[:2]
        scale = min(640/w,420/h)
        resized = cv2.resize(frame,(max(1,round(w*scale)),max(1,round(h*scale))))
        preview = np.full((420,640,3),(60,36,23),dtype=np.uint8)
        top,left=(420-resized.shape[0])//2,(640-resized.shape[1])//2
        preview[top:top+resized.shape[0],left:left+resized.shape[1]]=resized
        success, encoded = cv2.imencode(".png", preview)
        if success:
            with self.frame_lock:
                self.frame = encoded.tobytes()

    def _capture_loop(self):
        student = next((row for row in self.store.students(active_only=True) if row["student_id"] == self.student_id), None)
        if not student:
            raise ValueError("请先保存并启用该学生的信息。")
        FaceLifecycleService(self.store).require(self.student_id)
        DATASET_DIR.mkdir(exist_ok=True)
        require_disk_space(DATASET_DIR)
        indices = []
        for file in sample_paths():
            try:
                if sample_student_id(file) == self.student_id:
                    indices.append(int(file.stem.split(".")[-1]))
            except ValueError:
                continue
        next_index = max(indices, default=-1) + 1
        classifier = load_classifier()
        if self.stop_event.is_set():
            return None
        capture = open_camera(self.store.camera_index())
        self.capture = capture
        last_save = 0.0
        try:
            while not self.stop_event.is_set() and self.saved < self.target:
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise CameraError()
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = classifier.detectMultiScale(gray, 1.2, 5, minSize=(80,80))
                hint = "请保持画面中只有一人" if len(faces) != 1 else ""
                if len(faces) == 1:
                    x, y, w, h = faces[0]
                    face = gray[y:y+h, x:x+w]
                    hint = quality_hint(face)
                    if not hint and time.monotonic() - last_save >= .4:
                        require_disk_space(DATASET_DIR)
                        with self.store.connect(write=True) as connection:
                            connection.execute('BEGIN IMMEDIATE')
                            require_authorized(connection, self.student_id)
                            save_gray(DATASET_DIR / f"{self.student_id}.{next_index}.jpg", face)
                        next_index += 1
                        self.saved += 1
                        last_save = time.monotonic()
                        self.events.put(("progress", (self.saved, self.target)))
                        hint = "采集中，请缓慢转头并改变表情"
                self.hint(hint or "请正对摄像头")
                self._preview(frame, faces)
                self.stop_event.wait(.03)
            self.events.put(("complete", self.saved))
        finally:
            capture.release()
        return None

    def _sign_loop(self):
        active_ids = {row["student_id"] for row in self.store.students(active_only=True)}
        lifecycle = FaceLifecycleService(self.store)
        consented_ids = lifecycle.eligible_ids()
        # 校验与加载期间阻止训练替换模型，保证加载的是同一个已核对版本。
        with ResourceLease(DATA_LOCK_PATH, "采集或训练正在进行，请稍后重新开始识别。"):
            healthy, message, manifest = model_health(consented_ids=consented_ids)
            if not healthy:
                raise ValueError(message)
            matcher = load_recognizer()
        session = self.store.active_session()
        if not session or session["id"] != self.session_id:
            raise ValueError("老师尚未开启本课次签到，或课次已结束。")
        eligible = {row["student_id"] for row in self.store.session_rows(self.session_id)} & active_ids & consented_ids
        classifier = load_classifier()
        if self.stop_event.is_set():
            return None
        capture = open_camera(self.store.camera_index())
        self.capture = capture
        streak = RecognitionStreak()
        last_check = 0.0
        last_messages = {}
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now - last_check > .75:
                    current = self.store.active_session()
                    if not current or current["id"] != self.session_id:
                        self.events.put(("session_closed", None))
                        break
                    active_ids = {row["student_id"] for row in self.store.students(active_only=True)}
                    consented_ids = lifecycle.eligible_ids()
                    healthy, message, current_manifest = model_health(consented_ids=consented_ids)
                    if not healthy:
                        raise ValueError(message)
                    if current_manifest.get("model_sha256") != manifest.get("model_sha256"):
                        raise ValueError("识别模型已更新，请重新开始识别。")
                    eligible = {row["student_id"] for row in self.store.session_rows(self.session_id)} & active_ids & consented_ids
                    last_check = now
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise CameraError()
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = classifier.detectMultiScale(gray, 1.2, 5, minSize=(80,80))
                candidate, hint = None, "请正对摄像头，画面中保持一人"
                if len(faces) == 1:
                    x, y, w, h = faces[0]
                    face = gray[y:y+h, x:x+w]
                    hint = quality_hint(face)
                    if not hint:
                        label, distance = matcher.predict(preprocess(face))
                        student_id = matcher.getLabelInfo(label) or str(label)
                        if distance < 60 and student_id in eligible:
                            candidate, hint = student_id, "正在确认身份，请保持稳定"
                        elif distance < 60 and student_id not in eligible:
                            hint = "未在本课次名单中，请联系老师"
                        else:
                            hint = "暂未识别，请调整位置或联系老师补签"
                if streak.update(candidate, now) and now - last_messages.get(candidate, -100) > 8:
                    try:
                        record = self.store.record_sign_in(candidate, self.session_id)
                        self.events.put(("signed", record))
                        hint = f"{'签到成功' if record['inserted'] else '本课次已有记录'}（{STATUS_TEXT[record['status']]}）"
                        last_messages[candidate] = now
                    except ValueError as error:
                        log_error("signin_validation_failed",error)
                        hint = user_message(error)
                    streak.update(None, now)
                self.hint(hint)
                self._preview(frame, faces)
                self.stop_event.wait(.03)
        finally:
            capture.release()
        return None
