from tkinter import Label, TclError, Toplevel
from lib.theme import BORDER, CARD, FONT, TEXT

class ToolTip:
    """Create a tooltip for a given widget."""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tooltip_window = None
        self.timer = None
        widget.bind('<Enter>', self.enter, add='+')
        widget.bind('<Leave>', self.leave, add='+')
        widget.bind('<ButtonPress>', self.leave, add='+')
        widget.bind('<Destroy>', self.leave, add='+')

    def enter(self, event=None):
        self.leave()
        self.timer = self.widget.after(450, self._show)

    def _show(self):
        self.timer = None
        try:
            if not self.widget.winfo_exists():
                return
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            self.tooltip_window = Toplevel(self.widget)
            self.tooltip_window.wm_overrideredirect(True)
            label = Label(self.tooltip_window, text=self.text, justify='left', wraplength=700,
                          background=CARD, foreground=TEXT, highlightbackground=BORDER,
                          highlightthickness=1, font=(FONT, 9), padx=10, pady=7)
            label.pack()
            self.tooltip_window.update_idletasks()
            x = max(0, min(x, self.widget.winfo_screenwidth() - self.tooltip_window.winfo_width() - 10))
            y = max(0, min(y, self.widget.winfo_screenheight() - self.tooltip_window.winfo_height() - 10))
            self.tooltip_window.wm_geometry(f'+{x}+{y}')
        except TclError:
            self.tooltip_window = None

    def leave(self, event=None):
        try:
            if self.timer:
                self.widget.after_cancel(self.timer)
                self.timer = None
            if self.tooltip_window:
                self.tooltip_window.destroy()
        except TclError:
            pass
        self.tooltip_window = None
