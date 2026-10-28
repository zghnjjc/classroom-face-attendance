"""推进真实事件循环，等待后台流程及其可观察结果，避免用固定长延时猜测完成。"""

import threading
import time


def pump_flows(testcase, root, *owners, predicate=None, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        root.update()
        idle = all(not owner._running_flows and not getattr(owner, 'busy', False) for owner in owners)
        if idle and (predicate is None or predicate()):
            return
        if time.monotonic() >= deadline:
            testcase.fail('异步操作没有在截止时间内完成')
        threading.Event().wait(.005)
