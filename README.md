# card-notify

クレジットカードの利用通知メール（Gmail）を自動取得し、毎日 LINE に支出サマリを
プッシュ通知するセルフホスト型の家計管理ツール。現金支出は LINE から手入力でき、
ブラウザの WebUI でも確認・編集できる。**各自が自分の Google / LINE / サーバーで
動かす**構成なので、他人のデータを預かるサービスではない。

- 日次 / 週次 / 月次の集計と LINE 通知
- WebUI（ログイン制）でダッシュボード・取引一覧・CSV エクスポート・**通知時刻の設定**
- 外貨決済に対応（円合計に混ぜず通貨別に集計）
- **カード会社ごとのメール解釈はプラグイン式**（`parsers/` に1ファイル追加するだけ）
- **通知はアプリ内スケジューラで実行**（cron 不要。時刻は WebUI から変更）
- **自動アップデート対応**（配信元をポーリングし、安全に更新・失敗時ロールバック。既定 OFF）

> ⚠️ このツールは自分の受信箱と金融情報を扱う。無保証（MIT）。公開リポジトリに
> `.env` / `credentials.json` / `token.json` / `data/` を**絶対にコミットしない**
> こと（`.gitignore` 済み）。

---

## 必要環境
- Linux（`fcntl` によるファイルロックを使用）
- Python 3.10 以上
- リバースプロキシ（nginx 等）で HTTPS 終端できること（LINE Webhook は HTTPS 必須）
- **常時インターネット接続**（Gmail・LINE との通信、証明書更新のため）

常時起動はするが、実際の処理は1日数回のメール取得と LINE 送信だけ。DB は使わず
軽量な JSON ファイルに保存するため、要求スペックは控えめ。目安は以下のとおり:

| 項目 | オンプレミス（自宅サーバー・VPS） | AWS |
|---|---|---|
| CPU | 1 vCPU（x86_64 / ARM64 どちらでも可） | `t4g.micro`（Arm, 2vCPU）または `t3.micro`（x86, 2vCPU） |
| メモリ | 1GB 以上（512MB でも動作） | 1GB（上記インスタンスタイプ標準） |
| ディスク | 空き 5GB 以上 | EBS gp3 8GB 程度 |
| OS | Linux（Debian / Ubuntu 系推奨）・systemd 必須 | Ubuntu Server AMI または Amazon Linux 2023 |
| ネットワーク | 固定 IP または DDNS、443/80 番ポート開放、独自ドメイン | Elastic IP 1つ、SG で 443/80 を開放（22 は管理者 IP のみ） |

AWS では固定月額プランの Lightsail でも同程度のスペックで問題なく動作する。

## かんたんインストール（配布版）
配信元を用意している場合は、ワンライナーで導入できる（専用ユーザー作成・venv・
依存導入・systemd 登録まで自動。対話的なウィザードはここでは実行しない）:
```bash
curl -fsSL https://card-notify.yama3394.uk/install.sh | bash
```
フォークして自分で配信する場合は `install.sh` 冒頭の `FEED_URL` を差し替える。
cron の設定は不要（通知はアプリ内スケジューラが実行）。

インストールが終わりサービスが起動すると、秘密情報（WebUI パスワードや LINE の
トークンなど）が未設定の間はアプリが**セットアップモード**で起動する。ブラウザで
`http://<サーバーのホスト名>/setup`（リバースプロキシで HTTPS 化済みならその URL）を
開けば、初回セットアップウィザードにそのまま進める。詳しくは次の
「セットアップ（手動）」内、「2. 初回セットアップ（Web ウィザード）」を参照。

以下は、そのインストーラが内部でやっていること＝**手動セットアップ**の手順。

## セットアップ（手動）

### 1. 取得と依存インストール
```bash
# 配布 tar を展開、またはソースを配置してから
cd card-notify
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

### 2. 初回セットアップ（Web ウィザード）
`install.sh` はもう対話的なセットアップを実行しない。代わりに、秘密情報
（WebUI パスワード・LINE トークンなど）が未設定の間、アプリは**セットアップモード**
で起動し、未ログインでも `/setup` にアクセスできる。ブラウザで
`http://<サーバーのホスト名>/setup`（リバースプロキシ済みならその HTTPS URL）を開くと
以下の2ステップのウィザードが表示される。

1. **`/setup`**: WebUI のログインパスワードを設定し、LINE Messaging API の
   チャンネルアクセストークンとチャンネルシークレット（取得方法は次の
   「3. LINE Messaging API」を参照。値は以前と同じ、入力欄がターミナルから
   フォームに変わっただけ）を貼り付ける。公開 URL（`SITE_URL`）は任意項目。
   送信すると `SECRET_KEY` が自動生成され、これらの値がデータディレクトリ配下の
   env ファイルへ権限 600 で書き出される。公開 URL に `https://` の URL を
   指定した場合は `SESSION_COOKIE_SECURE=true` も書き込まれる（サービス
   再起動後に有効。HTTPS 化前に LAN の `http://` でウィザードを進めても
   支障はない）。
2. **`/setup/gmail`**: 続けて Gmail 連携を設定する（任意）。手順は次の
   「4. Gmail OAuth」を参照。あとで設定したい場合は「後で設定」リンクで
   スキップでき、現金入力など Gmail に依存しない機能は先に使い始められる。

> 🔑 `/setup` の送信には**セットアップトークン**の入力が必要。トークンは
> `install.sh` がインストール完了時に画面へ表示する（後から確認する場合は
> データディレクトリの `setup_token` ファイル。手動起動時はアプリが初回
> `/setup` アクセス時に生成し起動ログへ出力する）。これにより、設定完了前に
> 公開 URL へ到達した第三者が先に初期設定を乗っ取ることを防ぐ。設定が完了
> するとトークンは削除され、`/setup*` へは以後アクセスできなくなり（`/login`
> にリダイレクトされ）、再実行はできない。

#### CLI セットアップ（上級者向け）
ブラウザから到達しにくい環境（`.env` を直接編集したい、SSH ポートフォワードで
WebUI にトンネルして作業したい、など）向けに、従来の対話 CLI ウィザードも
フォールバックとして残っている:
```bash
python setup.py
```
WebUI パスワードを決め、LINE のトークンを貼り付けると、`SECRET_KEY` を自動生成して
`.env`（権限 600）を書き出す。`credentials.json` があれば続けて Gmail 認証まで案内
する。本番では `.env` をデータディレクトリ配下の `/var/lib/card-notify/card-notify.env`
に置き `CARD_NOTIFY_ENV_FILE` で場所を指すのが既定（`CARD_NOTIFY_ENV_FILE=/var/lib/card-notify/card-notify.env python setup.py`
のように実行すればその場所に書き出せる。稼働中アプリが非 root・`ProtectSystem=strict`
で動き、書き込めるのは `/var/lib/card-notify` 配下だけのため、Web セットアップ
ウィザードが自分で書き込める場所である必要がある）。

> 手動で用意したい場合は `cp .env.example .env` して各値を編集し、パスワードは
> `python gen_password.py '好きなパスワード'`、鍵は
> `python -c "import secrets; print(secrets.token_hex(32))"` で生成する。

### 3. LINE Messaging API
[LINE Developers](https://developers.line.biz/) でチャンネルを作成し、チャンネル
シークレットとアクセストークンを `.env` に設定。Webhook URL に
`https://<あなたのドメイン>/webhook` を登録し、Bot を友だち追加する。
**初回に何かメッセージを送ると、その userId が通知先として自動登録される**
（以後、他人のメッセージは無視される）。

#### LINE でできる操作（メッセージ構文）
Bot に送るメッセージで現金支出の登録・照会ができる（WebUIを開かなくてよい）:

| 送るメッセージ | 動作 |
|---|---|
| `500 コンビニ` | 今日の現金支出として ¥500 をメモ「コンビニ」付きで登録 |
| `500` | 店舗名なしで登録 |
| `500 コンビニ 7/8` / `500 コンビニ 昨日` | 日付を指定して登録（末尾トークンが日付として解釈される） |
| `現金 500 コンビニ` | 先頭の「現金」は無視されるので付けても同じ |
| `今日` | 今日登録済みの支出一覧と合計を返信 |
| `今週` | 今週の日別一覧を返信 |
| `今月のまとめ` / `月次レポート` / `まとめ` | 月次レポートを即時再送 |

金額は先頭トークンが数字（カンマ区切り可）である必要がある。認識できない形式は
使い方の案内を返信する。

### 4. Gmail OAuth
Google Cloud 側の準備（テストユーザー追加・OAuth クライアント作成）は従来どおり。
認証そのものは Web ウィザードの `/setup/gmail` から行うのが既定の手順:

1. Google Cloud でプロジェクトを作成 → **Gmail API を有効化**。
2. OAuth 同意画面を設定し、自分を**テストユーザー**に追加。
3. 認証情報で **OAuth クライアント ID（デスクトップ）** を作成し、
   `credentials.json` をダウンロードする。
4. ブラウザで `/setup/gmail` を開き、ダウンロードした `credentials.json` を
   アップロードする。表示される認証 URL を開いて許可すると、ブラウザは
   `http://localhost/?code=...` という URL へ遷移し「このサイトにアクセス
   できません」等のエラーを表示する（これは想定どおりの動作）。アドレスバーに
   表示されたその URL をコピーしてフォームに貼り付け、送信する。`token.json`
   が生成されれば完了。あとで設定したい場合は「後で設定」リンクでスキップでき、
   Gmail 未連携でも現金入力など他の機能は使える。

> ⚠️ **重要な落とし穴**: OAuth 同意画面が「テスト中」ステータスのままだと、Gmail の
> **refresh token は約7日で失効**し、通知が静かに止まる。長期運用では本番モードへの
> 切り替えが必須。手順:
>
> 1. Google Cloud Console →「APIとサービス」→「OAuth 同意画面」を開く。
> 2. 公開ステータスの「アプリを公開」を押して**「本番環境」**に切り替える
>    （Google の審査申請は不要。確認ダイアログはそのまま確定してよい）。
> 3. 以後の認証時に「このアプリは Google で確認していません」という警告画面が
>    出るが、**自分で作成した自分専用の OAuth クライアント**なので「詳細」→
>    「（安全でないページに）移動」で進んで問題ない（100ユーザー未満の
>    未検証アプリに許容されているフロー）。

> CLI から設定したい場合（`setup.py` の CLI セットアップと組み合わせるときなど）は、
> `credentials.json` をデータディレクトリ（既定 `data/`、本番は
> `CARD_NOTIFY_DATA_DIR`）に置いてから `python oauth_setup.py` を実行する。表示
> される URL をブラウザで開いて許可し、`http://localhost/?code=...` を貼り付けれ
> ば同様に `token.json` が生成される。

### 5. 常駐化（systemd）
`deploy/card-notify.service` を `/etc/systemd/system/` にコピーし、`User=` を専用
ユーザーに、パスを実環境に合わせて編集してから:
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now card-notify
```
**cron は不要。** 日次バッチ（メール取得＋通知）は常駐プロセス内のスケジューラが
毎日決まった時刻に実行する。**通知時刻は WebUI の［設定］から変更**でき、即座に
反映される（`data/settings.json` に保存）。

### 6. nginx
`deploy/nginx.conf.example` を参考に vhost を作り、`certbot --nginx` で HTTPS 化する。
nginx 経由で公開する場合は env ファイルに `CARD_NOTIFY_TRUSTED_IP_HEADER=X-Real-IP` を
設定する（ログイン試行ロックが実クライアント IP 単位で効くようになる）。

同一サーバーに複数インスタンスを同居させたい場合は、インスタンスごとに
`CARD_NOTIFY_PORT`（既定 5000）・`CARD_NOTIFY_DATA_DIR`・`CARD_NOTIFY_ENV_FILE`・
systemd ユニット名・nginx vhost を別々に用意する（`deploy/card-notify.service` を
複製し、`Environment=CARD_NOTIFY_PORT=5001` のように追記して nginx の
`proxy_pass` 先を合わせればよい）。

---

## 自動アップデート（任意・既定 OFF）
配信元に新しい版を置くだけで、各インストールが自動追従できる。**信頼できる自分の
配信元だけを指すこと**（更新は無人でコードを実行するため）。

1. `.env` に配信元とオプトインを設定:
   ```
   AUTO_UPDATE=true
   CARD_NOTIFY_UPDATE_FEED_URL=https://card-notify.yama3394.uk
   ```
2. `deploy/card-notify-update.{service,timer}` を `/etc/systemd/system/` に置き、
   `User=` を編集して `systemctl enable --now card-notify-update.timer`。
3. タイマーが毎日 `auto_update.py` を実行。`{FEED}/version.json` を見て新しければ
   **バックアップ → tar 取得 → SHA256 検証 → 差し替え → マイグレーション →
   再起動 → ヘルスチェック**。失敗すればコードとデータを**ロールバック**し、結果を
   LINE 通知する。`data/` `.env` `token.json` は更新で触られない。
   コードの差し替えは tar 同梱の `MANIFEST`（配布ファイルの一覧）に基づき、
   そこに載るファイルだけを置換・削除する。`parsers/<issuer>.py` のような
   利用者が追加したファイル（新旧どちらの `MANIFEST` にも無いもの）は更新後も残る。

手動確認は `python auto_update.py --check`（差分の有無だけ表示）。

### 更新の署名検証（任意・推奨）
sha256 は tar と同じ配信元の `version.json` 由来のため、配信元が乗っ取られると
改ざん tar がそのまま実行されうる。Ed25519 署名を使うと、配信元とは独立に配った
公開鍵で tar の真正性を検証できる。3ステップで有効化する:

1. **配布者**: 鍵ペアを生成する（秘密鍵は権限 600 で保存され、公開鍵が表示される）:
   ```bash
   python gen_signing_key.py ~/.card-notify-signing.key
   ```
2. **配布者**: リリース時に `SIGNING_KEY` を指定すると tar に署名し、
   `version.json` に `sig`（base64 の Ed25519 署名）が追加される:
   ```bash
   SIGNING_KEY=~/.card-notify-signing.key R2_REMOTE=r2:<bucket> ./release.sh
   ```
3. **利用者**: 配布者から受け取った公開鍵（base64 の raw 32byte）を `.env` に設定する:
   ```
   CARD_NOTIFY_UPDATE_PUBKEY=<公開鍵のbase64>
   ```
   設定すると `sig` の無い更新・検証に失敗した更新は適用されない。未設定なら
   従来どおり sha256 のみで動作し、「署名検証なし」の警告ログが出る。

## リリース（配布する人向け）
新しい版を出す手順:
```bash
# 1) VERSION を更新（例 1.1.0）
# 2) tar・version.json・紹介サイト・install.sh を生成し R2 へアップロード
R2_REMOTE=r2:<bucket> ./release.sh
```
`release.sh` は `card-notify-<VERSION>.tar.gz`（`.env`/`data/` を除外）・
`version.json`（version・ファイル名・SHA256）・`index.html`（紹介サイト）・
`install.sh` を作り、`rclone copy` で配信元へ送る。各インストールの自動アップデートが
`version.json` を検知して更新する。

配信は **配信ドメイン直下**を前提（`/`=紹介サイト, `/install.sh`,
`/version.json`, `/card-notify-<VERSION>.tar.gz`）。Cloudflare の **R2 カスタム
ドメイン**で配信ドメインをバケット（ルート）に割り当て、`R2_REMOTE` にそのバケットを
指定する。

## 紹介サイト
`site/index.html` は依存ゼロの静的ランディングページ（自己完結）。`release.sh` が
配信元へ一緒に上げるので配信ドメインのルートで公開される。

---

## 対応カードを増やす（構造のキモ）
メールの解釈は `parsers/` のプラグインに分離されている。新しいカード/銀行に対応
するには `parsers/<issuer>.py` を作り `register()` するだけ。`gmail_fetcher` 側の
変更は不要:

```python
# parsers/rakuten.py
from .base import CardParser, register

register(CardParser(
    key="rakuten", label="楽天カード",
    from_addrs=["info@mail.rakuten-card.co.jp"],
    amount_markers=["ご利用金額"],
    date_markers=[r"ご利用日"],
    store_patterns=[r"ご利用店名[：:\s]+(.+?)[\n\r]"],
    is_cancellation=lambda text: "キャンセル" in text[:300],
    is_ignorable=lambda text: "ご利用いただけませんでした" in text[:500],
))
```
最後に `parsers/__init__.py` の import に1行足せば有効化される。付属の
`parsers/smcc.py`（三井住友）と `parsers/jcb.py` が実装例。

### 既知の制約（取消メールの突合せ）
取消メールと元取引の対応付けは、メール ID ではなく金額・店舗・利用日による
ヒューリスティックで行う（店舗名は全角/半角ゆれを吸収して比較する）。店舗名・
利用日のどちらでも1件に絞り込めず、同一種別・金額・通貨の取引が複数残る場合は、
誤って無関係な取引を削除しないよう取消を反映せず見送る（ログに警告が出るので
手動で確認する）。

### 店舗名が取れなかった取引の手直し（`/errors`）
メール文面が想定パターンと合わず店舗名を抽出できなかった取引（現金以外）は、
金額・日付だけ登録したうえで WebUI の `/errors` 画面（ダッシュボードからも遷移可）
に一覧表示される。ここで店舗名を追記、または不要なら削除できる。

## テスト
```bash
python -m pytest -q
```

## セキュリティ設計メモ
- 初回セットアップは**セットアップトークン**（install.sh が生成・表示）で所有者を
  確認。設定完了前の `/setup` 乗っ取りを防ぐ。
- WebUI はパスワード＋セッション。ログイン失敗はファイル永続のロック（既定 3回/10分）。
- **リバースプロキシ配下では `CARD_NOTIFY_TRUSTED_IP_HEADER` の設定を推奨**
  （deploy/nginx.conf.example なら `X-Real-IP`）。未設定だと接続元が常にプロキシの
  IP になり、ログイン試行ロックが全クライアント共有になる（第三者の失敗3回で
  正規ユーザーもロックされる）。ただし**プロキシがそのヘッダを必ず上書きする
  構成でのみ**設定すること。素通しするプロキシで設定すると偽装可能になる。
- CSRF トークン、`hmac.compare_digest` による定数時間比較、HttpOnly/SameSite
  Cookie（Secure は `SESSION_COOKIE_SECURE=true` で有効化。HTTPS 終端済みなら
  推奨・ウィザードが https の公開 URL 指定時に自動設定）、CSP・HSTS・各種
  セキュリティヘッダ、リクエストサイズ上限 1MB。
- LINE Webhook は HMAC 署名検証＋**オーナー userId 検証**（第三者の通知乗っ取り防止）
  ＋ `webhookEventId` による再配達の重複排除（現金支出の二重登録防止）。
- CSV エクスポートは店舗名の先頭記号をクォートし、表計算ソフトでの数式実行
  （CSV インジェクション）を防ぐ。
- `history.json`・`notify_state.json` は排他ロック下の read-modify-write
  （`storage.update_history` / `state.update`）で同時更新のロストを防止。
- 機密はソースに書かず env 解決。専用ユーザーでの非 root 稼働を推奨。
- **設計上のトレードオフ（自己アップデート）**: 自動更新をサービスユーザー自身が
  行うため、サービスユーザーはアプリコード（`/opt/card-notify`）への書き込み権限と
  `sudo systemctl restart card-notify.service`（これ1コマンドのみの NOPASSWD）を
  持つ。アプリが侵害された場合、コード書き換えによる永続化が可能になる。
  これを許容しない場合は `AUTO_UPDATE=false`（既定）のままにし、更新タイマーを
  無効化（`systemctl disable card-notify-update.timer`）した上でコードを root 所有
  にし、更新は手動で行うこと。さらに `CARD_NOTIFY_UPDATE_PUBKEY` による署名検証を
  有効にすると配信元侵害への防御になる。
- **Web サーバーについて**: 本アプリは Flask 内蔵サーバー（threaded）を
  127.0.0.1 で動かし、前段の nginx で TLS 終端・レート制限を行う設計
  （単一ユーザー・低トラフィック前提の意図的な選択）。多人数・高負荷で使う場合は
  gunicorn 等への載せ替えを検討（その場合アプリ内スケジューラの多重起動に注意。
  `CARD_NOTIFY_SCHEDULER=false` にして `main.py` を cron 実行する構成が安全）。

## アンインストール
`install.sh` で導入した環境を撤去する場合:
```bash
sudo bash uninstall.sh
```
systemd ユニット（本体・自動更新タイマー）・sudoers ドロップイン・専用ユーザーを削除する。
取引履歴や LINE/Gmail 連携情報も含めて完全に消す場合:
```bash
sudo PURGE_DATA=true bash uninstall.sh
```
`INSTALL_DIR` / `SERVICE_USER` / `DATA_DIR` を導入時に変更していた場合は、同じ値を
環境変数で指定して実行する（既定値は `install.sh` と同じ）。

## ライセンス
MIT（`LICENSE` 参照）。無保証。
