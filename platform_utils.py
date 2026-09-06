# -*- coding: utf-8 -*-
"""
Централизованное определение платформы и платформенных writable-путей.

ЦЕЛЬ ЭТОГО МОДУЛЯ: все остальные модули проекта (config.py, main.py и
т.д.) должны спрашивать "какая это платформа" и "куда можно писать"
ЗДЕСЬ, а не рассыпать собственные проверки sys.platform / os.environ по
всему коду. Так поддержка новой платформы в будущем — это правки в
одном файле, а не поиск всех "if android" по проекту.

Все функции ниже намеренно defensive: они не должны бросать исключения
ни на Windows, ни на Android, ни при запуске обычных unit-тестов на
linux/mac (например, в CI) — в последнем случае считаем платформу
"desktop, не Android, не обязательно Windows".
"""
import os
import sys


def is_android():
    """True, если процесс запущен под Android (python-for-android /
    Buildozer runtime). Использует несколько независимых признаков —
    ни один из них по отдельности не гарантирован во всех сборках
    p4a/Buildozer, поэтому проверяем совокупность.
    """
    # python-for-android выставляет ANDROID_ARGUMENT/ANDROID_PRIVATE для
    # всех приложений, собранных через Buildozer/p4a.
    if os.environ.get("ANDROID_ARGUMENT") is not None:
        return True
    if os.environ.get("ANDROID_PRIVATE") is not None:
        return True
    # Модуль "android", предоставляемый python-for-android рантаймом,
    # доступен только внутри реально запущенного APK.
    try:
        import android  # noqa: F401
        return True
    except ImportError:
        pass
    # Общесистемные признаки Android как ОС (запасной вариант).
    if os.environ.get("ANDROID_ROOT") is not None:
        return True
    if os.environ.get("ANDROID_DATA") is not None:
        return True
    if hasattr(sys, "getandroidapilevel"):
        return True
    return False


def is_windows():
    """True для настоящего desktop-Windows (никогда True вместе с
    is_android(), даже если бы там встретилась строка 'win' в имени
    платформы — на Android sys.platform обычно 'linux')."""
    return sys.platform.startswith("win") and not is_android()


def is_desktop():
    """True для любой НЕ-Android платформы (Windows/Linux/macOS)."""
    return not is_android()


def get_app_writable_dir(app_name="HPBattleChess"):
    """
    Возвращает app-private writable-директорию, если она ИМЕЕТ смысл на
    текущей платформе, иначе None (в этом случае вызывающий код должен
    воспользоваться своим desktop-путём по умолчанию, см. config.py).

    Desktop (Windows/Linux/macOS): возвращает None — вызывающая сторона
    (config.py) сохраняет текущее поведение "писать рядом с программой",
    ничего не меняем, чтобы не сломать Windows-версию.

    Android: APK — read-only каталог, попытка писать внутрь него падает
    или молча ничего не сохраняет. Вместо этого используем
    app-private writable storage, который платформа гарантированно
    выделяет самому приложению и который переживает перезапуски (хотя
    и удаляется при удалении приложения — это ожидаемо и нормально для
    настроек/локальных сохранений).
    """
    if not is_android():
        return None

    path = None
    # Штатный API python-for-android для app-private storage.
    try:
        from android.storage import app_storage_path  # type: ignore
        path = app_storage_path()
    except Exception:
        path = None

    if not path:
        # Запасные варианты, если android.storage недоступен в данной
        # сборке рантайма (разные версии p4a).
        path = os.environ.get("ANDROID_PRIVATE") or os.environ.get("ANDROID_APP_PATH")

    if not path:
        # Последний защитный fallback — HOME точно доступен для записи
        # внутри sandbox-приложения на Android (обычно указывает в
        # app-private область), это лучше, чем упасть или писать в APK.
        path = os.path.join(os.path.expanduser("~"), ".hp_chess")

    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path
