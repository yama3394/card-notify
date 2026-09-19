"""データマイグレーション基盤。

アプリ更新に伴う history.json 等のデータ形式変更へ自動追随するための、スキーマ
バージョン管理＋順次適用ランナー。配信方式（cron / スケジューラ / 手動）に依存
しない汎用部品。

版数は config.SCHEMA_VERSION_FILE（JSON `{"version": N}`）で管理する。MIGRATIONS
に (version, name, func) を昇順で登録し、run() が current_version より新しいものを
順に適用する。各 func は引数なしで副作用的にデータを移行する（storage.update_history
経由で排他ロック下の read-modify-write を行うこと）。冪等なので再実行しても二重
適用しない。
"""
import json
import logging
import os

import config
import storage

logger = logging.getLogger(__name__)


def current_version() -> int:
    """現在のスキーマ版数。ファイルが無い/壊れていても例外安全に 0 を返す。"""
    try:
        data = json.loads(config.SCHEMA_VERSION_FILE.read_text(encoding="utf-8"))
        return int(data["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _set_version(n: int) -> None:
    """版数をアトミックに保存する（tmp へ書き出し→os.replace）。"""
    path = config.SCHEMA_VERSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": int(n)}), encoding="utf-8")
    os.replace(tmp, path)


# ── マイグレーション本体 ────────────────────────────────────────────────
def m1_add_currency() -> None:
    """全 transaction に currency 欠落があれば 'JPY' を補完する（冪等）。"""
    def mutator(data: dict) -> None:
        for t in data.get("transactions", []):
            if isinstance(t, dict):
                t.setdefault("currency", "JPY")

    storage.update_history(mutator)


def m2_add_transaction_numbers() -> None:
    """全 transaction に連番 ID（"no"）を登録順で振る（冪等）。

    採番そのものは storage.update_history が書き込みのたびに行う（"no" の無い
    取引に振る）ので、ここでは空の更新を1回走らせるだけ。更新直後に誰も登録
    しなくても、WebUI や CSV に ID が出るようにするために行う。
    """
    storage.update_history(lambda data: None)


# (version:int, name:str, func:callable) を昇順で登録する。
MIGRATIONS = [
    (1, "add_currency", m1_add_currency),
    (2, "add_transaction_numbers", m2_add_transaction_numbers),
]


def latest_version() -> int:
    """MIGRATIONS の最大 version。登録が無ければ 0。"""
    return max((version for version, _name, _func in MIGRATIONS), default=0)


def run() -> list[str]:
    """current_version より新しいマイグレーションを昇順に適用する。

    各適用後に版数を更新するので、途中で失敗しても適用済みの分は保持される
    （再実行で続きから安全に再開できる）。適用したものの "v{n}: {name}" のリスト
    を返す。適用対象が無ければ空リスト。
    """
    cur = current_version()
    applied: list[str] = []
    for version, name, func in sorted(MIGRATIONS, key=lambda m: m[0]):
        if version <= cur:
            continue
        logger.info("マイグレーション適用: v%d: %s", version, name)
        func()
        _set_version(version)
        cur = version
        applied.append(f"v{version}: {name}")

    if not applied:
        logger.info("マイグレーションは最新です (version=%d)", cur)
    else:
        logger.info("マイグレーション完了: %s", ", ".join(applied))
    return applied


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    results = run()
    if results:
        print("適用したマイグレーション:")
        for line in results:
            print(f"  - {line}")
    else:
        print(f"最新です (version={current_version()})")
