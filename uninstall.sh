#!/usr/bin/env bash
# uninstall.sh — card-notify のアンインストーラ。
#
# 利用者は保存してから実行する:
#   sudo bash uninstall.sh
# データ（取引履歴・LINE/Gmail連携情報・パスワード）も含めて完全に削除する場合:
#   sudo PURGE_DATA=true bash uninstall.sh
#
# systemd ユニット削除とユーザー削除のため root（または sudo）で実行する前提。
# install.sh のデフォルト値に合わせているため、INSTALL_DIR / SERVICE_USER / DATA_DIR を
# インストール時に変更していた場合は同じ値を環境変数で指定してください。
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# 設定変数（install.sh 実行時に変更していた場合は合わせて指定）
# ─────────────────────────────────────────────────────────────
INSTALL_DIR="${INSTALL_DIR:-/opt/card-notify}"   # 設置先
SERVICE_USER="${SERVICE_USER:-card-notify}"      # 実行専用ユーザー
DATA_DIR="${DATA_DIR:-/var/lib/card-notify}"     # データ・秘密情報の保存先
UNIT_DIR="/etc/systemd/system"
PURGE_DATA="${PURGE_DATA:-false}"                # true でデータディレクトリも削除

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 権限確認 ──
if [[ "$(id -u)" -ne 0 ]]; then
  die "root で実行してください（systemd 設置解除とユーザー削除のため）: sudo bash uninstall.sh"
fi

# ── サービス停止・無効化 ──
log "サービスを停止・無効化します"
systemctl disable --now card-notify.service >/dev/null 2>&1 \
  || warn "card-notify.service の停止/無効化に失敗しました（未インストールの可能性）"
systemctl disable --now card-notify-update.timer >/dev/null 2>&1 \
  || warn "card-notify-update.timer の停止/無効化に失敗しました（未インストールの可能性）"
systemctl stop card-notify-update.service >/dev/null 2>&1 || true

# ── systemd ユニットファイル削除 ──
log "systemd ユニットファイルを削除します: ${UNIT_DIR}"
rm -f "${UNIT_DIR}/card-notify.service" \
      "${UNIT_DIR}/card-notify-update.service" \
      "${UNIT_DIR}/card-notify-update.timer"
systemctl daemon-reload

# ── sudoers（自動更新の再起動許可）削除 ──
if [[ -f /etc/sudoers.d/card-notify ]]; then
  log "sudoers を削除します: /etc/sudoers.d/card-notify"
  rm -f /etc/sudoers.d/card-notify
fi

# ── アプリ本体削除 ──
if [[ -d "$INSTALL_DIR" ]]; then
  log "アプリ本体を削除します: ${INSTALL_DIR}"
  rm -rf "$INSTALL_DIR"
else
  log "アプリ本体は見つかりませんでした（既に削除済み）: ${INSTALL_DIR}"
fi

# ── データディレクトリ（既定では保持。明示指定時のみ削除） ──
if [[ "$PURGE_DATA" == "true" ]]; then
  if [[ -d "$DATA_DIR" ]]; then
    log "データディレクトリを削除します（PURGE_DATA=true）: ${DATA_DIR}"
    rm -rf "$DATA_DIR"
  fi
else
  if [[ -d "$DATA_DIR" ]]; then
    warn "データディレクトリは保持しました: ${DATA_DIR}"
    warn "（取引データ・LINE/Gmail 連携情報・WebUI パスワードが含まれます）"
    warn "完全に削除するには: sudo PURGE_DATA=true bash uninstall.sh"
    warn "または直接:        sudo rm -rf ${DATA_DIR}"
  fi
fi

# ── 専用ユーザー削除 ──
if id "$SERVICE_USER" >/dev/null 2>&1; then
  log "専用ユーザーを削除します: ${SERVICE_USER}"
  userdel "$SERVICE_USER" 2>/dev/null \
    || warn "ユーザー削除に失敗しました。手動で確認してください: ${SERVICE_USER}"
else
  log "専用ユーザーは見つかりませんでした（既に削除済みか未作成）: ${SERVICE_USER}"
fi

# ── 完了案内 ──
cat <<EOF

────────────────────────────────────────────────────────────
✅ card-notify のアンインストールが完了しました

  削除済み systemd ユニット: card-notify.service / card-notify-update.service / card-notify-update.timer
  削除済みアプリ本体        : ${INSTALL_DIR}
EOF
if [[ "$PURGE_DATA" == "true" ]]; then
  echo "  削除済みデータ            : ${DATA_DIR}"
else
  echo "  保持されたデータ          : ${DATA_DIR}（完全削除は上記メッセージを参照）"
fi
cat <<EOF
────────────────────────────────────────────────────────────
EOF
