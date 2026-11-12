"""history.json への同時安全なアクセス。

更新は排他ロック下で行い、書き込みは一時ファイル→os.replace のアトミック置換。
更新系は必ず update_history() を使うこと。load_history()→save_history() を
別々に呼ぶとロックが切れた隙に他プロセス/他スレッドの更新を取りこぼす
（lost-update）ため。
"""
import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import config

JST = timezone(timedelta(hours=9))


def _empty() -> dict:
    return {"transactions": [], "skipped_ids": [], "skipped": []}


@contextmanager
def _exclusive_lock():
    """更新の排他ロック。

    history.json 自体ではなく隣の .lock ファイルに掛ける。書き込みを os.replace
    で行うと history.json の実体（inode）が毎回入れ替わり、ファイル自体への
    flock では待っていた側が古い実体をロックしてしまうため。
    """
    config.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = config.HISTORY_FILE.with_name(config.HISTORY_FILE.name + ".lock")
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _write_atomic(data: dict) -> None:
    """history.json をアトミックに書き換える（_exclusive_lock 保持中に呼ぶ）。

    その場で truncate して書くと、書き込み途中のクラッシュやディスクフルで
    全履歴が壊れる。一時ファイルへ書き切ってから os.replace で差し替えれば、
    失敗しても元のファイルはそのまま残り、読み手は常に完全な内容だけを見る。
    """
    path = config.HISTORY_FILE
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_history() -> dict:
    # 書き込みがアトミック置換なので、読み取りはロック不要（常に完全な内容が見える）
    try:
        content = config.HISTORY_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    data = json.loads(content)
    data.setdefault("transactions", [])
    data.setdefault("skipped_ids", [])
    data.setdefault("skipped", [])
    return data


def save_history(data: dict) -> None:
    with _exclusive_lock():
        _write_atomic(data)


def update_history(mutator) -> None:
    """排他ロック下で read-modify-write を1トランザクションとして実行する。

    mutator(data) は data(dict) をその場で変更する。
    """
    with _exclusive_lock():
        try:
            content = config.HISTORY_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        data = json.loads(content) if content.strip() else {}
        apply_update(data, mutator)
        _write_atomic(data)


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
