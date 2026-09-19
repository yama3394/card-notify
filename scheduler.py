"""アプリ内スケジューラ（cron 不要）。

バックグラウンドの daemon スレッドで約 45〜60 秒ごとに tick し、JST の現在時刻が
設定された通知時刻（schedule.get_notify_time()）以降で、かつ本日まだ日次ジョブが
完了していなければ main.run() を実行する。完了済みかどうかは notify_state.json の
`last_scheduled_run`（YYYY-MM-DD）で判定する。

main.run() は「その日に送るべき通知がすべて送信済みになったか」を bool で返す。
True のときだけ `last_scheduled_run` を記録し、False（Gmail 取得失敗や送信失敗）
のときは記録せず、`last_attempt_ts`（unixtime）によるクールダウンを挟んで
次以降の tick で再試行する。

これにより:
  - 1 日 1 回、設定時刻以降の最初の tick で実行される。
  - サーバ停止で設定時刻の tick を取りこぼしても、同日中に復帰すればキャッチアップする。
  - 失敗した日次ジョブは _RETRY_COOLDOWN_SECONDS（15分）間隔で同日中に自動リトライする
    （main.run() 側の送信済み記録により通知が二重に飛ぶことはない）。
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import config
import schedule
import state as _state

logger = logging.getLogger("scheduler")

JST = timezone(timedelta(hours=9))

_TICK_SECONDS = 50

# 日次ジョブが完了しなかった（main.run() が False / 例外）ときの再試行間隔。
_RETRY_COOLDOWN_SECONDS = 15 * 60

# 多重起動ガード（モジュールレベル）。
_started = False
_lock = threading.Lock()


def _should_run(
    now_hm: tuple[int, int],
    sched_hm: tuple[int, int],
    last_run_date: str | None,
    today: str,
    last_attempt_ts: float | None = None,
    now_ts: float = 0.0,
) -> bool:
    """日次ジョブを今 tick で実行すべきかの純粋判定。

    now_hm    : 現在の (hour, minute) JST
    sched_hm  : 設定された通知時刻 (hour, minute)
    last_run_date: state の last_scheduled_run（未完了なら None/空）
    today     : 本日の日付文字列 YYYY-MM-DD
    last_attempt_ts: state の last_attempt_ts（前回試行の unixtime。未試行なら None）
    now_ts    : 現在の unixtime（クールダウン判定用）
    """
    if last_run_date == today:
        return False
    if now_hm < sched_hm:
        return False
    # 前回試行が失敗していた場合の再試行はクールダウンを挟む。
    if last_attempt_ts is not None and now_ts - last_attempt_ts < _RETRY_COOLDOWN_SECONDS:
        return False
    return True


def _run_daily_job() -> None:
    """日次ジョブを実行し、完了したら本日実行済みとして記録する。

    未完了（main.run() が False）なら記録せず、クールダウン後の tick で
    再試行される。例外はここでは投げない（ループを止めない）。
    """
    import main

    today = datetime.now(JST).strftime("%Y-%m-%d")
    logger.info("日次ジョブ開始 (%s)", today)

    def _mark_attempt(s: dict) -> None:
        s["last_attempt_ts"] = time.time()

    def _mark_done(s: dict) -> None:
        s["last_scheduled_run"] = today

    # 試行時刻を先に記録し、失敗（例外含む）時の再試行をクールダウンさせる。
    _state.update(_mark_attempt)
    if main.run():
        _state.update(_mark_done)
        logger.info("日次ジョブ完了 (%s)", today)
    else:
        logger.warning(
            "日次ジョブが完了しませんでした。%s秒後以降の tick で再試行します (%s)",
            _RETRY_COOLDOWN_SECONDS, today,
        )


def _loop() -> None:
    while True:
        try:
            now = datetime.now(JST)
            now_hm = (now.hour, now.minute)
            today = now.strftime("%Y-%m-%d")
            sched_hm = schedule.get_notify_time()
            s = _state.load()
            last_run = s.get("last_scheduled_run")
            last_attempt = s.get("last_attempt_ts")
            if _should_run(now_hm, sched_hm, last_run, today, last_attempt, time.time()):
                _run_daily_job()
        except Exception:  # noqa: BLE001 - 常駐を死なせない
            logger.exception("スケジューラ tick で例外が発生しました")
        time.sleep(_TICK_SECONDS)


def start() -> None:
    """スケジューラスレッドを起動する（多重起動しない）。

    config.ENABLE_SCHEDULER が False なら何もしない。secrets は要求しない
    （実際のジョブ実行時に main.run() が require_secrets する）。
    """
    global _started
    if not config.ENABLE_SCHEDULER:
        logger.info("ENABLE_SCHEDULER=False のためスケジューラを起動しません")
        return
    with _lock:
        if _started:
            return
        _started = True
        thread = threading.Thread(target=_loop, name="card-notify-scheduler", daemon=True)
        thread.start()
        logger.info("スケジューラを起動しました (tick=%ss)", _TICK_SECONDS)
