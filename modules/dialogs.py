#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dialogs.py - ダイアログウィンドウ
GPIOTestDialog, SettingsDialog
"""

import json
import os
import sys
import time
import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import numpy as np
from pathlib import Path

import cv2
from PIL import Image, ImageTk

# スクリプトを単体で実行する場合に code/ ディレクトリを検索パスに追加する
if __name__ == "__main__" or __package__ is None:
    import sys
    _here = os.path.dirname(os.path.abspath(__file__))
    _code_dir = os.path.dirname(_here)
    if _code_dir not in sys.path:
        sys.path.insert(0, _code_dir)

from .constants import (
    COLOR_BG_MAIN, COLOR_BG_PANEL, COLOR_BG_INPUT,
    COLOR_TEXT_MAIN, COLOR_TEXT_SUB, COLOR_ACCENT, COLOR_OK, COLOR_NG, COLOR_NG_MUTED, COLOR_WARNING,
    COLOR_BORDER,
    FONT_FAMILY, FONT_NORMAL, FONT_BOLD, FONT_LARGE,
    FONT_SET_TAB, FONT_SET_LBL, FONT_SET_VAL, FONT_BTN_LARGE,
    RES_OPTIONS, RES_OPTIONS_PREVIEW, RES_OPTIONS_SAVE,
    VALID_BCM_PINS
)
from .hardware import DigitalInputDevice, OutputDevice
from .widgets import create_card, Tooltip, HelpWindow

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False


def _activate_toplevel(win, parent):
    """Linux/Raspberry Pi で Toplevel がクリックを受け取れない問題への対処。"""
    try:
        win.transient(parent)
    except tk.TclError:
        pass
    if sys.platform.startswith("linux"):
        try:
            win.wm_attributes("-type", "dialog")
        except tk.TclError:
            pass
    try:
        parent.update_idletasks()
        win.update_idletasks()
        win.deiconify()
        win.lift()
    except tk.TclError:
        pass
    try:
        win.wait_visibility()
    except tk.TclError:
        pass

    def _apply_focus():
        if not win.winfo_exists():
            return
        try:
            win.attributes("-topmost", True)
            win.lift()
            win.focus_force()
            win.focus_set()
        except tk.TclError:
            pass
        try:
            win.attributes("-topmost", False)
        except tk.TclError:
            pass
        # Linux では grab_set が WM のフォーカスと競合し、初回クリックが効かないことがある
        if sys.platform == "win32":
            try:
                if not win.grab_current():
                    win.grab_set()
            except tk.TclError:
                pass

    win.after_idle(_apply_focus)
    if sys.platform.startswith("linux"):
        win.after(100, _apply_focus)
        win.after(300, _apply_focus)


def _release_toplevel_modal(win):
    """Toplevel のモーダル状態を解除する。"""
    try:
        current = win.grab_current()
        if current and str(current) == str(win):
            win.grab_release()
    except tk.TclError:
        pass
    try:
        win.attributes("-topmost", False)
    except tk.TclError:
        pass

def detect_available_cameras():
    """OSが認識しているカメラデバイスを探索し、
    [(index_int, display_label_str), ...] のリストを返す。
    ※ UIフリーズ防止のため、VideoCapture による同期的な強制接続テストは行わない。
    """
    import sys
    import subprocess
    import os

    devices = []

    if sys.platform.startswith("linux"):
        # Linux (Raspberry Pi 等): /sys/class/video4linux/video*/name
        v4l_dir = "/sys/class/video4linux"
        if os.path.exists(v4l_dir):
            ignore_keywords = ["codec", "rpivid", "vc4", "media-controller", "bcm2835-isp", "h264", "hevc", "vp8", "fd"]
            entries = sorted(
                os.listdir(v4l_dir),
                key=lambda x: int(x.replace("video", "")) if x.replace("video", "").isdigit() else 999
            )
            for entry in entries:
                if entry.startswith("video"):
                    try:
                        idx = int(entry.replace("video", ""))
                        name_file = os.path.join(v4l_dir, entry, "name")
                        cam_name = f"カメラ {idx}"
                        if os.path.exists(name_file):
                            with open(name_file, "r", encoding="utf-8", errors="ignore") as f:
                                name_text = f.read().strip()
                                if name_text:
                                    cam_name = name_text

                        # 非カメラ（コーダ/デコーダ/ISP/メタデータ）を除外
                        if any(k in cam_name.lower() for k in ignore_keywords):
                            continue

                        devices.append((idx, f"[{idx}] {cam_name}"))
                    except Exception:
                        pass
    elif sys.platform.startswith("win"):
        # Windows: PowerShell で PnP カメラデバイス取得
        names_from_ps = []
        try:
            ps_cmd = 'Get-CimInstance Win32_PnPEntity | Where-Object {$_.PNPClass -eq "Camera" -or $_.PNPClass -eq "Image"} | Select-Object -ExpandProperty Name'
            res = subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, text=True, timeout=2)
            if res.returncode == 0 and res.stdout:
                names_from_ps = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        except Exception:
            pass

        if names_from_ps:
            for idx, d_name in enumerate(names_from_ps):
                devices.append((idx, f"[{idx}] {d_name}"))

    # 一つも検出できない場合、あるいは標準的なインデックス 0〜3 の補完
    existing_indices = {d[0] for d in devices}
    for idx in range(4):
        if idx not in existing_indices:
            devices.append((idx, f"[{idx}] カメラ (インデックス {idx})"))

    devices.sort(key=lambda x: x[0])
    return devices


# ---------------------------------------------------------------------------
# システム日時設定ダイアログ
# ---------------------------------------------------------------------------
class SystemDateTimeDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("本体の時刻設定")
        self.geometry("540x440")
        self.configure(bg=COLOR_BG_MAIN)
        try:
            self.transient(parent)
        except tk.TclError:
            pass

        now = datetime.datetime.now()

        # 最下部ボタンエリア
        f_btns = tk.Frame(self, bg=COLOR_BG_MAIN)
        f_btns.pack(side=tk.BOTTOM, fill=tk.X, pady=20, padx=24)

        def _apply():
            try:
                y = self.v_year.get()
                m = self.v_month.get()
                d = self.v_day.get()
                h = self.v_hour.get()
                mi = self.v_min.get()
                s = self.v_sec.get()
                dt_str = f"{y:04d}-{m:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"
            except Exception as ex:
                messagebox.showerror("入力エラー", f"日時の入力値が不正です:\n{ex}", parent=self)
                return

            if sys.platform.startswith("win"):
                messagebox.showinfo(
                    "設定 (Windows)",
                    f"Windows環境のため実際のシステム日時変更はスキップします。\n設定指定値: {dt_str}\n(Linux/ラズパイ環境で自動設定コマンドを実行します)",
                    parent=self
                )
                self.destroy()
                return

            import subprocess
            cmds = [
                ["sudo", "timedatectl", "set-ntp", "false"],
                ["sudo", "timedatectl", "set-time", dt_str],
                ["sudo", "date", "-s", dt_str],
                ["sudo", "hwclock", "-w"]
            ]
            results = []
            success_count = 0
            for cmd in cmds:
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    if res.returncode == 0:
                        success_count += 1
                        results.append(f"成功: {' '.join(cmd)}")
                    else:
                        err = res.stderr.strip() or res.stdout.strip()
                        results.append(f"失敗 ({' '.join(cmd)}): {err}")
                except Exception as ex:
                    results.append(f"エラー ({' '.join(cmd)}): {ex}")

            msg = f"日時を [{dt_str}] に設定しました。\n\n【実行詳細】\n" + "\n".join(results)
            if success_count > 0:
                messagebox.showinfo("設定完了", msg, parent=self)
                self.destroy()
            else:
                messagebox.showerror("設定失敗", msg, parent=self)

        btn_save = tk.Button(
            f_btns, text="本体に反映", font=(FONT_FAMILY, 11, "bold"),
            bg=COLOR_ACCENT, fg="white", relief="flat", padx=20, pady=8,
            cursor="hand2", command=_apply
        )
        btn_save.pack(side=tk.RIGHT, padx=(10, 0))

        btn_cancel = tk.Button(
            f_btns, text="キャンセル", font=(FONT_FAMILY, 11, "bold"),
            bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN, relief="flat", padx=18, pady=8,
            cursor="hand2", command=self.destroy
        )
        btn_cancel.pack(side=tk.RIGHT)

        # ヘッダータイトル & 説明
        tk.Label(
            self, text="ラズパイ本体の時刻設定", font=FONT_LARGE,
            bg=COLOR_BG_MAIN, fg=COLOR_ACCENT
        ).pack(pady=(20, 6))

        tk.Label(
            self, text="本体のシステム日時を手動設定します。\n(Linux / Raspberry Pi 環境で timedatectl / date を更新します)",
            font=FONT_SET_VAL, bg=COLOR_BG_MAIN, fg=COLOR_TEXT_SUB, justify="center",
            wraplength=500
        ).pack(pady=(0, 16), padx=20)

        # 入力フレーム
        f_dt = tk.Frame(self, bg=COLOR_BG_PANEL, padx=20, pady=20)
        f_dt.pack(padx=24, fill=tk.X, expand=True)

        font_num = (FONT_FAMILY, 14, "bold")
        font_lbl = (FONT_FAMILY, 12, "bold")

        # 年月日
        f_date = tk.Frame(f_dt, bg=COLOR_BG_PANEL)
        f_date.pack(fill=tk.X, pady=8)

        self.v_year = tk.IntVar(value=now.year)
        self.v_month = tk.IntVar(value=now.month)
        self.v_day = tk.IntVar(value=now.day)

        tk.Label(f_date, text="日付:", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=6, anchor="w").pack(side=tk.LEFT)
        sp_y = ttk.Spinbox(f_date, from_=2020, to=2099, increment=1, textvariable=self.v_year, width=6, font=font_num)
        sp_y.pack(side=tk.LEFT, padx=4)
        tk.Label(f_date, text="年", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(0, 10))

        sp_m = ttk.Spinbox(f_date, from_=1, to=12, increment=1, textvariable=self.v_month, width=4, font=font_num)
        sp_m.pack(side=tk.LEFT, padx=4)
        tk.Label(f_date, text="月", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(0, 10))

        sp_d = ttk.Spinbox(f_date, from_=1, to=31, increment=1, textvariable=self.v_day, width=4, font=font_num)
        sp_d.pack(side=tk.LEFT, padx=4)
        tk.Label(f_date, text="日", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)

        # 時分秒
        f_time = tk.Frame(f_dt, bg=COLOR_BG_PANEL)
        f_time.pack(fill=tk.X, pady=8)

        self.v_hour = tk.IntVar(value=now.hour)
        self.v_min = tk.IntVar(value=now.minute)
        self.v_sec = tk.IntVar(value=now.second)

        tk.Label(f_time, text="時刻:", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=6, anchor="w").pack(side=tk.LEFT)
        sp_h = ttk.Spinbox(f_time, from_=0, to=23, increment=1, textvariable=self.v_hour, width=4, font=font_num)
        sp_h.pack(side=tk.LEFT, padx=4)
        tk.Label(f_time, text="時", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(0, 10))

        sp_mi = ttk.Spinbox(f_time, from_=0, to=59, increment=1, textvariable=self.v_min, width=4, font=font_num)
        sp_mi.pack(side=tk.LEFT, padx=4)
        tk.Label(f_time, text="分", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(0, 10))

        sp_s = ttk.Spinbox(f_time, from_=0, to=59, increment=1, textvariable=self.v_sec, width=4, font=font_num)
        sp_s.pack(side=tk.LEFT, padx=4)
        tk.Label(f_time, text="秒", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)

        # 全Spinboxの安全停止ハンドラ
        for sp in [sp_y, sp_m, sp_d, sp_h, sp_mi, sp_s]:
            def _stop_ttk_sp(event=None, widget=sp):
                try:
                    rep = widget.tk.call('set', '::ttk::spinbox::Repeater')
                    if rep: widget.tk.call('after', 'cancel', rep)
                except Exception:
                    pass
            sp.bind("<ButtonRelease-1>", _stop_ttk_sp, add="+")
            sp.bind("<Leave>", _stop_ttk_sp, add="+")
            sp.bind("<FocusOut>", _stop_ttk_sp, add="+")

        def _set_current():
            n = datetime.datetime.now()
            self.v_year.set(n.year)
            self.v_month.set(n.month)
            self.v_day.set(n.day)
            self.v_hour.set(n.hour)
            self.v_min.set(n.minute)
            self.v_sec.set(n.second)

        btn_now = tk.Button(
            f_dt, text="現在端末の時間にセット", font=(FONT_FAMILY, 11, "bold"),
            bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, relief="flat", padx=16, pady=6,
            cursor="hand2", command=_set_current
        )
        btn_now.pack(pady=(14, 4))
        _activate_toplevel(self, parent)


# ---------------------------------------------------------------------------
# GPIO テストダイアログ
# ---------------------------------------------------------------------------
class GPIOTestDialog(tk.Toplevel):
    def __init__(self, parent, gpio_settings):
        super().__init__(parent)
        self.title("GPIO 出力テスト")
        self.geometry("500x350")
        self.configure(bg=COLOR_BG_MAIN)
        self.transient(parent)
        self.grab_set()

        self.gpio_settings = gpio_settings
        self.running = True
        self.outputs = {}

        tk.Label(self, text="GPIO 出力テスト", font=FONT_LARGE,
                 bg=COLOR_BG_MAIN, fg=COLOR_ACCENT).pack(pady=20)

        # ハードウェア初期化
        self.setup_test_hardware()

        f_out = tk.LabelFrame(self, text="警報出力テスト",
                              font=FONT_SET_LBL, bg=COLOR_BG_PANEL,
                              fg=COLOR_TEXT_MAIN, padx=20, pady=20)
        f_out.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        self.output_state_ng = False

        def toggle_out(btn):
            self.output_state_ng = not self.output_state_ng
            state = self.output_state_ng

            if "ng" in self.outputs:
                if state:
                    self.outputs["ng"].on()
                    btn.config(text="警報出力 (ON)",
                               bg=COLOR_WARNING, fg="black")
                else:
                    self.outputs["ng"].off()
                    btn.config(text="警報出力 (OFF)",
                               bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN)

        btn_ng = tk.Button(f_out, text="警報出力 (OFF)", font=FONT_BTN_LARGE,
                           bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN,
                           relief="flat", width=25, height=2)
        btn_ng.pack(pady=10)
        btn_ng.config(command=lambda: toggle_out(btn_ng))

        tk.Label(f_out, text=f"(※クリックでON/OFFが切り替わります。ピン: {self.gpio_settings['outputs']['ng']})",
                 font=FONT_NORMAL, bg=COLOR_BG_PANEL,
                 fg=COLOR_TEXT_SUB).pack(pady=5)

        tk.Button(self, text="閉じる", font=FONT_BOLD, bg="#546E7A",
                  fg="white", relief="flat", height=2,
                  command=self.close_test).pack(fill=tk.X, padx=20, pady=15)

        self.protocol("WM_DELETE_WINDOW", self.close_test)

    def setup_test_hardware(self):
        try:
            self.outputs["ng"] = OutputDevice(self.gpio_settings["outputs"]["ng"])
        except Exception as e:
            print(f"GPIO Init Error in Test: {e}")

    def close_test(self):
        self.running = False
        for d in self.outputs.values():
            d.close()
        if hasattr(self.master, "app_instance"):
            self.master.app_instance.setup_hardware() # type: ignore
        self.destroy()

# ---------------------------------------------------------------------------
# 設定ダイアログ
# ---------------------------------------------------------------------------
class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, settings, on_close_callback):
        super().__init__(parent)
        self._parent_window = parent
        self.settings = settings
        self.on_close_callback = on_close_callback
        self.title("詳細設定")
        self.geometry("1400x900")
        self.configure(bg=COLOR_BG_MAIN)
        try:
            self.transient(parent)
        except tk.TclError:
            pass
        self.temp_data = json.loads(json.dumps(self.settings.data))
        self.has_changes = False
        self.model_classes = self._get_model_classes()
        self.available_cams = detect_available_cameras()
        self._scan_status_var = tk.StringVar(value="")
        
        # 設定表示中はメイン画面のプレビューを一時停止して負荷を軽減 (Raspi 5向け)
        if hasattr(self.master, "app_instance"):
            self.master.app_instance.preview_paused = True

        # UI要素のプレースホルダ
        self.active_entry = (None, None) 
        self.pin_widgets = {}
        self.input_pins = {}
        self.map_labels = {}
        self.trig_scroll = tk.Frame() # type: ignore
        self.trig_list_f = tk.Frame()     # type: ignore
        self.sel_list_f = tk.Frame()      # type: ignore
        self.lbl_gpio_status = tk.Label() # type: ignore
        self.cam_body = tk.Frame()  # type: ignore
        self.pat_body = tk.Frame()  # type: ignore
        self.lb_pat = tk.Listbox()  # type: ignore
        
        self.v_ng = tk.StringVar(value=str(self.temp_data["gpio"]["outputs"].get("ng", 20)))
        self._gpio_test_output = None
        self._gpio_test_on = False

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TNotebook", background=COLOR_BG_MAIN, borderwidth=0)
        style.configure("TNotebook.Tab", background=COLOR_BG_PANEL,
                        foreground=COLOR_TEXT_MAIN, font=FONT_SET_TAB,
                        padding=[20, 10], focuscolor=COLOR_BG_MAIN)
        style.map("TNotebook.Tab",
                  background=[("selected", COLOR_ACCENT)],
                  foreground=[("selected", "black")])

        btn_f = tk.Frame(self, pady=20, bg=COLOR_BG_MAIN)
        btn_f.pack(side=tk.BOTTOM, fill=tk.X, padx=20)
        
        self.btn_save = tk.Button(btn_f, text="保存して閉じる", font=FONT_BOLD, bg=COLOR_BG_INPUT,
                                  fg="white", relief="flat", width=22,
                                  command=self.save_and_close)
        self.btn_save.pack(side=tk.RIGHT, padx=5)
        
        tk.Button(btn_f, text="キャンセル", font=FONT_BOLD, bg="#546E7A",
                  fg="white", relief="flat", width=10,
                  command=self.on_cancel).pack(side=tk.RIGHT, padx=5)

        nb = ttk.Notebook(self)
        nb.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=20, pady=20)
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.t_cam = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_cam, text=" カメラ ")
        self.t_roi = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_roi, text=" 検知エリア ")
        self.t_gpio = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_gpio, text=" GPIOピン ")
        self.t_res = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_res, text=" 画素数 ")
        self.t_sys = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_sys, text=" システム ")

        # ROI設定用の状態変数
        self._roi_points = []
        self._roi_picking = False
        self._roi_preview_running = False

        # 既存の roi_polygon があればロード、なければ roi (矩形) から生成してロード
        pts = self.temp_data.get("inference", {}).get("roi_polygon")
        if pts and len(pts) >= 3:
            self._roi_points = [list(pt) for pt in pts]
        else:
            roi = self.temp_data.get("inference", {}).get("roi")
            res_str = self.temp_data.get("storage", {}).get("capture_res", "1920x1080")
            try:
                w, h = map(int, res_str.split("x"))
            except Exception:
                w, h = 1920, 1080
            if roi and all(k in roi for k in ("xmin", "ymin", "xmax", "ymax")):
                self._roi_points = [
                    [roi["xmin"] / w, roi["ymin"] / h],
                    [roi["xmax"] / w, roi["ymin"] / h],
                    [roi["xmax"] / w, roi["ymax"] / h],
                    [roi["xmin"] / w, roi["ymax"] / h]
                ]

        self.setup_cam()
        self.setup_roi()
        self.setup_gpio()
        self.setup_res()
        self.setup_sys()


        btn_help = tk.Button(btn_f, text="ヘルプ", font=FONT_SET_LBL,
                             bg=COLOR_BG_INPUT, fg=COLOR_ACCENT,
                             relief="flat", command=self.show_settings_help)
        btn_help.pack(side=tk.LEFT, padx=20)

        self.protocol("WM_DELETE_WINDOW", self.on_cancel)
        
        # Combobox のドロップダウンリストのフォントを大きく設定
        self.option_add("*TCombobox*Listbox.font", FONT_SET_VAL)

        # Linux/Raspberry Pi: 描画完了後にフォーカスを設定（grab_set は Linux では使用しない）
        _activate_toplevel(self, parent)

    def on_cancel(self):
        """キャンセル時やウィンドウを閉じた際もプレビュー再開を保証する"""
        _release_toplevel_modal(self)
        self._release_gpio_test_output()
        if hasattr(self, "_live_preview_win") and self._live_preview_win.winfo_exists():
            self._live_preview_win.destroy()
        if self.on_close_callback:
            self.on_close_callback()
        
        # プレビュー再開
        if hasattr(self.master, "app_instance"):
            self.master.app_instance.preview_paused = False

        self.destroy()

    def show_settings_help(self):
        help_data = {
            "1. カメラ設定": (
                "【概要】使用するUSBカメラの接続設定です。\n"
                "・インデックス: カメラの識別番号です。\n"
                "・テストボタン: 現在のインデックスで正常に映るか、ライブ映像で確認できます。"
            ),
            "2. GPIOピン設定": (
                "【概要】Raspberry PiのGPIOピンへの配線設定です。\n"
                "・マップをクリックするだけで警報出力ピン（BCM番号）を設定できます。\n"
                "・選択中のピンは赤くハイライト表示されます。\n"
                "・テスト出力: ボタンを押すたびに警報出力をON/OFF切り替えて動作確認できます。"
            ),
            "3. 保存・画素数設定": (
                "【概要】画像の質や保存サイズを決めます。\n"
                "・撮影解像度: カメラから読み出す際の元サイズです。大きいほどAIの精度が上がる可能性があります。\n"
                "・プレビュー解像度: 画面に表示する監視映像のサイズです。\n"
                "・検出時保存画像: 接近検知時に保存するサイズです。\n"
                "・撮影保存画像: 撮影モードで保存するサイズです。"
            ),
            "4. システム最適化": (
                "【概要】AIの挙動やタイマーの設定です。\n"
                "・AIモデルのパス: 推論に使用するモデルのパスを指定します。\n"
                "・監視対象クラス: 接近検知する対象を選択します。「すべてのクラス」を選ぶと全検出物が対象になります。\n"
                "・連続検知フレーム数: 検知エリア内でこの回数連続検知したら警報を出します。\n"
                "・警報出力時間: 接近検知時に信号を何秒間出し続けるかです（最小0.1秒）。\n"
                "・撮影モード設定: 手動撮影時の連続撮影回数と撮影間隔を設定します。"
            ),
            "5. 容量監視": (
                "【概要】ディスク容量不足によるシステム停止を防ぐための自動削除設定です。\n"
                "・自動削除有効: 容量上限を超えた際、古い画像から順に自動削除します。\n"
                "・最大容量上限: 指定したGB数を超えると削除を開始します。"
            ),
        }
        HelpWindow(self, "詳細設定 操作ガイド", help_data)

    def _get_model_classes(self):
        classes = [""]
        if not YOLO_AVAILABLE:
            return classes
        try:
            path = self.temp_data["inference"].get("model_path")
            if path and os.path.exists(path):
                # .ptモデルをロードしてクラス名を取得 (設定画面を開くたびに最新のモデル状態を確認するため)
                model = YOLO(path)
                names = getattr(model, 'names', {})
                if names:
                    classes += sorted(list(names.values()))
        except Exception:
            pass
        return classes

    def _entry(self, parent, var, width=None, key_path=None):
        ent = tk.Entry(parent, textvariable=var, font=FONT_SET_VAL,
                        width=width, bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN,
                        insertbackground="white", relief="flat")
        if key_path:
            def _trace(*args):
                self._mark_changed()
            var.trace_add("write", _trace)
        return ent

    def _spinbox(self, parent, var, from_, to, increment=1, width=6, key_path=None):
        sb = tk.Spinbox(parent, from_=from_, to=to, increment=increment, textvariable=var,
                        font=FONT_SET_VAL, width=width, bg=COLOR_BG_INPUT, fg="white", 
                        buttonbackground="#78909C", bd=1, relief="solid",
                        repeatdelay=0, repeatinterval=0)
        
        # ラズパイ環境での長押しタイマー暴走を防止する安全ハンドラ
        def _stop_repeat(event=None):
            try:
                rep_id = sb.tk.call('set', '::tk::spinbox::Repeater')
                if rep_id:
                    sb.tk.call('after', 'cancel', rep_id)
            except Exception:
                pass
        sb.bind("<ButtonRelease-1>", _stop_repeat, add="+")
        sb.bind("<Leave>", _stop_repeat, add="+")
        sb.bind("<FocusOut>", _stop_repeat, add="+")

        if key_path:
            def _trace(*args):
                self._mark_changed()
            var.trace_add("write", _trace)
        return sb

    def create_scrollable_panel(self, parent):
        canvas = tk.Canvas(parent, bg=COLOR_BG_MAIN, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=COLOR_BG_MAIN)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            # マスコミなど他のウィジェット上でも、このキャンバスが属するタブが
            # 現在アクティブならスクロール実行
            if not self.winfo_exists(): return
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        # キャンバスに入った時だけMouseWheelをこのキャンバスに束縛する
        def _bind_mouse(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
        def _unbind_mouse(event):
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _bind_mouse)
        canvas.bind("<Leave>", _unbind_mouse)

        return scrollable_frame

    # ---- カメラタブ ----
    def setup_cam(self):
        outer, inner = create_card(self.t_cam, "カメラ設定")
        outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # 上部スキャン操作エリア
        f_top = tk.Frame(inner, bg=COLOR_BG_PANEL)
        f_top.pack(fill=tk.X, pady=(0, 10))

        btn_scan = tk.Button(
            f_top, text="カメラの再検出", font=FONT_NORMAL,
            bg=COLOR_ACCENT, fg="white", relief="flat", padx=10, pady=4,
            cursor="hand2", command=self.scan_cameras
        )
        btn_scan.pack(side=tk.LEFT)
        Tooltip(btn_scan, "OSに接続されているカメラ機器を再探索して選択肢を更新します")

        lbl_scan_st = tk.Label(
            f_top, textvariable=self._scan_status_var, font=FONT_SET_VAL,
            bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB
        )
        lbl_scan_st.pack(side=tk.LEFT, padx=15)

        self.cam_body = tk.Frame(inner, bg=COLOR_BG_PANEL)
        self.cam_body.pack(fill=tk.BOTH, expand=True)
        self.refresh_cam()

    def refresh_cam(self):
        for w in self.cam_body.winfo_children():
            w.destroy()

        if not hasattr(self, "available_cams") or not self.available_cams:
            self.available_cams = detect_available_cameras()

        if not self.temp_data["cameras"]:
            self.temp_data["cameras"] = [{"id": "cam_1", "name": "カメラ 1", "index": 0}]

        cam_obj = self.temp_data["cameras"][0]
        f = tk.LabelFrame(self.cam_body, text="カメラ 1",
                          font=FONT_SET_LBL, bg=COLOR_BG_PANEL,
                          fg=COLOR_TEXT_SUB, padx=15, pady=15,
                          relief="solid", bd=1)
        f.pack(fill=tk.X, pady=5)

        l_idx = tk.Label(f, text="使用カメラ:", font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN)
        l_idx.grid(row=0, column=0, sticky="w")
        Tooltip(l_idx, "システムに接続されているカメラを選択します。")

        curr_idx = cam_obj.get("index", 0)
        curr_device_name = cam_obj.get("device_name", "")

        # 選択肢の生成
        cam_options = [c[1] for c in self.available_cams]

        # 初期値の判定
        init_val = f"[{curr_idx}] カメラ (インデックス {curr_idx})"
        if curr_device_name and curr_device_name in cam_options:
            init_val = curr_device_name
        else:
            for c_idx, c_label in self.available_cams:
                if c_idx == curr_idx:
                    init_val = c_label
                    break

        vi = tk.StringVar(value=init_val)
        cb_dev = ttk.Combobox(f, textvariable=vi, values=cam_options, font=FONT_SET_VAL, width=32)
        cb_dev.grid(row=0, column=1, padx=15)

        def _upd_inner():
            sel_text = vi.get().strip()
            parsed_idx = curr_idx
            if sel_text.startswith("[") and "]" in sel_text:
                try:
                    parsed_idx = int(sel_text[1:sel_text.index("]")])
                except ValueError:
                    pass
            else:
                try:
                    parsed_idx = int(sel_text)
                except ValueError:
                    pass

            self.temp_data["cameras"][0].update({
                "index": parsed_idx,
                "device_name": sel_text
            })
            self._mark_changed()

        vi.trace_add("write", lambda *a: _upd_inner())

        tk.Button(f, text="テスト", font=FONT_BTN_LARGE, bg=COLOR_ACCENT,
                  fg="black", relief="flat", padx=15,
                  command=lambda: self.test_camera(0)).grid(row=0, column=2, padx=10)

    def test_camera(self, idx):
        c_idx_str = self.temp_data["cameras"][idx].get("index", 0)
        try:
            c_idx = int(c_idx_str)
        except ValueError:
            messagebox.showerror("エラー", "正しいカメラインデックスを入力してください。")
            return
        test_win = tk.Toplevel(self)
        test_win.title(f"カメラテスト (インデックス: {c_idx})")
        test_win.geometry("640x480")
        test_win.transient(self)
        test_win.grab_set()
        lbl = tk.Label(test_win, bg="black")
        lbl.pack(fill=tk.BOTH, expand=True)
        cap = cv2.VideoCapture(c_idx)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            messagebox.showerror("エラー", f"カメラ (インデックス: {c_idx}) を開けませんでした。")
            test_win.destroy()
            return

        def update_frame():
            if not test_win.winfo_exists():
                cap.release()
                return
            ret, frame = cap.read()
            if ret:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(frame)
                img = img.resize((640, 480))
                photo = ImageTk.PhotoImage(image=img)
                lbl.config(image=photo)
                lbl.image = photo
            else:
                lbl.config(text="フレームを取得できません", fg="white")
            test_win.after(30, update_frame)

        update_frame()

    def add_cam(self):
        if len(self.temp_data["cameras"]) < 4:
            next_num = len(self.temp_data["cameras"]) + 1
            self.temp_data["cameras"].append({
                "id": f"cam_{int(time.time())}",
                "name": f"カメラ {next_num}",
                "index": 0
            })
            self.refresh_cam()
            self._mark_changed()

    def del_cam(self, idx):
        self.temp_data["cameras"].pop(idx)
        self.refresh_cam()
        self._mark_changed()

    def scan_cameras(self):
        """バックグラウンドでカメラを再探索し、リストを更新する"""
        import threading
        self._scan_status_var.set("スキャン中...")

        def _do_scan():
            cams = detect_available_cameras()
            self.after(0, lambda: _on_found(cams))

        def _on_found(cams):
            if not self.winfo_exists():
                return
            self.available_cams = cams
            found_str = ", ".join([f"[{c[0]}]" for c in cams])
            self._scan_status_var.set(f"検出: {found_str if cams else 'なし'}")
            self.refresh_cam()

        threading.Thread(target=_do_scan, daemon=True).start()

    # ---- 変更検知 ----
    def _mark_changed(self, *args):
        """設定に変更があった場合のみ保存ボタンの色を緑に変え、テキストを更新する"""
        if not self.has_changes:
            self.has_changes = True
            if hasattr(self, "btn_save") and self.btn_save.winfo_exists():
                self.btn_save.config(bg=COLOR_OK, fg="black", text="変更を適用して保存")

    # ---- ライブしきい値プレビュー ----
    def _update_threshold_preview(self, threshold: float, recursive=True):
        """
        現在フォーカスされているカメラの最新フレームに、
        指定しきい値でのYOLO検出結果をオーバーレイして表示する（ライブプレビュー）。
        YOLO が使えない場合は何もしない。
        """
        if not self.winfo_exists():
            return
        if not YOLO_AVAILABLE:
            return
        app = getattr(self.master, "app_instance", None)
        if app is None:
            return
        model = getattr(app, "model", None)
        if model is None:
            return
        # NOTE: 設定画面ではカメラを一時解放(caps={})している場合があるが、
        # app.last_frames に最新フレームが残っていればプレビューは可能。
        
        # app.last_frames から最新のキャプチャ済みフレームを取得（同時アクセスを避ける）
        last_frames = getattr(app, "last_frames", {})
        if not last_frames:
            return  # まだ1枚もキャプチャされていない場合は中止
        
        cid = next(iter(last_frames.keys()), None)
        frame = last_frames.get(cid)
        if frame is None or not isinstance(frame, np.ndarray):
            # フレームがまだない場合、または不正なデータの場合は中止
            return

        # YOLO 推論 (別スレッドだと UI更新が難しいのでここでは推論を短時間だけ実行)
        def _infer():
            try:
                # コピーしたフレームを渡す（スレッドセーフ対策）
                results = model(frame.copy(), conf=threshold, verbose=False)
                if not results:
                    return
                overlay = results[0].plot()
                overlay_rgb = cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(overlay_rgb)
                img.thumbnail((640, 480))
                photo = ImageTk.PhotoImage(image=img)

                def _show():
                    if not self.winfo_exists():
                        return
                    if hasattr(self, "_live_preview_win") and self._live_preview_win.winfo_exists():
                        self._live_lbl.config(image=photo)
                        self._live_lbl.image = photo
                        # 動画化：100ms後に再帰的に自分を呼ぶ（ウィンドウが残っていれば）
                        self.after(100, lambda: self._update_threshold_preview(threshold, recursive=True))
                    elif not recursive:
                        # ウィンドウがない、かつ初回呼び出し（recursive=False）の場合のみ新規作成
                        win = tk.Toplevel(self)
                        win.title(f"ライブプレビュー (しきい値: {threshold:.2f})")
                        win.geometry("660x510")
                        win.transient(self)
                        self._live_preview_win = win
                        self._live_lbl = tk.Label(win, bg="black")
                        self._live_lbl.pack(fill=tk.BOTH, expand=True)
                        self._live_lbl.config(image=photo)
                        self._live_lbl.image = photo
                        # 継続
                        self.after(100, lambda: self._update_threshold_preview(threshold, recursive=True))
                    # ウィンドウが閉じられた状態で再帰呼び出しが来た場合は、何もしない（停止）
                self.after(0, _show)
            except Exception as e:
                import traceback
                traceback.print_exc()
                if not recursive:
                    self.after(0, lambda err=e: messagebox.showerror("ライブプレビューエラー", f"推論実行中にエラーが発生しました:\n{err}", parent=self))
        threading.Thread(target=_infer, daemon=True).start()


    # ---- GPIOタブ ----
    def setup_gpio(self):
        main_f = tk.Frame(self.t_gpio, bg=COLOR_BG_MAIN)
        main_f.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        def _on_ng_change(*a):
            self._mark_changed()
            self._highlight_ng_pin_on_map()
        self.v_ng.trace_add("write", _on_ng_change)

        # ピンマップ（テスト出力ボタンはマップ右上に配置）
        self.show_gpio_map(main_f)
        # 初期ハイライト
        self.after(100, self._highlight_ng_pin_on_map)

    def _set_active_entry(self, entry, var):
        self.active_entry = (entry, var)

    def _highlight_ng_pin_on_map(self):
        """現在設定されているNGピンをピンマップ上でハイライト表示する"""
        try:
            ng_pin = int(self.v_ng.get())
        except (ValueError, tk.TclError):
            return
        for (bcm, lbl_no, lbl_name) in getattr(self, "_map_pin_labels", []):
            if bcm == ng_pin:
                lbl_no.config(bg=COLOR_NG, fg="white")
                lbl_name.config(bg=COLOR_NG, fg="white")
            else:
                lbl_no.config(bg="#222", fg="white")
                lbl_name.config(
                    bg=("#8D6E63" if "V" in lbl_name.cget("text") else
                        "#212121" if "GND" in lbl_name.cget("text") else "#444"),
                    fg=COLOR_TEXT_MAIN
                )

    def _check_gpio_connection(self):
        # lbl_gpio_status は廃止済み (GPIO設定画面簡素化)
        pass



    def show_gpio_map(self, parent):
        """Raspberry Pi 40ピンヘッダのマップを表示する（クリックでNG出力ピンに直接セット）"""
        outer, inner = create_card(parent, "Pi 40Pin Map")
        outer.pack(fill=tk.BOTH, expand=True)

        # タイトル行の右にテスト出力ボタンと現在のピン番号表示を配置
        hdr = tk.Frame(inner, bg=COLOR_BG_PANEL)
        hdr.pack(fill=tk.X, pady=(0, 6))
        tk.Label(hdr, text="現在の警報出力ピン:", font=FONT_SET_VAL,
                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT)
        self._lbl_ng_display = tk.Label(hdr, textvariable=self.v_ng,
                                        font=FONT_SET_VAL, bg=COLOR_BG_PANEL,
                                        fg=COLOR_ACCENT, width=4, anchor="w")
        self._lbl_ng_display.pack(side=tk.LEFT, padx=(4, 20))
        self._btn_gpio_test = tk.Button(hdr, text="テスト出力 (OFF)", font=FONT_BTN_LARGE,
                                        bg="#546E7A", fg="white", relief="flat",
                                        command=self.toggle_gpio_test)
        self._btn_gpio_test.pack(side=tk.RIGHT)
        Tooltip(self._btn_gpio_test, "押すたびに警報出力ピンをON/OFF切り替えます。")

        def _on_pin_clicked(bcm_val):
            # マップクリックで直接 v_ng にセット
            self.v_ng.set(str(bcm_val))
            self._mark_changed()

        
        # ピンデータ (BCM番号)
        # (PinNo, Name, BCM)
        pins = [
            (1, "3.3V", None),   (2, "5V", None),
            (3, "GPIO 2", 2),    (4, "5V", None),
            (5, "GPIO 3", 3),    (6, "GND", None),
            (7, "GPIO 4", 4),    (8, "GPIO 14", 14),
            (9, "GND", None),    (10, "GPIO 15", 15),
            (11, "GPIO 17", 17), (12, "GPIO 18", 18),
            (13, "GPIO 27", 27), (14, "GND", None),
            (15, "GPIO 22", 22), (16, "GPIO 23", 23),
            (17, "3.3V", None),  (18, "GPIO 24", 24),
            (19, "GPIO 10", 10), (20, "GND", None),
            (21, "GPIO 9", 9),   (22, "GPIO 25", 25),
            (23, "GPIO 11", 11), (24, "GPIO 8", 8),
            (25, "GND", None),   (26, "GPIO 7", 7),
            (27, "ID_SD", None), (28, "ID_SC", None),
            (29, "GPIO 5", 5),   (30, "GND", None),
            (31, "GPIO 6", 6),   (32, "GPIO 12", 12),
            (33, "GPIO 13", 13), (34, "GND", None),
            (35, "GPIO 19", 19), (36, "GPIO 16", 16),
            (37, "GPIO 26", 26), (38, "GPIO 20", 20),
            (39, "GND", None),   (40, "GPIO 21", 21)
        ]

        # --- ピンマップ (スクロール可能) ---
        map_scroll = tk.Frame(inner, bg=COLOR_BG_PANEL)
        map_scroll.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        map_canvas = tk.Canvas(map_scroll, bg=COLOR_BG_PANEL, highlightthickness=0)
        map_sb = ttk.Scrollbar(map_scroll, orient="vertical", command=map_canvas.yview)
        mf = tk.Frame(map_canvas, bg=COLOR_BG_PANEL)
        mf_window = map_canvas.create_window((0, 0), window=mf, anchor="nw")

        def _on_map_configure(_event=None):
            map_canvas.configure(scrollregion=map_canvas.bbox("all"))
            map_canvas.itemconfig(mf_window, width=map_canvas.winfo_width())

        mf.bind("<Configure>", _on_map_configure)
        map_canvas.bind("<Configure>", _on_map_configure)
        map_canvas.configure(yscrollcommand=map_sb.set)
        map_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        map_sb.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_map_wheel(event):
            map_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        map_canvas.bind("<Enter>", lambda e: map_canvas.bind_all("<MouseWheel>", _on_map_wheel))
        map_canvas.bind("<Leave>", lambda e: map_canvas.unbind_all("<MouseWheel>"))

        for i, (pno, name, bcm) in enumerate(pins):
            col_idx = 0 if i % 2 == 0 else 2
            row_idx = i // 2
            
            # ピン番号ラベル (外側) - フォントを大きく(8->10)
            lbl_no = tk.Label(mf, text=str(pno), font=(FONT_FAMILY, 10, "bold"),
                              width=3, bg="#222", fg="white")
            
            # ピン名称ラベル - フォントを大きく(8->10)、幅・余白を拡大
            lbl_color = "#444"
            if "V" in name: lbl_color = "#8D6E63"   # 電源ピン
            if "GND" in name: lbl_color = "#212121"  # グランドピン
            
            lbl_name = tk.Label(mf, text=name, font=(FONT_FAMILY, 10),
                                width=12, bg=lbl_color, fg=COLOR_TEXT_MAIN,
                                padx=5, pady=3, relief="flat")

            if i % 2 == 0:  # 左列
                lbl_no.grid(row=row_idx, column=0, padx=2, pady=1)
                lbl_name.grid(row=row_idx, column=1, padx=(2, 10), pady=1, sticky="w")
            else:  # 右列
                lbl_name.grid(row=row_idx, column=2, padx=(10, 2), pady=1, sticky="e")
                lbl_no.grid(row=row_idx, column=3, padx=2, pady=1)

            if bcm is not None:
                def make_handler(b=bcm): return lambda e: _on_pin_clicked(b)
                lbl_no.bind("<Button-1>", make_handler())
                lbl_name.bind("<Button-1>", make_handler())
                lbl_no.config(cursor="hand2")
                lbl_name.config(cursor="hand2")
                Tooltip(lbl_name, "クリックで警報出力ピンにこのBCM番号をセットします")
            # ハイライト用にラベル参照を保存
            if not hasattr(self, "_map_pin_labels"):
                self._map_pin_labels = []
            if bcm is not None:
                self._map_pin_labels.append((bcm, lbl_no, lbl_name))





    # ---- パターンタブ ----
    # setup_patは不要なので削除

    def _create_pat_scrollable_panel(self, parent):
        """パターン設定専用のスクロールパネル生成（キャンバスへのアクセスを容易にする）"""
        canvas = tk.Canvas(parent, bg=COLOR_BG_MAIN, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=COLOR_BG_MAIN)
        
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            if not self.winfo_exists(): return
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        return canvas, scrollable_frame

    def _auto_select_first_pat(self):
        if not self.winfo_exists(): return
        if self.lb_pat.size() > 0:
            self.lb_pat.selection_set(0)
            self.on_pat_sel(None)

    def refresh_pat_list(self):
        self.lb_pat.delete(0, tk.END)
        for pid in self.temp_data["pattern_order"]:
            self.lb_pat.insert(tk.END, self.temp_data["patterns"][pid]["name"])

    def add_pat(self):
        pid = f"p_{int(time.time())}"
        next_num = len(self.temp_data['pattern_order']) + 1
        name = f"パターン {next_num}"
        self.temp_data["patterns"][pid] = {
            "name": name,
            "pin_condition": [0] * len(self.temp_data["gpio"].get("pattern_pins", [])),
            "stages": {}
        }
        self.temp_data["pattern_order"].append(pid)
        self.refresh_pat_list()
        self._mark_changed()

    def del_pat(self):
        s = self.lb_pat.curselection()
        if s:
            pid = self.temp_data["pattern_order"].pop(s[0])
            del self.temp_data["patterns"][pid]
            self.refresh_pat_list()
            # 右側の詳細画面をクリア
            for w in self.pat_body.winfo_children():
                w.destroy()
            # もし他にパターンがあれば、次の（または前の）項目を自動選択する
            self.after(50, self._auto_select_first_pat)
            self._mark_changed()

    def on_pat_sel(self, e):
        # 現在のスクロール位置を保存
        y_pos = 0.0
        if hasattr(self, "pat_canvas") and self.pat_canvas.winfo_exists():
            y_pos = self.pat_canvas.yview()[0]

        for w in self.pat_body.winfo_children():
            w.destroy()
        s = self.lb_pat.curselection()
        if not s:
            return
        pid = self.temp_data["pattern_order"][s[0]]
        p = self.temp_data["patterns"][pid]

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 1. 基本設定カード (名称・ピン条件)
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        outer1, inner1 = create_card(self.pat_body, "基本設定")
        outer1.pack(fill=tk.X, pady=(0, 15))

        l_name = tk.Label(inner1, text="名称:", font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN)
        l_name.pack(anchor="w")
        Tooltip(l_name, "パターンの表示名です。")
        vn = tk.StringVar(value=p["name"])
        e_name = self._entry(inner1, vn, key_path=f"patterns.{pid}.name")
        e_name.pack(fill=tk.X, pady=(5, 15))
        vn.trace_add("write", lambda *a: p.update({"name": vn.get()}))

        l_pin = tk.Label(inner1, text="パターン信号条件:", font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN)
        l_pin.pack(anchor="w")
        Tooltip(l_pin, "このパターンを有効にするための入力ピンの状態を指定します。")
        
        pins = self.temp_data["gpio"].get("pattern_pins", [])
        if len(p["pin_condition"]) != len(pins):
            p["pin_condition"] = [0] * len(pins)

        p_grid = tk.Frame(inner1, bg=COLOR_BG_PANEL)
        p_grid.pack(anchor="w", pady=5)
        p_vars = []
        for i, pin in enumerate(pins):
            def _create_pin_ui(idx=i, pin_obj=pin):
                v = tk.IntVar(value=p["pin_condition"][idx])
                p_vars.append(v)
                btn = tk.Button(p_grid, font=FONT_SET_VAL, width=4, relief="flat")

                def _toggle(var=v, b=btn, i_idx=idx):
                    var.set(1 if var.get() == 0 else 0)
                    _upd_btn_color(b, var.get(), i_idx)
                    self._mark_changed()

                def _upd_btn_color(b, val, b_idx):
                    if val == 1:
                        b.config(text="ON", bg=COLOR_ACCENT, fg="black")
                    else:
                        b.config(text="OFF", bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN)

                l_p = tk.Label(p_grid, text=f"{pin_obj['name']}:", font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN)
                l_p.grid(row=idx // 3, column=(idx % 3) * 2, sticky="e", padx=(10, 2))
                Tooltip(l_p, f"このパターンを有効にするための {pin_obj['name']} の信号状態(ON/OFF)を指定します。")

                btn.config(command=_toggle)
                _upd_btn_color(btn, v.get(), idx)
                btn.grid(row=idx // 3, column=(idx % 3) * 2 + 1, padx=(0, 10), pady=5)
            
            _create_pin_ui()

        def _upd_p_pins(*a):
            try:
                p["pin_condition"] = [var.get() for var in p_vars]
            except tk.TclError:
                pass
        for v in p_vars:
            v.trace_add("write", _upd_p_pins)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 2. トリガー別 判定条件
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        tk.Label(self.pat_body, text="トリガー別 判定条件", font=FONT_SET_LBL,
                 bg=COLOR_BG_MAIN, fg=COLOR_ACCENT).pack(anchor="w", pady=(10, 5))

        # ツールチップ用共通テキスト
        tip_text = "【判定仕様】\n・同じトリガー内の条件はすべて満たす必要があります (AND条件)。\n・検出クラスを空欄にすると、指定カメラの全検出物の合計数で判定します。"

        for t in self.temp_data["gpio"]["triggers"]:
            tid = t["id"]
            if tid not in p["stages"]:
                p["stages"][tid] = {"conditions": {}}
            st = p["stages"][tid]
            
            # 個別カード (灰色枠線)
            cf_outer = tk.Frame(self.pat_body, bg="#808080", padx=1, pady=1)
            cf_outer.pack(fill=tk.X, pady=8)
            cf_inner = tk.Frame(cf_outer, bg=COLOR_BG_PANEL, padx=15, pady=10)
            cf_inner.pack(fill=tk.BOTH, expand=True)

            head_f = tk.Frame(cf_inner, bg=COLOR_BG_PANEL)
            head_f.pack(fill=tk.X)
            tk.Label(head_f, text=f"■ {t['name']}", font=FONT_BOLD,
                     bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)
            
            # 条件テーブルコンテナ
            cond_container = tk.Frame(cf_inner, bg=COLOR_BG_PANEL)
            cond_container.pack(fill=tk.X, pady=10)

            def _refresh_conditions(container=cond_container, stage=st, trigger_id=tid):
                for w in container.winfo_children(): w.destroy()
                
                # stage["conditions"] は毎回取り直す (クロージャ問題左回避)
                if not isinstance(stage.get("conditions"), dict):
                    stage["conditions"] = {}
                if isinstance(stage["conditions"], list):
                    # 旧形式からの救済
                    c_id = str(self.temp_data["cameras"][0]["id"]) if self.temp_data["cameras"] else "1"
                    stage["conditions"] = {c_id: stage["conditions"]}
                # 以後は常に stage["conditions"] を直接参照する
                
                # テーブルヘッダー
                header_f = tk.Frame(container, bg=COLOR_BG_PANEL)
                header_f.pack(fill=tk.X, pady=(0, 5))
                
                l_cam = tk.Label(header_f, text="対象カメラ", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, width=20, anchor="w")
                l_cam.pack(side=tk.LEFT, padx=5)
                Tooltip(l_cam, "判定に使用するカメラの名称です。")
                
                l_cls = tk.Label(header_f, text="検出クラス", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, width=15, anchor="w")
                l_cls.pack(side=tk.LEFT, padx=5)
                Tooltip(l_cls, "AIが検知する対象の種類を指定します。空欄の場合は全検出物の合計を判定に使用します。")
                
                l_cnt = tk.Label(header_f, text="基準個数", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, width=8, anchor="w")
                l_cnt.pack(side=tk.LEFT, padx=5)
                Tooltip(l_cnt, "判定OKとするための個数です。0を指定すると未検出でOKとなります。")
                
                tk.Label(header_f, text="", width=4, bg=COLOR_BG_PANEL).pack(side=tk.RIGHT)

                # 各カメラの条件をフラットに並べてテーブル化
                for c in self.temp_data["cameras"]:
                    c_id = str(c["id"])
                    c_conds = stage["conditions"].setdefault(c_id, [])

                    for ci, cond in enumerate(c_conds):
                        def _create_row_ui(cam_obj=c, cid=c_id, idx=ci, cond_obj=cond):
                            row_f = tk.Frame(container, bg=COLOR_BG_PANEL)
                            row_f.pack(fill=tk.X, pady=2)
                            
                            tk.Label(row_f, text=cam_obj["name"], font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=20, anchor="w").pack(side=tk.LEFT, padx=5)
                            
                            cv = tk.StringVar(value=cond_obj.get("class", ""))
                            cb = ttk.Combobox(row_f, textvariable=cv, values=self.model_classes, font=FONT_SET_VAL, width=15, state="readonly")
                            cb.pack(side=tk.LEFT, padx=5)
                            
                            nv = tk.StringVar(value=cond_obj.get("count", "1"))
                            kp = f"patterns.{pid}.stages.{tid}.conditions.{cid}.{idx}"
                            self._spinbox(row_f, nv, 0, 999, 1, width=8, key_path=f"{kp}.count").pack(side=tk.LEFT, padx=5)
                            
                            def _upd_cond(c_dict=cond_obj, v1=cv, v2=nv, w_cb=cb, k_p=kp):
                                try:
                                    c_dict["class"] = v1.get()
                                    c_dict["count"] = v2.get()
                                    self._mark_changed()
                                except tk.TclError:
                                    pass
                            
                            cv.trace_add("write", lambda *a, u=_upd_cond: u())
                            nv.trace_add("write", lambda *a, u=_upd_cond: u())
                            
                            def _do_del(cid_target=cid, target_cond=cond_obj, _stage=stage):
                                if cid_target in _stage["conditions"] and target_cond in _stage["conditions"][cid_target]:
                                    _stage["conditions"][cid_target].remove(target_cond)
                                    # パターン全体を再描画することで確実に反映させる
                                    self.on_pat_sel(None)
                                    self._mark_changed()

                            tk.Button(row_f, text="x", font=(FONT_FAMILY, 10, "bold"), bg=COLOR_NG_MUTED, fg="white", relief="flat", width=2,
                                      command=_do_del).pack(side=tk.RIGHT, padx=5)
                        
                        _create_row_ui()

                # 行の追加用ボタンエリア
                add_row_f = tk.Frame(container, bg=COLOR_BG_PANEL)
                add_row_f.pack(fill=tk.X, pady=10)
                
                cam_names = [c["name"] for c in self.temp_data["cameras"]]
                sel_cam_v = tk.StringVar()
                if cam_names: sel_cam_v.set(cam_names[0])
                cb_add = ttk.Combobox(add_row_f, textvariable=sel_cam_v, values=cam_names, state="readonly", width=18, font=FONT_SET_VAL)
                cb_add.pack(side=tk.LEFT, padx=5)

                def _add_cond_row(_stage=stage):
                    c_name = sel_cam_v.get()
                    target_c = next((c for c in self.temp_data["cameras"] if c["name"] == c_name), None)
                    if target_c:
                        c_id = str(target_c["id"])
                        _stage["conditions"].setdefault(c_id, []).append({"class": "", "count": "1"})
                        # パターン全体を再描画することで確実に反映させる
                        self.on_pat_sel(None)
                        self._mark_changed()

                btn_add = tk.Button(add_row_f, text="+ 条件追加", font=FONT_NORMAL, bg=COLOR_ACCENT, fg="black", relief="flat",
                                    command=_add_cond_row)
                btn_add.pack(side=tk.LEFT, padx=5)
                Tooltip(btn_add, tip_text)

            _refresh_conditions()

        # スクロール領域の更新
        self.after(50, lambda: self.pat_canvas.configure(scrollregion=self.pat_canvas.bbox("all")) if hasattr(self, "pat_canvas") and self.pat_canvas.winfo_exists() else None)
        # スクロール位置を復元
        self.after(60, lambda: self.pat_canvas.yview_moveto(y_pos) if hasattr(self, "pat_canvas") and self.pat_canvas.winfo_exists() else None)

    # ---- 解像度タブ ----
    def setup_res(self):
        # 解像度の通称マップ
        RES_MAP = {
            "320x240": "320x240 (QVGA)",
            "640x480": "640x480 (VGA)",
            "1280x720": "1280x720 (HD)",
            "1920x1080": "1920x1080 (Full HD)",
            "3840x2160": "3840x2160 (4K)"
        }

        def _to_friendly(s): return RES_MAP.get(s, s)
        def _to_raw(s): return s.split(" ")[0] if "x" in s else s

        # 4項目のみなので、1つのカードグループ内にまとめる
        outer, main_f = create_card(self.t_res, "画素数設定")
        outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        def _row(parent, label, key, options, tip):
            row_f = tk.Frame(parent, bg=COLOR_BG_PANEL)
            row_f.pack(fill=tk.X, pady=6, padx=10)
            
            lbl = tk.Label(row_f, text=label, font=FONT_SET_VAL, bg=COLOR_BG_PANEL,
                           fg=COLOR_TEXT_MAIN, anchor="w", width=30)
            lbl.pack(side=tk.LEFT)
            Tooltip(lbl, tip)
            
            raw_val = self.temp_data["storage"].get(key, options[0])
            v = tk.StringVar(value=_to_friendly(raw_val))
            
            friendly_opts = [_to_friendly(o) for o in options]
            cb = ttk.Combobox(row_f, textvariable=v, values=friendly_opts,
                              font=FONT_SET_VAL, state="readonly", width=25)
            cb.pack(side=tk.RIGHT, padx=5)
            
            def _on_change(*a, k=key, var=v, widget=cb):
                if not self.winfo_exists(): return
                raw = _to_raw(var.get())
                self.temp_data["storage"][k] = raw
                self._mark_changed()
                if k == "capture_res":
                    _update_all_filters()

            v.trace_add("write", _on_change)
            return cb, v, options

        def _parse_res(res_str):
            """'WxH' -> 画素数(int)。失敗時は0を返す。"""
            try:
                w, h = map(int, res_str.split("x"))
                return w * h
            except Exception:
                return 0

        # 撮影解像度
        cb_cap, v_cap, opt_cap = _row(main_f, "撮影解像度", "capture_res", RES_OPTIONS, "カメラから取得する画像の元サイズです。")

        # プレビュー解像度
        cb_prev, v_prev, opt_prev = _row(main_f, "プレビュー解像度", "preview_res", RES_OPTIONS_PREVIEW, "メイン画面のモニタ用サイズ。")

        # 接近検知時保存画像
        cb_detect, v_detect, opt_detect = _row(main_f, "検出時保存画像", "res_detect", RES_OPTIONS_SAVE, "接近検知時に保存するサイズ。「保存しない」で保存しません。")

        # 撮影モード時の保存画像
        cb_record, v_record, opt_record = _row(main_f, "撮影保存画像", "res_record", RES_OPTIONS_SAVE, "手動撮影モードで保存するサイズ。「保存しない」で撮影スキップ。")

        def _update_all_filters():
            """撮影解像度に応じて、保存解像度の候補を撮影解像度以下に制限する。"""
            if not self.winfo_exists():
                return

            storage = self.temp_data.get("storage", {})
            cap_raw = storage.get("capture_res", opt_cap[0])
            cap_px = _parse_res(cap_raw)

            def _filter_save(cb, var, all_opts, key):
                # 「保存しない」は常に残し、それ以外は撮影解像度以下のみ許可
                allowed = []
                for o in all_opts:
                    if o == "保存しない":
                        allowed.append(o)
                    elif _parse_res(o) <= cap_px:
                        allowed.append(o)

                # コンボボックス候補の更新
                cb["values"] = [_to_friendly(o) for o in allowed]

                # 現在値を取得し、もし許可リストから外れていれば撮影解像度か最も近い解像度に補正
                current_raw = storage.get(key, allowed[0] if allowed else "")
                if current_raw not in allowed:
                    # 撮影解像度が許可リストにあればそれを優先
                    new_raw = cap_raw if cap_raw in allowed else (allowed[-1] if allowed else current_raw)
                    storage[key] = new_raw
                    var.set(_to_friendly(new_raw))
                else:
                    # 表示だけ同期
                    var.set(_to_friendly(current_raw))

            _filter_save(cb_detect, v_detect, opt_detect, "res_detect")
            _filter_save(cb_record, v_record, opt_record, "res_record")

        # 初期表示時にも一度フィルタを適用
        _update_all_filters()

        # capture_res 変更時にフィルタをかけ直すよう、トレース内から呼び出される
        def _on_cap_change(*_a):
            _update_all_filters()

        # すでに _row 内で trace_add されているため、ここでは追加トレースだけ行う
        v_cap.trace_add("write", _on_cap_change)



    # ---- システムタブ ----
    def setup_sys(self):
        import os
        from tkinter import filedialog

        # スクロール可能なコンテナ
        scroll_f = self.create_scrollable_panel(self.t_sys)

        s = self.temp_data["inference"]

        def _make_group(parent, title, pady=(10, 4)):
            outer, inner = create_card(parent, title)
            outer.pack(fill=tk.X, padx=20, pady=pady)
            return inner

        def _row_frame(parent, column_widths=(280, 1)):
            f = tk.Frame(parent, bg=COLOR_BG_PANEL)
            f.pack(fill=tk.X, pady=4)
            f.columnconfigure(0, minsize=column_widths[0])
            return f

        def _lbl(parent, text, tip=""):
            l = tk.Label(parent, text=text, font=FONT_SET_VAL,
                         bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, anchor="w", width=22)
            l.pack(side=tk.LEFT, padx=(0, 8))
            if tip:
                Tooltip(l, tip)
            return l

        def _unit(parent, text):
            lbl = tk.Label(parent, text=text, font=FONT_SET_VAL,
                           bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB)
            lbl.pack(side=tk.LEFT, padx=(2, 0))
            return lbl

        def _entry_w(parent, var, width=10):
            e = self._entry(parent, var, width=width)
            e.pack(side=tk.LEFT)
            return e

        def _browse_btn(parent, var, mode="file", filetypes=None):
            def _pick():
                if mode == "dir":
                    p = filedialog.askdirectory(title="フォルダを選択", parent=self)
                else:
                    p = filedialog.askopenfilename(
                        title="ファイルを選択",
                        parent=self,
                        filetypes=filetypes or [("すべてのファイル", "*.*")])
                if p:
                    var.set(p)
            btn = tk.Button(parent, text="参照", font=FONT_NORMAL,
                            bg=COLOR_BG_INPUT, fg=COLOR_ACCENT,
                            relief="flat", padx=6, pady=2, cursor="hand2",
                            command=_pick)
            btn.pack(side=tk.LEFT, padx=(6, 0))
            Tooltip(btn, "クリックしてファイル/フォルダを選択します")
            return btn

        def _play_btn(parent, var):
            def _play():
                try:
                    import pygame
                    if not pygame.mixer.get_init():
                        pygame.mixer.init()
                    p = var.get().strip()
                    if p and os.path.exists(p):
                        pygame.mixer.music.load(p)
                        pygame.mixer.music.play(0)
                    else:
                        messagebox.showwarning("テスト再生",
                                               "ファイルが見つかりません:\n" + p,
                                               parent=self)
                except Exception as ex:
                    messagebox.showwarning("テスト再生エラー", str(ex), parent=self)
            btn = tk.Button(parent, text="テスト再生", font=FONT_NORMAL,
                            bg="#37474f", fg=COLOR_TEXT_MAIN,
                            relief="flat", padx=6, pady=2, cursor="hand2",
                            command=_play)
            btn.pack(side=tk.LEFT, padx=(4, 0))
            Tooltip(btn, "設定した音声を1回再生して確認します")
            return btn

        # グループ1: AI判定設定
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        g1 = _make_group(scroll_f, "接近検知設定", pady=(16, 4))

        # しきい値スライダー
        r_thr = _row_frame(g1)
        _lbl(r_thr, "判定しきい値:", "AIの自信度がこの値(0.0〜1.0)以上なら「検出した」とみなします。")
        v_thr = tk.DoubleVar(value=float(s.get("threshold", 0.5)))
        lbl_thr_val = tk.Label(r_thr, text=f"{v_thr.get():.2f}", font=FONT_SET_VAL,
                               bg=COLOR_BG_PANEL, fg=COLOR_ACCENT, width=5)
        lbl_thr_val.pack(side=tk.LEFT, padx=(0, 6))
        sl = ttk.Scale(r_thr, from_=0.0, to=1.0, length=200,
                       variable=v_thr, orient="horizontal")
        sl.pack(side=tk.LEFT)
        # ライブプレビューボタン（スライダー値で即座にYOLO結果を確認）
        btn_live = tk.Button(r_thr, text="ライブ", font=FONT_NORMAL,
                              bg="#546E7A", fg="white", relief="flat", cursor="hand2",
                              command=lambda: self._update_threshold_preview(round(v_thr.get(), 2), recursive=False))
        btn_live.pack(side=tk.LEFT, padx=(8, 0))
        Tooltip(btn_live, "現在のしきい値で検出した結果をプレビューウィンドウで確認します")

        def _upd_thr(*a):
            val = round(v_thr.get(), 2)
            lbl_thr_val.config(text=f"{val:.2f}")
            s["threshold"] = val
            self._mark_changed()
        v_thr.trace_add("write", _upd_thr)

        # --- 監視対象クラス (チェックボックス形式での複数選択) ---
        r_alert_cls_hdr = _row_frame(g1)
        _lbl(r_alert_cls_hdr, "監視対象クラス:", "接近を検知する対象クラスを選択します。")
        
        # クラス名のリストを取得 (空文字を除去)
        model_classes = [c for c in self._get_model_classes() if c]
        if not model_classes:
            # フォールバック: モデルが読み込めていない等の場合は一般的な主要クラスを表示
            model_classes = ["person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat"]
            
        self._class_vars = {}
        for cls in model_classes:
            self._class_vars[cls] = tk.BooleanVar(value=False)
            
        # 既存の設定値を反映
        curr_cls_str = str(s.get("alert_target_classes", "")).strip()
        if curr_cls_str and curr_cls_str != "すべてのクラス":
            saved_classes = [c.strip() for c in curr_cls_str.split(",") if c.strip()]
            for sc in saved_classes:
                if sc in self._class_vars:
                    self._class_vars[sc].set(True)
                    
        def _upd_alert_classes(*args):
            selected = [cls for cls, var in self._class_vars.items() if var.get()]
            s["alert_target_classes"] = ",".join(selected)
            self._mark_changed()
            
        def _select_all_classes():
            for var in self._class_vars.values():
                var.set(True)
            _upd_alert_classes()

        def _deselect_all_classes():
            for var in self._class_vars.values():
                var.set(False)
            _upd_alert_classes()

        btn_sel_all = tk.Button(r_alert_cls_hdr, text="全選択", font=(FONT_FAMILY, 10, "bold"),
                                bg="#546E7A", fg="white", relief="flat", cursor="hand2", command=_select_all_classes)
        btn_sel_all.pack(side=tk.LEFT, padx=(10, 5))
        
        btn_desel_all = tk.Button(r_alert_cls_hdr, text="全解除", font=(FONT_FAMILY, 10, "bold"),
                                  bg="#546E7A", fg="white", relief="flat", cursor="hand2", command=_deselect_all_classes)
        btn_desel_all.pack(side=tk.LEFT, padx=5)

        # スクロール可能なチェックボックスコンテナ
        r_cls_scroll = _row_frame(g1)
        cls_canvas = tk.Canvas(r_cls_scroll, height=120, bg=COLOR_BG_PANEL, highlightthickness=1, highlightbackground=COLOR_BORDER)
        cls_sb = ttk.Scrollbar(r_cls_scroll, orient="vertical", command=cls_canvas.yview)
        cls_frame = tk.Frame(cls_canvas, bg=COLOR_BG_PANEL)
        
        canvas_window = cls_canvas.create_window((0, 0), window=cls_frame, anchor="nw")
        cls_canvas.configure(yscrollcommand=cls_sb.set)
        
        # マウスホイールイベントのバインド
        def _on_cls_wheel(event):
            cls_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        cls_canvas.bind("<Enter>", lambda e: cls_canvas.bind_all("<MouseWheel>", _on_cls_wheel))
        cls_canvas.bind("<Leave>", lambda e: cls_canvas.unbind_all("<MouseWheel>"))

        # スクロール領域のサイズ同期
        def _on_cls_configure(event=None):
            cls_canvas.configure(scrollregion=cls_canvas.bbox("all"))
            cls_canvas.itemconfig(canvas_window, width=cls_canvas.winfo_width())
            
        cls_frame.bind("<Configure>", _on_cls_configure)
        cls_canvas.bind("<Configure>", _on_cls_configure)

        cls_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cls_sb.pack(side=tk.RIGHT, fill=tk.Y)
        
        # グリッド配置 (4列)
        cols = 4
        for idx, cls in enumerate(model_classes):
            var = self._class_vars[cls]
            cb = tk.Checkbutton(cls_frame, text=cls, variable=var, font=FONT_SET_VAL,
                                bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, selectcolor=COLOR_BG_INPUT,
                                activebackground=COLOR_BG_PANEL, activeforeground=COLOR_TEXT_MAIN,
                                relief="flat", anchor="w", command=_upd_alert_classes)
            cb.grid(row=idx // cols, column=idx % cols, sticky="w", padx=10, pady=2)
            
        for c in range(cols):
            cls_frame.columnconfigure(c, weight=1)

        # 補足説明
        r_desc = _row_frame(g1)
        desc_lbl = tk.Label(r_desc, text="※何もチェックしない、またはすべてチェックした場合は「すべてのクラス」が監視対象になります。",
                            font=(FONT_FAMILY, 11), bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB)
        desc_lbl.pack(side=tk.LEFT)


        # --- 連続検知フレーム数 (確認フレーム数と統合) ---
        r_alert_confirm = _row_frame(g1)
        _lbl(r_alert_confirm, "連続検知フレーム数:", "検知エリア内でこの回数連続して検知したら警報を開始します。大きいほど誤検知が減ります。")
        v_alert_confirm = tk.StringVar(value=str(s.get("alert_confirm_frames", 3)))
        spin_confirm = self._spinbox(r_alert_confirm, v_alert_confirm, 1, 30, 1, width=8)
        spin_confirm.pack(side=tk.LEFT)
        _unit(r_alert_confirm, "フレーム")
        def _upd_alert_confirm(*a):
            try:
                s["alert_confirm_frames"] = int(v_alert_confirm.get())
            except Exception:
                pass
            self._mark_changed()
        v_alert_confirm.trace_add("write", _upd_alert_confirm)

        # --- 推論間隔 ---
        r_alert_interval = _row_frame(g1)
        _lbl(r_alert_interval, "推論間隔:", "何フレームごとにAIを実行するかを指定します。小さいほど反応が速いですが負荷が増えます。")
        v_alert_interval = tk.StringVar(value=str(s.get("alert_infer_every_n_frames", 2)))
        spin_interval = self._spinbox(r_alert_interval, v_alert_interval, 1, 10, 1, width=8)
        spin_interval.pack(side=tk.LEFT)
        _unit(r_alert_interval, "フレームごと")
        def _upd_alert_interval(*a):
            try:
                s["alert_infer_every_n_frames"] = int(v_alert_interval.get())
            except Exception:
                pass
            self._mark_changed()
        v_alert_interval.trace_add("write", _upd_alert_interval)

        # 数値パラメータ
        num_params = [
            ("プレビュー更新レート:", "preview_fps", "fps",
             "メイン画面のカメラ映像を毎秒何回更新するかです。Raspi5では10〜15推奨。", 0.1, 60.0, 0.1),
        ]
        for lbl_txt, key, unit, tip, min_val, max_val, inc in num_params:
            r = _row_frame(g1)
            _lbl(r, lbl_txt, tip)
            v = tk.StringVar(value=str(s.get(key, "")))
            ent = self._spinbox(r, v, min_val, max_val, inc, width=8, key_path=f"inference.{key}")
            ent.pack(side=tk.LEFT)
            _unit(r, unit)
            def _mk_upd(ky=key, var=v, e=ent):
                def _upd(*a):
                    val = var.get()
                    try:
                        s[ky] = float(val) if "." in val else int(val)
                    except Exception:
                        pass
                    self._mark_changed()
                return _upd
            v.trace_add("write", _mk_upd())

        # グループ2: 警報出力制御
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        g2 = _make_group(scroll_f, "警報出力設定")

        r_ng = _row_frame(g2)
        _lbl(r_ng, "警報出力時間:", "接近検知後に警報信号をONにし続ける秒数です。最小0.1秒。")
        v_ng_t = tk.StringVar(value=str(s.get("ng_output_time", 2.0)))
        ng_sp = self._spinbox(r_ng, v_ng_t, 0.1, 60.0, 0.1, width=8)
        ng_sp.pack(side=tk.LEFT)
        _unit(r_ng, "sec")
        def _upd_ng_t(*a):
            try:
                val = float(v_ng_t.get())
                if val <= 0.0:
                    val = 0.1
                    v_ng_t.set(f"{val:.1f}")
                s["ng_output_time"] = val
            except Exception:
                val = 0.1
                v_ng_t.set(f"{val:.1f}")
                s["ng_output_time"] = val
            self._mark_changed()
        v_ng_t.trace_add("write", _upd_ng_t)

        # グループ3: ファイルパス
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        g3 = _make_group(scroll_f, "ファイルパス設定")

        # 結果出力先
        r_res = _row_frame(g3)
        _lbl(r_res, "結果出力先フォルダ:", "ログ・CSV・保存画像の親フォルダを絶対パスで指定します。")
        vp = tk.StringVar(value=self.temp_data["storage"].get("results_dir", ""))
        _entry_w(r_res, vp, width=40)
        _browse_btn(r_res, vp, mode="dir")
        vp.trace_add("write", lambda *a: self.temp_data["storage"].update({"results_dir": vp.get()}))

        # AIモデルパス (.pt ファイル または ncnn フォルダ)
        r_mdl = _row_frame(g3)
        _lbl(r_mdl, "AIモデルパス:",
             "推論に使用するYOLOモデルを指定します。\n"
             "・.pt ファイル: 「.pt参照」ボタンでファイルを選択\n"
             "・ncnnモデル: 「ncnnフォルダ参照」ボタンでフォルダを選択")
        vm = tk.StringVar(value=s.get("model_path", ""))
        _entry_w(r_mdl, vm, width=35)
        # .pt ファイル選択ボタン
        _browse_btn(r_mdl, vm, mode="file",
                    filetypes=[("PyTorch モデル", "*.pt"), ("すべてのファイル", "*.*")])
        # ncnn フォルダ選択ボタン
        def _pick_ncnn():
            p = filedialog.askdirectory(title="ncnnモデルフォルダを選択", parent=self)
            if p:
                vm.set(p)
        btn_ncnn = tk.Button(r_mdl, text="ncnnフォルダ", font=FONT_NORMAL,
                             bg=COLOR_BG_INPUT, fg=COLOR_ACCENT,
                             relief="flat", padx=6, pady=2, cursor="hand2",
                             command=_pick_ncnn)
        btn_ncnn.pack(side=tk.LEFT, padx=(4, 0))
        Tooltip(btn_ncnn, "ncnn形式のモデルフォルダ(*.ncnnディレクトリ)を選択します")
        vm.trace_add("write", lambda *a: s.update({"model_path": vm.get()}))

        # グループ 3b: 撮影モード設定
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        st = self.temp_data.get("system", {})
        g_rec = _make_group(scroll_f, "撮影モード設定")

        r_retries = _row_frame(g_rec)
        _lbl(r_retries, "連続撮影回数:", "撮影モード時に1回の手動トリガーで何枚連続撮影するかです。")
        v_retries = tk.StringVar(value=str(st.get("max_retries", 3)))
        sp_retries = self._spinbox(r_retries, v_retries, 1, 99, 1, width=8)
        sp_retries.pack(side=tk.LEFT)
        _unit(r_retries, "枚")
        def _upd_retries(*a):
            try:
                st["max_retries"] = int(v_retries.get())
                self._mark_changed()
            except Exception:
                pass
        v_retries.trace_add("write", _upd_retries)

        r_interval = _row_frame(g_rec)
        _lbl(r_interval, "撮影間隔:", "連続撮影時の1枚ごとの待機時間です。")
        v_interval = tk.StringVar(value=str(st.get("burst_interval", 0.5)))
        sp_interval = self._spinbox(r_interval, v_interval, 0.0, 10.0, 0.1, width=8)
        sp_interval.pack(side=tk.LEFT)
        _unit(r_interval, "sec")
        def _upd_interval(*a):
            try:
                st["burst_interval"] = float(v_interval.get())
                self._mark_changed()
            except Exception:
                pass
        v_interval.trace_add("write", _upd_interval)

        # グループ5: 容量監視（自動削除）
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        g5 = _make_group(scroll_f, "容量監視 / 自動削除")

        st = self.temp_data["storage"]

        r_ad = _row_frame(g5)
        v_ad = tk.BooleanVar(value=bool(st.get("auto_delete_enabled", False)))
        cb = tk.Checkbutton(
            r_ad, text="古い結果画像を自動削除する",
            variable=v_ad, onvalue=True, offvalue=False,
            font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN,
            activebackground=COLOR_BG_PANEL, activeforeground=COLOR_TEXT_MAIN,
            selectcolor=COLOR_BG_INPUT, relief="flat"
        )
        cb.pack(side=tk.LEFT)
        Tooltip(cb, "容量が上限を超えると、保存フォルダ内の古い画像から順番に自動削除します。\nCSVログやモデルファイルは削除されません。")
        v_ad.trace_add("write", lambda *a: st.update({"auto_delete_enabled": v_ad.get()}))

        r_mg = _row_frame(g5)
        _lbl(r_mg, "最大容量上限:", "この容量を超えると古い画像から自動削除します。")
        v_mg = tk.StringVar(value=str(st.get("max_results_gb", "")))
        mg_sp = self._spinbox(r_mg, v_mg, 0.1, 9999.0, 1.0, width=8)
        mg_sp.pack(side=tk.LEFT)
        _unit(r_mg, "GB")
        def _upd_mg(*a):
            try:
                st["max_results_gb"] = float(v_mg.get())
            except Exception:
                pass
        v_mg.trace_add("write", _upd_mg)

        # 現在の使用量表示 (非同期計算)
        v_used = tk.StringVar(value="現在の使用量: 計算中...")
        lbl_used = tk.Label(g5, textvariable=v_used, font=FONT_SET_VAL,
                            bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, anchor="w")
        lbl_used.pack(fill=tk.X, pady=(4, 0))

        def _calc_storage():
            import shutil as _shutil
            _res_dir = st.get("results_dir", "")
            try:
                if _res_dir and os.path.exists(_res_dir):
                    # 大量ファイル走査のためスレッド実行
                    _used = sum(f.stat().st_size for f in Path(_res_dir).rglob('*') if f.is_file())
                    _used_gb = _used / (1024**3)
                    _total_gb = _shutil.disk_usage(_res_dir).total / (1024**3)
                    msg = f"現在の使用量: {_used_gb:.2f} GB / ディスク合計: {_total_gb:.1f} GB"
                    self.after(0, lambda: v_used.set(msg))
                else:
                    self.after(0, lambda: v_used.set("現在の使用量: -"))
            except Exception:
                self.after(0, lambda: v_used.set("(使用量の取得に失敗しました)"))

        threading.Thread(target=_calc_storage, daemon=True).start()

        # グループ6: ショートカット / システム設定
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        g6 = _make_group(scroll_f, "ショートカット / システム設定")

        r_sh = _row_frame(g6)
        _lbl(r_sh, "ショートカット作成:", "デスクトップ上に本アプリを起動するファイルを自動作成します。")

        is_win = sys.platform.startswith("win")
        btn_text = "デスクトップに起動ファイル作成 (.bat)" if is_win else "デスクトップに起動ファイル作成 (.sh)"

        btn_shortcut = tk.Button(
            r_sh, text=btn_text, font=FONT_NORMAL,
            bg=COLOR_ACCENT, fg="white",
            relief="flat", padx=10, pady=4, cursor="hand2",
            command=self._create_desktop_launcher
        )
        btn_shortcut.pack(side=tk.LEFT, padx=(0, 6))
        Tooltip(btn_shortcut, f"デスクトップに本アプリ起動用{'バッチ (.bat)' if is_win else 'シェル (.sh)'}ファイルを作成します")

        r_datetime = _row_frame(g6)
        _lbl(r_datetime, "ラズパイ本体の時刻設定:", "本体のシステム日付・時間を設定・変更します。")

        def _open_datetime_dialog():
            SystemDateTimeDialog(self)

        btn_dt = tk.Button(
            r_datetime, text="ラズパイ本体の時刻設定", font=FONT_NORMAL,
            bg=COLOR_ACCENT, fg="white",
            relief="flat", padx=10, pady=4, cursor="hand2",
            command=_open_datetime_dialog
        )
        btn_dt.pack(side=tk.LEFT, padx=(0, 6))
        Tooltip(btn_dt, "Linux/Raspberry Piのシステム時計(timedatectl/date)を設定するダイアログを開きます")




    # ---- 保存 / GPIO テスト ----

    def validate_pins(self):
        """NG出力ピン番号のバリデーション"""
        try:
            ng_pin = int(str(self.v_ng.get()).strip())
        except (ValueError, tk.TclError):
            messagebox.showerror("バリデーションエラー", "NG出力ピンには有効な数値を入力してください", parent=self)
            return False
        if ng_pin not in VALID_BCM_PINS:
            messagebox.showerror("バリデーションエラー",
                f"NG出力ピン番号 {ng_pin} は有効なBCMピンではありません\n"
                f"有効なピン: {sorted(VALID_BCM_PINS)}", parent=self)
            return False
        return True

    def _validate_delay_cycles(self):
        """遅延サイクル数のバリデーション（0.5刻みOFF時は整数のみ）"""
        st_sys = self.temp_data.get("system", {})
        try:
            delay = float(st_sys.get("delay_cycles", 0))
        except (TypeError, ValueError):
            messagebox.showerror(
                "バリデーションエラー",
                "遅延サイクル数に有効な数値を入力してください。",
                parent=self,
            )
            return False
        if not bool(st_sys.get("commit_half_step", False)):
            if abs(delay - int(delay)) > 1e-9:
                messagebox.showerror(
                    "バリデーションエラー",
                    "0.5刻みモードがOFFのとき、遅延サイクル数は整数のみ指定できます。",
                    parent=self,
                )
                return False
            st_sys["delay_cycles"] = int(delay)
        return True

    def save_and_close(self):
        # バリデーション前に最新のNGピン設定を同期
        try:
            self.temp_data["gpio"]["outputs"]["ng"] = int(self.v_ng.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("バリデーションエラー", "NG出力ピンには数値を入力してください", parent=self)
            return

        # ピンのバリデーション
        if not self.validate_pins():
            return

        # 保存先フォルダのバリデーション (書き込み権限チェック)
        res_dir = self.temp_data["storage"].get("results_dir", "")
        if res_dir:
            try:
                p = Path(res_dir)
                p.mkdir(parents=True, exist_ok=True)
                # テストファイルを書き込んで削除
                test_file = p / f".write_test_{int(time.time())}"
                test_file.touch()
                test_file.unlink()
            except Exception as e:
                messagebox.showerror("バリデーションエラー", 
                    f"出力先フォルダ「{res_dir}」に書き込み権限がないか、パスが無効です。\nエラー: {e}", parent=self)
                return

        self._release_gpio_test_output()

        self.settings.data = self.temp_data
        self.settings.save_settings()

        _release_toplevel_modal(self)

        if hasattr(self, "_live_preview_win") and self._live_preview_win.winfo_exists():
            self._live_preview_win.destroy()
            
        if self.on_close_callback:
            self.on_close_callback()
            
        if hasattr(self.master, "app_instance"):
            app = self.master.app_instance
            app.preview_paused = False
            
        self.destroy()

    def _release_gpio_test_output(self):
        """GPIOテスト出力をOFFにしてデバイスを解放する"""
        if self._gpio_test_output is not None:
            try:
                self._gpio_test_output.off()
                self._gpio_test_output.close()
            except Exception:
                pass
            self._gpio_test_output = None
        self._gpio_test_on = False
        if hasattr(self, "_btn_gpio_test") and self._btn_gpio_test.winfo_exists():
            self._btn_gpio_test.config(text="テスト出力 (OFF)", bg="#546E7A", fg="white")

    def toggle_gpio_test(self):
        """設定画面内で警報出力ピンをON/OFF切り替える"""
        try:
            ng_pin = int(self.v_ng.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("エラー", "NG出力ピンに有効な番号を入力してください", parent=self)
            return

        if self._gpio_test_output is None:
            try:
                self._gpio_test_output = OutputDevice(ng_pin)
            except Exception as e:
                messagebox.showerror("GPIOエラー", f"出力ピンの初期化に失敗しました:\n{e}", parent=self)
                return

        self._gpio_test_on = not self._gpio_test_on
        try:
            if self._gpio_test_on:
                self._gpio_test_output.on()
                self._btn_gpio_test.config(text="テスト出力 (ON)", bg=COLOR_WARNING, fg="black")
            else:
                self._gpio_test_output.off()
                self._btn_gpio_test.config(text="テスト出力 (OFF)", bg="#546E7A", fg="white")
        except Exception as e:
            messagebox.showerror("GPIOエラー", f"出力の切り替えに失敗しました:\n{e}", parent=self)
            self._release_gpio_test_output()

    # ---- 検知エリア（ROI）タブ ----
    def setup_roi(self):
        outer, inner = create_card(self.t_roi, "検知エリア設定（多角形ピック）")
        outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # 左右分割フレーム
        body = tk.Frame(inner, bg=COLOR_BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        # 左側: カメラ映像
        self._roi_canvas = tk.Canvas(body, width=640, height=480, bg="black", highlightthickness=0)
        self._roi_canvas.pack(side=tk.LEFT, padx=(0, 20), pady=10)
        self._roi_canvas.bind("<Button-1>", self._on_roi_canvas_click)

        # 右側: 操作パネル
        ctrl_f = tk.Frame(body, bg=COLOR_BG_PANEL)
        ctrl_f.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=10)

        self._btn_roi_pick = tk.Button(ctrl_f, text="ピック開始", font=FONT_BTN_LARGE,
                                       bg="#546E7A", fg="white", relief="flat", height=2,
                                       command=self._toggle_roi_picking)
        self._btn_roi_pick.pack(fill=tk.X, pady=5)
        Tooltip(self._btn_roi_pick, "クリックで映像上に頂点（点）を追加できる状態にします。")

        self._btn_roi_decide = tk.Button(ctrl_f, text="決定", font=FONT_BTN_LARGE,
                                         bg=COLOR_OK, fg="black", relief="flat", height=2,
                                         command=self._decide_roi_polygon)
        self._btn_roi_decide.pack(fill=tk.X, pady=5)
        Tooltip(self._btn_roi_decide, "現在の頂点で多角形（3点以上）を確定します。")

        self._btn_roi_reset = tk.Button(ctrl_f, text="リセット", font=FONT_BTN_LARGE,
                                        bg="#B0BEC5", fg="black", relief="flat", height=2,
                                        command=self._reset_roi_polygon)
        self._btn_roi_reset.pack(fill=tk.X, pady=5)
        Tooltip(self._btn_roi_reset, "ピックした頂点をすべて消去し、最初からやり直します。")

        # ヒント・解説
        hint_lbl = tk.Label(ctrl_f, text="【操作方法】\n"
                                         "1. 「ピック開始」を押します。\n"
                                         "2. 左のプレビュー映像上をクリックして、\n"
                                         "   検知したいエリアの頂点（3点以上）を\n"
                                         "   順番に指定します。\n"
                                         "3. 「決定」ボタンで確定します。\n"
                                         "4. 最後に「保存して閉じる」を押して\n"
                                         "   設定を保存してください。\n\n"
                                         "※ 3点以上の多角形のみ有効です。",
                            font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB,
                            justify=tk.LEFT, anchor="nw")
        hint_lbl.pack(fill=tk.BOTH, expand=True, pady=(20, 0))

    def _on_tab_changed(self, event):
        nb = event.widget
        selected_tab = nb.tab(nb.select(), "text").strip()
        if selected_tab == "検知エリア":
            self._start_roi_preview()
        else:
            self._stop_roi_preview()

    def _start_roi_preview(self):
        if not self._roi_preview_running:
            self._roi_preview_running = True
            self._roi_preview_loop()

    def _stop_roi_preview(self):
        self._roi_preview_running = False

    def _roi_preview_loop(self):
        if not self._roi_preview_running or not self.winfo_exists():
            return

        frame = self._roi_read_frame()
        if frame is not None:
            # 640x480にアスペクト比固定またはリサイズ
            frame_resized = cv2.resize(frame, (640, 480))
            frame_rgb = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB)

            # 点と線を描画
            pts = self._roi_points
            color = (0, 255, 0) if not self._roi_picking else (255, 165, 0)  # 通常時は緑、ピック時はオレンジ
            
            # 線を描画
            if len(pts) >= 2:
                for idx in range(len(pts) - 1):
                    p1 = (int(pts[idx][0] * 640), int(pts[idx][1] * 480))
                    p2 = (int(pts[idx+1][0] * 640), int(pts[idx+1][1] * 480))
                    cv2.line(frame_rgb, p1, p2, color, 2)
                
                # 決定済み、あるいは3点以上ピックした場合は閉じる
                if not self._roi_picking or len(pts) >= 3:
                    p1 = (int(pts[-1][0] * 640), int(pts[-1][1] * 480))
                    p2 = (int(pts[0][0] * 640), int(pts[0][1] * 480))
                    cv2.line(frame_rgb, p1, p2, color, 2)

            # 点を描画
            for idx, pt in enumerate(pts):
                px = int(pt[0] * 640)
                py = int(pt[1] * 480)
                # 頂点円
                cv2.circle(frame_rgb, (px, py), 6, color, -1)
                # 番号テキスト
                cv2.putText(frame_rgb, str(idx + 1), (px - 4, py + 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1, cv2.LINE_AA)

            # Canvasに描画
            img = Image.fromarray(frame_rgb)
            photo = ImageTk.PhotoImage(image=img)
            self._roi_canvas.create_image(0, 0, anchor=tk.NW, image=photo)
            self._roi_canvas.image = photo
        else:
            # カメラ映像が取得できない場合は黒画面にテキスト
            self._roi_canvas.delete("all")
            self._roi_canvas.create_text(320, 240, text="カメラ映像が取得できません", fill="white", font=FONT_NORMAL)

        # 100ms後に再呼び出し
        self.after(100, self._roi_preview_loop)

    def _roi_read_frame(self):
        """アプリのcapsからフレームを取得する。取得できなければ新しくVideoCaptureを開いてフォールバックする。"""
        app = getattr(self.master, "app_instance", None)
        if app:
            with app.camera_lock:
                cap = next(iter(app.caps.values()), None)
                if cap and cap.isOpened():
                    ret, frame = cap.read()
                    if ret:
                        return frame

        # フォールバック: カメラインデックスを直接開く
        if self.temp_data["cameras"]:
            cidx = self.temp_data["cameras"][0].get("index", 0)
            try:
                cap = cv2.VideoCapture(int(cidx))
                if cap.isOpened():
                    ret, frame = cap.read()
                    cap.release()
                    if ret:
                        return frame
            except Exception:
                pass
        return None

    def _on_roi_canvas_click(self, event):
        if not self._roi_picking:
            return
        # キャンバスサイズ(640x480)で正規化して座標を追加
        norm_x = max(0.0, min(1.0, event.x / 640.0))
        norm_y = max(0.0, min(1.0, event.y / 480.0))
        self._roi_points.append([norm_x, norm_y])

    def _toggle_roi_picking(self):
        self._roi_picking = not self._roi_picking
        if self._roi_picking:
            self._btn_roi_pick.config(text="ピック中 (クリックで点追加)", bg=COLOR_WARNING, fg="black")
        else:
            self._btn_roi_pick.config(text="ピック開始", bg="#546E7A", fg="white")

    def _decide_roi_polygon(self):
        if len(self._roi_points) < 3:
            messagebox.showerror("エラー", "検知エリアは3点以上の多角形で指定してください。", parent=self)
            return

        self._roi_picking = False
        self._btn_roi_pick.config(text="ピック開始", bg="#546E7A", fg="white")
        self.temp_data.setdefault("inference", {})["roi_polygon"] = [list(pt) for pt in self._roi_points]
        self._mark_changed()
        messagebox.showinfo("確定", "検知エリアを確定しました。保存ボタンを押すことで変更が保存されます。", parent=self)

    def _reset_roi_polygon(self):
        if messagebox.askyesno("リセット", "ピックしたすべての頂点をクリアしますか？", parent=self):
            self._roi_points = []
            self.temp_data.setdefault("inference", {})["roi_polygon"] = None
            self._roi_picking = False
            self._btn_roi_pick.config(text="ピック開始", bg="#546E7A", fg="white")
            self._mark_changed()

    def _get_desktop_path(self):
        home = os.path.expanduser("~")
        if sys.platform.startswith("win"):
            desktop = os.path.join(home, "Desktop")
            if os.path.exists(desktop):
                return desktop
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
                )
                path, _ = winreg.QueryValueEx(key, "Desktop")
                winreg.CloseKey(key)
                expanded = os.path.expandvars(path)
                if os.path.exists(expanded):
                    return expanded
            except Exception:
                pass
            return desktop
        else:
            desktop = os.path.join(home, "Desktop")
            if os.path.exists(desktop):
                return desktop
            desktop_ja = os.path.join(home, "デスクトップ")
            if os.path.exists(desktop_ja):
                return desktop_ja
            user_dirs = os.path.join(home, ".config", "user-dirs.dirs")
            if os.path.exists(user_dirs):
                try:
                    with open(user_dirs, "r", encoding="utf-8") as f:
                        for line in f:
                            if line.startswith("XDG_DESKTOP_DIR"):
                                p = line.split("=")[1].strip().strip('"')
                                p = p.replace("$HOME", home)
                                if os.path.exists(p):
                                    return p
                except Exception:
                    pass
            return desktop

    def _create_desktop_launcher(self):
        try:
            desktop_dir = self._get_desktop_path()
            if not os.path.exists(desktop_dir):
                os.makedirs(desktop_dir, exist_ok=True)

            app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            python_exe = sys.executable

            is_win = sys.platform.startswith("win")
            if is_win:
                filename = "システム起動.bat"
                file_path = os.path.join(desktop_dir, filename)
                content = (
                    "@echo off\n"
                    "chcp 65001 > nul\n"
                    "title システム起動\n"
                    f'cd /d "{app_dir}"\n'
                    f'"{python_exe}" main.py\n'
                    "if %errorlevel% neq 0 (\n"
                    "    echo.\n"
                    "    echo エラーが発生しました。キーを押すと終了します...\n"
                    "    pause > nul\n"
                    ")\n"
                )
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
            else:
                filename = "システム起動.sh"
                file_path = os.path.join(desktop_dir, filename)
                content = (
                    "#!/bin/bash\n"
                    f'cd "{app_dir}"\n'
                    f'"{python_exe}" main.py\n'
                    "if [ $? -ne 0 ]; then\n"
                    '    read -p "エラーが発生しました。Enterキーを押すと終了します..."\n'
                    "fi\n"
                )
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
                try:
                    os.chmod(file_path, 0o755)
                except Exception:
                    pass

            messagebox.showinfo(
                "ショートカット作成成功",
                f"デスクトップに起動スクリプトを作成しました:\n\n{file_path}",
                parent=self
            )
        except Exception as ex:
            messagebox.showerror(
                "作成失敗",
                f"起動スクリプトの作成中にエラーが発生しました:\n{ex}",
                parent=self
            )


