# Сборка Android APK (Buildozer / python-for-android)

## Важная оговорка

Как и PyInstaller для Windows, Buildozer/python-for-android собирает
APK на **Linux** (нативно или в WSL2) — на самой Windows Buildozer не
работает. Всё, что подготовлено в этом коммите (`buildozer.spec`,
Android-фиксы в коде ниже), — то, что нужно, чтобы `buildozer -v android
debug` прошёл чисто и не упал по причинам, специфичным для Android.
Сам APK физически ещё не собирался (нет доступа к Android SDK/NDK/JDK в
этом окружении) — это должен сделать кто-то с настроенным
Linux/Buildozer-окружением, следуя шагам ниже.

## Установка окружения (один раз)

```bash
pip install --user buildozer cython
sudo apt install -y git zip unzip openjdk-17-jdk python3-pip autoconf \
    libtool pkg-config zlib1g-dev libncurses-dev cmake libffi-dev \
    libssl-dev automake
```

Buildozer сам скачает Android SDK/NDK при первой сборке (в
`~/.buildozer/android/platform/`) — вручную ставить их не обязательно.

## Сборка debug APK

```bash
cd hp_chess
buildozer -v android debug
```

Результат: `bin/hpbattlechess-1.0-arm64-v8a-debug.apk`.

Установить на подключённое по USB устройство (с включённой отладкой по
USB) и сразу посмотреть логи:

```bash
buildozer android deploy run logcat
```

## Что было исправлено/добавлено специально под Android

1. **`platform_utils.py` (новый файл)** — единая точка определения
   платформы: `is_android()` / `is_windows()` / `is_desktop()` и
   `get_app_writable_dir()`. Все остальные модули спрашивают платформу
   здесь, а не через рассыпанные по коду `if sys.platform == ...`.

2. **`config.py`: `WRITABLE_DIR`.** Раньше `data/settings.json`,
   `data/ai_memory.json` и `data/games/` считались от `BASE_DIR`
   (папка рядом со скриптом/`.exe`). На Android эта папка — внутри
   read-only APK, писать туда нельзя. Теперь `DATA_DIR` строится от
   `WRITABLE_DIR`, который на Windows/desktop **равен** `BASE_DIR`
   (поведение не меняется), а на Android — app-private writable
   storage платформы (создаётся автоматически). `settings.py` и
   `ai_memory.py` не потребовали изменений — они уже читали пути через
   `config.DATA_DIR`/`config.SETTINGS_FILE`/`config.AI_MEMORY_FILE`.

3. **Шрифты (`renderer.make_font`).** `pygame.font.SysFont("Arial",
   ...)` — desktop-допущение (ищет системный TTF по имени), которого
   на Android просто нет. `main.py` и `renderer.py` теперь используют
   `renderer.make_font(size, bold=...)`, оборачивающий встроенный
   `pygame.font.Font(None, size)` — работает одинаково на
   Windows/Linux/macOS/Android без зависимости от системных шрифтов.
   Визуальный стиль не изменился (это тот же дефолтный pygame-шрифт,
   которым и раньше рисовались подписи координат доски).

4. **Fullscreen/landscape.** На Android `App.fullscreen` всегда `True`
   (значение из `settings.json` игнорируется — на этой платформе это
   не пользовательская настройка, а требование платформы), а
   `_apply_screen_size()` создаёт окно через
   `pygame.display.set_mode((0, 0), pygame.FULLSCREEN)` вместо
   desktop-варианта `NOFRAME`-борд-fullscreen. Ориентация задаётся
   декларативно в `buildozer.spec` (`orientation = landscape`).
   `toggle_fullscreen()`/F11 на Android — no-op (windowed-режима на
   этой платформе не существует как концепция).

5. **Desktop file picker (музыка/фон).** `_choose_file()` на Android
   не запускает `subprocess`/`tkinter` (которых там просто нет) —
   вместо этого выставляет статусное сообщение "недоступно на
   Android" и ничего не ломает. Экран настроек продолжает работать,
   кнопки просто помечены как недоступные на этой платформе. Полноценный
   Android Storage Access Framework сознательно не реализован сейчас
   (риск нестабильности отдельного bridge) — см. п.7 исходного
   задания.

6. **Ввод текста для поля IP (мультиплеер).** Добавлена обработка
   `pygame.TEXTINPUT` (`handle_local_network_text`) — печатаемые
   символы (цифры/точка) теперь читаются оттуда, а не из
   `event.unicode` в `KEYDOWN`. Это работает и от физической
   клавиатуры (desktop), и от экранной клавиатуры/IME (Android), и не
   задваивает ввод. `KEYDOWN` теперь отвечает только за
   Backspace/Enter (у них нет TEXTINPUT-события). `pygame.key.
   start_text_input()`/`stop_text_input()` включаются/выключаются
   автоматически, пока активно поле IP/порта (`_sync_text_input_state`,
   вызывается раз за кадр).

7. **Touch-ввод.** Основной цикл кликов/перетаскивания фигур уже был
   построен на `MOUSEBUTTONDOWN`/`MOUSEBUTTONUP` + опрос
   `pygame.mouse.get_pos()` каждый кадр (а не на `MOUSEMOTION`) — SDL2
   на Android транслирует касания в те же mouse-события, поэтому
   отдельного touch-слоя не потребовалось. Изменений в этой части не
   вносилось.

8. **LAN (`network.py`) — без изменений.** Модуль уже использует
   только стандартную библиотеку (`socket`/`threading`/`json`/
   `hashlib`/`struct`/`queue`/`uuid`) — она собирается p4a "из коробки"
   вместе с рецептом `python3`, отдельный recipe не требуется. Протокол
   (UDP discovery на 38477 + TCP-сессия на 38478, host-authoritative,
   Fog of War) не менялся.

9. **AI/Ollama — без изменений.** `ai_interface.check_ollama_available()`
   уже использует `urllib.request` с таймаутом и штатно откатывается на
   Algorithm AI, если Ollama недоступна — на Android (где Ollama
   заведомо не установлена) это просто означает, что доступен только
   Algorithm AI/Local AI-fallback, без падений.

## `buildozer.spec` — ключевые моменты

- `requirements = python3,pygame` — версия pygame НЕ пинится (в отличие
  от `requirements.txt` для Windows, где `pygame==2.6.1`), потому что
  Android-сборка использует pygame-recipe python-for-android, который
  не обязан существовать под ту же точную версию, что pip-пакет на
  desktop. `requirements.txt` (Windows) не менялся.
- `orientation = landscape`, `fullscreen = 1`.
- `android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE`
  — минимально необходимое для LAN (UDP broadcast discovery + TCP).
- `source.include_exts = py` и `source.exclude_dirs = data,...` — в APK
  попадает только код; `data/` (настройки/память ИИ/сохранённые партии)
  сознательно НЕ упаковывается — это writable runtime-каталог, который
  приложение создаёт само в app-private storage при первом запуске (см.
  `platform_utils.get_app_writable_dir()`).
- `p4a.bootstrap = sdl2` — стандартный SDL2-бутстрап для pygame-игр
  (НЕ Kivy-виджеты, рендерер проекта не переписывался).

## Известные ограничения / caveats

- **UDP broadcast discovery.** Некоторые Android-устройства и
  роутеры с "AP/client isolation" на Wi-Fi блокируют broadcast/
  multicast между клиентами одной сети — это ограничение сети/прошивки
  роутера, а не приложения; в таком случае пользователю нужно
  подключаться напрямую по IP (экран "ПОДКЛЮЧИТЬСЯ ПО IP" уже есть и
  не зависит от discovery).
- Файл-пикер (музыка/фон) на Android недоступен в этой версии (см.
  п.5 выше) — это сознательное решение согласно ТЗ, а не забытая
  функциональность.
- APK физически не собирался в этом окружении (нет Android SDK/NDK) —
  все правки проверены статически (`py_compile`, `tests.py`) и ручным
  разбором кода, а не реальным `buildozer android debug`.
