#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py - メインアプリケーション (InspectionSystem)
"""

import cv2
import threading
import time
import datetime
import logging
import queue
import os
import sys
import random
import csv
import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk
from pathlib import Path

from .constants import (
    RESULTS_DIR, COLOR_BG_MAIN, COLOR_BG_PANEL, COLOR_BG_INPUT,
    COLOR_TEXT_MAIN, COLOR_TEXT_SUB, COLOR_ACCENT, COLOR_OK, COLOR_NG, COLOR_WARNING,
    FONT_BOLD, FONT_LARGE, FONT_NORMAL, FONT_FAMILY, VERSION
)
from .hardware import OutputDevice, is_gpio_available, MockManager
from .settings import SettingsManager
from .widgets import create_card, Tooltip, HelpWindow, TenKeyDialog, get_commit_display_style
from .dialogs import SettingsDialog, _activate_toplevel

try:
    import pygame
    PYGAME_AVAILABLE = True
    # pygame.mixer.init() は起動時に行わず、初回使用時に遅延初期化する
except ImportError:
    PYGAME_AVAILABLE = False


def _ensure_mixer():
    """pygame.mixerを初回使用時に遅延初期化する (起動時間短縮)"""
    if PYGAME_AVAILABLE and not pygame.mixer.get_init():
        try:
            pygame.mixer.init()
        except Exception:
            pass

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False


class InspectionSystem:
    def __init__(self):
        self.settings = SettingsManager()
        self.setup_dirs()
        self.setup_logging()

        self.running = True
        self.camera_lock = threading.Lock()
        self.caps = {}
        self.last_frames = {}  # 最新フレーム保持用
        self.trigger_queue = queue.Queue()
        self.out_ng = None

        # リアルタイム監視用の状態変数
        self.alert_active = False
        self.dummy_detection_active = False
        self.alert_confirm_hits = 0
        self.alert_frame_counter = 0
        self._cached_alert_detections = []
        self._draft_roi = None
        self.roi_dragging = False
        self.roi_drag_start = None
        self.roi_drag_end = None
        self.roi_edit_cam = None

        self.result_display_frames = {}
        self.result_display_until = 0
        self.preview_paused = False
        self.inspecting = False
        self.settings_open = False
        self._settings_dialog = None
        self.manual_capture_trigger = False
        self.btn_manual_capture = None

        self.model = None
        self.model_lock = threading.Lock()
        self.load_model()
        self.setup_hardware()
        self.setup_gui()
        if sys.platform == "win32" and not is_gpio_available():
            self.setup_mock_ui()
        self.root.after(30 * 1000, self._monitor_storage)

    def _get_alert_config(self):
        """リアルタイム監視に必要な設定をまとめて返す。"""
        inf = self.settings.data.get("inference", {})
        target_str = str(inf.get("alert_target_classes", "truck")).strip()
        # "すべてのクラス" が選択されているか空の場合は None (全クラス対象)
        if target_str in ("", "すべてのクラス"):
            target_classes = None
        else:
            target_classes = [c.strip() for c in target_str.split(",") if c.strip()]
            if not target_classes:
                target_classes = None
        return {
            "enabled": bool(inf.get("alert_enabled", True)),
            "threshold": float(inf.get("threshold", 0.5)),
            "confirm_frames": max(1, int(inf.get("alert_confirm_frames", 3))),
            "infer_every_n_frames": max(1, int(inf.get("alert_infer_every_n_frames", 2))),
            "target_classes": target_classes,
        }

    def _normalize_roi(self, roi, frame_w, frame_h):
        """ROIを画面サイズに収め、四角形として使える形へ正規化する。"""
        if not isinstance(roi, dict):
            roi = {"xmin": 0, "ymin": 0, "xmax": frame_w, "ymax": frame_h}

        x1 = max(0, min(int(roi.get("xmin", 0)), frame_w - 1))
        y1 = max(0, min(int(roi.get("ymin", 0)), frame_h - 1))
        x2 = max(x1 + 10, min(int(roi.get("xmax", frame_w)), frame_w))
        y2 = max(y1 + 10, min(int(roi.get("ymax", frame_h)), frame_h))
        return {"xmin": x1, "ymin": y1, "xmax": x2, "ymax": y2}

    def _get_current_roi(self, frame_w=None, frame_h=None):
        """ドラッグ中の仮ROIがあれば優先し、なければ保存済みROIを返す。"""
        roi = self._draft_roi or self.settings.data.get("inference", {}).get("roi")
        if frame_w is None or frame_h is None:
            return roi
        return self._normalize_roi(roi, frame_w, frame_h)

    def _has_detection_in_roi(self, detections, roi):
        """検出ボックスの中心点がROI内に入っているか判定する。"""
        if roi is None:
            return False

        for det in detections:
            cx = (det["x1"] + det["x2"]) / 2.0
            cy = (det["y1"] + det["y2"]) / 2.0
            if roi["xmin"] <= cx <= roi["xmax"] and roi["ymin"] <= cy <= roi["ymax"]:
                return True
        return False

    def _build_live_detections(self, frame):
        """リアルタイム監視用にYOLO推論を実行し、検出結果を返す。"""
        cfg = self._get_alert_config()
        if self.inspecting or not self.model:
            return []

        try:
            with self.model_lock:
                res = self.model.predict(frame, conf=cfg["threshold"], half=True, verbose=False)[0]
            detections = []
            for box in res.boxes:
                cls_name = res.names[int(box.cls[0])]
                if cfg["target_classes"] is not None and cls_name not in cfg["target_classes"]:
                    continue
                conf = float(box.conf[0])
                x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                detections.append({
                    "class_name": cls_name,
                    "confidence": conf,
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                })
            self._cached_alert_detections = detections
            return detections
        except Exception as e:
            self.logger.error(f"リアルタイム監視の推論に失敗しました: {e}")
            return []

    def _update_live_alert_state(self, frame):
        """監視結果に基づきGPIOと状態を更新する。"""
        cfg = self._get_alert_config()
        if self.inspecting:
            if self.alert_active and self.out_ng:
                self.out_ng.off()
            self.alert_active = False
            return [], False

        self.alert_frame_counter += 1
        if self.alert_frame_counter % cfg["infer_every_n_frames"] == 0 or not self._cached_alert_detections:
            detections = self._build_live_detections(frame)
        else:
            detections = self._cached_alert_detections

        roi = self._get_current_roi(frame.shape[1], frame.shape[0])
        # ダミー接近検知が有効な場合は強制的に検知ありとみなす
        inside_roi = self._has_detection_in_roi(detections, roi) or getattr(self, "dummy_detection_active", False)

        if inside_roi:
            self.alert_confirm_hits += 1
        else:
            self.alert_confirm_hits = 0

        alert_active = self.alert_confirm_hits >= cfg["confirm_frames"]

        if alert_active and not self.alert_active:
            self.alert_active = True
            if self.out_ng:
                self.out_ng.on()
            if self.root.winfo_exists():
                self.root.after(0, lambda: self._update_monitor_status_ui("検知中", COLOR_NG))
                self.root.after(0, lambda: self.update_status("検知中", COLOR_NG))
            self.logger.info("リアルタイム監視で警報を開始しました。")
            self.save_alert_image(frame, detections)

            # 警報出力時間の設定に応じた自動OFFタイマーの適用
            try:
                out_time = self.settings.data.get("system", {}).get("ng_output_time", 2.0)
                out_time_f = float(out_time) if out_time else 2.0
                if out_time_f > 0:
                    ms = int(out_time_f * 1000)
                    self.root.after(ms, self.stop_buzzer)
            except Exception as e:
                self.logger.error(f"警報出力タイマー設定エラー: {e}")
        elif not alert_active and self.alert_active:
            self.alert_active = False
            if self.out_ng:
                self.out_ng.off()
            if self.root.winfo_exists():
                self.root.after(0, lambda: self._update_monitor_status_ui("監視中", COLOR_OK))
                self.root.after(0, lambda: self.update_status("監視中", COLOR_OK))
            self.logger.info("リアルタイム監視で警報を解除しました。")

        if not self.inspecting and self.root.winfo_exists():
            ui_state = "alert" if alert_active else "normal"
            if getattr(self, "_last_monitor_ui_state", None) != ui_state:
                self._last_monitor_ui_state = ui_state
                if alert_active:
                    self.root.after(0, lambda: self._update_monitor_status_ui("検知中", COLOR_NG))
                else:
                    self.root.after(0, lambda: self._update_monitor_status_ui("監視中", COLOR_OK))

        return detections, alert_active

    def _draw_monitor_overlay(self, frame, detections, alert_active):
        """リアルタイム監視用のROIと検出枠を描画する。"""
        roi = self._get_current_roi(frame.shape[1], frame.shape[0])
        color = (0, 0, 255) if alert_active else (0, 255, 0)
        cv2.rectangle(frame, (roi["xmin"], roi["ymin"]), (roi["xmax"], roi["ymax"]), color, 2)


        for det in detections:
            pt1 = (int(det["x1"]), int(det["y1"]))
            pt2 = (int(det["x2"]), int(det["y2"]))
            label = f"{det['class_name']} {det['confidence']:.2f}"
            # バウンディングボックス
            cv2.rectangle(frame, pt1, pt2, (255, 255, 255), 2)

            # クラス名＋信頼度ラベル（太字・背景付きで視認性を向上）
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.8
            thickness = 2
            (text_w, text_h), _ = cv2.getTextSize(label, font, font_scale, thickness)
            text_x = pt1[0]
            text_y = max(20, pt1[1] - 10)
            # 背景ボックス
            cv2.rectangle(
                frame,
                (text_x, text_y - text_h - 6),
                (text_x + text_w + 6, text_y + 2),
                (0, 0, 0),
                thickness=-1,
            )
            # 文字
            cv2.putText(
                frame,
                label,
                (text_x + 3, text_y - 3),
                font,
                font_scale,
                (255, 255, 255),
                thickness,
                lineType=cv2.LINE_AA,
            )


        return frame

    def save_alert_image(self, frame, detections):
        """検知開始時のみ画像を保存する (DETECT フォルダ、新命名規則)。"""
        if frame is None:
            return
        save_dir = Path(self.get_results_dir()) / "images" / "DETECT"
        save_dir.mkdir(parents=True, exist_ok=True)
        # ファイル名: DETECT_{日時12桁}_{最大確信度}.jpg
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M")
        max_conf = max((d.get("confidence", 0.0) for d in detections), default=1.0)
        filename = f"DETECT_{timestamp}_{max_conf:.2f}.jpg"
        out_path = save_dir / filename
        # 検知エリア・バウンディングボックスを描画
        annotated = self._draw_monitor_overlay(frame.copy(), detections, True)
        # res_detect 設定に合わせてリサイズ
        res_setting = self.settings.data.get("storage", {}).get("res_detect", "1920x1080")
        if "x" in res_setting:
            try:
                w, h = map(int, res_setting.split("x"))
                annotated = cv2.resize(annotated, (w, h))
            except Exception:
                pass
        if cv2.imwrite(str(out_path), annotated):
            self.logger.info(f"検知画像を保存しました: {out_path}")
        else:
            self.logger.error(f"検知画像の保存に失敗しました: {out_path}")

    def _start_roi_edit(self, cam_id, event):
        """プレビュー上でROIのドラッグ編集を開始する。"""
        frame = self.last_frames.get(cam_id)
        if frame is None:
            return

        self.roi_dragging = True
        self.roi_edit_cam = cam_id
        self.roi_drag_start = self._label_to_frame_coords(cam_id, event.x, event.y)
        self.roi_drag_end = self.roi_drag_start
        self._draft_roi = self._normalize_roi({
            "xmin": self.roi_drag_start[0],
            "ymin": self.roi_drag_start[1],
            "xmax": self.roi_drag_start[0],
            "ymax": self.roi_drag_start[1],
        }, frame.shape[1], frame.shape[0])
        self.root.config(cursor="crosshair")

    def _update_roi_edit(self, cam_id, event):
        """ドラッグ中のROIを更新する。"""
        if not self.roi_dragging or cam_id != self.roi_edit_cam:
            return
        self.roi_drag_end = self._label_to_frame_coords(cam_id, event.x, event.y)
        self._draft_roi = self._normalize_roi({
            "xmin": min(self.roi_drag_start[0], self.roi_drag_end[0]),
            "ymin": min(self.roi_drag_start[1], self.roi_drag_end[1]),
            "xmax": max(self.roi_drag_start[0], self.roi_drag_end[0]),
            "ymax": max(self.roi_drag_start[1], self.roi_drag_end[1]),
        }, self.last_frames[cam_id].shape[1], self.last_frames[cam_id].shape[0])

    def _finish_roi_edit(self, cam_id, event):
        """ROI編集を確定し、設定を保存する。"""
        if not self.roi_dragging or cam_id != self.roi_edit_cam:
            return
        self._update_roi_edit(cam_id, event)
        self.roi_dragging = False
        self.roi_drag_start = None
        self.roi_drag_end = None
        self.roi_edit_cam = None
        self.root.config(cursor="")

        if self._draft_roi is not None:
            self.settings.data.setdefault("inference", {})["roi"] = self._draft_roi.copy()
            self.settings.save_settings()
            self.logger.info(f"検知エリアを更新しました: {self._draft_roi}")
        self._draft_roi = None

    def _label_to_frame_coords(self, cam_id, x, y):
        """Tkinter上の座標を実フレーム座標へ変換する。"""
        label = self.cam_labels.get(cam_id)
        frame = self.last_frames.get(cam_id)
        if label is None or frame is None:
            return (0, 0)

        label_w = max(1, label.winfo_width())
        label_h = max(1, label.winfo_height())
        frame_w = frame.shape[1]
        frame_h = frame.shape[0]
        return (
            max(0, min(frame_w - 1, int(x * frame_w / label_w))),
            max(0, min(frame_h - 1, int(y * frame_h / label_h))),
        )

    def setup_mock_ui(self):
        """モックモード時の仮想GPIO操作パネル (Windowsデバッグ用)"""
        try:
            self.mock_root = tk.Toplevel(self.root)
            self.mock_root.title("仮想GPIO/デバッグパネル")
            self.mock_root.geometry("400x380")
            self.mock_root.configure(bg=COLOR_BG_MAIN)
            self.mock_root.attributes("-topmost", True)
            self.mock_root.resizable(False, False)

            container = tk.Frame(self.mock_root, bg=COLOR_BG_MAIN, padx=20, pady=20)
            container.pack(fill=tk.BOTH, expand=True)

            # --- デバッグ操作 (ダミー検知) ---
            outer_d, inner_d = create_card(container, "デバッグ操作")
            outer_d.pack(fill=tk.X, pady=(0, 15))

            self.v_dummy_detect = tk.BooleanVar(value=getattr(self, "dummy_detection_active", False))
            
            def toggle_dummy_detect():
                self.dummy_detection_active = self.v_dummy_detect.get()
                self.logger.info(f"ダミー検知を {'有効' if self.dummy_detection_active else '無効'} にしました。")
                if self.dummy_detection_active:
                    btn_dummy.config(bg=COLOR_NG, fg="black")
                else:
                    btn_dummy.config(bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN)

            btn_dummy = tk.Checkbutton(inner_d, text="ダミー接近検知 (ON/OFF)",
                                       font=FONT_NORMAL, variable=self.v_dummy_detect,
                                       bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN,
                                       selectcolor=COLOR_BG_INPUT, activebackground=COLOR_BG_PANEL,
                                       activeforeground=COLOR_TEXT_MAIN, relief="flat",
                                       command=toggle_dummy_detect)
            btn_dummy.pack(fill=tk.X, pady=8)
            Tooltip(btn_dummy, "ONにすると、カメラの映像に関わらず常に車両検知状態をシミュレートします。")

            # --- 出力 (ステータス) ---
            outer_s, inner_s = create_card(container, "仮想出力")
            outer_s.pack(fill=tk.X)

            self.mock_indicators = {}
            ng_pin = self.settings.data["gpio"]["outputs"].get("ng", 20)
            
            f = tk.Frame(inner_s, bg=COLOR_BG_PANEL)
            f.pack(fill=tk.X, pady=8)
            
            lbl = tk.Label(f, text=f"警報出力 (NG BCM {ng_pin})", font=FONT_NORMAL, bg=COLOR_BG_PANEL, 
                           fg=COLOR_TEXT_MAIN, width=20, anchor="w")
            lbl.pack(side=tk.LEFT)
            
            # インジケータ (外枠と中身)
            outer_ind = tk.Frame(f, width=24, height=24, bg=COLOR_BG_INPUT, padx=2, pady=2)
            outer_ind.pack(side=tk.RIGHT)
            outer_ind.pack_propagate(False)

            ind = tk.Frame(outer_ind, bg="#444")
            ind.pack(fill=tk.BOTH, expand=True)
            self.mock_indicators[str(ng_pin)] = (ind, COLOR_NG)

            # 凡例・閉じる
            low_f = tk.Frame(container, bg=COLOR_BG_MAIN)
            low_f.pack(fill=tk.X, pady=(20, 0))
            tk.Label(low_f, text="※Windowsデバッグ専用機能です", 
                     font=(FONT_FAMILY, 9), bg=COLOR_BG_MAIN, fg=COLOR_TEXT_SUB).pack()

            self._update_mock_ui()
        except Exception as e:
            self.logger.error(f"仮想GPIOパネル初期化エラー: {e}")

    def _update_mock_ui(self):
        """出力状態を周期的に確認してUIを更新"""
        try:
            if not hasattr(self, "mock_indicators") or not hasattr(self, "mock_root"):
                return
            
            if not self.mock_root.winfo_exists():
                return
            
            # 出力状態の更新
            for pin, ind_data in self.mock_indicators.items():
                ind, color = ind_data
                state = MockManager.get_output_state(pin)
                ind.configure(bg=color if state else "#444")
            
            self.mock_root.after(200, self._update_mock_ui)
        except Exception as e:
            self.logger.error(f"仮想GPIOパネル更新エラー: {e}")
            self.logger.error(f"仮想GPIOパネル更新エラー: {e}")

    def load_model(self):
        """設定されたパスからYOLOモデルをロードする (.pt / ncnn フォルダ両対応)"""
        if not YOLO_AVAILABLE:
            self.logger.warning("ultralytics がインストールされていないため、推論はシミュレーションモードで動作します。")
            return

        model_path = self.settings.data["inference"].get("model_path")
        if not model_path:
            self.logger.warning("モデルパスが設定されていません。シミュレーションモードで動作します。")
            return

        path_obj = Path(model_path)
        if not path_obj.exists():
            self.logger.warning(f"モデルファイルが見つからないため、シミュレーションモードで動作します: {model_path}")
            return

        # NCNNモデルのフォルダ名要件チェック
        # Ultralyticsはフォルダ名の末尾が「.ncnn」であることでNCNNモデルと識別する
        # (*.ncnn/best.ncnn.param, *.ncnn/best.ncnn.bin が必要)
        if path_obj.is_dir() and not str(model_path).endswith(".ncnn"):
            self.logger.error(
                f"[NCNNフォルダ名エラー] 指定フォルダの名前が '.ncnn' で終わっていません。\n"
                f"  現在のパス: {model_path}\n"
                f"  Ultralyticsはフォルダ名が '*.ncnn' であることでNCNNモデルと識別します。\n"
                f"  一例: /home/pi/inspection_app/models/best.ncnn\n"
                f"  フォルダ内構成: best.ncnn/best.ncnn.param, best.ncnn/best.ncnn.bin"
            )
            return

        try:
            is_ncnn = path_obj.is_dir() or str(model_path).endswith(".ncnn")
            self.model = YOLO(model_path, task="detect") if is_ncnn else YOLO(model_path)
            fmt = "ncnn" if is_ncnn else "pt"
            self.logger.info(f"YOLOモデルをロードしました ({fmt}): {model_path}")

            # --- ウォームアップ推論 ---
            # YOLOは初回 predict() 時に内部グラフのJITコンパイルが走るため
            # 起動後すぐにバックグラウンドでダミー推論を行い、初回検査の遅延を解消する
            def _warmup(model=self.model):
                try:
                    import numpy as np
                    dummy = np.zeros((640, 640, 3), dtype=np.uint8)
                    with self.model_lock:
                        model.predict(dummy, verbose=False)
                    self.logger.info("YOLOモデルのウォームアップ完了")
                except Exception:
                    pass  # ウォームアップ失敗は無視（実際の推論には影響しない）
            threading.Thread(target=_warmup, daemon=True).start()

        except Exception as e:
            self.logger.error(f"YOLOモデルのロードに失敗しました: {e}")

    def get_results_dir(self):
        """設定から結果保存ディレクトリを取得する"""
        path_str = self.settings.data["storage"].get("results_dir", str(RESULTS_DIR))
        return Path(path_str)

    # ------------------------------------------------------------------
    # 初期設定
    # ------------------------------------------------------------------
    def setup_dirs(self):
        try:
            res_dir = self.get_results_dir()
            # 大元のディレクトリを確実に作成する
            res_dir.mkdir(parents=True, exist_ok=True)
            for d in ["REC", "DETECT"]:
                (res_dir / "images" / d).mkdir(parents=True, exist_ok=True)
            (res_dir / "logs").mkdir(parents=True, exist_ok=True)
            (res_dir / "csv").mkdir(parents=True, exist_ok=True)
            print(f"ディレクトリ構成を確認しました: {res_dir}")
        except Exception as e:
            # GUI起動前なのでprintも使用
            msg = f"ディレクトリ生成エラー: {e}\n現在の結果出力先: {self.get_results_dir()}"
            print(msg)
            if hasattr(self, 'logger'):
                self.logger.error(msg)

    def setup_logging(self):
        res_dir = self.get_results_dir()
        log_f = res_dir / "logs" / f"app_{datetime.datetime.now().strftime('%Y%m%d')}.log"
        # 既存のハンドラをクリアして再設定可能にする
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(message)s',
            handlers=[
                logging.FileHandler(log_f, encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

    def setup_hardware(self):
        prev_paused = getattr(self, 'preview_paused', False)
        self.preview_paused = True
        try:
            for c in self.caps.values():
                c.release()
            self.caps = {}
            data = self.settings.data
            # カメラ1台のみ
            cam = data["cameras"][0]
            backend = cv2.CAP_V4L2 if sys.platform.startswith('linux') else cv2.CAP_ANY
            cap = cv2.VideoCapture(int(cam["index"]), backend)
            if cap and cap.isOpened():
                w, h = map(int, data["storage"]["capture_res"].split('x'))
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                with self.camera_lock:
                    self.caps[cam["id"]] = cap
                self.logger.info(f"カメラ(インデックス {cam['index']})を初期化しました: {cam['name']}")
            else:
                self.logger.error(f"カメラ(インデックス {cam['index']})を開けませんでした")
            # 既存の out_ng を安全にクローズして解放
            if hasattr(self, "out_ng") and self.out_ng is not None:
                try:
                    self.out_ng.off()
                    self.out_ng.close()
                except Exception:
                    pass
                self.out_ng = None
            # NG出力のみ
            self.out_ng = OutputDevice(data["gpio"]["outputs"]["ng"])
        except Exception as e:
            self.logger.error(f"ハードウェアエラー: {e}")
        finally:
            self.preview_paused = prev_paused

    def get_current_pattern(self):
        st = []
        for s in self.settings.data["gpio"].get("pattern_pins", []):
            pin_id = f"sel_{s['id']}"
            if pin_id in self.inputs:
                st.append(1 if self.inputs[pin_id].is_active else 0)
            else:
                st.append(0)
        for pid in self.settings.data["pattern_order"]:
            p = self.settings.data["patterns"][pid]
            if p.get("pin_condition") == st:
                return pid
        return None

    # ------------------------------------------------------------------
    # GUI
    # ------------------------------------------------------------------
    def setup_gui(self):
        self.root = tk.Tk()
        self.root.title(f"自動検査システム {VERSION}")
        self.root.geometry("1400x900")
        self.root.configure(bg=COLOR_BG_MAIN)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # ウィンドウ最大化（Windows/Raspi(X11)/Wayland対応）
        self.root.update_idletasks()
        try:
            self.root.state("zoomed")          # Windows向け
        except tk.TclError:
            try:
                # ラズパイ(X11)向け: タスクバーを隠さずに最大化
                self.root.attributes('-zoomed', True)
            except tk.TclError:
                # Wayland (Bookworm等) またはその他のフォールバック
                # 画面サイズを取得してジオメトリに設定
                w = self.root.winfo_screenwidth()
                h = self.root.winfo_screenheight()
                self.root.geometry(f"{w}x{h}+0+0")

        # --- ヘッダー ---
        self.header = tk.Frame(self.root, bg=COLOR_BG_PANEL, height=80)
        self.header.pack(fill=tk.X)

        self.lbl_status = tk.Label(self.header, text="検査中", font=FONT_LARGE,
                                   bg=COLOR_BG_PANEL, fg=COLOR_ACCENT)
        self.lbl_status.pack(side=tk.LEFT, padx=30, pady=15)

        self.lbl_clock = tk.Label(self.header, text="", font=FONT_LARGE,
                                  bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN)
        self.lbl_clock.pack(side=tk.RIGHT, padx=30)
        self.update_clock()

        btn_help = tk.Button(self.header, text="？", font=FONT_BOLD,
                             bg=COLOR_BG_INPUT, fg=COLOR_ACCENT,
                             relief="flat", width=3,
                             command=self.show_main_help)
        btn_help.pack(side=tk.RIGHT, padx=10)
        Tooltip(btn_help, "操作方法を表示します")

        # --- モード切り替えボタン ---
        mode_frm = tk.Frame(self.header, bg=COLOR_BG_PANEL)
        mode_frm.pack(side=tk.RIGHT, padx=20)

        self.v_mode = tk.StringVar(
            value=self.settings.data["inference"].get("mode", "inspection"))

        def _set_mode(m):
            self.v_mode.set(m)
            self.settings.data["inference"]["mode"] = m
            self.settings.save_settings()
            self.update_mode_ui()

        self.btn_insp = tk.Button(mode_frm, text="検査モード", font=FONT_BOLD,
                                  width=12, relief="flat",
                                  command=lambda: _set_mode("inspection"))
        self.btn_insp.pack(side=tk.LEFT, padx=5)

        self.btn_rec = tk.Button(mode_frm, text="撮影モード", font=FONT_BOLD,
                                 width=12, relief="flat",
                                 command=lambda: _set_mode("recording"))
        self.btn_rec.pack(side=tk.LEFT, padx=5)

        self.update_mode_ui()

        # --- メインコンテンツ ---
        main = tk.Frame(self.root, bg=COLOR_BG_MAIN)
        main.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # カメラプレビュー
        # pack_propagate(False) により、プレビュー画像のサイズに引っ張られてカメラエリアが膨張するのを抑止する
        self.v_frm_outer, v_frm_inner = create_card(main, "カメラプレビュー")
        self.v_frm_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.v_frm_outer.pack_propagate(False)
        self.cam_labels = {}

        self.v_frm = tk.Frame(v_frm_inner, bg=COLOR_BG_PANEL)
        self.v_frm.pack(fill=tk.BOTH, expand=True)

        cams = self.settings.data["cameras"]
        rows = 2 if len(cams) > 2 else 1
        cols = 2 if len(cams) >= 2 else 1

        for i, c in enumerate(cams):
            f = tk.Frame(self.v_frm, bg=COLOR_BG_PANEL, bd=1, relief="solid")
            f.grid(row=i // cols, column=i % cols, sticky="nsew", padx=5, pady=5)
            l = tk.Label(f, bg="black")
            l.pack(fill=tk.BOTH, expand=True)
            self.cam_labels[c["id"]] = l
            l.bind("<ButtonPress-1>", lambda e, cid=c["id"]: self._start_roi_edit(cid, e))
            l.bind("<B1-Motion>", lambda e, cid=c["id"]: self._update_roi_edit(cid, e))
            l.bind("<ButtonRelease-1>", lambda e, cid=c["id"]: self._finish_roi_edit(cid, e))

        for i in range(rows):
            self.v_frm.rowconfigure(i, weight=1)
        for i in range(cols):
            self.v_frm.columnconfigure(i, weight=1)

        # 操作パネル
        pnl_outer, pnl = create_card(main, "操作パネル")
        pnl_outer.pack(side=tk.RIGHT, fill=tk.Y, padx=(20, 0))
        pnl_outer.config(width=420)
        pnl_outer.pack_propagate(False)

        # リアルタイム監視ステータス
        tk.Label(pnl, text="監視ステータス", font=FONT_BOLD,
                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(pady=(5, 2))
        self.lbl_monitor_status = tk.Label(pnl, text="監視中", font=FONT_LARGE,
                                           bg=COLOR_BG_INPUT, fg=COLOR_OK, pady=5)
        self.lbl_monitor_status.pack(fill=tk.X, padx=10)

        # 撮影モード専用：手動トリガーボタン
        self.btn_manual_capture = tk.Button(pnl, text="撮影", font=FONT_BOLD, bg=COLOR_OK,
                                             fg="black", height=2, relief="flat",
                                             command=self.trigger_manual_capture)
        # 最初は非表示
        self.btn_manual_capture.pack(fill=tk.X, padx=10, pady=(10, 5))
        self.update_manual_capture_visibility()

        tk.Button(pnl, text="結果フォルダ", font=FONT_NORMAL, bg="#546E7A",
                  fg="white", relief="flat",
                  command=self.open_results_folder).pack(fill=tk.X, padx=10, pady=5)
        tk.Button(pnl, text="詳細設定", font=FONT_BOLD, bg="#455A64",
                  fg="white", height=2, relief="flat",
                  command=self.open_settings).pack(fill=tk.X, padx=10, pady=5)

        # アプリインスタンスの保持とループ開始
        self.root.app_instance = self
        threading.Thread(target=self._preview_loop, daemon=True).start()
        threading.Thread(target=self._main_logic_loop, daemon=True).start()

    def on_closing(self):
        """アプリケーション終了時のリソース解放と安全なシャットダウン"""
        if messagebox.askokcancel("終了", "アプリケーションを終了しますか？"):
            self.running = False
            self.logger.info("シャットダウン処理を開始します...")

            # 仮想GPIOモックの終了 (使用時)
            if hasattr(self, "mock_root") and self.mock_root.winfo_exists():
                try:
                    self.mock_root.destroy()
                except Exception: pass

            # GPIOリソースの解放
            try:
                for d in getattr(self, 'inputs', {}).values():
                    if hasattr(d, 'close'): d.close()
                for d in getattr(self, 'outputs', {}).values():
                    if hasattr(d, 'close'): d.close()
            except Exception as e:
                self.logger.error(f"GPIO解放エラー: {e}")

            # カメラリソースの解放
            try:
                for c in getattr(self, 'caps', {}).values():
                    c.release()
            except Exception as e:
                self.logger.error(f"カメラ解放エラー: {e}")

            if PYGAME_AVAILABLE:
                try:
                    pygame.mixer.quit()
                except Exception: pass

            self.root.destroy()
            self.logger.info("シャットダウン完了")


    def update_clock(self):
        now = datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        self.lbl_clock.config(text=now)
        self.root.after(1000, self.update_clock)

    def _monitor_storage(self):
        """保存フォルダの容量を確認し、上限を超えた場合は古い画像から削除する（10分おき・非同期）"""
        _INTERVAL_MS = 10 * 60 * 1000  # 10分
        
        def _thread_task():
            try:
                st = self.settings.data.get("storage", {})
                if not st.get("auto_delete_enabled", False):
                    return

                max_gb = float(st.get("max_results_gb", 0))
                if max_gb <= 0:
                    return

                res_dir = Path(self.get_results_dir())
                images_dir = res_dir / "images"
                if not images_dir.exists():
                    return

                # 全画像ファイルを更新日時昇順（古い順）でリストアップ
                # ※ 大量のファイル走査が発生するため、このスレッド内で実行
                img_files = sorted(
                    [f for f in images_dir.rglob("*") if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png")],
                    key=lambda f: f.stat().st_mtime
                )
                total_size = sum(f.stat().st_size for f in img_files)

                # ディスクの空き容量をチェック
                import shutil
                usage = shutil.disk_usage(res_dir)
                free_gb = usage.free / (1024 ** 3)
                max_bytes = max_gb * (1024 ** 3)

                needs_deletion = False
                target_bytes = total_size

                # (1) 画像フォルダの総使用量が設定上限を超過している場合
                if total_size > max_bytes:
                    needs_deletion = True
                    target_bytes = max_bytes * 0.9  # 上限の90%まで減らす
                # (2) ディスク全体の空き容量が 1.0 GB を切った場合（フェイルセーフ）
                elif free_gb < 1.0:
                    needs_deletion = True
                    # 空きを増やすため、現在の画像フォルダサイズから 1GB 分減らす
                    target_bytes = max(0, total_size - (1.0 * 1024**3))

                if not needs_deletion:
                    return

                self.logger.info(f"[容量監視] 削除開始。画像サイズ: {total_size/(1024**3):.2f} GB / 空き容量: {free_gb:.2f} GB")

                deleted_count = 0
                for f in img_files:
                    if total_size <= target_bytes:
                        break
                    try:
                        file_size = f.stat().st_size
                        f.unlink()
                        total_size -= file_size
                        deleted_count += 1
                    except Exception as e:
                        self.logger.warning(f"[容量監視] 削除失敗: {f.name} - {e}")

                if deleted_count > 0:
                    self.logger.info(f"[容量監視] {deleted_count} 件削除完了。残画像サイズ: {total_size/(1024**3):.2f} GB")
            except Exception as e:
                self.logger.error(f"[容量監視] エラー: {e}")
            finally:
                # 終わったら次のタイマーをセット (スレッド内からでも root.after は安全に呼べる)
                if self.running:
                    self.root.after(_INTERVAL_MS, self._monitor_storage)

        # 非同期実行
        t = threading.Thread(target=_thread_task, daemon=True)
        t.start()

    def get_commit_str(self):
        st_sys = self.settings.data.get("system", {})
        is_half_step = bool(st_sys.get("commit_half_step", False))
        if is_half_step:
            return f"{self.commit_number:06.1f}"
        else:
            return f"{int(self.commit_number):04d}"

    def adjust_commit(self, delta):
        st_sys = self.settings.data.get("system", {})
        is_half_step = bool(st_sys.get("commit_half_step", False))
        step = 0.5 if is_half_step else 1.0

        self.commit_number += delta * step
        if self.commit_number > 9999.0:
            self.commit_number = 1.0
        elif self.commit_number < 1.0:
            self.commit_number = 9999.0
        # UI更新はメインスレッド経由で実行（Tkinterスレッドセーフ対応）
        self.root.after(0, lambda: self.v_commit.set(self.get_commit_str()))

    def update_commit_display(self):
        commit_font, commit_width = get_commit_display_style(
            bool(self.settings.data.get("system", {}).get("commit_half_step", False))
        )
        if hasattr(self, "lbl_commit") and self.lbl_commit.winfo_exists():
            self.lbl_commit.config(font=commit_font, width=commit_width)
        if hasattr(self, "v_commit"):
            self.v_commit.set(self.get_commit_str())

    def manual_commit_set(self):
        is_half_step = bool(self.settings.data.get("system", {}).get("commit_half_step", False))
        d = TenKeyDialog(self.root, "コミット番号設定", self.commit_number, is_half_step)
        if d.result is not None:
            self.commit_number = float(d.result)
            self.v_commit.set(self.get_commit_str())

    def manual_commit_set_initial(self):
        try:
            is_half_step = bool(self.settings.data.get("system", {}).get("commit_half_step", False))
            d = TenKeyDialog(self.root, "開始コミット番号", self.commit_number, is_half_step)
            if d.result is not None:
                self.commit_number = float(d.result)
                self.v_commit.set(self.get_commit_str())
        except Exception as e:
            self.logger.error(f"初期コミット番号設定エラー: {e}")

    def stop_buzzer(self):
        """NG出力（警報出力）を停止する"""
        if self.out_ng:
            self.out_ng.off()

    def trigger_manual_capture(self):
        """撮影モード時の手動トリガーボタン"""
        if self.v_mode.get() == "recording":
            self.manual_capture_trigger = True
            # 撮影モード時はステータスバーを「撮影中」に変更
            self.update_status("撮影中", COLOR_ACCENT)
            self.logger.info("手動撮影トリガーを実行しました")

    def update_manual_capture_visibility(self):
        """モード変更に応じて手動トリガーボタンの表示・非表示を切り替え"""
        if not getattr(self, "btn_manual_capture", None):
            return
        if self.v_mode.get() == "recording":
            self.btn_manual_capture.pack(fill=tk.X, padx=10, pady=(10, 5))
        else:
            self.btn_manual_capture.pack_forget()

    def open_results_folder(self):
        """結果画像フォルダをOSのファイルマネージャーで開く"""
        folder = self.get_results_dir() / "images"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))
            elif sys.platform.startswith("linux"):
                import subprocess
                subprocess.Popen(["xdg-open", str(folder)])
            else:
                import subprocess
                subprocess.Popen(["open", str(folder)])
        except Exception as e:
            self.logger.error(f"結果フォルダを開けませんでした: {e}")
            messagebox.showerror("エラー", f"フォルダを開けませんでした:\n{folder}", parent=self.root)

    def on_history_double_click(self, e):
        # 削除済み - NG履歴ビューアは廃止されました
        pass

    def clear_history(self):
        # 削除済み - NG履歴は廃止されました
        pass

    def update_mode_ui(self):
        """モード変更時にUIを更新"""
        m = self.v_mode.get()
        if m == "inspection":
            self.btn_insp.config(bg=COLOR_ACCENT, fg="black")
            self.btn_rec.config(bg=COLOR_BG_INPUT, fg=COLOR_TEXT_SUB)
            self.update_status("検査中", COLOR_OK)
        else:
            self.btn_insp.config(bg=COLOR_BG_INPUT, fg=COLOR_TEXT_SUB)
            self.btn_rec.config(bg=COLOR_WARNING, fg="black")
            # 撮影モード待機中
            self.update_status("撮影待機中", COLOR_ACCENT)
        # 撮影モード時に手動撮影ボタンの表示・非表示を切り替え
        self.update_manual_capture_visibility()

    def _capture_and_save_manual(self, frame, camera_id, burst_index=1):
        """手動撮影モードで現在のフレームを保存する"""
        if frame is None or frame.size == 0:
            return
        try:
            res_key = "res_record"
            res_setting = self.settings.data["storage"].get(res_key, "640x480")
            
            save_frame = frame
            if res_setting != "保存しない" and "x" in res_setting:
                try:
                    w, h = map(int, res_setting.split("x"))
                    save_frame = cv2.resize(frame, (w, h))
                except Exception: pass

            # ファイル名: REC_タイムスタンプ_連番.jpg
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            filename = f"REC_{timestamp}_{burst_index:02d}.jpg"
            res_dir = self.get_results_dir()
            save_dir = res_dir / "images" / "REC"
            save_dir.mkdir(parents=True, exist_ok=True)
            save_path = save_dir / filename
            
            def _do_write(path, img, fname):
                if cv2.imwrite(str(path), img):
                    self.logger.info(f"手動撮影保存: {fname}")
                else:
                    self.logger.error(f"手動撮影保存失敗: {path}")
            threading.Thread(target=_do_write, args=(save_path, save_frame, filename), daemon=True).start()
        except Exception as e:
            self.logger.error(f"手動撮影処理エラー: {e}")

    def show_main_help(self):
        help_data = {
            "概要": (
                "AIを用いた車両等の接近監視システム（Sentry）です。\n\n"
                "【基本的な仕組み】\n"
                "1. カメラ映像からAIが車両等の対象物をリアルタイムに検出します。\n"
                "2. 検出された対象物が「プレビュー上の赤枠（検知エリア）」の内側に一定フレーム連続して入ると「検知中」とみなされます。\n"
                "3. 「検知中」の間、指定されたGPIOピンから警報ON信号が出力されます。"
            ),
            "検知エリアの調整": (
                "プレビュー画面上でドラッグ操作を行うことで、検知エリアの範囲を自由に変更できます。\n"
                "設定画面の「システム最適化」で、さらに検知のしきい値や確認フレーム数を調整可能です。"
            ),
            "警報出力時間": (
                "設定画面の「警報出力時間」で信号出力の時間を変更できます。\n"
                "・秒数を指定した場合: 接近検知後に指定時間だけ警報信号をONにし、その後自動でOFFにします。\n"
                "・空欄にした場合: 対象物が監視エリアからいなくなるまでONを出力し続けます。"
            ),
            "容量監視（自動削除）": (
                "ディスク容量不足を防ぐための自動削除機能が動作しています。\n"
                "設定画面から容量上限や自動削除のON/OFFを設定できます。"
            ),
        }
        HelpWindow(self.root, "接近監視システム 操作ヘルプ", help_data)

    def open_settings(self):
        # 設定画面を開く。
        # NOTE: 以前は競合回避のためここでカメラを解放していましたが、
        # 判定しきい値のライブプレビューを有効にするため、維持するように変更します。
        # with self.camera_lock:
        #     temp_caps = self.caps
        #     self.caps = {}
        # 
        # for cap in temp_caps.values():
        #     cap.release()
        dlg = getattr(self, "_settings_dialog", None)
        if dlg is not None:
            try:
                if dlg.winfo_exists():
                    _activate_toplevel(dlg, self.root)
                    return
            except tk.TclError:
                pass

        self.settings_open = True
        self.logger.info("設定画面を開きました。設定画面が閉じるまで検査処理をスキップします。")
        # GPIOの競合を防ぐため、設定画面を開く前に既存のGPIOデバイスを解放する
        if hasattr(self, "out_ng") and self.out_ng is not None:
            try:
                self.out_ng.off()
                self.out_ng.close()
            except Exception:
                pass
            self.out_ng = None
        self._settings_dialog = SettingsDialog(self.root, self.settings, self.on_settings_closed)

    def reset_delay_pattern_queue(self):
        """仕様情報遅延キューと経過サイクル数をリセットする"""
        self.delay_pattern_queue.clear()
        self.elapsed_cycles = 0.0
        self.cycle_is_delayed_skip = False
        self.cycle_active_pat_id = None
        self.logger.info("仕様情報遅延キューをリセットしました")

    def on_settings_closed(self):
        self.settings_open = False
        self._settings_dialog = None
        self.logger.info("設定画面が閉じられました。検査処理を再開します。")
        self.setup_hardware()
        self.v_mode.set(self.settings.data["inference"].get("mode", "inspection"))
        self.update_commit_display()
        self.update_mode_ui()

    # ------------------------------------------------------------------
    # プレビューループ
    # ------------------------------------------------------------------
    def _preview_loop(self):
        """Raspi 5向け軽量プレビューループ"""
        while self.running:
            current_caps = []
            # 検査中またはプレビュー一時停止中は更新をスキップ（CPU負荷削減）
            if self.preview_paused or self.inspecting:
                time.sleep(0.1)
                continue

            # 結果表示時間中は判定結果画像を固定表示する
            if time.time() < self.result_display_until:
                for cid, pil_img in list(self.result_display_frames.items()):
                    if not getattr(self.cam_labels.get(cid), 'is_updating', False):
                        self.cam_labels[cid].is_updating = True
                        def _upd_static(c=cid, img_data=pil_img):
                            if c in self.cam_labels:
                                try:
                                    tk_img = ImageTk.PhotoImage(img_data)
                                    self.cam_labels[c].config(image=tk_img)
                                    self.cam_labels[c].img = tk_img
                                except Exception: pass
                                finally:
                                    self.cam_labels[c].is_updating = False
                        self.root.after(0, _upd_static)
                time.sleep(0.1)
                continue

            t_start = time.time()

            # 手動撮影トリガー（撮影モード時の連続撮影）
            if self.manual_capture_trigger and self.v_mode.get() == "recording":
                self.manual_capture_trigger = False
                max_retries = max(1, int(self.settings.data.get("system", {}).get("max_retries", 5)))
                burst_interval = float(self.settings.data.get("system", {}).get("burst_interval", 0.5))

                for capture_idx in range(max_retries):
                    with self.camera_lock:
                        burst_caps = list(self.caps.items())
                    for cid, cap in burst_caps:
                        try:
                            with self.camera_lock:
                                if cid not in self.caps:
                                    continue
                                grabbed = cap.grab()
                                if grabbed:
                                    ret, frame = cap.retrieve()
                                else:
                                    ret = False
                            if ret and frame is not None:
                                self._capture_and_save_manual(frame, cid, burst_index=capture_idx + 1)
                        except Exception as e:
                            self.logger.error(f"手動撮影エラー (cid={cid}): {e}")
                    if capture_idx < max_retries - 1:
                        time.sleep(burst_interval)

                # 連続撮影完了後、ステータスバーを更新
                if self.root.winfo_exists():
                    self.root.after(0, lambda: self.update_status("撮影完了", COLOR_OK))

            with self.camera_lock:
                current_caps = list(self.caps.items())

            for cid, cap in current_caps:
                if self.preview_paused or self.inspecting:
                    break
                try:
                    with self.camera_lock:
                        if cid not in self.caps:
                            continue
                        grabbed = cap.grab()
                        if grabbed:
                            ret, frame = cap.retrieve()
                        else:
                            ret = False
                    if grabbed and ret:
                        preview_frame = frame.copy()
                        detections, alert_active = self._update_live_alert_state(preview_frame)
                        preview_frame = self._draw_monitor_overlay(preview_frame, detections, alert_active)
                        self.last_frames[cid] = preview_frame
                        # Tkinterのイベントキュー詰まりによるカクつきを防止
                        # (描画が追いつかない場合は、重いリサイズ・色変換・PIL変換そのものをスキップする)
                        if not getattr(self.cam_labels.get(cid), 'is_updating', False):
                            self.cam_labels[cid].is_updating = True
                             
                            def _upd_live(c=cid, f_data=preview_frame):
                                if c in self.cam_labels:
                                    try:
                                        # 重い処理をルートスレッド（Tkinter）側に逃がさず、
                                        # かといって描画キューが詰まらないように制限をかける
                                        preview_res = self.settings.data["storage"].get("preview_res", "320x240")
                                        if preview_res != "プレビューなし":
                                            try:
                                                pw, ph = map(int, preview_res.split('x'))
                                            except Exception:
                                                pw, ph = 320, 240
                                             
                                            img = cv2.resize(f_data, (pw, ph), interpolation=cv2.INTER_LINEAR)
                                            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                                            pil_img = Image.fromarray(img)
                                             
                                            tk_img = ImageTk.PhotoImage(pil_img)
                                            self.cam_labels[c].config(image=tk_img)
                                            self.cam_labels[c].img = tk_img
                                    except Exception: pass
                                    finally:
                                        self.cam_labels[c].is_updating = False

                            self.root.after(0, _upd_live)
                except Exception as e:
                    self.logger.error(f"Preview error (cid={cid}): {e}")

            fps = self.settings.data["inference"].get("preview_fps", 10)
            elapsed = time.time() - t_start
            wait_time = max(0.01, (1.0 / max(0.1, float(fps))) - elapsed)
            time.sleep(wait_time)

    # ------------------------------------------------------------------
    # 画像保存
    # ------------------------------------------------------------------
    def save_result_images(self, result_type, frame, camera_name, pattern_name,
                           confidence=1.0, trig_name="Trig1", burst_index=None):
        """命名規則に従って画像を保存する"""
        if frame is None:
            return None

        # 設定から解像度を取得してリサイズ
        if result_type == "REC":
            res_key = "res_record"
        elif result_type == "NG_RAW":
            # NG_RAW は NG と同じ解像度設定を使う
            res_key = "res_ng"
        else:
            res_key = f"res_{result_type.lower()}"
        res_setting = self.settings.data["storage"].get(res_key, "640x480")
        
        if res_setting == "保存しない":
            return None
            
        save_frame = frame
        if "x" in res_setting:
            try:
                w, h = map(int, res_setting.split("x"))
                save_frame = cv2.resize(frame, (w, h))
            except Exception: pass

        # ファイル名を組み立て: (判定結果)_(コミット番号)_(パターン名)_(カメラ名)_(トリガー名)_(信頼度)
        b_suffix = f"_{burst_index:02d}" if burst_index is not None else ""
        filename = (f"{result_type}_{self.get_commit_str()}_{pattern_name}_"
                    f"{camera_name}_{trig_name}{b_suffix}_{confidence:.2f}.jpg")

        # ファイル名に使えない文字を除去
        filename = "".join([c for c in filename if c not in '<>:"/\\|?*'])
        res_dir = self.get_results_dir()
        save_dir = res_dir / "images" / result_type
        save_dir.mkdir(parents=True, exist_ok=True)
        
        save_path = save_dir / filename
        
        # [案C] 非同期保存: SDカードへの書き込みブロッキングを回避
        def _do_write(path, img, fname):
            if cv2.imwrite(str(path), img):
                self.logger.info(f"保存成功: {fname}")
            else:
                self.logger.error(f"保存失敗: {path}")
        threading.Thread(target=_do_write, args=(save_path, save_frame, filename), daemon=True).start()
            
        return save_path

    def append_to_csv(self, pattern_name, camera_name, class_name, detected_count, res_type, confidence):
        """CSVファイルに判定結果を記録する（[P-5] 非同期書き込みでブロッキング解消）"""
        today = datetime.datetime.now().strftime('%Y%m%d')
        now_time = datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')
        res_dir = self.get_results_dir()
        csv_dir = res_dir / "csv"
        csv_dir.mkdir(parents=True, exist_ok=True)
        csv_file = csv_dir / f"inspection_results_{today}.csv"

        file_exists = csv_file.exists()
        header = ["日時", "コミット番号", "パターン名", "カメラ名", "判定対象クラス名", "検出個数", "判定結果", "信頼度"]
        data = [now_time, self.get_commit_str(), pattern_name, camera_name, class_name, detected_count, res_type, f"{confidence:.2f}"]

        def _do_csv(path, row, hdr, needs_hdr):
            try:
                with open(path, 'a', encoding='utf-8-sig', newline='') as f:
                    writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
                    if needs_hdr:
                        writer.writerow(hdr)
                    writer.writerow(row)
            except Exception as e:
                self.logger.error(f"CSV書き込みエラー: {e}")

        threading.Thread(target=_do_csv, args=(csv_file, data, header, file_exists is False), daemon=True).start()

    def _evaluate_conditions(self, conditions, detections):
        """
        複数条件による判定を行う。

        Args:
            conditions: [{"class": "dog", "count": "2"}, {"class": "cat", "count": "1"}]
            detections: {"dog": 3, "cat": 1, ...} クラスごとの検出個数

        Returns:
            "OK" | "NG" | "SKIP"
        """
        # --- 判定ロジックの再確認用ログ ---
        self.logger.debug(f"DEBUG: evaluate_conditions - conditions={conditions}, detections={detections}")

        # 条件が空ならスキップ
        if not conditions:
            return "SKIP"

        # 全条件を満たすかチェック (AND条件)
        for cond in conditions:
            cls_name = cond.get("class", "").strip()
            count_val = cond.get("count")
            
            # 基準個数は空欄NG (空欄の場合は判定失敗とする)
            if count_val is None or str(count_val).strip() == "":
                self.logger.info(f"判定NG: 基準個数が設定されていません (クラス: {cls_name or '全検出数'})")
                return "NG"
                
            try:
                required = int(str(count_val).strip())
            except ValueError:
                self.logger.error(f"判定NG: 基準個数が不正な数値です ({count_val})")
                return "NG"

            # 対象クラスの検出数を取得
            if cls_name:
                actual = detections.get(cls_name, 0)
            else:
                # クラス未指定の場合は全クラスの合計
                actual = sum(detections.values())

            # 判定ロジック: 一致判定 (基準個数と同じならOK、それ以外はNG)
            if actual != required:
                self.logger.info(f"判定NG: {cls_name or '全検出数'} が不一致 (基準={required}, 実際={actual})")
                return "NG"
                    
        return "OK"

    def _capture_burst_images(self, retries, interval):
        """指定回数のバースト撮影を行い、フレームのリストを返す
        
        パフォーマンス改善: grab() でフレームをバッファし、ロック外で retrieve() する。
        これによりカメラロックの保持時間を最小化し、プレビューループをブロックしない。
        """
        captured_frames = []
        d = self.settings.data
        cam_names = {c["id"]: c["name"] for c in d["cameras"]}

        for shot_idx in range(max(1, retries)):
            # --- (1) すべてのカメラで grab() (フレーム取得の予約)
            # grab()済みの cap オブジェクト自体も保持する（デプロイ中に caps が差し替わっても retrieve() を正しいインスタンスに対して実行できる）
            with self.camera_lock:
                # 初回のショットの前に、カメラのバッファに溜まっている古いフレームを空読みして破棄する
                if shot_idx == 0:
                    for cid, cap in self.caps.items():
                        # 3回空読みして古いバッファをクリア
                        for _ in range(3):
                            cap.grab()

                grabbed = {}  # {cid: capオブジェクト}
                for cid, cap in self.caps.items():
                    if cap.grab():
                        grabbed[cid] = cap  # grab成功時はcap自体を保持

            # --- (2) ロック外で retrieve() (デコード処理) ---
            shot_data = []
            for cid, cap in grabbed.items():
                ret, frame = cap.retrieve()
                if ret:
                    shot_data.append((cid, cam_names.get(cid, cid), frame))

            if shot_data:
                captured_frames.append(shot_data)
            if shot_idx < retries - 1 and interval > 0:
                time.sleep(interval)
        return captured_frames

    def _inspect_frames(self, captured_frames, mode, is_skip, pat_id, trig_id, pat_name, trig_name):
        """
        収集したフレームに対してAI推論と条件判定を行う（インクリメンタル判定）
        
        判定ロジック:
        - 検査モード: 1回でもOK判定なら総合判定OK（以降のリトライをスキップ）
        - 撮影モード: 全フレーム保存
        """
        d = self.settings.data
        results = []
        final_best_frames = {}
        
        # 判定結果の集計用
        any_ok = False

        # [P-3] conditions をループ外でカメラ別にキャッシュ（バーストごとの設定dict参照を削減）
        conditions_cache = {}  # {cid: [条件リスト]}
        if not is_skip and pat_id:
            stage = self.settings.data["patterns"][pat_id]["stages"].get(trig_id, {})
            cond_data = stage.get("conditions", [])
            for cam in self.settings.data["cameras"]:
                cid = cam["id"]
                if isinstance(cond_data, list):
                    conditions_cache[cid] = cond_data
                else:
                    conditions_cache[cid] = cond_data.get(str(cid), [])

        for burst_idx, shot_group in enumerate(captured_frames):
            shot_results = []
            for cid, cam_name, frame in shot_group:
                # 入力画像フレームのバリデーション（破損・空画像の排除）
                if frame is None or not hasattr(frame, "shape") or frame.size == 0 or len(frame.shape) < 2 or frame.shape[0] == 0 or frame.shape[1] == 0:
                    self.logger.warning(
                        f"不正な画像フレームを検出 (カメラ: {cam_name}, shape: {getattr(frame, 'shape', None)})。スキップします。"
                    )
                    continue

                if mode == "recording":
                    # 撮影モード: 保存のみ（バーストごとに保存）
                    save_needed = True
                    if is_skip and d["storage"].get("res_record_skip", "") == "保存しない":
                        save_needed = False
                    if save_needed:
                        # サイクル中は同じコミット番号を使用、全バースト保存
                        self.save_result_images("REC", frame, cam_name, pat_name, 
                                                trig_name=trig_name, burst_index=burst_idx + 1)
                    shot_results.append("OK")
                else:
                    # ---- 推論実行 ----
                    detections = {}
                    confidence = 0.0
                    
                    # [案B] res.plot() 遅延実行: 最良フレーム確定時のみ呼ぶためここでは保持だけする
                    _yolo_res = None
                    frame_to_save = frame

                    conditions = conditions_cache.get(cid, [])
                    if is_skip or not conditions:
                        res_type = "SKIP"
                        total_detected = 0
                        cond_summary = "-"
                        det_summary = "0"
                    else:
                        if self.model:
                            try:
                                # 判定閾値を取得
                                threshold = d["inference"].get("threshold", 0.5)
                                # 実際のモデル推論 (ハーフ精度 + 閾値を適用)
                                # half=True でFP16精度化 → ラズパイで推論速度50%高速化
                                with self.model_lock:
                                    res = self.model.predict(frame, conf=threshold, half=True, verbose=False)[0]
                                _yolo_res = res  # plot() は最良フレーム確定後に一度だけ実行する

                                # クラスごとの個数を集計 (念のためここでも閾値チェック)
                                for box in res.boxes:
                                    conf_val = float(box.conf[0])
                                    if conf_val < threshold:
                                        continue
                                        
                                    cls_id = int(box.cls[0])
                                    cls_name = res.names[cls_id]
                                    detections[cls_name] = detections.get(cls_name, 0) + 1
                                    # 最も高い信頼度を代表値にする
                                    confidence = max(confidence, conf_val)

                            except Exception as e:
                                import traceback
                                self.logger.error(f"推論実行エラー: {e}")
                                self.logger.error(traceback.format_exc())
                                detections = {}
                                confidence = 0.0
                        else:
                            # モデル未設定時はシミュレーションモード
                            detections = {"object": 1}
                            confidence = random.uniform(0.85, 0.99)
                            
                        total_detected = sum(detections.values())

                        # [P-3] ループ前にキャッシュ済みの conditions を使用
                        res_type = self._evaluate_conditions(conditions, detections)
                        if total_detected == 0:
                            confidence = 0.00

                        cond_summary = ", ".join(
                            f"{c.get('class', '*')}x{c.get('count', '?')}" for c in conditions
                        ) if conditions else "-"
                        
                        # ログの「実際検出数」と合わせるため、全検出結果の要約を作成
                        det_summary = ", ".join(f"{k}:{v}" for k, v in detections.items()) if detections else "0"

                    shot_results.append(res_type)
                    
                    # OK判定時はフラグを更新
                    if res_type == "OK":
                        any_ok = True
                    
                    # 最良フレーム選択（OK優先）
                    # [案B] このフレームが保存対象になる場合のみ res.plot() を実行（描画コスト削減）
                    if (cid, cam_name) not in final_best_frames or res_type == "OK":
                        if _yolo_res is not None:
                            frame_to_save = _yolo_res.plot()  # 確定時に1度だけ描画処理
                        final_best_frames[(cid, cam_name)] = (frame_to_save, frame, res_type, confidence, cond_summary, det_summary)

            if shot_results:
                results = shot_results
                
                # === インクリメンタル判定ロジック ===
                if mode == "inspection":
                    # 1回でもOK判定があれば総合判定OK（以降のリトライをスキップ）
                    if any_ok:
                        self.logger.info(f"バースト撮影 {burst_idx + 1}回目でOK判定確定。以降のリトライをスキップ")
                        break
                    # すべてがNG（NGが続いている）なら次のリトライへ
                    # （ただしバースト数が max_retries に達したら終了）
                    
        return results, final_best_frames

    # process_inspectionは不要なので削除

    def _update_monitor_status_ui(self, text, color):
        """操作パネルの監視ステータス表示を更新する"""
        if not getattr(self, "lbl_monitor_status", None):
            return
        try:
            if self.lbl_monitor_status.winfo_exists():
                self.lbl_monitor_status.config(text=text, fg=color)
        except tk.TclError:
            pass

    def update_status(self, text, color):
        """ステータス表示とヘッダー色の更新"""
        self.lbl_status.config(text=text, fg="white" if color != COLOR_BG_PANEL else COLOR_ACCENT)
        self.header.config(bg=color)
        self.lbl_status.config(bg=color)
        self.lbl_clock.config(bg=color)
        # ヘッダー内の全ウィジェットの背景を合わせる（必要に応じて）
        for w in self.header.winfo_children():
            try:
                if not isinstance(w, tk.Button): # ボタンの色は変えない
                    w.config(bg=color)
            except: pass

    def add_history(self, trig_id):
        # 削除済み - NG履歴は廃止されました
        pass

    def clear_trigger_queue(self):
        """トリガーキューに溜まっているイベントをすべて破棄する"""
        while not self.trigger_queue.empty():
            try:
                self.trigger_queue.get_nowait()
            except queue.Empty:
                break

    def _main_logic_loop(self):
        while self.running:
            try:
                trig_id = self.trigger_queue.get(timeout=1.0)

                # 設定画面が開いている間はトリガーを無視して検査をスキップする
                if self.settings_open:
                    trig_name = next((t["name"] for t in self.settings.data["gpio"]["triggers"] if t["id"] == trig_id), trig_id)
                    self.logger.warning(
                        f"設定画面表示中にトリガーを受信しました（受信={trig_name}）。検査をスキップします。"
                    )
                    continue

                self.process_inspection(trig_id)

                # --- キューフラッシュ（余剰トリガー破棄）---
                # ハーフステップモードでは、1つのコミット番号に対して同じトリガーが
                # 2回入ることがある（ドアライン対応）ため、サイクル未完了の場合は
                # キューを破棄しない（次のトリガーを待つ）。
                # サイクルが完了した場合のみ余剰トリガーを破棄する。
                if not self.trigger_queue.empty():
                    cycle_just_completed = (self.cycle_active_pat_id is None and len(self.cycle_fired_trigs) == 0)
                    if cycle_just_completed:
                        # サイクル完了直後: 余剰なチャタリングトリガーのみ破棄
                        self.logger.info("サイクル完了後の余剰トリガーをスキップします")
                        while not self.trigger_queue.empty():
                            try:
                                self.trigger_queue.get_nowait()
                            except queue.Empty:
                                break
                    # サイクル継続中: キューを破棄せず次のトリガーを処理する
                # ----------------------------------
            except queue.Empty:
                pass
