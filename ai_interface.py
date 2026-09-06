# -*- coding: utf-8 -*-
"""
Интерфейс противника для HP Battle Chess.

AlgorithmAI:
  minimax + alpha-beta + iterative deepening + TT.

LocalAI:
  Ollama/локальная LLM, которая НЕ генерирует игровые действия напрямую.
  Она получает компактное "обучение" правилам, состояние позиции и список
  уже проверенных legal_actions[] и возвращает только индекс одного действия.
  Любой сбой/непонятный ответ -> безопасный fallback на эвристику.
"""
import json
import re
import time
import urllib.request

import config
import actions as actions_mod
import fog
from ai import plan_ai_turn, eval_state


RULES_TEACHING = """
You are playing HP Battle Chess, a custom tactical game inspired by chess.
This is NOT standard chess. Always follow the game state and legal-action list.

CORE RULES:
- Two armies: WHITE is the player, BLACK is the AI.
- Pieces have HP and damage. A piece with HP <= 0 is destroyed.
- A turn has a king stage first, then the main/free-action stage.
- Only one atomic action from legal_actions[] may be chosen at a time.
- Never invent an action, target, coordinate, or rule not represented in legal_actions[].
- Standard mode: destroy the entire enemy army. The match also has an 8-minute
  overall clock; if it runs out before either army is wiped, whoever has
  captured MORE enemy pieces so far wins (tied captures = draw). This means
  trading favorably for captures is a valid strategy even without going for
  full annihilation, especially late in the clock.
- Flag mode: reach the enemy home row with any non-mounted living piece —
  immediate win, army strength is irrelevant if you get a piece there first.
- Base mode: losing your king is an immediate loss, exactly like in Fog of War
  (see below) — protecting your own king matters as much as attacking.
  Additionally, if an enemy piece breaks into your home deployment zone, your
  side's passive home-row healing is disabled for the rest of the game — keep
  the back line defended, not just the front.
- Fog of War: hidden enemy pieces are absent from this state. The enemy king is
  always visible. Do NOT assume hidden pieces exist at any particular location.
  Your information is intentionally incomplete. Losing your own king is an
  immediate loss here too (same as Base mode), so guard it even though you
  can't see the whole board.

PIECE ROLES:
- Pawn: infantry. Best for forming a front, blocking lines of fire, surrounding
  targets, and protecting important pieces.
- Rook: tank/heavy unit. High HP and strong basic attack. Its special move
  (rook_swap) lets it instantly swap places with an adjacent friendly piece —
  use it to pull a wounded ally out of danger or to push the rook itself to
  the front line in one action.
- Bishop: ranged support. Attacks an area up to 5x5, but line-of-sight can be
  blocked by any piece between bishop and target.
- Queen: powerful flexible unit. MOVE is normal movement. RANGED is a special
  turret mode (locking into it, via queen_lock, is itself an action). While
  locked, the queen cannot move, only make a ranged attack or unlock; the lock
  persists across turns until explicitly unlocked, so a locked queen keeps
  firing turn after turn without needing to re-lock.
- Knight: fast special unit. It does not attack directly. It can mount a friendly
  piece, becoming a shield and giving the host an extra action. In Fog of War,
  its mobility makes it a natural scout.
- King: normal fighter plus a support action: heal an adjacent damaged friendly
  piece. Protecting and using the king well is important, and in Base/Fog modes
  losing him ends the game instantly regardless of the rest of your army.

TACTICAL PRINCIPLES:
- Prefer actions that create a concrete advantage: safe damage, a useful kill,
  protection, good positioning, blocking enemy fire, healing a valuable unit,
  or preparing a strong follow-up.
- Do not sacrifice a high-value unit for meaningless movement.
- Consider what the opponent can do next.
- Use the objective of the current mode, not generic chess ideas — a lone piece
  racing for the flag row, or a clock-aware capture trade in Standard mode, can
  outweigh a "stronger" but slower plan.
- In Fog of War, use only visible information, and never leave your own king
  exposed to an unseen approach.
"""

MODE_OBJECTIVES = {
    "standard": "STANDARD BATTLE: destroy the enemy army.",
    "flag": "FLAG: get any living non-mounted piece onto the enemy home row. Attack and defend around this objective.",
    "base": "BASE / FRONTLINE: protect your home zone while trying to break into the enemy home zone; a broken base disables that side's passive healing.",
    "fog": "FOG OF WAR: destroy the enemy army while using limited vision; hidden enemy pieces are unknown and the enemy king is always visible.",
}

PIECE_NAMES = {
    "pawn": "pawn",
    "king": "king",
    "bishop": "bishop",
    "rook": "rook",
    "queen": "queen",
    "knight": "knight",
}


class AIPlayer:
    def choose_turn_plan(self, state, color):
        raise NotImplementedError

    def describe(self):
        return "AI"

    def _fogged_snapshot(self, state, color):
        """Единственная и ОБЯЗАТЕЛЬНАЯ точка применения тумана войны для
        любого AIPlayer. Всегда безопасно вызывать: fog.build_fogged_view
        сама ничего не убирает, если state.fog_enabled выключен (просто
        клонирует state), поэтому это не меняет поведение вне тумана."""
        return fog.build_fogged_view(state, color)


class AlgorithmAI(AIPlayer):
    def __init__(self, difficulty="normal", opening_bias_map=None):
        self.difficulty = difficulty
        self.opening_bias_map = opening_bias_map

    def choose_turn_plan(self, state, color):
        ws = self._fogged_snapshot(state, color)
        return plan_ai_turn(
            ws, color, difficulty=self.difficulty,
            opening_bias_map=self.opening_bias_map,
        )

    def describe(self):
        names = {"easy": "Лёгкая", "normal": "Средняя", "hard": "Сложная"}
        return f"Algorithm AI ({names.get(self.difficulty, self.difficulty)})"


def check_ollama_available(timeout=1.5):
    """Быстрая проверка локального Ollama. Ошибки наружу не выбрасывает."""
    if not config.LOCAL_AI_ENABLED:
        return False
    try:
        req = urllib.request.Request(config.LOCAL_AI_HOST.rstrip("/") + "/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


class LocalAI(AIPlayer):
    def __init__(self, model=None, timeout=None, difficulty="normal", opening_bias_map=None):
        self.model = model or config.LOCAL_AI_MODEL
        self.timeout = timeout or config.LOCAL_AI_TIMEOUT
        self.difficulty = difficulty
        self.opening_bias_map = opening_bias_map
        self.available = check_ollama_available()
        self._fallback = AlgorithmAI(
            difficulty=difficulty,
            opening_bias_map=opening_bias_map,
        )

    def describe(self):
        if self.available:
            return f"Local AI ({self.model})"
        return "Local AI (Ollama недоступна -> Algorithm AI)"

    def choose_turn_plan(self, state, color):
        if not self.available:
            return self._fallback.choose_turn_plan(state, color)

        ws = self._fogged_snapshot(state, color)
        plan = []
        deadline = time.time() + config.LOCAL_AI_TURN_BUDGET

        # Этап короля: сначала выбираем одно действие короля, если оно доступно.
        king = next((p for p in ws.pieces_of(color) if p.type == "king"), None)
        if king is not None and king.actions_available() > 0:
            acts = actions_mod.get_legal_actions(ws, king)
            if acts:
                choice = self._decide(ws, acts, color, deadline)
                if choice is not None and actions_mod.apply_action(ws, choice):
                    plan.append(choice)

        ws.phase = "lobby_stage"

        # Затем выбираем атомарные действия основного этапа.
        for _ in range(config.AI_MAX_ACTIONS_PER_TURN):
            if time.time() > deadline:
                break
            acts = actions_mod.get_all_legal_actions(ws, color)
            if not acts:
                break
            choice = self._decide(ws, acts, color, deadline)
            if choice is None or not actions_mod.apply_action(ws, choice):
                break
            plan.append(choice)

        return plan

    def _decide(self, state, legal_actions, color, deadline):
        time_left = deadline - time.time()
        if time_left > 0.5:
            idx = self._query_ollama(
                state,
                legal_actions,
                color,
                timeout=min(self.timeout, time_left),
            )
            if idx is not None and 0 <= idx < len(legal_actions):
                return legal_actions[idx]
        return self._best_by_eval(state, legal_actions, color)

    def _best_by_eval(self, state, legal_actions, color):
        best, best_v = None, -1e18
        for a in legal_actions:
            ns = state.clone_light()
            if not actions_mod.apply_action(ns, a):
                continue
            v = eval_state(ns, color)
            if v > best_v:
                best_v, best = v, a
        return best

    def _query_ollama(self, state, legal_actions, color, timeout):
        try:
            system_prompt = RULES_TEACHING.strip()
            prompt = self._build_prompt(state, legal_actions, color)

            payload = json.dumps({
                "model": self.model,
                "system": system_prompt,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": config.LOCAL_AI_TEMPERATURE,
                    "num_predict": config.LOCAL_AI_NUM_PREDICT,
                },
            }).encode("utf-8")

            req = urllib.request.Request(
                config.LOCAL_AI_HOST.rstrip("/") + "/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            text = data.get("response", "")
            # Модель должна отвечать одним индексом. Берём первое целое,
            # остальной текст игнорируем; сам индекс всё равно проверяется.
            m = re.search(r"\b(\d+)\b", text)
            if m:
                return int(m.group(1))
        except Exception:
            # Ollama могла быть доступна при старте, но перестать отвечать.
            # В этом случае текущий шаг продолжается локальной эвристикой.
            pass
        return None

    # ------------------------------------------------------------------
    # Составление контекста для LLM
    # ------------------------------------------------------------------

    def _build_prompt(self, state, legal_actions, color):
        mode = getattr(state, "game_mode", "standard")
        turn = getattr(state, "turn_number", 1)
        phase = getattr(state, "phase", "lobby_stage")
        fog = getattr(state, "fog_enabled", False)

        lines = [
            "CURRENT MATCH STATE",
            f"Side you control: {color.upper()}",
            f"Turn number: {turn}",
            f"Turn phase: {phase}",
            f"Game mode: {mode.upper()}",
            f"Objective: {MODE_OBJECTIVES.get(mode, MODE_OBJECTIVES['standard'])}",
            f"Fog of War enabled: {'YES' if fog else 'NO'}",
            "",
        ]
        lines.extend(self._build_ascii_board(state, color))
        lines.append("")
        lines.append("YOUR ARMY:")

        for p in self._sorted_pieces(state.pieces_of(color)):
            lines.append(self._piece_line(p))

        lines.append("")
        lines.append("VISIBLE ENEMY UNITS:")
        enemy = "black" if color == "white" else "white"
        enemies = self._sorted_pieces(state.pieces_of(enemy))
        if enemies:
            for p in enemies:
                lines.append(self._piece_line(p, enemy=True))
        else:
            lines.append("No enemy units are currently visible.")

        lines.append("")
        lines.append("RECENT ACTIONS:")
        history = getattr(state, "action_history", [])[-6:]
        if history:
            for i, a in enumerate(history, 1):
                lines.append(f"{i}. {self._history_action_text(state, a)}")
        else:
            lines.append("No recent actions.")

        lines.append("")
        lines.append("TACTICAL NOTES:")
        notes = self._tactical_notes(state, color, legal_actions)
        if notes:
            lines.extend(f"- {n}" for n in notes)
        else:
            lines.append("- No special tactical warning detected.")

        lines.append("")
        lines.append("LEGAL ACTIONS:")
        lines.append("Choose exactly ONE index from this list.")
        lines.append("Do not output an explanation. Output ONLY the integer index.")

        for i, action in enumerate(legal_actions):
            lines.append(f"{i}: {self._describe_action(state, action)}")

        return "\n".join(lines)

    def _build_ascii_board(self, state, color):
        """ASCII-схема всей доски — даёт модели геометрию поля напрямую,
        а не только текстовые координаты. Подробности (HP/урон/статусы)
        по-прежнему идут отдельным списком ниже (см. _build_prompt) —
        схема даёт форму позиции, список даёт числа.

        Использует state.get_piece_at (та же видимость, что и у самой
        игры): "спрятанный" конь-наездник не рисуется отдельной буквой —
        он не отображается сам по себе, вместо этого учтён у носителя
        (см. _piece_line: mounted-knight-shield)."""
        letters = {"king": "K", "queen": "Q", "rook": "R",
                   "bishop": "B", "knight": "N", "pawn": "P"}
        size = getattr(state, "board_size", (config.BOARD_WIDTH, config.BOARD_HEIGHT))
        if isinstance(size, (tuple, list)):
            width, height = int(size[0]), int(size[1])
        else:
            width = height = int(size)
        enemy = "black" if color == "white" else "white"

        lines = [
            f"BOARD {width}x{height}",
            f"{color.capitalize()} = you",
            f"{enemy.capitalize()} = enemy",
            "Uppercase = your piece, lowercase = enemy piece, '.' = empty cell.",
            "Row 0 is the top row of this diagram; column 0 is the left column.",
            "",
        ]
        for r in range(height):
            row_chars = []
            for c in range(width):
                p = state.get_piece_at(c, r)
                if p is None:
                    row_chars.append(".")
                else:
                    ch = letters.get(p.type, "?")
                    row_chars.append(ch if p.color == color else ch.lower())
            lines.append("".join(row_chars))
        return lines

    def _sorted_pieces(self, pieces):
        order = {"king": 0, "queen": 1, "rook": 2, "bishop": 3, "knight": 4, "pawn": 5}
        return sorted(pieces, key=lambda p: (order.get(p.type, 99), p.id))

    def _piece_line(self, piece, enemy=False):
        flags = []
        if piece.mounted_knight_id is not None:
            flags.append("mounted-knight-shield")
        if piece.is_mounted_knight:
            flags.append(f"mounted-on={piece.host_id}")
        if piece.queen_locked_mode:
            flags.append(f"queen-locked={piece.queen_locked_mode}")

        hp = f"HP {piece.hp}/{piece.max_hp}"
        pos = f"pos=({piece.col},{piece.row})"
        role = self._role_text(piece.type)
        suffix = f"; {', '.join(flags)}" if flags else ""
        side = "enemy" if enemy else "ally"
        return f"- id={piece.id} {side} {piece.type}: {role}; {hp}; damage={piece.damage}; {pos}{suffix}"

    def _role_text(self, piece_type):
        return {
            "pawn": "infantry/frontline/blocking fire",
            "rook": "tank/heavy attacker",
            "bishop": "ranged support/line of sight",
            "queen": "powerful flexible turret or mobile unit",
            "knight": "scout/mount/shield",
            "king": "fighter/healer/support",
        }.get(piece_type, "unit")

    def _describe_action(self, state, action):
        piece = state.pieces.get(action.get("piece_id"))
        pname = PIECE_NAMES.get(piece.type, "?") if piece else "?"
        pid = piece.id if piece else action.get("piece_id", "?")

        kind = action.get("type")
        if kind == "move":
            return f"move {pname}_{pid} to {tuple(action['target'])}"
        if kind == "attack":
            target = state.pieces.get(action.get("target_id"))
            if target is None:
                return f"attack target_{action.get('target_id')} with {pname}_{pid}"
            return (
                f"attack {target.type}_{target.id} with {pname}_{pid} "
                f"(mode={action.get('mode')}, target HP={target.hp}/{target.max_hp})"
            )
        if kind == "rook_swap":
            target = state.pieces.get(action.get("target_id"))
            t = f"{target.type}_{target.id}" if target else f"piece_{action.get('target_id')}"
            return f"swap rook_{pid} with friendly {t}"
        if kind == "rook_swap":
            return f"{pname} swapped places with piece_{action.get('target_id')}"
        if kind == "mount":
            target = state.pieces.get(action.get("target_id"))
            t = f"{target.type}_{target.id}" if target else f"piece_{action.get('target_id')}"
            return f"mount knight_{pid} on friendly {t}"
        if kind == "dismount":
            return f"dismount knight_{pid} to {tuple(action['target'])}"
        if kind == "heal":
            target = state.pieces.get(action.get("target_id"))
            t = f"{target.type}_{target.id}" if target else f"piece_{action.get('target_id')}"
            return f"heal friendly {t} with king_{pid}"
        if kind == "queen_lock":
            return f"lock queen_{pid} into {action.get('mode')} turret mode"
        if kind == "queen_unlock":
            return f"unlock queen_{pid} and return to mobile mode"
        return str(action)

    def _history_action_text(self, state, action):
        # Историю используем только как контекст. Даже если конкретная цель
        # уже уничтожена, текст остаётся полезным как краткий факт прошлого.
        piece = state.pieces.get(action.get("piece_id"))
        pname = f"{piece.type}_{piece.id}" if piece else f"piece_{action.get('piece_id')}"
        kind = action.get("type")

        if kind == "move":
            return f"{pname} moved to {tuple(action.get('target', ())) }"
        if kind == "attack":
            target = state.pieces.get(action.get("target_id"))
            tname = f"{target.type}_{target.id}" if target else f"piece_{action.get('target_id')}"
            return f"{pname} attacked {tname} with {action.get('mode')}"
        if kind == "mount":
            return f"{pname} mounted on piece_{action.get('target_id')}"
        if kind == "dismount":
            return f"{pname} dismounted to {tuple(action.get('target', ()))}"
        if kind == "heal":
            return f"{pname} healed piece_{action.get('target_id')}"
        if kind == "queen_lock":
            return f"{pname} selected {action.get('mode')} turret mode"
        if kind == "queen_unlock":
            return f"{pname} unlocked turret mode"
        return str(action)

    def _tactical_notes(self, state, color, legal_actions):
        notes = []
        enemy = "black" if color == "white" else "white"

        # 1) Прямые угрозы: какие наши фигуры сейчас могут быть атакованы
        try:
            enemy_actions = actions_mod.get_all_legal_actions(state, enemy)
            threatened = {}
            for a in enemy_actions:
                if a.get("type") == "attack" and a.get("target_id") is not None:
                    threatened[a["target_id"]] = threatened.get(a["target_id"], 0) + 1
            for pid, count in list(threatened.items())[:4]:
                p = state.pieces.get(pid)
                if p is not None:
                    notes.append(
                        f"{p.type}_{p.id} is currently threatened by about {count} legal enemy attack(s)."
                    )
        except Exception:
            pass

        # 2) Полезные действия поддержки
        heal_count = sum(1 for a in legal_actions if a.get("type") == "heal")
        mount_count = sum(1 for a in legal_actions if a.get("type") == "mount")
        if heal_count:
            notes.append("A king heal is available; compare healing a valuable damaged ally with attacking.")
        if mount_count:
            notes.append("A knight can mount a friendly piece; this can create a shield and an extra action.")

        # 3) Queen state
        queens = [p for p in state.pieces_of(color) if p.type == "queen"]
        for q in queens:
            if q.queen_locked_mode:
                notes.append(f"Queen_{q.id} is locked in {q.queen_locked_mode} mode and cannot move until it attacks or unlocks.")
            else:
                lock_count = sum(
                    1 for a in legal_actions
                    if a.get("piece_id") == q.id and a.get("type") == "queen_lock"
                )
                if lock_count:
                    notes.append(f"Queen_{q.id} can enter a turret mode, trading mobility for a future special attack.")

        # 4) Mode-specific advice
        mode = getattr(state, "game_mode", "standard")
        if mode == "flag":
            target_row = config.WHITE_HOME_ROW if color == "black" else config.BLACK_HOME_ROW
            candidates = [p for p in state.pieces_of(color) if not p.is_mounted_knight and p.type != "king"]
            if candidates:
                best = min(candidates, key=lambda p: abs(p.row - target_row))
                notes.append(f"Flag runner candidate: {best.type}_{best.id} is closest to the enemy home row.")

        elif mode == "base":
            compromised = getattr(state, "base_compromised", {})
            notes.append(
                "Base status: "
                + ("YOUR BASE COMPROMISED." if compromised.get(color) else "your base is intact.")
            )
            notes.append(
                "Enemy base status: "
                + ("enemy base compromised." if compromised.get(enemy) else "enemy base is intact.")
            )

        elif mode == "fog":
            visible = [
                p for p in state.pieces_of(enemy)
                if p.type == "king" or self._is_visible_enemy(state, p, color)
            ]
            nonking = [p for p in visible if p.type != "king"]
            if not nonking:
                notes.append("Most enemy forces are currently hidden. Avoid assuming exact hidden positions.")
            else:
                notes.append(f"Visible non-king enemy units: {len(nonking)}. Hidden forces may still exist elsewhere.")

        return notes[:8]

    def _is_visible_enemy(self, state, enemy_piece, color):
        if not getattr(state, "fog_enabled", False):
            return True
        friendly = state.pieces_of(color)
        return any(
            max(abs(enemy_piece.col - f.col), abs(enemy_piece.row - f.row)) <= config.VISION_RANGE
            for f in friendly
        )


def create_ai_player(ai_type, difficulty="normal", opening_bias_map=None):
    if ai_type == "local":
        return LocalAI(
            difficulty=difficulty,
            opening_bias_map=opening_bias_map,
        )
    return AlgorithmAI(
        difficulty=difficulty,
        opening_bias_map=opening_bias_map,
    )
