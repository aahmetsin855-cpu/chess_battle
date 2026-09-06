# -*- coding: utf-8 -*-
"""
Отрисовка. Архитектурно разделена на три независимых слоя, как требуется:

    BoardRenderer   — рисует неподвижные клетки доски, домашние линии,
                       рамку доски. Никогда не знает о фигурах/анимациях.
    PieceRenderer   — рисует силуэты фигур (классические бело/чёрные) и
                       их HP-полоски. Может принять `screen_pos`
                       (переопределение позиции во время анимации), но
                       рисует ТОЛЬКО саму фигуру — никогда фон клетки.
    EffectsRenderer — рисует эффекты атак/попаданий/смерти поверх доски
                       и фигур. Существует отдельно от PieceRenderer.

AnimationController (см. animation.py) физически отделён от рендера: он
только хранит/интерполирует screen-координаты и НИКОГДА не трогает
координаты самой доски (BoardRenderer их вообще не запрашивает).

Классы ниже — тонкие пространства имён (staticmethod). Модульные функции
в конце файла — обратно совместимые обёртки для main.py.
"""
import math
import pygame
import config
import coords


def board_to_screen(col, row):
    return coords.board_to_screen(col, row)


def screen_to_board(x, y):
    return coords.screen_to_board(x, y)


# ---------------------------------------------------------------------------
# Шрифты. НЕ используем pygame.font.SysFont("Arial", ...) — это desktop-
# допущение: SysFont ищет системный TTF по имени, которого на Android
# просто нет (и на "голом" Linux без fontconfig тоже может не быть), и
# в худшем случае либо падает, либо тихо подставляет что попало. Вместо
# этого everywhere используем make_font(), который оборачивает
# pygame.font.Font(None, size) — это ВСТРОЕННЫЙ в pygame шрифт,
# гарантированно работающий одинаково на Windows/Linux/macOS/Android без
# зависимости от системных шрифтов. Визуально это тот же дефолтный
# pygame-шрифт, которым и так уже рисуются, например, подписи координат
# доски (см. _coord_font выше) — стиль проекта не меняется.
_font_cache = {}


def make_font(size, bold=False):
    key = (int(size), bool(bold))
    font = _font_cache.get(key)
    if font is None:
        font = pygame.font.Font(None, key[0])
        if bold:
            font.set_bold(True)
        _font_cache[key] = font
    return font


# ---------------------------------------------------------------------------
# Небольшие декоративные утилиты, общие для всех рендереров ниже. Ничего
# здесь не трогает игровую логику/координаты — только пиксели.
# ---------------------------------------------------------------------------
_coord_font_cache = {}
_vignette_cache = {}


def _coord_font():
    size = max(11, config.CELL_SIZE // 5)
    f = _coord_font_cache.get(size)
    if f is None:
        try:
            f = pygame.font.Font(None, size + 6)
        except Exception:
            f = None
        _coord_font_cache[size] = f
    return f


def _vignette_surface(w, h):
    """Мягкое затемнение к краям экрана — кэшируется по размеру, потому
    что пересчитывать градиент по пикселям каждый кадр дорого, а размер
    окна меняется только на экране выбора карты."""
    key = (w, h)
    surf = _vignette_cache.get(key)
    if surf is not None:
        return surf
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    steps = 14
    max_r = math.hypot(w / 2, h / 2)
    band = int(max_r / steps) + 2
    center = (w // 2, h // 2)
    # Рисуем концентрические кольца (не залитые круги!) с нарастающей
    # альфой от центра к краю — так каждое кольцо перекрывается с соседним
    # лишь слегка, и получается настоящий радиальный градиент, а не
    # плоская засветка всего экрана.
    for i in range(steps):
        t = i / steps
        radius = int(max_r * 0.30) + int(max_r * 0.75 * t)
        alpha = int(65 * (t ** 2))
        if alpha <= 0 or radius <= 0:
            continue
        pygame.draw.circle(surf, (0, 0, 0, alpha), center, radius + band, band)
    _vignette_cache[(w, h)] = surf
    return surf


def _lerp_color(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _pulse(period, phase=0.0):
    """Значение 0..1 колеблющееся синусоидой по реальным часам pygame —
    используется для дыхания рамки выбора / предупреждения о низком HP."""
    ticks = pygame.time.get_ticks() / 1000.0
    return 0.5 + 0.5 * math.sin((ticks / max(0.05, period)) * 2 * math.pi + phase)


# ---------------------------------------------------------------------------
# Маска силуэта фигуры — нужна, чтобы блик НИКОГДА не вылезал за пределы
# самой фигуры. Раньше блик рисовался как отдельный круг в фиксированной
# точке (верхний левый угол) с радиусом, посчитанным от общего "r" клетки —
# это верно только для фигур, чей силуэт сам является кругом радиуса r
# (король/ферзь). У пешки силуэт МЕНЬШЕ r (r-6), у слона/коня — треугольник,
# не занимающий тот угол вообще, у ладьи — квадрат со скруглением. Во всех
# этих случаях блик оказывался частично вне модельки. Вместо того чтобы
# подбирать смещение под каждую фигуру на глаз (а проверить визуально я не
# могу — рендер недоступен), делаем маску из ТОЙ ЖЕ геометрии, что и сам
# силуэт, и умножаем блик на неё (BLEND_RGBA_MULT обнуляет альфу блика
# везде, где маска прозрачна) — тогда блик математически не может вылезти
# за контур фигуры, для любой формы.
_piece_mask_cache = {}


def _piece_silhouette_mask(piece_type, r):
    key = (piece_type, r)
    cached = _piece_mask_cache.get(key)
    if cached is not None:
        return cached
    size = r * 2 + 4
    ccx = ccy = size // 2
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    white = (255, 255, 255, 255)
    if piece_type == "pawn":
        pygame.draw.circle(surf, white, (ccx, ccy), max(3, r - 6))
    elif piece_type == "king":
        pygame.draw.circle(surf, white, (ccx, ccy), max(6, int(r * 0.72)))
    elif piece_type == "queen":
        # Квадратный корпус (как у ладьи), увеличенный относительно
        # первой версии — см. draw() — с местом для короны сверху.
        body_half = max(8, int(r * 0.80))
        body = pygame.Rect(ccx - body_half, ccy - body_half, 2 * body_half, 2 * body_half)
        pygame.draw.rect(surf, white, body, border_radius=max(2, body_half // 5))
    elif piece_type == "bishop":
        pts = [(ccx, ccy - r), (ccx - r, ccy + r), (ccx + r, ccy + r)]
        pygame.draw.polygon(surf, white, pts)
    elif piece_type == "rook":
        rr = max(9, r)
        body = pygame.Rect(ccx - rr, ccy - rr, 2 * rr, 2 * rr)
        pygame.draw.rect(surf, white, body, border_radius=max(2, rr // 6))
    elif piece_type == "knight":
        pts = [(ccx - r, ccy + r), (ccx - r // 3, ccy - r), (ccx + r, ccy + r // 3), (ccx + r // 2, ccy + r)]
        pygame.draw.polygon(surf, white, pts)
    else:  # king — полный круг радиуса r
        pygame.draw.circle(surf, white, (ccx, ccy), r)
    _piece_mask_cache[key] = surf
    return surf


def _draw_clipped_highlight(screen, piece_type, r, cx, cy, color, alpha, hi_r_frac, offset_frac):
    """Рисует полупрозрачный блик, обрезанный точно по силуэту фигуры."""
    mask = _piece_silhouette_mask(piece_type, r)
    size = mask.get_width()
    ccx = ccy = size // 2
    hi_r = max(2, int(r * hi_r_frac))
    blob_x = ccx - int(r * offset_frac)
    blob_y = ccy - int(r * offset_frac)
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    pygame.draw.circle(surf, (*color, alpha), (blob_x, blob_y), hi_r)
    surf.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    screen.blit(surf, (cx - ccx, cy - ccy))


# ---------------------------------------------------------------------------
# BoardRenderer — только неподвижные клетки. Никогда не получает никаких
# анимационных/фигурных данных, поэтому физически не может "утащить"
# фон клетки вместе с фигурой.
# ---------------------------------------------------------------------------
class BoardRenderer:
    @staticmethod
    def draw(screen):
        if config.BACKGROUND_SURFACE is not None:
            screen.blit(config.BACKGROUND_SURFACE, (0, 0))
        else:
            screen.fill(config.COLOR_BG)

        bevel = max(2, config.CELL_SIZE // 14)
        for row in range(config.BOARD_HEIGHT):
            for col in range(config.BOARD_WIDTH):
                x, y = board_to_screen(col, row)
                light = (col + row) % 2 == 0
                base = config.COLOR_BOARD_LIGHT if light else config.COLOR_BOARD_DARK
                hi = config.COLOR_BOARD_LIGHT_HI if light else config.COLOR_BOARD_DARK_HI
                lo = config.COLOR_BOARD_LIGHT_LO if light else config.COLOR_BOARD_DARK_LO
                pygame.draw.rect(screen, base, (x, y, config.CELL_SIZE, config.CELL_SIZE))
                # Тонкий бевел (фаска): светлее сверху/слева, темнее снизу/справа —
                # даёт лёгкое ощущение материальности клетки без текстур.
                pygame.draw.rect(screen, hi, (x, y, config.CELL_SIZE, bevel))
                pygame.draw.rect(screen, hi, (x, y, bevel, config.CELL_SIZE))
                pygame.draw.rect(screen, lo, (x, y + config.CELL_SIZE - bevel, config.CELL_SIZE, bevel))
                pygame.draw.rect(screen, lo, (x + config.CELL_SIZE - bevel, y, bevel, config.CELL_SIZE))

        for col in range(config.BOARD_WIDTH):
            x, y = board_to_screen(col, config.WHITE_HOME_ROW)
            pygame.draw.rect(screen, config.COLOR_ACCENT_DIM, (x, y, config.CELL_SIZE, config.CELL_SIZE), 2)
            x, y = board_to_screen(col, config.BLACK_HOME_ROW)
            pygame.draw.rect(screen, config.COLOR_ACCENT_DIM, (x, y, config.CELL_SIZE, config.CELL_SIZE), 2)

        # Экранный прямоугольник доски всегда начинается в (BOARD_MARGIN_X,
        # BOARD_MARGIN_Y) и имеет размер bw x bh — ЭТО НЕ ЗАВИСИТ от того,
        # какой цвет сейчас смотрит на доску (VIEWER_COLOR). Разворот
        # перспективы в coords.py меняет только то, какая ЛОГИЧЕСКАЯ клетка
        # попадает в какое место экрана, а не сам ограничивающий прямоугольник.
        #
        # Раньше здесь этот угол вычислялся как
        # board_to_screen(0, BOARD_HEIGHT - 1) — это давало верный
        # верхний левый угол ТОЛЬКО для белых (когда col/row не
        # переворачиваются). Для чёрных coords._screen_col() отражает
        # колонку 0 на САМУЮ ПРАВУЮ визуальную колонку, поэтому bx на
        # самом деле оказывался координатой правого края доски — отсюда
        # съехавшая рамка/"угол" в правом нижнем углу и цифры рядов,
        # налезающие на последнюю колонку у чёрных.
        bx, by = config.BOARD_MARGIN_X, config.BOARD_MARGIN_Y
        bw = config.BOARD_WIDTH * config.CELL_SIZE
        bh = config.BOARD_HEIGHT * config.CELL_SIZE
        # Внешняя рамка должна описывать именно прямоугольное игровое поле.
        # Раньше здесь по ошибке использовалась ширина и для высоты, поэтому
        # на картах 10x8/12x8 снизу появлялась лишняя "чёрная" зона.
        pygame.draw.rect(screen, (18, 18, 22), (bx - 3, by - 3, bw + 6, bh + 6), 2)
        pygame.draw.rect(screen, (98, 99, 110), (bx, by, bw, bh), 2)

        BoardRenderer._draw_coords(screen, bx, by, bw, bh)
        screen.blit(_vignette_surface(config.SCREEN_WIDTH, config.SCREEN_HEIGHT), (0, 0))

    @staticmethod
    def _draw_coords(screen, bx, by, bw, bh):
        font = _coord_font()
        if font is None:
            return
        files = "ABCDEFGHIJKLMNOP"
        for col in range(config.BOARD_WIDTH):
            x, _ = board_to_screen(col, 0)
            label = font.render(files[col] if col < len(files) else str(col + 1), True, config.COLOR_BOARD_COORD_LABEL)
            screen.blit(label, (x + config.CELL_SIZE // 2 - label.get_width() // 2, by + bh + 4))
        for row in range(config.BOARD_HEIGHT):
            _, y = board_to_screen(0, row)
            label = font.render(str(config.BOARD_HEIGHT - row), True, config.COLOR_BOARD_COORD_LABEL)
            screen.blit(label, (bx - label.get_width() - 6, y + config.CELL_SIZE // 2 - label.get_height() // 2))

    @staticmethod
    def draw_zone_tint(screen, rows, color, alpha=30):
        lo, hi = rows
        for row in range(lo, hi + 1):
            for col in range(config.BOARD_WIDTH):
                x, y = board_to_screen(col, row)
                s = pygame.Surface((config.CELL_SIZE, config.CELL_SIZE), pygame.SRCALPHA)
                s.fill((*color, alpha))
                screen.blit(s, (x, y))

    @staticmethod
    def draw_highlights(screen, cells, color, alpha=95):
        for c, r in cells:
            x, y = board_to_screen(c, r)
            s = pygame.Surface((config.CELL_SIZE, config.CELL_SIZE), pygame.SRCALPHA)
            s.fill((*color, alpha))
            screen.blit(s, (x, y))

    @staticmethod
    def draw_cell_border(screen, col, row, color, width=3):
        x, y = board_to_screen(col, row)
        pygame.draw.rect(screen, color, (x, y, config.CELL_SIZE, config.CELL_SIZE), width)

    @staticmethod
    def draw_fog_cell(screen, col, row):
        """Полупрозрачное белое 'облако' на клетке вне радиуса обзора —
        визуализация тумана войны. Один ровный alpha-слой на клетку —
        специально без узора (кружков и т.п.), чтобы при большом
        нетронутом облаке на много клеток не было некрасивых швов/пятен;
        доска под ним остаётся слабо видна (лёгкая дымка, не заслонка)."""
        x, y = board_to_screen(col, row)
        s = pygame.Surface((config.CELL_SIZE, config.CELL_SIZE), pygame.SRCALPHA)
        s.fill((255, 255, 255, 92))
        screen.blit(s, (x, y))


# ---------------------------------------------------------------------------
# PieceRenderer — только силуэт фигуры + её собственный HP/статус UI.
# Классические цвета: белые/чёрные модели. Синий/красный используется
# ТОЛЬКО в HP-полоске и рамке выбора — никогда в заливке самой фигуры.
# ---------------------------------------------------------------------------
class PieceRenderer:
    @staticmethod
    def _piece_colors(piece):
        if piece.color == config.PLAYER_COLOR:
            return config.COLOR_PIECE_WHITE, config.COLOR_PIECE_WHITE_OUTLINE
        return config.COLOR_PIECE_BLACK, config.COLOR_PIECE_BLACK_OUTLINE

    @staticmethod
    def draw(screen, piece, font_small, selected=False, screen_pos=None, hp_override=None, scale=1.0):
        """Рисует ОДНУ фигуру (тень + силуэт + блик + HP-полоска + статус-иконки).
        screen_pos, если задан, переопределяет позицию для анимации —
        но здесь рисуется исключительно сама фигура, никакого фона
        клетки, поэтому "клетка" физически не может двигаться вместе
        с ней. `scale` — необязательный множитель размера силуэта для
        лёгкого squash & stretch во время анимации (1.0 = без искажений)."""
        if piece.is_mounted_knight:
            return  # конь "спрятан" под фигурой-носителем

        animating = screen_pos is not None
        if screen_pos is not None:
            x, y = screen_pos
        else:
            x, y = board_to_screen(piece.col, piece.row)
        cx, cy = x + config.CELL_SIZE // 2, y + config.CELL_SIZE // 2
        r = int((config.CELL_SIZE // 2 - max(6, config.CELL_SIZE // 6)) * max(0.7, scale))
        fill, outline_default = PieceRenderer._piece_colors(piece)
        selected_glow = selected and not animating
        outline = config.COLOR_HIGHLIGHT_SELECT if selected else outline_default
        ow = 3 if selected else 2

        # Мягкая тень под фигурой — простой приём, который сразу даёт
        # ощущение веса и "приподнятости" силуэта над доской.
        shadow_r = max(4, int(r * 1.05))
        shadow = pygame.Surface((shadow_r * 2 + 6, shadow_r + 8), pygame.SRCALPHA)
        pygame.draw.ellipse(shadow, (0, 0, 0, 95), (0, int(shadow_r * 0.15), shadow_r * 2, int(shadow_r * 0.9)))
        screen.blit(shadow, (cx - shadow_r - 3, cy + int(r * 0.55)))

        # Пульсирующее свечение выбранной фигуры — рисуется ДО силуэта,
        # чтобы силуэт оставался поверх и полностью читаемым.
        if selected_glow:
            glow_t = _pulse(config.SELECT_PULSE_PERIOD)
            glow_r = int(r * (1.28 + 0.10 * glow_t))
            glow_alpha = int(70 + 50 * glow_t)
            glow_s = pygame.Surface((glow_r * 2 + 4, glow_r * 2 + 4), pygame.SRCALPHA)
            pygame.draw.circle(glow_s, (*config.COLOR_SELECT_GLOW, glow_alpha), (glow_r + 2, glow_r + 2), glow_r, 3)
            screen.blit(glow_s, (cx - glow_r - 2, cy - glow_r - 2))

        t = piece.type

        # Внутренние детали фигур должны быть заметны и на белом, и на чёрном
        # силуэте. Это НЕ цвет стороны — это нейтральный контрастный рисунок.
        detail = (48, 48, 54) if piece.color == config.PLAYER_COLOR else (218, 218, 222)

        if t == "pawn":
            pygame.draw.circle(screen, fill, (cx, cy), max(3, r - 6))
            pygame.draw.circle(screen, outline, (cx, cy), max(3, r - 6), ow)
        elif t == "king":
            # Раньше король был просто кругом с крестом, НАРИСОВАННЫМ
            # ПЛОСКО НА ПОВЕРХНОСТИ круга — это и читалось как вид сверху
            # (будто смотришь на метку, нанесённую на крышку фишки). У
            # остальных фигур силуэт "растёт" вверх за пределы корпуса
            # (апекс слона, голова коня) — то есть подразумевается вид
            # сбоку/спереди. Делаем так же: корпус чуть меньше r, а крест
            # — отдельный силуэт-навершие, ВЫСТУПАЮЩИЙ над корпусом до
            # той же верхней границы (cy - r), что и у слона.
            body_r = max(6, int(r * 0.72))
            pygame.draw.circle(screen, fill, (cx, cy), body_r)
            pygame.draw.circle(screen, outline, (cx, cy), body_r, ow)
            spike_top = cy - r
            spike_bottom = cy - body_r + 2
            spike_w = max(4, int(r * 0.20))
            spike_rect = pygame.Rect(cx - spike_w // 2, spike_top, spike_w, max(2, spike_bottom - spike_top))
            pygame.draw.rect(screen, fill, spike_rect, border_radius=1)
            pygame.draw.rect(screen, outline, spike_rect, ow, border_radius=1)
            bar_w = max(9, int(r * 0.46))
            bar_h = spike_w
            bar_y = spike_top + int((spike_bottom - spike_top) * 0.30)
            bar_rect = pygame.Rect(cx - bar_w // 2, bar_y, bar_w, bar_h)
            pygame.draw.rect(screen, fill, bar_rect, border_radius=1)
            pygame.draw.rect(screen, outline, bar_rect, ow, border_radius=1)
        elif t == "bishop":
            pts = [(cx, cy - r), (cx - r, cy + r), (cx + r, cy + r)]
            pygame.draw.polygon(screen, fill, pts)
            pygame.draw.polygon(screen, outline, pts, ow)
            pygame.draw.circle(screen, detail, (cx, cy - r + 6), 3)
        elif t == "rook":
            # Ладья в том же минималистичном стиле, что и остальные фигуры:
            # простой квадратный силуэт без отдельной "короны".
            rr = max(9, r)
            body = pygame.Rect(cx - rr, cy - rr, 2 * rr, 2 * rr)
            pygame.draw.rect(screen, fill, body, border_radius=max(2, rr // 6))
            pygame.draw.rect(screen, outline, body, ow, border_radius=max(2, rr // 6))
            # Небольшой внутренний штрих оставляет фигуру визуально живой,
            # но не превращает её снова в сложный силуэт.
            inset = max(4, rr // 4)
            pygame.draw.line(screen, detail,
                             (cx - inset, cy - inset),
                             (cx + inset, cy - inset), max(2, ow))
        elif t == "knight":
            pts = [(cx - r, cy + r), (cx - r // 3, cy - r), (cx + r, cy + r // 3), (cx + r // 2, cy + r)]
            pygame.draw.polygon(screen, fill, pts)
            pygame.draw.polygon(screen, outline, pts, ow)
        elif t == "queen":
            # Корпус — квадрат, как у ладьи (роднит их как "тяжёлые"
            # фигуры), но с фирменной короной — три зубца, выступающие
            # НАД корпусом (тот же приём, что у слона и короля) — чтобы
            # силуэты ферзя и ладьи не путались друг с другом несмотря
            # на общую квадратную форму корпуса. Модель увеличена
            # относительно первой версии — раньше ферзь выглядел заметно
            # мельче ладьи, хотя должен быть как минимум не меньше.
            body_half = max(8, int(r * 0.80))
            body = pygame.Rect(cx - body_half, cy - body_half, 2 * body_half, 2 * body_half)
            pygame.draw.rect(screen, fill, body, border_radius=max(2, body_half // 5))
            pygame.draw.rect(screen, outline, body, ow, border_radius=max(2, body_half // 5))
            top_y = cy - body_half
            tooth_w = max(7, int(r * 0.36))
            tooth_h = max(6, int(r * 0.20))
            spacing = int(tooth_w * 0.95)
            for off in (-1, 0, 1):
                tx = cx + off * spacing
                pts = [(tx - tooth_w // 2, top_y + 2), (tx, top_y - tooth_h), (tx + tooth_w // 2, top_y + 2)]
                pygame.draw.polygon(screen, fill, pts)
                pygame.draw.polygon(screen, outline, pts, ow)

        # Небольшой блик сверху-слева — недорогой способ дать силуэту
        # ощущение объёма, не отходя от плоского минималистичного стиля.
        # Раньше блик был просто кругом в фиксированной точке и вылезал за
        # контур фигуры (особенно заметно на белых — пешка/слон/конь/ладья
        # не заполняют тот угол целиком). Теперь блик обрезается маской
        # силуэта самой фигуры (_draw_clipped_highlight) — вылезти за
        # контур физически не может, для обоих цветов.
        if piece.color == config.PLAYER_COLOR:
            _draw_clipped_highlight(screen, t, r, cx, cy, config.COLOR_PIECE_HIGHLIGHT_WHITE,
                                     70, hi_r_frac=0.32, offset_frac=0.42)
        else:
            _draw_clipped_highlight(screen, t, r, cx, cy, config.COLOR_PIECE_HIGHLIGHT_BLACK,
                                     34, hi_r_frac=0.28, offset_frac=0.42)

        if piece.mounted_knight_id:
            pygame.draw.circle(screen, config.COLOR_HIGHLIGHT_MOUNT, (x + config.CELL_SIZE - 9, y + 9), 6)
            pygame.draw.circle(screen, (20, 20, 20), (x + config.CELL_SIZE - 9, y + 9), 6, 1)

        # Использованный ход затемняет только силуэт/статус, но HP-полосу
        # оставляем поверх, чтобы она всегда была читаемой.
        if not animating:
            if piece.actions_available() <= 0:
                overlay = pygame.Surface((config.CELL_SIZE, config.CELL_SIZE), pygame.SRCALPHA)
                overlay.fill(config.COLOR_USED_OVERLAY)
                screen.blit(overlay, (x, y))
            elif piece.max_actions() == 2 and piece.actions_used == 1:
                label = font_small.render("1/2", True, config.COLOR_TEXT)
                screen.blit(label, (x + config.CELL_SIZE - 24, y + config.CELL_SIZE - 16))

        # HP-полоса внизу клетки — не пересекается с верхней частью ладьи.
        bar_w = config.CELL_SIZE - 16
        bar_h = 7
        bar_x = x + 8
        bar_y = y + config.CELL_SIZE - 11
        hp_value = hp_override if hp_override is not None else piece.hp
        hp_color = config.COLOR_PLAYER_HP if piece.color == config.PLAYER_COLOR else config.COLOR_ENEMY_HP
        ratio = max(0.0, min(1.0, hp_value / piece.max_hp))
        if 0 < ratio <= config.LOW_HP_RATIO:
            hp_color = _lerp_color(hp_color, config.COLOR_HP_LOW_PULSE, _pulse(config.LOW_HP_PULSE_PERIOD))
        pygame.draw.rect(screen, config.COLOR_HP_BAR_BG, (bar_x, bar_y, bar_w, bar_h), border_radius=3)
        fill_w = max(0, int(bar_w * ratio))
        if fill_w > 0:
            pygame.draw.rect(screen, hp_color, (bar_x, bar_y, fill_w, bar_h), border_radius=3)
            # Тонкий блик по верхнему краю заполненной части — читается
            # как глянцевый "энергетический" индикатор, не просто плашка.
            hi_h = max(1, bar_h // 3)
            hi_w = max(0, fill_w - 2)
            if hi_w > 0:
                hi_bar = pygame.Surface((hi_w, hi_h), pygame.SRCALPHA)
                hi_bar.fill((*config.COLOR_HP_BAR_TOP, 60))
                screen.blit(hi_bar, (bar_x + 1, bar_y + 1))
        pygame.draw.rect(screen, config.COLOR_PANEL_BORDER, (bar_x, bar_y, bar_w, bar_h), 1, border_radius=3)

    @staticmethod
    def draw_all(screen, state, font_small, selected_piece_id=None, animation=None, visible_ids=None):
        """visible_ids: если не None — ограничивает набор ВРАЖЕСКИХ
        (не PLAYER_COLOR) фигур, которые вообще рисуются (Fog of War).
        Свои фигуры и вражеский король видны всегда."""
        for p in state.pieces.values():
            if not p.alive():
                continue
            if visible_ids is not None and p.color != config.PLAYER_COLOR:
                if p.type != "king" and p.id not in visible_ids:
                    continue
            override = animation.get_piece_override_pos(p.id) if animation else None
            scale = animation.get_piece_scale(p.id) if animation else 1.0
            PieceRenderer.draw(screen, p, font_small, selected=(p.id == selected_piece_id),
                                screen_pos=override, scale=scale)


# ---------------------------------------------------------------------------
# EffectsRenderer — атаки/попадания/смерть, полностью отдельно от
# PieceRenderer.
# ---------------------------------------------------------------------------
class EffectsRenderer:
    @staticmethod
    def _effect_color(color):
        return config.COLOR_EFFECT_PLAYER if color == config.PLAYER_COLOR else config.COLOR_EFFECT_ENEMY

    @staticmethod
    def draw_attack(screen, effect, attacker_color):
        if effect is None:
            return
        fx, fy = coords.board_to_screen_center(*effect["from"])
        tx, ty = coords.board_to_screen_center(*effect["to"])
        progress = effect["progress"]
        ptype = effect["ptype"]
        col = EffectsRenderer._effect_color(attacker_color)

        if ptype == "pawn" or ptype == "king":
            dx, dy = tx - fx, ty - fy
            dist = max(1.0, math.hypot(dx, dy))
            ux, uy = dx / dist, dy / dist
            reach = min(dist, config.CELL_SIZE * 0.6)
            p = math.sin(progress * math.pi)
            sx = fx + ux * reach * p
            sy = fy + uy * reach * p
            perp = (-uy, ux)
            p1 = (sx + perp[0] * 10, sy + perp[1] * 10)
            p2 = (sx - perp[0] * 10, sy - perp[1] * 10)
            glow = pygame.Surface((config.CELL_SIZE, config.CELL_SIZE), pygame.SRCALPHA)
            off = (int(sx - config.CELL_SIZE / 2), int(sy - config.CELL_SIZE / 2))
            pygame.draw.line(glow, (*col, 90), (p1[0] - off[0], p1[1] - off[1]), (p2[0] - off[0], p2[1] - off[1]), 9)
            screen.blit(glow, off)
            pygame.draw.line(screen, col, p1, p2, 4)

        elif ptype == "bishop":
            px = fx + (tx - fx) * progress
            py = fy + (ty - fy) * progress
            trail = (fx + (tx - fx) * max(0.0, progress - 0.15), fy + (ty - fy) * max(0.0, progress - 0.15))
            glow_s = pygame.Surface((config.CELL_SIZE, config.CELL_SIZE), pygame.SRCALPHA)
            pygame.draw.circle(glow_s, (*col, 90), (config.CELL_SIZE // 2, config.CELL_SIZE // 2), 11)
            screen.blit(glow_s, (int(px - config.CELL_SIZE / 2), int(py - config.CELL_SIZE / 2)))
            pygame.draw.line(screen, col, trail, (px, py), 3)
            pygame.draw.circle(screen, col, (int(px), int(py)), 6)
            pygame.draw.circle(screen, (255, 255, 255), (int(px), int(py)), 2)

        elif ptype == "rook":
            alpha = int(90 + 140 * progress)
            s = pygame.Surface((config.SCREEN_WIDTH, config.SCREEN_HEIGHT), pygame.SRCALPHA)
            pygame.draw.line(s, (*col, max(0, alpha // 3)), (fx, fy), (tx, ty), 15)
            pygame.draw.line(s, (*col, alpha), (fx, fy), (tx, ty), 7)
            screen.blit(s, (0, 0))
            pygame.draw.line(screen, (255, 255, 255), (fx, fy), (tx, ty), 2)

        elif ptype == "queen":
            # Ферзь атакует только в режиме RANGED (SPLASH убран) —
            # энергетический снаряд, летящий к цели.
            px = fx + (tx - fx) * progress
            py = fy + (ty - fy) * progress
            glow_s = pygame.Surface((config.CELL_SIZE, config.CELL_SIZE), pygame.SRCALPHA)
            pygame.draw.circle(glow_s, (*col, 80), (config.CELL_SIZE // 2, config.CELL_SIZE // 2), 14)
            screen.blit(glow_s, (int(px - config.CELL_SIZE / 2), int(py - config.CELL_SIZE / 2)))
            pygame.draw.circle(screen, col, (int(px), int(py)), 8)
            pygame.draw.circle(screen, (255, 255, 255), (int(px), int(py)), 3)

        elif ptype == "knight":
            px = fx + (tx - fx) * min(1.0, progress * 1.4)
            py = fy + (ty - fy) * min(1.0, progress * 1.4)
            glow_s = pygame.Surface((config.SCREEN_WIDTH, config.SCREEN_HEIGHT), pygame.SRCALPHA)
            pygame.draw.line(glow_s, (*col, 70), (fx, fy), (px, py), 11)
            screen.blit(glow_s, (0, 0))
            pygame.draw.line(screen, col, (fx, fy), (px, py), 5)
            pygame.draw.circle(screen, col, (int(px), int(py)), 5)

        else:
            pygame.draw.line(screen, col, (fx, fy), (tx, ty), 3)

    @staticmethod
    def draw_impact(screen, effect):
        cx, cy = coords.board_to_screen_center(*effect["cell"])
        progress = effect["progress"]
        radius = int(6 + 22 * progress)
        alpha = int(220 * (1 - progress))
        s = pygame.Surface((config.CELL_SIZE, config.CELL_SIZE), pygame.SRCALPHA)
        # Внешнее кольцо ударной волны + короткая яркая вспышка в центре
        # в первый момент — читается как "удар", а не просто расширяющийся круг.
        pygame.draw.circle(s, (*config.COLOR_EFFECT_IMPACT, alpha),
                            (config.CELL_SIZE // 2, config.CELL_SIZE // 2), radius, 3)
        core_alpha = int(200 * max(0.0, 1 - progress * 3))
        if core_alpha > 0:
            pygame.draw.circle(s, (255, 255, 255, core_alpha),
                                (config.CELL_SIZE // 2, config.CELL_SIZE // 2), max(2, int(9 * (1 - progress))))
        screen.blit(s, (cx - config.CELL_SIZE // 2, cy - config.CELL_SIZE // 2))

    @staticmethod
    def draw_particles(screen, effect):
        """Разлетающиеся искры при попадании/смерти — позиция каждой частицы
        считается заново из фиксированных angle/speed (см. animation.py),
        поэтому не нужно хранить и мутировать координаты кадр-в-кадр."""
        cx, cy = coords.board_to_screen_center(*effect["cell"])
        progress = effect["progress"]
        t = progress * config.PARTICLE_LIFETIME
        color = effect["color"]
        alpha = int(255 * max(0.0, 1 - progress) ** 1.4)
        if alpha <= 0:
            return
        for angle, speed in effect["particles"]:
            vx = math.cos(angle) * speed
            vy = math.sin(angle) * speed
            px = cx + vx * t
            py = cy + vy * t + 0.5 * config.PARTICLE_GRAVITY * t * t
            size = max(1, int(3.2 * (1 - progress)))
            spark = pygame.Surface((size * 2, size * 2), pygame.SRCALPHA)
            pygame.draw.circle(spark, (*color, alpha), (size, size), size)
            screen.blit(spark, (int(px) - size, int(py) - size))

    @staticmethod
    def draw_death(screen, effect):
        x, y = board_to_screen(*effect["cell"])
        progress = effect["progress"]
        fill = config.COLOR_PIECE_WHITE if effect["color"] == config.PLAYER_COLOR else config.COLOR_PIECE_BLACK
        alpha = int(200 * (1 - progress))
        scale = 1.0 - 0.4 * progress
        r = int((config.CELL_SIZE // 2 - 10) * scale)
        cx = x + config.CELL_SIZE // 2
        cy = y + config.CELL_SIZE // 2 - int(14 * progress)
        s = pygame.Surface((config.CELL_SIZE, config.CELL_SIZE), pygame.SRCALPHA)
        pygame.draw.circle(s, (*fill, alpha), (config.CELL_SIZE // 2, config.CELL_SIZE // 2 - int(14 * progress)), max(2, r))
        screen.blit(s, (x, y))
        for i in range(4):
            ang = i * (math.pi / 2) + progress * 2.0
            px = cx + math.cos(ang) * 18 * progress
            py = cy + math.sin(ang) * 18 * progress
            pygame.draw.circle(screen, fill, (int(px), int(py)), max(1, int(3 * (1 - progress))))

    @staticmethod
    def draw_damage_number(screen, effect):
        cx, cy = coords.board_to_screen_center(*effect["cell"])
        progress = effect["progress"]
        # Быстро появляется, затем плавно всплывает и растворяется.
        rise = config.DAMAGE_NUMBER_RISE * (progress ** 0.72)
        alpha = int(255 * min(1.0, max(0.0, (1.0 - progress) * 1.35)))
        size = max(18, int(config.CELL_SIZE * 0.34))
        font = make_font(size, bold=True)
        label = font.render(f"-{effect['amount']}", True, config.COLOR_DAMAGE_NUMBER)
        shadow = font.render(f"-{effect['amount']}", True, (20, 10, 10))
        label.set_alpha(alpha)
        shadow.set_alpha(alpha)
        x = int(cx + effect.get("offset_x", 0.0) * config.CELL_SIZE - label.get_width() / 2)
        y = int(cy - config.CELL_SIZE * 0.56 - rise)
        screen.blit(shadow, (x + 2, y + 2))
        screen.blit(label, (x, y))

    @staticmethod
    def draw_all(screen, animation, attacker_color_lookup=None):
        if animation is None:
            return
        atk = animation.get_attack_effect()
        if atk is not None:
            color = "white"
            if attacker_color_lookup is not None and animation.current is not None:
                color = attacker_color_lookup(animation.current.get("piece_id")) or "white"
            EffectsRenderer.draw_attack(screen, atk, color)
        for e in animation.get_effects():
            if e["kind"] == "impact":
                EffectsRenderer.draw_impact(screen, e)
            elif e["kind"] == "death":
                EffectsRenderer.draw_death(screen, e)
            elif e["kind"] == "particles":
                EffectsRenderer.draw_particles(screen, e)
            elif e["kind"] == "damage_number":
                EffectsRenderer.draw_damage_number(screen, e)


def draw_panel(screen, rect, radius=14):
    """Нейтральная поверхность меню/HUD: один визуальный материал для всех
    экранов — теперь с мягкой тенью позади и тонким "стеклянным" бликом
    вдоль верхней грани, вместо плоской заливки."""
    rect = pygame.Rect(rect)
    shadow = pygame.Surface((rect.width + 16, rect.height + 16), pygame.SRCALPHA)
    pygame.draw.rect(shadow, (0, 0, 0, 100), (8, 10, rect.width, rect.height), border_radius=radius)
    screen.blit(shadow, (rect.x - 8, rect.y - 6))

    pygame.draw.rect(screen, config.COLOR_PANEL_BG, rect, border_radius=radius)

    hi = pygame.Surface((rect.width, max(1, rect.height // 2)), pygame.SRCALPHA)
    pygame.draw.rect(hi, (*config.COLOR_PANEL_TOP_HI, 10), (0, 0, rect.width, hi.get_height()),
                      border_top_left_radius=radius, border_top_right_radius=radius)
    screen.blit(hi, (rect.x, rect.y))

    pygame.draw.rect(screen, config.COLOR_PANEL_BORDER, rect, 1, border_radius=radius)


def draw_button(screen, rect, text, font, active=False, hover=False, enabled=True):
    rect = pygame.Rect(rect)
    color = config.COLOR_BUTTON_ACTIVE if active else (config.COLOR_BUTTON_HOVER if hover else config.COLOR_BUTTON)
    if not enabled:
        color = (34, 34, 38)
    draw_rect = rect.inflate(-2, -2) if hover and enabled else rect
    pygame.draw.rect(screen, color, draw_rect, border_radius=10)

    if enabled:
        hi = pygame.Surface((draw_rect.width, max(1, draw_rect.height // 2)), pygame.SRCALPHA)
        pygame.draw.rect(hi, (*config.COLOR_BUTTON_TOP_HI, 16 if not active else 24),
                          (0, 0, draw_rect.width, hi.get_height()),
                          border_top_left_radius=10, border_top_right_radius=10)
        screen.blit(hi, (draw_rect.x, draw_rect.y))

    border = config.COLOR_ACCENT if active else (config.COLOR_ACCENT_DIM if hover else config.COLOR_PANEL_BORDER)
    pygame.draw.rect(screen, border, draw_rect, 2 if active else 1, border_radius=10)
    text_color = config.COLOR_TEXT if enabled else config.COLOR_TEXT_DIM
    label = font.render(text, True, text_color)
    lr = label.get_rect(center=draw_rect.center)
    screen.blit(label, lr)


def draw_text(screen, text, pos, font, color=None):
    color = color or config.COLOR_TEXT
    label = font.render(text, True, color)
    screen.blit(label, pos)


# ---------------------------------------------------------------------------
# Обратно совместимые модульные обёртки (main.py вызывает их как R.xxx)
# ---------------------------------------------------------------------------
def draw_board(screen):
    BoardRenderer.draw(screen)


def draw_zone_tint(screen, rows, color, alpha=30):
    BoardRenderer.draw_zone_tint(screen, rows, color, alpha=alpha)


def draw_highlights(screen, cells, color, alpha=95):
    BoardRenderer.draw_highlights(screen, cells, color, alpha=alpha)


def draw_cell_border(screen, col, row, color, width=3):
    BoardRenderer.draw_cell_border(screen, col, row, color, width=width)


def draw_fog_cell(screen, col, row):
    BoardRenderer.draw_fog_cell(screen, col, row)


def draw_piece(screen, piece, font_small, selected=False, screen_pos=None, hp_override=None):
    PieceRenderer.draw(screen, piece, font_small, selected=selected, screen_pos=screen_pos, hp_override=hp_override)


def draw_all_pieces(screen, state, font_small, selected_piece_id=None, animation=None, visible_ids=None):
    PieceRenderer.draw_all(screen, state, font_small, selected_piece_id=selected_piece_id,
                            animation=animation, visible_ids=visible_ids)


def draw_attack_effect(screen, effect, attacker_color):
    EffectsRenderer.draw_attack(screen, effect, attacker_color)


def draw_impact_effect(screen, effect):
    EffectsRenderer.draw_impact(screen, effect)


def draw_death_effect(screen, effect):
    EffectsRenderer.draw_death(screen, effect)


def draw_animation_effects(screen, animation, attacker_color_lookup=None):
    EffectsRenderer.draw_all(screen, animation, attacker_color_lookup=attacker_color_lookup)
