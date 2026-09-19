"""日次通知の「追加登録（過去日分）」判定を一箇所に集約する。

次に送信される日次通知が対象にする日付より過去の取引は、その通知の内訳には
二度と現れず week_total/month_total にだけ黙って加算されてしまう
（analyzer.analyze は target ではなく実行時点までを毎回通しで再計算するため）。
gmail_fetcher（メール受信遅延）と manual_entry（手入力の過去日付指定）の
両方がこの基準で late arrival を判定する。判定基準を一箇所にまとめることで、
どちらか一方だけ直して他方が取り残される（gmail 側は固定2日しきい値のまま、
というような）事態を防ぐ。
"""
from datetime import date, timedelta


def next_daily_target(today: date, last_daily: str | None) -> date:
    """次に送信される日次通知が対象にする日付。

    今日分（対象＝前日）が送信済みなら次に届く通知の対象は「今日」、
    まだ未送信ならその通知が対象にする「前日」。
    """
    notified_today = last_daily == today.strftime("%Y-%m-%d")
    return today if notified_today else today - timedelta(days=1)


def is_late(entry_date: date, today: date, last_daily: str | None) -> bool:
    """entry_date が、もうどの日次通知の内訳にも入れないタイミングなら True。"""
    return entry_date < next_daily_target(today, last_daily)
