#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
settings.py - 設定管理 (SettingsManager)
"""

import json
import os
import shutil
from pathlib import Path

from .constants import SETTINGS_FILE


class SettingsManager:
    def __init__(self):
        self.defaults = {
            "cameras": [
                {"id": "cam_1", "name": "カメラ 1", "index": 0}
            ],
            "gpio": {
                "outputs": {"ng": 20}
            },
            "inference": {
                "threshold": 0.5,
                "model_path": os.path.join(os.path.expanduser("~"), "models/rubber_best.pt"),
                "mode": "inspection",
                "preview_fps": 10,
                
                "alert_confirm_frames": 3,
                "alert_target_classes": "truck",
                "alert_infer_every_n_frames": 2,
                "roi": {
                    "xmin": 200,
                    "ymin": 120,
                    "xmax": 900,
                    "ymax": 560
                }
            },
            "storage": {
                "results_dir": os.path.join(os.path.expanduser("~"), "results"),
                "auto_delete_enabled": True,
                "max_results_gb": round(shutil.disk_usage(os.path.expanduser("~")).total / (1024**3), 1),
<<<<<<< HEAD
                # 解像度デフォルト値
                # 撮影解像度: Full HD, プレビュー: HD,
                # 検出時保存: VGA, 撮影保存: Full HD
                "capture_res": "1920x1080",   # Full HD
                "preview_res": "1280x720",    # HD
                "res_detect": "640x480",      # VGA
                "res_record": "1920x1080"     # Full HD
            },
            "system": {
                "ng_output_time": 2.0,
                "max_retries": 5,
                "burst_interval": 0.5
=======
                "capture_res": "1920x1080",
                "preview_res": "640x480",
                "res_ok": "320x240",
                "res_ng": "1920x1080",
                "res_skip": "320x240",
                "res_record": "1920x1080"
            },
            "system": {
                "commit_half_step": False,
                "delay_cycles": 0.0
>>>>>>> ac4e2c9439837f386afe67a422cbbd94894f4150
            }
        }
        self.data = self.load_settings()

    def load_settings(self):
        def merge(a, b):
            for k, v in b.items():
                if isinstance(v, dict):
                    a[k] = merge(a.get(k, {}), v)
                else:
                    if k not in a:
                        a[k] = v
            return a

        try:
            if Path(SETTINGS_FILE).exists():
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                    # 互換性: selectors -> pattern_pins
                    if "gpio" in data and "selectors" in data["gpio"] and "pattern_pins" not in data["gpio"]:
                        data["gpio"]["pattern_pins"] = data["gpio"].pop("selectors")
                    
                    # ユーザー名変更対応: /home/pi を現在のユーザーホームに置換
                    home = os.path.expanduser("~")
                    if "inference" in data and "model_path" in data["inference"]:
                        p = data["inference"]["model_path"]
                        if p.startswith("/home/pi/"):
                            data["inference"]["model_path"] = p.replace("/home/pi", home, 1)
                    if "storage" in data and "results_dir" in data["storage"]:
                        p = data["storage"]["results_dir"]
                        if p.startswith("/home/pi/"):
                            data["storage"]["results_dir"] = p.replace("/home/pi", home, 1)

                    # 旧モード名の互換
                    inf = data.get("inference", {})
                    if inf.get("mode") in ("monitoring", None, ""):
                        inf["mode"] = "inspection"

                    return merge(data, self.defaults.copy())
            return self.defaults.copy()
        except Exception as e:
            print(f"Error loading settings: {e}")
            return self.defaults.copy()

    def save_settings(self):
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving settings: {e}")
