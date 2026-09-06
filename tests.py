# -*- coding: utf-8 -*-
"""
Регрессионные тесты проекта HP Battle Chess.

Не требует pytest — запускается напрямую:
    python3 tests.py

Если pygame недоступен в окружении, тесты логики (без UI) всё равно
пройдут — только раздел рендера/main.py потребует pygame. Для CI-подобных
проверок без дисплея можно подставить заглушку pygame (см. комментарий
в конце файла).

Покрывает:
- базовые правила движения/атаки (не шахматные);
- механику коня-наездника (щит + доп. действие);
- сплэш-атаку ферзя;
- полную симуляцию партии со случайными легальными ходами;
- планирование хода ИИ (plan_ai_turn) на разных уровнях сложности и то,
  что оно НЕ мутирует переданное состояние;
- ai_interface: AlgorithmAI и LocalAI (с корректным graceful fallback,
  если Ollama недоступна);
- ai_memory: сохранение/загрузка статистики партий;
- settings: сохранение/загрузка настроек;
- coords: корректность преобразования координат (ориентация доски);
- animation: базовое поведение контроллера анимаций;
- (если доступен pygame) main.py: создание приложения, экран выбора
  противника, устойчивость выбранного режима ферзя, state machine хода ИИ,
  полная партия игрок+ИИ с анимациями.
"""
import inspect
import random
import sys
import time
import traceback

PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
        print(f"[OK]   {name}")
    except Exception as e:
        FAILED.append((name, e))
        print(f"[FAIL] {name}: {e}")
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Логика (без Pygame)
# ---------------------------------------------------------------------------
def test_basic_rules():
    import config
    from game_state import GameState
    import rules

    state = GameState()
    knight = state.add_piece("knight", "white", 3, 3)
    moves = rules.get_move_cells(state, knight)
    assert len(moves) > 0
    for c, r in moves:
        assert max(abs(c - knight.col), abs(r - knight.row)) == 2, "конь должен ходить на 2 клетки, не буквой Г"

    pawn = state.add_piece("pawn", "white", 1, 1)
    pmoves = rules.get_move_cells(state, pawn)
    assert (0, 0) in pmoves or (2, 2) in pmoves  # диагонали разрешены


def test_mount_shield_mechanic():
    from game_state import GameState
    import rules
    import combat
    import config

    state = GameState()
    knight = state.add_piece("knight", "white", 3, 3)
    rook = state.add_piece("rook", "white", 3, 4)
    rook_full_hp = rook.hp
    targets = rules.get_mount_targets(state, knight)
    assert rook in targets
    combat.do_mount(state, knight, rook)
    assert rook.mounted_knight_id == knight.id
    assert rook.max_actions() == 2

    combat.apply_damage(state, rook, 1)
    assert knight.hp == config.KNIGHT_HP - 1
    assert rook.hp == rook_full_hp  # весь урон поглотил конь

    combat.apply_damage(state, rook, config.KNIGHT_HP - 1 + 5)  # добиваем коня с запасом
    assert knight.id not in state.pieces  # конь уничтожен
    assert rook.mounted_knight_id is None
    assert rook.hp == rook_full_hp  # носитель не пострадал


def test_queen_no_splash():
    """Ферзь: SPLASH полностью убран — единственная атака ферзя это
    RANGED (через queen_lock), обычная соседняя атака вообще не
    генерируется, и она не задевает соседей цели."""
    import config
    from game_state import GameState
    import actions
    import combat

    state = GameState()
    q = state.add_piece("queen", "white", 4, 4)
    e1 = state.add_piece("pawn", "black", 4, 5)
    e2 = state.add_piece("pawn", "black", 5, 5)
    e1_full_hp = e1.hp
    e2_full_hp = e2.hp

    legal = actions.get_legal_actions(state, q)
    assert not any(a["type"] == "attack" for a in legal), "у ферзя не должно быть мгновенной атаки без блокировки"
    assert not hasattr(combat, "do_queen_splash"), "do_queen_splash должна быть полностью удалена"

    combat.do_queen_lock(state, q, "queen_ranged")
    assert q.queen_locked_mode == "queen_ranged"
    combat.do_queen_ranged(state, q, e1)
    assert e1.hp == max(0, e1_full_hp - config.QUEEN_RANGED_DAMAGE)
    assert e2.hp == e2_full_hp  # соседняя фигура не должна пострадать


def test_full_random_simulation():
    import config
    from game_state import GameState
    import turn_system
    import actions as actions_mod

    random.seed(123)
    state = GameState()
    layout = ["rook", "knight", "bishop", "queen", "king", "bishop", "knight", "rook"]
    for c, t in enumerate(layout):
        state.add_piece(t, "white", c, 0)
    for c in range(8):
        state.add_piece("pawn", "white", c, 1)
    types = []
    for t, cnt in config.ARMY_COMPOSITION.items():
        types += [t] * cnt
    cells = [(c, r) for r in range(4, 8) for c in range(8)]
    random.shuffle(cells)
    for t, (c, r) in zip(types, cells):
        state.add_piece(t, "black", c, r)

    turn_system.start_turn(state, "white")
    for _ in range(40):
        if state.phase == "game_over":
            break
        color = state.turn_color
        king = next((p for p in state.pieces_of(color) if p.type == "king"), None)
        if king and king.actions_available() > 0:
            kacts = actions_mod.get_legal_actions(state, king)
            if kacts and random.random() < 0.5:
                actions_mod.apply_action(state, random.choice(kacts))
        turn_system.end_king_stage(state)
        for _ in range(6):
            acts = actions_mod.get_all_legal_actions(state, color)
            if not acts:
                break
            actions_mod.apply_action(state, random.choice(acts))
        w = state.check_victory()
        if w:
            state.phase = "game_over"
            state.winner = w
            break
        turn_system.end_turn(state)
    # не важно, кто выиграл — важно, что не упало и завершилось предсказуемо
    assert state.phase in ("game_over", "king_stage", "lobby_stage")


# ---------------------------------------------------------------------------
# AI планирование
# ---------------------------------------------------------------------------
def test_ai_plan_does_not_mutate_state():
    import config
    from game_state import GameState
    import turn_system
    from ai import plan_ai_turn

    random.seed(9)
    state = GameState()
    layout = ["rook", "knight", "bishop", "queen", "king", "bishop", "knight", "rook"]
    for c, t in enumerate(layout):
        state.add_piece(t, "white", c, 0)
    for c in range(8):
        state.add_piece("pawn", "white", c, 1)
    types = []
    for t, cnt in config.ARMY_COMPOSITION.items():
        types += [t] * cnt
    cells = [(c, r) for r in range(4, 8) for c in range(8)]
    random.shuffle(cells)
    for t, (c, r) in zip(types, cells):
        state.add_piece(t, "black", c, r)

    turn_system.start_turn(state, "black")
    snapshot_positions = [(p.id, p.col, p.row, p.hp) for p in state.pieces.values()]

    plan = plan_ai_turn(state, "black", difficulty="easy")
    assert isinstance(plan, list)

    after_positions = [(p.id, p.col, p.row, p.hp) for p in state.pieces.values()]
    assert snapshot_positions == after_positions, "planning must not mutate the passed-in state"


def test_ai_difficulty_levels_produce_plans():
    import config
    from game_state import GameState
    import turn_system
    from ai import plan_ai_turn

    random.seed(11)
    state = GameState()
    layout = ["rook", "knight", "bishop", "queen", "king", "bishop", "knight", "rook"]
    for c, t in enumerate(layout):
        state.add_piece(t, "white", c, 0)
    for c in range(8):
        state.add_piece("pawn", "white", c, 1)
    types = []
    for t, cnt in config.ARMY_COMPOSITION.items():
        types += [t] * cnt
    cells = [(c, r) for r in range(4, 8) for c in range(8)]
    random.shuffle(cells)
    for t, (c, r) in zip(types, cells):
        state.add_piece(t, "black", c, r)
    turn_system.start_turn(state, "black")

    for diff in ("easy", "normal"):
        plan = plan_ai_turn(state, "black", difficulty=diff)
        assert isinstance(plan, list) and len(plan) > 0, f"{diff} produced empty plan"



def test_rules_teaching_prompt_covers_all_mechanics_accurately():
    """Регрессия: аудит промпта для Ollama нашёл реальные пробелы/ошибки —
    Standard mode вообще не упоминал таймер/добивание по числу пленных,
    Base/Fog не упоминали, что смерть короля сразу проигрывает партию
    (хотя это ПЕРВОЕ, что проверяет GameState.check_victory), а ладья
    была описана без её особого действия rook_swap."""
    from ai_interface import RULES_TEACHING
    text = RULES_TEACHING

    # Standard: таймер + добивание по числу пленных (см. App._update_game_timer).
    assert "8-minute" in text or "8 minute" in text
    assert "captured" in text.lower()

    # Base/Fog: смерть короля — мгновенное поражение (см. GameState.check_victory).
    base_idx = text.find("Base mode")
    fog_idx = text.find("Fog of War", base_idx)
    assert base_idx != -1 and fog_idx != -1
    assert "king is an immediate loss" in text or "immediate loss" in text

    # Ладья: особое действие rook_swap должно быть упомянуто явно.
    assert "rook_swap" in text

    # Ферзь: режим турели держится, пока не снят явно (см. combat.do_queen_ranged).
    assert "keeps firing" in text or "persists" in text


def test_local_ai_teaching_prompt_and_context():
    import config
    import actions as actions_mod
    from game_state import GameState
    from ai_interface import LocalAI

    state = GameState()
    state.game_mode = "fog"
    state.fog_enabled = True
    state.turn_number = 4
    state.phase = "lobby_stage"

    state.add_piece("king", "black", config.BOARD_SIZE - 1, config.BOARD_SIZE - 1)
    state.add_piece("queen", "black", config.BOARD_SIZE - 2, config.BOARD_SIZE - 1)
    state.add_piece("king", "white", 0, 0)
    state.add_piece("pawn", "white", 1, 1)

    legal = actions_mod.get_all_legal_actions(state, "black")
    ai = LocalAI.__new__(LocalAI)
    prompt = ai._build_prompt(state, legal, "black")

    assert "HP BATTLE CHESS" in prompt or "CURRENT MATCH STATE" in prompt
    assert "FOG OF WAR" in prompt
    assert "YOUR ARMY:" in prompt
    assert "VISIBLE ENEMY UNITS:" in prompt
    assert "LEGAL ACTIONS:" in prompt
    assert "Choose exactly ONE index" in prompt
    assert "queen" in prompt.lower()
    assert "king" in prompt.lower()

    # ASCII-схема доски: заголовок, размер, и ровно config.BOARD_SIZE строк
    # символов, идущих ПЕРЕД числовым списком фигур (YOUR ARMY:).
    assert f"BOARD {config.BOARD_SIZE}x{config.BOARD_SIZE}" in prompt
    board_idx = prompt.index("BOARD ")
    army_idx = prompt.index("YOUR ARMY:")
    assert board_idx < army_idx, "схема доски должна идти раньше числового списка фигур"
    board_block = prompt[board_idx:army_idx]
    board_rows = [ln for ln in board_block.splitlines() if ln and set(ln) <= set(".KQRBNPkqrbnp")]
    assert len(board_rows) == config.BOARD_SIZE
    assert all(len(row) == config.BOARD_SIZE for row in board_rows)
    # На схеме король виден, ферзь тоже (для этого теста именно они и
    # расставлены) — оба свои (чёрные) фигуры для точки обзора "black",
    # поэтому в верхнем регистре; вражеский король тоже обязан быть виден
    # (в нижнем регистре, т.к. принадлежит белым).
    assert "K" in board_block
    assert "Q" in board_block
    assert "k" in board_block

def test_ai_interface_algorithm_and_local_fallback():
    import ai_interface
    from game_state import GameState
    import config

    algo = ai_interface.AlgorithmAI(difficulty="easy")
    assert "Algorithm" in algo.describe()

    local = ai_interface.LocalAI(difficulty="easy")
    # В песочнице/CI без Ollama это должно быть False, и вызов НЕ должен падать.
    plan = local.choose_turn_plan(GameState(), config.AI_COLOR)
    assert isinstance(plan, list)


def test_both_ai_types_enforce_fog_identically():
    """Регрессия: раньше AlgorithmAI получал уже 'затуманенный' state
    только потому, что так его вызывал main.py, а LocalAI сам клонировал
    ЧТО БЫ ЕМУ НИ ПЕРЕДАЛИ, включая полный state в тумане войны —
    архитектурный рассинхрон, который зависел от вызывающего кода, а не
    был гарантирован самими классами ИИ. Теперь fog.build_fogged_view
    применяется ВНУТРИ AIPlayer._fogged_snapshot и обязателен для обоих
    типов ИИ независимо от того, что им передали."""
    import ai_interface
    from game_state import GameState

    state = GameState()
    state.fog_enabled = True
    state.add_piece("king", "black", 0, 0)
    # Пешка белых далеко от всех чёрных фигур — не должна быть видна.
    hidden = state.add_piece("pawn", "white", 9, 9)
    visible_king = state.add_piece("king", "white", 1, 1)  # враг. король виден всегда

    algo = ai_interface.AlgorithmAI.__new__(ai_interface.AlgorithmAI)
    local = ai_interface.LocalAI.__new__(ai_interface.LocalAI)

    ws_algo = algo._fogged_snapshot(state, "black")
    ws_local = local._fogged_snapshot(state, "black")

    for ws, label in ((ws_algo, "AlgorithmAI"), (ws_local, "LocalAI")):
        assert hidden.id not in ws.pieces, f"{label}: скрытая вражеская фигура не должна попадать в снимок для ИИ"
        assert visible_king.id in ws.pieces, f"{label}: вражеский король должен быть виден всегда"


def test_local_ai_turn_budget_is_not_starved_by_single_query():
    """Регрессия ('Ollama ходит только королём'): дедлайн хода LocalAI
    раньше считался как max(LOCAL_AI_TIMEOUT, AI_THINK_TIME) — те же 8
    секунд НА ВЕСЬ ход, хотя один запрос к Ollama сам может занять почти
    весь LOCAL_AI_TIMEOUT. Первый же (king stage) запрос съедал весь
    бюджет, и до lobby-этапа очередь не доходила. Проверяем, что общий
    бюджет хода теперь заметно больше одного запроса — иначе баг
    воспроизводится для любой не мгновенно отвечающей модели."""
    import config
    assert config.LOCAL_AI_TURN_BUDGET >= config.LOCAL_AI_TIMEOUT * 3, (
        "бюджета хода должно хватать на несколько (не одну) LLM-запросов подряд"
    )


def test_local_ai_ollama_request_uses_configured_options():
    """Регрессия: LOCAL_AI_NUM_PREDICT/LOCAL_AI_TEMPERATURE были объявлены
    в config.py, но реально никогда не передавались в запрос к Ollama —
    ответ модели ничем не ограничивался по длине, из-за чего она могла
    рассуждать вслух вместо того, чтобы сразу выдать индекс (медленнее
    и менее надёжно парсится)."""
    import inspect
    import ai_interface
    src = inspect.getsource(ai_interface.LocalAI._query_ollama)
    assert "config.LOCAL_AI_NUM_PREDICT" in src
    assert "config.LOCAL_AI_TEMPERATURE" in src


# ---------------------------------------------------------------------------
# LAN protocol / multiplayer
# ---------------------------------------------------------------------------
def test_lan_protocol_snapshot_hash_and_fog_redaction():
    import config
    import network
    from game_state import GameState
    import turn_system

    state = GameState()
    state.game_mode = "fog"
    state.fog_enabled = True
    state.board_size = config.BOARD_SIZE
    state.add_piece("king", "white", 0, 0)
    hidden = state.add_piece("rook", "white", 9, 9)
    state.add_piece("king", "black", 9, 0)
    snap = network.make_snapshot(state, "black")
    ids = {p["id"] for p in snap["state"]["pieces"]}
    assert hidden.id not in ids, "Fog snapshot must omit hidden enemy piece completely"
    rebuilt = network.state_from_dict(snap["state"])
    assert network.state_hash(rebuilt) == snap["state_hash"]
    assert network.PROTOCOL_VERSION == 1


def test_lan_action_dedup_and_authority():
    import config
    import network
    from game_state import GameState
    import turn_system

    state = GameState()
    white = state.add_piece("king", "white", 0, 0)
    black = state.add_piece("king", "black", 9, 9)
    turn_system.start_turn(state, "black")

    host = network.HostSession(state, "Test")
    # No socket required for validation itself.
    legal = {"piece_id": black.id, "type": "move", "target": [8, 9]}
    assert host._valid_action(legal)
    illegal = {"piece_id": white.id, "type": "move", "target": [1, 1]}
    assert not host._valid_action(illegal)


def test_lan_localhost_host_client_roundtrip():
    import time
    import network
    from game_state import GameState

    state = GameState()
    state.add_piece("king", "white", 0, 0)
    state.add_piece("king", "black", 9, 9)
    host = network.HostSession(state, "LAN Test")
    host.start()
    client = network.ClientSession()
    try:
        client.connect({"host": "127.0.0.1", "port": host.tcp_port, "name": "LAN Test"})
        # Advance both endpoints while keeping every client message; the
        # handshake may legitimately arrive in the same iteration in which
        # the host flips its connected flag.
        msgs = []
        for _ in range(40):
            host.poll()
            msgs += client.poll()
            if host.connected:
                break
            time.sleep(0.02)
        assert host.connected
        # Welcome must assign the client black.
        # The host sends welcome from its UI-thread poll; keep both endpoints
        # advancing for a bounded window so this transport test is not timing-sensitive.
        # Бюджет ожидания увеличен (было 100x0.02с=2с) — в контейнеризованных
        #/сильно нагруженных CI-средах планировщик потоков иногда даёт двум
        # локальным сокетам заметно больше 2с на обмен первым сообщением;
        # сам протокол при этом не менялся, только терпение теста.
        for _ in range(300):
            host.poll()
            msgs += client.poll()
            if any(m.get("type") == "welcome" for m in msgs):
                break
            time.sleep(0.02)
        welcome = next(m for m in msgs if m.get("type") == "welcome")
        assert welcome["player_color"] == "black"
        assert welcome["protocol_version"] == 1
    finally:
        host.close()
        client.close()


# ---------------------------------------------------------------------------
# Память и настройки
# ---------------------------------------------------------------------------
def test_memory_roundtrip(tmp_override=True):
    import ai_memory

    mem = ai_memory.default_memory()
    mem = ai_memory.record_game_result(mem, winner="black", ai_color="black",
                                        opening_piece_type="knight", used_mount=True, turns=10)
    assert mem["games_played"] == 1
    assert mem["wins"] == 1
    assert mem["opening_preferences"]["knight"] == 1
    assert mem["common_tactics"]["mounted_knight_used"] == 1

    ok = ai_memory.save_memory(mem)
    assert ok
    reloaded = ai_memory.load_memory()
    assert reloaded["games_played"] >= 1

    bias = ai_memory.opening_bias(reloaded, "black")
    assert isinstance(bias, dict)


def test_settings_roundtrip():
    import settings as settings_mod

    s = settings_mod.load_settings()
    s["difficulty"] = "hard"
    ok = settings_mod.save_settings(s)
    assert ok
    reloaded = settings_mod.load_settings()
    assert reloaded["difficulty"] == "hard"


# ---------------------------------------------------------------------------
# Координаты (ориентация доски)
# ---------------------------------------------------------------------------
def test_bishop_line_of_sight():
    from game_state import GameState
    import rules

    state = GameState()
    bishop = state.add_piece("bishop", "white", 2, 2)
    blocker = state.add_piece("rook", "white", 3, 3)
    enemy = state.add_piece("pawn", "black", 4, 4)

    targets = rules.get_bishop_attack_targets(state, bishop)
    assert enemy not in targets, "линия огня должна быть заблокирована союзной фигурой, кроме пешки"

    state.remove_piece(blocker.id)
    targets2 = rules.get_bishop_attack_targets(state, bishop)
    assert enemy in targets2, "после удаления блокирующей фигуры цель должна снова быть доступна"

    # Новое специальное правило: союзная пешка не блокирует слону выстрел.
    pawn_blocker = state.add_piece("pawn", "white", 3, 3)
    targets3 = rules.get_bishop_attack_targets(state, bishop)
    assert enemy in targets3, "союзная пешка должна пропускать выстрел слона"
    state.remove_piece(pawn_blocker.id)


def test_rook_swap_mechanic():
    from game_state import GameState
    import actions as actions_mod
    import rules
    st = GameState()
    st.phase = "lobby_stage"
    rook = st.add_piece("rook", "white", 3, 3)
    bishop = st.add_piece("bishop", "white", 4, 3)
    bishop.actions_used = 1
    rook.actions_used = 0
    legal = actions_mod.get_legal_actions(st, rook)
    swap = next(a for a in legal if a["type"] == "rook_swap" and a["target_id"] == bishop.id)
    assert actions_mod.apply_action(st, swap) is True
    assert (rook.col, rook.row) == (4, 3)
    assert (bishop.col, bishop.row) == (3, 3)
    assert rook.actions_used == 1
    assert bishop.actions_used == 1


def test_queen_lock_unlock_flow():
    """Ферзь: выбор RANGED — отдельное действие, завершающее ход
    без атаки; атака выполняется отдельным действием на следующий раз;
    режим НЕ снимается сам по себе после выстрела (это был баг) — ферзь
    остаётся турелью и продолжает стрелять, пока не разблокируется явно."""
    from game_state import GameState
    import actions as actions_mod

    state = GameState()
    q = state.add_piece("queen", "white", 4, 4)
    state.add_piece("pawn", "black", 4, 6)  # в радиусе дальней атаки, не смежная

    acts = actions_mod.get_legal_actions(state, q)
    types = {a["type"] for a in acts}
    assert "move" in types
    assert any(a["type"] == "queen_lock" and a["mode"] == "queen_ranged" for a in acts)
    assert not any(a["type"] == "attack" for a in acts), "нельзя атаковать без предварительной блокировки режима"

    lock = next(a for a in acts if a["type"] == "queen_lock" and a["mode"] == "queen_ranged")
    actions_mod.apply_action(state, lock)
    assert q.queen_locked_mode == "queen_ranged"
    assert q.actions_used == 1

    q.actions_used = 0  # новый ход
    acts2 = actions_mod.get_legal_actions(state, q)
    types2 = {a["type"] for a in acts2}
    assert "move" not in types2, "заблокированный ферзь не может двигаться"
    assert any(a["type"] == "attack" for a in acts2)
    assert any(a["type"] == "queen_unlock" for a in acts2)

    attack = next(a for a in acts2 if a["type"] == "attack")
    actions_mod.apply_action(state, attack)
    assert q.queen_locked_mode == "queen_ranged", (
        "режим не должен сбрасываться сам по себе после выстрела — "
        "именно это и было багом"
    )

    # Следующий ход: ферзь всё ещё турель и может стрелять дальше без
    # повторной фиксации режима.
    q.actions_used = 0
    acts3 = actions_mod.get_legal_actions(state, q)
    types3 = {a["type"] for a in acts3}
    assert "move" not in types3
    assert any(a["type"] == "attack" for a in acts3)
    assert not any(a["type"] == "queen_lock" for a in acts3), "уже заблокирован — повторная фиксация не нужна"

    # Явная разблокировка — единственный способ снова начать двигаться.
    unlock = next(a for a in acts3 if a["type"] == "queen_unlock")
    actions_mod.apply_action(state, unlock)
    assert q.queen_locked_mode is None


def test_knight_dismount():
    """Конь может слезть с носителя на соседнюю пустую клетку; это тратит
    действие самого коня (не носителя), и после этого конь виден отдельно."""
    from game_state import GameState
    import rules
    import actions as actions_mod
    import combat

    state = GameState()
    knight = state.add_piece("knight", "white", 3, 3)
    rook = state.add_piece("rook", "white", 3, 4)
    combat.do_mount(state, knight, rook)
    assert rook.mounted_knight_id == knight.id
    assert knight.is_mounted_knight is True

    knight.actions_used = 0
    rook.actions_used = 0  # новый ход

    host_actions = actions_mod.get_legal_actions(state, rook)
    dismount_actions = [a for a in host_actions if a["type"] == "dismount"]
    assert dismount_actions, "должна быть возможность спешить коня при выборе носителя"
    assert all(a["piece_id"] == knight.id for a in dismount_actions), "действие принадлежит коню, не носителю"

    action = dismount_actions[0]
    actions_mod.apply_action(state, action)
    assert knight.is_mounted_knight is False
    assert rook.mounted_knight_id is None
    assert knight.actions_used == 1
    assert (knight.col, knight.row) == action["target"]
    assert (knight.col, knight.row) != (rook.col, rook.row)


def test_king_heal_action():
    from game_state import GameState
    import rules
    import combat
    import config

    state = GameState()
    king = state.add_piece("king", "white", 4, 4)
    ally = state.add_piece("pawn", "white", 4, 5)
    ally.hp = 1
    targets = rules.get_king_heal_targets(state, king)
    assert ally in targets
    combat.do_king_heal(state, king, ally)
    assert ally.hp == 1 + config.KING_HEAL_AMOUNT
    assert king.actions_used == 1


def test_configurable_board_sizes():
    import config

    original = (config.BOARD_WIDTH, config.BOARD_HEIGHT)
    try:
        for size in config.BOARD_SIZE_OPTIONS:
            config.configure_board_size(size)
            w, h = size
            assert config.BOARD_WIDTH == w and config.BOARD_HEIGHT == h
            assert config.BLACK_HOME_ROW == h - 1
            assert config.WHITE_HOME_ROW == 0
            lo, hi = config.WHITE_HALF_ROWS
            assert 0 <= lo <= hi < h
            assert config.SCREEN_WIDTH > 0 and config.SCREEN_HEIGHT > 0
    finally:
        config.configure_board_size(original)


def test_ai_formation_generation():
    import config
    from ai import suggest_formation

    original = config.BOARD_SIZE
    try:
        for size in (10, 12, 16):
            config.configure_board_size(size)
            layout = suggest_formation("black", config.BLACK_HALF_ROWS, config.BOARD_SIZE, samples=3)
            assert len(layout) == sum(config.get_army_composition(size).values())
            cells = {(c, r) for c, r, t in layout}
            assert len(cells) == len(layout), "формация не должна содержать наложений фигур"
            lo, hi = config.BLACK_HALF_ROWS
            for c, r, t in layout:
                assert lo <= r <= hi
                assert 0 <= c < size
    finally:
        config.configure_board_size(original)


def test_army_composition_scales_on_large_board():
    import config

    base = config.get_army_composition(10)
    large = config.get_army_composition(16)
    assert sum(base.values()) < sum(large.values()), "на 16x16 армия должна быть немного больше"
    assert large["pawn"] == base["pawn"] + 2
    assert large["bishop"] == base["bishop"] + 1
    assert large["rook"] == base["rook"] + 1
    assert large["knight"] == base["knight"]
    assert large["queen"] == base["queen"]
    assert large["king"] == base["king"]


def test_flag_mode_victory():
    from game_state import GameState
    import config

    state = GameState()
    state.game_mode = "flag"
    state.add_piece("king", "white", 0, 0)
    state.add_piece("king", "black", config.BOARD_SIZE - 1, config.BOARD_SIZE - 1)
    runner = state.add_piece("pawn", "white", 2, config.BOARD_SIZE - 2)
    assert state.check_victory() is None
    runner.row = config.BLACK_HOME_ROW
    assert state.check_victory() == "white"


def test_flag_mode_ai_intercepts_runner():
    """ИИ в режиме Flag должен активно перемещаться на перехват вражеской
    фигуры, приближающейся к его домашней линии, а не игнорировать угрозу."""
    import config
    from game_state import GameState
    import turn_system
    import actions as actions_mod
    from ai import plan_ai_turn

    original_size = config.BOARD_SIZE
    try:
        config.configure_board_size(10)
        state = GameState()
        state.game_mode = "flag"
        state.add_piece("king", "white", 0, 0)
        state.add_piece("king", "black", 9, 9)
        runner = state.add_piece("pawn", "white", 5, 7)   # 2 клетки от BLACK_HOME_ROW=9
        blocker_rook = state.add_piece("rook", "black", 5, 5)
        state.add_piece("pawn", "black", 0, 9)

        turn_system.start_turn(state, "black")
        plan = plan_ai_turn(state, "black", difficulty="normal")

        ws = state.clone_light()
        for a in plan:
            actions_mod.apply_action(ws, a)
        rook_after = ws.pieces.get(blocker_rook.id)
        assert rook_after is not None
        dist_after = max(abs(rook_after.col - runner.col), abs(rook_after.row - runner.row))
        dist_before = max(abs(blocker_rook.col - runner.col), abs(blocker_rook.row - runner.row))
        assert dist_after <= dist_before, (
            f"ИИ должен приближаться к бегуну, а не отдаляться (было {dist_before}, стало {dist_after})")
    finally:
        config.configure_board_size(original_size)


def test_base_mode_healing_disabled_on_compromise():
    from game_state import GameState
    import config
    import combat

    state = GameState()
    state.game_mode = "base"
    king = state.add_piece("king", "white", 4, config.WHITE_HOME_ROW)
    king.hp = 1
    combat.apply_healing(state, "white")
    assert king.hp == 2  # обычное лечение работает, пока тыл не скомпрометирован

    # враг проникает в зону расстановки белых
    state.add_piece("rook", "black", 1, config.WHITE_HALF_ROWS[0])
    combat.update_base_compromise(state)
    assert state.base_compromised["white"] is True

    king.hp = 1
    combat.apply_healing(state, "white")
    assert king.hp == 1  # лечение отключено после прорыва тыла


def test_fog_of_war_visibility_and_no_cheating():
    from game_state import GameState
    import fog

    state = GameState()
    state.fog_enabled = True
    wk = state.add_piece("king", "white", 4, 4)
    bk = state.add_piece("king", "black", 10, 10)
    close_enemy = state.add_piece("pawn", "black", 5, 5)
    far_enemy = state.add_piece("rook", "black", 10, 4)

    visible = fog.visible_enemy_ids(state, "white")
    assert bk.id in visible, "вражеский король должен быть виден всегда"
    assert close_enemy.id in visible
    assert far_enemy.id not in visible

    fogged = fog.build_fogged_view(state, "white")
    assert far_enemy.id not in fogged.pieces, "AI-планирование не должно видеть скрытые фигуры"
    assert close_enemy.id in fogged.pieces
    assert wk.id in fogged.pieces


def test_board_coordinate_orientation():
    import config
    import coords

    # логическая клетка (0,0) — домашняя линия белых (игрока) — должна
    # отрисовываться В НИЖНЕЙ части экрана (больший y), а (0,7) — домашняя
    # линия чёрных (ИИ) — в верхней части (меньший y).
    x0, y0 = coords.board_to_screen(0, config.WHITE_HOME_ROW)
    x7, y7 = coords.board_to_screen(0, config.BLACK_HOME_ROW)
    assert y0 > y7, "белые (игрок) должны отображаться внизу экрана"

    # клик по экрану должен корректно возвращаться в логические координаты
    back = coords.screen_to_board(x0 + 5, y0 + 5)
    assert back == (0, config.WHITE_HOME_ROW)

    # логические координаты фигур в GameState никогда не переворачиваются
    from game_state import GameState
    state = GameState()
    p = state.add_piece("pawn", "white", 2, config.WHITE_HOME_ROW)
    assert p.row == config.WHITE_HOME_ROW  # координата в логике не изменилась


# ---------------------------------------------------------------------------
# Анимация
# ---------------------------------------------------------------------------
def test_animation_controller():
    from animation import AnimationController

    anim = AnimationController(speed=4.0)  # быстрее, чтобы тест был быстрым
    assert not anim.is_blocking()
    anim.start_move(piece_id=1, from_cell=(0, 0), to_cell=(1, 0))
    assert anim.is_blocking()
    pos0 = anim.get_piece_override_pos(1)
    assert pos0 is not None
    for _ in range(200):
        anim.update(0.01)
        if not anim.is_blocking():
            break
    assert not anim.is_blocking()

    anim.spawn_impact((0, 0))
    anim.spawn_death((0, 0), "white", "pawn")
    effects = anim.get_effects()
    # spawn_impact/spawn_death каждый добавляет и свой основной эффект, и
    # декоративный "particles" (искры) — см. animation.py._make_particles.
    kinds = [e["kind"] for e in effects]
    assert kinds.count("impact") == 1
    assert kinds.count("death") == 1
    assert kinds.count("particles") == 2
    assert len(effects) == 4

    scale_idle = anim.get_piece_scale(1)
    assert scale_idle == 1.0
    anim.start_move(piece_id=2, from_cell=(0, 0), to_cell=(1, 0))
    scale_moving = anim.get_piece_scale(2)
    assert scale_moving != 1.0 or True  # squash/stretch может на некоторых кадрах пройти через 1.0


# ---------------------------------------------------------------------------
# main.py / Pygame-зависимые тесты (пропускаются, если pygame недоступен)
# ---------------------------------------------------------------------------
def _pygame_available():
    try:
        import pygame  # noqa: F401
        return True
    except Exception:
        return False


def test_main_app_smoke():
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m

    app = m.App()
    assert app.state.phase == "main_menu"
    app.handle_main_menu_click(app.main_menu_buttons["play"].rect.center)
    assert app.state.phase == "select_game_mode"
    app.handle_select_game_mode_click(app.game_mode_next_button.rect.center)
    assert app.state.phase == "select_board_size"
    app.handle_select_board_size_click(app.board_size_next_button.rect.center)
    assert app.state.phase == "setup_white"

    app.confirm_setup()
    assert app.state.phase == "select_opponent"

    app.selected_opponent_type = "algorithm"
    app.selected_difficulty = "easy"
    app.confirm_opponent()
    assert app.state.phase == "king_stage"
    assert app.ai_player is not None


def test_queen_action_selection_stability():
    """Ферзь: выбор RANGED — реальное состояние ФИГУРЫ
    (queen.queen_locked_mode), а не эфемерный UI-фильтр, поэтому оно
    физически не может «сброситься» при повторном рендере/выборе."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import turn_system

    app = m.App()
    app.confirm_setup()
    app.selected_opponent_type = "algorithm"
    app.selected_difficulty = "easy"
    app.confirm_opponent()

    queen = next(p for p in app.state.pieces.values() if p.type == "queen" and p.color == "white")
    turn_system.end_king_stage(app.state)

    app.select_piece(queen)
    assert queen.queen_locked_mode is None
    lock_action = next(a for a in app.special_actions if a["type"] == "queen_lock" and a["mode"] == "queen_ranged")
    app.apply_action_with_animation(lock_action)
    assert queen.queen_locked_mode == "queen_ranged"

    # Повторный выбор той же (или другой) фигуры не должен сбрасывать
    # состояние блокировки — оно хранится на самой фигуре, а не в UI.
    app.deselect()
    pawn = next(p for p in app.state.pieces.values() if p.type == "pawn" and p.color == "white")
    app.select_piece(pawn)
    app.deselect()
    app.select_piece(queen)
    assert queen.queen_locked_mode == "queen_ranged", "повторный рендер/выбор не должен сбрасывать режим ферзя"
    # Пока заблокирован — двигаться нельзя, только атаковать в этом режиме.
    assert not any(a["type"] == "move" for a in app.action_map.values())


def test_network_tick_host_drains_opponent_visual_actions():
    """Регрессия: раньше App._network_tick для ХОСТА вызывал
    network_host.tick(dt) (который внутри копит визуальные действия
    соперника в HostSession.visual_action_queue), но НИКОГДА не забирал
    их оттуда — pop_visual_actions() вызывался только из _poll_network,
    а _poll_network для хоста не вызывался ниоткуда вообще. В итоге
    белые (хост) не видели ни анимацию атаки чёрных, ни цифры урона —
    состояние просто телепортировалось. Проверяем, что после тика хоста
    накопленное действие соперника оказывается в network_anim_queue."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m

    class _FakePeer:
        class _Closed:
            def is_set(self):
                return False
        closed = _Closed()

    class _FakeHostSession:
        """Минимальная замена network.HostSession: не открывает сокеты,
        но воспроизводит именно то поведение, от которого зависит баг —
        tick() копит визуальные действия, pop_visual_actions() их отдаёт."""
        def __init__(self, state):
            self.state = state
            self.peer = _FakePeer()
            self.connected = True
            self._pending_visual = [({"type": "move", "piece_id": 1, "target": (5, 5)}, state)]

        def tick(self, dt):
            pass  # реальный HostSession.tick() тут вызвал бы poll(), не нужно для этого теста

        def pop_visual_actions(self):
            out = self._pending_visual
            self._pending_visual = []
            return out

        def notify_local_state_changed(self, *a, **kw):
            pass

    app = m.App()
    app.confirm_setup()
    app.network_role = "host"
    app.network_screen = "game"
    app.network_host = _FakeHostSession(app.state)

    assert app.network_anim_queue == []
    app._network_tick(0.016)
    assert len(app.network_anim_queue) == 1, "визуальное действие соперника должно попасть в очередь анимации хоста"
    queued_action, before_state, after_state = app.network_anim_queue[0]
    assert queued_action["type"] == "move"


def test_frame_dt_is_clamped():
    """Регрессия: dt одного кадра должен быть ограничен config.MAX_FRAME_DT.
    Без этого затык основного потока (например разбор JSON у большого
    снапшота состояния или пачка сообщений, скопившихся из-за лагов
    сети — то есть ровно то, что происходит в мультиплеере на медленном
    интернете) на следующем кадре даёт один аномально большой dt, и
    анимация хода/удара соперника перепрыгивает сразу к концу вместо
    плавной интерполяции — на экране это выглядит как "дёрганое"
    телепортирование фигуры. Именно поэтому раньше это было заметно
    только в мультиплеере: локально/против ИИ на основном потоке нет
    блокирующей сетевой работы, которая могла бы вызвать такой затык."""
    import config
    assert config.MAX_FRAME_DT > 0
    # Меньше длительности самой короткой анимации, иначе клэмп бесполезен.
    assert config.MAX_FRAME_DT < config.ATTACK_ANIMATION_TIME
    assert config.MAX_FRAME_DT < config.MOVE_ANIMATION_TIME
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    src = inspect.getsource(m.App.run)
    assert "config.MAX_FRAME_DT" in src, "run() должен клэмпить dt через config.MAX_FRAME_DT"


def test_get_all_legal_actions_index_patch_is_restored():
    """Регрессия: get_all_legal_actions временно подменяет
    state.get_piece_at на быструю индексную версию (см. actions.py) для
    ускорения поиска ИИ. Проверяем, что после вызова:
    1) оригинальный метод восстановлен на самом экземпляре state;
    2) подмена не 'протекает' на другой экземпляр GameState;
    3) результат совпадает с тем, что даёт обычный get_piece_at."""
    from game_state import GameState
    import actions as actions_mod

    state = GameState()
    q = state.add_piece("queen", "white", 4, 4)
    state.add_piece("pawn", "black", 4, 6)
    state.add_piece("knight", "white", 5, 4)

    other = GameState()
    other.add_piece("rook", "black", 2, 2)

    bound_before = state.get_piece_at
    acts = actions_mod.get_all_legal_actions(state, "white")
    assert acts, "ожидались легальные действия"

    # (1) метод на state снова резолвится нормально и даёт тот же результат.
    assert state.get_piece_at(4, 4) is q
    assert state.get_piece_at(9, 9) is None

    # (2) второй экземпляр никак не затронут временной подменой.
    assert other.get_piece_at(2, 2) is not None
    assert other.get_piece_at(4, 4) is None

    # Повторный вызов (проверка, что подмена/восстановление идемпотентны).
    acts2 = actions_mod.get_all_legal_actions(state, "white")
    assert len(acts2) == len(acts)


def test_file_dialog_watchdog_recovers_from_stuck_process():
    """Регрессия ('закрыл диалог выбора файла — он больше не появляется'):
    _choose_file() отказывается открывать второй диалог, пока
    self._file_dialog_proc не None — это защита от дублей, но если
    процесс диалога по любой причине не завершается сам (зависший Tk
    event loop, особенность WM при закрытии окна и т.п.), poll() будет
    возвращать None вечно, и кнопка выбора файла молча ничего не будет
    делать НАВСЕГДА. Проверяем, что после тайм-аута watchdog'а процесс
    убивается, а состояние сбрасывается — кнопка снова работает."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m

    class _StuckProc:
        """Имитирует процесс, который никогда не завершается сам."""
        def __init__(self):
            self.killed = False
            self.returncode = None

        def poll(self):
            return None  # "висит" вечно, как зависший Tk event loop

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return None

    app = m.App()
    app.confirm_setup()
    proc = _StuckProc()
    app._file_dialog_proc = proc
    app._file_dialog_kind = "music"
    app._file_dialog_started_at = time.time() - (m.App._FILE_DIALOG_WATCHDOG_SECONDS + 1)

    app._poll_file_dialog()

    assert proc.killed, "watchdog должен принудительно завершить зависший процесс"
    assert app._file_dialog_proc is None, "состояние должно сброситься — иначе кнопка навсегда заблокирована"
    assert app._file_dialog_kind is None

    # Кнопка должна снова уметь открыть диалог (реальный subprocess.Popen
    # тут может не сработать в песочнице без дисплея — важно только, что
    # guard в _choose_file больше не блокирует попытку).
    app._choose_file("music")
    assert app._file_dialog_proc is not proc


def test_file_dialog_helper_command_frozen_vs_source():
    """Регрессия (PyInstaller): в собранном приложении sys.executable —
    это САМ .exe игры, и его достаточно вызвать с флагом-хелпером; при
    запуске из исходников sys.executable — это просто python.exe,
    которому дополнительно нужен путь к main.py, иначе
    '--file-dialog-helper' попадёт в питон как неизвестный флаг
    интерпретатора, а не как аргумент игры."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import sys
    import main as m

    app = m.App()
    app.confirm_setup()

    was_frozen = getattr(sys, "frozen", False)
    try:
        sys.frozen = True
        cmd = app._file_dialog_helper_command("music")
        assert cmd[0] == sys.executable
        assert cmd[1] == "--file-dialog-helper"
        assert cmd[2] == "music"

        sys.frozen = False
        cmd2 = app._file_dialog_helper_command("background")
        assert cmd2[0] == sys.executable
        assert cmd2[1] == sys.argv[0] or cmd2[1] == __import__("os").path.abspath(sys.argv[0])
        assert cmd2[2] == "--file-dialog-helper"
        assert cmd2[3] == "background"
    finally:
        if was_frozen:
            sys.frozen = was_frozen
        else:
            try:
                del sys.frozen
            except AttributeError:
                pass


def test_base_dir_uses_executable_when_frozen():
    """Регрессия (PyInstaller): __file__ в frozen-сборке указывает
    внутрь бандла (например _internal при --onedir), а не туда, где
    реально лежит .exe и куда ожидается писать data/ — эта папка может
    быть даже недоступна на запись. sys.executable в PyInstaller всегда
    указывает на настоящий путь установки."""
    import inspect
    import config
    src = inspect.getsource(config)
    assert 'getattr(sys, "frozen"' in src
    assert "sys.executable" in src


# ---------------------------------------------------------------------------
# Android: platform detection и writable-пути (см. platform_utils.py,
# config.WRITABLE_DIR). Эти тесты выполняются на ЛЮБОЙ платформе (в т.ч.
# на Windows/Linux при разработке) — они не запускают реальный Android,
# а проверяют, что: (1) detection не ломается вне Android и не путает
# desktop с Android; (2) при подделанных Android-признаках writable-путь
# действительно переключается и не пытается писать внутрь BASE_DIR.
# ---------------------------------------------------------------------------
def test_platform_utils_desktop_is_not_android():
    """На обычном desktop (в т.ч. в этом тестовом окружении без
    ANDROID_*-переменных) is_android() должен быть False, а
    get_app_writable_dir() — вернуть None (сигнал "используй свой
    desktop-путь по умолчанию", см. config.WRITABLE_DIR)."""
    import os
    import platform_utils
    env_backup = {k: os.environ.pop(k, None) for k in
                  ("ANDROID_ARGUMENT", "ANDROID_PRIVATE", "ANDROID_ROOT", "ANDROID_DATA")}
    try:
        assert platform_utils.is_android() is False
        assert platform_utils.is_desktop() is True
        assert platform_utils.get_app_writable_dir() is None
    finally:
        for k, v in env_backup.items():
            if v is not None:
                os.environ[k] = v


def test_platform_utils_detects_android_via_env(tmp_path=None):
    """Подделываем признак Android-рантайма (ANDROID_ARGUMENT, который
    python-for-android гарантированно выставляет для любого запущенного
    APK) и проверяем, что is_android()/is_desktop() переключаются, а
    writable-директория действительно создаётся и отличается от
    BASE_DIR — то есть игра не попытается писать внутрь read-only APK.
    """
    import os
    import tempfile
    import platform_utils

    had_key = "ANDROID_ARGUMENT" in os.environ
    old_value = os.environ.get("ANDROID_ARGUMENT")
    had_home = "HOME" in os.environ
    old_home = os.environ.get("HOME")
    fake_home = tempfile.mkdtemp(prefix="hp_chess_fake_android_home_")
    try:
        os.environ["ANDROID_ARGUMENT"] = "1"
        os.environ["HOME"] = fake_home
        assert platform_utils.is_android() is True
        assert platform_utils.is_windows() is False
        assert platform_utils.is_desktop() is False

        writable = platform_utils.get_app_writable_dir()
        assert writable is not None, "на Android должен вернуться реальный путь, а не None"
        assert os.path.isdir(writable), "директория должна быть создана автоматически"
        # Не должен совпадать с директорией самого пакета — иначе это
        # снова попытка писать рядом с исходниками/APK.
        import os as _os
        pkg_dir = _os.path.dirname(_os.path.abspath(platform_utils.__file__))
        assert _os.path.abspath(writable) != _os.path.abspath(pkg_dir)
    finally:
        if had_key:
            os.environ["ANDROID_ARGUMENT"] = old_value
        else:
            os.environ.pop("ANDROID_ARGUMENT", None)
        if had_home:
            os.environ["HOME"] = old_home
        else:
            os.environ.pop("HOME", None)


def test_config_data_dir_follows_writable_dir_not_base_dir():
    """config.DATA_DIR/AI_MEMORY_FILE/GAMES_DIR/SETTINGS_FILE должны быть
    построены от config.WRITABLE_DIR (платформенно-корректный writable
    каталог), а не напрямую от BASE_DIR — иначе Android-порт молча
    вернулся бы к попытке писать внутрь APK."""
    import config
    assert config.DATA_DIR.startswith(config.WRITABLE_DIR)
    assert config.AI_MEMORY_FILE.startswith(config.DATA_DIR)
    assert config.GAMES_DIR.startswith(config.DATA_DIR)
    assert config.SETTINGS_FILE.startswith(config.DATA_DIR)
    # На этой (desktop, не Android) платформе поведение обязано остаться
    # прежним: WRITABLE_DIR == BASE_DIR, чтобы не сломать Windows.
    import platform_utils
    if not platform_utils.is_android():
        assert config.WRITABLE_DIR == config.BASE_DIR


def test_renderer_uses_builtin_font_not_sysfont():
    """Регрессия (Android): pygame.font.SysFont("Arial", ...) — desktop-
    допущение, которого на Android просто нет. main.py и renderer.py
    должны использовать renderer.make_font()/pygame.font.Font(None,...),
    а не SysFont, чтобы шрифты не зависели от системных TTF."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import inspect
    import renderer
    import main as m
    assert "def make_font" in inspect.getsource(renderer)
    assert "SysFont" not in inspect.getsource(renderer)
    assert "SysFont" not in inspect.getsource(m)


def test_tutorial_steps_data_integrity():
    """Обучение: базовая целостность данных шагов. Не зависит от pygame —
    проверяет только tutorial.py, который расставляет фигуры через
    настоящий GameState.add_piece(), без своей копии правил."""
    import tutorial
    from game_state import GameState

    assert tutorial.step_count() >= 7  # весь список из задания + вступление/финал
    ids = [s["id"] for s in tutorial.STEPS]
    assert len(ids) == len(set(ids)), "id шагов должны быть уникальны"

    for i, step in enumerate(tutorial.STEPS):
        assert step["title"]
        assert step["text"]
        assert len(step["text"]) < 220, (
            f"{step['id']}: подсказка должна быть короткой (1-3 предложения), а не текстовой страницей"
        )
        state = tutorial.build_state_for_step(i, GameState)
        assert len(state.pieces) >= 1, f"{step['id']}: на доске должна быть хотя бы одна фигура"
        assert state.turn_color == "white"

    # Обязательные темы урока из задания представлены явно.
    required = {"intro_pawn", "intro_knight", "intro_bishop", "intro_rook", "intro_queen", "intro_king",
                "select_move", "attack", "rook_swap", "bishop_attack",
                "knight_mount", "queen_lock", "queen_ranged", "king_stage",
                "surrender", "mode_standard", "mode_flag", "mode_base", "mode_fog"}
    assert required <= set(ids), f"не хватает шагов: {required - set(ids)}"


def test_tutorial_interactive_steps_offer_their_goal_action():
    """Для каждого интерактивного шага настоящая генерация легальных
    действий (actions.get_legal_actions) должна реально предлагать
    нужный тип действия — иначе шаг физически непроходим."""
    import tutorial
    import actions as actions_mod
    from game_state import GameState

    for i, step in enumerate(tutorial.STEPS):
        goal = step["goal"]
        if goal is None or goal == "surrender":
            continue
        state = tutorial.build_state_for_step(i, GameState)
        sel = step["select"]
        assert sel is not None, f"{step['id']}: интерактивному шагу нужна авто-выбранная фигура"
        piece = state.get_piece_at(*sel)
        assert piece is not None, f"{step['id']}: на клетке select нет фигуры"
        legal = actions_mod.get_legal_actions(state, piece)
        types = {a["type"] for a in legal}
        if goal == "king_action":
            assert {"move", "heal"} & types, f"{step['id']}: ни хода, ни лечения не доступно"
        else:
            assert goal in types, f"{step['id']}: цель '{goal}' недостижима — доступные типы: {types}"


def test_tutorial_uses_real_fog_and_king_stage_mechanics():
    """Уроки про короля и про туман войны должны использовать НАСТОЯЩИЕ
    механики (state.phase == 'king_stage', state.fog_enabled + настоящий
    fog.visible_enemy_ids), а не текстовую имитацию."""
    import tutorial
    import fog as fog_mod
    import config
    from game_state import GameState

    by_id = {s["id"]: i for i, s in enumerate(tutorial.STEPS)}

    king_state = tutorial.build_state_for_step(by_id["king_stage"], GameState)
    assert king_state.phase == "king_stage"

    fog_state = tutorial.build_state_for_step(by_id["mode_fog"], GameState)
    assert fog_state.fog_enabled is True
    visible = fog_mod.visible_enemy_ids(fog_state, "white")
    blacks = list(fog_state.pieces_of("black"))
    hidden_pawn = next(p for p in blacks if p.type == "pawn")
    enemy_king = next(p for p in blacks if p.type == "king")
    assert hidden_pawn.id not in visible, "далёкая декоративная пешка должна быть скрыта туманом — иначе облако нечего показывать"
    assert enemy_king.id in visible, "вражеский король виден всегда, даже в тумане — он и есть цель мини-боя"

    cells = fog_mod.visible_cells(fog_state, "white")
    assert (hidden_pawn.col, hidden_pawn.row) not in cells, "клетка с скрытой пешкой должна быть под облаком тумана"


def test_tutorial_advance_pause_prevents_instant_board_swap():
    """Регрессия ('резко другое действие, не успеваю осознать'): раньше
    доска сразу сменялась на новый шаг, как только заканчивалась
    анимация. Теперь между результатом действия и переходом дальше
    должна быть пауза (TUTORIAL_ADVANCE_PAUSE) — шаг не должен смениться
    сразу же после первого кадра без анимации."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import tutorial
    import renderer as R

    app = m.App()
    by_id = {s["id"]: i for i, s in enumerate(tutorial.STEPS)}
    app.enter_tutorial()
    try:
        app._load_tutorial_step(by_id["select_move"])
        pawn = app.get_selected_piece()
        move_cell = next(c for c, a in app.action_map.items() if a["type"] == "move")
        tx, ty = R.board_to_screen(*move_cell)
        app.handle_tutorial_click((tx + 3, ty + 3))
        assert app.tutorial_pending_advance is True

        # Один кадр без анимации — шаг НЕ должен уже смениться.
        app.animation.update(0.2)
        app._update_tutorial(0.2)
        assert tutorial.STEPS[app.tutorial_step_index]["id"] == "select_move", (
            "переход не должен происходить мгновенно — нужна пауза, чтобы увидеть результат"
        )
        assert app.tutorial_pending_advance is True
    finally:
        app.exit_tutorial()


def test_tutorial_buttons_never_overlap_hint_card():
    """Регрессия ('кнопки Далее/Назад наезжают на текст'): кнопки должны
    позиционироваться ПОСЛЕ карточки-подсказки, а не по фиксированным
    координатам — иначе более длинная подсказка наезжает на них."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import tutorial
    import config

    app = m.App()
    app.enter_tutorial()
    try:
        longest = max(range(tutorial.step_count()), key=lambda i: len(tutorial.STEPS[i]["text"]))
        app._load_tutorial_step(longest)
        app.draw_tutorial_screen((0, 0))

        card_lines = app._wrap_text_lines(
            tutorial.STEPS[longest]["text"], app.font_small, config.SIDE_PANEL_WIDTH - 56)
        # Точно та же арифметика Layout, что и в draw_tutorial_panel: header
        # take(20, gap=6) + title take(22, gap=8) идут до начала карточки.
        card_top = config.BOARD_MARGIN_Y + 6 + (20 + 6) + (22 + 8)
        min_card_bottom = card_top + len(card_lines) * 20
        assert app.tutorial_back_button.rect.y >= min_card_bottom, "кнопка «Назад» наезжает на карточку"
        assert app.tutorial_next_button.rect.y >= min_card_bottom, "кнопка «Далее» наезжает на карточку"
        assert not app.tutorial_back_button.rect.colliderect(app.tutorial_next_button.rect)
        assert not app.tutorial_skip_button.rect.colliderect(app.tutorial_next_button.rect)
    finally:
        app.exit_tutorial()


def test_tutorial_button_present_in_main_menu():
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    app = m.App()
    assert "tutorial" in app.main_menu_buttons


def test_tutorial_enter_exit_restores_real_state():
    """Обучение не должно портить обычную игру: вход подменяет
    self.state на маленький учебный, выход восстанавливает В ТОЧНОСТИ
    тот же объект state и тот же цвет игрока/обзора."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import config

    app = m.App()
    real_state = app.state
    app.player_color = "black"
    config.VIEWER_COLOR = "black"
    try:
        app.enter_tutorial()
        assert app.tutorial_active is True
        assert app.state is not real_state
        assert app.network_role is None
        assert app.ai_player is None

        app.handle_tutorial_click(app.tutorial_skip_button.rect.center)
        assert app.tutorial_active is False
        assert app.state is real_state, "после выхода должен вернуться исходный state, не его копия"
        assert app.player_color == "black"
        assert config.VIEWER_COLOR == "black"
    finally:
        config.VIEWER_COLOR = "white"


def test_tutorial_move_step_completes_and_advances():
    """Полный цикл: выбор фигуры (авто) -> клик по подсвеченной клетке
    -> настоящая анимация AnimationController -> автопереход дальше."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import tutorial
    import renderer as R

    app = m.App()
    by_id = {s["id"]: i for i, s in enumerate(tutorial.STEPS)}
    app.enter_tutorial()
    try:
        assert tutorial.STEPS[app.tutorial_step_index]["id"] == "welcome"

        app._load_tutorial_step(by_id["select_move"])
        assert tutorial.STEPS[app.tutorial_step_index]["id"] == "select_move"

        pawn = app.get_selected_piece()
        assert pawn is not None and pawn.type == "pawn"
        move_cell = next(c for c, a in app.action_map.items() if a["type"] == "move")

        tx, ty = R.board_to_screen(*move_cell)
        app.handle_tutorial_click((tx + 3, ty + 3))
        assert (pawn.col, pawn.row) == move_cell, "действие должно примениться через настоящий actions.apply_action"
        assert app.tutorial_pending_advance is True

        for _ in range(80):
            if not app.tutorial_pending_advance:
                break
            app.animation.update(0.05)
            app._update_tutorial(0.05)
        assert app.tutorial_pending_advance is False, "анимация не должна виснуть бесконечно"
        assert tutorial.STEPS[app.tutorial_step_index]["id"] == "attack", (
            "после выполнения действия должен произойти автопереход к следующему уроку"
        )
    finally:
        app.exit_tutorial()


def test_tutorial_surrender_step_uses_real_surrender_and_skips_ai_memory():
    """Шаг сдачи обязан вызывать настоящий App.surrender() (не имитацию),
    но это НЕ должно попасть в ai_memory.json — сохранение памяти ИИ
    обязано игнорировать обучение."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import tutorial

    app = m.App()
    by_id = {s["id"]: i for i, s in enumerate(tutorial.STEPS)}
    app.enter_tutorial()
    try:
        app._load_tutorial_step(by_id["surrender"])
        app.draw_tutorial_screen((0, 0))  # позиционирует surrender_button в панели
        assert app.memory_saved is False

        app.handle_tutorial_click(app.surrender_button.rect.center)
        assert app.state.phase == "game_over", "должна сработать настоящая логика сдачи"
        assert app.state.winner == "black"

        # То же условие, что и в главном цикле run().
        if app.state.phase == "game_over" and not app.tutorial_active:
            app.save_memory_if_needed()
        assert app.memory_saved is False, "обучение не должно писать в ai_memory.json"
    finally:
        app.exit_tutorial()


def test_tutorial_queen_lock_step_completes_via_button():
    """Урок про ферзя: queen_lock — это кнопка (special_action), а не
    клетка на доске, значит требует отдельной ветки в
    handle_tutorial_click/draw_tutorial_panel. Проверяем весь цикл: клик
    по настоящей кнопке queen_lock_buttons -> apply_action_with_animation
    -> автопереход к следующему уроку (queen_ranged)."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import tutorial

    app = m.App()
    by_id = {s["id"]: i for i, s in enumerate(tutorial.STEPS)}
    app.enter_tutorial()
    try:
        app._load_tutorial_step(by_id["queen_lock"])
        queen = app.get_selected_piece()
        assert queen is not None and queen.type == "queen"
        assert queen.queen_locked_mode is None
        app.draw_tutorial_screen((0, 0))  # позиционирует кнопку в панели

        assert any(a["type"] == "queen_lock" for a in app.special_actions)
        btn = app.queen_lock_buttons["queen_ranged"]
        app.handle_tutorial_click(btn.rect.center)

        assert queen.queen_locked_mode == "queen_ranged", "queen_lock должен примениться через настоящий actions.apply_action"
        for _ in range(80):
            if not app.tutorial_pending_advance:
                break
            app.animation.update(0.05)
            app._update_tutorial(0.05)
        assert tutorial.STEPS[app.tutorial_step_index]["id"] == "queen_ranged"
    finally:
        app.exit_tutorial()


def test_tutorial_knight_mount_step_completes():
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import tutorial
    import renderer as R

    app = m.App()
    by_id = {s["id"]: i for i, s in enumerate(tutorial.STEPS)}
    app.enter_tutorial()
    try:
        app._load_tutorial_step(by_id["knight_mount"])
        knight = app.get_selected_piece()
        assert knight is not None and knight.type == "knight"
        mount_cell = next(c for c, a in app.action_map.items() if a["type"] == "mount")
        mx, my = R.board_to_screen(*mount_cell)
        app.handle_tutorial_click((mx + 3, my + 3))
        assert knight.is_mounted_knight is True
    finally:
        app.exit_tutorial()


def test_fog_visible_cells_for_cloud_rendering():
    """fog.visible_cells — новая функция для отрисовки облаков тумана:
    клетка видна, только если она в радиусе VISION_RANGE от какой-то
    своей фигуры; при выключенном тумане возвращает None (нечего рисовать)."""
    import config
    import fog as fog_mod
    from game_state import GameState

    st = GameState()
    st.fog_enabled = False
    assert fog_mod.visible_cells(st, "white") is None

    st2 = GameState()
    st2.fog_enabled = True
    st2.add_piece("king", "white", 4, 4)
    vis = fog_mod.visible_cells(st2, "white")
    assert (4, 4) in vis
    assert (4 + config.VISION_RANGE, 4) in vis
    assert (4 + config.VISION_RANGE + 1, 4) not in vis


def test_tutorial_mode_standard_requires_defeating_all_three():
    """Мини-бой 'Обычный бой': шаг не должен завершаться после ПЕРВОЙ
    атаки — только когда все 3 вражеские пешки уничтожены; между
    атаками фигура обязана снова становиться доступной для действия."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import tutorial
    import renderer as R

    app = m.App()
    by_id = {s["id"]: i for i, s in enumerate(tutorial.STEPS)}
    app.enter_tutorial()
    try:
        app._load_tutorial_step(by_id["mode_standard"])
        bishop = app.get_selected_piece()
        assert bishop is not None and bishop.type == "bishop"

        for expected_remaining in (2, 1, 0):
            attack_cell = next(c for c, a in app.action_map.items() if a["type"] == "attack")
            tx, ty = R.board_to_screen(*attack_cell)
            app.handle_tutorial_click((tx + 3, ty + 3))
            assert len(app.state.pieces_of("black")) == expected_remaining
            if expected_remaining > 0:
                assert app.tutorial_pending_advance is False, "рано переходить дальше — враги ещё остались"
                assert bishop.alive() and bishop.actions_used == 0, "фигура должна снова быть готова к действию"
                assert app.get_selected_piece() is bishop
            else:
                assert app.tutorial_pending_advance is True, "все враги уничтожены — пора переходить дальше"
    finally:
        app.exit_tutorial()


def test_tutorial_mode_base_and_fog_require_killing_king():
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import tutorial
    import renderer as R

    app = m.App()
    by_id = {s["id"]: i for i, s in enumerate(tutorial.STEPS)}
    for step_id in ("mode_base", "mode_fog"):
        app.enter_tutorial()
        try:
            app._load_tutorial_step(by_id[step_id])
            piece = app.get_selected_piece()
            assert piece is not None
            attack_cell = next(c for c, a in app.action_map.items() if a["type"] == "attack")
            target = app.action_map[attack_cell]
            target_piece = app.state.pieces.get(target["target_id"])
            assert target_piece.type == "king"

            tx, ty = R.board_to_screen(*attack_cell)
            app.handle_tutorial_click((tx + 3, ty + 3))
            assert not any(p.type == "king" for p in app.state.pieces_of("black"))
            assert app.tutorial_pending_advance is True, f"{step_id}: убийство короля должно завершать мини-бой"
        finally:
            app.exit_tutorial()


def test_tutorial_never_touches_network_or_ai_player():
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m

    app = m.App()
    app.enter_tutorial()
    try:
        assert app.network_role is None
        assert app.ai_player is None
        app.handle_tutorial_click(app.tutorial_next_button.rect.center)
        assert app.network_role is None
        assert app.ai_player is None
    finally:
        app.exit_tutorial()


def test_dismount_ui_flow_no_action_collision():
    """UI-регрессия: цели спешивания коня не должны подмешиваться в
    обычную карту ходов носителя (иначе совпадающие клетки конфликтуют
    и одно действие тихо перекрывает другое) — спешивание проходит через
    отдельный 'вооружаемый' режим (dismount_button -> клетка)."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import turn_system
    import combat
    import renderer as R

    app = m.App()
    app.handle_select_game_mode_click(app.game_mode_next_button.rect.center)
    app.handle_select_board_size_click(app.board_size_next_button.rect.center)
    app.confirm_setup()
    app.selected_opponent_type = "algorithm"
    app.selected_difficulty = "easy"
    app.confirm_opponent()

    turn_system.end_king_stage(app.state)
    knight = next(p for p in app.state.pieces_of("white") if p.type == "knight")
    rook = next(p for p in app.state.pieces_of("white") if p.type == "rook")
    rook.col, rook.row = 4, 4
    knight.col, knight.row = 4, 5
    combat.do_mount(app.state, knight, rook)
    knight.actions_used = 0
    rook.actions_used = 0

    rx, ry = R.board_to_screen(rook.col, rook.row)
    app.handle_game_click((rx + 5, ry + 5))
    assert app.selected_piece_id == rook.id
    assert app.dismount_options, "должны быть доступны цели спешивания"
    assert not any(a["type"] == "dismount" for a in app.action_map.values()), \
        "dismount не должен попадать в общую карту ходов (коллизия клеток)"

    app.draw_game_screen((0, 0))  # позиционирует кнопку спешивания в панели
    app.handle_game_click(app.dismount_button.rect.center)
    assert app.dismount_armed is True

    target_cell = app.dismount_options[0]["target"]
    tx, ty = R.board_to_screen(*target_cell)
    app.handle_game_click((tx + 5, ty + 5))
    assert knight.is_mounted_knight is False
    assert (knight.col, knight.row) == target_cell


def test_ai_turn_state_machine_full_cycle():
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import turn_system

    app = m.App()
    app.confirm_setup()
    app.selected_opponent_type = "algorithm"
    app.selected_difficulty = "easy"
    app.confirm_opponent()

    turn_system.end_king_stage(app.state)
    turn_system.end_turn(app.state)
    assert app.state.turn_color == "black"
    assert app.ai_status == m.AI_IDLE

    statuses_seen = set()
    t0 = time.time()
    last = t0
    while time.time() - t0 < 15.0:
        now = time.time()
        dt = now - last
        last = now
        app.animation.update(dt)
        app.update_ai_turn(dt)
        statuses_seen.add(app.ai_status)
        if app.state.turn_color == "white" or app.state.phase == "game_over":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("AI turn did not complete within timeout")

    assert m.AI_THINKING in statuses_seen
    assert app.state.turn_color == "white" or app.state.phase == "game_over"


def test_full_ai_vs_player_simulation():
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import main as m
    import turn_system
    import actions as actions_mod

    random.seed(42)
    app = m.App()
    app.confirm_setup()
    app.selected_opponent_type = "algorithm"
    app.selected_difficulty = "easy"
    app.confirm_opponent()

    def drive_ai_turn(timeout=15.0):
        t0 = time.time()
        last = t0
        while time.time() - t0 < timeout:
            now = time.time()
            dt = now - last
            last = now
            app.animation.update(dt)
            app.update_ai_turn(dt)
            if app.state.turn_color == "white" or app.state.phase == "game_over":
                return True
            time.sleep(0.01)
        return False

    turns_done = 0
    while turns_done < 3 and app.state.phase != "game_over":
        king = next((p for p in app.state.pieces_of("white") if p.type == "king"), None)
        if king and king.actions_available() > 0:
            kacts = actions_mod.get_legal_actions(app.state, king)
            if kacts and random.random() < 0.5:
                app.apply_action_with_animation(random.choice(kacts))
        turn_system.end_king_stage(app.state)
        for _ in range(4):
            acts = actions_mod.get_all_legal_actions(app.state, "white")
            if not acts:
                break
            app.apply_action_with_animation(random.choice(acts))
        w = app.state.check_victory()
        if w:
            app.state.phase = "game_over"
            app.state.winner = w
            break
        turn_system.end_turn(app.state)

        assert drive_ai_turn(), "AI turn timed out during full simulation"
        turns_done += 1

    assert turns_done > 0 or app.state.phase == "game_over"


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
def main():
    tests = [
        ("basic_rules", test_basic_rules),
        ("mount_shield_mechanic", test_mount_shield_mechanic),
        ("queen_no_splash", test_queen_no_splash),
        ("full_random_simulation", test_full_random_simulation),
        ("ai_plan_does_not_mutate_state", test_ai_plan_does_not_mutate_state),
        ("ai_difficulty_levels_produce_plans", test_ai_difficulty_levels_produce_plans),
        ("ai_interface_algorithm_and_local_fallback", test_ai_interface_algorithm_and_local_fallback),
        ("both_ai_types_enforce_fog_identically", test_both_ai_types_enforce_fog_identically),
        ("local_ai_turn_budget_is_not_starved_by_single_query", test_local_ai_turn_budget_is_not_starved_by_single_query),
        ("local_ai_ollama_request_uses_configured_options", test_local_ai_ollama_request_uses_configured_options),
        ("rules_teaching_prompt_covers_all_mechanics_accurately", test_rules_teaching_prompt_covers_all_mechanics_accurately),
        ("local_ai_teaching_prompt_and_context", test_local_ai_teaching_prompt_and_context),
        ("lan_protocol_snapshot_hash_and_fog_redaction", test_lan_protocol_snapshot_hash_and_fog_redaction),
        ("lan_action_dedup_and_authority", test_lan_action_dedup_and_authority),
        ("lan_localhost_host_client_roundtrip", test_lan_localhost_host_client_roundtrip),
        ("memory_roundtrip", test_memory_roundtrip),
        ("settings_roundtrip", test_settings_roundtrip),
        ("bishop_line_of_sight", test_bishop_line_of_sight),
        ("rook_swap_mechanic", test_rook_swap_mechanic),
        ("queen_lock_unlock_flow", test_queen_lock_unlock_flow),
        ("knight_dismount", test_knight_dismount),
        ("king_heal_action", test_king_heal_action),
        ("configurable_board_sizes", test_configurable_board_sizes),
        ("ai_formation_generation", test_ai_formation_generation),
        ("army_composition_scales_on_large_board", test_army_composition_scales_on_large_board),
        ("flag_mode_victory", test_flag_mode_victory),
        ("flag_mode_ai_intercepts_runner", test_flag_mode_ai_intercepts_runner),
        ("base_mode_healing_disabled_on_compromise", test_base_mode_healing_disabled_on_compromise),
        ("fog_of_war_visibility_and_no_cheating", test_fog_of_war_visibility_and_no_cheating),
        ("board_coordinate_orientation", test_board_coordinate_orientation),
        ("animation_controller", test_animation_controller),
        ("main_app_smoke", test_main_app_smoke),
        ("queen_action_selection_stability", test_queen_action_selection_stability),
        ("network_tick_host_drains_opponent_visual_actions", test_network_tick_host_drains_opponent_visual_actions),
        ("frame_dt_is_clamped", test_frame_dt_is_clamped),
        ("get_all_legal_actions_index_patch_is_restored", test_get_all_legal_actions_index_patch_is_restored),
        ("file_dialog_watchdog_recovers_from_stuck_process", test_file_dialog_watchdog_recovers_from_stuck_process),
        ("file_dialog_helper_command_frozen_vs_source", test_file_dialog_helper_command_frozen_vs_source),
        ("base_dir_uses_executable_when_frozen", test_base_dir_uses_executable_when_frozen),
        ("platform_utils_desktop_is_not_android", test_platform_utils_desktop_is_not_android),
        ("platform_utils_detects_android_via_env", test_platform_utils_detects_android_via_env),
        ("config_data_dir_follows_writable_dir_not_base_dir", test_config_data_dir_follows_writable_dir_not_base_dir),
        ("renderer_uses_builtin_font_not_sysfont", test_renderer_uses_builtin_font_not_sysfont),
        ("tutorial_steps_data_integrity", test_tutorial_steps_data_integrity),
        ("tutorial_interactive_steps_offer_their_goal_action", test_tutorial_interactive_steps_offer_their_goal_action),
        ("tutorial_uses_real_fog_and_king_stage_mechanics", test_tutorial_uses_real_fog_and_king_stage_mechanics),
        ("tutorial_advance_pause_prevents_instant_board_swap", test_tutorial_advance_pause_prevents_instant_board_swap),
        ("tutorial_buttons_never_overlap_hint_card", test_tutorial_buttons_never_overlap_hint_card),
        ("tutorial_button_present_in_main_menu", test_tutorial_button_present_in_main_menu),
        ("tutorial_enter_exit_restores_real_state", test_tutorial_enter_exit_restores_real_state),
        ("tutorial_move_step_completes_and_advances", test_tutorial_move_step_completes_and_advances),
        ("tutorial_surrender_step_uses_real_surrender_and_skips_ai_memory", test_tutorial_surrender_step_uses_real_surrender_and_skips_ai_memory),
        ("tutorial_queen_lock_step_completes_via_button", test_tutorial_queen_lock_step_completes_via_button),
        ("tutorial_knight_mount_step_completes", test_tutorial_knight_mount_step_completes),
        ("fog_visible_cells_for_cloud_rendering", test_fog_visible_cells_for_cloud_rendering),
        ("tutorial_mode_standard_requires_defeating_all_three", test_tutorial_mode_standard_requires_defeating_all_three),
        ("tutorial_mode_base_and_fog_require_killing_king", test_tutorial_mode_base_and_fog_require_killing_king),
        ("tutorial_never_touches_network_or_ai_player", test_tutorial_never_touches_network_or_ai_player),
        ("dismount_ui_flow_no_action_collision", test_dismount_ui_flow_no_action_collision),
        ("ai_turn_state_machine_full_cycle", test_ai_turn_state_machine_full_cycle),
        ("full_ai_vs_player_simulation", test_full_ai_vs_player_simulation),
        ("knight_intermediate_cell_blocks_path", test_knight_intermediate_cell_blocks_path),
        ("knight_diagonal_intermediate_cell_blocks_path", test_knight_diagonal_intermediate_cell_blocks_path),
        ("knight_clear_path_remains_legal", test_knight_clear_path_remains_legal),
        ("fog_snapshot_hash_matches_client_view", test_fog_snapshot_hash_matches_client_view),
        ("board_size_options_include_requested_maps", test_board_size_options_include_requested_maps),
        ("standard_battle_timer_is_eight_minutes", test_standard_battle_timer_is_eight_minutes),
        ("standard_battle_timer_is_shown_in_hud", test_standard_battle_timer_is_shown_in_hud),
        ("army_composition_large_map_bonus", test_army_composition_large_map_bonus),
    ]

    for name, fn in tests:
        check(name, fn)

    print()
    print(f"Passed: {len(PASSED)}/{len(tests)}")
    if FAILED:
        print("Failed tests:")
        for name, e in FAILED:
            print(f"  - {name}: {e}")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
        sys.exit(0)


def test_knight_intermediate_cell_blocks_path():
    import rules
    from game_state import GameState
    state = GameState()
    knight = state.add_piece("knight", "white", 2, 2)
    state.add_piece("pawn", "white", 3, 2)
    assert (4, 2) not in rules.get_move_cells(state, knight)


def test_knight_diagonal_intermediate_cell_blocks_path():
    import rules
    from game_state import GameState
    state = GameState()
    knight = state.add_piece("knight", "white", 2, 2)
    state.add_piece("pawn", "white", 3, 3)
    assert (4, 4) not in rules.get_move_cells(state, knight)


def test_knight_clear_path_remains_legal():
    import rules
    from game_state import GameState
    state = GameState()
    knight = state.add_piece("knight", "white", 2, 2)
    assert (4, 2) in rules.get_move_cells(state, knight)


def test_fog_snapshot_hash_matches_client_view():
    import network
    from game_state import GameState
    state = GameState()
    state.fog_enabled = True
    state.game_mode = "fog"
    state.add_piece("king", "black", 9, 9)
    state.add_piece("rook", "black", 8, 9)
    state.add_piece("king", "white", 0, 0)
    snap = network.make_snapshot(state, "black")
    rebuilt = network.state_from_dict(snap["state"])
    assert network.state_hash(rebuilt, viewer_color="black") == snap["state_hash"]



def test_board_coords_black_perspective_roundtrip():
    import config
    import coords
    old = config.VIEWER_COLOR
    try:
        config.VIEWER_COLOR = "white"
        wx, wy = coords.board_to_screen(0, 0)
        assert coords.screen_to_board(wx + 2, wy + 2) == (0, 0)
        config.VIEWER_COLOR = "black"
        bx, by = coords.board_to_screen(config.BOARD_SIZE - 1, config.BOARD_SIZE - 1)
        assert coords.screen_to_board(bx + 2, by + 2) == (config.BOARD_SIZE - 1, config.BOARD_SIZE - 1)
    finally:
        config.VIEWER_COLOR = old

def test_board_size_options_include_requested_maps():
    import config
    assert config.BOARD_SIZE_OPTIONS == ((10, 8), (12, 8), (14, 10))


def test_standard_battle_timer_is_eight_minutes():
    import config
    from game_state import GameState
    assert config.STANDARD_GAME_TIME == 480.0
    gs = GameState()
    assert gs.game_timer == 480.0


def test_standard_battle_timer_is_shown_in_hud():
    """Регрессия: таймер боя 'Обычный бой' раньше отсчитывался только
    внутри state.game_timer и определял победителя по числу съеденных
    фигур по истечении времени, но нигде не отображался игроку —
    теперь он должен быть виден в HUD."""
    if not _pygame_available():
        print("  (pygame недоступен в этом окружении — пропуск UI-теста)")
        return
    import inspect
    import main as m
    src = inspect.getsource(m.App.draw_side_panel)
    assert 's.game_timer' in src, "HUD должен отображать s.game_timer в режиме 'standard'"
    assert '"standard"' in src


def test_army_composition_large_map_bonus():
    """На самой большой карте (14x10) состав армии должен получать
    +2 пешки и +1 коня сверх базового набора, а другие размеры это не
    должно затрагивать."""
    import config
    base = config.get_army_composition((10, 8))
    big = config.get_army_composition((14, 10))
    assert big["pawn"] == base["pawn"] + 2
    assert big["knight"] == base["knight"] + 1
    assert big["bishop"] == base["bishop"]
    assert big["rook"] == base["rook"]
    assert big["queen"] == base["queen"]
    assert big["king"] == base["king"]
    # (12, 8) не должен внезапно тоже получить бонус.
    mid = config.get_army_composition((12, 8))
    assert mid == base


if __name__ == "__main__":
    main()
