# -*- coding: utf-8 -*-
"""
Класс фигуры. Не зависит от Pygame — чистая логика, чтобы её можно было
тестировать и использовать в поиске ИИ без графики.
"""
import itertools

_id_counter = itertools.count(1)


class Piece:
    __slots__ = (
        "id", "type", "color", "hp", "max_hp", "damage",
        "col", "row", "actions_used",
        "mounted_knight_id",  # id коня, "сидящего" на этой фигуре (если есть)
        "is_mounted_knight",  # True, если ЭТА фигура — конь, сидящий на другой
        "host_id",            # id фигуры-носителя, если is_mounted_knight
        "queen_locked_mode",  # None | "queen_ranged" — см. ниже
    )

    def __init__(self, ptype, color, hp, max_hp, damage, col, row, _id=None):
        self.id = _id if _id is not None else next(_id_counter)
        self.type = ptype
        self.color = color
        self.hp = hp
        self.max_hp = max_hp
        self.damage = damage
        self.col = col
        self.row = row
        self.actions_used = 0
        self.mounted_knight_id = None
        self.is_mounted_knight = False
        self.host_id = None
        # Ферзь: если не None, он зафиксирован в режиме RANGED — это
        # единственный вид атаки ферзя (SPLASH убран).
        self.queen_locked_mode = None

    def alive(self):
        return self.hp > 0

    def max_actions(self):
        """Обычно 1 действие за ход. Если на фигуре сидит конь — 2."""
        return 2 if self.mounted_knight_id else 1

    def actions_available(self):
        return self.max_actions() - self.actions_used

    def clone(self):
        p = Piece(
            self.type, self.color, self.hp, self.max_hp, self.damage,
            self.col, self.row, _id=self.id,
        )
        p.actions_used = self.actions_used
        p.mounted_knight_id = self.mounted_knight_id
        p.is_mounted_knight = self.is_mounted_knight
        p.host_id = self.host_id
        p.queen_locked_mode = self.queen_locked_mode
        return p

    def __repr__(self):
        return f"<Piece {self.color} {self.type} hp={self.hp}/{self.max_hp} ({self.col},{self.row})>"
