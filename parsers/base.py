"""カード発行会社ごとのメールパーサの基盤とレジストリ。

新しいカード/銀行に対応するには parsers/<issuer>.py を追加し、`CardParser`
を作って `register()` するだけでよい（gmail_fetcher 側の変更は不要）。共通の
金額/日付/店舗の抽出は下のヘルパーで賄い、発行会社ごとの差分（フィールドの
見出し語・取消/対象外の判定）だけを各パーサが与える。
"""
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

JST = timezone(timedelta(hours=9))
_NOTES_SPLIT_RE = re.compile(r"例[）)】：:]|▼ご留意点|＜注意事項＞")


@dataclass
class ParseResult:
    amount: int | float
    currency: str            # 'JPY' または 'KRW' 等の3文字コード
    date: str                # 'YYYY-MM-DD'
    store: str | None


# ── 共通抽出ヘルパー（見出し語を引数で受けて発行会社差を吸収） ─────────────

def _to_number(raw: str) -> int | float:
    val = float(raw.replace(",", ""))
    return int(val) if val == int(val) else val


def parse_amount_currency(text: str, field_markers: list[str], strict: bool = False) -> tuple:
    """(金額, 通貨コード) を返す。抽出できなければ (None, None)。

    海外決済は末尾に通貨コードが付く（例: `◇利用金額：10,950.00KRW`）。
    国内は `◇利用金額：1,200円` / `【ご利用金額】 1,380円` のように円。
    取消メールは `【金額】- 430円（取消）` のように見出し語の直後にマイナス
    記号が入ることがあるが、数字そのものにはマイナスを含めない（＝取消額も
    常に正の数として返し、呼び出し側で符号を気にしなくてよいようにする）。

    strict=True の場合、見出し語にマッチしなければ以下のフォールバックの
    拾い読み（`¥1,000` 等、文中の金額らしき数字を無条件に採用する）を行わず
    None を返す。販促メール等に紛れ込んだ金額らしき数字を取引と誤認しない
    ようにするため、SMCC/JCB など実データ抽出には strict_amount=True を使う。
    """
    # 全角数字・全角カンマ・全角ピリオドを半角に正規化してから読む
    # （◇【】などの記号や漢字はNFKCの影響を受けないので安全）。
    text = unicodedata.normalize("NFKC", text)

    marker_alt = "|".join(re.escape(m) for m in field_markers)
    amount_re = rf"(?:{marker_alt})\s*[：:\s]*-?\s*([¥￥]?[\d,\.]+)\s*([A-Z]{{3}}|円|JPY)?"

    # 実取引データは「例）...」等の説明文より前に出るのが通例。見出し語が
    # 説明文中に再度現れて誤マッチしないよう、まず説明文より前の本文だけを
    # 探し、見つからなければ全文にフォールバックする。
    body = _NOTES_SPLIT_RE.split(text)[0]
    field_m = re.search(amount_re, body) or re.search(amount_re, text)
    if field_m:
        numeric = re.sub(r"[¥￥\s]", "", field_m.group(1))
        unit = field_m.group(2)
        try:
            if unit is None or unit in ("円", "JPY"):
                return int(float(numeric.replace(",", ""))), "JPY"
            return _to_number(numeric), unit
        except ValueError:
            pass

    if strict:
        return None, None

    # フォールバック（フィールド欠落時）はすべて日本円扱い
    patterns = [
        r"[¥￥](\d{1,3}(?:,\d{3})*)",
        r"(\d{1,3}(?:,\d{3})+)円",
        r"(\d+)円",
        r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*JPY",
    ]
    for pattern in patterns:
        m = re.search(pattern, body)
        if m:
            return int(float(m.group(1).replace(",", ""))), "JPY"
    return None, None


def parse_date(text: str, field_markers: list[str]) -> str:
    marker_alt = "|".join(field_markers)  # 見出しは正規表現片を許容
    field_m = re.search(
        rf"(?:{marker_alt})\s*[：:\s]*(\d{{4}}[/\-]\d{{1,2}}[/\-]\d{{1,2}})",
        text,
    )
    if field_m:
        parts = re.split(r"[/\-]", field_m.group(1))
        return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"

    # フォールバック: 見出し語で日付が見つからない場合のみ、説明文より前の
    # 本文から日付らしき数字列を探す。「お問い合わせ番号：2024-01-15-9981」
    # のような管理番号を誤って日付と解釈しないよう、末尾がさらに
    # ハイフン+数字で続く場合は除外する。
    body = _NOTES_SPLIT_RE.split(text)[0]
    patterns = [
        r"(\d{4})[年/\-](\d{1,2})[月/\-](\d{1,2})(?!\d)[日]?(?!\s*[\-–]\s*\d)",
        r"(?<!\d)(\d{2})[/\-](\d{1,2})[/\-](\d{1,2})(?!\d)(?!\s*[\-–]\s*\d)",
    ]
    for pattern in patterns:
        m = re.search(pattern, body)
        if m:
            g = m.groups()
            try:
                year = (2000 + int(g[0])) if len(g[0]) == 2 else int(g[0])
                return f"{year}-{int(g[1]):02d}-{int(g[2]):02d}"
            except (ValueError, IndexError):
                pass
    return datetime.now(JST).strftime("%Y-%m-%d")


def parse_store(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return None


# ── パーサ本体とレジストリ ─────────────────────────────────────────────

class CardParser:
    """1発行会社ぶんのメール解釈ルール。"""

    def __init__(
        self,
        key: str,
        label: str,
        from_addrs: list[str],
        amount_markers: list[str],
        date_markers: list[str],
        store_patterns: list[str],
        is_cancellation: Callable[[str], bool],
        is_ignorable: Callable[[str], bool],
        subject_contains: str | None = None,
        ignore_subjects: list[str] | None = None,
        strict_amount: bool = False,
        split_pattern: str | None = None,
    ):
        self.key = key                       # 内部種別（'smcc' 等、history に保存）
        self.label = label                   # 表示名（'SMCC' 等）
        self.from_addrs = from_addrs         # 差出人アドレス（部分一致）
        self.subject_contains = subject_contains
        self.ignore_subjects = ignore_subjects or []  # 件名に含まれれば対象外（保険的フィルタ）
        self.amount_markers = amount_markers
        self.date_markers = date_markers
        self.store_patterns = store_patterns
        self.strict_amount = strict_amount   # True で金額フォールバックの拾い読みを禁止
        self.split_pattern = split_pattern   # 1通に複数取引が入る形式のブロック区切り正規表現
        self._is_cancellation = is_cancellation
        self._is_ignorable = is_ignorable

    def is_cancellation(self, text: str) -> bool:
        return self._is_cancellation(text)

    def is_ignorable(self, text: str) -> bool:
        return self._is_ignorable(text)

    def parse(self, text: str) -> ParseResult | None:
        amount, currency = parse_amount_currency(text, self.amount_markers, self.strict_amount)
        if not amount:
            return None
        return ParseResult(
            amount=amount,
            currency=currency,
            date=parse_date(text, self.date_markers),
            store=parse_store(text, self.store_patterns),
        )

    def parse_all(self, text: str) -> list[ParseResult]:
        """1通のメールに複数取引が含まれる場合（JCB「売上到着分」等）に対応する。

        split_pattern が無ければ従来どおり parse() の結果を最大1件返す。ある
        場合はブロックごとに parse() し、共通の注意書きなど金額見出しが無い
        ブロックは parse() が None を返すので自然に無視される。
        """
        if not self.split_pattern:
            r = self.parse(text)
            return [r] if r is not None else []
        blocks = re.split(self.split_pattern, text)
        return [r for r in (self.parse(b) for b in blocks) if r is not None]


_registry: dict[str, CardParser] = {}


def register(parser: CardParser) -> None:
    _registry[parser.key] = parser


def all_parsers() -> list[CardParser]:
    return list(_registry.values())


def get(key: str) -> CardParser | None:
    return _registry.get(key)


def type_labels() -> dict[str, str]:
    """種別キー→表示名。現金('cash')も含める。"""
    labels = {p.key: p.label for p in _registry.values()}
    labels.setdefault("cash", "現金")
    return labels


def type_keys() -> list[str]:
    """全種別キーの順序付きリスト（パーサ登録順 + 末尾に 'cash'）。

    集計・通知・画面の表示順の基準。type_labels() と同じキー集合を返す。
    """
    keys = [p.key for p in _registry.values()]
    if "cash" not in keys:
        keys.append("cash")
    return keys
