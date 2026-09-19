"""gmail_fetcher の本文抽出・取消突合せのテスト。"""
import base64
from datetime import date
from email.message import EmailMessage

import config
import gmail_fetcher
import parsers
import state as _state


def _html_msg(html: str) -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = "test"
    m.set_content(html, subtype="html")
    return m


# ---------------------------------------------------------------------------
# fetch_for_parser の統合テスト用: 実際の Gmail API を叩かず、messages.list /
# messages.get だけを模したスタブサービス。
# ---------------------------------------------------------------------------

class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeGmailService:
    """msg_id -> base64 raw の辞書からメールを返す最小限の Gmail API スタブ。"""

    def __init__(self, raws: dict):
        self._raws = raws

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, pageToken=None):
        return _Exec({"messages": [{"id": mid} for mid in self._raws]})

    def get(self, userId, id, format):
        return _Exec({"raw": self._raws[id]})


def _raw_b64(subject: str, from_addr: str, body: str, date: str | None = None) -> str:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = "me@example.com"
    if date:
        msg["Date"] = date
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _use_tmp_files(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "notify_state.json")


class TestExtractBody:
    def test_plain_text_returned_as_is(self):
        m = EmailMessage()
        m.set_content("◇利用金額：1,200円\n◇利用先：TEST STORE")
        assert "◇利用金額：1,200円" in gmail_fetcher._extract_body(m)

    def test_plain_preferred_over_html(self):
        m = EmailMessage()
        m.set_content("plain body")
        m.add_alternative("<p>html body</p>", subtype="html")
        assert gmail_fetcher._extract_body(m).strip() == "plain body"

    def test_script_and_style_blocks_removed(self):
        html = (
            "<html><head><style>body { color: red; }</style>"
            "<script type='text/javascript'>var secret = 999;</script></head>"
            "<body><p>◇利用金額：1,200円</p></body></html>"
        )
        text = gmail_fetcher._extract_body(_html_msg(html))
        assert "color" not in text
        assert "secret" not in text
        assert "999" not in text
        assert "◇利用金額：1,200円" in text

    def test_entities_unescaped(self):
        html = "<p>A&amp;B &yen;1,200 &lt;test&gt;</p>"
        text = gmail_fetcher._extract_body(_html_msg(html))
        assert "A&B" in text
        assert "¥1,200" in text
        assert "<test>" in text

    def test_block_tags_become_newlines(self):
        html = "<div>◇利用日：2026/07/08</div><p>◇利用金額：1,200円</p>◇利用先：X<br>おわり"
        text = gmail_fetcher._extract_body(_html_msg(html))
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        assert "◇利用日：2026/07/08" in lines
        assert "◇利用金額：1,200円" in lines

    def test_whitespace_collapsed(self):
        html = "<p>◇利用金額：   1,200円　　テスト</p>"
        text = gmail_fetcher._extract_body(_html_msg(html))
        assert "◇利用金額： 1,200円 テスト" in text


def _history(txns):
    return {"transactions": list(txns), "skipped_ids": []}


def _txn(i, amount, store, date, card_type="smcc", currency="JPY"):
    return {
        "id": str(i), "date": date, "amount": amount,
        "type": card_type, "store": store, "currency": currency,
    }


class TestRemoveMatchingTransaction:
    def test_date_match_preferred_over_last(self):
        # 同種別・同額・同店舗が2件。日付が一致する古い方が消えること。
        h = _history([
            _txn(1, 1200, "STORE A", "2026-07-01"),
            _txn(2, 1200, "STORE A", "2026-07-05"),
        ])
        assert gmail_fetcher._remove_matching_transaction(
            h, "smcc", "STORE A", 1200, "JPY", "2026-07-01"
        ) is True
        assert [t["id"] for t in h["transactions"]] == ["2"]

    def test_falls_back_to_store_match_when_date_unmatched(self):
        h = _history([
            _txn(1, 1200, "STORE A", "2026-07-01"),
            _txn(2, 1200, "STORE A", "2026-07-05"),
        ])
        assert gmail_fetcher._remove_matching_transaction(
            h, "smcc", "STORE A", 1200, "JPY", "2026-07-31"
        ) is True
        # 日付不一致なら従来通り末尾（最後に登録されたもの）。
        assert [t["id"] for t in h["transactions"]] == ["1"]

    def test_ambiguous_amount_only_match_does_not_delete(self):
        # 店舗名・日付のどちらでも絞り込めず候補が複数残る場合、無関係な取引を
        # 誤って削除しないよう取消を反映しない（以前は末尾を無条件で削除していた）。
        h = _history([
            _txn(1, 1200, "STORE A", "2026-07-01"),
            _txn(2, 1200, "STORE B", "2026-07-05"),
        ])
        assert gmail_fetcher._remove_matching_transaction(
            h, "smcc", "STORE C", 1200, "JPY", "2026-07-09"
        ) is False
        assert [t["id"] for t in h["transactions"]] == ["1", "2"]

    def test_unique_amount_match_without_store_or_date_still_removes(self):
        h = _history([_txn(1, 1200, "STORE A", "2026-07-01")])
        assert gmail_fetcher._remove_matching_transaction(
            h, "smcc", "STORE C", 1200, "JPY", "2026-07-09"
        ) is True
        assert h["transactions"] == []

    def test_store_width_variant_still_matches(self):
        # 取消メールの店舗名表記が全角/半角ゆれで完全一致しなくても突合せできる
        h = _history([_txn(1, 1200, "ABCストア", "2026-07-01")])
        assert gmail_fetcher._remove_matching_transaction(
            h, "smcc", "ＡＢＣストア", 1200, "JPY", "2026-07-01"
        ) is True
        assert h["transactions"] == []

    def test_store_and_date_narrow_together(self):
        h = _history([
            _txn(1, 1200, "STORE A", "2026-07-01"),
            _txn(2, 1200, "STORE B", "2026-07-01"),
            _txn(3, 1200, "STORE A", "2026-07-05"),
        ])
        assert gmail_fetcher._remove_matching_transaction(
            h, "smcc", "STORE A", 1200, "JPY", "2026-07-01"
        ) is True
        assert [t["id"] for t in h["transactions"]] == ["2", "3"]

    def test_none_date_keeps_previous_behavior(self):
        h = _history([
            _txn(1, 1200, "STORE A", "2026-07-01"),
            _txn(2, 1200, "STORE A", "2026-07-05"),
        ])
        assert gmail_fetcher._remove_matching_transaction(
            h, "smcc", "STORE A", 1200
        ) is True
        assert [t["id"] for t in h["transactions"]] == ["1"]

    def test_none_store_matches_any_store(self):
        h = _history([
            _txn(1, 1200, "STORE A", "2026-07-01"),
            _txn(2, 1200, "STORE B", "2026-07-05"),
        ])
        assert gmail_fetcher._remove_matching_transaction(
            h, "smcc", None, 1200, "JPY", "2026-07-01"
        ) is True
        assert [t["id"] for t in h["transactions"]] == ["2"]

    def test_currency_must_match(self):
        h = _history([_txn(1, 1200, "STORE A", "2026-07-01", currency="KRW")])
        assert gmail_fetcher._remove_matching_transaction(
            h, "smcc", "STORE A", 1200, "JPY", "2026-07-01"
        ) is False
        assert len(h["transactions"]) == 1

    def test_no_match_returns_false(self):
        h = _history([_txn(1, 999, "STORE A", "2026-07-01")])
        assert gmail_fetcher._remove_matching_transaction(
            h, "smcc", "STORE A", 1200, "JPY", "2026-07-01"
        ) is False

    def test_none_amount_returns_false(self):
        h = _history([_txn(1, 1200, "STORE A", "2026-07-01")])
        assert gmail_fetcher._remove_matching_transaction(
            h, "smcc", "STORE A", None, "JPY", "2026-07-01"
        ) is False


# ---------------------------------------------------------------------------
# 回帰テスト: 遅延到着の判定が固定「2日以上前」しきい値だと、当日分の通知が
# 送信済みかどうかを見ずに判定してしまい manual_entry.py の判定とズレていた。
# late_arrivals.is_late に基準を統一したことを確認する。
# ---------------------------------------------------------------------------

class TestLateArrivalsFrom:
    def test_uses_last_daily_to_determine_reachable_target(self):
        today = date(2026, 5, 29)
        txns = [_txn(1, 500, "A", "2026-05-28"), _txn(2, 800, "B", "2026-05-20")]
        # 今日(5/29)分の通知が既に送信済み → 次の対象は5/29。5/28・5/20は
        # どちらも対象外（旧実装の固定2日しきい値では5/28は拾えなかった）
        late = gmail_fetcher._late_arrivals_from(txns, today, "2026-05-29")
        assert [t["date"] for t in late] == ["2026-05-28", "2026-05-20"]

    def test_not_late_when_still_within_reachable_window(self):
        today = date(2026, 5, 29)
        txns = [_txn(1, 500, "A", "2026-05-28")]
        # last_daily=5/27（前日分未送信） → 次の対象は5/28。この後の通知で拾われる
        late = gmail_fetcher._late_arrivals_from(txns, today, "2026-05-27")
        assert late == []

    def test_returns_only_late_arrival_fields(self):
        today = date(2026, 5, 29)
        txns = [_txn(1, 500, "A", "2026-05-20")]
        late = gmail_fetcher._late_arrivals_from(txns, today, "2026-05-28")
        assert late == [
            {"date": "2026-05-20", "amount": 500, "currency": "JPY", "type": "smcc", "store": "A"}
        ]


# ---------------------------------------------------------------------------
# _remove_matching_transaction: 削除した取引の id を skipped_ids にも積む
# （取消メール自体はスキップ済みでも、削除された「元の購入」の msg_id が
#  未処理のまま残ると、翌日の再実行で幽霊取引として復活してしまうため）。
# ---------------------------------------------------------------------------

class TestRemoveMatchingTransactionPersistsSkippedId:
    def test_removed_transaction_id_is_added_to_skipped_ids(self):
        h = _history([_txn(1, 1200, "STORE A", "2026-07-01")])
        assert gmail_fetcher._remove_matching_transaction(
            h, "smcc", "STORE A", 1200, "JPY", "2026-07-01"
        ) is True
        assert h["skipped_ids"] == ["1"]

    def test_failed_removal_does_not_touch_skipped_ids(self):
        h = _history([
            _txn(1, 1200, "STORE A", "2026-07-01"),
            _txn(2, 1200, "STORE B", "2026-07-05"),
        ])
        removed = gmail_fetcher._remove_matching_transaction(
            h, "smcc", "STORE C", 1200, "JPY", "2026-07-09"
        )
        assert removed is False
        assert h["skipped_ids"] == []


# ---------------------------------------------------------------------------
# fetch_for_parser 統合テスト（Gmail サービスをスタブ化）
# ---------------------------------------------------------------------------

class TestFetchForParserCancellationPersistence:
    """回帰テスト: 実データ(cron.log)で確認された誤削除の再現と修正確認。

    2026-07-21 に jcb ¥430 マクドナルドモバイルオーダーが取消で削除された翌日
    (07-22)、同じ取消メールが Gmail の取得窓の重なりで再度取得され、無関係な
    別店舗の同額取引「ニホンマクドナルド（カ」¥430 が誤って削除された実害が
    あった。取消メールの msg_id を skipped_ids に永続化し、二度と再処理しない
    ことでこれを防ぐ。
    """

    def test_cancellation_email_not_reprocessed_on_next_run(self, monkeypatch, tmp_path):
        _use_tmp_files(monkeypatch, tmp_path)
        parser = parsers.get("jcb")

        cancel_raw = _raw_b64(
            subject="JCBカード／ショッピング取消のお知らせ",
            from_addr="statement@jcb.co.jp",
            body=(
                "JCBカードでのショッピングの取消がありましたので、ご連絡します。\n"
                "【日時（日本時間）】　2026/07/21 03:20\n"
                "【金額】- 430円（取消）\n"
                "【ご利用先】　マクドナルドモバイルオ－ダ－\n"
            ),
        )
        monkeypatch.setattr(gmail_fetcher, "_get_gmail_service", lambda: _FakeGmailService({"cancel1": cancel_raw}))

        # Day1: 対象の購入取引が存在する状態で処理 → 正しく削除される
        gmail_fetcher.update_history(lambda h: h["transactions"].append(
            _txn("purchase-mcdonalds", 430, "マクドナルドモバイルオ－ダ－", "2026-07-21", card_type="jcb")
        ))
        gmail_fetcher.fetch_for_parser(parser, "2026/07/01")

        h = gmail_fetcher.load_history()
        assert h["transactions"] == []
        assert "cancel1" in h["skipped_ids"]
        assert "purchase-mcdonalds" in h["skipped_ids"]  # 削除された取引のidも永続化される

        # Day2: 無関係な同額の新規取引が登録された状態で、同じ取消メールが
        # Gmail の取得窓の重なりでまた返る。既に処理済みなので無視され、
        # 無関係な取引は削除されない（以前はここで誤削除が発生していた）。
        gmail_fetcher.update_history(lambda h: h["transactions"].append(
            _txn("purchase-other-store", 430, "ニホンマクドナルド（カ", "2026-07-22", card_type="jcb")
        ))
        gmail_fetcher.fetch_for_parser(parser, "2026/07/01")

        h2 = gmail_fetcher.load_history()
        assert [t["id"] for t in h2["transactions"]] == ["purchase-other-store"]

    def test_same_run_purchase_and_cancellation_then_rerun_does_not_resurrect_purchase(self, monkeypatch, tmp_path):
        # 07-07 の実データ: 日本オラクル ¥155 の購入通知と取消通知が同じ日次
        # 実行で届き、_merge 内で登録直後に削除される。この場合でも購入メール
        # 自身の msg_id が skipped_ids に残るため、翌日の再実行（Gmailの取得窓の
        # 重なりで同じ2通がまた返る）で購入だけが「未処理」として再登録される
        # 幽霊取引(¥155)を防ぐ。
        _use_tmp_files(monkeypatch, tmp_path)
        parser = parsers.get("smcc")

        purchase_raw = _raw_b64(
            subject="ご利用のお知らせ【三井住友カード】",
            from_addr="statement@vpass.ne.jp",
            body="◇利用日：2026/07/07 08:59\n◇利用先：日本オラクル\n◇利用取引：買物\n◇利用金額：155円\n",
        )
        cancel_raw = _raw_b64(
            subject="ご利用のお知らせ【三井住友カード】",
            from_addr="statement@vpass.ne.jp",
            body="◇利用日：2026/07/07 08:59\n◇利用先：日本オラクル\n◇利用取引：取消\n◇利用金額：155円\n",
        )
        raws = {"purchase1": purchase_raw, "cancel1": cancel_raw}
        monkeypatch.setattr(gmail_fetcher, "_get_gmail_service", lambda: _FakeGmailService(raws))

        # Day1: 購入と取消が同じ実行で届く → 相殺されて0件
        gmail_fetcher.fetch_for_parser(parser, "2026/07/01")
        h = gmail_fetcher.load_history()
        assert h["transactions"] == []
        assert {"purchase1", "cancel1"} <= set(h["skipped_ids"])

        # Day2: 同じ2通がまた返るが、両方処理済みなので完全に無視される
        gmail_fetcher.fetch_for_parser(parser, "2026/07/01")
        h2 = gmail_fetcher.load_history()
        assert h2["transactions"] == []  # 幽霊取引(¥155)が復活しない


class TestFetchForParserSkipPersistence:
    def test_subject_mismatch_email_is_persisted_and_not_reprocessed(self, monkeypatch, tmp_path):
        _use_tmp_files(monkeypatch, tmp_path)
        parser = parsers.get("jcb")  # subject_contains='ショッピング'
        raw = _raw_b64(subject="その他のお知らせ", from_addr="statement@jcb.co.jp", body="本文")
        calls = {"get": 0}
        service = _FakeGmailService({"m1": raw})
        orig_get = service.get

        def _counting_get(*a, **kw):
            calls["get"] += 1
            return orig_get(*a, **kw)

        service.get = _counting_get
        monkeypatch.setattr(gmail_fetcher, "_get_gmail_service", lambda: service)

        gmail_fetcher.fetch_for_parser(parser, "2026/07/01")
        h = gmail_fetcher.load_history()
        assert h["transactions"] == []
        assert "m1" in h["skipped_ids"]
        assert calls["get"] == 1

        # 2回目は existing_ids に含まれるので Gmail から再取得すらされない
        gmail_fetcher.fetch_for_parser(parser, "2026/07/01")
        assert calls["get"] == 1

    def test_ignore_subjects_email_is_skipped_and_persisted(self, monkeypatch, tmp_path):
        _use_tmp_files(monkeypatch, tmp_path)
        parser = parsers.get("smcc")
        raw = _raw_b64(
            subject="お支払い金額のお知らせ【三井住友カード】",
            from_addr="statement@vpass.ne.jp",
            body="今月のお支払い金額が確定しました。",
        )
        monkeypatch.setattr(gmail_fetcher, "_get_gmail_service", lambda: _FakeGmailService({"m1": raw}))

        gmail_fetcher.fetch_for_parser(parser, "2026/07/01")
        h = gmail_fetcher.load_history()
        assert h["transactions"] == []
        assert "m1" in h["skipped_ids"]
        assert h["skipped"] == []  # 対象外は要手動確認メタには積まない

    def test_ignorable_email_is_persisted(self, monkeypatch, tmp_path):
        _use_tmp_files(monkeypatch, tmp_path)
        parser = parsers.get("smcc")
        raw = _raw_b64(
            subject="ご利用のお知らせ【三井住友カード】",
            from_addr="statement@vpass.ne.jp",
            body="VポイントPayのご利用内容をお知らせいたします。\n◇利用先　:　TEST\n◇利用金額　:　500円\n",
        )
        monkeypatch.setattr(gmail_fetcher, "_get_gmail_service", lambda: _FakeGmailService({"m1": raw}))

        gmail_fetcher.fetch_for_parser(parser, "2026/07/01")
        h = gmail_fetcher.load_history()
        assert h["transactions"] == []
        assert "m1" in h["skipped_ids"]


class TestFetchForParserSkippedMeta:
    def test_no_amount_email_recorded_with_reason_and_jst_date(self, monkeypatch, tmp_path):
        _use_tmp_files(monkeypatch, tmp_path)
        parser = parsers.get("smcc")
        raw = _raw_b64(
            subject="ご利用のお知らせ【三井住友カード】",
            from_addr="statement@vpass.ne.jp",
            body="◇利用先：どこか\n（金額の記載が無い本文）",
            date="Tue, 07 Jul 2026 23:30:00 +0900",
        )
        monkeypatch.setattr(gmail_fetcher, "_get_gmail_service", lambda: _FakeGmailService({"m1": raw}))

        gmail_fetcher.fetch_for_parser(parser, "2026/07/01")
        h = gmail_fetcher.load_history()
        assert h["transactions"] == []
        assert "m1" in h["skipped_ids"]
        assert len(h["skipped"]) == 1
        meta = h["skipped"][0]
        assert meta["id"] == "m1"
        assert meta["type"] == "smcc"
        assert meta["reason"] == "no_amount"
        assert meta["date"] == "2026-07-07"
        assert meta["subject"] == "ご利用のお知らせ【三井住友カード】"


class TestFetchForParserRawText:
    def test_store_none_transaction_gets_raw_text(self, monkeypatch, tmp_path):
        _use_tmp_files(monkeypatch, tmp_path)
        parser = parsers.get("smcc")
        raw = _raw_b64(
            subject="ご利用のお知らせ【三井住友カード】",
            from_addr="statement@vpass.ne.jp",
            body="◇利用日：2026/07/07 08:59\n◇利用取引：買物\n◇利用金額：500円\n",  # 利用先が無い
        )
        monkeypatch.setattr(gmail_fetcher, "_get_gmail_service", lambda: _FakeGmailService({"m1": raw}))

        gmail_fetcher.fetch_for_parser(parser, "2026/07/01")
        h = gmail_fetcher.load_history()
        assert len(h["transactions"]) == 1
        t = h["transactions"][0]
        assert t["store"] is None
        assert "◇利用金額：500円" in t["raw_text"]

    def test_store_found_transaction_has_no_raw_text(self, monkeypatch, tmp_path):
        _use_tmp_files(monkeypatch, tmp_path)
        parser = parsers.get("smcc")
        raw = _raw_b64(
            subject="ご利用のお知らせ【三井住友カード】",
            from_addr="statement@vpass.ne.jp",
            body="◇利用日：2026/07/07 08:59\n◇利用先：テスト店\n◇利用取引：買物\n◇利用金額：500円\n",
        )
        monkeypatch.setattr(gmail_fetcher, "_get_gmail_service", lambda: _FakeGmailService({"m1": raw}))

        gmail_fetcher.fetch_for_parser(parser, "2026/07/01")
        h = gmail_fetcher.load_history()
        assert "raw_text" not in h["transactions"][0]


class TestFetchForParserMultiTransaction:
    def test_jcb_settlement_arrival_email_registers_two_transactions(self, monkeypatch, tmp_path):
        _use_tmp_files(monkeypatch, tmp_path)
        parser = parsers.get("jcb")
        body = (
            "（売上到着分）JCBカードのご利用がありましたのでご連絡します。\n\n"
            "◆ご利用１\n【ご利用日】　2026/08/25\n【ご利用金額】　 210円\n【ご利用先】　ＮｅｗＤａｙｓ／ＫＩＯＳＫ\n\n"
            "◆ご利用２\n【ご利用日】　2026/08/25\n【ご利用金額】　 477円\n【ご利用先】　ＮｅｗＤａｙｓ／ＫＩＯＳＫ\n\n"
            "▼ご留意点\n　・国内の加盟店の場合、【ご利用先】はすべてカタカナ表示となります。\n"
        )
        raw = _raw_b64(subject="（売上到着分）JCBカード/ショッピングご利用のお知らせ", from_addr="statement@jcb.co.jp", body=body)
        monkeypatch.setattr(gmail_fetcher, "_get_gmail_service", lambda: _FakeGmailService({"multi1": raw}))

        new_count = gmail_fetcher.fetch_for_parser(parser, "2026/08/01")
        assert new_count == 2

        h = gmail_fetcher.load_history()
        assert [t["id"] for t in h["transactions"]] == ["multi1", "multi1#2"]
        assert [t["amount"] for t in h["transactions"]] == [210, 477]

        # 2回目は元の msg_id (1件目の id) が existing_ids に含まれるので再取得されない
        new_count2 = gmail_fetcher.fetch_for_parser(parser, "2026/08/01")
        assert new_count2 == 0
        h2 = gmail_fetcher.load_history()
        assert len(h2["transactions"]) == 2
