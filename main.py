#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - エントリーポイント

使い方:
    フォルダ名に関わらず、このファイルを直接実行できます:
    python main.py
"""

import sys
import os

# pygameのサポートメッセージ（Hello from the pygame community...）を非表示にする
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

# main.py のある場所（アプリのルートフォルダ）を基準に解決する
# これにより、フォルダ名が inspection_app 以外でも動作する
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    # PyInstallerでパッケージ化された場合
    _app_dir = sys._MEIPASS
else:
    # 通常の実行時: main.py と同じフォルダを _app_dir とする
    _app_dir = os.path.dirname(os.path.abspath(__file__))

# _app_dir/{modules} を直接 sys.path に追加する必要はなく、
# _app_dir の親を追加してパッケージ名でインポートするのではなく、
# _app_dir そのものを追加して「modules」をトップレベルパッケージとして使う
if _app_dir not in sys.path:
    sys.path.insert(0, _app_dir)

# カレントディレクトリも強制的にアプリルートに合わせる
# (settings.json等の相対パスがどこから実行しても正しく解決されるようにする)
os.chdir(_app_dir)

from modules.app import InspectionSystem  # noqa: E402


if __name__ == "__main__":
    app = InspectionSystem()
    app.root.mainloop()
