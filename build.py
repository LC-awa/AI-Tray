#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 AI-Tray 打包成单文件 exe。

用法：
    python build.py            # 无控制台窗口（推荐）
    python build.py --console  # 保留控制台，方便看报错

需要先安装：python -m pip install pyinstaller
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "AI-Tray"


def run(args: list[str]) -> None:
    print(">>", subprocess.list2cmdline(args), flush=True)
    subprocess.run(args, cwd=str(ROOT), check=True)


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("未安装 PyInstaller，请先运行：python -m pip install pyinstaller")
        return 1

    run([sys.executable, "ai_tray.py", "--export-icon", "icon.ico"])

    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        NAME,
        "--icon",
        "icon.ico",
        # pystray 在运行时动态导入 Windows 后端，PyInstaller 静态分析找不到
        "--hidden-import",
        "pystray._win32",
    ]
    if "--console" not in sys.argv[1:]:
        args.append("--windowed")
    args.append("ai_tray.py")
    run(args)

    print(f"\n打包完成：{ROOT / 'dist' / (NAME + '.exe')}")
    print("把 config.json 放到 exe 同一个文件夹即可（没有时会自动生成）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
