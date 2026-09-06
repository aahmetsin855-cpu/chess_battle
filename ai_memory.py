# -*- coding: utf-8 -*-
"""
Компактная память ИИ о прошлых партиях: data/ai_memory.json.

Хранит только небольшую статистику (не полные логи ходов), чтобы:
- ИИ мог опираться на общий опыт (например, какие открытия чаще
  приводили к победе);
- НЕ превращаться в нечестный чит — здесь нет информации о конкретных
  будущих ходах игрока, только агрегированная статистика прошлых
  завершившихся партий.

Отдельно, в data/games/, можно сохранять краткие записи каждой партии
(итог, число ходов, кто победил) — это тоже компактно (несколько строк
JSON на партию), не тяжёлые логи.
"""
import json
import os
import time

import config


def _ensure_dirs():
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        os.makedirs(config.GAMES_DIR, exist_ok=True)
    except Exception:
        pass


def default_memory():
    return {
        "games_played": 0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "opening_preferences": {},   # тип первой активированной фигуры -> счётчик
        "common_tactics": {},        # напр. "mounted_knight_used" -> счётчик
        "mistakes": {},              # напр. "king_exposed_early" -> счётчик
    }


def load_memory():
    _ensure_dirs()
    if not os.path.exists(config.AI_MEMORY_FILE):
        return default_memory()
    try:
        with open(config.AI_MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = default_memory()
        if isinstance(data, dict):
            merged.update(data)
        return merged
    except Exception:
        return default_memory()


def save_memory(memory):
    _ensure_dirs()
    try:
        with open(config.AI_MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def record_game_result(memory, winner, ai_color="black", opening_piece_type=None,
                        used_mount=False, turns=0):
    """Обновляет память после завершения партии. Возвращает обновлённый dict."""
    memory = memory or default_memory()
    memory["games_played"] = memory.get("games_played", 0) + 1

    if winner == ai_color:
        memory["wins"] = memory.get("wins", 0) + 1
    elif winner in ("white", "black"):
        memory["losses"] = memory.get("losses", 0) + 1
    else:
        memory["draws"] = memory.get("draws", 0) + 1

    if opening_piece_type:
        prefs = memory.setdefault("opening_preferences", {})
        prefs[opening_piece_type] = prefs.get(opening_piece_type, 0) + 1

    if used_mount:
        tactics = memory.setdefault("common_tactics", {})
        tactics["mounted_knight_used"] = tactics.get("mounted_knight_used", 0) + 1

    return memory


def append_game_log(winner, ai_color, turns, difficulty, ai_type):
    """Сохраняет краткую запись об одной партии в data/games/ (не тяжёлый лог)."""
    _ensure_dirs()
    entry = {
        "timestamp": time.time(),
        "winner": winner,
        "ai_color": ai_color,
        "turns": turns,
        "difficulty": difficulty,
        "ai_type": ai_type,
    }
    try:
        fname = os.path.join(config.GAMES_DIR, f"game_{int(entry['timestamp'])}.json")
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def opening_bias(memory, color):
    """
    Возвращает dict {piece_type: bonus} — небольшую подсказку ИИ на основе
    исторически чаще выбираемых (и не обязательно выигрышных, а просто
    предпочитаемых) первых фигур. Используется как маленький бонус в
    оценке позиции в самом начале партии — НЕ читерство, т.к. это не
    знание о текущем сопернике, а лишь статистика собственного стиля ИИ.
    """
    prefs = memory.get("opening_preferences", {}) if memory else {}
    if not prefs:
        return {}
    total = sum(prefs.values()) or 1
    return {t: (cnt / total) * 1.5 for t, cnt in prefs.items()}
