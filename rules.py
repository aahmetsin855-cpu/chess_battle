# -*- coding: utf-8 -*-
"""
Правила движения / атаки / способностей.

ВАЖНО: это НЕ классические шахматные правила.
- Все фигуры (кроме коня) двигаются на 1 клетку в любую из 8 сторон.
- Конь двигается на 2 клетки в любую из 8 сторон (включая диагонали) —
  не буквой "Г".
- Атака у пешки/короля/ладьи — на 1 клетку, во все 8 сторон.
- Слон атакует область 5x5 (дистанция по Чебышеву <= 2).
- Ферзь: обычное движение или дальняя атака (5x5, режим-турель).
"""
import config

DIRS8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def in_bounds(col, row):
    return 0 <= col < config.BOARD_WIDTH and 0 <= row < config.BOARD_HEIGHT


def cells_in_radius(col, row, radius):
    """Все клетки в квадрате (2*radius+1) x (2*radius+1) вокруг (col,row), кроме центра."""
    cells = []
    for dc in range(-radius, radius + 1):
        for dr in range(-radius, radius + 1):
            if dc == 0 and dr == 0:
                continue
            c, r = col + dc, row + dr
            if in_bounds(c, r):
                cells.append((c, r))
    return cells


def _line_cells(c0, r0, c1, r1):
    """Промежуточные клетки строго МЕЖДУ (c0,r0) и (c1,r1) (без концов).
    Используется для проверки линии огня слона/дальней атаки ферзя —
    любая фигура на этой линии блокирует атаку (см. get_bishop_attack_targets)."""
    dc = c1 - c0
    dr = r1 - r0
    steps = max(abs(dc), abs(dr))
    if steps <= 1:
        return []
    cells = []
    for i in range(1, steps):
        t = i / steps
        c = round(c0 + dc * t)
        r = round(r0 + dr * t)
        if (c, r) != (c0, r0) and (c, r) != (c1, r1):
            if not cells or cells[-1] != (c, r):
                cells.append((c, r))
    return cells


def has_line_of_sight(state, from_col, from_row, to_col, to_row, shooter_color=None):
    """Проверка линии огня.

    Специальное правило HP Battle Chess: СОЮЗНАЯ ПЕШКА стрелку не
    блокирует. Любая другая фигура — в том числе вражеская пешка, союзная
    сильная фигура или чужой юнит — блокирует линию.
    """
    if shooter_color is None:
        p = state.get_piece_at(from_col, from_row)
        shooter_color = getattr(p, "color", None)
    for c, r in _line_cells(from_col, from_row, to_col, to_row):
        blocker = state.get_piece_at(c, r)
        if blocker is None:
            continue
        if blocker.color == shooter_color and blocker.type == "pawn":
            continue
        return False
    return True


def get_move_cells(state, piece):
    """Клетки, куда фигура может переместиться (без учёта занятия хода)."""
    cells = []
    if piece.type == "knight":
        # Custom HPBC knight: exactly two cells in one of 8 directions,
        # with the intermediate cell required to be empty.
        for dx, dy in DIRS8:
            mid_c, mid_r = piece.col + dx, piece.row + dy
            c, r = piece.col + 2 * dx, piece.row + 2 * dy
            if (in_bounds(mid_c, mid_r) and in_bounds(c, r)
                    and state.get_piece_at(mid_c, mid_r) is None
                    and state.get_piece_at(c, r) is None):
                cells.append((c, r))
    else:
        for dx, dy in DIRS8:
            c, r = piece.col + dx, piece.row + dy
            if in_bounds(c, r) and state.get_piece_at(c, r) is None:
                cells.append((c, r))
    return cells


def get_rook_swap_targets(state, rook):
    """Ладья может потратить действие и поменяться местами с любой
    соседней живой союзной фигурой, которая не является скрытым
    спешившимся конём. Сама цель своего действия не расходует."""
    targets = []
    if rook.type != "rook" or rook.is_mounted_knight:
        return targets
    for dx, dy in DIRS8:
        c, r = rook.col + dx, rook.row + dy
        if not in_bounds(c, r):
            continue
        target = state.get_piece_at(c, r)
        if target is not None and target.color == rook.color and target.id != rook.id:
            targets.append(target)
    return targets


def get_basic_attack_targets(state, piece):
    """Для пешки / короля / ладьи: враг на соседней клетке (8 направлений)."""
    targets = []
    for dx, dy in DIRS8:
        c, r = piece.col + dx, piece.row + dy
        if in_bounds(c, r):
            t = state.get_piece_at(c, r)
            if t is not None and t.color != piece.color:
                targets.append(t)
    return targets


def get_bishop_attack_targets(state, piece):
    """Слон: любая вражеская фигура в области 5x5 (дистанция <= 2), но
    ТОЛЬКО если линия огня не перекрыта другой фигурой (см. модуль
    BISHOP_LINE_OF_SIGHT в config.py). Bishop -> empty -> enemy можно;
    Bishop -> empty -> pawn -> enemy нельзя."""
    targets = []
    for c, r in cells_in_radius(piece.col, piece.row, config.BISHOP_RANGE):
        t = state.get_piece_at(c, r)
        if t is None or t.color == piece.color:
            continue
        if config.BISHOP_LINE_OF_SIGHT and not has_line_of_sight(state, piece.col, piece.row, c, r, piece.color):
            continue
        targets.append(t)
    return targets


def get_queen_ranged_targets(state, piece):
    """Ферзь: дальняя атака, область 5x5, как у слона (та же проверка
    линии огня), но другой урон."""
    targets = []
    for c, r in cells_in_radius(piece.col, piece.row, config.QUEEN_RANGED_RANGE):
        t = state.get_piece_at(c, r)
        if t is None or t.color == piece.color:
            continue
        if config.BISHOP_LINE_OF_SIGHT and not has_line_of_sight(state, piece.col, piece.row, c, r, piece.color):
            continue
        targets.append(t)
    return targets


def get_mount_targets(state, piece):
    """Конь: соседние дружественные фигуры без коня, на которые можно 'сесть'."""
    targets = []
    if piece.type != "knight" or piece.is_mounted_knight:
        return targets
    for dx, dy in DIRS8:
        c, r = piece.col + dx, piece.row + dy
        if in_bounds(c, r):
            t = state.get_piece_at(c, r)
            if (t is not None and t.color == piece.color and t.type != "knight"
                    and t.mounted_knight_id is None):
                targets.append(t)
    return targets


def get_dismount_targets(state, host):
    """Клетки, куда может 'спешиться' конь, сидящий на host (пустые
    соседние клетки). Используется когда игрок хочет разделить связку
    конь+носитель — конь тратит на это своё собственное действие."""
    if host.mounted_knight_id is None:
        return []
    cells = []
    for dx, dy in DIRS8:
        c, r = host.col + dx, host.row + dy
        if in_bounds(c, r) and state.get_piece_at(c, r) is None:
            cells.append((c, r))
    return cells


def get_king_heal_targets(state, king):
    """Король: соседняя (8 направлений) дружественная повреждённая фигура,
    которую можно вылечить действием короля (см. combat.do_king_heal)."""
    targets = []
    if king.type != "king":
        return targets
    for dx, dy in DIRS8:
        c, r = king.col + dx, king.row + dy
        if in_bounds(c, r):
            t = state.get_piece_at(c, r)
            if (t is not None and t.color == king.color and t.id != king.id
                    and not t.is_mounted_knight and t.hp < t.max_hp):
                targets.append(t)
    return targets


def in_own_half(color, col, row):
    if color == "white":
        lo, hi = config.WHITE_HALF_ROWS
    else:
        lo, hi = config.BLACK_HALF_ROWS
    return lo <= row <= hi and 0 <= col < config.BOARD_SIZE
