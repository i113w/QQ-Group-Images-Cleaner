import os
import random
import threading
from tkinter import ttk, Frame, Label, Scrollbar, Canvas, Toplevel
from lib.ToolTip import ToolTip
from lib.ImportCheck import import_PIL

Image, ImageTk = import_PIL()


class ConfirmationDialog:
    """A custom dialog to confirm deletion with image previews."""

    def __init__(self, parent, app, year, month, image_paths, custom_message=None):
        self.parent = parent
        self.app = app
        self.confirmed = False
        self.loading_label = None

        self.top = Toplevel(parent)
        self.top.title(self.app._('confirm_delete_title'))
        self.top.transient(parent)
        self.top.grab_set()

        # --- Message ---
        # Fix #8: unified positional-arg call for both languages
        if custom_message:
            msg = custom_message
        else:
            msg = self.app._('confirm_delete_msg', year, month)
        Label(self.top, text=msg, justify='left', padx=10, pady=10).pack()

        # --- Thumbnails Frame ---
        thumb_frame = Frame(self.top, bd=2, relief='sunken')
        thumb_frame.pack(padx=10, pady=10, fill='both', expand=True)
        self.canvas = Canvas(thumb_frame, width=480, height=250)

        scrollbar = Scrollbar(thumb_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = Frame(self.canvas)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.photo_references = []

        # --- Loading indicator (removed when first thumbnail arrives) ---
        self.loading_label = Label(self.scrollable_frame,
                                   text=self.app._('loading_thumbnails'))
        self.loading_label.pack(pady=20)

        # --- Fix #9: load thumbnails asynchronously in a background thread ---
        self.load_thumbnails(image_paths)

        # --- Buttons ---
        button_frame = Frame(self.top, pady=5)
        button_frame.pack()
        ttk.Button(button_frame, text=self.app._('cancel_btn'),
                   command=self.cancel).pack(side='right', padx=10)
        ttk.Button(button_frame, text=self.app._('confirm_btn'),
                   command=self.confirm).pack(side='right')

    # ---------------------------------------------------- async thumbnail loading
    def load_thumbnails(self, image_paths):
        """Select a random sample and start asynchronous loading."""
        sample_size = 20
        paths_to_show = (random.sample(image_paths, min(len(image_paths), sample_size))
                         if image_paths else [])

        if not paths_to_show:
            if self.loading_label:
                self.loading_label.destroy()
                self.loading_label = None
            return

        threading.Thread(target=self._load_thumbnails_thread,
                         args=(paths_to_show,), daemon=True).start()

    def _load_thumbnails_thread(self, paths):
        """Decode thumbnail PIL images in a background thread.

        Only ``Image.open()`` / ``thumbnail()`` are called here.
        ``ImageTk.PhotoImage`` instantiation is deferred to the main
        thread via ``after()`` to keep Tkinter thread-safe (Fix #4 / #9).
        """
        for i, path in enumerate(paths):
            try:
                image = Image.open(path)
                image.thumbnail((100, 100))
                # Send the PIL Image to the main thread
                self.top.after(0, self._add_thumbnail_to_ui, i, path, image)
            except Exception as e:
                print(f"Could not load thumbnail for {path}: {e}")

    def _add_thumbnail_to_ui(self, index, path, pil_image):
        """Create PhotoImage and place it on the canvas — main thread only."""
        if not self.top.winfo_exists():
            try:
                pil_image.close()
            except Exception:
                pass
            return

        # Remove loading indicator on first successful thumbnail
        if self.loading_label:
            self.loading_label.destroy()
            self.loading_label = None

        try:
            photo = ImageTk.PhotoImage(pil_image)
            self.photo_references.append(photo)

            row, col = divmod(index, 4)
            item_frame = Frame(self.scrollable_frame)
            item_frame.grid(row=row, column=col, padx=5, pady=5)

            img_label = Label(item_frame, image=photo)
            img_label.pack()

            ToolTip(img_label, os.path.basename(path))
        except Exception as e:
            print(f"Error creating thumbnail UI for {path}: {e}")
        finally:
            try:
                pil_image.close()
            except Exception:
                pass

    # ---------------------------------------------------- dialog actions
    def confirm(self):
        self.confirmed = True
        self.top.destroy()

    def cancel(self):
        self.confirmed = False
        self.top.destroy()