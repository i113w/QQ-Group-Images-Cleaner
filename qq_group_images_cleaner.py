#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import argparse
from pathlib import Path
from tkinter import Tk
from lib.QQCleanerApp import QQCleanerApp

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Browse indexed QQ image caches and review recycling plans.')
    parser.add_argument('--folder', type=Path, help='Image folder to load at startup')
    parser.add_argument('--state-dir', type=Path, help='Folder for local indexes and settings')
    args = parser.parse_args()
    root = Tk()
    app = QQCleanerApp(root, state_dir=args.state_dir, autoload=args.folder is None)
    if args.folder:
        root.after(50, lambda: app.set_folder_path(str(args.folder)))
    root.mainloop()
