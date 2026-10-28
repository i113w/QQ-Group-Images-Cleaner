"""Paged image previews backed by index metadata, with scoped selection events."""

import math
import os
import time
from tkinter import BooleanVar, Canvas, Frame, Label, Menu, StringVar, Toplevel, ttk

from lib.file_metadata import IMAGE_EXTENSIONS, format_size, path_key
from lib.ImportCheck import import_PIL
from lib.theme import ACCENT, BG, BORDER, CARD, FONT, MUTED, TEXT, TINT
from lib.ToolTip import ToolTip

Image, ImageTk = import_PIL()
from PIL import ImageOps


class ThumbnailViewerWindow:
    SORT_KEYS = ('time_desc', 'time_asc', 'size_desc', 'size_asc')

    def __init__(self, parent, app, image_paths, year, month):
        self.parent, self.app = parent, app
        self.service = app.service
        self.year, self.month = year, month
        self.custom_paths = tuple(image_paths) if image_paths is not None else None
        self.closed = False
        self.generation = 0
        self.current_page, self.total_pages = 1, 1
        self.columns = 4
        self.sort_key = 'time_desc'
        self.items_per_page = StringVar(parent, value='20')
        self.sort_option = StringVar(parent)
        self.page_select_var = BooleanVar(parent, value=False)
        self.page_number = StringVar(parent, value='1')
        self.selected_paths = set()
        self.images_on_page = []
        self.photos, self.thumb_labels, self.thumb_frames, self.thumb_vars = {}, {}, {}, {}
        self.widget_paths = {}
        self.drag = None
        self.selection_rect = None
        self.clicked_image_path = None
        self.top = Toplevel(parent, background=BG)
        self.top.transient(parent)
        self.top.geometry('1050x750')
        self.top.minsize(850, 620)
        self.top.protocol('WM_DELETE_WINDOW', self.close)
        self.setup_ui()
        self.update_language()
        self.reload()

    def setup_ui(self):
        header = ttk.Frame(self.top, padding=(20, 16, 20, 8))
        header.pack(fill='x')
        self.heading = ttk.Label(header, style='Title.TLabel')
        self.heading.pack(side='left')
        self.delete_selected_button = ttk.Button(header, style='Recycle.TButton',
                                                   command=self.delete_selected_images)
        self.delete_selected_button.pack(side='right')
        self.hint = ttk.Label(self.top, style='Muted.TLabel')
        self.hint.pack(anchor='w', padx=22, pady=(2, 12))
        controls = ttk.Frame(self.top, padding=(20, 0, 20, 12))
        controls.pack(fill='x')
        self.per_page_label = ttk.Label(controls)
        self.per_page_label.pack(side='left', padx=(0, 8))
        per_page = ttk.Combobox(controls, textvariable=self.items_per_page, state='readonly',
                                values=('10', '20', '50', '100'), width=5)
        per_page.pack(side='left')
        per_page.bind('<<ComboboxSelected>>', self._reset_page)
        self.sort_label = ttk.Label(controls)
        self.sort_label.pack(side='left', padx=(20, 8))
        self.sort_menu = ttk.Combobox(controls, textvariable=self.sort_option, state='readonly', width=23)
        self.sort_menu.pack(side='left')
        self.sort_menu.bind('<<ComboboxSelected>>', self._sort_changed)
        self.page_select_check = ttk.Checkbutton(controls, variable=self.page_select_var,
                                                command=self.toggle_current_page_selection)
        self.page_select_check.pack(side='left', padx=(18, 0))
        self.clear_button = ttk.Button(controls, command=self.clear_selection)
        self.clear_button.pack(side='right')

        frame = ttk.Frame(self.top, style='Card.TFrame')
        frame.pack(fill='both', expand=True, padx=20)
        self.canvas = Canvas(frame, background=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.canvas.yview)
        scrollbar.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.scrollable_frame = Frame(self.canvas, background=BG)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor='nw')
        self.scrollable_frame.bind('<Configure>', lambda _: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self._resize)
        footer = ttk.Frame(self.top, padding=(20, 12))
        footer.pack(fill='x')
        self.selection_label = ttk.Label(footer, style='Muted.TLabel')
        self.selection_label.pack(side='left')
        self.next_button = ttk.Button(footer, command=lambda: self.change_page(1))
        self.next_button.pack(side='right')
        self.total_pages_label = ttk.Label(footer)
        self.total_pages_label.pack(side='right', padx=8)
        self.page_entry = ttk.Entry(footer, width=5, justify='center', textvariable=self.page_number)
        self.page_entry.pack(side='right')
        self.page_entry.bind('<Return>', self.on_goto_page)
        self.goto_label = ttk.Label(footer)
        self.goto_label.pack(side='right', padx=(12, 6))
        self.prev_button = ttk.Button(footer, command=lambda: self.change_page(-1))
        self.prev_button.pack(side='right')
        self.context_menu = Menu(self.top, tearoff=0)
        self.context_menu.add_command(command=self.delete_image)
        self.context_menu.add_command(command=self.open_image)
        self.context_menu.add_command(command=self.open_image_directory)
        # Toplevel bindtags receive child events without global bind_all hooks.
        self.top.bind('<MouseWheel>', self._on_mousewheel)
        self.top.bind('<Button-4>', self._on_mousewheel)
        self.top.bind('<Button-5>', self._on_mousewheel)
        self.top.bind('<ButtonPress-1>', lambda event: self._drag_press(event, False))
        self.top.bind('<ButtonPress-3>', lambda event: self._drag_press(event, True))
        self.top.bind('<B1-Motion>', self._drag_motion)
        self.top.bind('<B3-Motion>', self._drag_motion)
        self.top.bind('<ButtonRelease-1>', self._drag_release)
        self.top.bind('<ButtonRelease-3>', self._drag_release)

    def update_language(self):
        title = self.app._('thumb_viewer_title', self.year, self.month)
        self.top.title(title)
        self.heading.configure(text=title)
        for widget, key in ((self.hint, 'selection_hint'), (self.per_page_label, 'items_per_page'),
                            (self.sort_label, 'sort_by'), (self.page_select_check, 'select_current_page'),
                            (self.clear_button, 'clear_selection'), (self.prev_button, 'prev_page'),
                            (self.next_button, 'next_page'), (self.goto_label, 'goto_page')):
            widget.configure(text=self.app._(key))
        values = [self.app._('sort_' + key) for key in self.SORT_KEYS]
        self.sort_menu.configure(values=values)
        self.sort_option.set(self.app._('sort_' + self.sort_key))
        for index, key in enumerate(('context_delete', 'context_open', 'context_open_dir')):
            self.context_menu.entryconfigure(index, label=self.app._(key))
        self._update_selection_state()
        self.update_page_controls()

    def _reset_page(self, _=None):
        self.current_page = 1
        self.reload()

    def _sort_changed(self, _=None):
        index = self.sort_menu.current()
        if 0 <= index < len(self.SORT_KEYS):
            self.sort_key = self.SORT_KEYS[index]
        self._reset_page()

    def reload(self):
        if self.closed:
            return
        self.generation += 1
        generation, page = self.generation, self.current_page
        per_page, sort_key = int(self.items_per_page.get()), self.sort_key
        service, custom = self.service, self.custom_paths
        def query():
            if custom is not None:
                rows = [service.store.record(path) for path in custom]
                rows = [row for row in rows if row and row['extension'] in IMAGE_EXTENSIONS]
                key = 'size' if sort_key.startswith('size') else 'real_time'
                rows.sort(key=lambda row: (row[key], row['path']), reverse=sort_key.endswith('desc'))
                return rows[(page - 1) * per_page:page * per_page], len(rows)
            return service.store.query_files(year=self.year, month=self.month, images_only=True,
                                             sort=sort_key, limit=per_page, offset=(page - 1) * per_page)
        self.app.run_background(query, lambda value: self._accept_page(generation, value),
                                lambda error: self._query_failed(generation, error))

    def _query_failed(self, generation, error):
        if not self.closed and generation == self.generation:
            self.app.show_error(error)

    @staticmethod
    def _signature(row):
        return row['size'], row['mtime_ns'], row['ctime_ns']

    def _accept_page(self, generation, value):
        if self.closed or generation != self.generation:
            return
        rows, count = value
        self.total_pages = max(1, math.ceil(count / int(self.items_per_page.get())))
        if self.current_page > self.total_pages:
            self.current_page = self.total_pages
            self.reload()
            return
        self.images_on_page = rows
        keep = {row['display_path'] for row in rows}
        self.photos = {path: photo for path, photo in self.photos.items() if path in keep}
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        self.thumb_frames, self.thumb_labels, self.thumb_vars, self.widget_paths = {}, {}, {}, {}
        if not rows:
            Label(self.scrollable_frame, text=self.app._('preview_empty'), background=BG,
                  foreground=MUTED, pady=70).grid(row=0, column=0, sticky='ew')
        missing = []
        for index, row in enumerate(rows):
            path = row['display_path']
            card = Frame(self.scrollable_frame, width=182, height=214, background=CARD,
                         highlightbackground=BORDER, highlightthickness=2)
            card.grid_propagate(False)
            card.grid(row=index // self.columns, column=index % self.columns, padx=6, pady=6, sticky='n')
            image = Label(card, text=self.app._('loading_thumbnails'), background=CARD, foreground=MUTED)
            image.place(x=11, y=12, width=156, height=146)
            cached = self.photos.get(path)
            if cached and cached[0] == self._signature(row):
                image.configure(image=cached[1], text='')
            else:
                missing.append(row)
            name = row['name']
            label = Label(card, text=name if len(name) <= 23 else name[:20] + '…', background=CARD,
                          foreground=TEXT, font=(FONT, 9), anchor='w')
            label.place(x=10, y=164, width=157, height=20)
            details = Label(card, text=f'{format_size(row["size"])} · {time.strftime("%Y-%m-%d", time.localtime(row["real_time"]))}',
                            background=CARD, foreground=MUTED, font=(FONT, 8), anchor='w')
            details.place(x=10, y=184, width=159, height=18)
            variable = BooleanVar(self.top, value=path in self.selected_paths)
            check = ttk.Checkbutton(card, variable=variable, style='Card.TCheckbutton',
                                    command=lambda chosen=path: self.on_checkbox_toggle(chosen))
            check.place(x=147, y=3)
            self.thumb_frames[path], self.thumb_labels[path], self.thumb_vars[path] = card, image, variable
            for widget in (card, image, label, details):
                self.widget_paths[widget] = path
                ToolTip(widget, path)
            image.bind('<Double-1>', lambda _, chosen=path: self.app.open_path(chosen))
        self._reflow()
        self.update_page_controls()
        self._update_selection_state()
        if missing:
            self.app.run_background(lambda: self._decode_page(missing),
                                    lambda images: self._accept_images(generation, images))

    @staticmethod
    def _decode_page(rows):
        images = []
        for row in rows:
            try:
                with Image.open(row['display_path']) as source:
                    image = ImageOps.exif_transpose(source)
                    image.thumbnail((150, 142))
                    thumbnail = image.copy()
                images.append((row['display_path'], ThumbnailViewerWindow._signature(row), thumbnail))
            except (OSError, ValueError, Image.DecompressionBombError):
                images.append((row['display_path'], ThumbnailViewerWindow._signature(row), None))
        return images

    def _accept_images(self, generation, images):
        for path, signature, pil_image in images:
            try:
                if self.closed or generation != self.generation or path not in self.thumb_labels:
                    continue
                if pil_image is None:
                    self.thumb_labels[path].configure(text=self.app._('image_unavailable'))
                else:
                    photo = ImageTk.PhotoImage(pil_image, master=self.top)
                    self.photos[path] = (signature, photo)
                    self.thumb_labels[path].configure(image=photo, text='')
            finally:
                if pil_image is not None:
                    pil_image.close()

    def _resize(self, event):
        self.canvas.itemconfigure(self.canvas_window, width=event.width)
        columns = max(1, event.width // 196)
        if columns != self.columns:
            self.columns = columns
            self._reflow()

    def _reflow(self):
        if self.closed:
            return
        for index, row in enumerate(self.images_on_page):
            card = self.thumb_frames.get(row['display_path'])
            if card:
                card.grid_configure(row=index // self.columns, column=index % self.columns)
        for column in range(8):
            self.scrollable_frame.grid_columnconfigure(column, weight=1 if column < self.columns else 0)

    def _on_mousewheel(self, event):
        x, y = event.x_root - self.canvas.winfo_rootx(), event.y_root - self.canvas.winfo_rooty()
        if 0 <= x < self.canvas.winfo_width() and 0 <= y < self.canvas.winfo_height():
            amount = -int(event.delta / 120) if event.delta else (-1 if event.num == 4 else 1)
            self.canvas.yview_scroll(amount, 'units')
            return 'break'

    def _get_current_page_paths(self):
        return [row['display_path'] for row in self.images_on_page]

    def _is_current_page_fully_selected(self):
        paths = self._get_current_page_paths()
        return bool(paths) and all(path in self.selected_paths for path in paths)

    def _update_selection_state(self):
        for path, variable in self.thumb_vars.items():
            selected = path in self.selected_paths
            variable.set(selected)
            self.thumb_frames[path].configure(highlightbackground=ACCENT if selected else BORDER,
                                               background=TINT if selected else CARD)
        count = len(self.selected_paths)
        self.page_select_var.set(self._is_current_page_fully_selected())
        self.page_select_check.configure(state='normal' if self.images_on_page else 'disabled')
        self.selection_label.configure(text=self.app._('selection_count', count))
        self.delete_selected_button.configure(text=self.app._('delete_selected_btn_with_count', count)
                                               if count else self.app._('delete_selected_btn'),
                                               state='normal' if count else 'disabled')
        self.clear_button.configure(state='normal' if count else 'disabled')

    def on_checkbox_toggle(self, path):
        if self.thumb_vars[path].get():
            self.selected_paths.add(path)
        else:
            self.selected_paths.discard(path)
        self._update_selection_state()

    def toggle_current_page_selection(self):
        paths = self._get_current_page_paths()
        if self._is_current_page_fully_selected():
            self.selected_paths.difference_update(paths)
        else:
            self.selected_paths.update(paths)
        self._update_selection_state()

    def clear_selection(self):
        self.selected_paths.clear()
        self._update_selection_state()

    def _drag_press(self, event, deselect):
        if event.widget not in self.widget_paths and event.widget not in (self.canvas, self.scrollable_frame):
            return
        self.drag = {'x': event.x_root, 'y': event.y_root, 'deselect': deselect,
                     'before': set(self.selected_paths), 'active': False,
                     'path': self.widget_paths.get(event.widget)}

    def _drag_motion(self, event):
        if self.drag is None:
            return
        if not self.drag['active'] and abs(event.x_root - self.drag['x']) + abs(event.y_root - self.drag['y']) < 7:
            return
        self.drag['active'] = True
        x0, x1 = sorted((self.drag['x'], event.x_root))
        y0, y1 = sorted((self.drag['y'], event.y_root))
        hit = set()
        for path, card in self.thumb_frames.items():
            left, top = card.winfo_rootx(), card.winfo_rooty()
            if x0 < left + card.winfo_width() and x1 > left and y0 < top + card.winfo_height() and y1 > top:
                hit.add(path)
        self.selected_paths = self.drag['before'] - hit if self.drag['deselect'] else self.drag['before'] | hit
        self._update_selection_state()
        coords = (self.canvas.canvasx(x0 - self.canvas.winfo_rootx()),
                  self.canvas.canvasy(y0 - self.canvas.winfo_rooty()),
                  self.canvas.canvasx(x1 - self.canvas.winfo_rootx()),
                  self.canvas.canvasy(y1 - self.canvas.winfo_rooty()))
        if self.selection_rect is None:
            self.selection_rect = self.canvas.create_rectangle(*coords, outline=ACCENT, dash=(4, 3), width=2)
        else:
            self.canvas.coords(self.selection_rect, *coords)

    def _drag_release(self, event):
        if self.drag is None:
            return
        drag, self.drag = self.drag, None
        if self.selection_rect is not None:
            self.canvas.delete(self.selection_rect)
            self.selection_rect = None
        if drag['active']:
            return
        path = drag['path']
        if path and drag['deselect']:
            self.clicked_image_path = path
            try:
                self.context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.context_menu.grab_release()
        elif path:
            if path in self.selected_paths:
                self.selected_paths.discard(path)
            else:
                self.selected_paths.add(path)
            self._update_selection_state()

    def update_page_controls(self):
        self.page_number.set(str(self.current_page))
        self.total_pages_label.configure(text=self.app._('page_count_suffix', self.total_pages))
        self.prev_button.configure(state='normal' if self.current_page > 1 else 'disabled')
        self.next_button.configure(state='normal' if self.current_page < self.total_pages else 'disabled')

    def change_page(self, delta):
        page = max(1, min(self.total_pages, self.current_page + delta))
        if page != self.current_page:
            self.current_page = page
            self.canvas.yview_moveto(0)
            self.reload()

    def on_goto_page(self, _=None):
        try:
            page = int(self.page_number.get())
        except ValueError:
            self.update_page_controls()
            return
        self.current_page = max(1, min(self.total_pages, page))
        self.canvas.yview_moveto(0)
        self.reload()

    def delete_selected_images(self):
        if self.selected_paths:
            paths = sorted(self.selected_paths)
            self.app.request_recycle(paths, self.app._('cleanup_reason_selected', len(paths)), parent=self.top)

    def delete_image(self):
        if self.clicked_image_path:
            self.app.request_recycle([self.clicked_image_path], self.app._('cleanup_reason_selected', 1), parent=self.top)

    def remove_recycled(self, paths):
        self.selected_paths.difference_update(paths)
        if self.custom_paths is not None:
            removed = set(map(path_key, paths))
            self.custom_paths = tuple(path for path in self.custom_paths if path_key(path) not in removed)
        self.reload()

    def open_image(self):
        if self.clicked_image_path:
            self.app.open_path(self.clicked_image_path)

    def open_image_directory(self):
        if self.clicked_image_path:
            self.app.open_path(os.path.dirname(self.clicked_image_path))

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        self.photos.clear()
        if self in self.app.viewers:
            self.app.viewers.remove(self)
        self.top.destroy()
