# -*- coding: utf-8 -*-
"""
GameState — центральное хранилище состояния партии:
фигуры, чей ход, фаза хода, таймеры, результат.

Не зависит от Pygame.
"""
import config
from pieces import Piece


class GameState:
    # Фазы:
    #   "setup_white"  - расстановка игрока (белые)
    #   "setup_black"  - расстановка ИИ (обычно мгновенная)
    #   "king_stage"   - стадия 1 текущего хода (только король)
    #   "lobby_stage"  - стадия 2 текущего хода (любые фигуры)
    #   "game_over"    - игра окончена

    def __init__(self):
        self.pieces = {}          # id -> Piece
        self.turn_color = "white"
        self.phase = "setup_white"
        self.king_stage_timer = config.KING_ACTION_TIME
        self.main_timer = config.MAIN_TURN_TIME
        self.game_timer = config.STANDARD_GAME_TIME
        self.captured = {"white": 0, "black": 0}
        self.turn_number = 1
        self.winner = None
        self.last_event = ""
        # Короткая история последних действий для Local AI (не сохраняет полные партии).
        self.action_history = []

        # --- Игровой режим (см. config.GAME_MODES) ---
        self.game_mode = config.DEFAULT_GAME_MODE
        self.board_size = (config.BOARD_WIDTH, config.BOARD_HEIGHT)
        # Base/Frontline: скомпрометирован ли тыл каждой стороны (см. combat.py)
        self.base_compromised = {"white": False, "black": False}
        # Fog of War: включена ли ограниченная видимость для этой партии
        self.fog_enabled = False

    # ------------------------------------------------------------------
    # Управление фигурами
    # ------------------------------------------------------------------
    def add_piece(self, ptype, color, col, row):
        stats = config.PIECE_STATS[ptype]
        p = Piece(ptype, color, stats["hp"], stats["hp"], stats["damage"], col, row)
        self.pieces[p.id] = p
        return p

    def remove_piece(self, pid):
        if pid in self.pieces:
            del self.pieces[pid]

    def get_piece_at(self, col, row):
        """Возвращает видимую (не 'спрятанного' коня) фигуру в клетке или None."""
        for p in self.pieces.values():
            if p.alive() and not p.is_mounted_knight and p.col == col and p.row == row:
                return p
        return None

    def pieces_of(self, color, selectable_only=False):
        result = []
        for p in self.pieces.values():
            if p.color == color and p.alive():
                if selectable_only and p.is_mounted_knight:
                    continue
                result.append(p)
        return result

    def reset_turn_flags(self, color):
        for p in self.pieces_of(color):
            p.actions_used = 0

    # ------------------------------------------------------------------
    # Победа
    # ------------------------------------------------------------------
    def check_victory(self):
        """
        Возвращает 'white', 'black', 'draw' или None (игра продолжается).

        - "standard" / "base" / "fog": у противника не осталось живых фигур.
        - "flag": любая фигура добралась до вражеской домашней линии —
          немедленная победа (не нужно уничтожать всю армию).
        """
        if self.game_mode in ("base", "fog"):
            if not self.king_alive("white"):
                return "black"
            if not self.king_alive("black"):
                return "white"

        if self.game_mode == "flag":
            for p in self.pieces.values():
                if not p.alive() or p.is_mounted_knight:
                    continue
                if p.color == "white" and p.row == config.BLACK_HOME_ROW:
                    return "white"
                if p.color == "black" and p.row == config.WHITE_HOME_ROW:
                    return "black"

        white_has = any(p.color == "white" and p.alive() for p in self.pieces.values())
        black_has = any(p.color == "black" and p.alive() for p in self.pieces.values())
        if self.game_mode == "standard":
            # Standard Battle is decided by the total match clock, not by
            # wiping the army. A completely wiped army still wins instantly.
            if not white_has and not black_has:
                return "draw"
            if not black_has:
                return "white"
            if not white_has:
                return "black"
        else:
            if not white_has and not black_has:
                return "draw"
            if not black_has:
                return "white"
            if not white_has:
                return "black"
        return None

    def king_alive(self, color):
        return any(p.color == color and p.type == "king" and p.alive() for p in self.pieces.values())

    # ------------------------------------------------------------------
    # Клонирование (для поиска ИИ) — облегчённое, без Pygame-объектов
    # ------------------------------------------------------------------
    def clone_light(self):
        gs = GameState()
        gs.turn_color = self.turn_color
        gs.phase = self.phase
        gs.turn_number = self.turn_number
        gs.winner = self.winner
        gs.game_mode = self.game_mode
        gs.board_size = self.board_size
        gs.base_compromised = dict(self.base_compromised)
        gs.fog_enabled = self.fog_enabled
        gs.action_history = list(self.action_history[-12:])
        gs.game_timer = self.game_timer
        gs.captured = dict(self.captured)
        for pid, p in self.pieces.items():
            gs.pieces[pid] = p.clone()
        return gs
