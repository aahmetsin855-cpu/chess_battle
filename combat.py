# -*- coding: utf-8 -*-
"""
Исполнение боевых и вспомогательных действий: нанесение урона, уничтожение
фигур, посадка коня, лечение на домашней линии.

Правило коня как "щита": пока конь жив и "сидит" на фигуре, весь урон,
адресованный этой фигуре, получает конь. Как только у коня HP <= 0,
конь уничтожается, а усиление исчезает. Хозяин в этой же атаке
дополнительный урон не получает (конь полностью поглощает урон атаки).
"""
import config
import rules


def apply_damage(state, target, dmg):
    """Наносит урон цели с учётом того, что конь-щит поглощает урон первым."""
    if target is None or not target.alive():
        return
    if target.mounted_knight_id is not None:
        knight = state.pieces.get(target.mounted_knight_id)
        if knight is not None and knight.alive():
            knight.hp -= dmg
            if knight.hp <= 0:
                destroy_piece(state, knight)
                target.mounted_knight_id = None
            return
        else:
            target.mounted_knight_id = None
    target.hp -= dmg
    if target.hp <= 0:
        destroy_piece(state, target)


def destroy_piece(state, piece):
    piece.hp = 0
    # если уничтожаемая фигура была носителем коня — конь теряет наездника
    for p in state.pieces.values():
        if p.id != piece.id and p.mounted_knight_id == piece.id:
            p.mounted_knight_id = None
    state.remove_piece(piece.id)


def do_move(state, piece, dest):
    piece.col, piece.row = dest
    piece.actions_used += 1


def do_rook_swap(state, rook, target):
    """Меняет местами ладью и соседнюю союзную фигуру. Тратится
    одно действие ладьи; действия и статус цели не расходуются.
    Если у одной из фигур есть конь-щит, конь следует за своим хозяином."""
    old_r = (rook.col, rook.row)
    old_t = (target.col, target.row)
    rook.col, rook.row = old_t
    target.col, target.row = old_r
    if rook.mounted_knight_id is not None:
        knight = state.pieces.get(rook.mounted_knight_id)
        if knight is not None and knight.alive():
            knight.col, knight.row = rook.col, rook.row
    if target.mounted_knight_id is not None:
        knight = state.pieces.get(target.mounted_knight_id)
        if knight is not None and knight.alive():
            knight.col, knight.row = target.col, target.row
    rook.actions_used += 1


def do_basic_attack(state, piece, target):
    apply_damage(state, target, piece.damage)
    piece.actions_used += 1


def do_bishop_attack(state, piece, target):
    apply_damage(state, target, piece.damage)
    piece.actions_used += 1


def do_queen_ranged(state, piece, target):
    apply_damage(state, target, config.QUEEN_RANGED_DAMAGE)
    piece.actions_used += 1
    # ВАЖНО: раньше здесь стояло piece.queen_locked_mode = None — режим
    # снимался автоматически сразу после каждой атаки. Это и был баг:
    # ферзь "забывал" состояние турели после выстрела вне зависимости от
    # того, стоит на нём конь или нет (конь тут был ни при чём, просто
    # чаще всплывало в связке ферзь+конь, где на атаку уходило второе,
    # бонусное действие). Теперь блокировка режима — состояние, которое
    # держится, пока игрок явно не снимет его действием queen_unlock:
    # ферзь остаётся турелью и может стрелять дальше без повторной
    # фиксации режима каждый раз.


def do_queen_lock(state, piece, mode):
    """Ферзь фиксируется только в RANGED-режиме — это единственный
    режим и единственный вид атаки ферзя (SPLASH убран)."""
    if mode != "queen_ranged":
        return
    piece.queen_locked_mode = "queen_ranged"
    piece.actions_used += 1


def do_queen_unlock(state, piece):
    """Отменяет режим дальнего боя без атаки (ферзь снова мобилен со
    следующего действия). Тоже стоит действие — уход из турели не бесплатен."""
    piece.queen_locked_mode = None
    piece.actions_used += 1


def do_dismount(state, knight, dest):
    """Конь слезает с носителя на соседнюю пустую клетку. Тратит
    СОБСТВЕННОЕ действие коня (не носителя) — после этого конь в этот
    ход больше действовать не может."""
    host = state.pieces.get(knight.host_id)
    knight.is_mounted_knight = False
    knight.host_id = None
    knight.col, knight.row = dest
    if host is not None:
        host.mounted_knight_id = None
    knight.actions_used += 1


def do_mount(state, knight, host):
    knight.is_mounted_knight = True
    knight.host_id = host.id
    knight.col, knight.row = host.col, host.row
    host.mounted_knight_id = knight.id
    knight.actions_used += 1


def do_king_heal(state, king, target, amount=None):
    """Король лечит соседнюю союзную фигуру (роль короля как
    поддерживающего юнита). Расходует действие короля."""
    heal = amount if amount is not None else config.KING_HEAL_AMOUNT
    target.hp = min(target.max_hp, target.hp + heal)
    king.actions_used += 1


def apply_healing(state, color):
    """Лечение на домашней линии. Вызывается в начале хода данного цвета.
    В режиме "base" лечение отключается, если тыл этого цвета был
    скомпрометирован (враг проник в зону расстановки) — см.
    update_base_compromise()."""
    if getattr(state, "game_mode", "standard") == "base" and getattr(state, "base_compromised", {}).get(color):
        return
    home_row = config.WHITE_HOME_ROW if color == "white" else config.BLACK_HOME_ROW
    for p in state.pieces.values():
        if p.color == color and p.alive() and not p.is_mounted_knight and p.row == home_row:
            if p.hp < p.max_hp:
                p.hp = min(p.max_hp, p.hp + config.HEAL_AMOUNT)


def update_base_compromise(state):
    """Режим Base/Frontline: если вражеская фигура проникла в тыловую
    (стартовую) зону игрока, лечение для этого цвета отключается до
    конца партии — нужно защищать тыл, а не просто удерживать фронт.
    Не влияет на другие режимы (проверяется вызывающей стороной/apply_healing)."""
    if getattr(state, "game_mode", "standard") != "base":
        return
    if not hasattr(state, "base_compromised"):
        state.base_compromised = {"white": False, "black": False}
    white_zone = config.WHITE_HALF_ROWS
    black_zone = config.BLACK_HALF_ROWS
    for p in state.pieces.values():
        if not p.alive() or p.is_mounted_knight:
            continue
        if p.color == "black" and white_zone[0] <= p.row <= white_zone[1]:
            state.base_compromised["white"] = True
        if p.color == "white" and black_zone[0] <= p.row <= black_zone[1]:
            state.base_compromised["black"] = True
