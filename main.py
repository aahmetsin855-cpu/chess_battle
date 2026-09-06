# -*- coding: utf-8 -*-
"""
HP Battle Chess — офлайн 2D игра на Python 3 + Pygame.

Запуск:
    python3 main.py

Это НЕ классические шахматы — см. README.md для полного описания правил.

Поток экранов:
    SELECT GAME MODE -> SELECT BOARD SIZE -> расстановка армии ->
    SELECT OPPONENT (+ сложность) -> партия -> game over

Горячие клавиши:
    Esc  — выход
    F11  — переключить полноэкранный режим
"""
import os
import subprocess
import sys
import threading
from pathlib import Path
import time
import uuid

# --- Режим-хелпер диалога выбора файла --------------------------------
# ВАЖНО ДЛЯ PYINSTALLER: раньше диалог запускался как отдельный процесс
# через `sys.executable -c <скрипт>` — это работает, когда sys.executable
# это настоящий python.exe, но ЛОМАЕТСЯ в собранном PyInstaller-приложении:
# там sys.executable — это САМ .exe игры (отдельного интерпретатора python
# внутри onedir/onefile-сборки нет), и "-c" для него — это просто
# неизвестный аргумент запуска игры, а не флаг питона. Вместо отдельного
# скрипта .exe теперь умеет запускать САМ СЕБЯ в лёгком режиме-хелпере по
# этому флагу — работает одинаково что в собранном виде, что в виде
# исходников, и не инициализирует pygame/SDL вообще (проверяется ДО всех
# тяжёлых импортов ниже).
if len(sys.argv) > 1 and sys.argv[1] == "--file-dialog-helper":
    def _run_file_dialog_helper():
        kind = sys.argv[2] if len(sys.argv) > 2 else "background"
        initialdir = sys.argv[3] if len(sys.argv) > 3 else None
        try:
            import tkinter as tk
            from tkinter import filedialog
        except Exception as exc:
            print("__ERROR__:" + str(exc))
            sys.exit(1)
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        # update() (не update_idletasks()) прогоняет и события тоже —
        # это даёт оконному менеджеру реально зарегистрировать root
        # ДО того, как поверх него откроется модальный диалог.
        root.update()
        if kind == "music":
            path = filedialog.askopenfilename(
                parent=root, title="Выберите музыку", initialdir=initialdir,
                filetypes=[("Аудио", "*.mp3 *.ogg *.wav *.flac"), ("Все файлы", "*.*")],
            )
        else:
            path = filedialog.askopenfilename(
                parent=root, title="Выберите фон", initialdir=initialdir,
                filetypes=[("Изображения", "*.png *.jpg *.jpeg *.bmp *.webp"), ("Все файлы", "*.*")],
            )
        root.destroy()
        print(path or "")

    _run_file_dialog_helper()
    sys.exit(0)
# ------------------------------------------------------------------------

import platform_utils
import network

import pygame

import config
import rules
import actions as actions_mod
import turn_system
import coords
import fog
from game_state import GameState
import ai as ai_core
import ai_interface
import ai_memory
import settings as settings_mod
import tutorial as tutorial_mod
from animation import AnimationController
import renderer as R


PIECE_NAMES_RU = {"pawn": "Пешка", "king": "Король", "bishop": "Слон",
                   "rook": "Ладья", "queen": "Ферзь", "knight": "Конь"}

QUEEN_MODE_NAMES = {"move": "ПЕРЕМЕЩЕНИЕ", "queen_ranged": "ДАЛЬНЯЯ АТАКА"}

# Состояния хода ИИ (предсказуемая state machine вместо мгновенного
# применения всего хода целиком).
AI_IDLE = "AI_IDLE"
AI_THINKING = "AI_THINKING"
AI_EXECUTING = "AI_EXECUTING"
AI_FINISHED = "AI_FINISHED"


def default_white_layout():
    """Стартовая расстановка белых — осмысленная формация (пехота у
    фронта, тяжёлые фигуры в тылу), которую игрок может свободно менять
    перетаскиванием в пределах своей зоны расстановки перед началом
    партии. Масштабируется под текущий размер доски (config.BOARD_SIZE)."""
    return ai_core.suggest_formation("white", config.WHITE_HALF_ROWS, (config.BOARD_WIDTH, config.BOARD_HEIGHT), samples=4)


def ai_black_layout():
    """Расстановка ИИ: осмысленная формация (см. ai.suggest_formation),
    НЕ чисто случайная и НЕ жёстко заскриптованная — ИИ оценивает
    несколько вариантов и выбирает лучший по простой эвристике."""
    return ai_core.suggest_formation("black", config.BLACK_HALF_ROWS, (config.BOARD_WIDTH, config.BOARD_HEIGHT))


class Button:
    def __init__(self, rect, text):
        self.rect = pygame.Rect(rect)
        self.text = text
        self.enabled = True

    def handle_click(self, pos):
        return self.enabled and self.rect.collidepoint(pos)

    def draw(self, screen, font, mouse_pos, active=False):
        hover = self.rect.collidepoint(mouse_pos)
        # Состояние выбора показываем стилем кнопки; лишние символы вроде ✓
        # внутри текста больше не добавляем.
        R.draw_button(screen, self.rect, self.text, font, active=active, hover=hover, enabled=self.enabled)


class Layout:
    """Простой вертикальный курсор для раскладки меню без наложений:
    каждый вызов take()/line() возвращает Y и сдвигает курсор вниз.
    Использование этого хелпера на всех экранах — способ системно
    избежать наложения текста/кнопок вместо ручной арифметики координат."""

    def __init__(self, start_y, x=0):
        self.y = start_y
        self.x = x

    def take(self, height, gap=10):
        y = self.y
        self.y += height + gap
        return y


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------
class App:
    def __init__(self):
        pygame.init()
        # Разрешение доски (CELL_SIZE) больше не завязано на произвольную
        # константу — оно считается от реального доступного места на
        # экране, иначе доска выглядит одинаково мелкой что на ноутбуке,
        # что на большом мониторе. В оконном режиме оставляем 85% от
        # рабочего стола (место под панель задач/рамку окна); в fullscreen
        # используется фактический размер экрана (см. _apply_screen_size).
        try:
            _disp_info = pygame.display.Info()
            desktop_w, desktop_h = int(_disp_info.current_w), int(_disp_info.current_h)
        except Exception:
            desktop_w, desktop_h = 0, 0
        if desktop_w <= 0 or desktop_h <= 0:
            desktop_w, desktop_h = 1280, 800
        self._desktop_size = (desktop_w, desktop_h)
        self._windowed_budget = (int(desktop_w * 0.85), int(desktop_h * 0.85))
        config.set_available_area(*self._windowed_budget)

        self.audio_available = False
        try:
            pygame.mixer.init()
            self.audio_available = True
        except pygame.error:
            self.audio_available = False
        pygame.display.set_caption(config.GAME_TITLE)
        self.settings = settings_mod.load_settings()
        # Android-версия всегда fullscreen+landscape (см. п.3 ТЗ) — это не
        # пользовательская настройка на этой платформе, а требование
        # платформы (нет desktop-style window management), поэтому
        # значение из settings.json игнорируется на Android, но сам файл
        # настроек не трогаем — Windows-поведение (настройка сохраняется
        # и уважается) остаётся прежним.
        self.fullscreen = True if platform_utils.is_android() else bool(self.settings.get("fullscreen", False))
        self.music_path = str(self.settings.get("music_path", "") or "")
        self.background_path = str(self.settings.get("background_path", "") or "")
        self.background_surface = None
        self._background_cache_size = None
        self._load_selected_media()
        # Реальное создание окна (и пересчёт CELL_SIZE под него) делает
        # _apply_screen_size() чуть ниже, после того как выбран/загружен
        # размер доски — дублировать это здесь не нужно.
        self.clock = pygame.time.Clock()

        # make_font() оборачивает встроенный pygame.font.Font(None, ...) —
        # без зависимости от системного шрифта Arial, которого на Android
        # (и не только) может не быть (см. renderer.make_font).
        self.font = R.make_font(17)
        self.font_small = R.make_font(13)
        self.font_big = R.make_font(26, bold=True)
        self.font_mid = R.make_font(19, bold=True)

        # --- Настройки и память ИИ (переживают перезапуски) ---
        self.ai_memory = ai_memory.load_memory()

        self.selected_board_size = self.settings.get("board_size", (config.BOARD_WIDTH, config.BOARD_HEIGHT))
        self.selected_game_mode = self.settings.get("game_mode", config.DEFAULT_GAME_MODE)
        config.configure_board_size(self.selected_board_size)
        self._apply_screen_size()

        self.state = GameState()
        # Настоящее главное меню; Play ведёт к выбору режима.
        self.state.phase = "main_menu"

        self.setup_pieces = []
        self.dragging_index = None
        self.drag_offset = (0, 0)

        self.selected_opponent_type = self.settings.get("ai_type", "algorithm")
        self.selected_difficulty = self.settings.get("difficulty", "normal")
        self.ai_player = None

        self.selected_piece_id = None
        self.action_map = {}       # (col,row) -> action dict (move/attack/heal)
        self.special_actions = []  # действия без клетки на доске (queen_lock/queen_unlock)
        self.dismount_options = []  # доступные действия спешивания коня для выбранной фигуры
        self.dismount_armed = False  # True, пока игрок выбирает клетку для спешивания

        self.animation = AnimationController(speed=self.settings.get("animation_speed", 1.0))

        # --- Обучение ("Обучение" в главном меню) --------------------------
        # Полностью отдельный флаг верхнего уровня — НЕ self.state.phase,
        # потому что урок про стадию короля намеренно использует настоящие
        # значения phase ("king_stage"/"lobby_stage"), чтобы показать
        # реальную механику, а не имитацию. self.tutorial_active решает,
        # какой обработчик клика/отрисовки главный цикл вызывает, и не
        # пересекается с обычным self.state.phase-переключателем экранов.
        self.tutorial_active = False
        self.tutorial_step_index = 0
        self.tutorial_pending_advance = False  # шаг завершён, ждём конца анимации перед переходом дальше
        self.tutorial_advance_timer = None  # пауза после результата действия — см. TUTORIAL_ADVANCE_PAUSE
        self.tutorial_saved_state = None
        self.tutorial_saved_player_color = None
        self.tutorial_saved_viewer_color = None

        self.ai_status = AI_IDLE
        self.ai_status_text = ""
        self.ai_thread = None
        self.ai_thread_result = {}
        self.ai_plan = []
        self.ai_step_index = 0
        self.ai_step_pause_timer = 0.0
        self.ai_first_action_type = None
        self.ai_used_mount = False
        self.memory_saved = False

        # --- LAN multiplayer -------------------------------------------------
        self.network_role = None          # None | "host" | "client"
        self.network_host = None          # network.HostSession
        self.network_client = None        # network.ClientSession
        self.player_color = "white"
        self.network_screen = "menu"   # menu | browser | ip | host_wait | setup_black | game
        self.network_games = []
        self.network_scan_thread = None
        self.network_scan_result = []
        self.network_scan_busy = False
        # Direct LAN connection fields. IP starts empty (no fake example);
        # port defaults to the protocol port but can be edited independently.
        self.network_ip_text = ""
        self.network_port_text = str(network.DEFAULT_TCP_PORT)
        self.network_ip_active = False
        self.network_port_active = False
        self._text_input_active = False  # см. _sync_text_input_state
        self.pending_network_host = False
        self.network_status = ""
        self._file_dialog_proc = None
        self._file_dialog_kind = None
        self.network_seen_actions = set()
        self.network_reconnect_timer = 0.0
        self.network_reconnect_attempts = 0
        self.network_rematch_sent = False
        self.pending_network_action = None
        self.network_anim_queue = []  # см. _pump_network_animations
        self.setup_player_color = "white"
        self.remote_setup_pending = False
        self.timer_beeped_turn = None
        self.standard_timer_last = None

        # Fog of War: id вражеских фигур, временно раскрытых атакой
        self.temporarily_revealed_ids = set()

        self._build_ui_widgets()
        self._rebuild_default_setup_pieces()

        self.running = True

    # ------------------------------------------------------------------
    # Экран/окно
    # ------------------------------------------------------------------
    def _apply_screen_size(self, force_recreate=True):
        """force_recreate=False пропускает пересоздание SDL-окна в
        fullscreen, если оно уже есть — используется, когда меняется
        только размер КАРТЫ, а не сам режим fullscreen/windowed.

        В fullscreen физический размер экрана и так не меняется между
        картами (это всегда весь монитор в текущем видеорежиме) —
        пересоздавать окно (pygame.display.set_mode) ради пересчёта
        CELL_SIZE не нужно, а именно это раньше вызывало заметное мигание
        экрана при каждом выборе размера карты (переключение видеорежима
        мигает почти на любой системе, это системное поведение SDL/ОС, а
        не баг, но вызывать его без необходимости — лишнее). В windowed
        же режиме set_mode() ниже вызывается всегда: там окно ДЕЙСТВИТЕЛЬНО
        должно изменить размер под новую карту, и обычный ресайз окна
        так не мигает."""
        if self.fullscreen:
            if force_recreate or self.screen is None:
                if platform_utils.is_android():
                    # На Android нет desktop-style window management (нет
                    # чужих окон, с которыми могло бы конфликтовать
                    # эксклюзивное переключение видеорежима, и нет
                    # tkinter-диалога поверх игры — см. _choose_file,
                    # которая на Android вообще не запускает subprocess).
                    # (0, 0) — стандартный для python-for-android паттерн
                    # "взять текущее разрешение экрана устройства";
                    # альбомная ориентация задаётся декларативно на уровне
                    # Android-манифеста (buildozer.spec: orientation =
                    # landscape), а не через размер surface здесь.
                    self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
                else:
                    # ВАЖНО: используем "оконный" (borderless) fullscreen —
                    # обычное окно без рамки, растянутое на весь рабочий стол —
                    # а НЕ pygame.FULLSCREEN. Настоящий эксклюзивный fullscreen
                    # захватывает дисплей на уровне драйвера, и переключение
                    # этого режима вокруг блокирующего диалога tkinter (см.
                    # _choose_file) — известная причина полного зависания
                    # системы на Windows, а не только самой игры. Borderless
                    # окно ведёт себя как обычное окно и не конфликтует с
                    # другими окнами/диалогами.
                    self.screen = pygame.display.set_mode(self._desktop_size, pygame.NOFRAME)
            actual_w, actual_h = self.screen.get_size()
            # Доска должна реально использовать весь экран в fullscreen,
            # а не оставаться маленьким островком посреди тёмного фона —
            # поэтому бюджет пикселей под доску пересчитывается от
            # фактического разрешения монитора, а не от оконного (85%).
            config.set_available_area(actual_w, actual_h)
            config.configure_board_size((config.BOARD_WIDTH, config.BOARD_HEIGHT))
            config.SCREEN_WIDTH, config.SCREEN_HEIGHT = actual_w, actual_h
        else:
            config.set_available_area(*self._windowed_budget)
            config.configure_board_size((config.BOARD_WIDTH, config.BOARD_HEIGHT))
            config.SCREEN_WIDTH, config.SCREEN_HEIGHT = config.get_windowed_size()
            self.screen = pygame.display.set_mode((config.SCREEN_WIDTH, config.SCREEN_HEIGHT))
        self._center_board_group()

    def toggle_fullscreen(self):
        if platform_utils.is_android():
            # На Android "оконный" режим не существует как концепция
            # (нет desktop window management, см. п.3 ТЗ) — приложение
            # всегда fullscreen. F11/кнопка настроек не должны пытаться
            # создать windowed-режим здесь: это не поддерживаемая на
            # платформе конфигурация и она не нужна пользователю Android.
            return
        self.fullscreen = not self.fullscreen
        self.settings["fullscreen"] = self.fullscreen
        settings_mod.save_settings(self.settings)
        self._apply_screen_size()
        self._background_cache_size = None
        self._build_ui_widgets()

    # ------------------------------------------------------------------
    # Пересборка UI-виджетов/раскладки под текущий config.BOARD_SIZE
    # ------------------------------------------------------------------
    def _center_board_group(self):
        # Доска + HUD образуют одну композицию, но на широких картах
        # композицию слегка смещаем вправо. Без этого 14x10 визуально
        # "уезжала" заметно сильнее маленькой карты: центрирование целой
        # группы математически верное, но зрительно перегружает левую часть.
        board_w_px = config.BOARD_WIDTH * config.CELL_SIZE
        board_h_px = config.BOARD_HEIGHT * config.CELL_SIZE
        gap = 24
        group_px = board_w_px + gap + config.SIDE_PANEL_WIDTH

        # По горизонтали центрируем именно композицию «доска + HUD», но не
        # добавляем отдельный сдвиг для широких карт: он был причиной того,
        # что 12x8 визуально заметно уезжала влево/вправо относительно панели.
        config.BOARD_MARGIN_X = max(24, (config.SCREEN_WIDTH - group_px) // 2)

        # В окне высота может относиться к предыдущему размеру карты. Поэтому
        # не используем старый центр от полного окна; держим поле в стабильной
        # верхней зоне и привязываем все кнопки к его низу.
        top_pad = 42
        config.BOARD_MARGIN_Y = max(24, min(top_pad, config.SCREEN_HEIGHT - board_h_px - 24))

    def _set_viewer_color(self, color):
        config.VIEWER_COLOR = color if color in ("white", "black") else "white"

    def _build_ui_widgets(self):
        self._center_board_group()
        panel_x = config.BOARD_MARGIN_X + config.BOARD_WIDTH * config.CELL_SIZE + 20
        self.panel_x = panel_x
        menu_x = panel_x - 240  # общий левый край для полноэкранных меню-оверлеев

        # --- Главное меню --------------------------------------------------
        main_x = config.SCREEN_WIDTH // 2 - 180
        self.main_menu_buttons = {
            "play": Button((main_x, 230, 360, 52), "ИГРАТЬ"),
            "network": Button((main_x, 298, 360, 52), "МУЛЬТИПЛЕЕР ПО СЕТИ"),
            "tutorial": Button((main_x, 366, 360, 52), "ОБУЧЕНИЕ"),
            "settings": Button((main_x, 434, 360, 52), "НАСТРОЙКИ"),
            "exit": Button((main_x, 502, 360, 52), "ВЫХОД"),
        }
        self.settings_fullscreen_button = Button((main_x, 250, 360, 46), "ПОЛНЫЙ ЭКРАН")
        self.settings_music_button = Button((main_x, 306, 360, 46), "МУЗЫКА: ВЫБРАТЬ")
        self.settings_background_button = Button((main_x, 362, 360, 46), "ФОН: ВЫБРАТЬ")
        self.settings_back_button = Button((main_x, 432, 360, 46), "НАЗАД")

        # --- Выбор игрового режима (ПЕРВЫЙ экран) ---
        lay = Layout(150)
        self.game_mode_buttons = {}
        for mode in config.GAME_MODES:
            y = lay.take(48, gap=26)  # доп. место под строку описания под кнопкой
            self.game_mode_buttons[mode] = Button((menu_x, y, 480, 48), config.GAME_MODE_NAMES[mode])
        self.game_mode_next_button = Button((menu_x + 130, lay.take(48, gap=0), 220, 48), "Далее")

        # --- Выбор размера доски (ВТОРОЙ экран, после режима) ---
        lay2 = Layout(190)
        row_y = lay2.take(56, gap=20)
        self.board_size_buttons = {}
        for i, size in enumerate(config.BOARD_SIZE_OPTIONS):
            self.board_size_buttons[size] = Button((menu_x + i * 160, row_y, 140, 56), f"{size[0]} x {size[1]}")
        self.board_size_next_button = Button((menu_x + 130, lay2.take(48, gap=0), 220, 48), "Далее")

        # --- Расстановка (кнопки закреплены снизу панели) ---
        # ВАЖНО: раньше эти кнопки были привязаны к config.SCREEN_HEIGHT —
        # абсолютному низу ОКНА. В windowed-режиме окно всегда точно по
        # размеру доски (SCREEN_HEIGHT = BOARD_MARGIN_Y + доска + 50), так
        # что разницы не было видно. Но в fullscreen окно = весь монитор,
        # а доска (даже с новым адаптивным CELL_SIZE) занимает лишь часть
        # экрана и центрируется — в результате кнопки "прилипали" к
        # физическому низу экрана, отрываясь от текста/доски огромным
        # пустым провалом. Теперь якорь — низ САМОЙ ДОСКИ, а не окна.
        board_bottom = config.BOARD_MARGIN_Y + config.BOARD_HEIGHT * config.CELL_SIZE
        self.ready_button = Button((panel_x, board_bottom - 120, config.SIDE_PANEL_WIDTH - 40, 44), "Начать партию")
        self.random_button = Button((panel_x, board_bottom - 68, config.SIDE_PANEL_WIDTH - 40, 36), "Случайная расстановка")

        # --- Игровой экран ---
        self.skip_king_button = Button((panel_x, board_bottom - 92, config.SIDE_PANEL_WIDTH - 40, 40), "Пропустить действие короля")
        self.end_turn_button = Button((panel_x, board_bottom - 40, config.SIDE_PANEL_WIDTH - 40, 40), "Завершить ход")
        self.restart_button = Button((config.SCREEN_WIDTH // 2 - 100, config.SCREEN_HEIGHT // 2 + 40, 200, 46), "Новая игра")
        self.network_rematch_button = Button((config.SCREEN_WIDTH // 2 - 110, config.SCREEN_HEIGHT // 2 + 35, 220, 42), "ИГРАТЬ СНОВА")
        self.network_exit_button = Button((config.SCREEN_WIDTH // 2 - 110, config.SCREEN_HEIGHT // 2 + 88, 220, 42), "ВЫЙТИ")
        self.surrender_button = Button((panel_x, board_bottom - 150, config.SIDE_PANEL_WIDTH - 40, 34), "СДАТЬСЯ")

        # --- Обучение: все нижние кнопки привязаны к нижней части панели
        # и не зависят от высоты текста карточки. Реальные позиции
        # выставляются каждый кадр в draw_tutorial_panel().
        tut_btn_w = config.SIDE_PANEL_WIDTH - 40
        self.tutorial_back_button = Button((panel_x, board_bottom - 92, tut_btn_w, 40), "Назад")
        self.tutorial_next_button = Button((panel_x, board_bottom - 40, tut_btn_w, 40), "Далее")
        self.tutorial_skip_button = Button((panel_x, board_bottom - 212, tut_btn_w, 30), "Пропустить обучение")

        # Ферзь: MOVE выполняется обычным кликом по доске (действие уже
        # есть в action_map). Кнопки — только для входа/выхода из режима
        # турели (see actions.py: queen_lock / queen_unlock).
        # У ферзя только ОДИН режим атаки вообще — дальняя атака (SPLASH убран).
        self.queen_lock_buttons = {
            "queen_ranged": Button((panel_x, 300, config.SIDE_PANEL_WIDTH - 40, 34), "Включить дальнюю атаку"),
        }
        self.queen_unlock_button = Button((panel_x, 300, config.SIDE_PANEL_WIDTH - 40, 34), "Выйти из режима ДАЛЬНЕЙ АТАКИ")

        # Спешивание коня: сначала "вооружить" действие кнопкой, затем
        # выбрать клетку на доске (см. build_action_map — dismount-цели
        # не подмешиваются в обычную карту ходов во избежание коллизий).
        self.dismount_button = Button((panel_x, 380, config.SIDE_PANEL_WIDTH - 40, 32), "Спешить коня")

        # --- Выбор противника ---
        self.opponent_type_buttons = {
            "algorithm": Button((menu_x, 160, 220, 60), "Алгоритмический ИИ"),
            "local": Button((menu_x + 260, 160, 220, 60), "Локальный ИИ"),
            "network": Button((menu_x, 390, 480, 56), "Мультиплеер по локальной сети"),
        }
        self.difficulty_buttons = {
            "easy": Button((menu_x, 296, 140, 40), "Легко"),
            "normal": Button((menu_x + 160, 296, 140, 40), "Нормально"),
            "hard": Button((menu_x + 320, 296, 140, 40), "Сложно"),
        }
        self.opponent_confirm_button = Button((menu_x + 130, 470, 220, 48), "Начать игру")

        # --- LAN screen ---
        self.lan_host_button = Button((menu_x, 190, 480, 48), "СОЗДАТЬ КОМНАТУ")
        self.lan_find_button = Button((menu_x, 250, 480, 48), "НАЙТИ КОМНАТЫ")
        self.lan_ip_button = Button((menu_x, 310, 480, 48), "ПОДКЛЮЧИТЬСЯ ПО IP")
        self.lan_back_button = Button((menu_x, 370, 480, 48), "НАЗАД")
        self.lan_refresh_button = Button((menu_x, 150, 180, 40), "СКАНИРОВАТЬ СНОВА")
        self.lan_ip_join_button = Button((menu_x + 250, 380, 230, 46), "ПОДКЛЮЧИТЬСЯ")
        self.lan_ip_cancel_button = Button((menu_x, 380, 220, 46), "НАЗАД")
        self.lan_back_menu_button = Button((menu_x, 500, 480, 42), "НАЗАД В МЕНЮ СЕТИ")

    def _rebuild_default_setup_pieces(self):
        self.setup_pieces = [{"type": t, "col": c, "row": r} for c, r, t in default_white_layout()]

    # ------------------------------------------------------------------
    # Локальные медиа-настройки
    # ------------------------------------------------------------------
    def _load_selected_media(self):
        self.background_surface = None
        self._background_cache_size = None
        if self.background_path:
            try:
                image = pygame.image.load(self.background_path).convert()
                self.background_surface = image
            except (OSError, pygame.error):
                self.background_path = ""
                self.settings["background_path"] = ""

        if self.audio_available:
            try:
                pygame.mixer.music.stop()
                if self.music_path and os.path.isfile(self.music_path):
                    pygame.mixer.music.load(self.music_path)
                    volume = max(0.0, min(1.0, float(self.settings.get("music_volume", 0.7))))
                    pygame.mixer.music.set_volume(volume)
                    pygame.mixer.music.play(-1)
                else:
                    self.music_path = ""
                    self.settings["music_path"] = ""
            except (OSError, pygame.error, ValueError):
                self.music_path = ""
                self.settings["music_path"] = ""

    def _update_background_surface(self):
        if self.background_surface is None:
            config.BACKGROUND_SURFACE = None
            return
        size = (config.SCREEN_WIDTH, config.SCREEN_HEIGHT)
        if self._background_cache_size != size:
            src = self.background_surface
            sw, sh = src.get_size()
            if sw <= 0 or sh <= 0:
                config.BACKGROUND_SURFACE = None
                return
            scale = max(size[0] / sw, size[1] / sh)
            nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
            scaled = pygame.transform.smoothscale(src, (nw, nh))
            ox = max(0, (nw - size[0]) // 2)
            oy = max(0, (nh - size[1]) // 2)
            self._background_screen = scaled.subsurface((ox, oy, size[0], size[1])).copy()
            self._background_cache_size = size
        config.BACKGROUND_SURFACE = self._background_screen

    def _file_dialog_helper_command(self, kind):
        """Команда для запуска этого же .exe/скрипта в режиме-хелпере
        диалога (см. --file-dialog-helper в самом верху файла).
        В собранном PyInstaller-приложении sys.executable — это сам .exe
        игры, и его достаточно вызвать с флагом. При запуске из исходников
        sys.executable — это просто python.exe, которому дополнительно
        нужен путь к main.py (иначе '--file-dialog-helper' попадёт в
        python как неизвестный флаг интерпретатора, а не аргумент игры)."""
        initialdir = os.path.expanduser("~")
        if getattr(sys, "frozen", False):
            return [sys.executable, "--file-dialog-helper", kind, initialdir]
        return [sys.executable, os.path.abspath(sys.argv[0]), "--file-dialog-helper", kind, initialdir]

    def _choose_file(self, kind):
        """Запускает системный диалог выбора файла в ОТДЕЛЬНОМ процессе.

        ПОЧЕМУ отдельный процесс, а не просто "тот же tkinter-код, но без
        переключения fullscreen" (как было раньше): tkinter и pygame — это
        два разных GUI-тулкита, и запуск event-loop'а второго тулкита
        БЛОКИРУЮЩИМ вызовом прямо внутри процесса pygame ведёт себя
        по-разному и одинаково плохо на разных системах:
          - на X11 (тестировалось) диалог открывается, но пока он висит,
            цикл pygame не крутится вообще — окно игры не перерисовывается
            и не отвечает композитору, поэтому при перетаскивании диалога
            по экрану игра оставляет "шлейф" из старого кадра;
          - на Wayland (Ubuntu) второй Tk-рут, создаваемый из процесса,
            который уже держит SDL/Wayland-поверхность, у многих
            композиторов вообще не получает поверхность и не появляется
            на экране — диалог "открывается", но невидим и по факту снова
            блокирует игру намертво, только уже без всякого диалога.
        Вынос диалога в отдельный процесс полностью решает оба случая: это
        независимое Tk-приложение со своим окном, которое ОС создаёт и
        показывает так же, как любую другую программу, а мы не блокируем
        цикл pygame — просто опрашиваем процесс каждый кадр (см.
        _poll_file_dialog), продолжая рисовать игру как обычно.
        """
        if platform_utils.is_android():
            # На Android нет tkinter/subprocess-диалога выбора файла (см.
            # docstring выше — весь механизм рассчитан на desktop-тулкит).
            # Не пытаемся реализовывать Android Storage Access Framework
            # сейчас (это отдельная, более рискованная задача) — просто
            # сообщаем пользователю, что выбор файла недоступен, и не
            # ломаем остальной экран настроек.
            self.network_status = "Выбор файла недоступен на Android в этой версии"
            return
        if self._file_dialog_proc is not None:
            return  # диалог уже открыт — не плодим второй поверх первого
        try:
            proc = subprocess.Popen(
                self._file_dialog_helper_command(kind),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        except Exception as exc:
            self.network_status = f"Не удалось открыть выбор файла: {exc}"
            return
        self._file_dialog_proc = proc
        self._file_dialog_kind = kind
        self._file_dialog_started_at = time.time()
        self.network_status = "Открывается диалог выбора файла…"

    _FILE_DIALOG_WATCHDOG_SECONDS = 300  # 5 минут — щедрый запас на выбор файла

    def _poll_file_dialog(self):
        """Вызывается каждый кадр из главного цикла, пока диалог открыт.
        Не блокирует: если процесс ещё не завершился — просто выходит.

        ВАЖНО (watchdog): если по любой причине процесс диалога не
        завершается сам (зависший Tk event loop, особенность оконного
        менеджера при закрытии окна и т.п.), poll() будет возвращать None
        бесконечно. Без watchdog'а это навсегда блокирует повторное
        открытие диалога — _choose_file видит self._file_dialog_proc не
        None и просто ничего не делает при следующих нажатиях кнопки.
        Именно это выглядит как "закрыл диалог — он больше не
        появляется". Поэтому после разумного тайм-аута процесс
        принудительно убивается и состояние сбрасывается независимо от
        того, что именно пошло не так внутри него."""
        proc = self._file_dialog_proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if time.time() - self._file_dialog_started_at > self._FILE_DIALOG_WATCHDOG_SECONDS:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        pass
                    self._file_dialog_proc = None
                    self._file_dialog_kind = None
                    self.network_status = "Диалог выбора файла не ответил и был закрыт — попробуйте снова."
                return  # ещё выбирает файл — ничего не делаем в этом кадре
            kind = self._file_dialog_kind
            self._file_dialog_proc = None
            self._file_dialog_kind = None
            try:
                out, err = proc.communicate(timeout=2)
            except Exception:
                out, err = "", ""
            out = (out or "").strip()
            if proc.returncode != 0 or out.startswith("__ERROR__:"):
                detail = out[len("__ERROR__:"):] if out.startswith("__ERROR__:") else (err or "").strip()
                if "tkinter" in detail.lower() or "tkinter" in (err or "").lower():
                    # На Debian/Ubuntu/Xubuntu пакет python3-tk НЕ ставится
                    # вместе с python3 по умолчанию (в отличие от Fedora, где
                    # tkinter обычно уже есть) — без него диалог не открывается
                    # молча, без единого признака ошибки на экране.
                    self.network_status = "Диалог недоступен: не найден python3-tk. Установите: sudo apt install python3-tk"
                else:
                    self.network_status = f"Не удалось открыть выбор файла: {detail or 'нет доступного диалога'}"
                return
            if not out:
                self.network_status = ""
                return  # пользователь закрыл диалог без выбора файла
            self._apply_chosen_file(kind, out)
        except Exception as exc:
            # Что бы тут ни пошло не так — состояние ОБЯЗАНО сброситься,
            # иначе кнопка выбора файла молча перестаёт что-либо делать
            # навсегда (см. docstring выше).
            self._file_dialog_proc = None
            self._file_dialog_kind = None
            self.network_status = f"Не удалось открыть выбор файла: {exc}"

    def _apply_chosen_file(self, kind, path):
        path = os.path.abspath(path)
        if kind == "music":
            self.music_path = path
            self.settings["music_path"] = path
            self._load_selected_media()
        else:
            old = self.background_path
            self.background_path = path
            self.settings["background_path"] = path
            self._load_selected_media()
            if not self.background_path and old:
                self.background_path = old
        settings_mod.save_settings(self.settings)
        self.network_status = ""
        self._build_ui_widgets()

    def _clear_music(self):
        self.music_path = ""
        self.settings["music_path"] = ""
        if self.audio_available:
            try:
                pygame.mixer.music.stop()
            except pygame.error:
                pass
        settings_mod.save_settings(self.settings)

    def _clear_background(self):
        self.background_path = ""
        self.settings["background_path"] = ""
        self.background_surface = None
        self._background_cache_size = None
        config.BACKGROUND_SURFACE = None
        settings_mod.save_settings(self.settings)

    def _draw_plain_background(self):
        self._update_background_surface()
        if config.BACKGROUND_SURFACE is not None:
            self.screen.blit(config.BACKGROUND_SURFACE, (0, 0))
        else:
            self.screen.fill(config.COLOR_BG)

    def handle_main_menu_click(self, pos):
        if self.main_menu_buttons["play"].handle_click(pos):
            self.state.phase = "select_game_mode"
        elif self.main_menu_buttons["network"].handle_click(pos):
            self.enter_local_network()
        elif self.main_menu_buttons["tutorial"].handle_click(pos):
            self.enter_tutorial()
        elif self.main_menu_buttons["settings"].handle_click(pos):
            self.state.phase = "settings"
        elif self.main_menu_buttons["exit"].handle_click(pos):
            self.running = False

    def draw_main_menu(self, mouse_pos):
        self._draw_plain_background()
        cx = config.SCREEN_WIDTH // 2
        R.draw_panel(self.screen, pygame.Rect(cx - 230, 64, 460, 530), radius=18)
        title = self.font_big.render("HP BATTLE CHESS", True, config.COLOR_TEXT)
        self.screen.blit(title, title.get_rect(center=(cx, 118)))
        sub = self.font_mid.render("ТАКТИЧЕСКОЕ ПОЛЕ БОЯ", True, config.COLOR_TEXT_DIM)
        self.screen.blit(sub, sub.get_rect(center=(cx, 158)))
        pygame.draw.line(self.screen, config.COLOR_PANEL_BORDER, (cx - 150, 186), (cx + 150, 186), 1)
        for key, btn in self.main_menu_buttons.items():
            btn.draw(self.screen, self.font, mouse_pos)

    # ------------------------------------------------------------------
    # Обучение ("Обучение" в главном меню)
    # ------------------------------------------------------------------
    def enter_tutorial(self):
        """Входим в обучение: откладываем текущее self.state (обычно
        main_menu) в сторону и подменяем его на маленький GameState урока.
        Ничего в обычной игре/LAN/AI/настройках не трогаем — они просто
        не видят self.state, пока он подменён, а после выхода получают
        его обратно в точности таким, каким он был."""
        self.tutorial_saved_state = self.state
        self.tutorial_saved_player_color = self.player_color
        self.tutorial_saved_viewer_color = config.VIEWER_COLOR
        self.tutorial_active = True
        self._load_tutorial_step(0)

    def exit_tutorial(self):
        self.tutorial_active = False
        self.tutorial_pending_advance = False
        self.tutorial_advance_timer = None
        if self.tutorial_saved_state is not None:
            self.state = self.tutorial_saved_state
        self.player_color = self.tutorial_saved_player_color or "white"
        self._set_viewer_color(self.tutorial_saved_viewer_color or "white")
        self.tutorial_saved_state = None
        self.tutorial_saved_player_color = None
        self.tutorial_saved_viewer_color = None
        self.deselect()

    def _load_tutorial_step(self, index):
        index = max(0, min(index, tutorial_mod.step_count() - 1))
        self.tutorial_step_index = index
        self.tutorial_pending_advance = False
        self.tutorial_advance_timer = None
        # Свежий GameState на каждый шаг — настоящий GameState.add_piece(),
        # никакой отдельной "учебной" копии правил.
        self.state = tutorial_mod.build_state_for_step(index, GameState)
        self.player_color = "white"
        self._set_viewer_color("white")
        # Свежий контроллер анимаций — чтобы из предыдущего шага не
        # тянулась хвостом недоигранная анимация на другую расстановку.
        self.animation = AnimationController(speed=self.settings.get("animation_speed", 1.0))
        self.deselect()
        step = tutorial_mod.STEPS[index]
        sel = step.get("select")
        if sel is not None:
            piece = self.state.get_piece_at(*sel)
            if piece is not None:
                self.select_piece(piece)

    @staticmethod
    def _tutorial_action_matches_goal(goal, applied_type):
        if goal is None:
            return False
        if goal == "king_action":
            return applied_type in ("move", "heal")
        return applied_type == goal

    TUTORIAL_ADVANCE_PAUSE = 1.1  # секунд паузы после результата действия — время увидеть, что произошло

    def _queue_tutorial_advance(self):
        """Действие урока выполнено — переходим к следующему шагу, но не
        раньше, чем доиграет настоящая анимация И пройдёт короткая пауза
        (см. _update_tutorial), чтобы результат действия было видно, а не
        резко сменялась вся доска на новую сцену."""
        self.tutorial_pending_advance = True
        self.tutorial_advance_timer = None

    def _update_tutorial(self, dt):
        if not self.tutorial_active or not self.tutorial_pending_advance:
            return
        if self.animation.is_blocking():
            return
        if self.tutorial_advance_timer is None:
            self.tutorial_advance_timer = self.TUTORIAL_ADVANCE_PAUSE
            return
        self.tutorial_advance_timer -= dt
        if self.tutorial_advance_timer > 0:
            return
        self.tutorial_pending_advance = False
        self.tutorial_advance_timer = None
        if self.tutorial_step_index >= tutorial_mod.step_count() - 1:
            self.exit_tutorial()
        else:
            self._load_tutorial_step(self.tutorial_step_index + 1)

    def handle_tutorial_click(self, pos):
        if self.tutorial_skip_button.handle_click(pos):
            self.exit_tutorial()
            return

        is_last = self.tutorial_step_index >= tutorial_mod.step_count() - 1
        if self.tutorial_next_button.handle_click(pos):
            if is_last:
                self.exit_tutorial()
            else:
                self._load_tutorial_step(self.tutorial_step_index + 1)
            return
        if self.tutorial_back_button.handle_click(pos):
            if self.tutorial_step_index > 0:
                self._load_tutorial_step(self.tutorial_step_index - 1)
            return

        if self.animation.is_blocking():
            return

        step = tutorial_mod.STEPS[self.tutorial_step_index]

        if step["goal"] == "surrender":
            if self.surrender_button.handle_click(pos):
                self.surrender()
                if self.state.phase == "game_over":
                    self._queue_tutorial_advance()
            return

        if step["goal"] == "queen_lock":
            for a in self.special_actions:
                if a["type"] == "queen_lock":
                    btn = self.queen_lock_buttons.get(a["mode"])
                    if btn is not None and btn.handle_click(pos):
                        ok = self.apply_action_with_animation(a)
                        self.deselect()
                        if ok:
                            self._queue_tutorial_advance()
                        return
            return

        sel = self.get_selected_piece()
        cell = R.screen_to_board(*pos)
        if cell is None:
            return

        if sel is not None and cell in self.action_map:
            action = self.action_map[cell]
            applied_type = action["type"]
            attacker = sel
            ok = self.apply_action_with_animation(action)
            self.deselect()
            if not ok:
                return
            is_complete_fn = step.get("is_complete")
            if is_complete_fn is not None:
                if is_complete_fn(self.state):
                    self._queue_tutorial_advance()
                elif attacker.alive():
                    # Мини-бой ещё не закончен — даём фигуре снова
                    # действовать, не дожидаясь полноценной смены хода.
                    attacker.actions_used = 0
                    self.select_piece(attacker)
                return
            if self._tutorial_action_matches_goal(step["goal"], applied_type):
                self._queue_tutorial_advance()
            return

        piece = self.state.get_piece_at(*cell)
        if piece is not None and piece.color == self.player_color:
            if actions_mod.get_legal_actions(self.state, piece):
                self.select_piece(piece)
            else:
                self.deselect()
        else:
            self.deselect()

    def _wrap_text_lines(self, text, font, max_width):
        words = text.split(" ")
        lines = []
        current = ""
        for word in words:
            candidate = (current + " " + word).strip()
            if font.size(candidate)[0] <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    def draw_tutorial_screen(self, mouse_pos):
        self._update_background_surface()
        R.draw_board(self.screen)

        fogged_cells = fog.visible_cells(self.state, self.player_color)
        if fogged_cells is not None:
            for c in range(config.BOARD_WIDTH):
                for r in range(config.BOARD_HEIGHT):
                    if (c, r) not in fogged_cells:
                        R.draw_fog_cell(self.screen, c, r)

        sel = self.get_selected_piece()
        if sel is not None:
            move_cells = [c for c, a in self.action_map.items() if a["type"] == "move"]
            attack_cells = [c for c, a in self.action_map.items() if a["type"] == "attack"]
            heal_cells = [c for c, a in self.action_map.items() if a["type"] == "heal"]
            swap_cells = [c for c, a in self.action_map.items() if a["type"] == "rook_swap"]
            R.draw_highlights(self.screen, move_cells, config.COLOR_HIGHLIGHT_MOVE)
            R.draw_highlights(self.screen, attack_cells, config.COLOR_HIGHLIGHT_ATTACK)
            R.draw_highlights(self.screen, heal_cells, config.COLOR_HIGHLIGHT_HEAL)
            R.draw_highlights(self.screen, swap_cells, config.COLOR_HIGHLIGHT_SWAP)
            R.draw_cell_border(self.screen, sel.col, sel.row, config.COLOR_HIGHLIGHT_SELECT)

        R.draw_all_pieces(self.screen, self.state, self.font_small, self.selected_piece_id,
                           animation=self.animation, visible_ids=self._visible_enemy_ids())
        R.draw_animation_effects(self.screen, self.animation, attacker_color_lookup=self._piece_color_lookup)

        self.draw_tutorial_panel(mouse_pos)

    _TUTORIAL_LEGEND_LABELS = {
        "move": "ход",
        "attack": "атака",
        "heal": "лечение",
        "rook_swap": "обмен местами",
        "mount": "оседлать союзника",
    }

    def draw_tutorial_panel(self, mouse_pos):
        panel_x = config.BOARD_MARGIN_X + config.BOARD_WIDTH * config.CELL_SIZE + 20
        self.panel_x = panel_x
        board_bottom = config.BOARD_MARGIN_Y + config.BOARD_HEIGHT * config.CELL_SIZE
        lay = Layout(config.BOARD_MARGIN_Y + 6, x=panel_x)
        step = tutorial_mod.STEPS[self.tutorial_step_index]
        total = tutorial_mod.step_count()

        R.draw_text(self.screen, f"ОБУЧЕНИЕ  ({self.tutorial_step_index + 1}/{total})",
                    (panel_x, lay.take(20, gap=6)), self.font_mid, config.COLOR_ACCENT)
        R.draw_text(self.screen, step["title"], (panel_x, lay.take(22, gap=8)), self.font_mid, config.COLOR_TEXT)

        # Легенда: что означает подсветка на доске прямо сейчас — отвечает
        # на "как понять, что фигура выбрана" и "что означает подсветка
        # (радиус хода/атаки)" без необходимости объяснять на словах рядом.
        # Считается ЗАРАНЕЕ, чтобы карточка ниже могла вместить и текст,
        # И легенду в ОДНУ рамку — раньше легенда рисовалась за пределами
        # фона карточки и выглядела "отдельно висящей".
        legend = []
        if self.get_selected_piece() is not None:
            legend.append(("выбранная фигура (рамка)", config.COLOR_HIGHLIGHT_SELECT))
        seen = []
        for a in self.action_map.values():
            if a["type"] not in seen:
                seen.append(a["type"])
        legend_colors = {
            "move": config.COLOR_HIGHLIGHT_MOVE,
            "attack": config.COLOR_HIGHLIGHT_ATTACK,
            "heal": config.COLOR_HIGHLIGHT_HEAL,
            "rook_swap": config.COLOR_HIGHLIGHT_SWAP,
            "mount": config.COLOR_HIGHLIGHT_SWAP,
        }
        for t in seen:
            label = self._TUTORIAL_LEGEND_LABELS.get(t)
            if label:
                legend.append((label, legend_colors[t]))

        card_top = lay.y
        card_lines = self._wrap_text_lines(step["text"], self.font_small, config.SIDE_PANEL_WIDTH - 56)
        card_h = 16 + len(card_lines) * 20 + (8 + len(legend) * 20 if legend else 0)
        R.draw_panel(self.screen, pygame.Rect(panel_x - 8, card_top - 8, config.SIDE_PANEL_WIDTH - 24, card_h), radius=10)
        for line in card_lines:
            R.draw_text(self.screen, line, (panel_x, lay.take(18, gap=2)), self.font_small, config.COLOR_TEXT_DIM)
        if legend:
            lay.take(6, gap=0)
            for label, color in legend:
                y = lay.take(18, gap=2)
                pygame.draw.rect(self.screen, color, pygame.Rect(panel_x, y + 3, 12, 12), border_radius=2)
                R.draw_text(self.screen, label, (panel_x + 18, y), self.font_small, config.COLOR_TEXT_DIM)

        if step["goal"] == "queen_lock":
            for a in self.special_actions:
                if a["type"] == "queen_lock":
                    btn = self.queen_lock_buttons.get(a["mode"])
                    if btn is not None:
                        lay.take(10, gap=0)
                        btn.rect.y = lay.take(34, gap=6)
                        btn.rect.x = panel_x
                        btn.draw(self.screen, self.font_small, mouse_pos)

        if self.tutorial_pending_advance:
            lay.take(8, gap=0)
            msg_y = lay.take(22, gap=8)
            bubble_cx = panel_x + 9
            bubble_cy = msg_y + 9
            pygame.draw.circle(self.screen, config.COLOR_HIGHLIGHT_HEAL, (bubble_cx, bubble_cy), 9)
            pygame.draw.lines(self.screen, (24, 26, 24), False, [
                (bubble_cx - 4, bubble_cy),
                (bubble_cx - 1, bubble_cy + 3),
                (bubble_cx + 5, bubble_cy - 4),
            ], 2)
            R.draw_text(self.screen, "Готово! Переходим дальше…",
                        (panel_x + 22, msg_y), self.font_small, config.COLOR_HIGHLIGHT_HEAL)

        # Нижний блок управления обучения — ФИКСИРОВАННЫЙ и независимый от
        # высоты текста карточки. Все кнопки одной ширины и находятся в
        # правой панели; "Пропустить обучение" не приклеивается к тексту.
        btn_w = config.SIDE_PANEL_WIDTH - 40

        # Отдельная нейтральная кнопка примерно по центру нижней части панели.
        skip_y = board_bottom - 212
        self.tutorial_skip_button.rect.update(panel_x, skip_y, btn_w, 30)
        self.tutorial_skip_button.draw(self.screen, self.font_small, mouse_pos)

        # СДАТЬСЯ — выше навигационных кнопок, как часть единого нижнего блока.
        self.surrender_button.rect.update(panel_x, board_bottom - 150, btn_w, 34)
        if step["goal"] == "surrender":
            self.surrender_button.draw(self.screen, self.font, mouse_pos)

        self.tutorial_back_button.rect.update(panel_x, board_bottom - 92, btn_w, 40)
        self.tutorial_next_button.rect.update(panel_x, board_bottom - 40, btn_w, 40)
        self.tutorial_back_button.draw(self.screen, self.font, mouse_pos)
        self.tutorial_next_button.draw(self.screen, self.font, mouse_pos, active=self.tutorial_pending_advance)

    def _short_name(self, name, limit=22):
        if len(name) <= limit:
            return name
        return name[:limit-1] + "…"

    def handle_settings_click(self, pos):
        if self.settings_fullscreen_button.handle_click(pos):
            self.toggle_fullscreen()
        elif self.settings_music_button.handle_click(pos):
            self._choose_file("music")
        elif self.settings_background_button.handle_click(pos):
            self._choose_file("background")
        elif self.settings_back_button.handle_click(pos):
            self.state.phase = "main_menu"

    def draw_settings_screen(self, mouse_pos):
        self._draw_plain_background()
        cx = config.SCREEN_WIDTH // 2
        panel_w = min(540, config.SCREEN_WIDTH - 36)
        panel_h = min(540, config.SCREEN_HEIGHT - 48)
        panel_x = cx - panel_w // 2
        panel_y = 24
        R.draw_panel(self.screen, pygame.Rect(panel_x, panel_y, panel_w, panel_h), radius=18)

        title = self.font_big.render("НАСТРОЙКИ", True, config.COLOR_TEXT)
        self.screen.blit(title, title.get_rect(center=(cx, panel_y + 50)))

        bx = cx - min(180, (panel_w - 40) // 2)
        bw = min(360, panel_w - 40)
        self.settings_fullscreen_button.text = f"ПОЛНЫЙ ЭКРАН  {'ВКЛ' if self.fullscreen else 'ВЫКЛ'}"
        # На Android нет desktop file picker (см. _choose_file) — кнопки
        # остаются на месте (не переделываем layout), но подписи честно
        # говорят, что действие недоступно на этой платформе.
        if platform_utils.is_android():
            self.settings_music_button.text = "МУЗЫКА: НЕДОСТУПНО НА ANDROID"
            self.settings_background_button.text = "ФОН: НЕДОСТУПНО НА ANDROID"
        else:
            self.settings_music_button.text = "МУЗЫКА: ВЫБРАТЬ"
            self.settings_background_button.text = "ФОН: ВЫБРАТЬ"
        self.settings_fullscreen_button.rect = pygame.Rect(bx, panel_y + 92, bw, 46)
        self.settings_music_button.rect = pygame.Rect(bx, panel_y + 148, bw, 46)
        self.settings_background_button.rect = pygame.Rect(bx, panel_y + 204, bw, 46)

        self.settings_fullscreen_button.draw(self.screen, self.font_small, mouse_pos, active=self.fullscreen)
        self.settings_music_button.draw(self.screen, self.font_small, mouse_pos, active=bool(self.music_path))
        self.settings_background_button.draw(self.screen, self.font_small, mouse_pos, active=bool(self.background_path))

        music_label = ("Музыка: " + self._short_name(os.path.basename(self.music_path), 30)) if self.music_path else "Музыка: файл не выбран"
        bg_label = ("Фон: " + self._short_name(os.path.basename(self.background_path), 30)) if self.background_path else "Фон: файл не выбран"
        R.draw_text(self.screen, music_label, (bx, panel_y + 262), self.font_small, config.COLOR_TEXT_DIM)
        R.draw_text(self.screen, bg_label, (bx, panel_y + 286), self.font_small, config.COLOR_TEXT_DIM)
        R.draw_text(self.screen, "Музыка повторяется автоматически. Формат зависит от SDL_mixer.",
                    (bx, panel_y + 322), self.font_small, config.COLOR_TEXT_DIM)
        R.draw_text(self.screen, "Фон вписывается в окно с сохранением пропорций.",
                    (bx, panel_y + 344), self.font_small, config.COLOR_TEXT_DIM)
        R.draw_text(self.screen, "Повторное нажатие на кнопку заменяет выбранный файл.",
                    (bx, panel_y + 366), self.font_small, config.COLOR_TEXT_DIM)
        back_y = panel_y + panel_h - 60
        if self.network_status:
            is_error = "Не удалось" in self.network_status or "недоступен" in self.network_status
            status_color = config.COLOR_DAMAGE_NUMBER if is_error else config.COLOR_TEXT_DIM
            status_y = min(panel_y + 396, back_y - 40)
            text = self.network_status
            # Простой перенос на вторую строку для длинных сообщений
            # (например подсказки про недостающий python3-tk) — font.render
            # не умеет '\n', а панель настроек не резиновая по ширине.
            if len(text) > 46:
                cut = text.rfind(" ", 0, 46)
                cut = cut if cut > 0 else 46
                lines = [text[:cut].strip(), text[cut:].strip()]
            else:
                lines = [text]
            for i, line in enumerate(lines):
                R.draw_text(self.screen, line, (bx, status_y + i * 20), self.font_small, status_color)

        self.settings_back_button.rect = pygame.Rect(bx, back_y, bw, 46)
        self.settings_back_button.draw(self.screen, self.font_small, mouse_pos)

    # ------------------------------------------------------------------
    # Экран выбора игрового режима (ПЕРВЫЙ экран)
    # ------------------------------------------------------------------
    def handle_select_game_mode_click(self, pos):
        for mode, btn in self.game_mode_buttons.items():
            if btn.handle_click(pos):
                self.selected_game_mode = mode
                return
        if self.game_mode_next_button.handle_click(pos):
            self.state.phase = "select_board_size"
            return

    def draw_select_game_mode_screen(self, mouse_pos):
        R.draw_board(self.screen)
        menu_x = self.panel_x - 240
        overlay = pygame.Surface((config.SCREEN_WIDTH, config.SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((10, 10, 14, 210))
        self.screen.blit(overlay, (0, 0))
        R.draw_panel(self.screen, pygame.Rect(menu_x - 22, 62, 524, 500), radius=16)
        R.draw_text(self.screen, "ВЫБОР РЕЖИМА ИГРЫ", (menu_x, 90), self.font_big, config.COLOR_ACCENT)
        R.draw_text(self.screen, "Выберите цель партии:", (menu_x, 124), self.font_small, config.COLOR_TEXT_DIM)
        for mode, btn in self.game_mode_buttons.items():
            btn.draw(self.screen, self.font, mouse_pos, active=(mode == self.selected_game_mode))
            R.draw_text(self.screen, config.GAME_MODE_DESCRIPTIONS[mode],
                        (btn.rect.x + 4, btn.rect.y + btn.rect.h + 5), self.font_small, config.COLOR_TEXT_DIM)
        self.game_mode_next_button.draw(self.screen, self.font, mouse_pos)

    # ------------------------------------------------------------------
    # Экран выбора размера доски (ВТОРОЙ экран — после режима)
    # ------------------------------------------------------------------
    def handle_select_board_size_click(self, pos):
        for size, btn in self.board_size_buttons.items():
            if btn.handle_click(pos):
                self.selected_board_size = size
                return
        if self.board_size_next_button.handle_click(pos):
            config.configure_board_size(self.selected_board_size)
            # ВАЖНО: configure_board_size() пересчитывает config.SCREEN_WIDTH/
            # HEIGHT под новую карту (get_windowed_size() внутри неё), но
            # РЕАЛЬНОЕ окно pygame (self.screen) само по себе не меняется —
            # его меняет только pygame.display.set_mode(), а его вызывает
            # только _apply_screen_size(). Если не вызвать её здесь, вся
            # раскладка (доска+панель, кнопки) начинает считаться от нового
            # виртуального SCREEN_WIDTH/HEIGHT, а реальная поверхность окна
            # остаётся старого размера — отсюда и "уезжающая" карта при
            # выборе размера, отличного от того, с которым стартовало окно
            # (характерно проявлялось на 12x8, если старт был на 10x8).
            self._apply_screen_size(force_recreate=False)
            self._build_ui_widgets()
            self._rebuild_default_setup_pieces()
            self.state.phase = "setup_white"
            return

    def draw_select_board_size_screen(self, mouse_pos):
        R.draw_board(self.screen)
        menu_x = self.panel_x - 240
        overlay = pygame.Surface((config.SCREEN_WIDTH, config.SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((10, 10, 14, 210))
        self.screen.blit(overlay, (0, 0))
        R.draw_panel(self.screen, pygame.Rect(menu_x - 22, 62, 524, 360), radius=16)
        R.draw_text(self.screen, "РАЗМЕР КАРТЫ", (menu_x, 90), self.font_big, config.COLOR_ACCENT)
        R.draw_text(self.screen, "Режим: " + config.GAME_MODE_NAMES[self.selected_game_mode],
                    (menu_x, 124), self.font_small, config.COLOR_TEXT_DIM)
        R.draw_text(self.screen, "Маленькая армия — большая территория:",
                    (menu_x, 146), self.font_small, config.COLOR_TEXT_DIM)
        for size, btn in self.board_size_buttons.items():
            btn.draw(self.screen, self.font, mouse_pos, active=(size == self.selected_board_size))
        self.board_size_next_button.draw(self.screen, self.font, mouse_pos)

    # ------------------------------------------------------------------
    # Расстановка (setup)
    # ------------------------------------------------------------------
    def setup_piece_rect(self, sp):
        x, y = R.board_to_screen(sp["col"], sp["row"])
        return pygame.Rect(x, y, config.CELL_SIZE, config.CELL_SIZE)

    def handle_setup_mousedown(self, pos):
        if self.ready_button.handle_click(pos):
            self.confirm_setup()
            return
        if self.random_button.handle_click(pos):
            self.randomize_white_setup()
            return
        for i, sp in enumerate(self.setup_pieces):
            if self.setup_piece_rect(sp).collidepoint(pos):
                self.dragging_index = i
                px, py = R.board_to_screen(sp["col"], sp["row"])
                self.drag_offset = (pos[0] - px, pos[1] - py)
                return

    def handle_setup_mouseup(self, pos):
        if self.dragging_index is None:
            return
        cell = R.screen_to_board(pos[0], pos[1])
        idx = self.dragging_index
        self.dragging_index = None
        if cell is None:
            return
        col, row = cell
        if not rules.in_own_half(self.setup_player_color, col, row):
            return
        for j, sp in enumerate(self.setup_pieces):
            if j != idx and sp["col"] == col and sp["row"] == row:
                return
        self.setup_pieces[idx]["col"] = col
        self.setup_pieces[idx]["row"] = row

    def randomize_white_setup(self):
        formation = default_white_layout() if self.setup_player_color == "white" else ai_black_layout()
        for sp, (c, r, t) in zip(self.setup_pieces, formation):
            sp["col"], sp["row"] = c, r

    def confirm_setup(self):
        if self.network_role == "client" and self.setup_player_color == "black":
            if self.network_client and self.network_client.connected:
                self.network_client.command("setup_ready", payload={"pieces": self.setup_pieces})
            self.network_screen = "host_wait"
            self.state.phase = "local_network"
            self.network_status = "Расстановка отправлена. Ждём хоста…"
            return

        self.state = GameState()
        self.state.game_mode = self.selected_game_mode
        self.state.board_size = (config.BOARD_WIDTH, config.BOARD_HEIGHT)
        self.state.fog_enabled = (self.selected_game_mode == "fog")
        for sp in self.setup_pieces:
            self.state.add_piece(sp["type"], "white", sp["col"], sp["row"])
        for c, r, t in ai_black_layout():
            self.state.add_piece(t, "black", c, r)
        self.state.phase = "select_opponent"
        self._set_viewer_color(self.player_color)
        self.selected_piece_id = None
        self.action_map = {}

        # In Host-LAN mode, confirming deployment creates the room now.
        # Do not jump directly into the board; the host must wait for the
        # second player before the match starts.
        if self.pending_network_host:
            self.start_host_game()

    def draw_setup_screen(self, mouse_pos):
        R.draw_board(self.screen)
        R.draw_zone_tint(self.screen, config.WHITE_HALF_ROWS if self.setup_player_color == "white" else config.BLACK_HALF_ROWS, config.COLOR_PLAYER_UI, alpha=30)

        for i, sp in enumerate(self.setup_pieces):
            if i == self.dragging_index:
                continue
            fake = self._fake_piece(sp["type"], sp["col"], sp["row"])
            R.draw_piece(self.screen, fake, self.font_small)

        if self.dragging_index is not None:
            sp = self.setup_pieces[self.dragging_index]
            mx, my = mouse_pos
            x = mx - self.drag_offset[0]
            y = my - self.drag_offset[1]
            col, row = coords.screen_to_board_unclamped(x, y)
            fake = self._fake_piece(sp["type"], col, row)
            R.draw_piece(self.screen, fake, self.font_small)

        panel_x = self.panel_x
        # Раньше здесь был захардкожен y=20 — панель текста всегда
        # начиналась у самого верха окна, независимо от того, где
        # реально начинается доска (BOARD_MARGIN_Y пересчитывается под
        # размер карты/экрана). На игровом экране панель уже правильно
        # привязана к config.BOARD_MARGIN_Y — делаем так же здесь, чтобы
        # текст не "улетал" вверх и был на одном уровне с доской.
        lay = Layout(config.BOARD_MARGIN_Y + 6, x=panel_x)
        R.draw_text(self.screen, "Расстановка армии", (panel_x, lay.take(24, gap=4)), self.font_mid, config.COLOR_ACCENT)
        R.draw_text(self.screen, "(свои фигуры внизу доски)", (panel_x, lay.take(16, gap=14)), self.font_small, config.COLOR_TEXT_DIM)
        for line in ("Перетащите свои фигуры", "внутри своей стартовой зоны", "(подсвечена синим)."):
            R.draw_text(self.screen, line, (panel_x, lay.take(16, gap=2)), self.font_small, config.COLOR_TEXT_DIM)
        lay.take(6, gap=0)
        R.draw_text(self.screen, f"Карта: {config.BOARD_WIDTH}x{config.BOARD_HEIGHT}",
                    (panel_x, lay.take(16, gap=2)), self.font_small, config.COLOR_TEXT_DIM)
        R.draw_text(self.screen, f"Режим: {config.GAME_MODE_NAMES[self.selected_game_mode]}",
                    (panel_x, lay.take(16, gap=2)), self.font_small, config.COLOR_TEXT_DIM)

        self.ready_button.draw(self.screen, self.font, mouse_pos)
        self.random_button.draw(self.screen, self.font_small, mouse_pos)

    def _fake_piece(self, ptype, col, row):
        fake = type("F", (), {})()
        fake.type = ptype
        fake.color = getattr(self, "setup_player_color", "white")
        fake.col = col
        fake.row = row
        fake.hp = 1
        fake.max_hp = 1
        fake.mounted_knight_id = None
        fake.is_mounted_knight = False
        fake.actions_used = 0
        fake.max_actions = lambda: 1
        fake.actions_available = lambda: 1
        return fake

    # ------------------------------------------------------------------
    # Экран выбора противника
    # ------------------------------------------------------------------
    def handle_select_opponent_click(self, pos):
        for key, btn in self.opponent_type_buttons.items():
            if btn.handle_click(pos):
                self.selected_opponent_type = key
                return
        for key, btn in self.difficulty_buttons.items():
            if btn.handle_click(pos):
                self.selected_difficulty = key
                return
        if self.opponent_confirm_button.handle_click(pos):
            if self.selected_opponent_type == "network":
                self.enter_local_network()
            else:
                self.confirm_opponent()
            return

    def confirm_opponent(self):
        self._set_viewer_color("white")
        self.settings["ai_type"] = self.selected_opponent_type
        self.settings["difficulty"] = self.selected_difficulty
        self.settings["board_size"] = (config.BOARD_WIDTH, config.BOARD_HEIGHT)
        self.settings["game_mode"] = self.selected_game_mode
        self.settings["fullscreen"] = self.fullscreen
        settings_mod.save_settings(self.settings)

        bias = None
        if self.settings.get("memory_enabled", True):
            bias = ai_memory.opening_bias(self.ai_memory, config.AI_COLOR)

        self.ai_player = ai_interface.create_ai_player(
            self.selected_opponent_type, difficulty=self.selected_difficulty, opening_bias_map=bias)

        turn_system.start_turn(self.state, "white")
        self.memory_saved = False
        self.ai_first_action_type = None
        self.ai_used_mount = False

    def draw_select_opponent_screen(self, mouse_pos):
        R.draw_board(self.screen)
        R.draw_all_pieces(self.screen, self.state, self.font_small)

        menu_x = self.panel_x - 240
        overlay = pygame.Surface((config.SCREEN_WIDTH, config.SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((10, 10, 14, 210))
        self.screen.blit(overlay, (0, 0))
        R.draw_panel(self.screen, pygame.Rect(menu_x - 22, 62, 524, 500), radius=16)

        R.draw_text(self.screen, "ВЫБЕРИТЕ ПРОТИВНИКА", (menu_x, 90), self.font_big, config.COLOR_ACCENT)
        R.draw_text(self.screen, f"Карта: {config.BOARD_WIDTH}x{config.BOARD_HEIGHT}   "
                                  f"Режим: {config.GAME_MODE_NAMES[self.selected_game_mode]}",
                    (menu_x, 124), self.font_small, config.COLOR_TEXT_DIM)

        for key, btn in self.opponent_type_buttons.items():
            btn.draw(self.screen, self.font, mouse_pos, active=(key == self.selected_opponent_type))
        algo_x = self.opponent_type_buttons["algorithm"].rect.x
        local_x = self.opponent_type_buttons["local"].rect.x
        R.draw_text(self.screen, "Minimax + Alpha-Beta", (algo_x, 226), self.font_small, config.COLOR_TEXT_DIM)
        R.draw_text(self.screen, "+ итеративное углубление", (algo_x, 244), self.font_small, config.COLOR_TEXT_DIM)
        R.draw_text(self.screen, "Локальная LLM (Ollama)", (local_x, 226), self.font_small, config.COLOR_TEXT_DIM)
        R.draw_text(self.screen, "Полностью офлайн", (local_x, 244), self.font_small, config.COLOR_TEXT_DIM)

        if self.selected_opponent_type == "algorithm":
            R.draw_text(self.screen, "Сложность (глубина/время поиска):", (menu_x, 270), self.font_small)
            for key, btn in self.difficulty_buttons.items():
                btn.draw(self.screen, self.font_small, mouse_pos, active=(key == self.selected_difficulty))
        else:
            avail = ai_interface.check_ollama_available(timeout=0.4)
            status = "Ollama обнаружена \u2713" if avail else "Ollama не найдена — будет использован Algorithm AI"
            color = (90, 200, 120) if avail else config.COLOR_TEXT_DIM
            R.draw_text(self.screen, status, (menu_x, 270), self.font_small, color)

        mem = self.ai_memory
        if self.selected_opponent_type != "network":
            stats = (f"Память ИИ: партий {mem.get('games_played', 0)}, "
                     f"побед ИИ {mem.get('wins', 0)}, поражений {mem.get('losses', 0)}")
            R.draw_text(self.screen, stats, (menu_x, 350), self.font_small, config.COLOR_TEXT_DIM)

        self.opponent_confirm_button.draw(self.screen, self.font, mouse_pos)

    # ------------------------------------------------------------------
    # LAN multiplayer
    # ------------------------------------------------------------------
    def enter_local_network(self):
        self.pending_network_host = False
        self.network_role = None
        self.network_screen = "menu"
        # True while Host flow is waiting for game mode/board/deployment.
        self.pending_network_host = False
        self.network_status = ""
        self.network_games = []
        # Direct LAN connection fields. IP starts empty (no fake example);
        # port defaults to the protocol port but can be edited independently.
        self.network_ip_text = ""
        self.network_port_text = str(network.DEFAULT_TCP_PORT)
        self.network_ip_active = False
        self.network_port_active = False
        self.pending_network_host = False
        self.state.phase = "local_network"

    def _network_game_name(self):
        # Имя сессии показывается в discovery и не влияет на протокол.
        return "Alexander's Game"

    def _build_network_initial_state(self):
        # White = host, Black = client. Parameters are copied before connect
        # and never changed for the lifetime of the room.
        self.state.game_mode = self.selected_game_mode
        self.state.board_size = (config.BOARD_WIDTH, config.BOARD_HEIGHT)
        self.state.fog_enabled = (self.selected_game_mode == "fog")
        self.state.phase = "setup_white"
        self.state.pieces = {}
        for sp in self.setup_pieces:
            self.state.add_piece(sp["type"], "white", sp["col"], sp["row"])
        # Black army is provided by the connecting player after deployment.
        self.state.phase = "waiting_for_player"
        self.state.last_event = "Ожидание игрока по локальной сети"

    def start_host_game(self):
        self.close_network()
        self._build_network_initial_state()
        self.network_role = "host"
        self.player_color = "white"
        self._set_viewer_color("white")
        try:
            host_port = int(self.network_port_text.strip() or network.DEFAULT_TCP_PORT)
            if not (1 <= host_port <= 65535):
                raise ValueError
        except ValueError:
            self.network_status = "Порт хоста должен быть числом от 1 до 65535"
            return
        self.network_port_text = str(host_port)
        self.network_host = network.HostSession(self.state, self._network_game_name(), tcp_port=host_port)
        try:
            self.network_host.start()
        except OSError as exc:
            self.network_host = None
            self.network_role = None
            self.network_status = f"Не удалось открыть LAN-порт: {exc}"
            return
        self.network_screen = "host_wait"
        self.network_status = f"Сессия создана. IP: {network.get_local_ipv4()}  порт: {self.network_host.tcp_port}"

    def _scan_worker(self):
        try:
            self.network_scan_result = network.DiscoveryClient.find_games(timeout=1.2)
        except Exception:
            self.network_scan_result = []
        finally:
            self.network_scan_busy = False

    def scan_network_games(self):
        if self.network_scan_busy:
            return
        self.network_scan_busy = True
        self.network_games = []
        self.network_scan_result = []
        self.network_status = "Поиск игр в локальной сети..."
        self.network_scan_thread = threading.Thread(target=self._scan_worker, daemon=True)
        self.network_scan_thread.start()
        self.network_screen = "browser"

    def join_network_game(self, game):
        self.close_network()
        client = network.ClientSession()
        self.network_client = client
        self.network_role = "client"
        self.player_color = "black"
        self._set_viewer_color("black")
        try:
            client.connect(game)
        except OSError as exc:
            self.network_role = None
            self.network_client = None
            self.network_status = f"Не удалось подключиться к {game.get('host', '?')}:{game.get('port', '?')}: {exc}"
            self.network_screen = "browser"
            return
        self.network_screen = "host_wait"
        self.setup_player_color = "black"
        self.network_status = "Подключено. Готовим расстановку чёрных…"

    def join_network_by_ip(self):
        host = self.network_ip_text.strip()
        if not host:
            self.network_status = "Введите IP хоста или выберите комнату через поиск"
            return
        try:
            port = int(self.network_port_text.strip() or network.DEFAULT_TCP_PORT)
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            self.network_status = "Порт должен быть числом от 1 до 65535"
            return
        self.join_network_game({"host": host, "port": port, "name": "Direct IP"})

    def close_network(self):
        if self.network_host is not None:
            self.network_host.close()
        if self.network_client is not None:
            self.network_client.close()
        self.network_host = None
        self.network_client = None
        self.network_role = None
        self.network_seen_actions.clear()
        self.network_anim_queue = []
        self.pending_network_action = None

    def _start_network_play(self, snapshot_data):
        # Client receives authoritative player view. Host already owns the
        # full state and only needs to enter normal gameplay.
        if self.network_role == "client":
            self.state = network.state_from_dict(snapshot_data["state"])
        else:
            turn_system.start_turn(self.state, "white")
        self.selected_piece_id = None
        self.action_map = {}
        self.special_actions = []
        self.dismount_options = []
        self.network_seen_actions.clear()
        self.network_reconnect_attempts = 0
        self.network_reconnect_timer = 0.0
        self.temporarily_revealed_ids = set()
        if self.state.phase in ("waiting_for_player", "setup_white"):
            turn_system.start_turn(self.state, "white")
        self.network_screen = "game"
        self.pending_network_host = False
        self.network_status = "Игра началась"

    def _pump_network_animations(self):
        """Действия соперника по сети иногда приходят пачкой за один
        poll() — например, если соперник сделал несколько действий подряд
        быстрее, чем успел отрисоваться кадр. AnimationController хранит
        только ОДНУ 'текущую' блокирующую анимацию: если запустить
        следующую, не дождавшись окончания предыдущей, она молча
        перезатирается — фигура просто "телепортируется" в финальную
        позицию без видимого движения/удара. Именно это и выглядело как
        "анимации соперника не видно". У хода ИИ такой проблемы не было —
        он уже проигрывает свои действия по одному (ai_step_pause_timer +
        is_blocking()). Здесь — то же самое, но для сетевой очереди."""
        if self.animation.is_blocking():
            return
        if not self.network_anim_queue:
            return
        action, before_state, after_state = self.network_anim_queue.pop(0)
        self._animate_network_action(action, after_state, before_state=before_state)

    def _animate_network_action(self, action, after_state, before_state=None):
        if not action:
            return
        before = before_state if before_state is not None else self.state
        pid = action.get("piece_id")
        piece = before.pieces.get(pid)
        if piece is None:
            return
        old_cell = (piece.col, piece.row)
        kind = action.get("type")
        if kind == "move":
            self.animation.start_move(piece.id, old_cell, tuple(action.get("target", old_cell)))
        elif kind == "dismount":
            self.animation.start_move(piece.id, old_cell, tuple(action.get("target", old_cell)))
        elif kind == "rook_swap":
            target = before.pieces.get(action.get("target_id"))
            if target:
                self.animation.start_move(piece.id, old_cell, (target.col, target.row))
        elif kind == "mount":
            target = before.pieces.get(action.get("target_id"))
            if target:
                self.animation.start_mount(piece.id, old_cell, (target.col, target.row))
        elif kind == "attack":
            target = before.pieces.get(action.get("target_id"))
            if target:
                target_cell = (target.col, target.row)
                self._spawn_damage_visuals(before, action)
                self.animation.start_attack(piece.id, old_cell, target_cell, piece.type, action.get("mode"))
                self.animation.spawn_impact(target_cell)
                after_target = after_state.pieces.get(target.id)
                if after_target is None or not after_target.alive():
                    self.animation.spawn_death(target_cell, target.color, target.type)
        elif kind == "heal":
            target = before.pieces.get(action.get("target_id"))
            if target:
                self.animation.spawn_impact((target.col, target.row))

    def _network_snapshot_received(self, msg):
        # Config is authoritative and immutable after welcome.
        cfg = msg.get("session_config") or {}
        if cfg:
            raw_board = cfg.get("board_size", list(self.state.board_size) if isinstance(self.state.board_size, tuple) else self.state.board_size)
            board = tuple(raw_board) if isinstance(raw_board, (list, tuple)) else int(raw_board)
            mode = cfg.get("mode", self.state.game_mode)
            current_board = (config.BOARD_WIDTH, config.BOARD_HEIGHT)
            if board != current_board or mode != self.state.game_mode:
                config.configure_board_size(board)
                self.selected_board_size = board
                self.selected_game_mode = mode
                # См. комментарий в handle_select_board_size_click — окно
                # обязательно нужно пересоздать/пересчитать под новый размер
                # карты, иначе раскладка разъезжается с реальным окном (у
                # клиента карта хоста может отличаться от той, что была
                # выбрана локально при входе в LAN-меню).
                self._apply_screen_size(force_recreate=False)
                self._build_ui_widgets()
        old_state = self.state
        next_state = network.state_from_dict(msg["state"])
        if self.network_role == "client":
            if self.pending_network_action:
                self.network_anim_queue.append((self.pending_network_action, old_state, next_state))
            self.pending_network_action = None
            self.state = next_state
            self.player_color = "black"
        else:
            self.state = next_state
            self.player_color = "white"
        self.selected_piece_id = None
        self.action_map = {}
        self.special_actions = []
        self.dismount_options = []
        # A client view may legitimately have a different piece set under Fog
        # of War; compare the host-supplied view hash for sync diagnostics.
        supplied_hash = msg.get("state_hash", "")
        viewer = self.player_color if self.network_role == "client" else "white"
        local_hash = network.state_hash(self.state, viewer_color=viewer)
        if supplied_hash and supplied_hash != local_hash:
            self.network_status = "ОШИБКА СИНХРОНИЗАЦИИ: состояние отличается"
            if self.network_role == "client" and self.network_client:
                self.network_client.command("resync_request")
        else:
            self.network_status = "Синхронизировано"

        if self.state.phase == "game_over":
            return
        if self.network_role == "client" and self.state.phase == "waiting_for_player" and self.setup_player_color == "black":
            # Initial snapshot only contains the host army. Ask the client for deployment.
            self.setup_pieces = [{"type": t, "col": c, "row": r} for c, r, t in ai_black_layout()]
            self.network_screen = "setup_black"
            self.state.phase = "local_network"
            self.network_status = "Расставьте свои чёрные фигуры"
            return
        if self.state.phase == "waiting_for_player":
            self.state.phase = "king_stage"

    def _network_apply_local_host_action(self, action):
        # Host local white action: apply to authoritative state and broadcast.
        before = self.state.clone_light()
        ok = self.apply_action_with_animation(action)
        if not ok:
            return False
        result = self.state.check_victory()
        if result:
            self.state.phase = "game_over"
            self.state.winner = result
        if self.network_host:
            self.network_host.notify_local_state_changed(action, before_state=before)
        return True

    def _network_request_action(self, action):
        if self.network_role == "client" and self.network_client:
            if not self.network_client.connected:
                return False
            return self.network_client.request_action(action) is not None
        if self.network_role == "host":
            return self._network_apply_local_host_action(action)
        return False

    def _network_end_king_stage(self):
        if self.network_role == "host":
            if self.state.phase == "king_stage" and self.state.turn_color == "white":
                turn_system.end_king_stage(self.state)
                self.network_host.notify_local_state_changed()
        elif self.network_client:
            self.network_client.command("end_king_stage")

    def _network_end_turn(self):
        if self.network_role == "host":
            if self.state.phase == "lobby_stage" and self.state.turn_color == "white":
                turn_system.end_turn(self.state)
                if self.network_host:
                    self.network_host.action_id = 0
                    self.network_host.notify_local_state_changed()
        elif self.network_client:
            self.network_client.command("end_turn")

    def _drain_host_visual_actions(self):
        """Забирает у HostSession накопленные визуальные действия чёрных
        (соперника) и кладёт их в network_anim_queue, чтобы они
        проигрались через _pump_network_animations (см. run())."""
        if not self.network_host:
            return
        visual_batch = self.network_host.pop_visual_actions()
        if visual_batch:
            after_snapshot = self.state.clone_light()
            for remote_action, before_state in visual_batch:
                self.network_anim_queue.append((remote_action, before_state, after_snapshot))

    def _poll_network(self):
        if self.network_role == "host" and self.network_host:
            self.network_host.poll()
            # ВАЖНО: HostSession хранит СВОЙ self.state и в некоторых местах
            # (например при реванше, _maybe_start_rematch) полностью
            # ПЕРЕСОЗДАЁТ этот объект (self.state = state_from_dict(...))
            # вместо изменения существующего. Раньше App.state был просто
            # тем же объектом, что и network_host.state, НА МОМЕНТ создания
            # HostSession — и после такой пересборки два атрибута расходились:
            # network_host.state указывал на новый (сброшенный) стейт, а
            # self.state в App продолжал ссылаться на старый (например,
            # застрявший в game_over). Клиент это не задевало — он получает
            # свежий snapshot по сети и пересобирает self.state явно. А вот
            # у ХОСТА своя же кнопка "Играть снова" визуально не работала:
            # сервер честно стартовал реванш внутри себя, а экран хоста
            # продолжал показывать старую (уже выигранную/проигранную)
            # партию. Поэтому здесь всегда подтягиваем актуальный объект.
            self.state = self.network_host.state
            self._drain_host_visual_actions()
            if self.network_host.connected and self.network_screen == "host_wait":
                self._start_network_play({"state": network.state_to_dict(self.state, "white"), "state_hash": network.state_hash(self.state, "white")})
            return
        if self.network_role != "client" or not self.network_client:
            return
        for msg in self.network_client.poll():
            typ = msg.get("type")
            if typ == "welcome":
                self.network_status = "Подключено: хост = белые / вы = чёрные"
            elif typ == "snapshot":
                # Client hash is computed over exactly the received view.
                self._network_snapshot_received(msg)
                if self.state.phase == "waiting_for_player":
                    self.state.phase = "king_stage"
                # The snapshot is the definitive proof that the TCP session
                # is ready for play.  Older flow only updated the state here,
                # leaving the UI stuck on the connection/lobby screen.
                if self.network_role == "client" and self.network_screen == "host_wait":
                    self.network_screen = "game"
                    self.pending_network_host = False
                    self.network_status = "Игра началась. Вы — чёрные"
            elif typ == "action_applied":
                key = (int(msg.get("turn_id", 0)), int(msg.get("action_id", 0)))
                if key in self.network_seen_actions:
                    continue
                self.network_seen_actions.add(key)
                self.pending_network_action = msg.get("action")
                request_id = msg.get("request_id")
                if request_id:
                    self.network_client.pending_request.pop(request_id, None)
            elif typ == "action_rejected":
                self.network_status = "Действие отклонено хостом: " + str(msg.get("reason", "неизвестно"))
            elif typ == "error":
                self.network_status = msg.get("message", "Ошибка сетевого протокола")

        if not self.network_client.connected:
            self.network_reconnect_timer += self.clock.get_time() / 1000.0
            if self.network_reconnect_timer >= 1.5 and self.network_reconnect_attempts < 6:
                self.network_reconnect_timer = 0.0
                self.network_reconnect_attempts += 1
                if self.network_client.reconnect():
                    self.network_status = f"Переподключение... {self.network_reconnect_attempts}/6"

    def _network_tick(self, dt):
        if self.network_role == "host" and self.network_host:
            self._update_game_timer(dt)
            before_phase = self.state.phase
            before_king = float(self.state.king_stage_timer)
            self.network_host.tick(dt)
            self.state = self.network_host.state  # см. комментарий в _poll_network
            self._drain_host_visual_actions()
            if before_phase == "king_stage" and self.state.phase != "king_stage" and self.timer_beeped_turn != (self.state.turn_number, "king"):
                self._play_timer_beep(); self.timer_beeped_turn = (self.state.turn_number, "king")
            if self.state.phase == "game_over" and before_phase != "game_over" and self.network_host:
                self.network_host.notify_local_state_changed()
            peer_alive = bool(
                self.network_host.peer is not None
                and not self.network_host.peer.closed.is_set()
            )
            # A live TCP peer is the authoritative signal that the second
            # player joined.  Do not leave the UI on the lobby merely because
            # the connection flag has not yet caught up with the reader thread.
            if peer_alive and self.network_screen == "host_wait" and self.state.phase in ("king_stage", "lobby_stage"):
                self._start_network_play({
                    "state": network.state_to_dict(self.state, "white"),
                    "state_hash": network.state_hash(self.state, "white"),
                })
            elif not self.network_host.connected and self.state.phase not in ("waiting_for_player", "game_over"):
                self.network_status = "Соперник отключился — переподключение..."
        elif self.network_role == "client":
            if self.state.phase in ("king_stage", "lobby_stage"):
                before_king = float(self.state.king_stage_timer)
                if self.state.phase == "king_stage":
                    self.state.king_stage_timer = max(0.0, self.state.king_stage_timer - dt)
                    if before_king > 0 and self.state.king_stage_timer <= 0 and self.timer_beeped_turn != (self.state.turn_number, "king"):
                        self._play_timer_beep(); self.timer_beeped_turn = (self.state.turn_number, "king")
                else:
                    self.state.main_timer = max(0.0, self.state.main_timer - dt)
            self._poll_network()

    def request_network_rematch(self):
        if self.network_role == "host" and self.network_host:
            self.network_host.request_local_rematch()
            self.network_status = "Запрошен реванш — ждём соперника"
        elif self.network_role == "client" and self.network_client:
            self.network_client.command("rematch_ready")
            self.network_status = "Запрошен реванш — ждём соперника"

    def exit_network_room(self):
        self.close_network()
        self.pending_network_host = False
        self.state.phase = "main_menu"
        self.network_screen = "menu"
        self.network_status = ""

    def handle_local_network_click(self, pos):
        menu_x = self.panel_x - 240
        if self.network_screen == "menu":
            if self.lan_host_button.handle_click(pos):
                # First configure the room exactly like a normal game:
                # mode -> board size -> white deployment -> create room.
                self.pending_network_host = True
                self.state.phase = "select_game_mode"
                return
            if self.lan_find_button.handle_click(pos):
                self.scan_network_games(); return
            if self.lan_ip_button.handle_click(pos):
                self.network_screen = "ip"
                self.network_status = ""
                self.network_ip_active = True
                self.network_port_active = False
                if len(self.network_games) == 1:
                    game = self.network_games[0]
                    self.network_ip_text = str(game.get("host", ""))
                    self.network_port_text = str(game.get("port", network.DEFAULT_TCP_PORT))
                return
            if self.lan_back_button.handle_click(pos):
                self.close_network()
                self.pending_network_host = False
                self.state.phase = "main_menu"
                return
        elif self.network_screen == "browser":
            if self.lan_refresh_button.handle_click(pos):
                self.scan_network_games(); return
            y = 210
            if self.network_games:
                y += 34
                for game in self.network_games:
                    btn = Button((menu_x, y, 480, 64), f"JOIN  {game.get('name', 'Game')}")
                    if btn.handle_click(pos):
                        self.join_network_game(game); return
                    y += 76
            # Кнопка имеет фиксированную нижнюю позицию и больше не зависит
            # от количества комнат, поэтому не налезает на сообщение.
            self.lan_back_menu_button.rect.y = config.SCREEN_HEIGHT - 88
            if self.lan_back_menu_button.handle_click(pos):
                self.network_screen = "menu"; self.network_status = ""; return
        elif self.network_screen == "ip":
            if self.lan_ip_join_button.handle_click(pos):
                self.join_network_by_ip(); return
            if self.lan_ip_cancel_button.handle_click(pos):
                self.network_screen = "menu"; return
            ip_rect = pygame.Rect(menu_x, 285, 480, 42)
            port_rect = pygame.Rect(menu_x, 340, 180, 42)
            if ip_rect.collidepoint(pos):
                self.network_ip_active = True
                self.network_port_active = False
                return
            if port_rect.collidepoint(pos):
                self.network_ip_active = False
                self.network_port_active = True
                return
        elif self.network_screen == "host_wait":
            self.lan_back_menu_button.rect.y = config.SCREEN_HEIGHT - 88
            if self.lan_back_menu_button.handle_click(pos) and self.network_role is not None:
                self.close_network()
                self.pending_network_host = False
                self.network_screen = "menu"
                self.state.phase = "main_menu"

    def handle_local_network_key(self, event):
        """Обрабатывает ТОЛЬКО управляющие клавиши (Backspace/Enter) для
        полей IP/порта. Ввод самих символов больше не читается отсюда —
        см. handle_local_network_text() и pygame.TEXTINPUT ниже.

        ПОЧЕМУ разделено: раньше символы добавлялись через event.unicode
        прямо в KEYDOWN — рабочий, но чисто desktop-приём (ручной разбор
        физической клавиатуры). На Android виртуальная клавиатура и IME
        генерируют текст через pygame.TEXTINPUT, а не через KEYDOWN с
        осмысленным unicode на каждую нажатую клавишу. Если оставить оба
        пути одновременно на desktop, где both события существуют,
        символы задваивались бы. Поэтому KEYDOWN теперь отвечает только
        за Backspace/Enter (у них нет TEXTINPUT-события в принципе), а
        весь печатаемый текст — единственно через TEXTINPUT.
        """
        if self.network_screen != "ip":
            return
        if not (self.network_ip_active or self.network_port_active):
            return

        if event.key == pygame.K_BACKSPACE:
            active_attr = "network_ip_text" if self.network_ip_active else "network_port_text"
            value = getattr(self, active_attr)
            setattr(self, active_attr, value[:-1])
        elif event.key == pygame.K_RETURN:
            self.join_network_by_ip()

    def handle_local_network_text(self, event):
        """pygame.TEXTINPUT — печатаемые символы для полей IP/порта.

        Работает одинаково от физической клавиатуры (desktop) и от
        экранной клавиатуры/IME (Android), в отличие от чтения
        event.unicode из KEYDOWN. Требует pygame.key.start_text_input()
        (см. _sync_text_input_state), иначе на некоторых платформах
        TEXTINPUT вообще не генерируется.
        """
        if self.network_screen != "ip":
            return
        active_attr = None
        if self.network_ip_active:
            active_attr = "network_ip_text"
        elif self.network_port_active:
            active_attr = "network_port_text"
        if active_attr is None:
            return
        for ch in event.text:
            value = getattr(self, active_attr)
            if active_attr == "network_ip_text":
                # IP-адрес состоит из десятичных цифр и точек.
                if (ch.isdigit() or ch == ".") and len(value) < 15:
                    setattr(self, active_attr, value + ch)
            else:
                if ch.isdigit() and len(value) < 5:
                    setattr(self, active_attr, value + ch)

    def _sync_text_input_state(self):
        """Включает/выключает SDL text-input режим (IME/виртуальная
        клавиатура на Android) синхронно с тем, активно ли сейчас поле
        ввода IP/порта. Вызывается раз за кадр из run() — дешёво и
        идемпотентно, поэтому не нужно расставлять start/stop по всем
        местам, где меняется network_screen/*_active (простая логика,
        а не "if android" в десяти местах).
        """
        want_active = (
            self.state.phase == "local_network"
            and self.network_screen == "ip"
            and (self.network_ip_active or self.network_port_active)
        )
        if want_active and not self._text_input_active:
            pygame.key.start_text_input()
            self._text_input_active = True
        elif not want_active and self._text_input_active:
            pygame.key.stop_text_input()
            self._text_input_active = False

    def draw_local_network_screen(self, mouse_pos):
        R.draw_board(self.screen)
        overlay = pygame.Surface((config.SCREEN_WIDTH, config.SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((10, 10, 14, 210))
        self.screen.blit(overlay, (0, 0))
        menu_x = self.panel_x - 240
        R.draw_panel(self.screen, pygame.Rect(menu_x - 22, 62, 524, min(config.SCREEN_HEIGHT - 86, 620)), radius=16)
        R.draw_text(self.screen, "LOCAL NETWORK", (menu_x, 90), self.font_big, config.COLOR_ACCENT)
        R.draw_text(self.screen, f"Карта: {config.BOARD_WIDTH} × {config.BOARD_HEIGHT}", (menu_x, 124), self.font_small, config.COLOR_TEXT_DIM)
        R.draw_text(self.screen, f"Режим: {config.GAME_MODE_NAMES[self.selected_game_mode]}", (menu_x + 180, 124), self.font_small, config.COLOR_TEXT_DIM)

        if self.network_screen == "menu":
            self.lan_host_button.draw(self.screen, self.font, mouse_pos)
            self.lan_find_button.draw(self.screen, self.font, mouse_pos)
            self.lan_ip_button.draw(self.screen, self.font, mouse_pos)
            self.lan_back_button.draw(self.screen, self.font, mouse_pos)
        elif self.network_screen == "browser":
            self.lan_refresh_button.draw(self.screen, self.font_small, mouse_pos)
            y = 190
            if self.network_scan_busy:
                R.draw_text(self.screen, "Сканирование локальной сети...", (menu_x, y), self.font, config.COLOR_TEXT)
            elif not self.network_games:
                R.draw_text(self.screen, "Игры не найдены.", (menu_x, y), self.font, config.COLOR_TEXT_DIM)
                R.draw_text(self.screen, "Нажмите «Сканировать снова» или подключитесь по IP.",
                            (menu_x, y + 28), self.font_small, config.COLOR_TEXT_DIM)
            else:
                R.draw_text(self.screen, "ДОСТУПНЫЕ ИГРЫ", (menu_x, y), self.font_mid, config.COLOR_TEXT)
                y += 34
                for game in self.network_games:
                    rect = pygame.Rect(menu_x, y, 480, 64)
                    R.draw_button(self.screen, rect, f"JOIN  {game.get('name', 'Game')}  |  {game.get('board', '?')}  |  {game.get('mode', '?')}", self.font_small, hover=rect.collidepoint(mouse_pos))
                    R.draw_text(self.screen, "ОЖИДАНИЕ...", (menu_x + 12, y + 38), self.font_small, config.COLOR_TEXT_DIM)
                    y += 76
            self.lan_back_menu_button.rect.y = config.SCREEN_HEIGHT - 88
            self.lan_back_menu_button.draw(self.screen, self.font_small, mouse_pos)
        elif self.network_screen == "ip":
            R.draw_text(self.screen, "IP хоста", (menu_x, 250), self.font_small, config.COLOR_TEXT_DIM)
            ip_rect = pygame.Rect(menu_x, 285, 480, 42)
            R.draw_button(self.screen, ip_rect, self.network_ip_text or "Введите IP хоста", self.font, active=self.network_ip_active)
            R.draw_text(self.screen, "Порт", (menu_x, 330), self.font_small, config.COLOR_TEXT_DIM)
            port_rect = pygame.Rect(menu_x, 350, 180, 42)
            R.draw_button(self.screen, port_rect, self.network_port_text or str(network.DEFAULT_TCP_PORT), self.font, active=self.network_port_active)
            self.lan_ip_cancel_button.rect.y = 420
            self.lan_ip_join_button.rect.y = 420
            self.lan_ip_cancel_button.draw(self.screen, self.font_small, mouse_pos)
            self.lan_ip_join_button.draw(self.screen, self.font_small, mouse_pos)
        elif self.network_screen == "host_wait":
            # Waiting screen intentionally does not show the gameplay board.
            # It is a room lobby until the second player connects.
            self.screen.fill(config.COLOR_BG)
            title = "КОМНАТА СОЗДАНА — ЖДЁМ ИГРОКА" if self.network_role == "host" else "ПОДКЛЮЧЕНИЕ"
            R.draw_text(self.screen, title, (menu_x, 190), self.font_mid, config.COLOR_TEXT)
            R.draw_text(self.screen, self.network_status, (menu_x, 228), self.font_small, config.COLOR_TEXT_DIM)
            if self.network_role == "host":
                R.draw_text(self.screen, "Передайте этот IP второму ПК:", (menu_x, 265), self.font_small, config.COLOR_TEXT_DIM)
                R.draw_text(self.screen, f"IP: {network.get_local_ipv4()}", (menu_x, 290), self.font_mid, config.COLOR_TEXT)
                R.draw_text(self.screen, f"Порт: {self.network_host.tcp_port if self.network_host else network.DEFAULT_TCP_PORT}", (menu_x, 318), self.font_small, config.COLOR_TEXT_DIM)
                R.draw_text(self.screen, "На втором ПК: Мультиплеер → Подключиться по IP.", (menu_x, 350), self.font_small, config.COLOR_TEXT_DIM)
                R.draw_text(self.screen, "Хост — белые, подключившийся — чёрные.", (menu_x, 372), self.font_small, config.COLOR_TEXT_DIM)
            else:
                R.draw_text(self.screen, "Получаем конфигурацию комнаты…", (menu_x, 270), self.font_small, config.COLOR_TEXT_DIM)
            self.lan_back_menu_button.rect.y = config.SCREEN_HEIGHT - 88
            self.lan_back_menu_button.draw(self.screen, self.font_small, mouse_pos)
        if self.network_status:
            R.draw_text(self.screen, self.network_status, (menu_x, config.SCREEN_HEIGHT - 28), self.font_small, config.COLOR_TEXT_DIM)

    # ------------------------------------------------------------------
    # Игровой процесс — выбор фигуры и построение карты доступных действий
    # ------------------------------------------------------------------
    def get_selected_piece(self):
        if self.selected_piece_id is None:
            return None
        return self.state.pieces.get(self.selected_piece_id)

    def build_action_map(self, piece):
        """Строит карту (клетка -> действие) для действий с целевой
        клеткой (move/attack/heal), и отдельно собирает действия БЕЗ
        клетки на доске (queen_lock/queen_unlock) и действия спешивания
        коня (dismount).

        ВАЖНО: dismount-клетки НЕ подмешиваются в общую cell_map — цели
        спешивания коня (пустые клетки рядом с носителем) часто
        совпадают с обычными клетками хода носителя, и при смешивании в
        один словарь одно действие тихо перекрывало бы другое. Вместо
        этого спешивание — отдельный "вооружаемый" режим (см.
        dismount_armed), как и выбор режима ферзя."""
        cell_map = {}
        special = []
        dismount_options = []
        legal = actions_mod.get_legal_actions(self.state, piece)
        for a in legal:
            if a["type"] == "move":
                cell_map[a["target"]] = a
            elif a["type"] == "dismount":
                dismount_options.append(a)
            elif a["type"] in ("attack", "mount", "heal", "rook_swap"):
                t = self.state.pieces.get(a["target_id"])
                if t is not None:
                    cell_map[(t.col, t.row)] = a
            elif a["type"] in ("queen_lock", "queen_unlock"):
                special.append(a)
        return cell_map, special, dismount_options

    def select_piece(self, piece):
        self.selected_piece_id = piece.id
        self.action_map, self.special_actions, self.dismount_options = self.build_action_map(piece)
        self.dismount_armed = False

    def deselect(self):
        self.selected_piece_id = None
        self.action_map = {}
        self.special_actions = []
        self.dismount_options = []
        self.dismount_armed = False

    def _damage_visual_events(self, before_state, action):
        """Возвращает визуальные попадания для конкретного действия.
        Считаются по ДО-ХОДОВОМУ снимку, поэтому одинаково работают локально,
        для ИИ и для удалённого игрока, включая коня-щит.
        """
        if not action or action.get("type") != "attack":
            return []
        attacker = before_state.pieces.get(action.get("piece_id"))
        target = before_state.pieces.get(action.get("target_id"))
        if attacker is None or target is None or not attacker.alive() or not target.alive():
            return []
        if action.get("mode") == "queen_ranged":
            damage = config.QUEEN_RANGED_DAMAGE
        else:
            damage = attacker.damage

        events = []

        def add_target(piece, dmg):
            actual = piece
            if piece.mounted_knight_id is not None:
                knight = before_state.pieces.get(piece.mounted_knight_id)
                if knight is not None and knight.alive():
                    actual = knight
            events.append({
                "cell": (actual.col, actual.row),
                "amount": int(dmg),
            })

        add_target(target, damage)
        return events

    def _spawn_damage_visuals(self, before_state, action):
        events = self._damage_visual_events(before_state, action)
        for i, event in enumerate(events):
            # Несколько цифр рядом с одной целью слегка разводим в стороны.
            offsets = (-0.34, 0.34, 0.0, -0.2, 0.2)
            self.animation.spawn_damage_number(event["cell"], event["amount"], offsets[i % len(offsets)])

    # ------------------------------------------------------------------
    # Применение действия с анимацией (используется и игроком, и ИИ)
    # ------------------------------------------------------------------
    def apply_action_with_animation(self, action):
        piece = self.state.pieces.get(action.get("piece_id"))
        if piece is None:
            return False
        old_cell = (piece.col, piece.row)

        if action["type"] == "attack":
            target = self.state.pieces.get(action["target_id"])
            if target is None:
                return False
            target_cell = (target.col, target.row)
            t_type, t_color = target.type, target.color
            before = self.state.clone_light()
            ok = actions_mod.apply_action(self.state, action)
            if ok:
                self._spawn_damage_visuals(before, action)
                self.animation.start_attack(piece.id, old_cell, target_cell, piece.type, action.get("mode"))
                self.animation.spawn_impact(target_cell)
                if not target.alive():
                    self.animation.spawn_death(target_cell, t_color, t_type)
                self._auto_finish_king_stage_after_action(piece.type)
            return ok

        if action["type"] == "rook_swap":
            target = self.state.pieces.get(action["target_id"])
            if target is None:
                return False
            old_target_cell = (target.col, target.row)
            ok = actions_mod.apply_action(self.state, action)
            if ok:
                self.animation.start_move(piece.id, old_cell, old_target_cell)
            return ok

        if action["type"] == "mount":
            target = self.state.pieces.get(action["target_id"])
            if target is None:
                return False
            target_cell = (target.col, target.row)
            ok = actions_mod.apply_action(self.state, action)
            if ok:
                self.animation.start_mount(piece.id, old_cell, target_cell)
            return ok

        if action["type"] == "dismount":
            ok = actions_mod.apply_action(self.state, action)
            if ok:
                self.animation.start_move(piece.id, old_cell, (piece.col, piece.row))
            return ok

        if action["type"] == "heal":
            target = self.state.pieces.get(action["target_id"])
            if target is None:
                return False
            target_cell = (target.col, target.row)
            ok = actions_mod.apply_action(self.state, action)
            if ok:
                self.animation.spawn_impact(target_cell)
                self._auto_finish_king_stage_after_action(piece.type)
            return ok

        if action["type"] in ("queen_lock", "queen_unlock"):
            return actions_mod.apply_action(self.state, action)

        # move
        ok = actions_mod.apply_action(self.state, action)
        if ok:
            self.animation.start_move(piece.id, old_cell, (piece.col, piece.row))
            self._auto_finish_king_stage_after_action(piece.type)
        return ok

    def _auto_finish_king_stage_after_action(self, piece_type):
        """Первое действие короля сразу завершает king_stage. Таймер больше
        не заставляет игрока ждать, если король уже сделал действие."""
        if piece_type != "king" or self.state.phase != "king_stage":
            return
        turn_system.end_king_stage(self.state)

    # ------------------------------------------------------------------
    # Обработка кликов во время хода игрока
    # ------------------------------------------------------------------
    def handle_game_click(self, pos):
        if self.surrender_button.handle_click(pos):
            self.surrender()
            return
        if self.animation.is_blocking():
            return
        if self.state.turn_color != self.player_color:
            return
        if self.state.phase not in ("king_stage", "lobby_stage"):
            return

        if self.state.phase == "king_stage" and self.skip_king_button.handle_click(pos):
            self.deselect()
            if self.network_role:
                self._network_end_king_stage()
            else:
                turn_system.end_king_stage(self.state)
            return
        if self.state.phase == "lobby_stage" and self.end_turn_button.handle_click(pos):
            self.deselect()
            if self.network_role:
                self._network_end_turn()
            else:
                turn_system.end_turn(self.state)
            return

        sel = self.get_selected_piece()
        if sel is not None and sel.type == "queen":
            for a in self.special_actions:
                if a["type"] == "queen_lock":
                    btn = self.queen_lock_buttons.get(a["mode"])
                    if btn is not None and btn.handle_click(pos):
                        if self.network_role:
                            self._network_request_action(a)
                        else:
                            self.apply_action_with_animation(a)
                        self.deselect()
                        return
                elif a["type"] == "queen_unlock":
                    if self.queen_unlock_button.handle_click(pos):
                        if self.network_role:
                            self._network_request_action(a)
                        else:
                            self.apply_action_with_animation(a)
                        self.deselect()
                        return

        if sel is not None and self.dismount_options and self.dismount_button.handle_click(pos):
            self.dismount_armed = not self.dismount_armed
            return

        cell = R.screen_to_board(*pos)
        if cell is None:
            return

        if sel is not None and self.dismount_armed:
            dismount_action = next((a for a in self.dismount_options if a["target"] == cell), None)
            if dismount_action is not None:
                if self.network_role:
                    self._network_request_action(dismount_action)
                else:
                    self.apply_action_with_animation(dismount_action)
                self.deselect()
                return
            # клик мимо подсвеченной клетки спешивания — просто снимаем "вооружение"
            self.dismount_armed = False
            return

        if sel is not None and cell in self.action_map:
            action = self.action_map[cell]
            if self.network_role:
                self._network_request_action(action)
            else:
                self.apply_action_with_animation(action)
            self.deselect()
            w = self.state.check_victory()
            if w:
                self.state.phase = "game_over"
                self.state.winner = w
            return

        piece = self.state.get_piece_at(*cell)
        if piece is not None and piece.color == self.player_color:
            if self.state.phase == "king_stage":
                if piece.type == "king" and piece.actions_available() > 0:
                    self.select_piece(piece)
                else:
                    self.deselect()
            else:
                # Разрешаем выбрать фигуру, если у неё есть ЛЮБОЕ действие
                # (включая спешивание коня, которое не требует action у носителя).
                if actions_mod.get_legal_actions(self.state, piece):
                    self.select_piece(piece)
                else:
                    self.deselect()
        else:
            self.deselect()

    # ------------------------------------------------------------------
    # Таймеры
    # ------------------------------------------------------------------
    def _play_timer_beep(self):
        try:
            if not self.audio_available:
                return
            import wave, io, math, struct
            rate = 22050
            duration = 0.16
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
                frames = bytearray()
                for i in range(int(rate * duration)):
                    env = 1.0 - i / (rate * duration)
                    v = int(10000 * env * math.sin(2 * math.pi * 880 * i / rate))
                    frames += struct.pack("<h", v)
                wf.writeframes(frames)
            snd = pygame.mixer.Sound(buffer=buf.getvalue())
            snd.play()
        except Exception:
            pass

    def _play_turn_notify_beep(self):
        """Мягкий двухтональный сигнал 'ход передан вам' — звучит один раз,
        когда противник (ИИ) закончил свой ход и очередь снова за игроком.
        Специально сделан мягче и мелодичнее одиночного резкого "тик" таймера
        короля (880 Гц), чтобы два сигнала не путались на слух: тут два
        коротких восходящих тона вместо одного тревожного щелчка."""
        try:
            if not self.audio_available:
                return
            import wave, io, math, struct
            rate = 22050
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
                frames = bytearray()
                for freq, duration in ((523.0, 0.09), (784.0, 0.14)):
                    n = int(rate * duration)
                    for i in range(n):
                        env = 1.0 - i / n
                        v = int(7000 * env * math.sin(2 * math.pi * freq * i / rate))
                        frames += struct.pack("<h", v)
                wf.writeframes(frames)
            snd = pygame.mixer.Sound(buffer=buf.getvalue())
            snd.play()
        except Exception:
            pass

    def _update_game_timer(self, dt):
        if self.state.phase == "game_over" or self.state.game_mode != "standard":
            return
        self.state.game_timer = max(0.0, float(self.state.game_timer) - dt)
        if self.state.game_timer <= 0:
            captured_w = self.state.captured.get("white", 0)
            captured_b = self.state.captured.get("black", 0)
            if captured_w > captured_b:
                winner = "white"
            elif captured_b > captured_w:
                winner = "black"
            else:
                winner = "draw"
            self.state.phase = "game_over"
            self.state.winner = winner

    def update_timers(self, dt):
        if self.state.phase == "game_over":
            return
        self._update_game_timer(dt)
        if self.state.phase == "game_over":
            return
        if self.network_role:
            return
        if self.state.turn_color != "white":
            return
        if self.animation.is_blocking():
            return
        if self.state.phase == "king_stage":
            self.state.king_stage_timer -= dt
            if self.state.king_stage_timer <= 0:
                if self.timer_beeped_turn != (self.state.turn_number, "king"):
                    self._play_timer_beep(); self.timer_beeped_turn = (self.state.turn_number, "king")
                self.deselect(); turn_system.end_king_stage(self.state)
        elif self.state.phase == "lobby_stage":
            self.state.main_timer -= dt
            if self.state.main_timer <= 0:
                self.deselect(); turn_system.end_turn(self.state)

    # ------------------------------------------------------------------
    # Ход ИИ: фоновый поток на расчёт + пошаговое выполнение с анимацией
    # ------------------------------------------------------------------
    def _ai_compute_worker(self, state_snapshot, color):
        try:
            plan = self.ai_player.choose_turn_plan(state_snapshot, color)
        except Exception:
            plan = []
        self.ai_thread_result["plan"] = plan

    def update_ai_turn(self, dt):
        if self.state.phase == "game_over":
            return
        if self.state.turn_color != "black":
            self.ai_status = AI_IDLE
            return

        if self.ai_status == AI_IDLE:
            self.ai_status_text = "Думает..."
            self.ai_thread_result = {}
            self.temporarily_revealed_ids = set()
            # Полный (не "затуманенный") клон — только для потокобезопасности:
            # даёт фоновому потоку независимую от self.state копию. Сам туман
            # войны применяет ai_player.choose_turn_plan() (см.
            # AIPlayer._fogged_snapshot в ai_interface.py) — это единственная
            # и обязательная точка его применения для ЛЮБОГО типа ИИ, а не
            # ответственность этого экрана.
            snapshot = self.state.clone_light()
            self.ai_thread = threading.Thread(
                target=self._ai_compute_worker, args=(snapshot, "black"), daemon=True)
            self.ai_thread.start()
            self.ai_status = AI_THINKING
            return

        if self.ai_status == AI_THINKING:
            if self.ai_thread is not None and not self.ai_thread.is_alive():
                self.ai_plan = self.ai_thread_result.get("plan", [])
                self.ai_step_index = 0
                self.ai_status = AI_EXECUTING if self.ai_plan else AI_FINISHED
            return

        if self.ai_status == AI_EXECUTING:
            if self.animation.is_blocking():
                return
            if self.ai_step_pause_timer > 0:
                self.ai_step_pause_timer -= dt
                return
            if self.ai_step_index >= len(self.ai_plan):
                self.ai_status = AI_FINISHED
                return
            self._execute_next_ai_step()
            return

        if self.ai_status == AI_FINISHED:
            if self.animation.is_blocking():
                return
            self._finish_ai_turn()
            self.ai_status = AI_IDLE

    def _execute_next_ai_step(self):
        action = self.ai_plan[self.ai_step_index]
        self.ai_step_index += 1
        piece = self.state.pieces.get(action.get("piece_id"))
        if piece is None:
            return
        pname = PIECE_NAMES_RU.get(piece.type, piece.type)

        if self.ai_first_action_type is None:
            self.ai_first_action_type = piece.type

        if action["type"] == "move":
            self.ai_status_text = f"Ход: {pname}"
        elif action["type"] == "attack":
            target = self.state.pieces.get(action["target_id"])
            tname = PIECE_NAMES_RU.get(target.type, target.type) if target else "?"
            self.ai_status_text = f"Атака: {pname} \u2192 {tname}"
            if self.state.fog_enabled and config.FOG_REVEAL_ON_ATTACK:
                self.temporarily_revealed_ids.add(piece.id)
        elif action["type"] == "mount":
            target = self.state.pieces.get(action["target_id"])
            tname = PIECE_NAMES_RU.get(target.type, target.type) if target else "?"
            self.ai_status_text = f"Конь садится на {tname}"
            self.ai_used_mount = True
        elif action["type"] == "dismount":
            self.ai_status_text = "Конь спешивается"
        elif action["type"] == "heal":
            target = self.state.pieces.get(action["target_id"])
            tname = PIECE_NAMES_RU.get(target.type, target.type) if target else "?"
            self.ai_status_text = f"Король лечит {tname}"
        elif action["type"] == "queen_lock":
            self.ai_status_text = f"Ферзь включает: {QUEEN_MODE_NAMES.get(action['mode'], action['mode'])}"
        elif action["type"] == "queen_unlock":
            self.ai_status_text = "Ферзь выходит из режима ДАЛЬНЕЙ АТАКИ"

        self.apply_action_with_animation(action)
        self.ai_step_pause_timer = config.AI_STEP_PAUSE

    def _finish_ai_turn(self):
        w = self.state.check_victory()
        if w:
            self.state.phase = "game_over"
            self.state.winner = w
            return
        turn_system.end_turn(self.state)
        self.ai_status_text = ""
        # Ход снова у игрока — короткий звуковой сигнал, чтобы не приходилось
        # постоянно смотреть на экран, пока думает ИИ.
        self._play_turn_notify_beep()

    # ------------------------------------------------------------------
    # Память ИИ — сохраняется один раз при завершении партии
    # ------------------------------------------------------------------
    def save_memory_if_needed(self):
        if self.network_role:
            return
        if self.memory_saved or self.state.phase != "game_over":
            return
        if not self.settings.get("memory_enabled", True):
            self.memory_saved = True
            return
        self.ai_memory = ai_memory.record_game_result(
            self.ai_memory, self.state.winner, ai_color=config.AI_COLOR,
            opening_piece_type=self.ai_first_action_type, used_mount=self.ai_used_mount,
            turns=self.state.turn_number)
        ai_memory.save_memory(self.ai_memory)
        ai_memory.append_game_log(self.state.winner, config.AI_COLOR, self.state.turn_number,
                                   self.selected_difficulty, self.selected_opponent_type)
        self.memory_saved = True

    # ------------------------------------------------------------------
    # Отрисовка игрового экрана
    # ------------------------------------------------------------------
    def _visible_enemy_ids(self):
        if not self.state.fog_enabled:
            return None
        return fog.visible_enemy_ids(self.state, self.player_color) | self.temporarily_revealed_ids

    def draw_game_screen(self, mouse_pos):
        self._update_background_surface()
        R.draw_board(self.screen)

        fogged_cells = fog.visible_cells(self.state, self.player_color)
        if fogged_cells is not None:
            for c in range(config.BOARD_WIDTH):
                for r in range(config.BOARD_HEIGHT):
                    if (c, r) not in fogged_cells:
                        R.draw_fog_cell(self.screen, c, r)

        visible_ids = self._visible_enemy_ids()

        sel = self.get_selected_piece()
        if sel is not None:
            move_cells = [c for c, a in self.action_map.items() if a["type"] == "move"]
            attack_cells = [c for c, a in self.action_map.items() if a["type"] == "attack"]
            mount_cells = [c for c, a in self.action_map.items() if a["type"] == "mount"]
            heal_cells = [c for c, a in self.action_map.items() if a["type"] == "heal"]
            swap_cells = [c for c, a in self.action_map.items() if a["type"] == "rook_swap"]
            R.draw_highlights(self.screen, move_cells, config.COLOR_HIGHLIGHT_MOVE)
            R.draw_highlights(self.screen, attack_cells, config.COLOR_HIGHLIGHT_ATTACK)
            R.draw_highlights(self.screen, mount_cells, config.COLOR_HIGHLIGHT_MOUNT)
            R.draw_highlights(self.screen, heal_cells, config.COLOR_HIGHLIGHT_HEAL)
            R.draw_highlights(self.screen, swap_cells, config.COLOR_HIGHLIGHT_SWAP)
            if self.dismount_armed:
                dismount_cells = [a["target"] for a in self.dismount_options]
                R.draw_highlights(self.screen, dismount_cells, config.COLOR_HIGHLIGHT_MOUNT)
            R.draw_cell_border(self.screen, sel.col, sel.row, config.COLOR_HIGHLIGHT_SELECT)

        R.draw_all_pieces(self.screen, self.state, self.font_small, self.selected_piece_id,
                           animation=self.animation, visible_ids=visible_ids)
        R.draw_animation_effects(self.screen, self.animation, attacker_color_lookup=self._piece_color_lookup)

        self.draw_side_panel(mouse_pos)

    def _piece_color_lookup(self, piece_id):
        p = self.state.pieces.get(piece_id)
        return p.color if p else None

    def draw_side_panel(self, mouse_pos):
        # HUD никогда не участвует в повороте перспективы: для чёрных
        # разворачивается только игровая доска через coords.py.
        panel_x = config.BOARD_MARGIN_X + config.BOARD_WIDTH * config.CELL_SIZE + 20
        self.panel_x = panel_x
        s = self.state
        # HUD выравнивается по верхнему краю доски, а не по верхнему краю окна.
        # На прямоугольных картах это заметно аккуратнее.
        lay = Layout(config.BOARD_MARGIN_Y + 6, x=panel_x)

        if self.network_role:
            turn_label = "ВЫ (белые)" if self.player_color == "white" and s.turn_color == "white" else ("ВЫ (чёрные)" if self.player_color == "black" and s.turn_color == "black" else "СОПЕРНИК")
        else:
            turn_label = "ВЫ (белые)" if s.turn_color == "white" else "ИИ (чёрные)"
        R.draw_text(self.screen, f"Ход: {turn_label}", (panel_x, lay.take(24, gap=4)), self.font_mid, config.COLOR_ACCENT)
        R.draw_text(self.screen, f"Номер хода: {s.turn_number}   Карта: {config.BOARD_WIDTH}x{config.BOARD_HEIGHT}",
                    (panel_x, lay.take(16, gap=2)), self.font_small, config.COLOR_TEXT_DIM)
        R.draw_text(self.screen, f"Режим: {config.GAME_MODE_NAMES.get(s.game_mode, s.game_mode)}",
                    (panel_x, lay.take(16, gap=6)), self.font_small, config.COLOR_TEXT_DIM)

        if s.game_mode == "standard":
            total_seconds = max(0, int(s.game_timer))
            minutes, seconds = divmod(total_seconds, 60)
            timer_color = config.COLOR_DAMAGE_NUMBER if total_seconds <= 30 else config.COLOR_TEXT_DIM
            R.draw_text(self.screen, f"Таймер боя: {minutes}:{seconds:02d}",
                        (panel_x, lay.take(16, gap=6)), self.font_small, timer_color)

        phase_names = {
            "king_stage": "Стадия 1: действие короля",
            "lobby_stage": "Стадия 2: свободные действия",
            "game_over": "Игра окончена",
        }
        R.draw_text(self.screen, phase_names.get(s.phase, s.phase), (panel_x, lay.take(16, gap=4)), self.font_small, config.COLOR_TEXT)

        if s.turn_color == self.player_color:
            if s.phase == "king_stage":
                R.draw_text(self.screen, f"Таймер короля: {max(0, s.king_stage_timer):.1f} c",
                            (panel_x, lay.take(16, gap=6)), self.font_small, config.COLOR_PLAYER_UI)
            elif s.phase == "lobby_stage":
                R.draw_text(self.screen, f"Таймер хода: {max(0, s.main_timer):.1f} c",
                            (panel_x, lay.take(16, gap=6)), self.font_small, config.COLOR_PLAYER_UI)
        else:
            if self.network_role:
                hud = "Сеть: ваш ход" if s.turn_color == self.player_color else "Сеть: ход соперника"
                R.draw_text(self.screen, hud, (panel_x, lay.take(16, gap=2)), self.font_small, config.COLOR_ENEMY_UI)
                if self.network_status:
                    R.draw_text(self.screen, self.network_status, (panel_x, lay.take(14, gap=6)), self.font_small, config.COLOR_TEXT_DIM)
            else:
                hud = {
                    AI_IDLE: "ИИ...",
                    AI_THINKING: "ИИ ДУМАЕТ...",
                    AI_EXECUTING: f"ИИ: {self.ai_status_text}" if self.ai_status_text else "ИИ ХОДИТ...",
                    AI_FINISHED: "ИИ ЗАВЕРШАЕТ ХОД...",
                }.get(self.ai_status, "ИИ...")
                R.draw_text(self.screen, hud, (panel_x, lay.take(16, gap=2)), self.font_small, config.COLOR_ENEMY_UI)
                if self.ai_player is not None:
                    R.draw_text(self.screen, self.ai_player.describe(), (panel_x, lay.take(14, gap=6)), self.font_small, config.COLOR_TEXT_DIM)

        if s.game_mode == "base":
            comp = s.base_compromised
            if self.network_role:
                me_comp = comp.get(self.player_color)
                opp_color = "black" if self.player_color == "white" else "white"
                txt = f"Тыл: вы {'ПРОРВАН' if me_comp else 'в порядке'} / соперник {'прорван' if comp.get(opp_color) else 'в порядке'}"
            else:
                txt = f"Тыл: вы {'ПРОРВАН' if comp.get('white') else 'в порядке'} / ИИ {'прорван' if comp.get('black') else 'в порядке'}"
            R.draw_text(self.screen, txt, (panel_x, lay.take(16, gap=6)),
                        self.font_small, config.COLOR_ENEMY_UI if comp.get("white") else config.COLOR_TEXT_DIM)

        sel = self.get_selected_piece()
        card_top = lay.take(0, gap=10)
        if sel is not None:
            card = Layout(card_top, x=panel_x)
            R.draw_text(self.screen, f"Выбрано: {PIECE_NAMES_RU.get(sel.type, sel.type)}",
                        (panel_x, card.take(20, gap=6)), self.font, config.COLOR_TEXT)
            R.draw_text(self.screen, f"HP: {sel.hp} / {sel.max_hp}", (panel_x, card.take(16, gap=2)), self.font_small)
            R.draw_text(self.screen, f"Урон: {sel.damage}", (panel_x, card.take(16, gap=2)), self.font_small)
            if sel.type == "rook":
                R.draw_text(self.screen, "Спецприём: обмен местами с соседней союзной фигурой",
                            (panel_x, card.take(16, gap=4)), self.font_small, config.COLOR_HIGHLIGHT_SWAP)
            R.draw_text(self.screen, f"Действий доступно: {sel.actions_available()} / {sel.max_actions()}",
                        (panel_x, card.take(16, gap=4)), self.font_small)
            if sel.mounted_knight_id:
                R.draw_text(self.screen, "Конь: щит + доп. действие",
                            (panel_x, card.take(16, gap=2)), self.font_small, config.COLOR_HIGHLIGHT_MOUNT)
                if self.dismount_options:
                    label = "Отменить спешивание" if self.dismount_armed else "Спешить коня"
                    self.dismount_button.text = label
                    self.dismount_button.rect.y = card.take(32, gap=6)
                    self.dismount_button.draw(self.screen, self.font_small, mouse_pos, active=self.dismount_armed)
                    if self.dismount_armed:
                        R.draw_text(self.screen, "Выберите пустую клетку рядом для спешивания",
                                    (panel_x, card.take(14, gap=2)), self.font_small, config.COLOR_HIGHLIGHT_MOUNT)
            if sel.type == "king":
                R.draw_text(self.screen, "Может лечить соседнюю союзную фигуру",
                            (panel_x, card.take(16, gap=2)), self.font_small, config.COLOR_HIGHLIGHT_HEAL)

            if sel.type == "queen":
                mode_label = QUEEN_MODE_NAMES.get(sel.queen_locked_mode or "move", "ПЕРЕМЕЩЕНИЕ")
                status = " (турель — режим активен)" if sel.queen_locked_mode else " (мобилен)"
                R.draw_text(self.screen, f"Режим: {mode_label}{status}",
                            (panel_x, card.take(16, gap=8)),
                            self.font_small, config.COLOR_HIGHLIGHT_HEAL if sel.queen_locked_mode else config.COLOR_TEXT_DIM)
                if sel.queen_locked_mode is None:
                    for mode, btn in self.queen_lock_buttons.items():
                        btn.rect.y = card.take(34, gap=6)
                        btn.draw(self.screen, self.font_small, mouse_pos)
                    R.draw_text(self.screen, "Сплэш-атака — обычное действие, доступное на клетках цели.",
                                (panel_x, card.take(14, gap=2)), self.font_small, config.COLOR_TEXT_DIM)
                else:
                    self.queen_unlock_button.rect.y = card.take(32, gap=6)
                    self.queen_unlock_button.draw(self.screen, self.font_small, mouse_pos)
        else:
            R.draw_text(self.screen, "Выберите свою фигуру на доске", (panel_x, card_top + 4), self.font_small, config.COLOR_TEXT_DIM)

        if s.turn_color == self.player_color:
            if s.phase == "king_stage":
                self.skip_king_button.draw(self.screen, self.font, mouse_pos)
            elif s.phase == "lobby_stage":
                self.end_turn_button.draw(self.screen, self.font, mouse_pos)

        board_bottom = config.BOARD_MARGIN_Y + config.BOARD_HEIGHT * config.CELL_SIZE
        self.skip_king_button.rect.y = board_bottom - 40 - self.skip_king_button.rect.h - 6
        self.end_turn_button.rect.y = board_bottom - 40 - self.end_turn_button.rect.h - 6
        self.surrender_button.rect.y = board_bottom - self.surrender_button.rect.h
        self.surrender_button.draw(self.screen, self.font_small, mouse_pos)

    def draw_game_over(self, mouse_pos):
        self.draw_game_screen(mouse_pos)
        overlay = pygame.Surface((config.SCREEN_WIDTH, config.SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 180))
        self.screen.blit(overlay, (0, 0))
        if self.state.winner == self.player_color:
            text = "ПОБЕДА: ВЫ"
        elif self.state.winner in ("white", "black"):
            text = "ПОБЕДА: СОПЕРНИКА" if self.network_role else "ПОБЕДА: ИИ"
        else:
            text = "НИЧЬЯ"
        label = self.font_big.render(text, True, config.COLOR_ACCENT)
        rect = label.get_rect(center=(config.SCREEN_WIDTH // 2, config.SCREEN_HEIGHT // 2 - 20))
        self.screen.blit(label, rect)
        if self.network_role:
            self.network_rematch_button.draw(self.screen, self.font, mouse_pos)
            self.network_exit_button.draw(self.screen, self.font, mouse_pos)
        else:
            self.restart_button.draw(self.screen, self.font, mouse_pos)

    def surrender(self):
        if self.state.phase in ("game_over", "main_menu", "local_network"):
            return
        winner = "black" if self.player_color == "white" else "white"
        self.state.phase = "game_over"
        self.state.winner = winner
        self.state.last_event = "Игрок сдался"
        if self.network_role == "host" and self.network_host:
            self.network_host.force_game_over(winner)
        elif self.network_role == "client" and self.network_client:
            self.network_client.command("surrender")

    def restart(self):
        """Начать новую игру без пересоздания SDL-окна.

        Раньше restart() вызывал __init__(), а тот заново делал
        pygame.display.set_mode(). В fullscreen это давало заметный
        чёрный/белый всплеск. Теперь очищаем игровую/сетевую сессию
        прямо на существующем окне.
        """
        self.close_network()

        self.state = GameState()
        # "Новая игра" должна возвращать в ГЛАВНОЕ меню, а не сразу на
        # экран выбора режима (это пропускало сам главный экран).
        self.state.phase = "main_menu"
        self.setup_pieces = []
        self.dragging_index = None
        self.drag_offset = (0, 0)

        self.network_screen = "menu"
        self.network_games = []
        self.network_scan_thread = None
        self.network_scan_result = []
        self.network_scan_busy = False
        self.network_reconnect_timer = 0.0
        self.network_reconnect_attempts = 0
        self.network_rematch_sent = False
        self.pending_network_action = None
        self.pending_network_host = False
        self.network_status = ""
        self.remote_setup_pending = False
        self.setup_player_color = "white"
        self.player_color = "white"
        self._set_viewer_color("white")

        self.selected_piece_id = None
        self.action_map = {}
        self.special_actions = []
        self.dismount_options = []
        self.dismount_armed = False
        self.temporarily_revealed_ids = set()

        self.ai_player = None
        self.ai_status = AI_IDLE
        self.ai_status_text = ""
        self.ai_thread = None
        self.ai_thread_result = {}
        self.ai_plan = []
        self.ai_step_index = 0
        self.ai_step_pause_timer = 0.0
        self.ai_first_action_type = None
        self.ai_used_mount = False
        self.memory_saved = False

        self.animation = AnimationController(speed=self.settings.get("animation_speed", 1.0))
        self.timer_beeped_turn = None
        self.standard_timer_last = None

        self._rebuild_default_setup_pieces()
        self._build_ui_widgets()

    # ------------------------------------------------------------------
    # Главный цикл
    # ------------------------------------------------------------------
    def run(self):
        while self.running:
            dt = self.clock.tick(config.FPS) / 1000.0
            dt = min(dt, config.MAX_FRAME_DT)
            self._update_background_surface()
            mouse_pos = pygame.mouse.get_pos()
            self._poll_file_dialog()
            dialog_pending = self._file_dialog_proc is not None
            self._sync_text_input_state()

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif dialog_pending:
                    # Диалог выбора файла открыт в отдельном процессе —
                    # игнорируем игровой ввод, но продолжаем рисовать
                    # игру каждый кадр (см. _choose_file), чтобы окно не
                    # выглядело зависшим, пока пользователь работает с
                    # системным диалогом.
                    continue
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        self.running = False
                    elif event.key == pygame.K_F11:
                        self.toggle_fullscreen()
                    elif self.state.phase == "local_network":
                        self.handle_local_network_key(event)
                elif event.type == pygame.TEXTINPUT:
                    if self.state.phase == "local_network":
                        self.handle_local_network_text(event)
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if self.network_role is not None and self.network_screen == "setup_black":
                        if self.state.phase == "local_network":
                            # Reuse the deployment UI handlers for the remote black player.
                            self.handle_setup_mousedown(event.pos)
                        continue
                    if (self.network_role is not None
                            and self.network_screen in ("menu", "browser", "ip", "host_wait")):
                        self.handle_local_network_click(event.pos)
                        continue
                    if self.tutorial_active:
                        self.handle_tutorial_click(event.pos)
                    elif self.state.phase == "main_menu":
                        self.handle_main_menu_click(event.pos)
                    elif self.state.phase == "settings":
                        self.handle_settings_click(event.pos)
                    elif self.state.phase == "select_game_mode":
                        self.handle_select_game_mode_click(event.pos)
                    elif self.state.phase == "select_board_size":
                        self.handle_select_board_size_click(event.pos)
                    elif self.state.phase == "setup_white":
                        self.handle_setup_mousedown(event.pos)
                    elif self.state.phase == "select_opponent":
                        self.handle_select_opponent_click(event.pos)
                    elif self.state.phase == "local_network":
                        self.handle_local_network_click(event.pos)
                    elif self.state.phase == "game_over":
                        if self.network_role:
                            if self.network_rematch_button.handle_click(event.pos):
                                self.request_network_rematch()
                                continue
                            if self.network_exit_button.handle_click(event.pos):
                                self.exit_network_room()
                                continue
                        if self.restart_button.handle_click(event.pos):
                            self.restart()
                            continue
                    else:
                        self.handle_game_click(event.pos)
                elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                    if self.state.phase == "setup_white" or self.network_screen == "setup_black":
                        self.handle_setup_mouseup(event.pos)

            self.animation.update(dt)
            if self.tutorial_active:
                self._update_tutorial(dt)
            if self.network_role:
                self._pump_network_animations()
            if self.state.phase == "local_network" and not self.network_scan_busy and self.network_scan_thread is not None:
                self.network_games = list(self.network_scan_result)
                self.network_scan_thread = None

            active_game_phases = ("king_stage", "lobby_stage")
            if self.tutorial_active:
                pass  # обучение не крутит таймеры/ИИ/сеть — см. _update_tutorial
            elif self.network_role:
                self._network_tick(dt)
            elif self.state.phase in active_game_phases:
                self.update_timers(dt)
                self.update_ai_turn(dt)

            if self.state.phase == "game_over" and not self.tutorial_active:
                self.save_memory_if_needed()

            # Safety net for LAN transitions: once a match is actually in a
            # playable phase, the lobby UI is never allowed to remain on top.
            if (self.network_role is not None
                    and self.network_screen == "host_wait"
                    and self.state.phase in ("king_stage", "lobby_stage", "game_over")):
                self.network_screen = "game"
            if self.tutorial_active:
                self.draw_tutorial_screen(mouse_pos)
            elif self.network_role is not None and self.network_screen == "setup_black":
                self.draw_setup_screen(mouse_pos)
            elif (self.network_role is not None and
                    self.network_screen in ("menu", "browser", "ip", "host_wait")):
                self.draw_local_network_screen(mouse_pos)
            elif self.state.phase == "main_menu":
                self.draw_main_menu(mouse_pos)
            elif self.state.phase == "settings":
                self.draw_settings_screen(mouse_pos)
            elif self.state.phase == "select_game_mode":
                self.draw_select_game_mode_screen(mouse_pos)
            elif self.state.phase == "select_board_size":
                self.draw_select_board_size_screen(mouse_pos)
            elif self.state.phase == "setup_white":
                self.draw_setup_screen(mouse_pos)
            elif self.state.phase == "select_opponent":
                self.draw_select_opponent_screen(mouse_pos)
            elif self.state.phase == "local_network":
                self.draw_local_network_screen(mouse_pos)
            elif self.state.phase == "game_over":
                self.draw_game_over(mouse_pos)
            else:
                self.draw_game_screen(mouse_pos)

            pygame.display.flip()

        if self._file_dialog_proc is not None:
            try:
                self._file_dialog_proc.kill()
            except Exception:
                pass
        self.close_network()
        pygame.quit()
        sys.exit()


def main():
    app = App()
    app.run()


if __name__ == "__main__":
    main()
