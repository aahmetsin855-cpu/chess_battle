# -*- coding: utf-8 -*-
"""
Контроллер анимаций. Не содержит игровой логики — только визуальную
интерполяцию поверх уже применённых изменений GameState. Это позволяет
не трогать combat.py / game_state.py (логика уничтожается мгновенно
для целей правил, а визуально "смерть" фигуры показывается отдельным
эффектом на основе снимка её последних данных).

Использование:
    anim = AnimationController(speed=1.0)
    anim.start_move(piece_id, from_cell, to_cell)
    anim.start_attack(piece_id, from_cell, to_cell, ptype, mode)
    anim.spawn_impact(cell)
    anim.spawn_death(cell, color, ptype)

    # каждый кадр:
    anim.update(dt)
    anim.is_blocking()   # True, пока идёт "главная" анимация (можно
                          # придержать обработку следующего шага/клика)
"""
import math
import random
import config
import coords


class AnimationController:
    def __init__(self, speed=None):
        self.speed = speed if speed and speed > 0 else config.DEFAULT_ANIMATION_SPEED
        self.current = None     # блокирующая анимация (move/attack/mount)
        self.effects = []       # неблокирующие визуальные эффекты (impact/death)

    def set_speed(self, speed):
        self.speed = speed if speed and speed > 0 else config.DEFAULT_ANIMATION_SPEED

    def _dur(self, base):
        return max(0.03, base / self.speed)

    # ------------------------------------------------------------------
    # Запуск анимаций
    # ------------------------------------------------------------------
    def start_move(self, piece_id, from_cell, to_cell):
        self.current = {
            "kind": "move",
            "piece_id": piece_id,
            "from": from_cell,
            "to": to_cell,
            "elapsed": 0.0,
            "duration": self._dur(config.MOVE_ANIMATION_TIME),
        }

    def start_mount(self, piece_id, from_cell, to_cell):
        self.current = {
            "kind": "mount",
            "piece_id": piece_id,
            "from": from_cell,
            "to": to_cell,
            "elapsed": 0.0,
            "duration": self._dur(config.MOVE_ANIMATION_TIME),
        }

    def start_attack(self, piece_id, from_cell, to_cell, ptype, mode=None):
        self.current = {
            "kind": "attack",
            "piece_id": piece_id,
            "from": from_cell,
            "to": to_cell,
            "ptype": ptype,
            "mode": mode,
            "elapsed": 0.0,
            "duration": self._dur(config.ATTACK_ANIMATION_TIME),
        }

    def spawn_impact(self, cell, delay=0.0):
        self.effects.append({
            "kind": "impact",
            "cell": cell,
            "elapsed": -delay,
            "duration": self._dur(config.IMPACT_FLASH_TIME),
        })
        self.effects.append(self._make_particles(
            cell, config.COLOR_EFFECT_IMPACT, config.PARTICLE_COUNT_IMPACT, delay))

    def spawn_damage_number(self, cell, amount, offset_x=0.0, delay=0.0):
        """Плавающее число урона рядом с пострадавшей фигурой."""
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        self.effects.append({
            "kind": "damage_number",
            "cell": tuple(cell),
            "amount": amount,
            "offset_x": float(offset_x),
            "elapsed": -delay,
            "duration": self._dur(config.DAMAGE_NUMBER_LIFETIME),
        })

    def spawn_death(self, cell, color, ptype, delay=0.0):
        self.effects.append({
            "kind": "death",
            "cell": cell,
            "color": color,
            "ptype": ptype,
            "elapsed": -delay,
            "duration": self._dur(config.DEATH_ANIMATION_TIME),
        })
        tint = config.COLOR_PIECE_WHITE if color == config.PLAYER_COLOR else config.COLOR_PIECE_BLACK_OUTLINE
        self.effects.append(self._make_particles(
            cell, tint, config.PARTICLE_COUNT_DEATH, delay, speed_mult=1.3))

    def _make_particles(self, cell, color, count, delay, speed_mult=1.0):
        """Небольшой чисто-декоративный взрыв искр. Генерируется один раз при
        спавне (angle/speed фиксированы), позиция каждой частицы затем
        считается детерминированно из progress в EffectsRenderer — никакого
        мутируемого состояния кадр-в-кадр, поэтому контроллер остаётся
        простым value-object'ом, как и остальные эффекты."""
        particles = []
        for _ in range(count):
            angle = random.uniform(0, 2 * math.pi)
            speed = config.PARTICLE_SPEED * speed_mult * random.uniform(0.5, 1.0)
            particles.append((angle, speed))
        return {
            "kind": "particles",
            "cell": cell,
            "color": color,
            "particles": particles,
            "elapsed": -delay,
            "duration": self._dur(config.PARTICLE_LIFETIME),
        }

    # ------------------------------------------------------------------
    # Обновление / запросы состояния
    # ------------------------------------------------------------------
    def update(self, dt):
        if self.current is not None:
            self.current["elapsed"] += dt
            if self.current["elapsed"] >= self.current["duration"]:
                self.current = None
        still_alive = []
        for e in self.effects:
            e["elapsed"] += dt
            if e["elapsed"] < e["duration"]:
                still_alive.append(e)
        self.effects = still_alive

    def is_blocking(self):
        return self.current is not None

    def clear(self):
        self.current = None
        self.effects = []

    def get_piece_override_pos(self, piece_id):
        """Экранные координаты (x, y) верхнего левого угла клетки для
        анимируемой (перемещающейся) фигуры, либо None."""
        if self.current is None or self.current["kind"] not in ("move", "mount"):
            return None
        if self.current["piece_id"] != piece_id:
            return None
        progress = min(1.0, self.current["elapsed"] / self.current["duration"])
        progress = _ease_out_back(progress) if self.current["kind"] == "move" else _ease_out(progress)
        fx, fy = coords.board_to_screen(*self.current["from"])
        tx, ty = coords.board_to_screen(*self.current["to"])
        x = fx + (tx - fx) * progress
        y = fy + (ty - fy) * progress
        return x, y

    def get_piece_scale(self, piece_id):
        """Множитель масштаба для лёгкого squash & stretch — чисто
        декоративная "живость" анимации перемещения/атаки. Возвращает 1.0
        (без искажений), если фигура сейчас не анимируется. Рендер сам
        решает, как применить масштаб (PieceRenderer), контроллер только
        поставляет число."""
        if self.current is None or self.current["piece_id"] != piece_id:
            return 1.0
        progress = min(1.0, self.current["elapsed"] / self.current["duration"])
        if self.current["kind"] in ("move", "mount"):
            return 1.0 + config.MOVE_SQUASH_STRENGTH * math.sin(progress * math.pi)
        if self.current["kind"] == "attack":
            return 1.0 + (config.MOVE_SQUASH_STRENGTH * 0.6) * math.sin(progress * math.pi)
        return 1.0

    def get_attack_effect(self):
        if self.current is not None and self.current["kind"] == "attack":
            c = self.current
            progress = min(1.0, c["elapsed"] / c["duration"])
            return {
                "from": c["from"],
                "to": c["to"],
                "ptype": c["ptype"],
                "mode": c["mode"],
                "progress": progress,
            }
        return None

    def get_effects(self):
        result = []
        for e in self.effects:
            if e["elapsed"] < 0:
                continue
            progress = min(1.0, max(0.0, e["elapsed"] / e["duration"]))
            item = dict(e)
            item["progress"] = progress
            result.append(item)
        return result


def _ease_out(t):
    return 1 - (1 - t) * (1 - t)


def _ease_out_back(t):
    """Лёгкий "пружинистый" overshoot на приземлении — фигура чуть
    проскакивает клетку и мягко возвращается, вместо сухой линейной
    остановки. Константа подобрана консервативно (c1=0.9), чтобы
    перелёт был почти незаметен геометрически, но заметен как "вес"."""
    c1 = 0.9
    c3 = c1 + 1
    t -= 1
    return 1 + c3 * t * t * t + c1 * t * t
