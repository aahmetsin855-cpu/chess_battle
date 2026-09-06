# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec для HP Battle Chess (Windows x64, onedir, windowed).

ВАЖНО: PyInstaller НЕ кросс-компилирует — этот spec нужно запускать
НА САМОЙ Windows-машине, с Python 3.13 x64 и pygame, установленными
именно там (см. WINDOWS_BUILD.md рядом с этим файлом).

Сборка:
    pyinstaller hp_chess.spec

Результат:
    dist/HP Battle Chess/HP Battle Chess.exe   (+ вся папка рядом с ним)

У игры нет статических ассетов (изображений/шрифтов/звуков) — всё
рисуется и генерируется в рантайме через pygame.draw/pygame.font.Font(None, ...),
поэтому add_data тут не нужен. Папка data/ (настройки, память ИИ,
сохранённые игры) создаётся программой САМА рядом с .exe при первом
запуске (см. config.BASE_DIR — учитывает sys.frozen) и НЕ должна
паковаться внутрь бандла как вход.
"""

import sys

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # tkinter импортируется лениво (только внутри режима
        # --file-dialog-helper, см. main.py) — статический анализ
        # PyInstaller может это не заметить без явного hidden-import.
        'tkinter',
        'tkinter.filedialog',
        # network.py использует чистый stdlib socket/threading — hidden
        # imports тут не нужны, но модуль перечислен для ясности.
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Тестовый раннер не нужен в собранном приложении.
        'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HP Battle Chess',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX иногда триггерит антивирусы на false-positive
    console=False,       # --windowed: без консольного окна
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='x86_64',
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='HP Battle Chess',
)
