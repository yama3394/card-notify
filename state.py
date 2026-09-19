"""通知の実行状態（最終取得日・最終日次通知日）の永続化。

更新は必ず update() を使うこと。load()→save() を別々に呼ぶとロックが切れた隙に
他プロセス/他スレッドの更新を取りこぼす（storage.update_history と同じ理由）。
"""
import fcntl
import json
import os

import config


def load() -> dict:
    if not config.STATE_FILE.exists():
        return {}
    with config.STATE_FILE.open(encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return json.load(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def save(data: dict) -> None:
    config.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 'a+' は切り詰めずに開く。排他ロック取得後に truncate することで
    # 他プロセスがロック保持中にファイルが空になるレースを防ぐ。
    with config.STATE_FILE.open("a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
            # Python のバッファに残ったままロックを解放すると、flush(close時)前に
            # 他スレッド/他プロセスが古い内容を読み lost-update になるため、
            # 必ずロック保持中に OS へ書き切る。
            f.flush()
            # late_arrivals に店舗名・金額を含むため他ユーザから読めないようにする。
            os.chmod(config.STATE_FILE, 0o600)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def update(mutator) -> dict:
    """排他ロック下で read-modify-write を1トランザクションとして実行する。

    mutator(data) は data(dict) をその場で変更する。変更後の dict を返す。
    """
    config.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with config.STATE_FILE.open("a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            content = f.read()
            data = json.loads(content) if content.strip() else {}
            if not isinstance(data, dict):
                data = {}
            mutator(data)
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()  # ロック解放前に OS へ書き切る（save と同じ理由）
            os.chmod(config.STATE_FILE, 0o600)  # late_arrivals に店舗名・金額を含むため
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return data
