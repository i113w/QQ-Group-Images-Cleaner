"""Main window: cached indexes, live changes, browsing and reviewed recycling."""

from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import threading
import time
from tkinter import BooleanVar, Menu, StringVar, filedialog, messagebox, simpledialog, ttk

from lib.ConfirmationDialog import ConfirmationDialog
from lib.file_metadata import IMAGE_EXTENSIONS, format_size
from lib.i18n import I18N_STRINGS
from lib.index_service import IndexService
from lib.safe_recycle import NoRecycleTargets, make_plan, recycle, save_manifest
from lib.theme import configure_theme
from lib.ToolTip import ToolTip
from lib.ThumbnailViewerWindow import ThumbnailViewerWindow
from lib.ui_dispatch import UiDispatcher


class QQCleanerApp:
    def __init__(self, root, *, state_dir=None, autoload=True):
        self.root = root
        base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
        self.state_dir = Path(state_dir) if state_dir else base / '.qq-cleaner'
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            if state_dir is not None:
                raise
            local_data = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local' / 'share')))
            self.state_dir = local_data / 'QQ-Group-Images-Cleaner'
            self.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.state_dir / 'settings.json'
        try:
            self.settings = json.loads(self.settings_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.settings = {}
        if not isinstance(self.settings, dict):
            self.settings = {}
        self.lang = self.settings.get('lang', 'zh')
        if not isinstance(self.lang, str) or self.lang not in I18N_STRINGS:
            self.lang = 'zh'
        self.service = None
        self.summary = {'months': [], 'file_count': 0, 'total_size': 0, 'errors': []}
        self.closed = False
        self.busy_kind = ''
        self.cancel_event = threading.Event()
        self.service_token = 0
        self.page_token = 0
        self.file_page = 1
        self.file_page_size = 50
        self.file_rows = {}
        self.viewers = []
        self.active_dialog = None
        self._search_timer = None
        self._next_fallback = time.monotonic() + 10
        self.dispatch = UiDispatcher(root)
        self.index_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='index-service')
        self.query_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='index-queries')
        self.root_path = StringVar(root)
        self.status = StringVar(root)
        self.live_status = StringVar(root)
        self.updated_status = StringVar(root)
        self.search = StringVar(root)
        self.month_filter = StringVar(root)
        self.images_only = BooleanVar(root, value=False)
        now = datetime.now()
        cutoff = now.year * 12 + now.month - 1 - 6
        self.cutoff_year = StringVar(root, value=str(cutoff // 12))
        self.cutoff_month = StringVar(root, value=f'{cutoff % 12 + 1:02d}')
        self.style = configure_theme(root)
        self.setup_ui()
        self.update_ui_language()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind('<F5>', lambda _: self.start_scan())
        self.root.bind('<Control-f>', self.focus_search)
        self.root.after(400, self._poll_live)
        saved_root = self.settings.get('last_root')
        if autoload and isinstance(saved_root, str) and saved_root and os.path.isdir(saved_root):
            self.root.after(50, lambda: self.set_folder_path(saved_root))

    def _(self, key, *args, **kwargs):
        return I18N_STRINGS[self.lang].get(key, key).format(*args, **kwargs)

    def setup_ui(self):
        self.root.minsize(940, 720)
        self.root.geometry('1100x800')
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)
        menubar = Menu(self.root, tearoff=0)
        self.language_menu = Menu(menubar, tearoff=0)
        self.language_menu.add_command(label='中文', command=lambda: self.set_language('zh'))
        self.language_menu.add_command(label='English', command=lambda: self.set_language('en'))
        menubar.add_cascade(label=self._('language_menu'), menu=self.language_menu)
        self.root.configure(menu=menubar)

        header = ttk.Frame(self.root, padding=(28, 16, 28, 12))
        header.grid(row=0, column=0, sticky='ew')
        header.columnconfigure(1, weight=1)
        self.title_label = ttk.Label(header, style='Title.TLabel')
        self.title_label.grid(row=0, column=0, sticky='w')
        self.subtitle_label = ttk.Label(header, style='Muted.TLabel', wraplength=530)
        self.subtitle_label.grid(row=0, column=1, sticky='w', padx=(22, 0))

        folder = ttk.Frame(self.root, style='Card.TFrame', padding=10)
        folder.grid(row=1, column=0, sticky='ew', padx=28, pady=(0, 10))
        folder.columnconfigure(1, weight=1)
        self.folder_label = ttk.Label(folder, style='Card.TLabel')
        self.folder_label.grid(row=0, column=0, sticky='w', padx=(0, 12))
        ttk.Entry(folder, textvariable=self.root_path, state='readonly').grid(row=0, column=1, sticky='ew')
        self.select_folder_button = ttk.Button(folder, command=self.select_folder)
        self.select_folder_button.grid(row=0, column=2, padx=(10, 0))
        self.auto_select_button = ttk.Button(folder, command=self.auto_select_folder)
        self.auto_select_button.grid(row=0, column=3, padx=(8, 0))

        metrics = ttk.Frame(self.root)
        metrics.grid(row=2, column=0, sticky='ew', padx=28, pady=(0, 12))
        self.metric_labels, self.metric_values = [], []
        for column in range(3):
            metrics.columnconfigure(column, weight=1, uniform='metrics')
            card = ttk.Frame(metrics, style='Card.TFrame', padding=(20, 8))
            card.grid(row=0, column=column, sticky='ew', padx=(0 if column == 0 else 7, 0 if column == 2 else 7))
            label = ttk.Label(card, style='CardMuted.TLabel')
            label.pack(anchor='w')
            value = ttk.Label(card, text='—', style='Metric.TLabel')
            value.pack(anchor='w', pady=(2, 0))
            self.metric_labels.append(label)
            self.metric_values.append(value)

        content = ttk.Frame(self.root)
        content.grid(row=3, column=0, sticky='nsew', padx=28)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(content)
        toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 6))
        self.scan_button = ttk.Button(toolbar, style='Accent.TButton', command=self.start_scan, state='disabled')
        self.scan_button.pack(side='left')
        self.rebuild_button = ttk.Button(toolbar, command=lambda: self.start_scan(force=True), state='disabled')
        self.rebuild_button.pack(side='left', padx=8)
        self.stop_button = ttk.Button(toolbar, command=self.cancel_index, state='disabled')
        self.stop_button.pack(side='left')
        self.live_label = ttk.Label(toolbar, textvariable=self.live_status, style='Muted.TLabel')
        self.live_label.pack(side='right')
        self.live_tip = ToolTip(self.live_label, self._('offline_hint'))
        ToolTip(self.rebuild_button, self._('verify_hint'))

        self.notebook = ttk.Notebook(content)
        self.notebook.grid(row=1, column=0, sticky='nsew')
        self.month_tab = ttk.Frame(self.notebook, style='Card.TFrame', padding=12)
        self.index_tab = ttk.Frame(self.notebook, style='Card.TFrame', padding=12)
        self.notebook.add(self.month_tab)
        self.notebook.add(self.index_tab)
        self.month_tab.columnconfigure(0, weight=1)
        self.month_tab.rowconfigure(1, weight=1)
        self.month_hint = ttk.Label(self.month_tab, style='CardMuted.TLabel')
        self.month_hint.grid(row=0, column=0, columnspan=2, sticky='w', pady=(2, 10))
        self.tree = ttk.Treeview(self.month_tab, columns=('month', 'size', 'count'), show='headings', selectmode='browse')
        self.tree.column('month', anchor='w', width=350)
        self.tree.column('size', anchor='e', width=200)
        self.tree.column('count', anchor='e', width=180)
        self.tree.grid(row=1, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(self.month_tab, orient='vertical', command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.tag_configure('alternate', background='#F8FAFD')
        self.tree.bind('<Double-1>', self.on_tree_double_click)
        self.tree.bind('<Return>', self.on_tree_double_click)
        self.empty_label = ttk.Label(self.tree, style='CardMuted.TLabel')
        self.empty_label.place(relx=.5, rely=.5, anchor='center')

        self.index_tab.columnconfigure(0, weight=1)
        self.index_tab.rowconfigure(1, weight=1)
        filters = ttk.Frame(self.index_tab, style='Card.TFrame')
        filters.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(2, 12))
        filters.columnconfigure(1, weight=1)
        self.search_label = ttk.Label(filters, style='CardMuted.TLabel')
        self.search_label.grid(row=0, column=0, padx=(0, 10))
        self.search_entry = ttk.Entry(filters, textvariable=self.search)
        self.search_entry.grid(row=0, column=1, sticky='ew')
        self.search.trace_add('write', self._schedule_search)
        self.month_combo = ttk.Combobox(filters, textvariable=self.month_filter, state='readonly', width=15)
        self.month_combo.grid(row=0, column=2, padx=10)
        self.month_combo.bind('<<ComboboxSelected>>', lambda _: self.reset_file_page())
        self.image_check = ttk.Checkbutton(filters, variable=self.images_only, style='Card.TCheckbutton',
                                           command=self.reset_file_page)
        self.image_check.grid(row=0, column=3)
        self.file_tree = ttk.Treeview(self.index_tab, columns=('name', 'date', 'size', 'path'),
                                      show='headings', selectmode='extended')
        for column, width, anchor in (('name', 210, 'w'), ('date', 110, 'w'), ('size', 100, 'e'), ('path', 480, 'w')):
            self.file_tree.column(column, width=width, anchor=anchor, stretch=column in ('name', 'path'))
        self.file_tree.grid(row=1, column=0, sticky='nsew')
        file_scroll = ttk.Scrollbar(self.index_tab, orient='vertical', command=self.file_tree.yview)
        file_scroll.grid(row=1, column=1, sticky='ns')
        self.file_tree.configure(yscrollcommand=file_scroll.set)
        file_xscroll = ttk.Scrollbar(self.index_tab, orient='horizontal', command=self.file_tree.xview)
        file_xscroll.grid(row=2, column=0, sticky='ew')
        self.file_tree.configure(xscrollcommand=file_xscroll.set)
        self.file_tree.tag_configure('alternate', background='#F8FAFD')
        self.empty_file_label = ttk.Label(self.file_tree, style='CardMuted.TLabel')
        self.file_tree.bind('<Double-1>', lambda _: self.preview_files())
        self.file_tree.bind('<Return>', lambda _: self.preview_files())
        pagebar = ttk.Frame(self.index_tab, style='Card.TFrame')
        pagebar.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(10, 0))
        self.file_preview_button = ttk.Button(pagebar, command=self.preview_files)
        self.file_preview_button.pack(side='left')
        self.file_open_button = ttk.Button(pagebar, command=self.open_selected_file)
        self.file_open_button.pack(side='left', padx=8)
        self.file_next = ttk.Button(pagebar, command=lambda: self.change_file_page(1))
        self.file_next.pack(side='right')
        self.file_prev = ttk.Button(pagebar, command=lambda: self.change_file_page(-1))
        self.file_prev.pack(side='right', padx=6)
        self.file_page_label = ttk.Label(pagebar, style='CardMuted.TLabel')
        self.file_page_label.pack(side='right', padx=10)

        cleanup = ttk.Frame(self.root, style='Card.TFrame', padding=(18, 10))
        cleanup.grid(row=4, column=0, sticky='ew', padx=28, pady=(12, 10))
        cleanup.columnconfigure(1, weight=1)
        cleanup_text = ttk.Frame(cleanup, style='Card.TFrame')
        cleanup_text.grid(row=0, column=0, sticky='w')
        self.delete_label = ttk.Label(cleanup_text, style='Heading.TLabel')
        self.delete_label.pack(anchor='w')
        self.cleanup_hint = ttk.Label(cleanup_text, style='CardMuted.TLabel', wraplength=360)
        self.cleanup_hint.pack(anchor='w', pady=(2, 0))
        controls = ttk.Frame(cleanup, style='Card.TFrame')
        controls.grid(row=0, column=2)
        self.cutoff_label = ttk.Label(controls, style='CardMuted.TLabel')
        self.cutoff_label.pack(side='left', padx=(0, 8))
        self.year_spinbox = ttk.Spinbox(controls, from_=1970, to=datetime.now().year,
                                       textvariable=self.cutoff_year, width=6)
        self.year_spinbox.pack(side='left')
        ttk.Label(controls, text=' / ', style='CardMuted.TLabel').pack(side='left')
        self.month_spinbox = ttk.Spinbox(controls, from_=1, to=12, textvariable=self.cutoff_month, width=4)
        self.month_spinbox.pack(side='left')
        self.delete_button = ttk.Button(controls, command=self.start_delete, style='Recycle.TButton', state='disabled')
        self.delete_button.pack(side='left', padx=(12, 0))

        statusbar = ttk.Frame(self.root, padding=(28, 0, 28, 8))
        statusbar.grid(row=5, column=0, sticky='ew')
        statusbar.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(statusbar, textvariable=self.status, style='Muted.TLabel')
        self.status_label.grid(row=0, column=0, sticky='w')
        ttk.Label(statusbar, textvariable=self.updated_status, style='Muted.TLabel').grid(row=0, column=1, sticky='e')
        self.progress = ttk.Progressbar(statusbar, mode='indeterminate')
        self.progress.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(4, 0))

    def update_ui_language(self):
        self.root.title(self._('window_title'))
        self.root.nametowidget(self.root.cget('menu')).entryconfig(0, label=self._('language_menu'))
        pairs = ((self.title_label, 'window_title'), (self.subtitle_label, 'app_subtitle'),
                 (self.folder_label, 'folder_label'), (self.select_folder_button, 'select_folder_btn'),
                 (self.auto_select_button, 'auto_select_folder_btn'), (self.scan_button, 'scan_files_btn'),
                 (self.rebuild_button, 'rebuild_btn'), (self.stop_button, 'stop_index_btn'),
                 (self.month_hint, 'month_hint'), (self.empty_label, 'empty_index'),
                 (self.search_label, 'search_label'), (self.image_check, 'images_only'),
                 (self.file_preview_button, 'preview_selected'), (self.file_open_button, 'open_selected'),
                 (self.empty_file_label, 'empty_files'),
                 (self.file_prev, 'prev_page'), (self.file_next, 'next_page'),
                 (self.delete_label, 'delete_prompt'), (self.cleanup_hint, 'cleanup_hint'),
                 (self.cutoff_label, 'cutoff_label'), (self.delete_button, 'delete_files_btn'))
        for widget, key in pairs:
            widget.configure(text=self._(key))
        for widget, key in zip(self.metric_labels, ('total_files', 'total_size', 'total_months')):
            widget.configure(text=self._(key))
        self.notebook.tab(self.month_tab, text=self._('month_tab'))
        self.notebook.tab(self.index_tab, text=self._('index_tab'))
        self.live_tip.text = self._('offline_hint')
        for column, key in (('month', 'tree_month'), ('size', 'tree_size'), ('count', 'tree_count')):
            self.tree.heading(column, text=self._(key))
        for column, key in (('name', 'file_name'), ('date', 'file_date'), ('size', 'file_size'), ('path', 'file_path')):
            self.file_tree.heading(column, text=self._(key))
        if not self.service:
            self.status.set(self._('status_select_folder'))
        elif not self.busy_kind:
            self.status.set(self._('status_partial', len(self.summary['errors'])) if self.summary['errors'] else
                            self._('status_scan_complete', self.summary['file_count'], len(self.summary['months'])))
        self.show_summary(self.summary)
        self.reset_file_page()

    def set_language(self, lang):
        if lang not in I18N_STRINGS:
            return
        self.lang = lang
        self.settings['lang'] = lang
        self._save_settings()
        self.update_ui_language()
        for viewer in list(self.viewers):
            viewer.update_language()

    def _save_settings(self):
        self.settings_path.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding='utf-8')

    def run_background(self, work, success, error=None, *, executor=None):
        future = (executor or self.query_executor).submit(work)
        def completed(done):
            if self.closed or done.cancelled():
                return
            try:
                value = done.result()
            except Exception as failure:
                self.dispatch.post(error or self.show_error, failure)
            else:
                self.dispatch.post(success, value)
        future.add_done_callback(completed)
        return future

    def show_error(self, error):
        if not self.closed:
            messagebox.showerror(self._('error_title'), str(error), parent=self.root)

    def set_busy(self, kind=''):
        self.busy_kind = kind
        state = 'disabled' if kind else 'normal'
        self.select_folder_button.configure(state=state)
        self.auto_select_button.configure(state=state)
        for widget in (self.scan_button, self.rebuild_button):
            widget.configure(state='normal' if self.service and not kind else 'disabled')
        self.stop_button.configure(state='normal' if kind == 'index' else 'disabled')
        self.delete_button.configure(state='normal' if self.service and self.summary['file_count'] and not kind else 'disabled')
        if kind:
            self.progress.configure(mode='indeterminate')
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode='determinate', value=0)

    def select_folder(self):
        if self.busy_kind:
            return
        path = filedialog.askdirectory(parent=self.root, title=self._('folder_dialog_title'))
        if path:
            self.set_folder_path(path)

    @staticmethod
    def get_documents_path():
        if sys.platform == 'win32':
            buffer = ctypes.create_unicode_buffer(32768)
            function = ctypes.WinDLL('shell32').SHGetFolderPathW
            function.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR)
            function.restype = ctypes.c_int32
            if function(None, 5, None, 0, buffer) == 0:
                return buffer.value
        return str(Path.home() / 'Documents')

    def auto_select_folder(self):
        if self.busy_kind:
            return
        base = Path(self.get_documents_path()) / 'Tencent Files'
        candidates = [child / 'Image' / 'Group2' for child in base.iterdir()
                      if child.is_dir() and (child / 'Image' / 'Group2').is_dir()] if base.is_dir() else []
        if len(candidates) == 1:
            self.set_folder_path(str(candidates[0]))
            return
        number = simpledialog.askstring(self._('qq_number_title'), self._('prompt_qq_number'), parent=self.root)
        if not number:
            return
        path = base / number / 'Image' / 'Group2'
        if number.isascii() and number.isdigit() and path.is_dir():
            self.set_folder_path(str(path))
        else:
            messagebox.showwarning(self._('error_find_qq_folder_title'), self._('error_find_qq_folder_msg'), parent=self.root)

    def set_folder_path(self, path):
        if self.busy_kind:
            return
        self.service_token += 1
        token = self.service_token
        self.cancel_event = threading.Event()
        self.set_busy('index')
        self.root_path.set(os.path.abspath(path))
        old_service = self.service
        self.service = None
        for viewer in list(self.viewers):
            viewer.close()
        def prepare():
            if old_service:
                old_service.close()
            service = IndexService(path, self.state_dir / 'indexes')
            service.start_watching()
            return service, service.store.summary()
        def prepared(value):
            service, summary = value
            if self.closed or token != self.service_token:
                service.close()
                return
            self.service = service
            self.settings.update(last_root=service.root, lang=self.lang)
            self._save_settings()
            self.root_path.set(service.root)
            self.show_summary(summary)
            if summary['ready']:
                self.status.set(self._('status_cached', summary['file_count']))
                self.reset_file_page()
            else:
                self.status.set(self._('status_scanning', 0, 0))
            self._start_refresh(token)
        self.run_background(prepare, prepared, lambda error: self._index_failed(token, error), executor=self.index_executor)

    def start_scan(self, *, force=False):
        if self.busy_kind or not self.service:
            return
        self.cancel_event = threading.Event()
        self.set_busy('index')
        self.status.set(self._('status_scanning', 0, 0))
        self._start_refresh(self.service_token, force=force)

    def _start_refresh(self, token, *, force=False, events_only=False):
        service, cancelled = self.service, self.cancel_event
        def work():
            result = service.refresh(force=force, cancel=cancelled, events_only=events_only,
                progress=lambda files, directories: self.dispatch.post(self._show_progress, token, files, directories))
            return result, service.store.summary()
        self.run_background(work, lambda value: self._index_finished(token, value),
                            lambda error: self._index_failed(token, error), executor=self.index_executor)

    def _show_progress(self, token, files, directories):
        if token == self.service_token and not self.closed:
            self.status.set(self._('status_scanning', files, directories))

    def _index_finished(self, token, value):
        if token != self.service_token or self.closed:
            return
        result, summary = value
        self.show_summary(summary)
        self.set_busy()
        self.status.set(self._('status_partial', len(result.errors)) if result.errors else
                        self._('status_update_complete', result.changed, result.files, result.elapsed))
        self.load_file_page()
        for viewer in list(self.viewers):
            viewer.reload()

    def _index_failed(self, token, error):
        if token != self.service_token or self.closed:
            return
        self.set_busy()
        if self.service is None:
            self.show_summary({'months': [], 'file_count': 0, 'total_size': 0, 'errors': []})
            for item in self.file_tree.get_children():
                self.file_tree.delete(item)
            self.file_rows = {}
            self.file_page_label.configure(text=self._('file_page', 1, 1, 0))
        self.status.set(self._('status_index_cancelled') if isinstance(error, InterruptedError)
                        else self._('status_index_error', error))

    def cancel_index(self):
        if self.busy_kind == 'index':
            self.cancel_event.set()

    def _poll_live(self):
        if self.closed:
            return
        if self.service and not self.busy_kind:
            fallback = not self.service.watcher.running and time.monotonic() >= self._next_fallback
            if self.service.pending or fallback:
                self._next_fallback = time.monotonic() + 10
                self.cancel_event = threading.Event()
                self.set_busy('index')
                self._start_refresh(self.service_token, events_only=not fallback)
        self.root.after(400, self._poll_live)

    def show_summary(self, summary):
        self.summary = summary
        values = (f'{summary["file_count"]:,}', format_size(summary['total_size']), str(len(summary['months'])))
        for widget, value in zip(self.metric_values, values):
            widget.configure(text=value)
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, row in enumerate(summary['months']):
            iid = f'{row["year"]}-{row["month"]:02d}'
            self.tree.insert('', 'end', iid=iid,
                values=(iid, format_size(row['total_size']), f'{row["file_count"]:,}'),
                tags=('alternate',) if index % 2 else ())
        self.empty_label.place_forget() if summary['months'] else self.empty_label.place(relx=.5, rely=.5, anchor='center')
        selected = self.month_filter.get()
        months = [self._('all_months'), *(f'{row["year"]}-{row["month"]:02d}' for row in summary['months'])]
        self.month_combo.configure(values=months)
        self.month_filter.set(selected if selected in months else months[0])
        watcher = self.service and self.service.watcher.running
        self.live_status.set(self._('status_watching') if watcher else self._('status_directory_mode') if self.service else '')
        updated = summary.get('last_updated', 0)
        self.updated_status.set(self._('status_last_updated', time.strftime('%Y-%m-%d %H:%M', time.localtime(updated))) if updated else '')

    def _schedule_search(self, *_):
        if self._search_timer:
            self.root.after_cancel(self._search_timer)
        self._search_timer = self.root.after(180, self.reset_file_page)

    def focus_search(self, _=None):
        self.notebook.select(self.index_tab)
        self.search_entry.focus_set()
        return 'break'

    def reset_file_page(self):
        self._search_timer = None
        self.file_page = 1
        self.load_file_page()

    def load_file_page(self):
        if not self.service or self.closed:
            return
        self.page_token += 1
        token, service_token, service = self.page_token, self.service_token, self.service
        arguments = dict(search=self.search.get(), images_only=self.images_only.get(),
                         offset=(self.file_page - 1) * self.file_page_size, limit=self.file_page_size)
        selected = self.month_filter.get()
        if selected != self._('all_months') and '-' in selected:
            arguments['year'], arguments['month'] = map(int, selected.split('-'))
        def display(value):
            if token != self.page_token or service_token != self.service_token or self.closed:
                return
            rows, total = value
            pages = max(1, (total + self.file_page_size - 1) // self.file_page_size)
            if self.file_page > pages:
                self.file_page = pages
                self.load_file_page()
                return
            for item in self.file_tree.get_children():
                self.file_tree.delete(item)
            self.file_rows = {}
            for index, row in enumerate(rows):
                iid = str(index)
                self.file_rows[iid] = row
                date = time.strftime('%Y-%m-%d', time.localtime(row['real_time']))
                self.file_tree.insert('', 'end', iid=iid,
                    values=(row['name'], date, format_size(row['size']), row['display_path']),
                    tags=('alternate',) if index % 2 else ())
            self.file_page_label.configure(text=self._('file_page', self.file_page, pages, total))
            if rows:
                self.empty_file_label.place_forget()
            else:
                self.empty_file_label.place(relx=.5, rely=.5, anchor='center')
            self.file_prev.configure(state='normal' if self.file_page > 1 else 'disabled')
            self.file_next.configure(state='normal' if self.file_page < pages else 'disabled')
        self.run_background(lambda: service.store.query_files(**arguments), display)

    def change_file_page(self, delta):
        self.file_page = max(1, self.file_page + delta)
        self.load_file_page()

    def on_tree_double_click(self, _=None):
        selected = self.tree.selection()
        if selected and self.service:
            year, month = map(int, selected[0].split('-'))
            self.viewers.append(ThumbnailViewerWindow(self.root, self, None, year, month))

    def preview_files(self):
        if self.service is None:
            return
        rows = [self.file_rows[iid] for iid in self.file_tree.selection() if iid in self.file_rows]
        images = [row for row in rows if row['extension'] in IMAGE_EXTENSIONS]
        if not images:
            messagebox.showinfo(self._('window_title'), self._('no_images_selected'), parent=self.root)
            return
        self.viewers.append(ThumbnailViewerWindow(self.root, self, [row['display_path'] for row in images],
                                                  images[0]['year'], images[0]['month']))

    def open_path(self, path):
        try:
            if sys.platform == 'win32':
                os.startfile(path)
            else:
                import subprocess
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', path])
        except OSError as error:
            messagebox.showerror(self._('error_title'), self._('error_open', error), parent=self.root)

    def open_selected_file(self):
        selection = self.file_tree.selection()
        if selection and selection[0] in self.file_rows:
            self.open_path(self.file_rows[selection[0]]['display_path'])

    def start_delete(self):
        if self.busy_kind or not self.service:
            return
        try:
            year, month = int(self.cutoff_year.get()), int(self.cutoff_month.get())
            if not 1970 <= year <= datetime.now().year or not 1 <= month <= 12:
                raise ValueError
        except ValueError:
            messagebox.showerror(self._('error_title'), self._('error_invalid_date'), parent=self.root)
            return
        service = self.service
        self.request_recycle(lambda: [row['display_path'] for row in service.store.files_before(year, month)],
                             self._('cleanup_reason_date', year, month), cutoff=(year, month))

    def request_recycle(self, paths, reason, *, parent=None, completed=None, cutoff=None):
        if self.busy_kind or not self.service:
            return
        service = self.service
        self.set_busy('plan')
        self.status.set(self._('status_plan'))
        def prepare():
            selected = paths() if callable(paths) else paths
            if not selected:
                return None
            try:
                plan = make_plan(service.root, selected, reason, cutoff=cutoff)
            except NoRecycleTargets:
                return None
            manifest = save_manifest(plan, self.state_dir / 'recycle-plans')
            return plan, manifest
        def reviewed(value):
            if value is None:
                self.set_busy()
                self.status.set(self._('no_files_to_recycle'))
                return
            plan, manifest = value
            dialog = ConfirmationDialog(parent or self.root, self, plan, manifest)
            self.active_dialog = dialog
            self.root.wait_window(dialog.top)
            self.active_dialog = None
            if self.closed:
                return
            if not dialog.approval:
                self.set_busy()
                self.status.set(self._('status_deletion_cancelled'))
                return
            self.set_busy('recycle')
            self.progress.stop()
            self.progress.configure(mode='determinate', maximum=len(plan.targets), value=0)
            owner = (parent or self.root).winfo_id()
            def execute():
                result = recycle(plan, dialog.approval, owner_window=owner,
                    progress=lambda current, total: self.dispatch.post(self._recycle_progress, current, total))
                service.mark_recycled(result.recycled)
                return result, service.store.summary()
            def finished(value):
                result, summary = value
                self.show_summary(summary)
                self.set_busy()
                self.status.set(self._('status_delete_complete', len(result.recycled)))
                self.load_file_page()
                for viewer in list(self.viewers):
                    viewer.remove_recycled(result.recycled)
                if completed:
                    completed(result.recycled)
                if result.stopped_reason:
                    messagebox.showerror(self._('error_title'), self._('recycle_stopped', result.stopped_reason), parent=parent or self.root)
            self.run_background(execute, finished, failed, executor=self.index_executor)
        def failed(error):
            self.set_busy()
            self.status.set(self._('status_index_error', error))
            messagebox.showerror(self._('error_title'), self._('recycle_stopped', error), parent=parent or self.root)
        self.run_background(prepare, reviewed, failed)

    def _recycle_progress(self, current, total):
        self.progress.configure(value=current)
        self.status.set(self._('status_deleting', current, total))

    def close(self):
        if self.busy_kind == 'recycle':
            messagebox.showinfo(self._('window_title'), self._('recycle_wait'), parent=self.root)
            return
        self.closed = True
        self.cancel_event.set()
        for viewer in list(self.viewers):
            viewer.close()
        if self.service:
            self.service.close()
        self.index_executor.shutdown(wait=False, cancel_futures=True)
        self.query_executor.shutdown(wait=False, cancel_futures=True)
        self.dispatch.close()
        self.root.destroy()
