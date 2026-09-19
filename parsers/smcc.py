"""三井住友カード(SMCC / Vpass)の利用通知メールパーサ。

差し替えの参考例。自分の使うカードに合わせてこのファイルを複製し、見出し語・
差出人・取消/対象外の判定を書き換えれば新しい発行会社に対応できる。
"""
import re

from .base import CardParser, register


def _is_cancellation(text: str) -> bool:
    return "◇利用取引：取消" in text


_DECLINED_RE = re.compile(r"(いただけ|頂け)(なかった|ませんでした)")


def _is_ignorable(text: str) -> bool:
    head = text[:500]
    return bool(
        "利用不可" in head
        or "残高不足" in head
        or _DECLINED_RE.search(head)
        or "VポイントPay" in head
        or "ＪＣＢカード" in head
        or "JCBカード" in head
    )


register(CardParser(
    key="smcc",
    label="SMCC",
    from_addrs=["statement@vpass.ne.jp", "smbc-card.com"],
    amount_markers=["◇利用金額"],
    date_markers=[r"◇利用日"],
    store_patterns=[r"◇利用先[：:\s]+(.+?)[\n\r]"],
    is_cancellation=_is_cancellation,
    is_ignorable=_is_ignorable,
    # 金額の記載が無い/取引ではない通知（件名フィルタで確実に除くための保険）
    ignore_subjects=["お支払い金額のお知らせ", "カードがご利用いただけませんでした"],
    strict_amount=True,  # 見出し語が無ければ拾い読みしない（幽霊取引防止）
))
