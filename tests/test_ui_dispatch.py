import time
from tkinter import TclError, Tk, Toplevel
import unittest

from lib.ui_dispatch import UiDispatcher


class DispatcherTests(unittest.TestCase):
    def test_worker_results_are_delivered_inside_modal_wait(self):
        try:
            root = Tk()
        except TclError as error:
            self.skipTest('Tk display unavailable: ' + str(error))
        root.withdraw()
        dispatcher = UiDispatcher(root)
        modal = Toplevel(root)
        modal.withdraw()
        state = {'delivered': False, 'returned': False}

        def worker_result():
            state['delivered'] = True
            modal.destroy()

        def show_modal():
            root.after(50, lambda: dispatcher.post(worker_result))
            root.wait_window(modal)
            state['returned'] = True

        def timeout():
            if modal.winfo_exists():
                modal.destroy()

        root.after(1500, timeout)
        dispatcher.post(show_modal)
        try:
            deadline = time.monotonic() + 2
            while not state['returned'] and time.monotonic() < deadline:
                root.update()
                time.sleep(.01)
            self.assertTrue(state['returned'])
            self.assertTrue(state['delivered'], 'Async results were blocked until the modal closed.')
        finally:
            dispatcher.close()
            root.destroy()


if __name__ == '__main__':
    unittest.main()
