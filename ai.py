# -*- coding: utf-8 -*-
"""
Полностью локальный ИИ (без нейросетей, без интернета).

Алгоритм:
- minimax с альфа-бета отсечением;
- итеративное углубление, ограниченное AI_THINK_TIME;
- transposition table (кэш позиций);
- сортировка ходов (атаки/захваты в приоритете);
- модель хода: каждый атомарный шаг (move/attack/mount/pass) — это один
  "полуход" в дереве поиска; ход стороны заканчивается действием 'pass',
  после чего право хода/использования переходит другой стороне. Это
  адекватно отражает механику игры: за один реальный ход можно
  использовать много фигур по одному разу каждую.

Оценка позиции учитывает: суммарный HP, число живых фигур, стоимость
фигур, состояние коней/mounted-бонус, лечение на домашней линии,
подвижность (число доступных действий) и грубую защиту короля.
"""
import time
import random

import config
import actions as actions_mod


class SearchTimeout(Exception):
    pass


def _state_hash(state):
    items = []
    for pid in sorted(state.pieces.keys()):
        p = state.pieces[pid]
        items.append((
            pid, p.type, p.color, p.hp, p.col, p.row,
            p.actions_used, p.mounted_knight_id, p.is_mounted_knight,
        ))
    return (tuple(items), state.turn_color)


def _center_bonus(piece):
    """Небольшой бонус за контроль центра доски — масштабируется под
    текущий (возможно увеличенный) размер доски."""
    c = (config.BOARD_WIDTH - 1) / 2.0
    dist = abs(piece.col - c) + abs(piece.row - c)
    max_dist = max(config.BOARD_WIDTH, config.BOARD_HEIGHT)  # нормировка под масштаб карты
    return max(0.0, 3.0 * (1.0 - dist / max_dist))


def _development_bonus(state, color, turn_number):
    """
    Эвристика "хорошего начала" (НЕ жёсткий scripted opening): в первые
    несколько ходов поощряет постепенный вывод фигур с домашних рядов и
    занятие более активных позиций, как в обычной шахматной стратегии
    (развитие фигур, контроль центра), без привязки к конкретным клеткам.
    """
    if turn_number > 10:
        return 0.0
    bonus = 0.0
    back_rows = (0, 1) if color == "white" else (config.BOARD_HEIGHT - 1, config.BOARD_HEIGHT - 2)
    fade = max(0.0, 1.0 - (turn_number - 1) / 10.0)
    for p in state.pieces_of(color):
        if p.type in ("king", "pawn"):
            continue
        if p.row not in back_rows:
            weight = 2.2 if p.type in ("knight", "bishop") else 1.4
            bonus += weight * fade
    return bonus


def _action_damage(state, action):
    piece = state.pieces.get(action.get("piece_id"))
    if piece is None:
        return 0
    if action.get("mode") == "queen_ranged":
        return config.QUEEN_RANGED_DAMAGE
    return piece.damage


def _attack_opportunity_value(state, color, my_actions=None):
    """Суммарная условная ценность вражеских фигур, которые я прямо сейчас
    могу атаковать — поощряет создание угроз и подготовку атаки."""
    total = 0.0
    acts = my_actions if my_actions is not None else actions_mod.get_all_legal_actions(state, color)
    for a in acts:
        if a["type"] != "attack":
            continue
        t = state.pieces.get(a["target_id"])
        if t is None:
            continue
        val = config.PIECE_VALUE.get(t.type, 10)
        if t.type == "king":
            val += 250
        dmg = _action_damage(state, a)
        if dmg >= t.hp:
            val *= 1.6  # выгодный размен / добивание
        total += val
    return total


def _threat_exposure_value(state, color, opp_actions=None):
    """Суммарная условная 'опасность', которой подвергаются мои фигуры —
    поощряет защиту важных фигур и избегание размена в свою пользу."""
    opp = "black" if color == "white" else "white"
    total = 0.0
    acts = opp_actions if opp_actions is not None else actions_mod.get_all_legal_actions(state, opp)
    for a in acts:
        if a["type"] != "attack":
            continue
        t = state.pieces.get(a["target_id"])
        if t is None or t.color != color:
            continue
        val = config.PIECE_VALUE.get(t.type, 10)
        if t.type == "king":
            val += 250
        dmg = _action_damage(state, a)
        if dmg >= t.hp:
            val *= 1.8  # моя фигура может погибнуть бесплатно — очень плохо
        total += val
    return total




def _heal_action_value(state, color, my_actions=None, threat_value=None):
    """Value of available healing relative to the urgency of the target.
    Healing is useful when it preserves an expensive/important unit and
    when the king can heal without immediately exposing itself.
    """
    total = 0.0
    acts = my_actions if my_actions is not None else actions_mod.get_all_legal_actions(state, color)
    for a in acts:
        if a.get("type") != "heal":
            continue
        king = state.pieces.get(a.get("piece_id"))
        target = state.pieces.get(a.get("target_id"))
        if king is None or target is None:
            continue
        missing = max(0, target.max_hp - target.hp)
        urgency = missing / max(1, target.max_hp)
        value = config.PIECE_VALUE.get(target.type, 10)
        if target.type == "king":
            value += 250
        # Stronger signal for lethal/near-lethal damage, but not a hard rule.
        tv = threat_value if threat_value is not None else _threat_exposure_value(state, color)
        safety = max(0.0, 1.0 - tv / 120.0)
        total += value * (0.5 + 1.8 * urgency) * (0.7 + 0.3 * safety)
    return total

def _mode_bonus(state, color):
    """
    Небольшие дополнительные слагаемые оценки для нестандартных режимов
    (см. config.GAME_MODES). Не переписывает базовую оценку — лишь
    добавляет мотивацию, специфичную для цели режима:

    - "flag": поощряет продвижение своей самой дальней фигуры к вражеской
      домашней линии (наступление), СИЛЬНО штрафует, если вражеская
      фигура приближается к нашей домашней линии (оборона — иначе ИИ
      может просто пропустить прорыв игрока, гоняясь за материалом), и
      дополнительно поощряет держать свою фигуру рядом с самым опасным
      вражеским "бегуном", чтобы реально перехватывать его, а не просто
      абстрактно проигрывать по счёту.
    - "base": штраф, если собственный тыл уже скомпрометирован (лечение
      недоступно) — мотивирует не подставлять тыл под удар.
    """
    mode = getattr(state, "game_mode", "standard")
    bonus = 0.0

    if mode == "flag":
        opp = "black" if color == "white" else "white"
        my_target_row = config.BLACK_HOME_ROW if color == "white" else config.WHITE_HOME_ROW
        my_home_row = config.WHITE_HOME_ROW if color == "white" else config.BLACK_HOME_ROW

        # Наступление: чем ближе моя самая продвинутая фигура к цели,
        # тем больше бонус (нелинейно — рывок к финишу особенно ценен).
        best_progress = 0.0
        for p in state.pieces_of(color):
            if p.type == "king" or p.is_mounted_knight:
                continue
            dist = abs(p.row - my_target_row)
            progress = max(0.0, max(config.BOARD_WIDTH, config.BOARD_HEIGHT) - dist)
            best_progress = max(best_progress, progress)
        bonus += best_progress * 0.7 + (best_progress ** 1.5) * 0.03

        # Оборона: враг, приближающийся к МОЕЙ домашней линии — это
        # прямая угроза поражения, а не просто размен фигур, поэтому
        # штраф весомее и тоже нелинейно растёт по мере приближения.
        # ВАЖНО: считаем угрозой только фигуры, уже перешедшие среднюю
        # линию карты в мою сторону — иначе любая вражеская фигура,
        # находящаяся у СВОЕЙ половины (например, чужой защитник рядом
        # со своим бегуном), ошибочно считается "рвущимся к финишу
        # бегуном" только по формальной близости к дальнему краю, что
        # порождает ложный сигнал и сбивает всю оценку.
        midline = config.BOARD_HEIGHT // 2
        worst_threat = 0.0
        runner = None
        for p in state.pieces_of(opp):
            if p.type == "king" or p.is_mounted_knight:
                continue
            dist = abs(p.row - my_home_row)
            if dist > midline:
                continue  # ещё не пересёк среднюю линию — не реальная угроза
            threat = max(0.0, max(config.BOARD_WIDTH, config.BOARD_HEIGHT) - dist)
            if threat > worst_threat:
                worst_threat = threat
                runner = p
        bonus -= worst_threat * 0.9 + (worst_threat ** 1.5) * 0.05

        # Перехват: поощряем держать СВОЮ фигуру рядом с самым опасным
        # вражеским "бегуном" — иначе ИИ видит угрозу в счёте, но не
        # предпринимает конкретных действий по защите зоны. Вес должен
        # быть достаточно большим, чтобы перевесить обычный штраф за
        # "подставленную под атаку фигуру" (иначе ИИ считает сближение
        # с бегуном невыгодным чисто по материальным причинам и не
        # блокирует его).
        if runner is not None:
            defenders = [p for p in state.pieces_of(color)
                         if p.type != "king" and not p.is_mounted_knight]
            if defenders:
                min_dist = min(max(abs(p.col - runner.col), abs(p.row - runner.row)) for p in defenders)
                closeness = max(0.0, 6.0 - min_dist)
                bonus += (closeness ** 1.6) * 3.0

    elif mode == "base":
        compromised = getattr(state, "base_compromised", {})
        if compromised.get(color):
            bonus -= 6.0
        opp = "black" if color == "white" else "white"
        if compromised.get(opp):
            bonus += 6.0

    return bonus


def eval_state(state, color, opening_bias_map=None):
    """
    Оценка позиции с точки зрения `color` (больше = лучше для color).

    Учитывает: ценность и HP фигур, состояние коней/mounted-бонус,
    лечение на домашней линии, защиту короля, контроль центра, развитие
    в начале партии, возможности атаки (свои и угрозы соперника),
    подвижность. `opening_bias_map`, если передан, — небольшая подсказка
    из памяти ИИ о ранее предпочитаемых первых фигурах (не читерство —
    это статистика собственного стиля ИИ, а не знание о ходах игрока).
    """
    opp = "black" if color == "white" else "white"
    score = 0.0

    for p in state.pieces.values():
        if not p.alive():
            continue
        sign = 1.0 if p.color == color else -1.0
        base_value = config.PIECE_VALUE.get(p.type, 10)
        val = base_value + p.hp * 3.0

        # Конь ценен не только своей формальной "силой", а прежде всего
        # тем, что даёт носителю доп. действие.
        if p.mounted_knight_id is not None:
            val += 14.0
        if p.is_mounted_knight:
            # "спрятанный" конь физически не действует сам по себе —
            # его ценность выражается через бонус носителя выше.
            val *= 0.4

        # Потенциал лечения на домашней линии
        home_row = config.WHITE_HOME_ROW if p.color == "white" else config.BLACK_HOME_ROW
        if p.row == home_row and p.hp < p.max_hp and not p.is_mounted_knight:
            val += 2.5

        # Контроль центра / развитие
        if p.type != "king":
            val += _center_bonus(p) * 0.55

        # Король — берегём
        if p.type == "king":
            threats = 0
            for e in state.pieces.values():
                if e.color != p.color and e.alive() and not e.is_mounted_knight:
                    if max(abs(e.col - p.col), abs(e.row - p.row)) <= 2:
                        threats += 1
            val -= threats * 4.5

        score += sign * val

    # Подвижность — грубый tie-break. Считаем легальные действия ОДИН
    # раз на сторону и переиспользуем их во всех эвристиках ниже —
    # раньше eval_state (вызывается на КАЖДОМ узле поиска) пересчитывал
    # get_all_legal_actions до 8 раз за вызов (см. _attack_opportunity_value/
    # _threat_exposure_value/_heal_action_value), что и было главной
    # причиной того, что "сложный" ИИ почти не отличался от "среднего":
    # обоим просто не хватало реального времени поиска, чтобы уйти
    # глубже одного-двух полуходов, так что оба упирались в один и тот
    # же потолок по времени и сходились к похожему (мелкому) качеству.
    my_actions = actions_mod.get_all_legal_actions(state, color)
    opp_actions = actions_mod.get_all_legal_actions(state, opp)
    score += 0.4 * (len(my_actions) - len(opp_actions))

    # Возможности атаки / угрозы — поощряет подготовку атаки и защиту
    # важных фигур, штрафует за подставленные под бесплатный удар фигуры.
    my_threat = _threat_exposure_value(state, color, opp_actions=opp_actions)
    opp_threat = _threat_exposure_value(state, opp, opp_actions=my_actions)
    score += 0.45 * _attack_opportunity_value(state, color, my_actions=my_actions)
    score -= 0.5 * my_threat
    # Лечение — отдельная стратегическая возможность, а не просто
    # следствие низкого HP. Оно конкурирует с атакой и позиционированием
    # через общую оценку позиции.
    score += 0.32 * _heal_action_value(state, color, my_actions=my_actions, threat_value=my_threat)
    score -= 0.20 * _heal_action_value(state, opp, my_actions=opp_actions, threat_value=opp_threat)

    # Развитие в начале партии (без жёсткого дебюта — просто эвристика)
    score += _development_bonus(state, color, state.turn_number)
    score -= _development_bonus(state, opp, state.turn_number)

    # Небольшая подсказка из памяти прошлых партий (см. ai_memory.py)
    if opening_bias_map and state.turn_number <= 6:
        for p in state.pieces_of(color):
            b = opening_bias_map.get(p.type)
            if b:
                score += b * 0.3

    # Специфика игрового режима (Flag / Base) — см. _mode_bonus()
    score += _mode_bonus(state, color)
    score -= _mode_bonus(state, opp)

    return score


def _order_actions(state, acts):
    def key(a):
        s = 0
        if a["type"] == "attack":
            s += 100
            target = state.pieces.get(a.get("target_id"))
            if target is not None:
                s += config.PIECE_VALUE.get(target.type, 10)
                if target.type == "king":
                    s += 200
        elif a["type"] == "mount":
            s += 40
        elif a["type"] == "rook_swap":
            target = state.pieces.get(a.get("target_id"))
            # Обмен особенно полезен для вывода тяжёлой фигуры/разгрузки
            # тесной позиции; небольшая награда не заставляет ИИ спамить им.
            if target is not None:
                s += 5 + max(0, config.PIECE_VALUE.get(target.type, 10) - config.PIECE_VALUE.get("rook", 24)) * 0.08
        elif a["type"] == "heal":
            target = state.pieces.get(a.get("target_id"))
            if target is not None:
                missing = max(0, target.max_hp - target.hp)
                s += 24 + missing * 4 + config.PIECE_VALUE.get(target.type, 10) * 0.5
        elif a["type"] == "move":
            s += 1
        return -s

    return sorted(acts, key=key)


class AI:
    def __init__(self, color, think_time=None, max_depth=None, opening_bias_map=None):
        self.color = color
        self.tt = {}
        self.think_time = think_time if think_time is not None else config.AI_THINK_TIME
        self.max_depth = max_depth if max_depth is not None else config.AI_MAX_SEARCH_DEPTH
        self.opening_bias_map = opening_bias_map
        self.deadline = time.time() + self.think_time

    def _eval(self, state, root_color):
        return eval_state(state, root_color, opening_bias_map=self.opening_bias_map)

    def _apply_step(self, state, action):
        """Применяет один атомарный шаг (или pass со сменой стороны/сбросом флагов)."""
        ns = state.clone_light()
        if action["type"] == "pass":
            new_color = "black" if ns.turn_color == "white" else "white"
            ns.turn_color = new_color
            for p in ns.pieces.values():
                if p.color == new_color:
                    p.actions_used = 0
        else:
            actions_mod.apply_action(ns, action)
        return ns

    def minimax(self, state, depth, alpha, beta, root_color):
        if time.time() > self.deadline:
            raise SearchTimeout()

        winner = state.check_victory()
        if winner:
            if winner == root_color:
                return 100000.0
            if winner == "draw":
                return 0.0
            return -100000.0

        if depth <= 0:
            return self._eval(state, root_color)

        h = _state_hash(state)
        cached = self.tt.get(h)
        if cached is not None and cached[0] >= depth:
            return cached[1]

        acts = actions_mod.get_all_legal_actions(state, state.turn_color)
        acts = _order_actions(state, acts)
        acts.append({"type": "pass"})
        acts = acts[: config.AI_BRANCH_LIMIT]

        maximizing = (state.turn_color == root_color)
        best = -1e9 if maximizing else 1e9

        for a in acts:
            ns = self._apply_step(state, a)
            val = self.minimax(ns, depth - 1, alpha, beta, root_color)
            if maximizing:
                if val > best:
                    best = val
                alpha = max(alpha, val)
            else:
                if val < best:
                    best = val
                beta = min(beta, val)
            if beta <= alpha:
                break

        if len(self.tt) < config.TRANSPOSITION_TABLE_MAX:
            self.tt[h] = (depth, best)
        return best

    def choose_action(self, state, think_time=None, restrict_actions=None):
        """Выбирает лучшее атомарное действие для state.turn_color."""
        budget = think_time if think_time is not None else self.think_time
        self.deadline = time.time() + max(0.05, budget)

        acts = restrict_actions
        if acts is None:
            acts = actions_mod.get_all_legal_actions(state, state.turn_color)
        if not acts:
            return {"type": "pass"}

        acts = _order_actions(state, acts)[: config.AI_BRANCH_LIMIT]
        best_action = acts[0]

        depth = 1
        try:
            while depth <= self.max_depth:
                alpha, beta = -1e9, 1e9
                best_val = -1e9
                cur_best = best_action
                for a in acts:
                    ns = self._apply_step(state, a)
                    val = self.minimax(ns, depth - 1, alpha, beta, state.turn_color)
                    if val > best_val:
                        best_val = val
                        cur_best = a
                    alpha = max(alpha, val)
                best_action = cur_best
                depth += 1
        except SearchTimeout:
            pass

        return best_action


def plan_ai_turn(state, color, difficulty="normal", opening_bias_map=None):
    """
    Строит ПЛАН всего хода ИИ (список атомарных действий) на облегчённой
    копии состояния, НЕ трогая переданный `state`. Это позволяет считать
    ход ИИ в фоновом потоке (main.py), а затем применять готовые
    действия к реальному состоянию по одному, с анимацией между ними.
    """
    settings = config.AI_DIFFICULTY_SETTINGS.get(difficulty, config.AI_DIFFICULTY_SETTINGS["normal"])
    think_time = settings["think_time"]
    max_depth = settings["max_depth"]

    ws = state.clone_light()
    ai = AI(color, think_time=think_time, max_depth=max_depth, opening_bias_map=opening_bias_map)
    plan = []

    # --- Стадия 1: король ---
    king = next((p for p in ws.pieces_of(color) if p.type == "king"), None)
    if king is not None and king.actions_available() > 0:
        king_actions = actions_mod.get_legal_actions(ws, king)
        if king_actions:
            current_eval = ai._eval(ws, color)
            best_action, best_val = None, current_eval
            for a in king_actions:
                ns = ws.clone_light()
                actions_mod.apply_action(ns, a)
                v = ai._eval(ns, color)
                if v > best_val:
                    best_val, best_action = v, a
            if best_action is not None:
                actions_mod.apply_action(ws, best_action)
                plan.append(best_action)

    ws.phase = "lobby_stage"

    # --- Стадия 2: lobby ---
    turn_deadline = time.time() + think_time
    for _ in range(config.AI_MAX_ACTIONS_PER_TURN):
        remaining = actions_mod.get_all_legal_actions(ws, color)
        if not remaining:
            break
        time_left = turn_deadline - time.time()
        if time_left <= 0.03:
            best_a, best_v = None, -1e18
            for a in remaining:
                ns = ws.clone_light()
                actions_mod.apply_action(ns, a)
                v = ai._eval(ns, color)
                if v > best_v:
                    best_v, best_a = v, a
            if best_a is None:
                break
            actions_mod.apply_action(ws, best_a)
            plan.append(best_a)
            continue
        step_budget = max(0.05, min(time_left, think_time / 6.0))
        action = ai.choose_action(ws, think_time=step_budget, restrict_actions=remaining)
        if action.get("type") == "pass":
            break
        ok = actions_mod.apply_action(ws, action)
        if not ok:
            break
        plan.append(action)

    return plan


def ai_take_turn(state, color, difficulty="normal"):
    """
    Совместимость с более ранней версией / тестами: считает план и сразу
    же применяет его целиком к переданному `state` (без анимации).
    """
    plan = plan_ai_turn(state, color, difficulty=difficulty)
    for action in plan:
        actions_mod.apply_action(state, action)
    state.phase = "lobby_stage"
    return state


# ---------------------------------------------------------------------------
# Расстановка ИИ (opening formation)
# ---------------------------------------------------------------------------
def _score_formation(layout, board_size):
    if isinstance(board_size, (tuple, list)):
        board_w, board_h = int(board_size[0]), int(board_size[1])
    else:
        board_w = board_h = int(board_size)
    """Оценивает формацию и доступность коней: случайно запертый конь
    получает заметный штраф, а несколько свободных направлений — бонус."""
    center_c = (board_w - 1) / 2.0
    occupied = {(c, r) for c, r, _ in layout}
    dirs8 = ((-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1))
    score = 0.0
    for c, r, t in layout:
        if t == "bishop":
            score += max(0.0, 2.0 - abs(c - center_c) * 0.3)
        elif t == "knight":
            score += max(0.0, 2.0 - abs(c - center_c) * 0.2)
            free_dirs = 0
            for dc, dr in dirs8:
                mid = (c + dc, r + dr)
                dst = (c + 2 * dc, r + 2 * dr)
                if (0 <= mid[0] < board_w and 0 <= mid[1] < board_h
                        and 0 <= dst[0] < board_w and 0 <= dst[1] < board_h
                        and mid not in occupied and dst not in occupied):
                    free_dirs += 1
            score += free_dirs * 1.8
            if free_dirs < 3:
                score -= 12.0
        elif t == "pawn":
            score += 1.0
        elif t in ("rook", "queen"):
            score += 1.0
    return score


def suggest_formation(color, deployment_rows, board_size, samples=None):
    """
    Строит несколько (config.AI_FORMATION_SAMPLES) случайных, но
    ОСМЫСЛЕННЫХ вариантов расстановки — пехота ближе к фронту (к центру
    карты), тяжёлые фигуры (ладья/ферзь) в тылу, слон/конь в средней
    линии зоны расстановки — и выбирает лучший по простой эвристике
    (`_score_formation`). Это НЕ жёсткий scripted opening: конкретные
    колонки для каждого варианта перемешиваются заново, поэтому итоговая
    расстановка меняется от партии к партии, сохраняя общую логику ролей.

    Возвращает список (col, row, piece_type).
    """
    samples = samples or config.AI_FORMATION_SAMPLES
    if isinstance(board_size, (tuple, list)):
        board_w, board_h = int(board_size[0]), int(board_size[1])
    else:
        board_w = board_h = int(board_size)
    types = []
    for t, cnt in config.get_army_composition(board_size).items():
        types += [t] * cnt

    lo, hi = deployment_rows
    rows = list(range(lo, hi + 1)) or [lo]
    if color == "white":
        front_row, back_row = rows[-1], rows[0]
    else:
        front_row, back_row = rows[0], rows[-1]
    mid_rows = rows[1:-1] if len(rows) > 2 else rows

    row_preference = {
        "pawn": front_row,
        "rook": back_row,
        "queen": back_row,
        "king": back_row,
        "bishop": mid_rows[len(mid_rows) // 2] if mid_rows else back_row,
        "knight": mid_rows[0] if mid_rows else back_row,
    }

    best_layout, best_score = None, -1e18
    for _ in range(samples):
        occupied = set()
        layout = []
        shuffled_types = list(types)
        random.shuffle(shuffled_types)
        for t in shuffled_types:
            preferred = row_preference.get(t, back_row)
            row_order = [preferred] + [r for r in rows if r != preferred]
            placed = False
            for r_try in row_order:
                cols = list(range(board_w))
                random.shuffle(cols)
                for c in cols:
                    if (c, r_try) not in occupied:
                        layout.append((c, r_try, t))
                        occupied.add((c, r_try))
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                # запасной вариант — первая свободная клетка зоны (не должно случаться)
                for r_try in rows:
                    for c in range(board_w):
                        if (c, r_try) not in occupied:
                            layout.append((c, r_try, t))
                            occupied.add((c, r_try))
                            placed = True
                            break
                    if placed:
                        break

        score = _score_formation(layout, (board_w, board_h))
        if score > best_score:
            best_score, best_layout = score, layout

    return best_layout
