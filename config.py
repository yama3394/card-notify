"""設定と機密値の解決。

機密値はソースに書かない。解決順は「プロセスの環境変数 > env ファイル」。
env ファイルの場所は CARD_NOTIFY_ENV_FILE で上書きでき、未指定ならリポジトリ
直下の .env を読む。データ/トークンの保存先は CARD_NOTIFY_DATA_DIR で変更可能
（未指定ならリポジトリ直下の data/）。root 前提の絶対パスは持たない。
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ── env ファイルの読み込み（環境変数が優先。既存の環境変数は上書きしない） ──
_ENV_FILE = Path(os.environ.get("CARD_NOTIFY_ENV_FILE", BASE_DIR / ".env"))

# env ファイル読み込み前からプロセス環境に存在したキー。reload() でもこれらは
# 上書きしない（「プロセスの環境変数 > env ファイル」の優先順位を維持するため）。
_PROCESS_ENV_KEYS = frozenset(os.environ)


def _parse_env_file(path: Path) -> dict:
    """env ファイルを {キー: 値} に読み取る（コメント・空行は無視）。"""
    values: dict = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _load_env_file() -> None:
    for key, value in _parse_env_file(_ENV_FILE).items():
        os.environ.setdefault(key, value)


_load_env_file()


def _as_bool(value: str, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# ── パス（すべて DATA_DIR 配下。デフォルトはリポジトリ直下 data/） ──
DATA_DIR = Path(os.environ.get("CARD_NOTIFY_DATA_DIR", BASE_DIR / "data"))
HISTORY_FILE = DATA_DIR / "history.json"
STATE_FILE = DATA_DIR / "notify_state.json"
RUNTIME_STATE_FILE = DATA_DIR / "runtime_state.json"
LOGIN_FAILURES_FILE = DATA_DIR / "login_failures.json"
SETTINGS_FILE = DATA_DIR / "settings.json"          # 通知時刻など可変設定
SCHEMA_VERSION_FILE = DATA_DIR / "schema_version.json"
TOKEN_FILE = DATA_DIR / "token.json"
CREDENTIALS_FILE = DATA_DIR / "credentials.json"
SETUP_TOKEN_FILE = DATA_DIR / "setup_token"          # 初回セットアップの認可トークン
WEBHOOK_EVENTS_FILE = DATA_DIR / "webhook_events.json"  # LINE 再配達デデュープ

# ── 機密値（デフォルト値は持たない。require_secrets() で存在検証する） ──
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")

# ── 動作設定 ──
SESSION_LIFETIME_DAYS = int(os.environ.get("SESSION_LIFETIME_DAYS", "7"))
# Secure Cookie（既定 false）。HTTPS 終端済みの公開環境では true を推奨。
# 既定 true にすると HTTPS 化前の http://<LAN IP>/setup でブラウザが Cookie を
# 保存できずウィザードが進めないため false。/setup ウィザードで site_url が
# https なら自動で true が env ファイルへ書き込まれる。
SESSION_COOKIE_SECURE = _as_bool(os.environ.get("SESSION_COOKIE_SECURE"), False)

# ログイン試行ロックで使うクライアント IP の取得元ヘッダ名（既定は空＝使わない）。
# リバースプロキシが必ず上書きするヘッダ名（例 X-Real-IP）を指定したときだけ
# そのヘッダを信用する。未指定なら remote_addr のみを使う（ヘッダ偽装による
# ロック回避を防ぐため、ハードコードで信用するヘッダは持たない）。
TRUSTED_IP_HEADER = os.environ.get("CARD_NOTIFY_TRUSTED_IP_HEADER", "").strip()

# WebUI の公開URL（setup.py が記録。完了案内や将来のリンク表示に使う。任意）
SITE_URL = os.environ.get("SITE_URL", "")

# 待ち受けポート（同一ホストで複数インスタンスを動かすときに変更する）
PORT = int(os.environ.get("CARD_NOTIFY_PORT", "5000"))

# ── アプリ内スケジューラ ──
# 日次ジョブ（メール取得＋通知）はアプリ自身がバックグラウンドで回す（cron 不要）。
# 通知時刻は settings.json に保存し WebUI から変更する。
ENABLE_SCHEDULER = _as_bool(os.environ.get("CARD_NOTIFY_SCHEDULER"), True)
DEFAULT_NOTIFY_HOUR = 8
DEFAULT_NOTIFY_MINUTE = 0

# ── 自動アップデート（HTTPS フィード＝R2 等。既定 OFF のオプトイン） ──
# タイマーから auto_update.py が呼ばれても、これが true でなければ何もしない。
AUTO_UPDATE = _as_bool(os.environ.get("AUTO_UPDATE"), False)
# version.json と tar を置く配信元のベース URL（例 https://dl.example.com）。
UPDATE_FEED_URL = os.environ.get("CARD_NOTIFY_UPDATE_FEED_URL", "").rstrip("/")
# 更新 tar の Ed25519 署名検証用の公開鍵（raw 32byte を base64 したもの。既定は空）。
# 設定すると version.json の "sig"（tar への署名）を必須とし、検証失敗なら更新を
# 中止する。配信元（R2 等）が乗っ取られても改ざん tar を実行しないための防御。
# 鍵ペアは配布者が gen_signing_key.py で生成し、公開鍵を利用者へ案内する。
UPDATE_PUBKEY = os.environ.get("CARD_NOTIFY_UPDATE_PUBKEY", "").strip()
# 更新後に再起動する systemd ユニット名とヘルスチェック先。
# ヘルスチェックの既定はポート設定（CARD_NOTIFY_PORT）に連動する。
UPDATE_SERVICE = os.environ.get("CARD_NOTIFY_UPDATE_SERVICE", "card-notify")
HEALTHCHECK_URL = os.environ.get("CARD_NOTIFY_HEALTHCHECK_URL", f"http://127.0.0.1:{PORT}/login")
# アプリの設置ディレクトリ（更新時の差し替え対象）。
INSTALL_DIR = BASE_DIR

# アプリ本体のバージョン（VERSION ファイル）。
try:
    APP_VERSION = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
except OSError:
    APP_VERSION = "0.0.0"


def _load_line_user_id() -> str:
    """通知の送信先 LINE userId。初回メッセージ受信時に自動登録される。

    ネットワーク入力でソースを書き換えないよう runtime_state.json に分離。
    未登録なら空文字を返し、webhook 側が最初のメッセージでブートストラップする。
    """
    try:
        state = json.loads(RUNTIME_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return str(state.get("line_user_id", "") or "")
    except (OSError, ValueError):
        pass
    return ""


LINE_USER_ID = _load_line_user_id()


REQUIRED_SECRET_KEYS = ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "SECRET_KEY", "WEB_PASSWORD")


def _missing_secrets() -> list:
    return [key for key in REQUIRED_SECRET_KEYS if not globals().get(key)]


def is_configured() -> bool:
    """必須の機密値（LINE トークン・SECRET_KEY・WEB_PASSWORD）が揃っているか。

    揃っていなければ WebUI はセットアップモード（/setup）で起動する。
    """
    return not _missing_secrets()


def require_secrets() -> None:
    """起動時に必須の機密値が揃っているか確認する。"""
    missing = _missing_secrets()
    if missing:
        raise RuntimeError(
            f"機密値が未設定です: {', '.join(missing)}。"
            f"ブラウザで /setup を開くか `python setup.py` を実行してください"
            f"（または {_ENV_FILE} を .env.example に従って手動で用意）。"
        )


def write_secrets(values: dict) -> None:
    """秘密値を env ファイルへ書き込む（アトミック・権限600）。

    既存の env ファイルの内容は保持しつつ渡されたキーだけ上書き/追記する。
    Web セットアップウィザード（POST /setup）から呼ばれる。呼び出し後は
    reload() でプロセス内の値も更新すること。
    """
    merged = _parse_env_file(_ENV_FILE)
    merged.update(values)
    text = "".join(f"{key}={value}\n" for key, value in merged.items())

    _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _ENV_FILE.with_name(_ENV_FILE.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _ENV_FILE)
    os.chmod(_ENV_FILE, 0o600)


def reload() -> None:
    """env ファイルを再読込し、モジュールグローバルを更新する（write_secrets 後に呼ぶ）。

    起動時からプロセス環境に存在したキーは上書きしない（systemd の Environment=
    等で明示された値が env ファイルより優先、という初回読み込み時の契約を維持）。
    """
    global LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, SECRET_KEY, WEB_PASSWORD, SITE_URL, LINE_USER_ID
    for key, value in _parse_env_file(_ENV_FILE).items():
        if key in _PROCESS_ENV_KEYS and key in os.environ:
            continue
        os.environ[key] = value
    LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
    SITE_URL = os.environ.get("SITE_URL", "")
    LINE_USER_ID = _load_line_user_id()
