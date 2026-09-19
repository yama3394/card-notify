#!/usr/bin/env bash
# release.sh — メンテナ（配布者）が実行するリリース用スクリプト。
#
#   ./release.sh
#
# VERSION を読み、card-notify-<VERSION>.tar.gz と version.json を生成する。
# 併せて紹介サイト(site/index.html→index.html)・インストーラ(install.sh)・
# アンインストーラ(uninstall.sh)も配信物に含める。
# 環境変数 R2_REMOTE が設定されていれば rclone copy で Cloudflare R2 へアップロードする。
#
# 配信レイアウトは「配信ドメイン直下」を前提:
#   https://card-notify.yama3394.uk/              … 紹介サイト(index.html)
#   https://card-notify.yama3394.uk/install.sh    … インストーラ
#   https://card-notify.yama3394.uk/uninstall.sh  … アンインストーラ
#   https://card-notify.yama3394.uk/version.json  … 更新メタデータ
#   https://card-notify.yama3394.uk/card-notify-<VERSION>.tar.gz … 本体
# 公開方法は2通り（併用可）:
#   DEPLOY_DIR=/var/www/card-notify … このサーバーの nginx 静的配信ディレクトリへコピー
#   R2_REMOTE=r2:<bucket>           … rclone で Cloudflare R2 へアップロード
# どちらも未設定ならローカル(dist/)生成のみ。
# 署名（任意）:
#   SIGNING_KEY=<秘密鍵ファイル>    … gen_signing_key.py で生成した Ed25519 秘密鍵
#                                     （base64 raw 32byte）。設定時は tar に署名し
#                                     version.json に "sig" を追加する。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# ── バージョン確認 ──
if [[ ! -f VERSION ]]; then
  echo "エラー: VERSION ファイルが見つかりません（$REPO_DIR/VERSION）" >&2
  exit 1
fi
VERSION="$(tr -d '[:space:]' < VERSION)"
if [[ -z "$VERSION" ]]; then
  echo "エラー: VERSION が空です" >&2
  exit 1
fi
log "リリース対象バージョン: $VERSION"

TARBALL="card-notify-${VERSION}.tar.gz"
DIST_DIR="${REPO_DIR}/dist"
mkdir -p "$DIST_DIR"

# ── MANIFEST 生成（tar に含める配布ファイルの相対パス一覧。auto_update.py が
#    「配布物としてどのファイルを置換/削除してよいか」を判定するのに使う
#    （利用者が追加した parsers/mycard.py 等の独自ファイルを更新で消さないため）。
#    除外パターンは tar 作成と同一にする。MANIFEST は生成物だが tar に同梱する
#    配布物なので .gitignore には追加しない。） ──
log "MANIFEST を生成"
{
  find . \
    \( -path './.git' -o -path './dist' -o -path './data' -o -path './.update-backup' \
       -o -path './.venv' -o -path './venv' \) -prune \
    -o -type f \
    ! -name '*.pyc' \
    ! -path '*/__pycache__/*' \
    ! -path '*/.pytest_cache/*' \
    ! -path './.env' \
    ! -name 'MANIFEST' \
    ! -name 'MANIFEST.tmp' \
    -print
  echo './MANIFEST'
} | sed 's|^\./||' | sort > MANIFEST.tmp
mv MANIFEST.tmp MANIFEST

# ── tar 作成（配布に不要なものは除外。tests は含める。MANIFEST も同梱） ──
log "アーカイブを作成: dist/${TARBALL}"
tar \
  --exclude='./.env' \
  --exclude='./data' \
  --exclude='./.git' \
  --exclude='./dist' \
  --exclude='.update-backup' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='*.pyc' \
  --exclude='./.venv' \
  --exclude='./venv' \
  -czf "${DIST_DIR}/${TARBALL}" -C "$REPO_DIR" .

# ── sha256 計算 ──
if command -v sha256sum >/dev/null 2>&1; then
  SHA256="$(sha256sum "${DIST_DIR}/${TARBALL}" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
  SHA256="$(shasum -a 256 "${DIST_DIR}/${TARBALL}" | awk '{print $1}')"
else
  echo "エラー: sha256sum も shasum も見つかりません" >&2
  exit 1
fi
log "sha256: ${SHA256}"

# ── Ed25519 署名（任意。SIGNING_KEY=秘密鍵ファイルのパスが設定されている場合のみ） ──
# 秘密鍵は gen_signing_key.py で生成した base64 の raw 32byte。利用者側は対応する
# 公開鍵を CARD_NOTIFY_UPDATE_PUBKEY に設定すると署名検証が有効になる。
SIG=""
if [[ -n "${SIGNING_KEY:-}" ]]; then
  if [[ ! -f "$SIGNING_KEY" ]]; then
    echo "エラー: SIGNING_KEY のファイルが見つかりません: ${SIGNING_KEY}" >&2
    exit 1
  fi
  # venv があればそれを使う（cryptography 導入済みのはず）。無ければシステム python3。
  PYBIN="python3"
  [[ -x "${REPO_DIR}/.venv/bin/python" ]] && PYBIN="${REPO_DIR}/.venv/bin/python"
  log "tar に Ed25519 署名を付与（鍵: ${SIGNING_KEY}）"
  SIG="$("$PYBIN" - "$SIGNING_KEY" "${DIST_DIR}/${TARBALL}" <<'PY'
import base64
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

key_b64 = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(key_b64))
print(base64.b64encode(key.sign(Path(sys.argv[2]).read_bytes())).decode())
PY
)" || { echo "エラー: 署名の生成に失敗しました（cryptography 導入済みの python が必要）" >&2; exit 1; }
  log "sig: ${SIG}"
fi

# ── version.json 生成（url はファイル名。FEED_URL からの相対で解決される） ──
log "version.json を生成"
if [[ -n "$SIG" ]]; then
cat > "${DIST_DIR}/version.json" <<JSON
{
  "version": "${VERSION}",
  "url": "${TARBALL}",
  "sha256": "${SHA256}",
  "sig": "${SIG}"
}
JSON
else
cat > "${DIST_DIR}/version.json" <<JSON
{
  "version": "${VERSION}",
  "url": "${TARBALL}",
  "sha256": "${SHA256}"
}
JSON
fi

# ── 紹介サイト・インストーラ・README も配信物として dist に用意 ──
# サイト(site/index.html)が相対リンク ./README.md でこれを直接参照するため、
# tar の中だけでなく配信ドメイン直下にも置く。
if [[ -f site/index.html ]]; then
  cp site/index.html "${DIST_DIR}/index.html"
fi
cp install.sh "${DIST_DIR}/install.sh"
cp uninstall.sh "${DIST_DIR}/uninstall.sh"
cp README.md "${DIST_DIR}/README.md"

log "ローカル生成完了:"
echo "    ${DIST_DIR}/${TARBALL}"
echo "    ${DIST_DIR}/version.json"
echo "    ${DIST_DIR}/index.html     (紹介サイト)"
echo "    ${DIST_DIR}/README.md      (サイトから ./README.md でリンクされる)"
echo "    ${DIST_DIR}/install.sh     (インストーラ)"
echo "    ${DIST_DIR}/uninstall.sh   (アンインストーラ)"

# ── ローカル nginx 配信ディレクトリへ公開（任意） ──
if [[ -n "${DEPLOY_DIR:-}" ]]; then
  log "配信ディレクトリへコピー: ${DEPLOY_DIR}"
  mkdir -p "$DEPLOY_DIR"
  cp "${DIST_DIR}/${TARBALL}" "${DIST_DIR}/version.json" "${DIST_DIR}/install.sh" "${DIST_DIR}/uninstall.sh" "${DIST_DIR}/README.md" "$DEPLOY_DIR/"
  [[ -f "${DIST_DIR}/index.html" ]] && cp "${DIST_DIR}/index.html" "$DEPLOY_DIR/"
  log "公開完了: ${DEPLOY_DIR}"
fi

# ── R2 へアップロード（任意） ──
if [[ -n "${R2_REMOTE:-}" ]]; then
  if ! command -v rclone >/dev/null 2>&1; then
    echo "エラー: R2_REMOTE が設定されていますが rclone が見つかりません" >&2
    exit 1
  fi
  log "R2 へアップロード: ${R2_REMOTE}"
  # tar と version.json は毎リリース更新。サイトとインストーラ/アンインストーラも同梱配信。
  rclone copy "${DIST_DIR}/${TARBALL}"      "${R2_REMOTE}" --progress
  rclone copy "${DIST_DIR}/version.json"    "${R2_REMOTE}" --progress
  rclone copy "${DIST_DIR}/install.sh"      "${R2_REMOTE}" --progress
  rclone copy "${DIST_DIR}/uninstall.sh"    "${R2_REMOTE}" --progress
  rclone copy "${DIST_DIR}/README.md"       "${R2_REMOTE}" --progress
  [[ -f "${DIST_DIR}/index.html" ]] && rclone copy "${DIST_DIR}/index.html" "${R2_REMOTE}" --progress
  log "アップロード完了。配信物: index.html / install.sh / uninstall.sh / README.md / version.json / ${TARBALL}"
fi

if [[ -z "${DEPLOY_DIR:-}" && -z "${R2_REMOTE:-}" ]]; then
  log "DEPLOY_DIR / R2_REMOTE とも未設定のため公開はスキップしました（dist/ 生成のみ）。"
  echo "    このサーバーで公開: DEPLOY_DIR=/var/www/card-notify ./release.sh"
  echo "    R2 で公開:          R2_REMOTE=r2:<bucket> ./release.sh"
fi
