"""Gmail から利用通知メールを取得し history.json に登録する。

発行会社ごとの解釈は parsers レジストリに委譲しているため、このモジュールは
「登録済みの全パーサを回して取得・登録・取消反映する」汎用処理だけを持つ。
"""
import base64
import email as _email
import email.policy
import email.utils
import html as _html
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import config
import late_arrivals
import parsers
import state as _state
from storage import load_history, update_history

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
JST = timezone(timedelta(hours=9))
_DEFAULT_DAYS_BACK = 30

logger = logging.getLogger(__name__)


def _get_gmail_service():
    creds = Credentials.from_authorized_user_file(str(config.TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        config.TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _html_to_text(raw: str) -> str:
    """HTML メール本文をパーサが解釈できるプレーンテキストへ変換する。"""
    # script/style はタグだけ消すと中身（JS/CSS）が本文に残るためブロックごと除去。
    text = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # パーサは行単位の構造に依存するため、改行相当のタグは改行に変換してから
    # 残りのタグを除去する。
    text = re.sub(r"<br\s*/?\s*>|</(?:p|div|td|tr)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # 文字実体参照（&amp; &yen; 等）をデコードし、連続空白を1つに畳む（改行は保持）。
    text = _html.unescape(text)
    return re.sub(r"[ \t　]+", " ", text)


def _extract_body(msg: _email.message.Message) -> str:
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/plain":
            return part.get_content()
        if ct == "text/html":
            return _html_to_text(part.get_content())
    return ""


def _normalize_store(store: str | None) -> str | None:
    """店舗名の全角/半角・前後空白ゆれを吸収して比較できるようにする。"""
    if store is None:
        return None
    return unicodedata.normalize("NFKC", store).strip()


def _remove_matching_transaction(history, card_type, store, amount, currency="JPY", date=None) -> bool:
    """取消メールに対応する既存取引を1件削除する。

    候補は「種別+金額+通貨+店舗+日付」→「種別+金額+通貨+店舗」の順に絞り込む
    （店舗名は正規化して比較し、表記ゆれによる取りこぼしを防ぐ）。店舗名・日付
    のどちらでも絞り込めなかった場合、「種別+金額+通貨」だけが一致する候補が
    複数あると誤って無関係な取引を削除しかねないため、候補が1件に定まる場合の
    みそれを削除し、定まらない場合は削除しない（README の既知の制約を参照）。

    削除した取引の id は history['skipped_ids'] にも積む。Gmail の取得窓の
    都合で取消メール自体は当日以降も再度取得され得るが、その msg_id は別途
    fetch_for_parser 側で skipped_ids に積まれ再処理されない。一方、削除された
    「元の購入取引」の msg_id をここで積んでおかないと、購入メールと取消メール
    が同じ日次実行に届いて相殺されたケースで、翌日以降の再実行時に購入メール
    だけが「未処理」として再登録され、幽霊取引が復活してしまう。
    """
    if amount is None:
        return False

    def _cur(t):
        return t.get("currency", "JPY")

    norm_store = _normalize_store(store)

    txns = history["transactions"]
    base = [
        i for i, t in enumerate(txns)
        if t["type"] == card_type and t["amount"] == amount and _cur(t) == currency
    ]
    by_store = [i for i in base if norm_store is None or _normalize_store(txns[i].get("store")) == norm_store]
    by_date = [i for i in by_store if date is None or txns[i].get("date") == date]

    if by_date:
        idx = by_date[-1]
    elif by_store:
        idx = by_store[-1]
    elif len(base) == 1:
        idx = base[0]
    else:
        return False

    removed = txns.pop(idx)
    history.setdefault("skipped_ids", []).append(removed["id"])
    logger.info(f"取消に伴い削除: {card_type} {removed['amount']} {removed.get('store')} {removed['date']}")
    return True


def _msg_date_jst(msg: _email.message.Message) -> str:
    """メールの Date ヘッダを JST の 'YYYY-MM-DD' に変換する。解釈できなければ今日の日付。"""
    date_header = msg.get("Date")
    if date_header:
        try:
            dt = email.utils.parsedate_to_datetime(date_header)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=JST)
                return dt.astimezone(JST).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass
    return datetime.now(JST).strftime("%Y-%m-%d")


def _get_fetch_after_date() -> str:
    last = _state.load().get("last_fetched_date")
    if last:
        dt = datetime.strptime(last, "%Y-%m-%d") - timedelta(days=1)
    else:
        dt = datetime.now(JST) - timedelta(days=_DEFAULT_DAYS_BACK)
    return dt.strftime("%Y/%m/%d")


def _save_fetch_date() -> None:
    today = datetime.now(JST).strftime("%Y-%m-%d")

    def _set(s: dict) -> None:
        s["last_fetched_date"] = today

    _state.update(_set)


def _late_arrivals_from(transactions: list[dict], today, last_daily: str | None) -> list[dict]:
    """transactions のうち、次の日次通知の内訳にはもう入れない日付のものを
    late arrival 形式で返す（manual_entry.py の手入力遅延判定と同じ基準）。"""
    return [
        {k: t[k] for k in ("date", "amount", "currency", "type", "store")}
        for t in transactions
        if late_arrivals.is_late(datetime.strptime(t["date"], "%Y-%m-%d").date(), today, last_daily)
    ]


def fetch_for_parser(parser, after_date: str) -> int:
    """1つのパーサ（=1発行会社）ぶんの取得・登録を行う。"""
    dt = datetime.strptime(after_date, "%Y/%m/%d")
    from_query = " OR ".join(f"from:{a}" for a in parser.from_addrs)
    query = f"({from_query}) after:{dt.strftime('%Y/%m/%d')}"
    if parser.subject_contains:
        query += f" subject:{parser.subject_contains}"

    # 重複判定用スナップショット。ネットワークI/Oはロックを持たずに行い、
    # 確定した差分だけを最後に短時間の排他ロックで最新データへマージする。
    snapshot = load_history()
    existing_ids = {t["id"] for t in snapshot["transactions"]} | set(snapshot["skipped_ids"])

    service = _get_gmail_service()
    results = service.users().messages().list(userId="me", q=query).execute()
    messages = results.get("messages", [])
    while "nextPageToken" in results:
        results = service.users().messages().list(
            userId="me", q=query, pageToken=results["nextPageToken"]
        ).execute()
        messages.extend(results.get("messages", []))

    new_transactions: list[dict] = []
    new_skipped: list[str] = []
    new_skipped_meta: list[dict] = []
    cancellations: list[tuple] = []
    new_count = 0

    for m in messages:
        msg_id = m["id"]
        if msg_id in existing_ids:
            continue

        raw = service.users().messages().get(userId="me", id=msg_id, format="raw").execute()
        msg = _email.message_from_bytes(
            base64.urlsafe_b64decode(raw["raw"]), policy=_email.policy.default
        )
        subject = msg.get("Subject", "")

        # 件名が対象条件に合わない/明示的に対象外なメールは、二度と再処理しない
        # よう skipped_ids に永続化する（そうしないと同じメールが Gmail の取得窓
        # の重なりで毎日再度現れ、そのたびに誤判定や重複ログの元になる）。
        if parser.subject_contains and parser.subject_contains not in subject:
            new_skipped.append(msg_id)
            existing_ids.add(msg_id)
            continue

        if parser.ignore_subjects and any(s in subject for s in parser.ignore_subjects):
            logger.info(f"件名により対象外: msg_id={msg_id} subject={subject!r}")
            new_skipped.append(msg_id)
            existing_ids.add(msg_id)
            continue

        text = _extract_body(msg)

        if parser.is_cancellation(text):
            r = parser.parse(text)
            if r:
                cancellations.append((r.store, r.amount, r.currency, r.date))
            else:
                logger.warning(f"取消メールを解釈できません: msg_id={msg_id}")
            new_skipped.append(msg_id)
            existing_ids.add(msg_id)
            continue

        if parser.is_ignorable(text):
            logger.info(f"スキップ: msg_id={msg_id}")
            new_skipped.append(msg_id)
            existing_ids.add(msg_id)
            continue

        results = parser.parse_all(text)
        if not results:
            logger.warning(f"金額を抽出できませんでした（要手動確認）: msg_id={msg_id}")
            new_skipped.append(msg_id)
            new_skipped_meta.append({
                "id": msg_id,
                "type": parser.key,
                "date": _msg_date_jst(msg),
                "subject": subject,
                "reason": "no_amount",
            })
            existing_ids.add(msg_id)
            continue

        # 1通に複数取引が入る形式（JCB「売上到着分」等）は2件目以降を
        # "{msg_id}#2", "#3"... とする。重複判定(existing_ids)は元の msg_id
        # ベースのまま（1件目の id が msg_id と一致するので次回実行時も拾える）。
        for i, r in enumerate(results):
            tx_id = msg_id if i == 0 else f"{msg_id}#{i + 1}"
            if r.store is None:
                logger.warning(f"店舗名を抽出できませんでした: msg_id={tx_id}")

            transaction = {
                "id": tx_id,
                "date": r.date,
                "amount": r.amount,
                "type": parser.key,
                "store": r.store,
                "currency": r.currency,
            }
            if r.store is None:
                # /errors 画面で本文を確認しながら手動で店舗名を確定できるようにする
                transaction["raw_text"] = text[:1500]
            new_transactions.append(transaction)
            new_count += 1
            disp = f"¥{r.amount:,}" if r.currency == "JPY" else f"{r.amount:,} {r.currency}"
            logger.info(f"登録: {parser.key} {disp} {r.store} {r.date}")

        existing_ids.add(msg_id)

    def _merge(history: dict) -> None:
        history["transactions"].extend(new_transactions)
        history["skipped_ids"].extend(new_skipped)
        history.setdefault("skipped", []).extend(new_skipped_meta)
        for store, amount, currency, date in cancellations:
            if not _remove_matching_transaction(history, parser.key, store, amount, currency, date):
                logger.warning(f"取消対象の取引が見つかりません: {parser.key} {amount} {currency} {store}")

    update_history(_merge)

    # メール受信が遅れて、もうどの日次通知の内訳にも入れないタイミングで登録
    # された取引はどの日次通知にも現れない。ここで拾って state に積み、次の
    # 日次通知の「追加登録」セクションで消化する（main.py が送信後にクリア）。
    today = datetime.now(JST).date()

    def _push_late(s: dict) -> None:
        late = _late_arrivals_from(new_transactions, today, s.get("last_daily"))
        if late:
            s.setdefault("late_arrivals", []).extend(late)
            logger.info(f"{parser.key}: 過去日付の遅延登録 {len(late)}件を次回日次通知で案内します")

    _state.update(_push_late)

    logger.info(f"{parser.key}: {new_count}件の新規取引を登録しました")
    return new_count


def fetch_all() -> int:
    """登録済みの全パーサを回して取得する。"""
    after_date = _get_fetch_after_date()
    logger.info(f"メール取得範囲: {after_date} 以降")
    total = 0
    for parser in parsers.all_parsers():
        total += fetch_for_parser(parser, after_date)
    _save_fetch_date()
    return total
