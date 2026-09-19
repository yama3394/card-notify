"""cron やアプリ内スケジューラから日次で叩くエントリポイント。

1) Gmail からメール取得 → 2) 集計 → 3) 日次通知 → 4) 月初なら月次 → 5) 月曜なら週次。

run() は「その日に送るべき通知がすべて送信済みになったか」を bool で返す。
Gmail 取得に失敗した場合は不完全なデータで通知せず False を返し、呼び出し側
（scheduler）の再試行に委ねる。日次・月次・週次はそれぞれ state に送信済みを
記録して重複送信を防ぐため、再試行しても同じ通知が二重に飛ぶことはない。
"""
import logging
import sys
from datetime import datetime, timedelta, timezone

import state as _state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("main")

JST = timezone(timedelta(hours=9))

# 境界日にサーバーが停止していた場合のキャッチアップ窓。
# 月次は月初3日以内、週次は月・火曜のあいだ未送信なら送る。
_MONTHLY_CATCHUP_DAYS = 3
_WEEKLY_CATCHUP_WEEKDAYS = (0, 1)  # 月・火


def _prev_month_key(today) -> tuple[str, int, int]:
    """前月の (キー "YYYY-MM", 年, 月) を返す。"""
    prev_month = today.month - 1 if today.month > 1 else 12
    prev_year = today.year if today.month > 1 else today.year - 1
    return f"{prev_year}-{prev_month:02d}", prev_year, prev_month


def _week_key(today) -> str:
    """今週月曜の日付文字列（週次送信済みマークのキー）。"""
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%Y-%m-%d")


def _init_period_marks(today) -> None:
    """月次・週次の送信済みマークが無ければ「送信済み」で初期化する。

    導入直後（または旧版からの更新直後）は取得済みデータが浅く、境界日に
    部分データのレポートを送ってしまうのを防ぐ。以降の境界からは通常送信。
    """
    month_key, _, _ = _prev_month_key(today)
    week_key = _week_key(today)

    def _init(s: dict) -> None:
        s.setdefault("last_monthly", month_key)
        s.setdefault("last_weekly", week_key)

    _state.update(_init)


def _already_notified_today() -> bool:
    today = datetime.now(JST).strftime("%Y-%m-%d")
    return _state.load().get("last_daily") == today


def _mark_notified() -> None:
    _mark_state("last_daily", datetime.now(JST).strftime("%Y-%m-%d"))


def _mark_state(key: str, value: str) -> None:
    """state に送信済み記録を書き込む（重複送信防止用）。"""
    def _set(s: dict) -> None:
        s[key] = value

    _state.update(_set)


def _pop_late_arrivals() -> list[dict]:
    """late_arrivals を読み取りと同時に空にする（1トランザクション）。

    読み取りとクリアを別々に行うと、その間（送信のネットワーク往復も含む）に
    gmail_fetcher / manual_entry が新たに積んだ分が、クリアで一緒に消えて
    しまう（lost-update）。送信に失敗した場合は _restore_late_arrivals で
    書き戻す。
    """
    captured: dict = {}

    def _pop(s: dict) -> None:
        captured["late_arrivals"] = s.get("late_arrivals") or []
        s["late_arrivals"] = []

    _state.update(_pop)
    return captured["late_arrivals"]


def _restore_late_arrivals(items: list[dict]) -> None:
    def _restore(s: dict) -> None:
        s.setdefault("late_arrivals", []).extend(items)

    _state.update(_restore)


_PENDING_NOTICE_KEYS = ("late_arrivals", "cancellations")


def _pop_pending_notices() -> dict[str, list[dict]]:
    """日次通知に載せる保留分（追加登録・取消）をまとめて読み取り＋クリアする（1トランザクション）。

    _pop_late_arrivals と同じ理由で読み取りとクリアを分けない。送信に失敗したら
    _restore_pending_notices で書き戻す。
    """
    captured: dict[str, list[dict]] = {}

    def _pop(s: dict) -> None:
        for key in _PENDING_NOTICE_KEYS:
            captured[key] = s.get(key) or []
            s[key] = []

    _state.update(_pop)
    return captured


def _restore_pending_notices(notices: dict[str, list[dict]]) -> None:
    def _restore(s: dict) -> None:
        for key in _PENDING_NOTICE_KEYS:
            if notices.get(key):
                s.setdefault(key, []).extend(notices[key])

    _state.update(_restore)


def _notify_fetch_error_once(notifier, message: str) -> None:
    """Gmail 取得失敗のエラーLINE通知を1日1回に抑制する。

    scheduler が短い間隔で再試行するため、毎回通知するとスパム化する。
    """
    today = datetime.now(JST).strftime("%Y-%m-%d")
    if _state.load().get("last_fetch_error_date") == today:
        logger.info("本日分の取得失敗通知は送信済みのため通知をスキップ")
        return
    notifier.notify_error(message)
    _mark_state("last_fetch_error_date", today)


def run() -> bool:
    """日次処理を実行し、その日に送るべき通知がすべて送信済みなら True を返す。

    False の場合、呼び出し側（scheduler）は本日実行済みとして記録せず、
    クールダウン後に再試行する。
    """
    import config
    import gmail_fetcher
    import analyzer
    import notifier
    import reports

    config.require_secrets()

    logger.info("Gmail メール取得開始")
    try:
        count = gmail_fetcher.fetch_all()
        logger.info(f"取得完了: {count}件")
    except Exception as e:
        logger.error(f"Gmail 取得失敗: {e}")
        _notify_fetch_error_once(notifier, f"Gmail 取得失敗: {e}")
        # 不完全なデータで通知しない（日次・月次・週次には進まず再試行に委ねる）
        return False

    logger.info("集計開始")
    try:
        data = analyzer.analyze()
    except Exception as e:
        logger.error(f"集計失敗: {e}")
        notifier.notify_error(f"集計失敗: {e}")
        return False

    ok = True

    if _already_notified_today():
        logger.info("本日の日次通知は送信済みのためスキップ")
    else:
        logger.info("日次通知送信")
        # メール遅延・手入力の過去日付登録（gmail_fetcher / manual_entry が積む）と
        # LINE の「取消」で消した通知済みの日の取引（manual_entry が積む）を今回の通知に載せる
        notices = _pop_pending_notices()
        try:
            notifier.send_daily_report(data, notices["late_arrivals"], notices["cancellations"])
            _mark_notified()
        except Exception as e:
            logger.error(f"日次通知失敗: {e}")
            notifier.notify_error(f"日次通知失敗: {e}")
            _restore_pending_notices(notices)
            ok = False

    today = datetime.now(JST).date()
    _init_period_marks(today)

    if today.day <= _MONTHLY_CATCHUP_DAYS:
        month_key, prev_year, prev_month = _prev_month_key(today)
        if _state.load().get("last_monthly") == month_key:
            logger.info("月次レポートは送信済みのためスキップ")
        else:
            logger.info("月次レポート送信")
            try:
                reports.send_monthly_report(prev_year, prev_month)
                _mark_state("last_monthly", month_key)
            except Exception as e:
                logger.error(f"月次レポート失敗: {e}")
                notifier.notify_error(f"月次レポート失敗: {e}")
                ok = False

    if today.weekday() in _WEEKLY_CATCHUP_WEEKDAYS:
        week_key = _week_key(today)  # 月・火とも同じ「今週月曜」のキー
        if _state.load().get("last_weekly") == week_key:
            logger.info("週次レポートは送信済みのためスキップ")
        else:
            logger.info("週次レポート送信")
            try:
                reports.send_weekly_report()
                _mark_state("last_weekly", week_key)
            except Exception as e:
                logger.error(f"週次レポート失敗: {e}")
                notifier.notify_error(f"週次レポート失敗: {e}")
                ok = False

    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
