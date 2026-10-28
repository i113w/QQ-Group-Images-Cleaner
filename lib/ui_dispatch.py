"""Workers enqueue results; only the Tk owner thread calls Tk APIs."""

import queue
from tkinter import TclError
import traceback


class UiDispatcher:
    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.closed = False
        self.timer = root.after(40, self._drain)

    def post(self, callback, *args):
        if not self.closed:
            self.queue.put((callback, args))

    def _drain(self):
        if self.closed:
            return
        # Schedule first: callbacks may enter Tk's nested wait_window loop.
        # Thumbnail and progress results must keep draining while a modal is open.
        self.timer = self.root.after(40, self._drain)
        for _ in range(100):
            if self.closed:
                return
            try:
                callback, args = self.queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args)
            except TclError:
                if not self.closed:
                    traceback.print_exc()
            except Exception:
                traceback.print_exc()

    def close(self):
        self.closed = True
        try:
            self.root.after_cancel(self.timer)
        except TclError:
            pass
