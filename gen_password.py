"""WebUI ログインパスワードのハッシュを生成する。

使い方:
    python gen_password.py '好きなパスワード'
出力された scrypt:... の行を .env の WEB_PASSWORD に貼り付ける。
"""
import sys

from werkzeug.security import generate_password_hash


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1]:
        sys.exit("使い方: python gen_password.py '好きなパスワード'")
    print(generate_password_hash(sys.argv[1]))


if __name__ == "__main__":
    main()
