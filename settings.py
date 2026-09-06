# -*- coding: utf-8 -*-
"""
Локальные настройки игры: тип ИИ, сложность, скорость анимаций,
включена ли память ИИ и т.д. Хранится в data/settings.json.

Не должно роняться из-за отсутствия файла/директории/повреждённого JSON —
в этом случае просто используются значения по умолчанию.
"""
import json
import os

import config

DEFAULTS = {
    "ai_type": "algorithm",       # "algorithm" | "local"
    "difficulty": "normal",       # "easy" | "normal" | "hard"
    "animation_speed": 1.0,
    "memory_enabled": True,
    "board_size": (10, 10),       # default; available maps are 10x8, 12x8, 14x10
    "game_mode": "standard",      # "standard" | "flag" | "base" | "fog"
    "fullscreen": True,
    "music_path": "",
    "background_path": "",
    "music_volume": 0.7,
}


def _ensure_dir():
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
    except Exception:
        pass


def load_settings():
    _ensure_dir()
    if not os.path.exists(config.SETTINGS_FILE):
        return dict(DEFAULTS)
    try:
        with open(config.SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULTS)
        if isinstance(data, dict):
            merged.update(data)
        return merged
    except Exception:
        return dict(DEFAULTS)


def save_settings(settings):
    _ensure_dir()
    try:
        with open(config.SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
