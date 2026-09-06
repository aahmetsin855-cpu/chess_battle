# -*- coding: utf-8 -*-
"""
Единая точка получения списка легальных действий фигуры и их применения.
Используется как игроком (через UI), так и ИИ (через поиск) — так правила
существуют только в одном месте.

Формат действия (dict):
  {'piece_id': int, 'type': 'move', 'target': (col, row)}
  {'piece_id': int, 'type': 'attack', 'target_id': int, 'mode': 'basic'|'bishop'|'queen_ranged'}
  {'piece_id': int, 'type': 'mount', 'target_id': int}
  {'piece_id': int, 'type': 'dismount', 'target': (col, row)}   # piece_id = КОНЬ, не носитель
  {'piece_id': int, 'type': 'heal', 'target_id': int}            # только король
  {'piece_id': int, 'type': 'queen_lock', 'mode': 'queen_ranged'}
  {'piece_id': int, 'type': 'queen_unlock'}
  {'type': 'pass'}   # используется только в поиске ИИ

Ферзь:
  - MOVE — обычное перемещение.
  - RANGED — отдельный режим-турель (единственный режим атаки ферзя,
    SPLASH убран). Его включение ('queen_lock') само
    по себе занимает действие. Пока RANGED активен, ферзь не может
    двигаться и может только выполнить дальнюю атаку или снять режим
    отдельным действием ('queen_unlock'). Режим НЕ снимается сам по
    себе после выстрела — ферзь остаётся турелью и может стрелять
    дальше, пока игрок явно не выполнит 'queen_unlock'.
"""
import rules


def get_legal_actions(state, piece):
    if piece is None or not piece.alive() or piece.is_mounted_knight:
        return []

    actions = []

    # Спешивание коня — тратит действие САМОГО КОНЯ, а не носителя,
    # поэтому доступно независимо от того, остались ли действия у
    # носителя. Проверяется для любой фигуры, на которой сидит конь.
    if piece.mounted_knight_id is not None:
        knight = state.pieces.get(piece.mounted_knight_id)
        if knight is not None and knight.alive() and knight.actions_available() > 0:
            for c, r in rules.get_dismount_targets(state, piece):
                actions.append({"piece_id": knight.id, "type": "dismount", "target": (c, r)})

    if piece.actions_available() <= 0:
        return actions

    if piece.type == "rook":
        for t in rules.get_rook_swap_targets(state, piece):
            actions.append({"piece_id": piece.id, "type": "rook_swap", "target_id": t.id})
        for c, r in rules.get_move_cells(state, piece):
            actions.append({"piece_id": piece.id, "type": "move", "target": (c, r)})
        for t in rules.get_basic_attack_targets(state, piece):
            actions.append({"piece_id": piece.id, "type": "attack", "target_id": t.id, "mode": "basic"})
        return actions

    if piece.type == "knight":
        for c, r in rules.get_move_cells(state, piece):
            actions.append({"piece_id": piece.id, "type": "move", "target": (c, r)})
        for t in rules.get_mount_targets(state, piece):
            actions.append({"piece_id": piece.id, "type": "mount", "target_id": t.id})
        return actions

    if piece.type == "queen":
        if piece.queen_locked_mode is None:
            for c, r in rules.get_move_cells(state, piece):
                actions.append({"piece_id": piece.id, "type": "move", "target": (c, r)})

            actions.append({"piece_id": piece.id, "type": "queen_lock", "mode": "queen_ranged"})
        else:
            # Единственный зафиксированный режим — RANGED.
            for t in rules.get_queen_ranged_targets(state, piece):
                actions.append({
                    "piece_id": piece.id,
                    "type": "attack",
                    "target_id": t.id,
                    "mode": "queen_ranged",
                })
            actions.append({"piece_id": piece.id, "type": "queen_unlock"})
        return actions

    for c, r in rules.get_move_cells(state, piece):
        actions.append({"piece_id": piece.id, "type": "move", "target": (c, r)})

    if piece.type in ("pawn", "king", "rook"):
        for t in rules.get_basic_attack_targets(state, piece):
            actions.append({"piece_id": piece.id, "type": "attack", "target_id": t.id, "mode": "basic"})
    elif piece.type == "bishop":
        for t in rules.get_bishop_attack_targets(state, piece):
            actions.append({"piece_id": piece.id, "type": "attack", "target_id": t.id, "mode": "bishop"})

    if piece.type == "king":
        # Король как поддерживающий юнит: может вылечить соседнюю
        # союзную повреждённую фигуру вместо хода/атаки (см. combat.do_king_heal).
        for t in rules.get_king_heal_targets(state, piece):
            actions.append({"piece_id": piece.id, "type": "heal", "target_id": t.id})

    return actions


def get_all_legal_actions(state, color):
    pieces = state.pieces_of(color, selectable_only=True)
    if not pieces:
        return []
    # Индекс клеток строится один раз на весь вызов и подменяет
    # state.get_piece_at на O(1)-версию по словарю. Это безопасно: за
    # время построения списка легальных действий состояние НЕ
    # мутируется (весь rules.py тут — чистое чтение), значит индекс не
    # может устареть; подмена — на КОНКРЕТНОМ экземпляре state, а не на
    # классе, поэтому клонированные копии (см. GameState.clone_light)
    # её не наследуют и не задеты. Оригинальный метод восстанавливается
    # в finally даже при исключении.
    original_get_piece_at = state.get_piece_at
    index = {}
    for p in state.pieces.values():
        if p.alive() and not p.is_mounted_knight:
            index[(p.col, p.row)] = p
    state.get_piece_at = lambda c, r, _idx=index: _idx.get((c, r))
    try:
        actions = []
        for p in pieces:
            actions.extend(get_legal_actions(state, p))
        return actions
    finally:
        state.get_piece_at = original_get_piece_at


def _record_action(state, action):
    """Сохраняет только компактное описание последних действий для UI/Local AI."""
    if action.get("type") == "pass":
        return
    entry = dict(action)
    # tuples -> lists для безопасного хранения/копирования
    if isinstance(entry.get("target"), tuple):
        entry["target"] = list(entry["target"])
    state.action_history = (getattr(state, "action_history", []) + [entry])[-12:]


def apply_action(state, action):
    """Применяет действие к состоянию. Возвращает True при успехе."""
    import combat  # локальный импорт, чтобы избежать циклической зависимости

    if action.get("type") == "pass":
        return True

    piece = state.pieces.get(action.get("piece_id"))
    if piece is None or not piece.alive():
        return False

    kind = action.get("type")
    before_alive = {"white": sum(1 for p in state.pieces.values() if p.color == "white" and p.alive()),
                    "black": sum(1 for p in state.pieces.values() if p.color == "black" and p.alive())}

    def finish(ok=True):
        if not ok:
            return False
        after_alive = {"white": sum(1 for p in state.pieces.values() if p.color == "white" and p.alive()),
                       "black": sum(1 for p in state.pieces.values() if p.color == "black" and p.alive())}
        state.captured[piece.color] = state.captured.get(piece.color, 0) + max(0, before_alive["black" if piece.color == "white" else "white"] - after_alive["black" if piece.color == "white" else "white"])
        return True

    if kind == "move":
        combat.do_move(state, piece, action["target"])
        _record_action(state, action)
        return finish(True)

    if kind == "attack":
        target = state.pieces.get(action.get("target_id"))
        if target is None or not target.alive():
            return False
        mode = action.get("mode")
        if mode == "basic":
            combat.do_basic_attack(state, piece, target)
        elif mode == "bishop":
            combat.do_bishop_attack(state, piece, target)
        elif mode == "queen_ranged":
            combat.do_queen_ranged(state, piece, target)
        else:
            return False
        _record_action(state, action)
        return finish(True)

    if kind == "rook_swap":
        target = state.pieces.get(action.get("target_id"))
        if target is None or not target.alive() or target.color != piece.color or piece.type != "rook":
            return False
        if target.id == piece.id:
            return False
        if target.is_mounted_knight:
            return False
        if (abs(target.col - piece.col), abs(target.row - piece.row)) != (1, 1) and \
           max(abs(target.col - piece.col), abs(target.row - piece.row)) != 1:
            return False
        # The formal adjacency/legality check is kept in rules as well.
        if target not in rules.get_rook_swap_targets(state, piece):
            return False
        combat.do_rook_swap(state, piece, target)
        _record_action(state, action)
        return finish(True)

    if kind == "mount":
        host = state.pieces.get(action.get("target_id"))
        if host is None or not host.alive():
            return False
        combat.do_mount(state, piece, host)
        _record_action(state, action)
        return finish(True)

    if kind == "dismount":
        combat.do_dismount(state, piece, action["target"])
        _record_action(state, action)
        return finish(True)

    if kind == "heal":
        target = state.pieces.get(action.get("target_id"))
        if target is None or not target.alive():
            return False
        combat.do_king_heal(state, piece, target)
        _record_action(state, action)
        return finish(True)

    if kind == "queen_lock":
        combat.do_queen_lock(state, piece, action["mode"])
        _record_action(state, action)
        return finish(True)

    if kind == "queen_unlock":
        combat.do_queen_unlock(state, piece)
        _record_action(state, action)
        return finish(True)

    return False

