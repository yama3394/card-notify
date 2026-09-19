"""発行会社パーサ（SMCC/JCB）の解釈テスト。本文サンプルはマスク済み。"""
import parsers

smcc = parsers.get("smcc")
jcb = parsers.get("jcb")

SMCC_NORMAL = """\
ヤマ　様
◇利用日：2026/05/28 07:12
◇利用先：モバイルＳｕｉｃａ（Ａｐｐｌｅ）
◇利用取引：買物
◇利用金額：500円
"""

SMCC_CANCELLATION = """\
◇利用日：2026/05/27 14:30
◇利用先：イオンモール
◇利用取引：取消
◇利用金額：3,000円
"""

SMCC_LARGE_AMOUNT = """\
◇利用日：2026/05/22 10:00
◇利用先：Trip.com
◇利用取引：買物
◇利用金額：24,350円
"""

JCB_WITH_EXAMPLE_TEXT = """\
カード名称　：　【ＯＳ】ＪＣＢカードＷ　ＮＬ
【ご利用日時(日本時間)】　2026/05/28 07:12
【ご利用金額】　11円
【ご利用先】　スイカ　ケイタイケツサイ

▼会費やサブスクリプションのお支払い
　　例）Amazonプライム年会費　5,900円　→　アマゾン　5,900円
"""

VPOINTPAY_NORMAL = """\
VポイントPayのご利用内容をお知らせいたします。
◇利用先　:　MOBILE SUICA APPLE
◇利用金額　:　697円
"""

VPOINTPAY_DECLINED = """\
お客様がご利用のVポイントPayは、以下の理由によりご利用頂けませんでした。
残高不足
◇利用先　:　AEONMALLSHINRIFU
◇利用金額　:　572円
"""

# JCB 取消（通常の「ショッピングご利用のお知らせ」とは見出し語が異なる：
# 日時は【日時（日本時間）】、金額は【金額】で、マイナス表記＋「（取消）」が付く）
JCB_CANCELLATION = """\
高田　篤 様
JCBカードでのショッピングの取消がありましたので、ご連絡します。

【日時（日本時間）】　2026/07/21 03:20
【金額】- 430円（取消）
【ご利用先】　マクドナルドモバイルオ－ダ－
"""

# JCB「（売上到着分）」: 1通に複数取引（◆ご利用１・◆ご利用２…）が入る
JCB_SETTLEMENT_ARRIVAL = """\
高田　篤 様
（売上到着分）JCBカードのご利用がありましたのでご連絡します。

◆ご利用１
【ご利用日】　2026/08/25
【ご利用金額】　 210円
【ご利用先】　ＮｅｗＤａｙｓ／ＫＩＯＳＫ

◆ご利用２
【ご利用日】　2026/08/25
【ご利用金額】　 477円
【ご利用先】　ＮｅｗＤａｙｓ／ＫＩＯＳＫ

▼ご留意点
　・国内の加盟店の場合、【ご利用先】はすべてカタカナ表示となります。
"""

# SMCC本体の利用不可通知（「利用不可」という文字列を含まないため見落とされていた実例）
SMCC_DECLINED_OVER_LIMIT = """\
カードがご利用いただけなかったお取引がございましたのでお知らせいたします。

◇利用日：2026/07/13 12:36
◇利用先：ＳＢＩ証券投信積立サービス
◇利用取引：買物
◇利用金額：6,000円

お客様のカードご利用枠を超えているため、ご利用いただけませんでした。
"""


class TestAmount:
    def test_smcc_plain_yen(self):
        assert smcc.parse(SMCC_NORMAL).amount == 500
        assert smcc.parse(SMCC_NORMAL).currency == "JPY"

    def test_smcc_comma(self):
        assert smcc.parse(SMCC_LARGE_AMOUNT).amount == 24350

    def test_smcc_cancellation_amount(self):
        assert smcc.parse(SMCC_CANCELLATION).amount == 3000

    def test_jcb_field_beats_example_text(self):
        # 例文の 5,900円 ではなく 11円 が正解
        assert jcb.parse(JCB_WITH_EXAMPLE_TEXT).amount == 11

    def test_empty_returns_none(self):
        assert smcc.parse("ご利用明細はございません。") is None

    def test_smcc_foreign_krw(self):
        r = smcc.parse("◇利用先：CJ OLIVE YOUNG\n◇利用金額：10,950.00KRW\n")
        assert (r.amount, r.currency) == (10950, "KRW")

    def test_smcc_foreign_usd_decimals(self):
        r = smcc.parse("◇利用金額：12.50USD\n")
        assert (r.amount, r.currency) == (12.5, "USD")

    def test_fullwidth_comma_amount(self):
        # 全角カンマ/全角数字で桁落ちしないこと（１，２００円 → 1円 になっていたバグ）
        r = smcc.parse("◇利用金額：１，２００円")
        assert (r.amount, r.currency) == (1200, "JPY")

    def test_marker_duplicated_in_notes_takes_real_value(self):
        # 見出し語が実データの後の説明文中に再度出現しても、実データ側を優先する
        text = (
            "◇利用日：2026/05/22 10:00\n◇利用先：Trip.com\n◇利用取引：買物\n◇利用金額：24,350円\n"
            "▼ご留意点\n　・注記です（例：◇利用金額：500円と表示される場合があります）\n"
        )
        r = smcc.parse(text)
        assert r.amount == 24350


class TestDate:
    def test_smcc(self):
        assert smcc.parse(SMCC_NORMAL).date == "2026-05-28"

    def test_smcc_cancellation(self):
        assert smcc.parse(SMCC_CANCELLATION).date == "2026-05-27"

    def test_jcb(self):
        assert jcb.parse(JCB_WITH_EXAMPLE_TEXT).date == "2026-05-28"

    def test_fallback_does_not_misread_reference_number_as_date(self):
        # 「お問い合わせ番号：2024-01-15-9981」のような管理番号を日付と誤認しない
        from datetime import datetime, timedelta, timezone

        from parsers.base import parse_date

        JST = timezone(timedelta(hours=9))
        text = "お問い合わせ番号：2024-01-15-9981\nご利用ありがとうございました。"
        assert parse_date(text, [r"◇利用日"]) == datetime.now(JST).strftime("%Y-%m-%d")


class TestStore:
    def test_smcc(self):
        assert smcc.parse(SMCC_NORMAL).store == "モバイルＳｕｉｃａ（Ａｐｐｌｅ）"

    def test_jcb(self):
        assert jcb.parse(JCB_WITH_EXAMPLE_TEXT).store == "スイカ　ケイタイケツサイ"


class TestCancellation:
    def test_smcc_cancel(self):
        assert smcc.is_cancellation(SMCC_CANCELLATION) is True

    def test_smcc_normal(self):
        assert smcc.is_cancellation(SMCC_NORMAL) is False

    def test_jcb_normal(self):
        assert jcb.is_cancellation(JCB_WITH_EXAMPLE_TEXT) is False

    def test_jcb_cancellation_detected_even_with_long_preamble(self):
        # 先頭300文字に収まらない長い挨拶文でも取消判定できること
        # （取りこぼすと取消のはずの取引が新規購入として二重加算されていた）
        preamble = 'あ' * 320
        text = preamble + '\nJCBカードでのショッピングの取消がありましたので、ご連絡します。\n【ご利用先】テスト店\n'
        assert jcb.is_cancellation(text) is True

    def test_jcb_real_cancellation_format_detected(self):
        assert jcb.is_cancellation(JCB_CANCELLATION) is True

    def test_jcb_bare_torikeshi_word_alone_is_not_cancellation(self):
        # 「取消」という1語だけでは判定しない（本文中の説明文等で無関係な
        # 購入通知まで取消と誤判定しないようにするための厳密化）
        text = '【ご利用先】テスト店\n【ご利用金額】1,000円\nお取消しになる場合は…\n'
        assert jcb.is_cancellation(text) is False


class TestIgnorable:
    def test_vpointpay_ignored_for_smcc(self):
        assert smcc.is_ignorable(VPOINTPAY_NORMAL) is True

    def test_vpointpay_declined_ignored_for_smcc(self):
        assert smcc.is_ignorable(VPOINTPAY_DECLINED) is True

    def test_jcb_body_ignored_for_smcc(self):
        assert smcc.is_ignorable(JCB_WITH_EXAMPLE_TEXT) is True

    def test_smcc_normal_not_ignorable(self):
        assert smcc.is_ignorable(SMCC_NORMAL) is False

    def test_jcb_declined_ignored_for_jcb(self):
        assert jcb.is_ignorable("利用不可のお知らせ\n【ご利用金額】1,000円") is True

    def test_smcc_declined_over_limit_ignored(self):
        # 実際に加算されてしまっていたバグ（「利用不可」の文字列を含まない利用不可通知）
        assert smcc.is_ignorable(SMCC_DECLINED_OVER_LIMIT) is True


# ---------------------------------------------------------------------------
# JCB 取消メールの金額・日付・店舗（見出し語が通常と異なる書式）
# ---------------------------------------------------------------------------

class TestJcbCancellationFormat:
    def test_amount_is_positive_despite_minus_notation(self):
        # 【金額】- 430円（取消） のようにマイナス表記でも常に正の数で返す
        r = jcb.parse(JCB_CANCELLATION)
        assert r is not None
        assert r.amount == 430
        assert r.currency == "JPY"

    def test_date_field(self):
        assert jcb.parse(JCB_CANCELLATION).date == "2026-07-21"

    def test_store_field(self):
        assert jcb.parse(JCB_CANCELLATION).store == "マクドナルドモバイルオ－ダ－"


# ---------------------------------------------------------------------------
# strict_amount: 見出し語が無ければフォールバックの拾い読みをしない
# ---------------------------------------------------------------------------

class TestStrictAmount:
    def test_non_strict_falls_back_to_bare_yen_amount(self):
        from parsers.base import parse_amount_currency
        assert parse_amount_currency("本日限定！¥1,000相当プレゼント", ["◇利用金額"]) == (1000, "JPY")

    def test_strict_returns_none_without_marker(self):
        from parsers.base import parse_amount_currency
        assert parse_amount_currency(
            "本日限定！¥1,000相当プレゼント", ["◇利用金額"], strict=True
        ) == (None, None)

    def test_smcc_parser_ignores_phantom_amount_without_marker(self):
        # smcc/jcb は strict_amount=True。見出し語の無い販促文中の金額を
        # 取引として誤登録しない（幽霊取引の防止）。
        promo = "本日限定！Amazonギフト券 ¥1,000 プレゼントキャンペーン実施中！"
        assert smcc.parse(promo) is None
        assert jcb.parse(promo) is None


# ---------------------------------------------------------------------------
# parse_all / split_pattern: 1通に複数取引が入るメール（JCB「売上到着分」）
# ---------------------------------------------------------------------------

class TestParseAll:
    def test_single_transaction_email_returns_one_result(self):
        results = smcc.parse_all(SMCC_NORMAL)
        assert len(results) == 1
        assert results[0].amount == 500

    def test_no_amount_email_returns_empty_list(self):
        assert smcc.parse_all("ご利用明細はございません。") == []

    def test_settlement_arrival_splits_into_two_transactions(self):
        results = jcb.parse_all(JCB_SETTLEMENT_ARRIVAL)
        assert [r.amount for r in results] == [210, 477]
        assert all(r.date == "2026-08-25" for r in results)
        assert all(r.store == "ＮｅｗＤａｙｓ／ＫＩＯＳＫ" for r in results)


# ---------------------------------------------------------------------------
# ignore_subjects: 件名ベースの対象外判定（属性の存在と内容の確認）
# ---------------------------------------------------------------------------

class TestIgnoreSubjects:
    def test_smcc_ignore_subjects(self):
        assert "お支払い金額のお知らせ" in smcc.ignore_subjects
        assert "カードがご利用いただけませんでした" in smcc.ignore_subjects

    def test_jcb_ignore_subjects(self):
        assert "ご利用不可" in jcb.ignore_subjects
        assert "お振替内容確定" in jcb.ignore_subjects


# ---------------------------------------------------------------------------
# パーサ自動探索: parsers/<issuer>.py を置くだけで register() されること
# ---------------------------------------------------------------------------

class TestAutoDiscovery:
    def test_new_module_is_auto_registered_without_editing_init(self):
        import importlib
        import sys
        from pathlib import Path

        parsers_dir = Path(parsers.__file__).parent
        mod_path = parsers_dir / "zz_test_autodiscover_card.py"
        mod_name = "parsers.zz_test_autodiscover_card"
        saved_registry = dict(parsers.base._registry)
        mod_path.write_text(
            "from .base import CardParser, register\n"
            "register(CardParser(\n"
            "    key='zzdummy', label='ZZDummy', from_addrs=['x@example.com'],\n"
            "    amount_markers=['利用金額'], date_markers=[r'利用日'],\n"
            "    store_patterns=[r'利用先[：:\\s]+(.+?)[\\n\\r]'],\n"
            "    is_cancellation=lambda t: False, is_ignorable=lambda t: False,\n"
            "))\n",
            encoding="utf-8",
        )
        try:
            sys.modules.pop(mod_name, None)
            importlib.reload(parsers)
            assert parsers.get("zzdummy") is not None
        finally:
            mod_path.unlink(missing_ok=True)
            sys.modules.pop(mod_name, None)
            parsers.base._registry.clear()
            parsers.base._registry.update(saved_registry)

    def test_leading_underscore_module_is_skipped(self):
        import importlib
        import sys
        from pathlib import Path

        parsers_dir = Path(parsers.__file__).parent
        mod_path = parsers_dir / "_zz_test_private_helper.py"
        mod_name = "parsers._zz_test_private_helper"
        saved_registry = dict(parsers.base._registry)
        # 探索対象なら import されて SyntaxError で reload が落ちるはずの壊れた内容
        mod_path.write_text("this is not valid python !!!\n", encoding="utf-8")
        try:
            sys.modules.pop(mod_name, None)
            importlib.reload(parsers)  # 例外なく完了する = 先頭 "_" は import されていない
        finally:
            mod_path.unlink(missing_ok=True)
            sys.modules.pop(mod_name, None)
            parsers.base._registry.clear()
            parsers.base._registry.update(saved_registry)
