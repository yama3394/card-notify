"""通知時刻の参照・変更（settings.json ベース）。

通知は cron ではなくアプリ内スケジューラ（scheduler.py）が実行する。
通知時刻は config.SETTINGS_FILE（data/settings.json）に保存し、WebUI から変更する。
"""
import json
import os
from pathlib import Path

import config


def _load_settings() -> dict:
    """settings.json を読み込む。存在しない/壊れている場合は空 dict。"""
    try:
        data = json.loads(Path(config.SETTINGS_FILE).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def get_notify_time() -> tuple[int, int]:
    """通知時刻 (hour, minute) を返す。未設定/不正なら既定値。"""
    settings = _load_settings()
    hour = settings.get("notify_hour")
    minute = settings.get("notify_minute")
    try:
        hour = int(hour)
        minute = int(minute)
    except (TypeError, ValueError):
        return (config.DEFAULT_NOTIFY_HOUR, config.DEFAULT_NOTIFY_MINUTE)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return (config.DEFAULT_NOTIFY_HOUR, config.DEFAULT_NOTIFY_MINUTE)
    return (hour, minute)


def set_notify_time(hour: int, minute: int) -> None:
    """通知時刻を settings.json にアトミックに保存する。既存の他キーは保持。"""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("時刻が範囲外です（時:0-23 分:0-59）")

    settings = _load_settings()
    settings["notify_hour"] = int(hour)
    settings["notify_minute"] = int(minute)

    path = Path(config.SETTINGS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(settings, ensure_ascii=False, indent=2))
    os.replace(tmp, path)
