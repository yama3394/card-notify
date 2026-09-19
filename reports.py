"""月次・週次レポートの生成と LINE 送信。"""
import calendar
import logging
from collections import defaultdict
from datetime import date, timedelta

import notifier
import parsers
from analyzer import analyze_week, is_jpy, load_transactions, today_jst

logger = logging.getLogger(__name__)

_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


# ── 月次 ──────────────────────────────────────────────────────────────

def _month_range(year: int, month: int) -> tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def generate_monthly(year: int | None = None, month: int | None = None) -> dict:
    today = today_jst()
    year = year or today.year
    month = month or today.month

    start, end = _month_range(year, month)
    transactions = load_transactions()

    by_type: dict[str, int] = {k: 0 for k in parsers.type_keys()}
    store_totals: dict[str, int] = defaultdict(int)
    for t in transactions:
        if not is_jpy(t):
            continue
        try:
            d = date.fromisoformat(t["date"])
        except (ValueError, KeyError):
            continue
        if not (start <= d <= end):
            continue
        amount = t["amount"]
        by_type[t["type"]] = by_type.get(t["type"], 0) + amount
        store_totals[t.get("store") or "（店舗名なし）"] += amount

    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    prev_start, prev_end = _month_range(prev_year, prev_month)
    prev_total = 0
    for t in transactions:
        if not is_jpy(t):
            continue
        try:
            d = date.fromisoformat(t["date"])
        except (ValueError, KeyError):
            continue
        if prev_start <= d <= prev_end:
            prev_total += t["amount"]

    return {
        "year": year, "month": month,
        "by_type": by_type, "total": sum(by_type.values()),
        "top_stores": sorted(store_totals.items(), key=lambda x: x[1], reverse=True)[:10],
        "prev_total": prev_total,
    }


def send_monthly_report(year: int | None = None, month: int | None = None) -> None:
    r = generate_monthly(year, month)
    diff = r["total"] - r["prev_total"]
    if r["prev_total"]:
        diff_rate = diff / r["prev_total"] * 100
        # 減少時も金額側に "-" を付ける（%側は diff_rate 自身が負号を持つため付けない）
        amount_sign = "+" if diff >= 0 else "-"
        rate_sign = "+" if diff_rate >= 0 else ""
        diff_line = f"先月比  {amount_sign}¥{abs(diff):,} ({rate_sign}{diff_rate:.1f}%)"
    else:
        diff_line = "先月比  (先月データなし)"

    store_lines = [
        f"  {notifier.sanitize(store) or store}  ¥{amount:,}"
        for store, amount in r["top_stores"]
    ]
    # 種別ごとの内訳行はレジストリから動的生成（0円の種別も表示する）
    labels = parsers.type_labels()
    type_lines = [
        f"{labels.get(key, key)}  ¥{r['by_type'].get(key, 0):,}"
        for key in parsers.type_keys()
    ]
    lines = [
        f"{r['year']}年{r['month']}月 月次まとめ",
        f"¥{r['total']:,}",
        "",
        *type_lines,
        "",
        "店舗別 TOP10",
        *store_lines,
        "",
        diff_line,
        f"先月  ¥{r['prev_total']:,}",
    ]
    notifier.push("\n".join(lines))
    logger.info(f"月次レポート送信完了: {r['year']}/{r['month']}")


# ── 週次 ──────────────────────────────────────────────────────────────

def generate_weekly(week_start: date | None = None) -> dict:
    if week_start is None:
        monday = today_jst() - timedelta(days=today_jst().weekday())
        week_start = monday - timedelta(days=7)
    return analyze_week(week_start)


def send_weekly_report(week_start: date | None = None) -> None:
    r = generate_weekly(week_start)
    ws = r["week_start"]
    we = ws + timedelta(days=6)

    diff = r["week_total"] - r["prev_week_total"]
    if r["prev_week_total"]:
        diff_rate = diff / r["prev_week_total"] * 100
        # 減少時も金額側に "-" を付ける（%側は diff_rate 自身が負号を持つため付けない）
        amount_sign = "+" if diff >= 0 else "-"
        rate_sign = "+" if diff_rate >= 0 else ""
        diff_line = f"先週比  {amount_sign}¥{abs(diff):,} ({rate_sign}{diff_rate:.1f}%)"
    else:
        diff_line = "先週比  (先週データなし)"

    day_lines = [
        f"  {d['date'].month}/{d['date'].day}({_WEEKDAYS[d['date'].weekday()]})  ¥{d['total']:,}"
        for d in r["days"] if d["total"]
    ]
    lines = [
        f"{ws.month}/{ws.day}〜{we.month}/{we.day} 週次まとめ",
        f"¥{r['week_total']:,}",
        "",
        *day_lines,
        "",
        diff_line,
    ]
    notifier.push("\n".join(lines))
    logger.info(f"週次レポート送信完了: {ws}")
