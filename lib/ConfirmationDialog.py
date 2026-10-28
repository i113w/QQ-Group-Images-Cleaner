"""Review the exact immutable file plan before granting recycling approval."""

import math
import os
import time
from tkinter import BooleanVar, Frame, Label, Toplevel, ttk

from lib.file_metadata import IMAGE_EXTENSIONS, format_size
from lib.ImportCheck import import_PIL
from lib.safe_recycle import RecycleApproval
from lib.theme import BG, CARD, MUTED

Image, ImageTk = import_PIL()


class ConfirmationDialog:
    PAGE_SIZE = 100

    def __init__(self, parent, app, plan, manifest):
        self.parent, self.app, self.plan, self.manifest = parent, app, plan, manifest
        self.approval = None
        self.confirmed = False
        self.closed = False
        self.page = 1
        self.photos = []
        self.acknowledged = BooleanVar(parent, value=False)
        self.top = Toplevel(parent, background=BG)
        self.top.title(app._('confirm_delete_title'))
        self.top.geometry('900x730')
        self.top.minsize(840, 650)
        self.top.transient(parent)
        self.top.grab_set()
        self.top.protocol('WM_DELETE_WINDOW', self.cancel)
        self.top.bind('<Destroy>', self._destroyed, add='+')
        self.top.bind('<Escape>', lambda _: self.cancel())
        self.top.columnconfigure(0, weight=1)
        self.top.rowconfigure(3, weight=1)
        header = ttk.Frame(self.top, padding=(22, 18, 22, 8))
        header.grid(row=0, column=0, sticky='ew')
        ttk.Label(header, text=app._('plan_summary', len(plan.targets), format_size(plan.total_size)),
                  style='Title.TLabel').pack(anchor='w')
        ttk.Label(header, text=plan.reason, wraplength=820).pack(anchor='w', pady=(8, 0))
        ttk.Label(header, text=app._('plan_root', plan.root), style='Muted.TLabel', wraplength=820).pack(anchor='w', pady=(6, 0))
        ttk.Label(header, text=app._('plan_impact'), style='Muted.TLabel', wraplength=820).pack(anchor='w', pady=(8, 0))
        titlebar = ttk.Frame(self.top, padding=(22, 6))
        titlebar.grid(row=1, column=0, sticky='ew')
        ttk.Label(titlebar, text=app._('plan_paths')).pack(side='left')
        ttk.Button(titlebar, text=app._('plan_manifest'), command=lambda: app.open_path(str(manifest))).pack(side='right')
        self.thumbs = Frame(self.top, background=BG, height=110)
        self.thumbs.grid(row=2, column=0, sticky='ew', padx=22, pady=(0, 10))
        self.thumbs.grid_propagate(False)
        self.loading_label = Label(self.thumbs, text=app._('loading_thumbnails'), background=BG, foreground=MUTED)
        self.loading_label.place(relx=.5, rely=.5, anchor='center')
        table_frame = ttk.Frame(self.top, style='Card.TFrame')
        table_frame.grid(row=3, column=0, sticky='nsew', padx=22)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table_frame, columns=('name', 'date', 'size', 'path'), show='headings')
        self.tree.heading('name', text=app._('file_name'))
        self.tree.heading('date', text=app._('file_date'))
        self.tree.heading('path', text=app._('file_path'))
        self.tree.heading('size', text=app._('file_size'))
        self.tree.column('name', width=150, stretch=False)
        self.tree.column('date', width=100, stretch=False)
        self.tree.column('path', width=510, minwidth=300, stretch=True)
        self.tree.column('size', width=90, anchor='e', stretch=False)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(table_frame, orient='vertical', command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set)
        horizontal = ttk.Scrollbar(table_frame, orient='horizontal', command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.configure(xscrollcommand=horizontal.set)
        pages = ttk.Frame(self.top, padding=(22, 8))
        pages.grid(row=4, column=0, sticky='ew')
        self.page_label = ttk.Label(pages, style='Muted.TLabel')
        self.page_label.pack(side='left')
        self.next_button = ttk.Button(pages, text=app._('next_page'), command=lambda: self.change_page(1))
        self.next_button.pack(side='right')
        self.prev_button = ttk.Button(pages, text=app._('prev_page'), command=lambda: self.change_page(-1))
        self.prev_button.pack(side='right', padx=8)
        footer = ttk.Frame(self.top, padding=(22, 4, 22, 18))
        footer.grid(row=5, column=0, sticky='ew')
        self.ack = ttk.Checkbutton(footer, text=app._('plan_ack'), variable=self.acknowledged,
                                   command=self._approval_changed)
        self.ack.pack(anchor='w', pady=(0, 12))
        self.confirm_button = ttk.Button(footer, text=app._('confirm_btn'), style='Recycle.TButton',
                                          command=self.confirm, state='disabled')
        self.confirm_button.pack(side='right')
        self.cancel_button = ttk.Button(footer, text=app._('cancel_btn'), command=self.cancel)
        self.cancel_button.pack(side='right', padx=10)
        self.cancel_button.focus_set()
        self.show_page()
        paths = [target.path for target in plan.targets
                 if os.path.splitext(target.path)[1].lower() in IMAGE_EXTENSIONS][:6]
        if paths:
            app.run_background(lambda: self._decode(paths), self._show_thumbnails)
        else:
            self.loading_label.configure(text='')

    @staticmethod
    def _decode(paths):
        images = []
        for path in paths:
            try:
                with Image.open(path) as source:
                    source.thumbnail((110, 95))
                    images.append(source.copy())
            except (OSError, ValueError, Image.DecompressionBombError):
                pass
        return images

    def _show_thumbnails(self, images):
        if not self.closed:
            self.loading_label.destroy()
            for index, pil_image in enumerate(images):
                photo = ImageTk.PhotoImage(pil_image, master=self.top)
                self.photos.append(photo)
                Label(self.thumbs, image=photo, background=CARD, padx=6, pady=4).grid(row=0, column=index, padx=5)
            if not images:
                Label(self.thumbs, text=self.app._('image_unavailable'), background=BG, foreground=MUTED).grid(row=0, column=0)
        for image in images:
            image.close()

    def show_page(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        targets = self.plan.targets[(self.page - 1) * self.PAGE_SIZE:self.page * self.PAGE_SIZE]
        for target in targets:
            self.tree.insert('', 'end', values=(os.path.basename(target.path),
                time.strftime('%Y-%m-%d', time.localtime(target.real_time)), format_size(target.size), target.path))
        pages = max(1, math.ceil(len(self.plan.targets) / self.PAGE_SIZE))
        self.page_label.configure(text=self.app._('plan_page', self.page, pages))
        self.prev_button.configure(state='normal' if self.page > 1 else 'disabled')
        self.next_button.configure(state='normal' if self.page < pages else 'disabled')

    def change_page(self, delta):
        pages = max(1, math.ceil(len(self.plan.targets) / self.PAGE_SIZE))
        self.page = max(1, min(pages, self.page + delta))
        self.show_page()

    def _approval_changed(self):
        self.confirm_button.configure(state='normal' if self.acknowledged.get() else 'disabled')

    def confirm(self):
        if not self.acknowledged.get():
            return
        self.approval = RecycleApproval(self.plan.id, self.plan.digest)
        self.confirmed = True
        self._close()

    def cancel(self):
        self.approval = None
        self.confirmed = False
        self._close()

    def _close(self):
        self.closed = True
        self.photos.clear()
        self.top.grab_release()
        self.top.destroy()

    def _destroyed(self, event):
        if event.widget is self.top:
            self.closed = True
