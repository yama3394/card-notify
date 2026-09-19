"""LINE メッセージから現金支出を登録し、集計コマンドに応答する。"""
import logging
import re
import unicodedata
import uuid
from datetime import date, timedelta

import late_arrivals
import parsers
import reports
import state as _state
from analyzer import analyze_week, today_jst
from storage import load_history, update_history

logger = logging.getLogger(__name__)

_MONTHLY_TRIGGERS = {"今月のまとめ", "月次レポート", "まとめ"}
_WEEKDAYS_JP = ["月", "火", "水", "木", "金", "土", "日"]

# 「取消」「#101 取消し」「取消 #101」。NFKC 後に照合するので全角の ＃・数字・空白も通る。
# ID には # を必須にする（「800 取消」は従来どおり店舗名「取消」での登録）。
_CANCEL_WORD = r"(?:取り消し|取消し|取消|とりけし)"
_CANCEL_RE = re.compile(rf"^(?:{_CANCEL_WORD}|#(\d+)\s*{_CANCEL_WORD}|{_CANCEL_WORD}\s*#(\d+))$")


class _NonexistentDate(Exception):
    """MM/DD形式だが、その年には存在しない日付（例：うるう年でない年の2/29）。"""


def _parse_date(token: str, base: date) -> date | None:
    if token == "昨日":
        return base - timedelta(days=1)
    if token == "今日":
        return base
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", token)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            d = date(base.year, month, day)
        except ValueError:
            # 2/29はうるう年になれば存在する日付であり、13/45のような明らかな
            # 非日付とは違って入力ミスに気づきにくい。黙って店舗名扱いにせず
            # 呼び出し元でエラーとして案内する。
            if month == 2 and day == 29:
                raise _NonexistentDate(token)
            return None
        # 年始（1月）に前年12月分（例：「12/31」）を入力すると今年の未来日付に
        # なってしまうケースだけを前年扱いに補正する。対象を「1月に11月・12月
        # を指定した場合」に絞ることで、同一年内の数か月先の入力（6月に12月分
        # を入力する等）まで前年に倒してしまう誤補正を防ぐ。
        if d > base and base.month == 1 and month >= 11:
            try:
                d = date(base.year - 1, month, day)
            except ValueError:
                return None
        return d
    return None


def _flag_if_late(entry_date: date, today: date, transaction: dict) -> None:
    """手入力の日付が、もうどの日次通知の内訳にも入れないタイミングなら
    late arrival として state に積む。

    判定基準は late_arrivals.is_late（gmail_fetcher と共有）。日の内訳には
    二度と現れず、週計・月計にだけ黙って加算されてしまうケース（analyzer.
    analyze の week_total/month_total は target ではなく実行時点の当日まで
    を毎回通しで再計算するため）を拾い、「追加登録」で案内する。

    判定（last_daily の読み取り）と積み込みを1回のロックで行う。別々に
    行うと、その間に日次通知が送信されて last_daily が書き換わった場合に
    誤判定しうるため。
    """
    def _mutator(s: dict) -> None:
        if late_arrivals.is_late(entry_date, today, s.get("last_daily")):
            s.setdefault("late_arrivals", []).append({
                "no": transaction["no"],  # 取り消されたときに通知予定から外すため
                "date": transaction["date"],
                "amount": transaction["amount"],
                "currency": transaction.get("currency", "JPY"),
                "type": transaction["type"],
                "store": transaction["store"],
            })

    _state.update(_mutator)


def _cash_total_on_date(transactions: list[dict], d: date) -> int:
    ds = d.strftime("%Y-%m-%d")
    return sum(t["amount"] for t in transactions if t["date"] == ds and t["type"] == "cash")


def _list_today() -> str:
    today = today_jst()
    ds = today.strftime("%Y-%m-%d")
    txs = [t for t in load_history().get("transactions", []) if t.get("date") == ds]
    if not txs:
        return f"📋 {today.month}/{today.day} の支出はありません"
    labels = parsers.type_labels()
    lines = [f"📋 {today.month}/{today.day} の支出"]
    total = 0
    for t in txs:
        label = labels.get(t.get("type", ""), t.get("type", ""))
        store = f" {t['store']}" if t.get("store") else ""
        # ID は LINE から取り消せる現金にだけ出す（カードの ID は WebUI のみ）
        no = f" (#{t['no']})" if t.get("type") == "cash" and t.get("no") else ""
        lines.append(f"  {label}{store}  ¥{t['amount']:,}{no}")
        total += t["amount"]
    lines.append(f"合計  ¥{total:,}")
    return "\n".join(lines)


def _list_week() -> str:
    r = analyze_week()
    ws = r["week_start"]
    we = ws + timedelta(days=6)
    lines = [f"📋 {ws.month}/{ws.day}〜{we.month}/{we.day}"]
    for d in r["days"]:
        if d["total"]:
            wday = _WEEKDAYS_JP[d["date"].weekday()]
            lines.append(f"  {d['date'].month}/{d['date'].day}({wday})  ¥{d['total']:,}")
    lines.append(f"週計  ¥{r['week_total']:,}")
    return "\n".join(lines)


def process_line(message: str) -> str:
    """LINE から届いたメッセージを処理する。

    取消は LINE 専用のコマンド。WebUI の /entry は process() を直接呼ぶので、
    登録フォームから取消が走ることはない（WebUI には削除ボタンがある）。
    """
    m = _CANCEL_RE.match(unicodedata.normalize("NFKC", message).strip())
    if m:
        no = m.group(1) or m.group(2)
        return _cancel(int(no) if no else None)
    return process(message)


def process(message: str) -> str:
    """LINE メッセージを解釈し現金支出を登録する。応答テキストを返す。"""
    text = message.strip()

    if text in _MONTHLY_TRIGGERS:
        try:
            reports.send_monthly_report()
            return "📋 月次レポートを送信しました"
        except Exception as e:
            return f"⚠️ 月次レポート送信失敗: {e}"

    if text == "今日":
        return _list_today()
    if text == "今週":
        return _list_week()

    text = re.sub(r"^現金\s*", "", text)  # 先頭の「現金」を除去
    tokens = text.split()
    if not tokens:
        return _invalid_reply()

    if not re.match(r"^(?:\d{1,3}(?:,\d{3})*|\d+)$", tokens[0]):
        return _invalid_reply()

    amount = int(tokens[0].replace(",", ""))
    store: str | None = None
    entry_date = today_jst()
    remaining = tokens[1:]

    if remaining:
        try:
            d = _parse_date(remaining[-1], today_jst())
        except _NonexistentDate as e:
            return f"❓ {e} は{today_jst().year}年に存在しない日付です（うるう年ではありません）"
        if d is not None:
            entry_date = d
            remaining = remaining[:-1]
        if remaining:
            store = " ".join(remaining)

    transaction = {
        "id": str(uuid.uuid4()),
        "date": entry_date.strftime("%Y-%m-%d"),
        "amount": amount,
        "type": "cash",
        "store": store,
    }
    captured: dict = {}

    def _add(data: dict) -> None:
        data["transactions"].append(transaction)
        # LINE の「取消」（ID 指定なし）が取り消す対象。取り消せるのはこの1件だけ
        data["last_cash_entry_id"] = transaction["id"]
        captured["cash_total"] = _cash_total_on_date(data["transactions"], entry_date)

    update_history(_add)
    _flag_if_late(entry_date, today_jst(), transaction)

    wday = _WEEKDAYS_JP[entry_date.weekday()]
    store_line = f"\n　店舗: {store}" if store else ""
    return (
        f"✅ {entry_date.year}/{entry_date.month}/{entry_date.day} ({wday}) の現金支出に ¥{amount:,} を登録しました\n"
        f"　ID: #{transaction['no']}"
        f"{store_line}\n"
        f"　{_total_label(entry_date)}: ¥{captured['cash_total']:,}"
    )


def _total_label(entry_date: date) -> str:
    # 過去日付の登録では「本日」ではなくその日の現金合計を示しているため、
    # 表記を entry_date に合わせて紛らわしさをなくす。
    if entry_date == today_jst():
        return "本日現金合計"
    return f"{entry_date.month}/{entry_date.day}の現金合計"


def _cancel(no: int | None) -> str:
    """当日に送信した現金登録を取り消す。no=None なら直前の登録（1件だけ）。

    「当日」は支出日ではなく送信日（created_at）で判定する。「800 セブン 昨日」
    を今日送ったなら今日のうちは取り消せ、23:59 に送ったものは 0:00 を過ぎたら
    取り消せない。created_at の無い v1.2.0 より前の取引は取り消せない。
    """
    today = today_jst()
    captured: dict = {}

    def _remove(data: dict) -> None:
        if no is None:
            last_id = data.get("last_cash_entry_id")
            t = next((t for t in data["transactions"] if last_id and t.get("id") == last_id), None)
            if t is None:
                captured["error"] = (
                    "❓ 取り消せる直前の登録がありません（取消済みか削除済みです）\n"
                    "　今日の登録は「#ID 取消」で指定できます"
                )
                return
        else:
            t = next((t for t in data["transactions"] if t.get("no") == no), None)
            if t is None:
                captured["error"] = f"❓ #{no} の取引は見つかりません（取消済みか削除済みです）"
                return
            if t.get("type") != "cash":
                captured["error"] = f"❓ #{no} はカードの取引のため LINE からは取り消せません（WebUI で削除してください）"
                return
        if not (t.get("created_at") or "").startswith(today.isoformat()):
            captured["error"] = (
                f"❓ #{t['no']} は今日送信した登録ではないため取り消せません（WebUI で削除してください）"
            )
            return
        # 現金はメール由来ではないので skipped_ids に積む必要はない
        data["transactions"] = [x for x in data["transactions"] if x is not t]
        if data.get("last_cash_entry_id") == t["id"]:
            data["last_cash_entry_id"] = None
        captured["removed"] = t
        captured["cash_total"] = _cash_total_on_date(data["transactions"], date.fromisoformat(t["date"]))

    update_history(_remove)
    if "error" in captured:
        return captured["error"]

    t = captured["removed"]
    entry_date = date.fromisoformat(t["date"])
    _unflag_cancelled(t, entry_date, today)

    wday = _WEEKDAYS_JP[entry_date.weekday()]
    store_line = f"\n　店舗: {t['store']}" if t.get("store") else ""
    return (
        f"🗑 {entry_date.year}/{entry_date.month}/{entry_date.day} ({wday}) の現金支出 ¥{t['amount']:,} を取り消しました\n"
        f"　ID: #{t['no']}"
        f"{store_line}\n"
        f"　{_total_label(entry_date)}: ¥{captured['cash_total']:,}"
    )


def _unflag_cancelled(t: dict, entry_date: date, today: date) -> None:
    """取り消した取引を、日次通知の保留分と整合させる。

    まだ通知していない「追加登録」に積まれていれば外す（外さないと消した取引が
    翌朝「追加登録」として通知される）。そうでなく、その日がもう日次通知
    （または追加登録）で報告済みなら「取消反映」に積む（積まないと週計・月計が
    黙って減る）。
    """
    def _mutator(s: dict) -> None:
        pending = s.get("late_arrivals") or []
        kept = [a for a in pending if a.get("no") != t["no"]]
        if len(kept) != len(pending):
            s["late_arrivals"] = kept
        elif late_arrivals.is_late(entry_date, today, s.get("last_daily")):
            s.setdefault("cancellations", []).append({
                "date": t["date"],
                "amount": t["amount"],
                "currency": t.get("currency", "JPY"),
                "type": t["type"],
                "store": t["store"],
            })

    _state.update(_mutator)


def _invalid_reply() -> str:
    return (
        "❓ 金額を送ってください\n"
        "　例: 800\n"
        "　例: 800 セブン\n"
        "　例: 800 セブン 昨日\n"
        "\n"
        "コマンド一覧:\n"
        "　今日 → 今日の支出一覧\n"
        "　今週 → 今週の日別集計\n"
        "　まとめ → 月次レポート送信\n"
        "　取消 → 直前の現金登録を取り消し\n"
        "　#ID 取消 → IDを指定して取り消し（今日送信した分のみ）"
    )
