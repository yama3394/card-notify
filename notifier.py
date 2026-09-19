"""LINE Messaging API への通知送信。"""
import logging
import re
from datetime import date

import requests

import config
import parsers

logger = logging.getLogger(__name__)

_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
_PUSH_URL = "https://api.line.me/v2/bot/message/push"
_URL_PATTERN = re.compile(r"\.(?:com|net|org|jp|co\.jp|io|uk|app|dev)\b", re.IGNORECASE)


def sanitize(name: str | None) -> str | None:
    """店舗名中の URL 風文字列を無害化（LINE 側でのリンク化を防ぐ）。"""
    if name and _URL_PATTERN.search(name):
        return _URL_PATTERN.sub(lambda m: "．" + m.group()[1:], name)
    return name


# LINE のテキストメッセージ上限は5000字。余裕を持って分割する。
_MAX_TEXT_LEN = 4900
# 1回の push API 呼び出しに載せられるメッセージ数の上限。
_MAX_MESSAGES_PER_PUSH = 5


def _split_text(text: str) -> list[str]:
    """上限超のテキストを改行境界優先で _MAX_TEXT_LEN 以下の断片に分割する。"""
    if len(text) <= _MAX_TEXT_LEN:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        # 1行だけで上限を超える場合は行内で強制分割する。
        while len(line) > _MAX_TEXT_LEN:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:_MAX_TEXT_LEN])
            line = line[_MAX_TEXT_LEN:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > _MAX_TEXT_LEN:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def push(text: str) -> None:
    if not config.LINE_USER_ID:
        logger.warning("LINE_USER_ID が未設定です。Botにメッセージを送って登録してください。")
        return
    # 5000字超（取引の多い月次レポート等）は分割し、5通ずつまとめて送る。
    chunks = _split_text(text)
    for i in range(0, len(chunks), _MAX_MESSAGES_PER_PUSH):
        batch = chunks[i:i + _MAX_MESSAGES_PER_PUSH]
        resp = requests.post(
            _PUSH_URL,
            headers={
                "Authorization": f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "to": config.LINE_USER_ID,
                "messages": [{"type": "text", "text": t} for t in batch],
            },
            timeout=10,
        )
        resp.raise_for_status()
    logger.info("LINE push 送信成功")


def notify_error(message: str) -> None:
    try:
        push(f"エラー: {message}")
    except Exception as e:
        logger.error(f"エラー通知失敗: {e}")


def _fmt_foreign(v: int | float) -> str:
    if isinstance(v, float) and v != int(v):
        return f"{v:,.2f}"
    return f"{int(v):,}"


def _fmt_amount(amount: int | float, currency: str) -> str:
    if currency == "JPY":
        return f"¥{amount:,}"
    return f"{_fmt_foreign(amount)} {currency}"


def _build_daily_text(analysis: dict, late_arrivals: list[dict] | None = None) -> str:
    d = analysis["date"]
    yday = analysis["yesterday"]
    diff = analysis["diff"]
    wday = _WEEKDAYS[d.weekday()]
    date_str = f"{d.year}/{d.month:02d}/{d.day:02d} ({wday})"

    diff_amt = diff["amount"]
    diff_rate = diff["rate"]
    if diff_rate is not None:
        if diff_amt < 0:
            diff_line = f"先週比  -¥{abs(diff_amt):,} ({diff_rate:.0f}%)"
        else:
            diff_line = f"先週比  +¥{diff_amt:,} (+{diff_rate:.0f}%)"
    else:
        diff_line = "先週比  (先週データなし)"

    # 種別ごとの内訳行はレジストリから動的生成（0円の種別も表示する）
    labels = parsers.type_labels()
    by_type = yday["by_type"]
    type_lines = [
        f"{labels.get(key, key)}  ¥{by_type.get(key, 0):,}"
        for key in parsers.type_keys()
    ]

    lines = [
        date_str,
        f"¥{yday['total']:,}",
        "",
        *type_lines,
        "",
        diff_line,
        f"今週計  ¥{analysis['week_total']:,}",
        f"今月計  ¥{analysis['month_total']:,}",
    ]

    foreign = analysis.get("foreign") or {}
    if foreign:
        lines.append("")
        lines.append("外貨")
        for cur in sorted(foreign):
            lines.append(f"{_fmt_foreign(foreign[cur])} {cur}")

    # メール受信が遅れて過去日付で登録された取引（日次の内訳には現れないが
    # 週計・月計には含まれている）をここで案内する
    if late_arrivals:
        lines.append("")
        lines.append("追加登録（過去日分）")
        week_start = analysis.get("week_start")
        month_start = analysis.get("month_start")
        has_prev_week_item = False
        has_prev_month_item = False
        for t in late_arrivals:
            # 年またぎの通知もあり得るため MM/DD ではなく年まで表示する
            date_str = t.get("date", "")
            d2 = date_str.replace("-", "/")  # 'YYYY-MM-DD' → 'YYYY/MM/DD'
            amount = _fmt_amount(t.get("amount", 0), t.get("currency", "JPY"))
            store = sanitize(t.get("store")) or "（店舗名なし）"
            lines.append(f"{d2}  {amount}  {store}")

            try:
                entry_date = date.fromisoformat(date_str)
            except ValueError:
                continue
            if week_start is not None and entry_date < week_start:
                has_prev_week_item = True
            if month_start is not None and entry_date < month_start:
                has_prev_month_item = True

        # 既に送信済みの先週・先月まとめの合計には、この追加登録が反映済み
        # であることを案内する（案内しないと利用者が正しい合計を知る手段が無い）
        if has_prev_month_item and analysis.get("prev_month_total") is not None:
            lines.append(f"先月計（追加登録反映）  ¥{analysis['prev_month_total']:,}")
        if has_prev_week_item and analysis.get("prev_week_total") is not None:
            lines.append(f"先週計（追加登録反映）  ¥{analysis['prev_week_total']:,}")

    return "\n".join(lines)


def send_daily_report(analysis: dict, late_arrivals: list[dict] | None = None) -> None:
    push(_build_daily_text(analysis, late_arrivals))
