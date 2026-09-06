# -*- coding: utf-8 -*-
"""
Данные интерактивного обучения ("Обучение" в главном меню).

Этот модуль НЕ содержит своей копии шахматных правил — он только
расставляет фигуры на настоящем GameState через настоящий
GameState.add_piece(), а проверка ходов/атак и их применение в main.py
идут через настоящие rules.py/actions.py/combat.py, как и в обычной
игре. Здесь нет pygame-зависимостей — вся отрисовка и обработка кликов
находится в main.py (App.draw_tutorial_screen/handle_tutorial_click),
как ai.py (логика) отделён от ai_interface.py (обвязка).

Каждый шаг:
  id       — короткий уникальный идентификатор шага.
  title    — заголовок карточки-подсказки.
  text     — 1-3 коротких предложения, объясняющих механику.
  setup(state) — расставляет фигуры/режим на СВЕЖЕМ GameState.
  goal     — что должен сделать игрок, чтобы шаг засчитался:
             None           — только кнопка "Далее" (чисто информационный шаг)
             "move"         — обычный ход
             "attack"       — атака (в т.ч. дальняя атака ферзя/слона — это тоже "attack")
             "rook_swap"    — обмен местами ладьи с союзником
             "mount"        — конь садится на союзника (щит + доп. действие)
             "queen_lock"   — ферзь включает режим турели (кнопка, не клетка доски)
             "king_action"  — любое действие короля (ход или лечение)
             "surrender"    — сдаться (через настоящую App.surrender())
  select   — (col, row) фигуры, которую нужно выбрать автоматически в
             начале шага, чтобы подсветка разрешённых действий была
             видна сразу, без лишнего клика.
  is_complete(state) — необязательный колбэк для шагов-мини-боёв
             (несколько действий подряд, напр. "уничтожь всех"): пока
             возвращает False, действовавшая фигура получает действие
             заново вместо перехода к следующему шагу.
"""
import config


def _welcome(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("king", "white", 2, 4)


def _intro_pawn(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("pawn", "white", 2, 5)


def _intro_knight(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("knight", "white", 2, 5)


def _intro_bishop(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("bishop", "white", 2, 5)


def _intro_rook(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("rook", "white", 2, 5)


def _intro_queen(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("queen", "white", 2, 5)


def _intro_king(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("king", "white", 2, 5)


def _select_move(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("pawn", "white", 2, 5)


def _attack(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("rook", "white", 2, 5)
    state.add_piece("pawn", "black", 2, 4)


def _rook_swap(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("rook", "white", 2, 5)
    state.add_piece("pawn", "white", 3, 5)


def _king_stage(state):
    state.phase = "king_stage"
    state.turn_color = "white"
    state.add_piece("king", "white", 2, 5)
    wounded = state.add_piece("pawn", "white", 3, 5)
    wounded.hp = max(1, wounded.hp - 2)


def _surrender(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("king", "white", 2, 5)
    state.add_piece("king", "black", 7, 2)


def _bishop_attack(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("bishop", "white", 2, 5)
    # Не соседняя клетка — специально показать, что слон бьёт по площади
    # (5x5, радиус config.BISHOP_RANGE), а не только вплотную, как пешка/ладья.
    state.add_piece("pawn", "black", 4, 5)


def _knight_mount(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("knight", "white", 2, 5)
    state.add_piece("rook", "white", 3, 5)


def _queen_lock(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("queen", "white", 2, 5)
    state.add_piece("pawn", "black", 2, 3)


def _queen_ranged(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    queen = state.add_piece("queen", "white", 2, 5)
    # Уже в режиме турели — показываем результат предыдущего шага сразу,
    # не заставляя игрока проходить lock ещё раз ради демонстрации атаки.
    queen.queen_locked_mode = "queen_ranged"
    state.add_piece("pawn", "black", 2, 3)


def _mode_standard(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.game_mode = "standard"
    state.add_piece("bishop", "white", 4, 5)
    # Три слабые цели в радиусе слона — добиваются по одной атаке каждая,
    # так и должна ощущаться победа в обычном режиме: уничтожить всю армию.
    for c, r in ((4, 3), (2, 5), (6, 5)):
        p = state.add_piece("pawn", "black", c, r)
        p.hp = 1


def _mode_flag(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.game_mode = "flag"
    state.add_piece("pawn", "white", 3, config.BLACK_HOME_ROW - 1)
    state.add_piece("king", "white", 2, 5)
    state.add_piece("king", "black", 7, config.BLACK_HOME_ROW)


def _mode_base(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.game_mode = "base"
    state.add_piece("rook", "white", 4, 5)
    king = state.add_piece("king", "black", 4, 4)  # рядом — сразу в радиусе атаки
    king.hp = 1  # мини-бой короткий: одна атака решает исход


def _mode_fog(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.game_mode = "fog"
    state.fog_enabled = True
    state.add_piece("rook", "white", 4, 5)
    king = state.add_piece("king", "black", 4, 4)  # видим всегда — цель мини-боя
    king.hp = 1
    # Спрятанная в тумане пешка — не обязательна к уничтожению, нужна
    # только чтобы увидеть само облако тумана рядом с целью.
    far_col = min(config.BOARD_WIDTH - 1, 9)
    far_row = max(0, config.BOARD_HEIGHT - 1)
    state.add_piece("pawn", "black", far_col, far_row)


def _finish(state):
    state.phase = "lobby_stage"
    state.turn_color = "white"
    state.add_piece("king", "white", 2, 4)


STEPS = [
    {
        "id": "welcome",
        "title": "Добро пожаловать",
        "text": "Короткое интерактивное обучение — от выбора фигуры до сдачи партии. Жмите «Далее» внизу справа.",
        "setup": _welcome,
        "goal": None,
        "select": None,
    },
    {
        "id": "intro_pawn",
        "title": "Пешка",
        "text": "HP 6, урон 1. Ходит и атакует на одну клетку в любом направлении — самая простая фигура.",
        "setup": _intro_pawn,
        "goal": None,
        "select": (2, 5),
    },
    {
        "id": "intro_knight",
        "title": "Конь",
        "text": "HP 2, атаки нет. Ходит на 2 клетки по прямой; вместо удара — садится на союзника и защищает его.",
        "setup": _intro_knight,
        "goal": None,
        "select": (2, 5),
    },
    {
        "id": "intro_bishop",
        "title": "Слон",
        "text": "HP 4, урон 2. Атакует по площади — на 2 клетки в любую сторону, не только вплотную.",
        "setup": _intro_bishop,
        "goal": None,
        "select": (2, 5),
    },
    {
        "id": "intro_rook",
        "title": "Ладья",
        "text": "HP 12, урон 3. Самая живучая фигура; умеет меняться местами с соседним союзником.",
        "setup": _intro_rook,
        "goal": None,
        "select": (2, 5),
    },
    {
        "id": "intro_queen",
        "title": "Ферзь",
        "text": "HP 10, урон 3. Может встать в режим турели и бить на 2 клетки, но тогда не может двигаться.",
        "setup": _intro_queen,
        "goal": None,
        "select": (2, 5),
    },
    {
        "id": "intro_king",
        "title": "Король",
        "text": "HP 6, урон 1. У него отдельная стадия хода и способность лечить раненых соседей.",
        "setup": _intro_king,
        "goal": None,
        "select": (2, 5),
    },
    {
        "id": "select_move",
        "title": "Выбор и ход",
        "text": "Пешка обведена белой рамкой — значит выбрана. Серые клетки — куда она может сходить. Нажмите любую серую клетку.",
        "setup": _select_move,
        "goal": "move",
        "select": (2, 5),
    },
    {
        "id": "attack",
        "title": "Атака",
        "text": "Ладья выбрана (белая рамка). Красная клетка — вражеская пешка в радиусе атаки. Нажмите на неё, чтобы ударить.",
        "setup": _attack,
        "goal": "attack",
        "select": (2, 5),
    },
    {
        "id": "rook_swap",
        "title": "Особая механика ладьи",
        "text": "Ладья выбрана. Фиолетовая клетка — союзная пешка рядом. Нажмите на неё: ладья и пешка поменяются местами.",
        "setup": _rook_swap,
        "goal": "rook_swap",
        "select": (2, 5),
    },
    {
        "id": "bishop_attack",
        "title": "Атака слона по площади",
        "text": "У слона радиус атаки больше — 2 клетки в любую сторону, не только вплотную. Нажмите красную клетку с врагом.",
        "setup": _bishop_attack,
        "goal": "attack",
        "select": (2, 5),
    },
    {
        "id": "knight_mount",
        "title": "Конь — живой щит",
        "text": "Фиолетовая клетка — союзник рядом. Конь может «сесть» на него: закроет его собой и даст доп. действие. Нажмите на клетку.",
        "setup": _knight_mount,
        "goal": "mount",
        "select": (2, 5),
    },
    {
        "id": "queen_lock",
        "title": "Ферзь: режим турели",
        "text": "Ферзь выбран. Нажмите кнопку «Включить дальнюю атаку» ниже — это отдельное действие, оно не требует клика по доске.",
        "setup": _queen_lock,
        "goal": "queen_lock",
        "select": (2, 5),
    },
    {
        "id": "queen_ranged",
        "title": "Ферзь: дальняя атака",
        "text": "В режиме турели ферзь двигаться не может, зато бьёт издалека. Нажмите красную клетку с врагом.",
        "setup": _queen_ranged,
        "goal": "attack",
        "select": (2, 5),
    },
    {
        "id": "king_stage",
        "title": "Стадия короля",
        "text": "Ход всегда начинается со стадии короля — у неё свой таймер вверху. Сходите королём или вылечьте раненого соседа (зелёная клетка).",
        "setup": _king_stage,
        "goal": "king_action",
        "select": (2, 5),
    },
    {
        "id": "surrender",
        "title": "Сдаться",
        "text": "Если позиция безнадёжна — кнопка сдачи доступна в любой момент партии. Нажмите «СДАТЬСЯ» ниже.",
        "setup": _surrender,
        "goal": "surrender",
        "select": None,
    },
    {
        "id": "mode_standard",
        "title": "Режим «Обычный бой»",
        "text": config.GAME_MODE_DESCRIPTIONS.get("standard", "") + " Уничтожьте все 3 вражеские пешки.",
        "setup": _mode_standard,
        "goal": "attack",
        "select": (4, 5),
        "is_complete": lambda state: not state.pieces_of("black"),
    },
    {
        "id": "mode_flag",
        "title": "Режим «Флаг»",
        "text": config.GAME_MODE_DESCRIPTIONS.get("flag", "") + " Проведите пешку на вражескую линию — серая клетка впереди.",
        "setup": _mode_flag,
        "goal": "move",
        "select": (3, config.BLACK_HOME_ROW - 1),
    },
    {
        "id": "mode_base",
        "title": "Режим «База»",
        "text": config.GAME_MODE_DESCRIPTIONS.get("base", "") + " Уничтожьте вражеского короля.",
        "setup": _mode_base,
        "goal": "attack",
        "select": (4, 5),
        "is_complete": lambda state: not any(p.type == "king" for p in state.pieces_of("black")),
    },
    {
        "id": "mode_fog",
        "title": "Режим «Туман войны»",
        "text": "Белые облака — клетки вне обзора. Король врага виден всегда — найдите и уничтожьте его.",
        "setup": _mode_fog,
        "goal": "attack",
        "select": (4, 5),
        "is_complete": lambda state: not any(p.type == "king" for p in state.pieces_of("black")),
    },
    {
        "id": "finish",
        "title": "Обучение завершено",
        "text": "Готово! Возвращайтесь в меню и начните настоящую партию.",
        "setup": _finish,
        "goal": None,
        "select": None,
    },
]


def step_count():
    return len(STEPS)


def build_state_for_step(index, game_state_cls):
    """Создаёт свежий GameState и расставляет его под шаг с номером index.
    game_state_cls передаётся явно (а не импортируется здесь), чтобы этот
    модуль оставался pygame-независимым и не создавал циклических импортов
    с main.py."""
    index = max(0, min(index, len(STEPS) - 1))
    step = STEPS[index]
    state = game_state_cls()
    step["setup"](state)
    return state
