#!/usr/bin/env bash
# install.sh — card-notify の配布インストーラ。
#
# 利用者はメンテナの配信 URL から実行する:
#   curl -fsSL https://<your-feed>/card-notify/install.sh | bash
# もしくは保存してから:
#   sudo bash install.sh
#
# systemd ユニット設置と専用ユーザー作成のため root（または sudo）で実行する前提。
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# 設定変数（必要に応じて書き換え。配信時は FEED_URL を差し替える）
# ─────────────────────────────────────────────────────────────
FEED_URL="${FEED_URL:-https://card-notify.yama3394.uk}"  # 配信ベース URL（tar/version.json を置く場所）
INSTALL_DIR="${INSTALL_DIR:-/opt/card-notify}"                       # 設置先
SERVICE_USER="${SERVICE_USER:-card-notify}"                         # 実行専用ユーザー

DATA_DIR="/var/lib/card-notify"
ENV_FILE="${DATA_DIR}/card-notify.env"
UNIT_DIR="/etc/systemd/system"
FEED_URL="${FEED_URL%/}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 権限確認 ──
if [[ "$(id -u)" -ne 0 ]]; then
  die "root で実行してください（専用ユーザー作成と systemd 設置のため）: sudo bash install.sh"
fi

# ── FEED_URL 差し替え確認 ──
if [[ "$FEED_URL" == *REPLACE_WITH_YOUR_FEED* ]]; then
  die "FEED_URL がプレースホルダのままです。配信ベース URL を設定してください（例: FEED_URL=https://dl.example.com bash install.sh）"
fi

# ── 依存確認 ──
log "依存コマンドを確認"
for cmd in python3 curl tar; do
  command -v "$cmd" >/dev/null 2>&1 || die "必須コマンドが見つかりません: $cmd"
done
SHA_CMD=""
if command -v sha256sum >/dev/null 2>&1; then SHA_CMD="sha256sum"
elif command -v shasum >/dev/null 2>&1; then SHA_CMD="shasum -a 256"
else die "sha256sum も shasum も見つかりません（ダウンロード検証に必要）"; fi

# ── 専用ユーザー作成（冪等） ──
if id "$SERVICE_USER" >/dev/null 2>&1; then
  log "専用ユーザーは既存: $SERVICE_USER"
else
  log "専用ユーザーを作成: $SERVICE_USER"
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" \
      || warn "useradd に失敗しました。手動でユーザーを用意してください: $SERVICE_USER"
  else
    warn "useradd が無いため専用ユーザーを作成できません。手動で用意してください: $SERVICE_USER"
  fi
fi

# ── version.json を取得して最新版を特定 ──
WORK_DIR="$(mktemp -d)"
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

log "配信メタデータを取得: ${FEED_URL}/version.json"
curl -fsSL "${FEED_URL}/version.json" -o "${WORK_DIR}/version.json" \
  || die "version.json を取得できません: ${FEED_URL}/version.json"

# 依存の少ない python3 で JSON を読む
read -r REMOTE_VERSION REMOTE_URL REMOTE_SHA < <(python3 - "$WORK_DIR/version.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
print(d.get("version", ""), d.get("url", ""), d.get("sha256", ""))
PY
)
[[ -n "$REMOTE_VERSION" && -n "$REMOTE_URL" && -n "$REMOTE_SHA" ]] \
  || die "version.json の内容が不正です（version/url/sha256 が必要）"
log "最新バージョン: ${REMOTE_VERSION}"

# url が相対ファイル名なら FEED_URL からの相対で解決
case "$REMOTE_URL" in
  http://*|https://*) TAR_URL="$REMOTE_URL" ;;
  *)                  TAR_URL="${FEED_URL}/${REMOTE_URL}" ;;
esac
TARBALL="${WORK_DIR}/$(basename "$REMOTE_URL")"

# ── tar 取得 + sha256 検証 ──
log "アーカイブを取得: ${TAR_URL}"
curl -fsSL "$TAR_URL" -o "$TARBALL" || die "アーカイブの取得に失敗: $TAR_URL"

log "sha256 を検証"
ACTUAL_SHA="$($SHA_CMD "$TARBALL" | awk '{print $1}')"
if [[ "$ACTUAL_SHA" != "$REMOTE_SHA" ]]; then
  die "sha256 不一致。期待: ${REMOTE_SHA} / 実際: ${ACTUAL_SHA}"
fi
log "検証 OK"

# ── tar の内容検査（パストラバーサル対策。auto_update.py の _safe_extract と同等） ──
log "アーカイブの内容を検査（不正パス・リンクの検出）"
python3 - "$TARBALL" "$INSTALL_DIR" <<'PY' || die "アーカイブに不正なパスが含まれています。展開を中止しました"
import os
import sys
import tarfile

tar_path, install_dir = sys.argv[1], sys.argv[2]
dest = os.path.realpath(install_dir)

def bad(msg: str) -> None:
    print(f"不正な tar メンバー: {msg}", file=sys.stderr)
    sys.exit(1)

with tarfile.open(tar_path, "r:*") as tar:
    for m in tar.getmembers():
        # 絶対パス・".." を含むパスは拒否
        if m.name.startswith("/") or ".." in m.name.split("/"):
            bad(m.name)
        # 展開先の外を指すシンボリックリンク/ハードリンクは拒否
        if m.issym() or m.islnk():
            target = m.linkname
            if m.issym():
                if not os.path.isabs(target):
                    target = os.path.join(dest, os.path.dirname(m.name), target)
            else:
                # ハードリンクのターゲットは tar ルートからの相対
                target = os.path.join(dest, target)
            target = os.path.normpath(target)
            if target != dest and not target.startswith(dest + os.sep):
                bad(f"{m.name} -> {m.linkname}")
PY

# ── 展開（冪等：既存ディレクトリを壊さず上書き展開） ──
log "展開先を用意: ${INSTALL_DIR}"
mkdir -p "$INSTALL_DIR"
tar -xzf "$TARBALL" -C "$INSTALL_DIR"

# ── venv 作成 + 依存インストール ──
VENV_DIR="${INSTALL_DIR}/.venv"
if [[ -x "${VENV_DIR}/bin/python" && -x "${VENV_DIR}/bin/pip" ]]; then
  log "venv は既存: ${VENV_DIR}"
else
  log "venv を作成: ${VENV_DIR}"
  rm -rf "$VENV_DIR"
  if ! python3 -m venv "$VENV_DIR" 2>/dev/null || [[ ! -x "${VENV_DIR}/bin/pip" ]]; then
    log "venv 作成に ensurepip 不足の疑い。OS パッケージの導入を試みます（python3-venv/python3-pip 相当）"
    rm -rf "$VENV_DIR"
    if command -v apt-get >/dev/null 2>&1; then
      (DEBIAN_FRONTEND=noninteractive apt-get update -qq \
        && DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip) \
        || warn "apt-get でのインストールに失敗しました"
    elif command -v dnf >/dev/null 2>&1; then
      dnf install -y python3-pip || warn "dnf でのインストールに失敗しました"
    elif command -v yum >/dev/null 2>&1; then
      yum install -y python3-pip || warn "yum でのインストールに失敗しました"
    elif command -v apk >/dev/null 2>&1; then
      apk add --no-cache python3 py3-pip || warn "apk でのインストールに失敗しました"
    else
      warn "パッケージマネージャを自動検出できませんでした（apt-get/dnf/yum/apk のいずれも未検出）"
    fi
    python3 -m venv "$VENV_DIR" \
      || die "venv 作成に失敗（python3-venv、または python3-pip 相当のパッケージを手動で導入してください）"
    [[ -x "${VENV_DIR}/bin/pip" ]] \
      || die "venv は作成できましたが pip が見つかりません（python3-pip 相当のパッケージを手動で導入してください）"
  fi
fi
log "依存パッケージをインストール"
"${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null
"${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

# ── データディレクトリ + 所有権 ──
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"

# ── セットアップトークン生成（初回のみ。/setup の所有者確認に使う） ──
# 未設定状態の /setup は誰でも到達できるため、このトークンの提示を必須にして
# 「公開URLに先に到達した第三者が初期設定を乗っ取る」のを防ぐ。
SETUP_TOKEN_FILE="${DATA_DIR}/setup_token"
SETUP_TOKEN=""
if [[ -f "$ENV_FILE" ]]; then
  log "設定済み（${ENV_FILE} あり）のためセットアップトークンは生成しません"
elif [[ -f "$SETUP_TOKEN_FILE" ]]; then
  SETUP_TOKEN="$(cat "$SETUP_TOKEN_FILE")"
  log "既存のセットアップトークンを使用します"
else
  SETUP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  (umask 077 && printf '%s\n' "$SETUP_TOKEN" > "$SETUP_TOKEN_FILE")
  log "セットアップトークンを生成しました: ${SETUP_TOKEN_FILE}"
fi

if id "$SERVICE_USER" >/dev/null 2>&1; then
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR"
fi

# ── systemd ユニット設置（SERVICE_USER プレースホルダを置換して設置） ──
log "systemd ユニットを設置: ${UNIT_DIR}"
install_unit() {
  local src="$1" dst="$2"
  [[ -f "$src" ]] || die "ユニットが見つかりません: $src"
  sed "s/REPLACE_WITH_SERVICE_USER/${SERVICE_USER}/g" "$src" > "${UNIT_DIR}/${dst}"
}
install_unit "${INSTALL_DIR}/deploy/card-notify.service"        "card-notify.service"
install_unit "${INSTALL_DIR}/deploy/card-notify-update.service" "card-notify-update.service"
install_unit "${INSTALL_DIR}/deploy/card-notify-update.timer"   "card-notify-update.timer"

# ── sudoers 設置（自動更新の再起動用。冪等：既存ファイルは上書き） ──
# 非 root の専用ユーザーで動く自動更新（auto_update.py）が
# `sudo -n systemctl restart card-notify.service` だけを実行できるようにする。
SUDOERS_FILE="/etc/sudoers.d/card-notify"
if ! command -v sudo >/dev/null 2>&1; then
  warn "sudo が見つからないため sudoers 設定をスキップします（自動更新のサービス再起動に必要です）"
else
  SYSTEMCTL_PATH="$(command -v systemctl)"
  log "sudoers を設置: ${SUDOERS_FILE}"
  printf '%s ALL=(root) NOPASSWD: %s restart card-notify.service\n' \
    "$SERVICE_USER" "$SYSTEMCTL_PATH" > "$SUDOERS_FILE"
  chmod 440 "$SUDOERS_FILE"
  if ! visudo -cf "$SUDOERS_FILE" >/dev/null 2>&1; then
    rm -f "$SUDOERS_FILE"
    warn "sudoers の構文検証に失敗したため ${SUDOERS_FILE} を削除しました（自動更新の再起動には手動設定が必要です）"
  fi
fi

log "daemon-reload"
systemctl daemon-reload

log "本体サービスを有効化・起動: card-notify"
systemctl enable --now card-notify.service || warn "card-notify の起動に失敗。journalctl -u card-notify で確認してください"

log "更新タイマーを有効化・起動: card-notify-update.timer"
systemctl enable --now card-notify-update.timer || warn "更新タイマーの起動に失敗。journalctl -u card-notify-update で確認してください"

# ── 完了案内 ──
cat <<EOF

────────────────────────────────────────────────────────────
✅ card-notify のインストールが完了しました（v${REMOTE_VERSION}）

  設置先      : ${INSTALL_DIR}
  設定 (.env) : ${ENV_FILE}
  データ      : ${DATA_DIR}
  実行ユーザー: ${SERVICE_USER}

次にやること:
  • 初期設定がまだ完了していません。ブラウザで以下を開いて、WebUI パスワード・
    LINE 連携・Gmail 連携を設定してください:
      http://127.0.0.1:5000/setup
    （リバースプロキシ等で公開している場合は、その公開 URL + /setup を開いてください）
$(if [[ -n "$SETUP_TOKEN" ]]; then cat <<TOKEN
  • /setup では次の「セットアップトークン」の入力が必要です（所有者確認のため）:

      ${SETUP_TOKEN}

    （後から確認する場合: cat ${SETUP_TOKEN_FILE}）
TOKEN
fi)
  • 設定完了後は WebUI にログインし、［設定］から毎日の通知時刻を設定してください。
    （通知はアプリ内スケジューラが実行します。cron 登録は不要です）
  • リバースプロキシに deploy/nginx.conf.example（X-Real-IP を設定）を使う場合は、
    ${ENV_FILE} に CARD_NOTIFY_TRUSTED_IP_HEADER=X-Real-IP を追記してください。
    未設定だとログイン試行ロックが「プロキシのIP単位」になり、第三者の失敗で
    自分もロックされることがあります（詳細は README のセキュリティ節）。

自動アップデートについて:
  • 更新タイマー（card-notify-update.timer）は有効ですが、AUTO_UPDATE は既定 off の
    ため、実行されても更新は行われません（空振り）。
  • 有効化するには ${ENV_FILE} に AUTO_UPDATE=true と
    CARD_NOTIFY_UPDATE_FEED_URL=${FEED_URL} を設定してください。
────────────────────────────────────────────────────────────
EOF
