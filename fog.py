# -*- coding: utf-8 -*-
"""
Fog of War: одинаковая дальность обзора (config.VISION_RANGE) для ВСЕХ
фигур — специально, чтобы не создавать формального разведывательного
превосходства одной фигуры над другой (тактическая роль разведчика у
коня — за счёт его мобильности, а не особого радиуса обзора).

Правила видимости:
- игрок всегда видит свои фигуры;
- вражеский король виден всегда (даже в тумане);
- остальные вражеские фигуры видны, только если находятся в пределах
  VISION_RANGE (дистанция по Чебышеву) от какой-либо своей фигуры.

ВАЖНО (не допустить AI-cheating): при расчёте хода ИИ используется не
исходный GameState, а "туманная" копия (build_fogged_view), из которой
физически удалены невидимые для ИИ фигуры игрока. Поэтому ни поиск
(minimax), ни LLM-подсказки не могут сослаться на то, чего ИИ не должен
знать — не нужно отдельно "прятать" знания внутри algorithm/LLM кода.
"""
import config


def visible_enemy_ids(state, viewer_color):
    """Возвращает множество id вражеских фигур, видимых стороне
    viewer_color. Если туман войны выключен — возвращает id всех живых
    вражеских фигур (т.е. эффект отсутствует)."""
    opp = "black" if viewer_color == "white" else "white"
    if not getattr(state, "fog_enabled", False):
        return {p.id for p in state.pieces.values() if p.alive() and p.color == opp}

    visible = set()
    friendly = [p for p in state.pieces_of(viewer_color) if p.alive()]
    for e in state.pieces.values():
        if not e.alive() or e.color != opp:
            continue
        if e.type == "king":
            visible.add(e.id)
            continue
        for f in friendly:
            if max(abs(e.col - f.col), abs(e.row - f.row)) <= config.VISION_RANGE:
                visible.add(e.id)
                break
    return visible


def visible_cells(state, viewer_color):
    """Возвращает множество клеток (col,row), видимых стороне
    viewer_color — все клетки в радиусе VISION_RANGE от любой её живой
    фигуры. Если туман выключен — возвращает None (рисовать облака не
    нужно, вызывающий код должен просто пропустить их)."""
    if not getattr(state, "fog_enabled", False):
        return None
    size = getattr(state, "board_size", (config.BOARD_WIDTH, config.BOARD_HEIGHT))
    if isinstance(size, (tuple, list)):
        width, height = int(size[0]), int(size[1])
    else:
        width = height = int(size)
    cells = set()
    for p in state.pieces_of(viewer_color):
        if not p.alive():
            continue
        for dc in range(-config.VISION_RANGE, config.VISION_RANGE + 1):
            for dr in range(-config.VISION_RANGE, config.VISION_RANGE + 1):
                c, r = p.col + dc, p.row + dr
                if 0 <= c < width and 0 <= r < height:
                    cells.add((c, r))
    return cells


def build_fogged_view(state, viewer_color):
    """
    Возвращает облегчённую копию state, из которой физически удалены
    вражеские фигуры, невидимые стороне viewer_color. Используется ТОЛЬКО
    для планирования хода ИИ (ai_interface.py / main.py), чтобы движок
    поиска и LLM не могли ссылаться на скрытую информацию — честный
    туман войны без AI-cheating.
    """
    ws = state.clone_light()
    if not getattr(state, "fog_enabled", False):
        return ws
    visible = visible_enemy_ids(state, viewer_color)
    opp = "black" if viewer_color == "white" else "white"
    to_remove = [pid for pid, p in ws.pieces.items() if p.color == opp and pid not in visible]
    for pid in to_remove:
        del ws.pieces[pid]
    # Если у оставшихся видимых фигур mounted_knight_id указывает на
    # удалённого (невидимого) коня — обнуляем ссылку, чтобы не путать
    # рассчитывающий ход код несуществующим id.
    for p in ws.pieces.values():
        if p.mounted_knight_id is not None and p.mounted_knight_id not in ws.pieces:
            p.mounted_knight_id = None
    return ws
