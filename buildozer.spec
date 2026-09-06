[app]

# -----------------------------------------------------------------------
# HP Battle Chess — Android (Buildozer / python-for-android) build spec.
#
# ВАЖНО: это ОТДЕЛЬНАЯ конфигурация от hp_chess.spec (Windows PyInstaller).
# hp_chess.spec НЕ используется и НЕ заменяется этим файлом — Windows-сборка
# продолжает собираться как раньше, через `pyinstaller hp_chess.spec`.
#
# Сборка debug APK (на Linux/WSL с установленными Buildozer + Android
# SDK/NDK, см. https://buildozer.readthedocs.io/en/latest/installation.html):
#
#     buildozer -v android debug
#
# Результат: bin/hpbattlechess-<version>-arm64-v8a-debug.apk
# -----------------------------------------------------------------------

title = HP Battle Chess
package.name = hpbattlechess
package.domain = org.hpchess

# Корень исходников — этот же каталог (main.py лежит рядом со spec).
source.dir = .

# Только исходники Python: НЕТ статических assets (изображений/шрифтов/
# звуков) — вся графика рисуется в рантайме через pygame.draw и
# renderer.make_font()/pygame.font.Font(None, ...) (см. renderer.py).
# .json тоже не нужен: data/ — это WRITABLE runtime-каталог (настройки,
# память ИИ, сохранённые партии), создаётся программой САМА при первом
# запуске в app-private storage (см. platform_utils.get_app_writable_dir()
# и config.WRITABLE_DIR) — паковать его как read-only вход НЕЛЬЗЯ и не
# нужно.
source.include_exts = py

# data/ — рантайм-каталог (см. выше), __pycache__/тесты/dev-артефакты и
# Windows-специфичные файлы не нужны внутри APK.
source.exclude_dirs = data, __pycache__, .git, .idea, .vscode

source.exclude_patterns = *.pyc,*.pyo,*.spec,*.bat,*.md,tests.py

version = 1.0

# python3 — рантайм; pygame — рендерер и ввод (SDL2-бэкенд p4a).
# У проекта НЕТ прочих сторонних зависимостей — network.py использует
# только стандартную библиотеку (socket/threading/json/hashlib/struct/
# queue/uuid), ai_interface.py обращается к Ollama через urllib.request
# из стандартной библиотеки (см. п.12 — Ollama не обязателен и не
# блокирует запуск, если недоступен/не установлен на устройстве).
#
# ВАЖНО про версию pygame: requirements.txt (Windows) пинит pygame==2.6.1,
# но python-for-android собирает pygame из СВОЕГО recipe (обычно
# ощутимо более старая/иная минорная версия, чем то, что ставится через
# pip на desktop) — recipe НЕ обязан существовать под 2.6.1 конкретно.
# Здесь намеренно НЕ указана версия (requirements = python3,pygame),
# чтобы Buildozer/p4a взял ту версию pygame recipe, что реально
# собирается под Android в используемой версии p4a, вместо того чтобы
# сборка падала из-за отсутствия recipe под точную версию. Это НЕ
# затрагивает requirements.txt (Windows) — тот файл не менялся и
# продолжает пинить 2.6.1 для PyInstaller-сборки. Если конкретная
# версия окажется нужна и recipe её поддерживает, можно уточнить здесь:
#     requirements = python3,pygame==2.5.2
requirements = python3,pygame-ce,pyjnius,cython

# Точка входа не меняется — main.py остаётся тем же файлом, что и на
# Windows (см. п.18 ТЗ: не переписываем игру под другой движок/лаунчер).
entrypoint = main.py

# -----------------------------------------------------------------------
# Ориентация / экран (см. п.3 ТЗ)
# -----------------------------------------------------------------------
orientation = landscape
fullscreen = 1

# -----------------------------------------------------------------------
# Иконка/сплэш — намеренно НЕ заданы. У проекта нет готовых файлов иконки
# (вся графика генерируется в рантайме), а Buildozer у же подставляет
# дефолтную иконку python-for-android, если ничего не указано — этого
# достаточно для первой debug-сборки. Добавить кастомную иконку later:
# icon.filename = %(source.dir)s/icon.png
# -----------------------------------------------------------------------

# -----------------------------------------------------------------------
# Android permissions (см. п.9 ТЗ) — только то, что реально нужно LAN
# (UDP discovery на 38477 + TCP-сессия на 38478, см. network.py).
# ACCESS_WIFI_STATE/ACCESS_NETWORK_STATE нужны, чтобы получить локальный
# IP интерфейса Wi-Fi для UDP broadcast/discovery (get_local_ipv4()).
# -----------------------------------------------------------------------
android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE

# -----------------------------------------------------------------------
# API/архитектуры. minapi/target подобраны консервативно-совместимо для
# первой debug-сборки; arm64-v8a покрывает подавляющее большинство
# реальных Android-устройств последних лет.
# -----------------------------------------------------------------------
android.minapi = 24
android.api = 33
android.ndk_api = 24
android.archs = arm64-v8a

# python-for-android bootstrap для pygame — sdl2 (стандартный вариант
# для pygame-приложений на Android; НЕ Kivy-виджеты, чисто SDL2-surface,
# на которой рисует сам pygame — родной Pygame-рендерер проекта не
# переписывается, см. п. "не переписывай Pygame-рендерер" в ТЗ).
p4a.bootstrap = sdl2

# Полноэкранный режим уже задаётся через fullscreen=1 выше; оставляем
# системную навигацию Android по умолчанию (не скрываем статус-бар
# агрессивными hacks на первом debug-билде — можно донастроить позже).

log_level = 2
warn_on_root = 1

[buildozer]
# Debug-сборки подписываются автоматическим debug-ключом Buildozer —
# этого достаточно для локального тестирования на устройстве/эмуляторе.
# Для релизной (release) сборки потребуется отдельная настройка подписи
# (android.release_artifact, keystore и т.д.) — вне рамок первой debug
# сборки.
