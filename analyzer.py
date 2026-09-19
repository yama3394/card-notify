"""取引データの集計（日次・週次・月次・予算・トレンド）。

種別（smcc/jcb/cash 等）は parsers のレジストリから取得するため、パーサを
register するだけで集計結果に新しい種別が現れる。
"""
import calendar
from datetime import date, datetime, timedelta, timezone

import parsers
from storage import load_history

JST = timezone(timedelta(hours=9))


def today_jst() -> date:
    return datetime.now(JST).date()


def is_jpy(t: dict) -> bool:
    """日本円取引か。currency 欠落は後方互換で JPY とみなす。"""
    return t.get("currency", "JPY") == "JPY"


def load_transactions() -> list[dict]:
    return load_history().get("transactions", [])


def _sum_by_date(transactions: list[dict], target_date: date) -> dict:
    ds = target_date.strftime("%Y-%m-%d")
    # 全種別を0埋めしておく（未使用の種別も画面・通知に0円で並べるため）
    by_type: dict[str, int] = {k: 0 for k in parsers.type_keys()}
    for t in transactions:
        if t.get("date") != ds or not is_jpy(t):
            continue
        by_type[t["type"]] = by_type.get(t["type"], 0) + t["amount"]
    return {"by_type": by_type, "total": sum(by_type.values())}


def _foreign_by_date(transactions: list[dict], target_date: date) -> dict:
    ds = target_date.strftime("%Y-%m-%d")
    totals: dict[str, int | float] = {}
    for t in transactions:
        if t.get("date") != ds or is_jpy(t):
            continue
        cur = t.get("currency", "JPY")
        totals[cur] = totals.get(cur, 0) + t["amount"]
    return totals


def _sum_range(transactions: list[dict], start: date, end: date) -> int:
    total = 0
    for t in transactions:
        if not is_jpy(t):
            continue
        try:
            d = date.fromisoformat(t["date"])
        except (ValueError, KeyError):
            continue
        if start <= d <= end:
            total += t["amount"]
    return total


def analyze_week(target_week: date | None = None) -> dict:
    today = today_jst()
    if target_week is None:
        target_week = today

    week_start = target_week - timedelta(days=target_week.weekday())
    transactions = load_transactions()

    days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        data = _sum_by_date(transactions, d)
        days.append({"date": d, "is_today": d == today, "is_future": d > today, **data})

    week_total = sum(d["total"] for d in days)
    prev_week_start = week_start - timedelta(days=7)
    prev_week_total = _sum_range(transactions, prev_week_start, prev_week_start + timedelta(days=6))

    # 進行中の週は「今週N日分 vs 先週7日分」の比較になり誤解を招くため、
    # 先週の同じ経過日数までの小計も返す（画面側で「先週同時点比」に使う）。
    is_current_week = week_start <= today <= week_start + timedelta(days=6)
    prev_week_to_date_total = None
    if is_current_week:
        prev_week_to_date_total = _sum_range(
            transactions, prev_week_start, prev_week_start + (today - week_start)
        )

    return {
        "week_start": week_start,
        "days": days,
        "week_total": week_total,
        "prev_week_total": prev_week_total,
        "is_current_week": is_current_week,
        "prev_week_to_date_total": prev_week_to_date_total,
    }


def analyze(target: date | None = None) -> dict:
    today = today_jst()
    yesterday = today - timedelta(days=1)
    if target is None:
        target = yesterday

    transactions = load_transactions()
    yesterday_data = _sum_by_date(transactions, target)

    week_start = today - timedelta(days=today.weekday())
    week_total = _sum_range(transactions, week_start, today)

    month_start = today.replace(day=1)
    month_total = _sum_range(transactions, month_start, today)

    same_day_last_week = target - timedelta(days=7)
    last_week_total = _sum_by_date(transactions, same_day_last_week)["total"]

    diff_amount = yesterday_data["total"] - last_week_total
    diff_rate = (diff_amount / last_week_total * 100) if last_week_total else None

    # 先週（月〜日）・先月（1日〜末日）の確定済み合計。追加登録（過去日分）が
    # それらの期間に属する場合、日次通知の末尾で「反映済みの正しい合計」として
    # 案内するために使う（notifier._build_daily_text 参照）。
    prev_week_start = week_start - timedelta(days=7)
    prev_week_end = week_start - timedelta(days=1)
    prev_week_total = _sum_range(transactions, prev_week_start, prev_week_end)

    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    prev_month_total = _sum_range(transactions, prev_month_start, prev_month_end)

    return {
        "date": target,
        "yesterday": yesterday_data,
        "week_start": week_start,
        "week_total": week_total,
        "month_start": month_start,
        "month_total": month_total,
        "last_week_same_day": last_week_total,
        "diff": {"amount": diff_amount, "rate": diff_rate},
        "foreign": _foreign_by_date(transactions, target),
        "prev_week_total": prev_week_total,
        "prev_month_total": prev_month_total,
    }


def monthly_trend(months: int = 6) -> list[dict]:
    today = today_jst()
    transactions = load_transactions()
    result = []
    y, m = today.year, today.month
    for _ in range(months):
        last_day = calendar.monthrange(y, m)[1]
        start, end = date(y, m, 1), date(y, m, last_day)
        by_type: dict[str, int] = {k: 0 for k in parsers.type_keys()}
        for t in transactions:
            if not is_jpy(t):
                continue
            try:
                d = date.fromisoformat(t["date"])
            except (ValueError, KeyError):
                continue
            if not (start <= d <= end):
                continue
            by_type[t["type"]] = by_type.get(t["type"], 0) + t["amount"]
        result.append({
            "year": y, "month": m, "label": f"{m}月",
            "by_type": by_type, "total": sum(by_type.values()),
        })
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    result.reverse()
    return result
