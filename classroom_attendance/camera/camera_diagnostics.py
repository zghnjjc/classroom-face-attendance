"""只在内存中显示相机画面，不采集样本、不识别、不签到。"""

import queue
import threading

from classroom_attendance.core.app_errors import CameraError, TaskCancelled, log_error, user_message
from classroom_attendance.faces.face_utils import cv2, open_camera
from classroom_attendance.core.resource_lock import ResourceLease


class CameraPreviewWorker(threading.Thread):
    def __init__(self, index, lock_path, capture_factory=None):
        super().__init__(daemon=True)
        self.index, self.lock_path = index, lock_path
        self.capture_factory = capture_factory or open_camera
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.frame_lock = threading.Lock()
        self.frame = None

    def stop(self):
        self.stop_event.set()

    def take_frame(self):
        with self.frame_lock:
            frame, self.frame = self.frame, None
        return frame

    def run(self):
        capture = None
        try:
            if self.stop_event.is_set():
                return
            with ResourceLease(self.lock_path, '摄像头正在使用，请先停止其他窗口的采集或签到，再重试。'):
                try:
                    if self.stop_event.is_set():
                        return
                    capture = self.capture_factory(self.index)
                    frames = 0
                    while not self.stop_event.is_set():
                        ok, frame = capture.read()
                        if not ok or frame is None:
                            raise CameraError()
                        height, width = frame.shape[:2]
                        scale = min(640 / width, 360 / height)
                        preview = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))))
                        success, encoded = cv2.imencode('.png', preview)
                        if not success:
                            raise CameraError()
                        with self.frame_lock:
                            self.frame = encoded.tobytes()
                        frames += 1
                        if frames == 3:
                            self.events.put(('ready', self.index))
                        self.stop_event.wait(.05)
                finally:
                    if capture is not None:
                        capture.release()
        except TaskCancelled:
            pass
        except Exception as error:
            log_error('camera_preview_failed', error)
            self.events.put(('error', user_message(error)))
        finally:
            self.events.put(('stopped', None))
