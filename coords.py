# -*- coding: utf-8 -*-
"""
Отдельное преобразование координат "игровая доска -> экран".

ВАЖНО: внутренняя логика игры (GameState, rules, combat, ai) всегда
работает в "логических" координатах доски, где row=0 — первая линия
белых (WHITE_HOME_ROW), а row=7 — первая линия чёрных (BLACK_HOME_ROW).
Эти координаты НИКОГДА не меняются ради отображения.

Игрок всегда играет за белых (config.PLAYER_COLOR) и должен видеть свои
фигуры внизу экрана, а фигуры ИИ (чёрные) — наверху, как будто сидит за
своей стороной доски. Поэтому здесь переворачивается только визуальный
ряд (row), а логика игры об этом не знает.
"""
import config


def _screen_col(col):
    # Белые видят доску слева-направо как обычно. Чёрные получают
    # классическую развёрнутую перспективу: вся доска поворачивается на 180°.
    if config.VIEWER_COLOR == "black":
        return config.BOARD_WIDTH - 1 - col
    return col


def _screen_row(row):
    # Белые: свои домашние ряды внизу. Чёрные: свои домашние ряды внизу.
    if config.VIEWER_COLOR == "black":
        return row
    return config.BOARD_HEIGHT - 1 - row


def board_to_screen(col, row):
    """Логические координаты клетки (col, row) -> экранные пиксели левого
    верхнего угла клетки."""
    screen_col = _screen_col(col)
    screen_row = _screen_row(row)
    x = config.BOARD_MARGIN_X + screen_col * config.CELL_SIZE
    y = config.BOARD_MARGIN_Y + screen_row * config.CELL_SIZE
    return x, y


def board_to_screen_center(col, row):
    x, y = board_to_screen(col, row)
    return x + config.CELL_SIZE // 2, y + config.CELL_SIZE // 2


def screen_to_board(x, y):
    """Экранные пиксели -> логические координаты клетки (col, row), либо
    None, если клик был вне доски."""
    screen_col = (x - config.BOARD_MARGIN_X) // config.CELL_SIZE
    screen_row = (y - config.BOARD_MARGIN_Y) // config.CELL_SIZE
    if 0 <= screen_col < config.BOARD_WIDTH and 0 <= screen_row < config.BOARD_HEIGHT:
        screen_col = int(screen_col)
        screen_row = int(screen_row)
        col = config.BOARD_WIDTH - 1 - screen_col if config.VIEWER_COLOR == "black" else screen_col
        row = screen_row if config.VIEWER_COLOR == "black" else config.BOARD_HEIGHT - 1 - screen_row
        return col, row
    return None


def screen_to_board_unclamped(x, y):
    """Как screen_to_board, но не отбрасывает результат, если клетка вне
    доски (нужно во время drag&drop, когда курсор может выйти за край)."""
    screen_col = (x - config.BOARD_MARGIN_X) / config.CELL_SIZE
    screen_row = (y - config.BOARD_MARGIN_Y) / config.CELL_SIZE
    screen_col = round(screen_col)
    screen_row = round(screen_row)
    col = config.BOARD_WIDTH - 1 - screen_col if config.VIEWER_COLOR == "black" else screen_col
    row = screen_row if config.VIEWER_COLOR == "black" else config.BOARD_HEIGHT - 1 - screen_row
    return col, row
