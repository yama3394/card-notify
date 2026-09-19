"""history.json への同時安全なアクセス。

読み取りは共有ロック、更新は排他ロック。更新系は必ず update_history() を使う
こと。load_history()→save_history() を別々に呼ぶとロックが切れた隙に他プロセス
/他スレッドの更新を取りこぼす（lost-update）ため。
"""
import fcntl
import json
from datetime import datetime, timedelta, timezone

import config

JST = timezone(timedelta(hours=9))


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
            apply_update(data, mutator)
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()  # ロック解放前に OS へ書き切る（save_history と同じ理由）
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def apply_update(data: dict, mutator) -> None:
    """update_history のロック内で行う処理本体（history.json を差し替えるテストスタブからも使う）。

    取引の連番 ID（"no"）はここで一括して振る。登録経路（現金の手入力・メール
    取込・/errors の手動登録）ごとに採番すると、新しい経路を足したときに
    付け忘れが起きるため。
    """
    data.setdefault("transactions", [])
    data.setdefault("skipped_ids", [])
    data.setdefault("skipped", [])
    _backfill_numbers(data)
    mutator(data)
    # mutator が追加した取引（"no" 無し）にだけ連番と登録時刻を付ける
    now = _now_jst().isoformat(timespec="seconds")
    for t in data["transactions"]:
        if "no" not in t:
            _assign_no(data, t)
            t["created_at"] = now


def _now_jst() -> datetime:
    return datetime.now(JST)


def _assign_no(data: dict, t: dict) -> None:
    # 削除しても番号は再利用しない（DB の AUTOINCREMENT と同じ）。「#101 取消」
    # のように番号で指定する操作が、削除後に別の取引を指してしまわないように。
    t["no"] = data["next_no"]
    data["next_no"] += 1


def _backfill_numbers(data: dict) -> None:
    """"no" を持たない既存取引に、リスト順（＝登録順）で連番を振る（v1.2.0 より前のデータの移行）。

    created_at は付けない。送信時刻の分からない取引に「今」を入れると、
    当日送信分に限る LINE の取消で過去の取引まで消せてしまう。
    """
    if "next_no" not in data:
        data["next_no"] = max((t.get("no", 0) for t in data["transactions"]), default=0) + 1
    for t in data["transactions"]:
        if "no" not in t:
            _assign_no(data, t)
