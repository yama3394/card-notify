"""JCB カードの「ショッピングご利用のお知らせ」メールパーサ。

MyJCB Pay の重複通知は件名フィルタ(subject_contains='ショッピング')で除外する。
"""
from .base import CardParser, register


def _is_cancellation(text: str) -> bool:
    # 「取消」という1語だけでの判定は、本文中に無関係な「取消」を含む通常の
    # 購入通知（本文の説明文等）まで取消と誤判定しうるため、実際のJCB取消
    # メールに現れる固定文言「ショッピングの取消」または金額欄の「（取消）」
    # のどちらかを含む場合だけを取消とみなす。
    return "ショッピングの取消" in text or "（取消）" in text


def _is_ignorable(text: str) -> bool:
    return "利用不可" in text[:500]


register(CardParser(
    key="jcb",
    label="JCB",
    from_addrs=["jcb.co.jp"],
    subject_contains="ショッピング",
    amount_markers=["【ご利用金額】", "【金額】"],
    date_markers=[r"【ご利用日時[^】]*】", r"【日時[^】]*】", r"【ご利用日】"],
    store_patterns=[
        r"【ご利用先】\s*(.+?)[\n\r]",
        r"利用店名[：:\s]+(.+?)[\n\r]",
        r"加盟店名[：:\s]+(.+?)[\n\r]",
    ],
    is_cancellation=_is_cancellation,
    is_ignorable=_is_ignorable,
    # 件名フィルタ(subject_contains='ショッピング')で概ね除外済みだが保険で明示除外
    ignore_subjects=["ご利用不可", "お振替内容確定"],
    strict_amount=True,  # 見出し語が無ければ拾い読みしない（幽霊取引防止）
    # 「（売上到着分）」メールは1通に ◆ご利用１ ◆ご利用２ … と複数取引が入りうる
    split_pattern=r"◆ご利用[０-９0-9]+",
))
