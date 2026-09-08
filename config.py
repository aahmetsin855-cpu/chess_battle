# -*- coding: utf-8 -*-
"""
Центральный файл конфигурации.
Все "спорные" / настраиваемые значения вынесены сюда, чтобы их можно
было менять, не трогая остальной код.

ВАЖНО про размер доски: BOARD_SIZE и все производные от него величины
(домашние линии, зоны расстановки, размер экрана/клетки) можно менять в
рантайме через configure_board_size(size) — все модули читают их как
config.BOARD_SIZE и т.д. (атрибут модуля), а не как захваченную при
импорте константу, поэтому переконфигурация подхватывается везде.
"""

import os
import sys

import platform_utils

# Проектные пути строятся относительно самого проекта, а не текущей папки
# запуска. Это важно для Windows, ярлыков и запуска через .bat/.exe.
#
# В СОБРАННОМ PyInstaller-приложении __file__ указывает внутрь бандла
# (например, в _internal при --onedir), а не туда, где реально лежит
# .exe и где ожидается писать data/ (та папка может быть даже не
# доступна на запись). sys.executable в frozen-сборке ВСЕГДА указывает
# на настоящий путь установки — используем его в этом случае.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Writable-каталог для рантайм-данных (настройки, память ИИ, сохранённые
# партии) — ОТДЕЛЬНО от BASE_DIR, потому что на Android BASE_DIR лежит
# внутри read-only APK и писать туда нельзя.
#
# Windows / прочий desktop: WRITABLE_DIR == BASE_DIR, поведение не
# меняется по сравнению с тем, что было раньше (data/ рядом с
# .exe/скриптом).
#
# Android: platform_utils.get_app_writable_dir() возвращает app-private
# writable storage платформы; все обращения к DATA_DIR/AI_MEMORY_FILE/
# GAMES_DIR/SETTINGS_FILE ниже автоматически указывают туда же, без
# необходимости трогать settings.py/ai_memory.py по отдельности.
WRITABLE_DIR = platform_utils.get_app_writable_dir() or BASE_DIR

# Runtime background surface используется renderer.py; None = стандартный фон.
BACKGROUND_SURFACE = None

# ---------------------------------------------------------------------------
# Доска / окно
# ---------------------------------------------------------------------------
BOARD_WIDTH = 10
BOARD_HEIGHT = 10
# BOARD_SIZE is kept as a compatibility alias for legacy AI/tests. New code should
# use BOARD_WIDTH / BOARD_HEIGHT explicitly.
BOARD_SIZE = BOARD_WIDTH
BOARD_SIZE_OPTIONS = ((10, 8), (12, 8), (14, 10))

# Целевой "бюджет" пикселей под саму доску — при увеличении BOARD_SIZE
# клетка становится меньше, поэтому 10/12/14x10 помещаются в ОДНО и то же
# окно. Это специально: переход между экранами не должен создавать/
# пересоздавать новое окно.
#
# Раньше бюджет был захардкожен в 640px независимо от реального экрана,
# из-за чего доска выглядела мелкой на любом сколь-нибудь большом
# мониторе (особенно в fullscreen) и почти "терялась" на широких картах
# (14x10 и т.п.), где CELL_SIZE считается от МЕНЬШЕЙ стороны бюджета.
# Теперь бюджет — не константа, а вычисляется от реально доступного
# места на экране через set_available_area(), которую main.py вызывает
# один раз при старте (по разрешению рабочего стола) и заново при
# переключении fullscreen/windowed. Один и тот же бюджет используется
# для ВСЕХ размеров карты, поэтому свойство "окно не меняется между
# картами" сохраняется — просто сам бюджет теперь честно соответствует
# реальному экрану, а не произвольному числу.
_DEFAULT_BOARD_PIXEL_BUDGET = 640
_BOARD_PIXEL_BUDGET_W = _DEFAULT_BOARD_PIXEL_BUDGET
_BOARD_PIXEL_BUDGET_H = _DEFAULT_BOARD_PIXEL_BUDGET

# Клетка не должна становиться нечитаемо мелкой на больших картах, ни
# нелепо гигантской на маленьких — оба предела делают доску "ровной" по
# ощущению независимо от того, какая карта выбрана.
MIN_CELL_SIZE = 34
MAX_CELL_SIZE = 108

# Множитель размера UI (шрифты, размер клеток доски) поверх базовых
# desktop-величин выше. На desktop всегда 1.0 — ничего не меняется. На
# Android пересчитывается в main.py._apply_screen_size() от реального
# разрешения экрана: тот же пиксельный размер, что нормально читается на
# desktop-мониторе, физически крошечный на высокоплотном экране телефона.
UI_SCALE = 1.0

BOARD_MARGIN_X = 30
BOARD_MARGIN_Y = 90
# Текущая визуальная перспектива доски. Логика игры всегда хранит
# координаты в единой системе; для чёрных UI разворачивает доску.
VIEWER_COLOR = "white"
SIDE_PANEL_WIDTH = 320

# Резерв по краям под рамку/подписи координат и под нижнюю панель
# кнопок при вычислении бюджета доски из доступной площади экрана.
_LAYOUT_CHROME_W = BOARD_MARGIN_X * 2 + SIDE_PANEL_WIDTH + 20
_LAYOUT_CHROME_H = BOARD_MARGIN_Y + 50


def _clamp_cell_size(value):
    return int(max(MIN_CELL_SIZE * UI_SCALE, min(MAX_CELL_SIZE * UI_SCALE, value)))


CELL_SIZE = _clamp_cell_size(min(_BOARD_PIXEL_BUDGET_W // BOARD_WIDTH,
                                  _BOARD_PIXEL_BUDGET_H // BOARD_HEIGHT))


def set_ui_scale(width_px, height_px):
    """Пересчитывает UI_SCALE от реального разрешения экрана. Вызывается
    ТОЛЬКО на Android (см. main.py._apply_screen_size) — на desktop
    UI_SCALE остаётся 1.0, поведение окна не меняется.

    480px по меньшей стороне — условная "базовая" высота, для которой
    рассчитаны все текущие desktop-размеры шрифтов/клеток (обычное
    desktop-окно игры). Телефоны в landscape обычно дают 1000-1500px по
    меньшей стороне при высокой физической плотности пикселей — тот же
    пиксельный размер на них физически мельче, чем на мониторе, поэтому
    масштаб считается от отношения к этой базовой высоте, а не от
    отношения площадей/диагоналей.
    """
    global UI_SCALE
    shorter_side = min(int(width_px), int(height_px))
    if shorter_side <= 0:
        UI_SCALE = 1.0
        return
    # Подняли потолок с 1.6 до 2.0 — 1.6 упирался в потолок почти сразу
    # (реальные телефонные экраны дают отношение около 1.7-1.8), и после
    # первого прохода всё ещё казалось мелким. Риск переполнения текста за
    # пределы кнопок remains, but ширина кнопок растёт тем же множителем
    # (см. S() в main.py), так что раздельного разъезжания не должно быть.
    UI_SCALE = max(1.0, min(2.0, shorter_side / 640.0))


def set_available_area(width_px, height_px):
    """Пересчитывает бюджет пикселей под доску исходя из реально доступного
    места на экране (разрешение рабочего стола в оконном режиме, либо
    фактический размер поверхности в fullscreen). Должна вызываться ДО
    (или сразу вместе с) configure_board_size(), иначе CELL_SIZE не
    подхватит новый бюджет.
    """
    global _BOARD_PIXEL_BUDGET_W, _BOARD_PIXEL_BUDGET_H
    width_px = int(width_px) if width_px else 0
    height_px = int(height_px) if height_px else 0
    _BOARD_PIXEL_BUDGET_W = max(400, width_px - _LAYOUT_CHROME_W)
    _BOARD_PIXEL_BUDGET_H = max(400, height_px - _LAYOUT_CHROME_H)


SCREEN_WIDTH = BOARD_MARGIN_X * 2 + BOARD_WIDTH * CELL_SIZE + SIDE_PANEL_WIDTH
SCREEN_HEIGHT = BOARD_MARGIN_Y + BOARD_HEIGHT * CELL_SIZE + 50
FPS = 30

# Временный оверлей FPS/frame-time в углу экрана (см. main.py run()) —
# нужен, чтобы увидеть на скриншоте с телефона, реально ли тормозит сам
# игровой цикл, или проблема где-то ещё. Поставить False, когда причина
# тормозов на Android найдена и подтверждена.
DEBUG_SHOW_FPS = True
# Верхний предел на dt ОДНОГО кадра. Если основной поток ненадолго
# стопорится (разбор JSON у большого снапшота состояния, разом
# обработанная пачка сообщений, скопившихся за время лагов сети — то
# есть именно то, что происходит в мультиплеере на медленном
# интернете), pygame.Clock.tick() на СЛЕДУЮЩЕМ кадре вернёт этот целый
# затык одним большим dt. Анимации считают elapsed += dt "как есть", и
# один такой аномально большой dt перепрыгивает анимацию хода/удара
# сразу к концу (или за него) вместо плавной интерполяции — на экране
# это выглядит как "дёрганое" телепортирование фигуры соперника, и
# именно поэтому оно не встречалось локально/против ИИ (там на
# основном потоке нет блокирующей сетевой работы) — только в
# мультиплеере, и тем заметнее, чем медленнее соединение.
MAX_FRAME_DT = 1.0 / 20.0  # 50 мс — не больше ~1.5 обычных кадров за раз

def get_windowed_size():
    """Stable windowed size for the current rectangular board."""
    return (BOARD_MARGIN_X * 2 + BOARD_WIDTH * CELL_SIZE + SIDE_PANEL_WIDTH,
            BOARD_MARGIN_Y + BOARD_HEIGHT * CELL_SIZE + 50)


# Глубина стартовой зоны расстановки (в клетках от своей домашней линии),
# НЕЗАВИСИМО от размера доски: "маленькая армия на большой территории" —
# зона высадки остаётся компактной даже на карте 16x16.
DEPLOYMENT_ZONE_DEPTH = 3

# ---------------------------------------------------------------------------
# Цвета (тёмная тема, минимализм)
# ---------------------------------------------------------------------------
COLOR_BG = (18, 18, 22)
COLOR_PANEL_BG = (26, 26, 32)
COLOR_PANEL_BORDER = (54, 55, 64)
COLOR_BOARD_LIGHT = (54, 55, 62)
COLOR_BOARD_DARK = (36, 37, 43)
COLOR_HOME_ROW_WHITE = (76, 78, 86)
COLOR_HOME_ROW_BLACK = (76, 78, 86)
COLOR_ACCENT = (190, 192, 198)
COLOR_ACCENT_DIM = (92, 94, 104)

# Фигуры — КЛАССИЧЕСКИЕ шахматные цвета. Игрок = белые, ИИ = чёрные.
# Принадлежность армии показывается через UI (HP-полоски, рамки, статус),
# а НЕ через перекраску самой модели фигуры.
COLOR_PIECE_WHITE = (235, 235, 232)
COLOR_PIECE_WHITE_OUTLINE = (120, 120, 126)
COLOR_PIECE_BLACK = (32, 32, 36)
COLOR_PIECE_BLACK_OUTLINE = (150, 150, 158)

# HP-полоски: синяя у игрока, красная у противника.
COLOR_PLAYER_HP = (58, 140, 235)
COLOR_ENEMY_HP = (214, 48, 48)

# Минимальные UI-индикаторы принадлежности (рамка выбора, статус хода) —
# НЕ красят саму фигуру.
COLOR_PLAYER_UI = (190, 192, 198)
COLOR_ENEMY_UI = (136, 138, 146)

COLOR_TEXT = (225, 225, 230)
COLOR_TEXT_DIM = (150, 150, 158)
COLOR_HP_BAR_BG = (60, 60, 66)
COLOR_HP_BAR_FG_HEAL = (60, 180, 90)
COLOR_HIGHLIGHT_MOVE = (150, 153, 162)
COLOR_HIGHLIGHT_ATTACK = (168, 120, 112)
COLOR_HIGHLIGHT_MOUNT = (148, 142, 116)
COLOR_HIGHLIGHT_HEAL = (118, 148, 132)
COLOR_HIGHLIGHT_SWAP = (148, 128, 176)
COLOR_DAMAGE_NUMBER = (245, 70, 70)
COLOR_HIGHLIGHT_SELECT = (214, 216, 222)
COLOR_BUTTON = (42, 43, 49)
COLOR_BUTTON_HOVER = (58, 59, 67)
COLOR_BUTTON_ACTIVE = (78, 80, 90)
COLOR_USED_OVERLAY = (0, 0, 0, 140)
COLOR_EFFECT_PLAYER = (225, 225, 232)
COLOR_EFFECT_ENEMY = (60, 60, 66)
COLOR_EFFECT_IMPACT = (255, 220, 90)
COLOR_EFFECT_DEATH = (60, 60, 66)
COLOR_FOG = (10, 10, 13)
COLOR_FLAG_ZONE = (230, 190, 40)

# ---------------------------------------------------------------------------
# Доп. визуальная полировка (тени, свечения, частицы). Ничего из этого не
# меняет игровую семантику цвета (см. COLOR_PLAYER_HP/COLOR_ENEMY_HP выше) —
# это чисто декоративный слой, читаемый BoardRenderer/PieceRenderer/
# EffectsRenderer как необязательные "усилители" картинки.
# ---------------------------------------------------------------------------
COLOR_BOARD_LIGHT_HI = (61, 62, 70)      # верхний блик светлой клетки (бевел)
COLOR_BOARD_DARK_HI = (41, 42, 49)       # верхний блик тёмной клетки (бевел)
COLOR_BOARD_LIGHT_LO = (48, 49, 56)      # нижняя тень светлой клетки
COLOR_BOARD_DARK_LO = (31, 32, 37)       # нижняя тень тёмной клетки
COLOR_BOARD_COORD_LABEL = (110, 111, 120)
COLOR_SHADOW = (0, 0, 0, 90)
COLOR_PIECE_HIGHLIGHT_WHITE = (255, 255, 253)  # блик на светлой фигуре
COLOR_PIECE_HIGHLIGHT_BLACK = (72, 72, 80)      # блик на тёмной фигуре
COLOR_SELECT_GLOW = (255, 255, 255)
COLOR_HP_LOW_PULSE = (255, 90, 70)
COLOR_HP_BAR_TOP = (255, 255, 255)       # тонкий блик на HP-полоске
COLOR_PANEL_TOP_HI = (255, 255, 255)     # верхняя грань панели (стекло)
COLOR_BUTTON_TOP_HI = (255, 255, 255)

# Частицы (искры) при попадании/смерти — количество и параметры.
PARTICLE_COUNT_IMPACT = 8
PARTICLE_COUNT_DEATH = 10
PARTICLE_SPEED = 145.0
PARTICLE_GRAVITY = 260.0
PARTICLE_LIFETIME = 0.42
DAMAGE_NUMBER_LIFETIME = 0.72
DAMAGE_NUMBER_RISE = 34.0

# "Дыхание" (пульс) рамки выбора и низкого HP — период в секундах.
SELECT_PULSE_PERIOD = 1.1
LOW_HP_PULSE_PERIOD = 0.6
LOW_HP_RATIO = 0.3

# Squash & stretch для перемещения фигур (визуальная "живость" анимации).
MOVE_SQUASH_STRENGTH = 0.16
MOVE_ANTICIPATION_TIME = 0.05

# Кто есть кто (внутренняя логика по-прежнему использует 'white'/'black')
PLAYER_COLOR = "white"
AI_COLOR = "black"

# ---------------------------------------------------------------------------
# Характеристики фигур: HP и урон
# ---------------------------------------------------------------------------
# HP увеличены примерно вдвое относительно исходного баланса, чтобы бой
# успевал развиваться и не заканчивался за 1-2 обмена ударами. Урон не
# менялся. Конь — исключение (см. роль коня ниже): HP коня НЕ увеличен,
# он должен оставаться относительно хрупким, потому что его сила — в
# защите/усилении союзника, а не в собственной живучести.
PAWN_HP = 6
KING_HP = 6
BISHOP_HP = 4
ROOK_HP = 12
QUEEN_HP = 10
KNIGHT_HP = 2   # намеренно НЕ увеличен

PAWN_DAMAGE = 1
KING_DAMAGE = 1
BISHOP_DAMAGE = 2
ROOK_DAMAGE = 3
QUEEN_DAMAGE = 3
KNIGHT_DAMAGE = 0   # конь не атакует напрямую

PIECE_STATS = {
    "pawn":   {"hp": PAWN_HP, "damage": PAWN_DAMAGE},
    "king":   {"hp": KING_HP, "damage": KING_DAMAGE},
    "bishop": {"hp": BISHOP_HP, "damage": BISHOP_DAMAGE},
    "rook":   {"hp": ROOK_HP, "damage": ROOK_DAMAGE},
    "queen":  {"hp": QUEEN_HP, "damage": QUEEN_DAMAGE},
    "knight": {"hp": KNIGHT_HP, "damage": KNIGHT_DAMAGE},
}

# Условная "ценность" фигуры для ИИ (используется в оценке позиции)
PIECE_VALUE = {
    "pawn": 10,
    "king": 1000,
    "bishop": 15,
    "rook": 24,
    "queen": 30,
    "knight": 18,
}

# Состав армии, который должен расставить каждый игрок.
# На 10x10/12x12 армия остаётся компактной ("маленькая армия — большая
# территория"). На самой большой карте (16x16) добавляется чуть больше
# войск (см. ARMY_COMPOSITION_LARGE_BONUS), т.к. иначе фронт на такой
# карте получается слишком разреженным — но рост всё равно умеренный,
# не "полная вторая армия".
ARMY_COMPOSITION = {
    "pawn": 8,
    "bishop": 2,
    "rook": 2,
    "knight": 2,
    "queen": 1,
    "king": 1,
}

# Прибавка к составу армии для карты 16x16 (см. get_army_composition()).
ARMY_COMPOSITION_LARGE_BOARD_SIZE = 16
ARMY_COMPOSITION_LARGE_BONUS = {
    "pawn": 2,
    "bishop": 1,
    "rook": 1,
}

# Прибавка для КОНКРЕТНЫХ размеров карты (в дополнение к порогу выше).
# Самая большая карта из BOARD_SIZE_OPTIONS — (14, 10) — иначе на таком
# длинном поле фронт выходит слишком разреженным для одной пешки на
# клетку по ширине.
ARMY_COMPOSITION_EXACT_SIZE_BONUS = {
    (14, 10): {"pawn": 2, "knight": 1},
}


def get_army_composition(board_size=None):
    """Возвращает состав армии для данного размера карты. На карте
    ARMY_COMPOSITION_LARGE_BOARD_SIZE (16x16) и больше добавляется
    ARMY_COMPOSITION_LARGE_BONUS сверх базового состава; для отдельных
    размеров (см. ARMY_COMPOSITION_EXACT_SIZE_BONUS) добавляется ещё и
    точечная прибавка независимо от порога."""
    size = board_size if board_size is not None else BOARD_SIZE
    exact = None
    if isinstance(size, (tuple, list)):
        exact = (int(size[0]), int(size[1]))
        size = max(exact)
    comp = dict(ARMY_COMPOSITION)
    if size >= ARMY_COMPOSITION_LARGE_BOARD_SIZE:
        for t, extra in ARMY_COMPOSITION_LARGE_BONUS.items():
            comp[t] = comp.get(t, 0) + extra
    if exact is not None:
        for t, extra in ARMY_COMPOSITION_EXACT_SIZE_BONUS.get(exact, {}).items():
            comp[t] = comp.get(t, 0) + extra
    return comp

# ---------------------------------------------------------------------------
# Половины доски и лечебные (домашние) линии — производные от BOARD_SIZE.
# Пересчитываются в configure_board_size(); значения по умолчанию — для 10x10.
# ---------------------------------------------------------------------------
WHITE_HOME_ROW = 0
BLACK_HOME_ROW = BOARD_HEIGHT - 1
WHITE_HALF_ROWS = (0, min(DEPLOYMENT_ZONE_DEPTH - 1, BOARD_HEIGHT - 1))
BLACK_HALF_ROWS = (max(0, BOARD_HEIGHT - DEPLOYMENT_ZONE_DEPTH), BOARD_HEIGHT - 1)


def configure_board_size(size, deployment_depth=None):
    """
    Пересчитывает все производные от размера доски константы в модуле.
    Вызывается ДО создания GameState/App (например, с экрана выбора
    размера карты). Так как все модули читают значения как config.X во
    время выполнения (а не через 'from config import X'), изменения
    подхватываются везде автоматически.
    """
    global BOARD_WIDTH, BOARD_HEIGHT, BOARD_SIZE, CELL_SIZE, SCREEN_WIDTH, SCREEN_HEIGHT
    global WHITE_HOME_ROW, BLACK_HOME_ROW, WHITE_HALF_ROWS, BLACK_HALF_ROWS
    global DEPLOYMENT_ZONE_DEPTH

    if deployment_depth is not None:
        DEPLOYMENT_ZONE_DEPTH = deployment_depth

    if isinstance(size, (tuple, list)):
        width, height = int(size[0]), int(size[1])
    else:
        width = height = int(size)
    if width < 4 or height < 4:
        raise ValueError("board dimensions are too small")
    BOARD_WIDTH = width
    BOARD_HEIGHT = height
    BOARD_SIZE = width  # compatibility alias
    CELL_SIZE = _clamp_cell_size(min(_BOARD_PIXEL_BUDGET_W // BOARD_WIDTH,
                                      _BOARD_PIXEL_BUDGET_H // BOARD_HEIGHT))
    SCREEN_WIDTH, SCREEN_HEIGHT = get_windowed_size()

    WHITE_HOME_ROW = 0
    BLACK_HOME_ROW = BOARD_HEIGHT - 1
    WHITE_HALF_ROWS = (0, min(DEPLOYMENT_ZONE_DEPTH - 1, BOARD_HEIGHT - 1))
    BLACK_HALF_ROWS = (max(0, BOARD_HEIGHT - DEPLOYMENT_ZONE_DEPTH), BOARD_HEIGHT - 1)


# ---------------------------------------------------------------------------
# Радиусы атак / способностей
# ---------------------------------------------------------------------------
BISHOP_RANGE = 2            # область 5x5 (дистанция по Чебышеву <= 2)
BISHOP_LINE_OF_SIGHT = True  # линия огня слона; союзные пешки НЕ блокируют
QUEEN_RANGED_RANGE = 2       # дальняя атака ферзя, область 5x5
QUEEN_RANGED_DAMAGE = 3
KING_HEAL_AMOUNT = 2         # сколько HP король лечит союзнику своим действием

# ---------------------------------------------------------------------------
# Лечение на домашней линии
# ---------------------------------------------------------------------------
HEAL_AMOUNT = 1          # сколько HP восстанавливается
HEAL_EVERY_TURNS = 1     # раз в сколько ходов (1 = каждый свой ход)

# ---------------------------------------------------------------------------
# Таймеры хода
# ---------------------------------------------------------------------------
KING_ACTION_TIME = 15.0
MAIN_TURN_TIME = 60.0
STANDARD_GAME_TIME = 480.0  # 8 minutes total for Standard Battle

# ---------------------------------------------------------------------------
# Игровые режимы
# ---------------------------------------------------------------------------
GAME_MODES = ("standard", "flag", "base", "fog")
DEFAULT_GAME_MODE = "standard"
GAME_MODE_NAMES = {
    "standard": "Обычный бой",
    "flag": "Флаг",
    "base": "База",
    "fog": "Туман войны",
}
GAME_MODE_DESCRIPTIONS = {
    "standard": "Уничтожьте всю армию противника",
    "flag": "Проведите фигуру на вражескую линию",
    "base": "Король — ключевой юнит: потеря короля завершает игру",
    "fog": "Ограниченная видимость: враг скрыт вне радиуса обзора; потеря короля завершает игру",
}

# Дальность обзора (одинаковая для ВСЕХ фигур — специально, чтобы не
# создавать "разведывательное" превосходство одних фигур над другими
# формально; тактическая роль разведчика у коня — в его мобильности).
VISION_RANGE = BISHOP_RANGE

# Сколько ходов ИИ "помнит" позицию своей фигуры, которая атаковала
# игрока и тем самым временно раскрыла себя в Fog of War.
FOG_REVEAL_ON_ATTACK = True

# ---------------------------------------------------------------------------
# ИИ
# ---------------------------------------------------------------------------
AI_THINK_TIME = 2.0             # бюджет времени на весь ход ИИ (сек), из config
AI_MAX_SEARCH_DEPTH = 6          # ограничение по глубине (в атомарных действиях)
AI_MAX_ACTIONS_PER_TURN = 24     # защитный предел числа действий ИИ за ход
AI_BRANCH_LIMIT = 14             # сколько лучших ходов рассматривать в узле поиска
TRANSPOSITION_TABLE_MAX = 200000
AI_FORMATION_SAMPLES = 8         # сколько вариантов расстановки ИИ оценивает перед выбором

# Уровни сложности для Algorithm AI. think_time — бюджет на весь ход (сек),
# max_depth — предельная глубина поиска (в атомарных действиях).
AI_DIFFICULTY_SETTINGS = {
    "easy":   {"think_time": 0.6, "max_depth": 3},
    "normal": {"think_time": 2.0, "max_depth": 6},
    "hard":   {"think_time": 4.5, "max_depth": 9},
}

# --- Локальный AI (LLM через Ollama), полностью офлайн -----------------
# Если Ollama не установлена/не запущена — игра автоматически и без
# падений переключается на Algorithm AI (см. ai_interface.py).
LOCAL_AI_ENABLED = True
LOCAL_AI_MODEL = "llama3.2"
LOCAL_AI_TIMEOUT = 8.0          # предел на ОДИН запрос к Ollama (секунды)
LOCAL_AI_TURN_BUDGET = 60.0     # предел на ВЕСЬ ход целиком (king stage + до
                                 # AI_MAX_ACTIONS_PER_TURN действий лобби-этапа).
                                 # ВАЖНО: это ОТДЕЛЬНАЯ, гораздо большая величина,
                                 # чем LOCAL_AI_TIMEOUT — раньше дедлайн хода
                                 # считался как max(LOCAL_AI_TIMEOUT, AI_THINK_TIME)
                                 # = те же 8 секунд НА ВЕСЬ ход, хотя каждый
                                 # отдельный запрос к Ollama мог сам по себе занять
                                 # почти все эти 8 секунд. В итоге первый же запрос
                                 # (king stage) съедал весь бюджет хода, и до
                                 # лобби-этапа очередь просто не доходила — ИИ
                                 # ходил только королём. Теперь общий бюджет хода
                                 # намного больше одного запроса, так что медленная
                                 # модель просто сделает МЕНЬШЕ действий за ход
                                 # вместо того, чтобы не делать вообще ничего кроме
                                 # короля.
LOCAL_AI_HOST = "http://localhost:11434"
LOCAL_AI_TEMPERATURE = 0.15
LOCAL_AI_NUM_PREDICT = 12

# ---------------------------------------------------------------------------
# Анимации (все длительности настраиваются здесь)
# ---------------------------------------------------------------------------
MOVE_ANIMATION_TIME = 0.25
ATTACK_ANIMATION_TIME = 0.20
DEATH_ANIMATION_TIME = 0.30
IMPACT_FLASH_TIME = 0.14
AI_STEP_PAUSE = 0.18       # пауза между атомарными действиями ИИ, чтобы ход был читаем
DEFAULT_ANIMATION_SPEED = 1.0

# ---------------------------------------------------------------------------
# Локальные данные (память ИИ, настройки)
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(WRITABLE_DIR, "data")
AI_MEMORY_FILE = os.path.join(DATA_DIR, "ai_memory.json")
GAMES_DIR = os.path.join(DATA_DIR, "games")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# ---------------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------------
GAME_TITLE = "HP Battle Chess"

