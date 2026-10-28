"""有取消令牌和截止时间的后台工作；UI 只在主线程推进流程。"""

import contextvars
import functools
import inspect
import queue
import threading
import time
from dataclasses import dataclass

from app_config import TASK_TIMEOUT
from app_errors import TaskCancelled, TaskTimeout, log_error

current_control = contextvars.ContextVar("classroom_task_control", default=None)


class TaskControl:
    def __init__(self, timeout=TASK_TIMEOUT):
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + timeout

    def check(self):
        if self.cancelled.is_set():
            raise TaskCancelled()
        if time.monotonic() >= self.deadline:
            raise TaskTimeout()


def checkpoint():
    control = current_control.get()
    if control:
        control.check()


class FlowHost:
    def init_flows(self, root, async_io=True):
        self.root, self.async_io = root, async_io
        self.closed = False
        self._flow_depth = 0
        self._flows = {}
        self._running_flows = set()

    def start_flow(self, generator, key=None, on_error=None):
        if not inspect.isgenerator(generator):
            return generator
        if not self.async_io:
            # 仅供同步单元测试使用；生产窗口使用默认异步 FlowRunner。
            value, error = None, None
            self._flow_depth += 1
            try:
                while True:
                    request = generator.throw(error) if error else generator.send(value)
                    try:
                        value, error = request.work(), None
                    except Exception as failure:
                        value, error = None, failure
            except StopIteration as stop:
                return stop.value
            except Exception as failure:
                log_error("ui_flow_failed",failure)
                if on_error: on_error(failure)
                else: self.handle_error(failure)
                return None
            finally:
                self._flow_depth -= 1
        if key and key in self._flows:
            generator.close()
            return
        runner = FlowRunner(self, generator, key, on_error)
        self._running_flows.add(runner)
        if key:
            self._flows[key] = runner
        return runner.advance()

    def flow(self, function):
        @functools.wraps(function)
        def invoke(*args, **kwargs):
            generator = function(*args, **kwargs)
            if self._flow_depth:
                return generator
            return self.start_flow(generator)
        return invoke

    def cancel_flows(self):
        for runner in list(self._running_flows):
            runner.cancel()


@dataclass
class IO:
    work: object
    timeout: float = TASK_TIMEOUT


class FlowRunner:
    """一个流程最多一个后台调用，取消后抛弃迟到回调并阻止后续提交。"""

    def __init__(self, owner, generator, key=None, on_error=None):
        self.owner, self.generator, self.key = owner, generator, key
        self.on_error = on_error
        self.pending = None
        self.closed = False
        self.interrupted = None

    def advance(self, value=None, error=None):
        if self.closed or self.owner.closed:
            self.cancel()
            return
        self.owner._flow_depth += 1
        try:
            request = self.generator.throw(error) if error else self.generator.send(value)
        except StopIteration as stop:
            self.finish()
            return stop.value
        except Exception as failure:
            self.finish()
            log_error("ui_flow_failed", failure)
            if self.on_error:
                self.on_error(failure)
            else:
                self.owner.handle_error(failure)
            return
        finally:
            self.owner._flow_depth -= 1
        if not isinstance(request, IO):
            raise TypeError("界面流程仅可等待 IO 请求")
        control = TaskControl(request.timeout)
        completed = queue.Queue()
        self.pending = control

        def execute():
            token = current_control.set(control)
            try:
                control.check()
                result = request.work()
                control.check()
                completed.put((True, result))
            except Exception as failure:
                log_error("background_io_failed", failure)
                completed.put((False, failure))
            finally:
                current_control.reset(token)
        threading.Thread(target=execute, daemon=True).start()

        def poll():
            if self.owner.closed:
                self.cancel()
                return
            try:
                ok, result = completed.get_nowait()
            except queue.Empty:
                if not self.closed and self.interrupted is None:
                    try:
                        control.check()
                    except (TaskCancelled, TaskTimeout) as failure:
                        self.interrupted = failure
                        control.cancelled.set()
                        self.owner.say("已请求停止，正在等待当前步骤结束；结束后可重试。")
                if not self.closed and self.interrupted is None and control.deadline-time.monotonic() < request.timeout-.4:
                    self.owner.say("正在处理数据；数据忙时将自动重试")
                self.owner.schedule(50, poll)
                return
            self.pending = None
            if self.closed:
                self.unregister()
                return
            interrupted, self.interrupted = self.interrupted, None
            self.advance(result if ok and interrupted is None else None, interrupted or (None if ok else result))
        self.owner.schedule(20, poll)

    def finish(self):
        self.closed = True
        if self.pending is None:
            self.unregister()

    def unregister(self):
        if self.key and self.owner._flows.get(self.key) is self:
            self.owner._flows.pop(self.key, None)
        self.owner._running_flows.discard(self)

    def cancel(self):
        if self.pending:
            self.pending.cancelled.set()
        if self.closed:
            return
        self.finish()
        self.generator.close()


def ui_flow(function):
    """生成器方法的顶层调用异步执行，流程内部通过 yield from 顺序衔接。"""
    @functools.wraps(function)
    def wrapped(self, *args, **kwargs):
        generator = function(self, *args, **kwargs)
        if self._flow_depth:
            return generator
        callback = getattr(self,"refresh_failed",None) if function.__name__.startswith("refresh_") else None
        return self.start_flow(generator, key=function.__name__,on_error=callback)
    return wrapped
