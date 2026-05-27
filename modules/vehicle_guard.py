#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vehicle_guard.py - Raspberry Pi 5向けの軽量車両監視システム

このスクリプトは、USBカメラの映像をYOLOv8nで解析し、
指定したROI内に車両が進入したときにGPIO出力をONにします。
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import cv2

from gpiozero import OutputDevice

try:
    from ultralytics import YOLO
except ImportError:
    YOLO = None


class VehicleGuardSystem:
    """単一カメラ・単一GPIO向けの車両監視システム"""

    DEFAULT_CONFIG = {
        "camera": {
            "index": 0,
            "width": 1280,
            "height": 720,
        },
        "model": {
            "path": "yolov8n.pt",
            "conf": 0.45,
            "iou": 0.45,
            "infer_every_n_frames": 1,
            "image_size": 640,
        },
        "roi": {
            "xmin": 200,
            "ymin": 120,
            "xmax": 900,
            "ymax": 560,
        },
        "detection": {
            "allowed_classes": ["truck"],
            "confirm_frames": 3,
            "gpio_pin": 18,
            "save_dir": "results/images/alerts",
        },
    }

    def __init__(self):
        self.base_dir = Path(__file__).resolve().parents[1]
        self.config_path = self.base_dir / "vehicle_guard_settings.json"
        self.config = self.load_config()
        self.ensure_directories()
        self.setup_logging()
        self.output = OutputDevice(self.config["detection"]["gpio_pin"], initial_value=False)

        self.model = self.load_model()
        self.capture = self.open_camera()

        self.frame_size = (self.config["camera"]["width"], self.config["camera"]["height"])
        self.roi = self.config["roi"].copy()
        self.current_detections = []
        self.confirm_hits = 0
        self.alert_active = False
        self.saved_current_alert = False
        self.frame_counter = 0
        self.dragging = False
        self.drag_start = None
        self.drag_end = None

        cv2.namedWindow("Vehicle Guard", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("Vehicle Guard", 960, 540)
        cv2.setMouseCallback("Vehicle Guard", self._on_mouse)

    def load_config(self):
        """設定ファイルを読み込み、欠けている項目はデフォルトで補完する。"""
        if not self.config_path.exists():
            config = self._merge_config(self.DEFAULT_CONFIG, {})
            self.save_config(config)
            return config

        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logging.warning(f"設定ファイルの読み込みに失敗したためデフォルト設定を使用します: {exc}")
            raw = {}

        merged = self._merge_config(self.DEFAULT_CONFIG, raw)
        self.save_config(merged)
        return merged

    @staticmethod
    def _merge_config(defaults, overrides):
        """ネストした辞書を安全にマージする。"""
        merged = json.loads(json.dumps(defaults))
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = VehicleGuardSystem._merge_config(merged[key], value)
            else:
                merged[key] = value
        return merged

    def save_config(self, config=None):
        """現在の設定をファイルに保存する。"""
        if config is None:
            config = self.config
        self.config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")

    def ensure_directories(self):
        """ログと保存先のディレクトリを準備する。"""
        save_dir = self._resolve_path(self.config["detection"]["save_dir"])
        save_dir.mkdir(parents=True, exist_ok=True)
        (self.base_dir / "results" / "logs").mkdir(parents=True, exist_ok=True)

    def setup_logging(self):
        """日次ログをファイルに出力する。"""
        log_path = self.base_dir / "results" / "logs" / f"vehicle_guard_{datetime.now().strftime('%Y%m%d')}.log"

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.FileHandler(log_path, encoding="utf-8"),
                logging.StreamHandler(),
            ],
        )
        self.logger = logging.getLogger(__name__)

    def load_model(self):
        """YOLOモデルを読み込む。"""
        if YOLO is None:
            raise RuntimeError("ultralytics がインストールされていません。")

        model_path = self._resolve_path(self.config["model"]["path"])
        if not model_path.exists():
            raise FileNotFoundError(f"YOLOモデルが見つかりません: {model_path}")

        model = YOLO(str(model_path))
        self.logger.info(f"YOLOモデルを読み込みました: {model_path}")
        return model

    def _resolve_path(self, candidate):
        """相対パスをアプリのルート基準に変換する。"""
        path = Path(candidate)
        if path.is_absolute():
            return path
        return self.base_dir / path

    def open_camera(self):
        """カメラを初期化して、取得成功時に返す。"""
        index = int(self.config["camera"]["index"])
        width = int(self.config["camera"]["width"])
        height = int(self.config["camera"]["height"])

        backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
        capture = cv2.VideoCapture(index, backend)
        if not capture.isOpened():
            raise RuntimeError(f"カメラが開けませんでした: index={index}")

        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        capture.set(cv2.CAP_PROP_FPS, 30)

        self.logger.info(f"カメラを初期化しました: index={index}, size={width}x{height}")
        return capture

    def run(self):
        """メインループ。フレームの取得、推論、描画、GPIO制御を繰り返す。"""
        self.output.off()

        while True:
            ok, frame = self.capture.read()
            if not ok or frame is None:
                self.logger.error("フレーム取得に失敗しました。")
                break

            self.frame_size = (frame.shape[1], frame.shape[0])
            self._clamp_roi_to_frame()

            detections = self.infer_frame(frame)
            self.current_detections = detections
            inside_roi = self._has_detection_in_roi(detections)

            if inside_roi:
                self.confirm_hits += 1
            else:
                self.confirm_hits = 0

            alert_active = self.confirm_hits >= int(self.config["detection"]["confirm_frames"])
            if alert_active and not self.alert_active:
                self.output.on()
                self.alert_active = True
                self.saved_current_alert = False
                self.logger.info("検知を開始しました。GPIOをHIGHに設定しました。")
            elif not alert_active and self.alert_active:
                self.output.off()
                self.alert_active = False
                self.saved_current_alert = False
                self.logger.info("検知を解除しました。GPIOをLOWに戻しました。")

            if alert_active and not self.saved_current_alert:
                self.save_alert_image(frame, detections)
                self.saved_current_alert = True

            annotated = self.draw_overlay(frame.copy(), detections, alert_active)
            cv2.imshow("Vehicle Guard", annotated)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("s"):
                self.save_config(self.config)
                self.logger.info("ROI設定を保存しました。")
            if key == ord("r"):
                self.reset_roi()
                self.logger.info("ROIを初期値に戻しました。")

            self.frame_counter += 1

        self.stop()

    def infer_frame(self, frame):
        """指定されたフレームをYOLOで推論し、検出結果を返す。"""
        infer_every = max(1, int(self.config["model"]["infer_every_n_frames"]))
        if self.frame_counter % infer_every != 0 and self.current_detections:
            return self.current_detections

        results = self.model(
            frame,
            conf=float(self.config["model"]["conf"]),
            iou=float(self.config["model"]["iou"]),
            imgsz=int(self.config["model"]["image_size"]),
            verbose=False,
        )

        detections = []
        for result in results:
            names = result.names
            for box in result.boxes:
                cls_id = int(box.cls[0])
                cls_name = names.get(cls_id, str(cls_id))
                if cls_name not in self.config["detection"]["allowed_classes"]:
                    continue

                conf = float(box.conf[0])
                x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                detections.append(
                    {
                        "class_name": cls_name,
                        "confidence": conf,
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                    }
                )

        return detections

    def _has_detection_in_roi(self, detections):
        """検出結果の中心点がROI内かどうかを判定する。"""
        x_min = int(self.roi["xmin"])
        y_min = int(self.roi["ymin"])
        x_max = int(self.roi["xmax"])
        y_max = int(self.roi["ymax"])

        for det in detections:
            cx = (det["x1"] + det["x2"]) / 2.0
            cy = (det["y1"] + det["y2"]) / 2.0
            if x_min <= cx <= x_max and y_min <= cy <= y_max:
                return True
        return False

    def draw_overlay(self, frame, detections, alert_active):
        """ROIと検出ボックスをOpenCVで描画する。"""
        roi_color = (0, 0, 255) if alert_active else (0, 255, 0)
        x_min = int(self.roi["xmin"])
        y_min = int(self.roi["ymin"])
        x_max = int(self.roi["xmax"])
        y_max = int(self.roi["ymax"])

        cv2.rectangle(frame, (x_min, y_min), (x_max, y_max), roi_color, 2)
        cv2.putText(frame, "ROI", (x_min + 6, y_min + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.8, roi_color, 2)

        for det in detections:
            pt1 = (int(det["x1"]), int(det["y1"]))
            pt2 = (int(det["x2"]), int(det["y2"]))
            label = f"{det['class_name']} {det['confidence']:.2f}"
            cv2.rectangle(frame, pt1, pt2, (255, 255, 255), 2)
            cv2.putText(frame, label, (pt1[0], max(20, pt1[1] - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        status = "ALERT" if alert_active else "MONITOR"
        cv2.putText(frame, f"Status: {status}", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        cv2.putText(frame, "Drag ROI with mouse | r: reset | s: save | q: quit", (10, frame.shape[0] - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        return frame

    def save_alert_image(self, frame, detections):
        """検知が開始されたタイミングで画像を保存する。"""
        annotated = self.draw_overlay(frame.copy(), detections, True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        save_dir = self._resolve_path(self.config["detection"]["save_dir"])
        output_path = save_dir / f"{timestamp}_alert.jpg"
        cv2.imwrite(str(output_path), annotated)
        self.logger.info(f"検知画像を保存しました: {output_path}")

    def _on_mouse(self, event, x, y, flags, _):
        """OpenCVのマウスイベントでROIを調整する。"""
        if event == cv2.EVENT_LBUTTONDOWN:
            self.dragging = True
            self.drag_start = (x, y)
            self.drag_end = (x, y)
            return

        if event == cv2.EVENT_MOUSEMOVE and self.dragging:
            self.drag_end = (x, y)
            return

        if event == cv2.EVENT_LBUTTONUP and self.dragging:
            self.dragging = False
            self.drag_end = (x, y)
            self._update_roi_from_drag()

    def _update_roi_from_drag(self):
        """ドラッグ結果をROIに反映し、設定ファイルに保存する。"""
        if self.drag_start is None or self.drag_end is None:
            return

        x1 = min(self.drag_start[0], self.drag_end[0])
        y1 = min(self.drag_start[1], self.drag_end[1])
        x2 = max(self.drag_start[0], self.drag_end[0])
        y2 = max(self.drag_start[1], self.drag_end[1])

        width, height = self.frame_size
        x1 = max(0, min(width - 1, x1))
        y1 = max(0, min(height - 1, y1))
        x2 = max(x1 + 10, min(width, x2))
        y2 = max(y1 + 10, min(height, y2))

        self.roi = {"xmin": x1, "ymin": y1, "xmax": x2, "ymax": y2}
        self.config["roi"] = self.roi.copy()
        self.save_config(self.config)
        self.logger.info(f"ROIを更新しました: {self.roi}")

    def _clamp_roi_to_frame(self):
        """ROIを現在のフレーム範囲内に収める。"""
        width, height = self.frame_size
        self.roi["xmin"] = max(0, min(width - 1, int(self.roi["xmin"])))
        self.roi["ymin"] = max(0, min(height - 1, int(self.roi["ymin"])))
        self.roi["xmax"] = max(self.roi["xmin"] + 10, min(width, int(self.roi["xmax"])))
        self.roi["ymax"] = max(self.roi["ymin"] + 10, min(height, int(self.roi["ymax"])))

    def reset_roi(self):
        """ROIを初期値へ戻す。"""
        self.roi = self.DEFAULT_CONFIG["roi"].copy()
        self.config["roi"] = self.roi.copy()
        self.save_config(self.config)

    def stop(self):
        """終了時にGPIOとカメラを安全に解放する。"""
        try:
            self.output.off()
        except Exception:
            pass

        if self.capture is not None:
            self.capture.release()

        cv2.destroyAllWindows()
        self.logger.info("終了処理が完了しました。")


if __name__ == "__main__":
    system = VehicleGuardSystem()
    try:
        system.run()
    finally:
        system.stop()
