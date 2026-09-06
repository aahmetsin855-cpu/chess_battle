# -*- coding: utf-8 -*-
"""
Система хода: стадия короля -> lobby-стадия -> смена хода.
Таймеры (KING_ACTION_TIME, MAIN_TURN_TIME) берутся из config.py.
"""
import config
import combat


def start_turn(state, color):
    """Начинает новый ход указанного цвета: сброс использования, лечение, таймеры."""
    state.turn_color = color
    state.phase = "king_stage"
    state.reset_turn_flags(color)
    state.king_stage_timer = config.KING_ACTION_TIME
    state.main_timer = config.MAIN_TURN_TIME
    combat.update_base_compromise(state)
    combat.apply_healing(state, color)
    state.last_event = f"Ход {('белых' if color == 'white' else 'чёрных')} №{state.turn_number}"


def end_king_stage(state):
    state.phase = "lobby_stage"
    state.main_timer = config.MAIN_TURN_TIME


def end_turn(state):
    """Завершает текущий ход и передаёт очередь сопернику (или завершает игру)."""
    winner = state.check_victory()
    if winner:
        state.phase = "game_over"
        state.winner = winner
        return
    next_color = "black" if state.turn_color == "white" else "white"
    state.turn_number += 1
    start_turn(state, next_color)
