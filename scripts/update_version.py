#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Update version.txt with the given version string.

Usage:
    python scripts/update_version.py <version> [path/to/version.txt]

Example:
    python scripts/update_version.py 1.1.3
    python scripts/update_version.py 1.2.0 version.txt
"""
import re
import sys
from pathlib import Path
from typing import Union


def parse_version_tuple(version: str) -> str:
    """把 '1.1.3' / '1.2.0.4' / '1.2.3-beta' 解析为 PyInstaller 需要的 4 元组字符串。

    返回示例: '1, 1, 3, 0'
    """
    # 去掉 pre-release / build 后缀 (例如 1.2.3-beta, 1.2.3+build5)
    base = re.split(r"[-+]", version, maxsplit=1)[0]
    parts = base.split(".")
    int_parts = []
    for p in parts[:4]:
        try:
            int_parts.append(int(p))
        except ValueError:
            int_parts.append(0)
    while len(int_parts) < 4:
        int_parts.append(0)
    return ", ".join(str(x) for x in int_parts)


def update_version_file(version: str, file_path: Union[str, Path] = "version.txt") -> None:
    file_path = Path(file_path)
    ver_tuple = parse_version_tuple(version)

    content = file_path.read_text(encoding="utf-8")

    # 1) filevers=(1, 0, 0, 0)  ->  filevers=(1, 1, 3, 0)
    content = re.sub(
        r"filevers=\([^)]*\)",
        f"filevers=({ver_tuple})",
        content,
    )
    # 2) prodvers=(1, 0, 0, 0)  ->  prodvers=(1, 1, 3, 0)
    content = re.sub(
        r"prodvers=\([^)]*\)",
        f"prodvers=({ver_tuple})",
        content,
    )
    # 3) StringStruct(u'FileVersion', u'xxx')
    content = re.sub(
        r"StringStruct\(u'FileVersion',\s*u'[^']*'\)",
        f"StringStruct(u'FileVersion', u'{version}')",
        content,
    )
    # 4) StringStruct(u'ProductVersion', u'xxx')
    content = re.sub(
        r"StringStruct\(u'ProductVersion',\s*u'[^']*'\)",
        f"StringStruct(u'ProductVersion', u'{version}')",
        content,
    )

    file_path.write_text(content, encoding="utf-8")
    print(f"[update_version] Wrote version '{version}' (tuple {ver_tuple}) -> {file_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/update_version.py <version> [path/to/version.txt]")
        sys.exit(1)
    ver = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else "version.txt"
    update_version_file(ver, path)