"""history.json への同時安全なアクセス。

読み取りは共有ロック、更新は排他ロック。更新系は必ず update_history() を使う
こと。load_history()→save_history() を別々に呼ぶとロックが切れた隙に他プロセス
/他スレッドの更新を取りこぼす（lost-update）ため。
"""
import fcntl
import json

import config


def _empty() -> dict:
    return {"transactions": [], "skipped_ids": [], "skipped": []}


def load_history() -> dict:
    if not config.HISTORY_FILE.exists():
        return _empty()
    with config.HISTORY_FILE.open(encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            data = json.load(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    data.setdefault("transactions", [])
    data.setdefault("skipped_ids", [])
    data.setdefault("skipped", [])
    return data


def save_history(data: dict) -> None:
    config.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with config.HISTORY_FILE.open("a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
            # Python のバッファに残ったままロックを解放すると、flush(close時)前に
            # 他スレッド/他プロセスが古い内容を読み lost-update になるため、
            # 必ずロック保持中に OS へ書き切る。
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def update_history(mutator) -> None:
    """排他ロック下で read-modify-write を1トランザクションとして実行する。

    mutator(data) は data(dict) をその場で変更する。
    """
    config.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with config.HISTORY_FILE.open("a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            content = f.read()
            data = json.loads(content) if content.strip() else {}
            data.setdefault("transactions", [])
            data.setdefault("skipped_ids", [])
            data.setdefault("skipped", [])
            mutator(data)
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()  # ロック解放前に OS へ書き切る（save_history と同じ理由）
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
