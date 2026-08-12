#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
constants.py - フォント・カラー・パス定数
"""

import os
from pathlib import Path

# --- バージョン ---
VERSION = "v0.2.1"




# 仕様情報遅延キュー中のサイクル（検査SKIP）を表す内部パターンID
# None と区別し、2つ目以降のトリガーでパターン再決定されないようにする
DELAYED_SKIP_PATTERN_ID = "__DELAYED_SKIP__"

# --- ファイル・パス ---
SETTINGS_FILE = "inspection_settings.json"
RESULTS_DIR = Path(os.path.expanduser("~")) / "results"
RESULTS_SUBDIR_NG_RAW = "NG_RAW"

# --- 有効なBCMピン番号 (Raspberry Pi 40ピンヘッダ) ---
VALID_BCM_PINS = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
                  16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27}

# --- 解像度オプション ---
RES_OPTIONS = ["320x240", "640x480", "1280x720", "1920x1080", "3840x2160"]
RES_OPTIONS_PREVIEW = ["プレビューなし", "320x240", "640x480", "1280x720"]
RES_OPTIONS_SAVE = RES_OPTIONS + ["保存しない"]

# --- フォント定義 ---
FONT_FAMILY = "Meiryo UI"
FONT_NORMAL = (FONT_FAMILY, 14)
FONT_BOLD = (FONT_FAMILY, 16, "bold")
FONT_LARGE = (FONT_FAMILY, 24, "bold")
FONT_HUGE = (FONT_FAMILY, 48, "bold")

# 設定画面用（大型化）
FONT_SET_TAB = (FONT_FAMILY, 18, "bold")  # タブ用
FONT_SET_LBL = (FONT_FAMILY, 16, "bold")
FONT_SET_VAL = (FONT_FAMILY, 16)
FONT_BTN_LARGE = (FONT_FAMILY, 16, "bold")  # 追加・削除ボタン用

# --- カラーパレット (Dark Gray Theme) ---
COLOR_BG_MAIN = "#2b2b2b"       # 背景全体
COLOR_BG_PANEL = "#3c3f41"      # パネル、カード背景
COLOR_BG_INPUT = "#45494a"      # 入力フィールド背景
COLOR_TEXT_MAIN = "#FFFFFF"     # メインテキスト
COLOR_TEXT_SUB = "#B0BEC5"      # サブテキスト
COLOR_ACCENT = "#4FC3F7"        # 水色（ボタン、強調）
COLOR_ACCENT_HOVER = "#81D4FA"

# ステータスカラー
COLOR_OK = "#66BB6A"            # マイルドな緑
COLOR_NG = "#FF5252"            # マイルドな赤
COLOR_NG_MUTED = "#B06666"      # さらに彩度を落とした赤（削除ボタン等）
COLOR_WARNING = "#FFB74D"       # 視認性の高いオレンジ


# ボーダー色
COLOR_BORDER = "#505050"
