"""初回セットアップウィザード。

対話形式で WebUI パスワード・LINE トークンを受け取り、SECRET_KEY を自動生成して
.env（権限 600）を書き出す。続けて Gmail OAuth まで案内する。

    python setup.py            # 初回セットアップ（対話ウィザード）
    python setup.py --upgrade  # 既存 .env に不足キーだけ補う（アップグレード用）

.env の場所は CARD_NOTIFY_ENV_FILE、データ保存先は CARD_NOTIFY_DATA_DIR に従う
（未指定ならリポジトリ直下の .env / data/）。既存の .env は確認の上で上書きする。
"""
import getpass
import os
import secrets
import subprocess
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = Path(os.environ.get("CARD_NOTIFY_ENV_FILE", BASE_DIR / ".env"))
DATA_DIR = Path(os.environ.get("CARD_NOTIFY_DATA_DIR", BASE_DIR / "data"))
CREDENTIALS_FILE = DATA_DIR / "credentials.json"


def _hr(title: str) -> None:
    print("\n" + "─" * 56)
    print(title)
    print("─" * 56)


def _ask(prompt: str, *, required: bool = True, secret: bool = False) -> str:
    reader = getpass.getpass if secret else input
    while True:
        try:
            value = reader(prompt).strip()
        except EOFError:
            print("\n入力が中断されました。中止します。")
            sys.exit(1)
        if value or not required:
            return value
        print("  ⚠️ 空にできません。もう一度入力してください。")


def _ask_password() -> str:
    while True:
        pw = _ask("WebUI ログインパスワード: ", secret=True)
        if len(pw) < 8:
            print("  ⚠️ 8文字以上を推奨します。もう一度。")
            continue
        confirm = _ask("確認のためもう一度: ", secret=True)
        if pw != confirm:
            print("  ⚠️ 一致しません。最初からやり直してください。")
            continue
        return pw


def _confirm(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        ans = input(prompt + suffix).strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def _write_env(values: dict) -> None:
    lines = [
        "# card-notify 設定（setup.py が生成）。この .env はコミットしないこと。",
        "",
        "# LINE Messaging API",
        f"LINE_CHANNEL_ACCESS_TOKEN={values['LINE_CHANNEL_ACCESS_TOKEN']}",
        f"LINE_CHANNEL_SECRET={values['LINE_CHANNEL_SECRET']}",
        "",
        "# WebUI ログインパスワード（ハッシュ）",
        f"WEB_PASSWORD={values['WEB_PASSWORD']}",
        "",
        "# Flask セッション署名鍵（自動生成）",
        f"SECRET_KEY={values['SECRET_KEY']}",
        "",
    ]
    if values.get("SITE_URL"):
        lines += [
            "# WebUI の公開URL（完了案内やリンク表示に使用）",
            f"SITE_URL={values['SITE_URL']}",
            "",
        ]
    # 先に 600 で作成してから書き込む（内容が一瞬でも 644 で晒されないように）
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.chmod(ENV_FILE, 0o600)


REQUIRED_KEYS = ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "WEB_PASSWORD")


def _parse_env(path: Path) -> dict:
    """既存 .env を {キー: 値} に読み取る（コメント・空行は無視）。"""
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


def _atomic_write(path: Path, text: str) -> None:
    """権限 600 を保ったままアトミックに書き戻す（同ディレクトリの一時ファイル経由）。"""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def upgrade() -> None:
    """既存 .env を保ちつつ不足キーだけ補う（アップグレード用・非破壊）。

    既存の値は一切上書きしない。SECRET_KEY が無ければ生成して追記する。
    必須キーが欠けていれば警告のみ表示する（対話でも非対話でも壊さず終了コード 0）。
    """
    _hr("card-notify 設定のアップグレード（--upgrade）")
    print(f".env: {ENV_FILE}")

    if not ENV_FILE.exists():
        print(f"\n⚠️ .env が見つかりません: {ENV_FILE}")
        print("初回セットアップは引数なしの `python setup.py` を実行してください。")
        sys.exit(0)

    existing = _parse_env(ENV_FILE)

    if existing.get("SECRET_KEY"):
        print("• SECRET_KEY: 既存の値を保持します。")
    else:
        secret_key = secrets.token_hex(32)
        original = ENV_FILE.read_text(encoding="utf-8")
        if not original.endswith("\n"):
            original += "\n"
        addition = (
            "\n# Flask セッション署名鍵（--upgrade が自動生成）\n"
            f"SECRET_KEY={secret_key}\n"
        )
        _atomic_write(ENV_FILE, original + addition)
        print("• SECRET_KEY: 無かったので生成して追記しました（権限 600 維持）。")

    current = _parse_env(ENV_FILE)
    missing = [key for key in REQUIRED_KEYS if not current.get(key)]
    if missing:
        print("\n⚠️ 必須キーが未設定です: " + ", ".join(missing))
        if sys.stdin.isatty():
            print("`python setup.py`（引数なし）で再設定するか、.env を直接編集してください。")
        else:
            print("（非対話実行のため警告のみ。.env を手動で補ってください）")
    else:
        print("\n✅ 必須キーは揃っています。アップグレード後の設定に問題はありません。")

    sys.exit(0)


def main() -> None:
    _hr("card-notify 初回セットアップ")
    print(f".env の書き出し先 : {ENV_FILE}")
    print(f"データ保存先      : {DATA_DIR}")

    if ENV_FILE.exists():
        print(f"\n⚠️ 既存の設定ファイルがあります: {ENV_FILE}")
        if not _confirm("上書きしますか？（既存の値は失われます）"):
            print("中止しました。")
            sys.exit(0)

    _hr("1) WebUI パスワード")
    print("ブラウザの管理画面にログインするためのパスワードを決めてください。")
    password_hash = generate_password_hash(_ask_password())

    _hr("2) LINE Messaging API")
    print("LINE Developers で作成したチャンネルの値を貼り付けてください。")
    print("（後で .env を直接編集して変更もできます）")
    access_token = _ask("チャンネルアクセストークン: ")
    channel_secret = _ask("チャンネルシークレット: ")

    _hr("3) WebUI のアクセス先")
    print("ブラウザで管理画面を開く URL です。公開ドメインがあれば入力してください。")
    print("（例: https://card.example.com  ／ 未入力なら http://127.0.0.1:5000 を表示）")
    site_url = _ask("WebUI の URL: ", required=False).rstrip("/")

    _hr("4) セッション鍵")
    secret_key = secrets.token_hex(32)
    print("SECRET_KEY を自動生成しました。")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _write_env({
        "LINE_CHANNEL_ACCESS_TOKEN": access_token,
        "LINE_CHANNEL_SECRET": channel_secret,
        "WEB_PASSWORD": password_hash,
        "SECRET_KEY": secret_key,
        "SITE_URL": site_url,
    })
    print(f"\n✅ 設定を書き出しました（権限 600）: {ENV_FILE}")
    print(f"✅ データディレクトリを用意しました: {DATA_DIR}")

    _hr("5) Gmail 連携")
    if CREDENTIALS_FILE.exists():
        print(f"credentials.json を検出しました: {CREDENTIALS_FILE}")
        if _confirm("続けて Gmail の OAuth 認証を実行しますか？", default=True):
            print()
            rc = subprocess.run([sys.executable, str(BASE_DIR / "oauth_setup.py")]).returncode
            if rc != 0:
                print("\n⚠️ OAuth 認証が完了しませんでした。後で `python oauth_setup.py` を再実行してください。")
    else:
        print("credentials.json がまだありません。")
        print("Google Cloud で OAuth クライアント(デスクトップ)を作成して")
        print(f"  {CREDENTIALS_FILE}")
        print("に置いてから、`python oauth_setup.py` を実行してください（README 参照）。")

    web_url = site_url or "http://127.0.0.1:5000"
    _hr("セットアップ完了 🎉")
    print("┌────────────────────────────────────────────────────")
    print("│  WebUI を開く:")
    print(f"│    → {web_url}")
    print("│")
    print("│  上で決めたパスワードでログインし、［設定］から")
    print("│  毎日の通知時刻を設定できます。")
    print("└────────────────────────────────────────────────────")
    if not site_url:
        print("\n※ このURLはローカル用です。別PCのブラウザから開くときは、")
        print("  リバースプロキシで公開したドメイン（https://...）を使ってください。")
    print("\n仕上げ（未実施なら）:")
    print("  • 起動:     python app.py  ／ もしくは systemctl start card-notify")
    print("  • 公開:     deploy/nginx.conf.example を参考に HTTPS 化")
    print("  • 通知時刻: WebUI の［設定］から変更できます（cron 不要）")


if __name__ == "__main__":
    if "--upgrade" in sys.argv[1:]:
        upgrade()
    else:
        main()
