"""相机驱动在独立进程中运行，驱动挂起时可终止并释放设备。"""

import multiprocessing
import os
import queue
import threading
import time

from classroom_attendance.core.app_config import CAMERA_READ_TIMEOUT
from classroom_attendance.core.app_errors import CameraError, TaskCancelled, log_error


def _camera_process(pipe, index):
    import cv2
    capture = None
    try:
        capture = cv2.VideoCapture(index, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(index)
        if not capture.isOpened():
            capture.release()
            capture = cv2.VideoCapture(index)
        if not capture.isOpened():
            pipe.send(("error", None))
            return
        pipe.send(("ready", None))
        while True:
            command = pipe.recv()
            if command == "close":
                break
            pipe.send(("frame", capture.read()))
    except (EOFError, BrokenPipeError, OSError):
        pass
    except Exception as error:
        log_error("camera_driver_failed",error)
        try: pipe.send(("error",None))
        except (EOFError,OSError): pass
    finally:
        if capture is not None:
            capture.release()
        pipe.close()


class ProcessCapture:
    def __init__(self, index=0, timeout=CAMERA_READ_TIMEOUT):
        context = multiprocessing.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=_camera_process, args=(child, index), daemon=True)
        self.timeout = timeout
        self.closed = False
        self.process.start()
        child.close()
        try:
            if not self.pipe.poll(timeout+2) or self.pipe.recv()[0] != "ready":
                raise CameraError()
        except (EOFError, OSError, CameraError) as error:
            self.release()
            raise CameraError() from error

    def read(self):
        if self.closed:
            raise CameraError()
        try:
            self.pipe.send("read")
            if not self.pipe.poll(self.timeout):
                raise CameraError()
            kind, value = self.pipe.recv()
            if kind != "frame":
                raise CameraError()
            return value
        except (EOFError, OSError) as error:
            raise CameraError() from error

    def release(self):
        if self.closed:
            return
        self.closed = True
        # 不等待挂起的 read/release；操作系统回收独立驱动进程及其设备句柄。
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=.7)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=.3)
        self.pipe.close()


class TimedCapture:
    """单个读取线程的请求/结果队列，适用于真设备与可注入的测试设备。"""
    def __init__(self, capture, stop_event, timeout=CAMERA_READ_TIMEOUT):
        self.capture, self.stop_event, self.timeout = capture, stop_event, timeout
        self.requests, self.results = queue.Queue(), queue.Queue()
        self.closed = False
        self.release_done = threading.Event()
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.reader.start()

    def _reader(self):
        while self.requests.get():
            try:
                self.results.put((True, self.capture.read()))
            except Exception as error:
                self.results.put((False, error))

    def read(self):
        deadline = time.perf_counter()+self.timeout
        self.requests.put(True)
        while time.perf_counter() < deadline:
            if self.stop_event.is_set():
                raise TaskCancelled()
            try:
                ok, result = self.results.get(timeout=.03)
            except queue.Empty:
                continue
            if ok:
                return result
            raise result
        raise CameraError()

    def release(self):
        if self.closed:
            return
        self.closed = True
        self.requests.put(False)
        def release():
            try:
                self.capture.release()
            finally:
                self.release_done.set()
        threading.Thread(target=release, daemon=True).start()
        self.release_done.wait(1.0)
