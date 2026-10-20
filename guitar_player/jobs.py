"""One analysis worker, one replaceable pending request, cooperative cancellation."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading

from .i18n import tr


class Cancelled(Exception):
    """Control flow, deliberately distinct from model/runtime failures."""


_context = ContextVar('analysis_context', default=None)


@contextmanager
def task_context(cancel, progress):
    token = _context.set((cancel, progress))
    try:
        yield
    finally:
        _context.reset(token)


def checkpoint(stage=None):
    context = _context.get()
    if context is None:
        return
    cancel, progress = context
    if cancel.is_set():
        raise Cancelled()
    if stage:
        progress(stage)


class LatestWorker:
    def __init__(self, messages):
        self.messages = messages
        self.condition = threading.Condition()
        self.pending = self.active = None
        self.closed = False
        self.thread = threading.Thread(target=self._run, daemon=True, name='guitar-analysis')
        self.thread.start()

    def cancel(self):
        with self.condition:
            if self.active:
                self.active.set()
            self.pending = None

    def submit(self, kind, token, work):
        with self.condition:
            if self.closed:
                raise RuntimeError(tr('error.worker_closed'))
            self.cancel()
            self.pending = (kind, token, work)
            self.condition.notify()

    def close(self):
        with self.condition:
            self.cancel()
            self.closed = True
            self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                kind, token, work = self.pending
                self.pending = None
                cancel = self.active = threading.Event()
            try:
                with task_context(cancel, lambda stage: self.messages.put(('progress', token, stage))):
                    checkpoint()
                    result = work()
                    checkpoint()
                    self.messages.put((kind, token, result))
            except Cancelled:
                pass
            except Exception as exc:
                if not cancel.is_set():
                    self.messages.put(('error', token, str(exc)))
            finally:
                with self.condition:
                    self.active = None
