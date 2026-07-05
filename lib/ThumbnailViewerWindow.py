import os
import sys
import threading
from tkinter import (messagebox, ttk, Frame, Label, Scrollbar, StringVar,
                     Canvas, Menu, Toplevel, Checkbutton, BooleanVar)
from lib.ToolTip import ToolTip
from lib.ImportCheck import import_PIL
from lib.ConfirmationDialog import ConfirmationDialog

Image, ImageTk = import_PIL()


class ThumbnailViewerWindow:
    def __init__(self, parent, app, image_paths, year, month):
        self.parent = parent
        self.app = app
        self.year = year
        self.month = month

        self.top = Toplevel(parent)
        self.top.title(self.app._('thumb_viewer_title', self.year, self.month))
        self.top.minsize(800, 600)
        self.top.transient(parent)
        self.top.grab_set()

        # Data
        self.all_images = []          # [{path, size, time}, ...]
        self.photo_references = {}    # path -> PhotoImage
        self.thumb_labels = {}        # path -> Label
        self.thumb_vars = {}          # path -> BooleanVar (checkbox state)
        self.thumb_frames = {}        # path -> Frame (for highlight & bbox)
        self.selected_paths = set()   # all selected paths (persists across pages)

        # Rubber-band selection state (left-click → select)
        self.rubber_band_active = False
        self.drag_started = False
        self.selection_rect = None
        self.drag_start_x = None
        self.drag_start_y = None
        self.press_widget = None

        # Rubber-band deselection state (right-click → deselect)
        self.right_rubber_band_active = False
        self.right_drag_started = False
        self.right_selection_rect = None
        self.right_drag_start_x = None
        self.right_drag_start_y = None
        self.right_press_widget = None

        # Pagination / layout
        self.current_page = 1
        self.items_per_page = StringVar(value='20')
        self.sort_option = StringVar()
        self.columns = 4
        self.last_width = 0

        self.setup_ui()
        self.load_image_data(image_paths)

    # ------------------------------------------------------------------ UI
    def setup_ui(self):
        # --- Top: centered delete button + hint ---
        top_btn_frame = Frame(self.top, padx=10)
        top_btn_frame.pack(fill='x', pady=(10, 0))

        self.delete_selected_button = ttk.Button(
            top_btn_frame,
            text=self.app._('delete_selected_btn'),
            command=self.delete_selected_images
        )
        self.delete_selected_button.pack(anchor='center')

        Label(
            top_btn_frame,
            text=self.app._('selection_hint'),
            fg='gray', font=('Segoe UI', 8)
        ).pack(anchor='center', pady=(2, 0))

        # --- Control frame (pagination / sorting) ---
        control_frame = Frame(self.top, padx=10, pady=5)
        control_frame.pack(fill='x')

        Label(control_frame, text=self.app._('items_per_page')).pack(side='left', padx=(0, 5))
        per_page_combo = ttk.Combobox(control_frame, textvariable=self.items_per_page,
                                      state='readonly', width=5)
        per_page_combo['values'] = ['5', '10', '20', '50', '100']
        per_page_combo.bind('<<ComboboxSelected>>', lambda e: self.update_view())
        per_page_combo.pack(side='left')

        Label(control_frame, text=self.app._('sort_by')).pack(side='left', padx=(20, 5))
        sort_menu = ttk.Combobox(control_frame, textvariable=self.sort_option,
                                 state='readonly', width=20)
        sort_menu['values'] = [
            self.app._('sort_time_desc'), self.app._('sort_time_asc'),
            self.app._('sort_size_desc'), self.app._('sort_size_asc')
        ]
        sort_menu.set(self.app._('sort_time_desc'))
        sort_menu.pack(side='left')
        sort_menu.bind('<<ComboboxSelected>>', self.sort_and_update)

        # --- Pagination controls (right side) ---
        self.total_pages_label = Label(control_frame, text="")
        self.total_pages_label.pack(side='right', padx=10)

        self.page_entry = ttk.Entry(control_frame, width=5, justify='center')
        self.page_entry.pack(side='right')
        self.page_entry.bind("<Return>", self.on_goto_page)

        self.goto_label = Label(control_frame, text=self.app._('goto_page'))
        self.goto_label.pack(side='right', padx=(10, 2))

        self.next_button = ttk.Button(control_frame, text=self.app._('next_page'),
                                      command=lambda: self.change_page(1))
        self.next_button.pack(side='right')
        self.prev_button = ttk.Button(control_frame, text=self.app._('prev_page'),
                                      command=lambda: self.change_page(-1))
        self.prev_button.pack(side='right', padx=5)

        # --- Canvas + scrollable frame ---
        main_frame = Frame(self.top, bd=1, relief='sunken')
        main_frame.pack(fill='both', expand=True, padx=10, pady=5)
        self.canvas = Canvas(main_frame)
        scrollbar = Scrollbar(main_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = Frame(self.canvas)

        self.scrollable_frame.bind("<Configure>",
                                   lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # --- Mouse wheel scrolling ---
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        # --- Rubber-band selection bindings (left-click → select) ---
        self.canvas.bind("<ButtonPress-1>", self.on_rubber_band_press)
        self.scrollable_frame.bind("<ButtonPress-1>", self.on_rubber_band_press)
        self.canvas.bind_all("<B1-Motion>", self.on_rubber_band_drag)
        self.canvas.bind_all("<ButtonRelease-1>", self.on_rubber_band_release)

        # --- Rubber-band deselection bindings (right-click → deselect) ---
        self.canvas.bind("<ButtonPress-3>", self.on_right_rubber_band_press)
        self.scrollable_frame.bind("<ButtonPress-3>", self.on_right_rubber_band_press)
        self.canvas.bind_all("<B3-Motion>", self.on_right_rubber_band_drag)
        self.canvas.bind_all("<ButtonRelease-3>", self.on_right_rubber_band_release)

        # --- Resize ---
        self.top.bind('<Configure>', self.on_resize)

        # --- Right-click context menu ---
        self.context_menu = Menu(self.top, tearoff=0)
        self.context_menu.add_command(label=self.app._('context_delete'), command=self.delete_image)
        self.context_menu.add_command(label=self.app._('context_open'), command=self.open_image)
        self.context_menu.add_command(label=self.app._('context_open_dir'),
                                      command=self.open_image_directory)
        self.clicked_image_path = None

    # ---------------------------------------------------------- Mouse wheel
    def _on_mousewheel(self, event):
        """Handle mouse wheel scrolling on the canvas."""
        if sys.platform == "win32":
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        elif sys.platform == "darwin":
            self.canvas.yview_scroll(int(-1 * event.delta), "units")
        else:
            if event.num == 4:
                self.canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                self.canvas.yview_scroll(1, "units")

    def on_resize(self, event):
        """Handle window resize to reflow thumbnails."""
        new_width = event.width
        if new_width != self.last_width:
            self.last_width = new_width
            new_columns = max(1, (self.canvas.winfo_width() - 20) // 180)
            if new_columns != self.columns:
                self.columns = new_columns
                self.update_view(force_reload=False)

    # ---------------------------------------------------------- Data loading
    def load_image_data(self, image_paths):
        """Load image metadata in a separate thread."""
        threading.Thread(target=self._load_image_data_thread,
                         args=(image_paths,), daemon=True).start()

    def _load_image_data_thread(self, image_paths):
        temp_list = []
        for path in image_paths:
            try:
                stat = os.stat(path)
                # Fix #7: use min(ctime, mtime, atime) as the "real time"
                real_time = min(stat.st_ctime, stat.st_mtime, stat.st_atime)
                temp_list.append({'path': path, 'size': stat.st_size, 'time': real_time})
            except FileNotFoundError:
                continue
        self.all_images = temp_list
        self.top.after(0, self.sort_and_update)

    # ---------------------------------------------------------- Sort / view
    def sort_and_update(self, event=None):
        """Sorts the master list of images and updates the view."""
        sort_key = self.sort_option.get()
        reverse = True
        if sort_key in [self.app._('sort_time_asc'), self.app._('sort_size_asc')]:
            reverse = False

        key_func = 'time'
        if sort_key in [self.app._('sort_size_asc'), self.app._('sort_size_desc')]:
            key_func = 'size'

        self.all_images.sort(key=lambda x: x[key_func], reverse=reverse)
        self.current_page = 1
        self.update_view()

    def update_view(self, force_reload=True):
        """Clears and repopulates the thumbnail view for the current page."""
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()

        if force_reload:
            self.photo_references.clear()

        # Per-page widget dicts are rebuilt in populate_thumbnails
        self.thumb_labels.clear()
        self.thumb_vars.clear()
        self.thumb_frames.clear()

        try:
            per_page = int(self.items_per_page.get())
        except ValueError:
            per_page = 20

        total_items = len(self.all_images)
        self.total_pages = (total_items + per_page - 1) // per_page
        if self.total_pages == 0:
            self.total_pages = 1

        start_index = (self.current_page - 1) * per_page
        end_index = start_index + per_page
        self.images_on_page = self.all_images[start_index:end_index]

        # --- Fix #5: prevent memory leak ---
        # Remove PhotoImage references that no longer belong to the current page.
        current_page_paths = set(img['path'] for img in self.images_on_page)
        paths_to_remove = [p for p in list(self.photo_references.keys())
                           if p not in current_page_paths]
        for p in paths_to_remove:
            del self.photo_references[p]

        self.update_page_controls()

        if self.images_on_page:
            missing = [img for img in self.images_on_page
                       if img['path'] not in self.photo_references]
            if missing:
                threading.Thread(target=self._load_thumbnails_thread,
                                 args=(missing, self.images_on_page),
                                 daemon=True).start()
            else:
                self.populate_thumbnails(self.images_on_page)

    # --- Fix #4: thread-safe thumbnail loading ---
    # Worker thread only does Image.open() / thumbnail() (PIL operations).
    # ImageTk.PhotoImage instantiation happens in the main thread via after().
    def _load_thumbnails_thread(self, image_data, full_page_data=None):
        """Load thumbnail images in the background (PIL only — thread-safe).

        Only ``Image.open()`` and ``thumbnail()`` are called here.
        ``ImageTk.PhotoImage`` instantiation is deferred to the main
        thread via ``after()`` to keep Tkinter thread-safe.
        """
        for data in image_data:
            path = data['path']
            try:
                image = Image.open(path)
                image.thumbnail((150, 150))
                # Send PIL Image to main thread for PhotoImage creation
                self.top.after(0, self._create_photo_image, path, image)
            except Exception as e:
                print(f"Error loading thumbnail for {path}: {e}")
        # After all PhotoImages are queued, populate the view in main thread
        target_data = full_page_data if full_page_data is not None else image_data
        self.top.after(0, lambda: self.populate_thumbnails(target_data))

    def _create_photo_image(self, path, pil_image):
        """Create ImageTk.PhotoImage in the main thread (thread-safe)."""
        if not self.top.winfo_exists():
            try:
                pil_image.close()
            except Exception:
                pass
            return
        if path in self.photo_references:
            try:
                pil_image.close()
            except Exception:
                pass
            return
        try:
            photo = ImageTk.PhotoImage(pil_image)
            self.photo_references[path] = photo
        except Exception as e:
            print(f"Error creating PhotoImage for {path}: {e}")
        finally:
            try:
                pil_image.close()
            except Exception:
                pass

    def populate_thumbnails(self, image_data):
        """Place loaded thumbnails onto the canvas in a fixed grid."""
        if not self.top.winfo_exists():
            return

        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        self.thumb_labels.clear()
        self.thumb_vars.clear()
        self.thumb_frames.clear()

        for i, data in enumerate(image_data):
            path = data['path']
            photo = self.photo_references.get(path)
            if not photo:
                continue

            row, col = divmod(i, self.columns)

            frame = Frame(self.scrollable_frame, width=170, height=170, bg='white')
            frame.grid(row=row, column=col, padx=5, pady=5, sticky='nsew')
            frame.grid_propagate(False)

            label = Label(frame, image=photo, bg='white')
            label.pack(expand=True)
            self.thumb_labels[path] = label

            # --- Selection checkbox (top-right corner) ---
            is_selected = path in self.selected_paths
            var = BooleanVar(value=is_selected)
            cb = Checkbutton(frame, variable=var, bg='white', activebackground='white',
                             command=lambda p=path, v=var: self.on_checkbox_toggle(p, v))
            cb.place(relx=1.0, rely=0.0, anchor='ne', x=-2, y=2)
            self.thumb_vars[path] = var
            self.thumb_frames[path] = frame

            if is_selected:
                frame.config(highlightbackground='#0078d7', highlightthickness=3)

            # Right-press → start right rubber-band (deselect) or show context menu on release
            label.bind("<ButtonPress-3>", self.on_right_rubber_band_press)
            # Left-press → start rubber-band (or single-click toggle on release)
            label.bind("<ButtonPress-1>", self.on_rubber_band_press)
            ToolTip(label, os.path.basename(path))

        for col_index in range(self.columns):
            self.scrollable_frame.grid_columnconfigure(col_index, weight=1)

        self._update_delete_button_text()

    def update_page_controls(self):
        """Update the state of pagination buttons, entry, and label."""
        has_multiple_pages = self.total_pages > 1
        self.total_pages_label.config(text=self.app._('page_count_suffix', self.total_pages))

        self.page_entry.config(state='normal')  # Temporarily enable to update text
        self.page_entry.delete(0, 'end')
        self.page_entry.insert(0, str(self.current_page))
        self.page_entry.config(state='normal' if has_multiple_pages else 'disabled')

        self.prev_button.config(
            state='normal' if (has_multiple_pages and self.current_page > 1) else 'disabled')
        self.next_button.config(
            state='normal' if (has_multiple_pages and self.current_page < self.total_pages) else 'disabled')

    def change_page(self, delta):
        """Navigate to the previous or next page."""
        new_page = self.current_page + delta
        if 1 <= new_page <= self.total_pages:
            self.current_page = new_page
            self.update_view()

    def on_goto_page(self, event=None):
        """Handle page jump when user presses Enter in the page entry."""
        try:
            target_page = int(self.page_entry.get())
        except ValueError:
            messagebox.showwarning(self.app._('error_title'), self.app._('error_invalid_page'), parent=self.top)
            self.page_entry.delete(0, 'end')
            self.page_entry.insert(0, str(self.current_page))
            return

        if 1 <= target_page <= self.total_pages:
            if target_page != self.current_page:
                self.current_page = target_page
                self.update_view()
        else:
            messagebox.showwarning(self.app._('error_title'), self.app._('error_invalid_page'), parent=self.top)
            self.page_entry.delete(0, 'end')
            self.page_entry.insert(0, str(self.current_page))

    # ---------------------------------------------------------- Context menu
    def show_context_menu(self, event, path):
        self.clicked_image_path = path
        self.context_menu.post(event.x_root, event.y_root)

    # ============================================================ Selection
    def _update_delete_button_text(self):
        count = len(self.selected_paths)
        if count > 0:
            self.delete_selected_button.config(
                text=self.app._('delete_selected_btn_with_count', count))
        else:
            self.delete_selected_button.config(
                text=self.app._('delete_selected_btn'))

    def on_checkbox_toggle(self, path, var):
        """Called when the user clicks a thumbnail's checkbox."""
        if var.get():
            self._select(path)
        else:
            self._deselect(path)
        self._update_delete_button_text()

    def _select(self, path):
        self.selected_paths.add(path)
        if path in self.thumb_vars:
            self.thumb_vars[path].set(True)
        if path in self.thumb_frames:
            self.thumb_frames[path].config(highlightbackground='#0078d7',
                                           highlightthickness=3)

    def _deselect(self, path):
        self.selected_paths.discard(path)
        if path in self.thumb_vars:
            self.thumb_vars[path].set(False)
        if path in self.thumb_frames:
            self.thumb_frames[path].config(highlightthickness=0)

    def _toggle_selection(self, path):
        if path in self.selected_paths:
            self._deselect(path)
        else:
            self._select(path)
        self._update_delete_button_text()

    # ---------------------------------------------------------- Rubber-band (left-click: select)
    def _get_canvas_coords(self, event):
        """Convert event coordinates (from any widget) to canvas coordinates."""
        cx = self.canvas.canvasx(event.x_root - self.canvas.winfo_rootx())
        cy = self.canvas.canvasy(event.y_root - self.canvas.winfo_rooty())
        return cx, cy

    def on_rubber_band_press(self, event):
        self.rubber_band_active = True
        self.drag_started = False
        self.press_widget = event.widget
        self.drag_start_x, self.drag_start_y = self._get_canvas_coords(event)
        # Clean up any leftover rectangle
        if self.selection_rect:
            self.canvas.delete(self.selection_rect)
            self.selection_rect = None

    def on_rubber_band_drag(self, event):
        if not self.rubber_band_active:
            return
        cur_x, cur_y = self._get_canvas_coords(event)

        # Only create the rectangle after a small movement threshold
        if not self.drag_started:
            if (abs(cur_x - self.drag_start_x) < 5 and
                    abs(cur_y - self.drag_start_y) < 5):
                return
            self.drag_started = True
            self.selection_rect = self.canvas.create_rectangle(
                self.drag_start_x, self.drag_start_y, cur_x, cur_y,
                outline='#0078d7', width=2, dash=(4, 2)
            )
        else:
            self.canvas.coords(self.selection_rect,
                               self.drag_start_x, self.drag_start_y,
                               cur_x, cur_y)
        # Real-time selection update
        self._select_within_rect(self.drag_start_x, self.drag_start_y,
                                 cur_x, cur_y)

    def on_rubber_band_release(self, event):
        if not self.rubber_band_active:
            return
        self.rubber_band_active = False

        if self.drag_started:
            # Rubber-band completed — clean up rectangle
            if self.selection_rect:
                self.canvas.delete(self.selection_rect)
                self.selection_rect = None
        else:
            # Single click without drag — toggle the thumbnail under cursor
            if isinstance(self.press_widget, Label):
                path = None
                for p, lbl in self.thumb_labels.items():
                    if lbl is self.press_widget:
                        path = p
                        break
                if path:
                    self._toggle_selection(path)

        self.press_widget = None
        self.drag_started = False

    def _select_within_rect(self, x1, y1, x2, y2):
        """Select every thumbnail whose frame intersects the rectangle."""
        left = min(x1, x2)
        right = max(x1, x2)
        top = min(y1, y2)
        bottom = max(y1, y2)

        for path, frame in self.thumb_frames.items():
            if not frame.winfo_ismapped():
                continue
            fx = frame.winfo_x()
            fy = frame.winfo_y()
            fw = frame.winfo_width()
            fh = frame.winfo_height()

            intersects = (fx < right and fx + fw > left and
                          fy < bottom and fy + fh > top)
            if intersects and path not in self.selected_paths:
                self._select(path)

        self._update_delete_button_text()

    # ---------------------------------------------------------- Rubber-band (right-click: deselect)
    def on_right_rubber_band_press(self, event):
        """Start tracking a right-click for potential drag-deselect."""
        self.right_rubber_band_active = True
        self.right_drag_started = False
        self.right_press_widget = event.widget
        self.right_drag_start_x, self.right_drag_start_y = self._get_canvas_coords(event)
        # Clean up any leftover rectangle
        if self.right_selection_rect:
            self.canvas.delete(self.right_selection_rect)
            self.right_selection_rect = None

    def on_right_rubber_band_drag(self, event):
        """Update the deselection rectangle while right-dragging."""
        if not self.right_rubber_band_active:
            return
        cur_x, cur_y = self._get_canvas_coords(event)

        # Only create the rectangle after a small movement threshold
        if not self.right_drag_started:
            if (abs(cur_x - self.right_drag_start_x) < 5 and
                    abs(cur_y - self.right_drag_start_y) < 5):
                return
            self.right_drag_started = True
            self.right_selection_rect = self.canvas.create_rectangle(
                self.right_drag_start_x, self.right_drag_start_y, cur_x, cur_y,
                outline='#d70000', width=2, dash=(4, 2)
            )
        else:
            self.canvas.coords(self.right_selection_rect,
                               self.right_drag_start_x, self.right_drag_start_y,
                               cur_x, cur_y)
        # Real-time deselection update
        self._deselect_within_rect(self.right_drag_start_x, self.right_drag_start_y,
                                   cur_x, cur_y)

    def on_right_rubber_band_release(self, event):
        """Finalize right-drag deselection, or show context menu on simple right-click."""
        if not self.right_rubber_band_active:
            return
        self.right_rubber_band_active = False

        if self.right_drag_started:
            # Rubber-band deselection completed — clean up rectangle
            if self.right_selection_rect:
                self.canvas.delete(self.right_selection_rect)
                self.right_selection_rect = None
        else:
            # Single right-click without drag — show context menu if on a thumbnail
            if isinstance(self.right_press_widget, Label):
                path = None
                for p, lbl in self.thumb_labels.items():
                    if lbl is self.right_press_widget:
                        path = p
                        break
                if path:
                    self.show_context_menu(event, path)

        self.right_press_widget = None
        self.right_drag_started = False

    def _deselect_within_rect(self, x1, y1, x2, y2):
        """Deselect every selected thumbnail whose frame intersects the rectangle."""
        left = min(x1, x2)
        right = max(x1, x2)
        top = min(y1, y2)
        bottom = max(y1, y2)

        for path, frame in self.thumb_frames.items():
            if not frame.winfo_ismapped():
                continue
            fx = frame.winfo_x()
            fy = frame.winfo_y()
            fw = frame.winfo_width()
            fh = frame.winfo_height()

            intersects = (fx < right and fx + fw > left and
                          fy < bottom and fy + fh > top)
            if intersects and path in self.selected_paths:
                self._deselect(path)

        self._update_delete_button_text()

    # ============================================================ Deletion
    # Fix #6: decouple file deletion from UI cleanup so that a failed
    # deletion does not corrupt in-memory data structures.
    def delete_selected_images(self):
        """Delete all selected images (checkbox + rubber-band) after confirmation."""
        if not self.selected_paths:
            messagebox.showinfo(self.app._('error_title'),
                                self.app._('no_selection_msg'),
                                parent=self.top)
            return

        paths_to_delete = list(self.selected_paths)

        # Build preview list (images only)
        image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.bmp')
        image_paths_to_preview = [p for p in paths_to_delete
                                  if p.lower().endswith(image_extensions)]

        custom_msg = self.app._('confirm_selected_delete_msg', len(paths_to_delete))
        dialog = ConfirmationDialog(
            self.top, self.app, self.year, self.month,
            image_paths_to_preview, custom_message=custom_msg
        )
        self.top.wait_window(dialog.top)

        if not dialog.confirmed:
            return

        deleted_count = 0
        error_count = 0
        deleted_paths = set()

        for path in paths_to_delete:
            try:
                os.remove(path)
                deleted_count += 1
                deleted_paths.add(path)
            except Exception as e:
                print(f"Could not delete {path}: {e}")
                error_count += 1

        # Clean up references ONLY for successfully deleted files
        for path in deleted_paths:
            self.selected_paths.discard(path)
            self.thumb_vars.pop(path, None)
            if path in self.thumb_frames:
                try:
                    self.thumb_frames[path].destroy()
                except Exception:
                    pass
                del self.thumb_frames[path]
            self.thumb_labels.pop(path, None)
            self.photo_references.pop(path, None)
            try:
                if path in self.app.file_data[self.year][self.month]['paths']:
                    self.app.file_data[self.year][self.month]['paths'].remove(path)
            except (KeyError, ValueError):
                pass

        # Update image lists — only remove successfully deleted files
        self.all_images = [img for img in self.all_images
                           if img['path'] not in deleted_paths]

        # Recalculate pagination
        try:
            per_page = int(self.items_per_page.get())
        except ValueError:
            per_page = 20
        self.total_pages = max(1, (len(self.all_images) + per_page - 1) // per_page)
        if self.current_page > self.total_pages:
            self.current_page = self.total_pages

        # Refresh view (reuse cached thumbnails)
        self.update_view(force_reload=False)
        self._update_delete_button_text()

        messagebox.showinfo(
            self.app._('confirm_delete_title'),
            self.app._('status_delete_complete', deleted_count, error_count),
            parent=self.top
        )

    # ---------------------------------------------------- Single-file delete (right-click)
    # Fix #6: if os.remove fails, return early without touching UI state.
    def delete_image(self):
        """Right-click → Delete (with proper exception handling)."""
        path = self.clicked_image_path
        if not path:
            return

        if messagebox.askyesno(
                self.app._('confirm_delete_title'),
                f"确认删除文件?\n{os.path.basename(path)}",
                parent=self.top):
            # --- Step 1: attempt file deletion ---
            try:
                os.remove(path)
            except Exception as e:
                messagebox.showerror(self.app._('error_title'),
                                     f"删除失败: {e}", parent=self.top)
                return  # deletion failed — do NOT modify UI or data

            # --- Step 2: deletion succeeded — safely clean up UI & data ---
            try:
                if path in self.thumb_labels:
                    self.thumb_labels[path].master.destroy()
                    del self.thumb_labels[path]
            except Exception:
                pass

            self.selected_paths.discard(path)
            self.thumb_vars.pop(path, None)
            self.thumb_frames.pop(path, None)
            self.photo_references.pop(path, None)

            self.all_images = [img for img in self.all_images
                               if img['path'] != path]
            self.images_on_page = [img for img in self.images_on_page
                                   if img['path'] != path]

            self.populate_thumbnails(self.images_on_page)
            self.update_page_controls()

            try:
                self.app.file_data[self.year][self.month]['paths'].remove(path)
            except (KeyError, ValueError):
                pass

    def open_image(self):
        if self.clicked_image_path:
            try:
                os.startfile(self.clicked_image_path)
            except AttributeError:
                import subprocess
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.call([opener, self.clicked_image_path])

    def open_image_directory(self):
        if self.clicked_image_path:
            try:
                os.startfile(os.path.dirname(self.clicked_image_path))
            except AttributeError:
                import subprocess
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.call([opener, os.path.dirname(self.clicked_image_path)])