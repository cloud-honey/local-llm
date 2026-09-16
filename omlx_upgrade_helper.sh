#!/bin/zsh
# oMLX 앱 교체·롤백·네이티브 경로 토글 도우미 (OMLX_0.6.4_UPGRADE_RUNBOOK.md 참고, 2026-09-17)
#   zsh omlx_upgrade_helper.sh status
#   zsh omlx_upgrade_helper.sh backup
#   zsh omlx_upgrade_helper.sh swap-app "/Volumes/oMLX 0.6.4/oMLX.app"
#   zsh omlx_upgrade_helper.sh rollback-app
#   zsh omlx_upgrade_helper.sh native-on | native-off
set -euo pipefail

APP=/Applications/oMLX.app
BACKUPS=$HOME/Claude_works/local-llm/backups
MODEL=$HOME/Claude_works/local-llm/models/Qwen3.8-Flash-Next-oQ4e-128k
SUPPORT=$MODEL/omlx_support
NATIVE=$HOME/Claude_works/local-llm/omlx_serve_native.sh   # 저장소에 버전 관리되는 네이티브 경로 serve
LABEL=ai.boomco.omlx-server
LOG=$HOME/Claude_works/hermes-agent/data/logs/omlx-server.stderr.log
UID_=$(id -u)

ver() { defaults read "$1/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null || echo "?"; }

stop_all() {
  echo "[stop] 메뉴바 앱 종료 + launchd 서버 정지"
  osascript -e 'tell application "oMLX" to quit' 2>/dev/null || true
  pkill -x oMLX 2>/dev/null || true
  launchctl bootout gui/$UID_/$LABEL 2>/dev/null || true
  sleep 3
  pgrep -fl "omlx-server|omlx.cli serve" && { echo "[stop] 아직 살아있음 — 5초 더 대기"; sleep 5; } || true
}

start_server() {
  echo "[start] launchd 서버 기동"
  launchctl bootstrap gui/$UID_ $HOME/Library/LaunchAgents/$LABEL.plist 2>/dev/null || launchctl kickstart -k gui/$UID_/$LABEL
  echo "[start] /health 대기 (최대 300초 — 모델 로드 1~3분)"
  for i in {1..60}; do
    if curl -s -m 3 http://127.0.0.1:8766/health -H "Authorization: Bearer omlx" | grep -q healthy; then
      echo "[start] healthy ($((i*5))초)"; curl -s http://127.0.0.1:8766/health -H "Authorization: Bearer omlx"; echo; return 0
    fi
    sleep 5
  done
  echo "[start] 300초 안에 healthy 안 됨 — 로그 확인: tail -80 $LOG"; return 1
}

case "${1:-}" in
  status)
    echo "앱 버전: $(ver $APP)  (백업: $(ls $BACKUPS 2>/dev/null | tr '\n' ' '))"
    launchctl print gui/$UID_/$LABEL 2>/dev/null | grep -E "^\s+(state|pid) " || echo "launchd: 등록 안 됨"
    curl -s -m 3 http://127.0.0.1:8766/health -H "Authorization: Bearer omlx" || echo "health 응답 없음"; echo
    [[ -f $SUPPORT/serve.bak-native ]] && echo "serve: 네이티브 경로 ON (serve.bak-native 존재)" || echo "serve: 기본(커스텀 로더 + .mlx-runtime 0.32.1)"
    grep -c "failed to load; falling back" $LOG 2>/dev/null | sed 's/^/커널 폴백 WARNING 누적: /' || true
    ;;
  backup)
    mkdir -p $BACKUPS
    dest=$BACKUPS/oMLX-$(ver $APP).app
    [[ -d $dest ]] && { echo "이미 있음: $dest"; exit 0; }
    echo "[backup] $APP → $dest"; cp -R "$APP" "$dest"; echo "완료 ($(du -sh $dest | cut -f1))"
    ;;
  swap-app)
    src=${2:?"사용법: swap-app /Volumes/.../oMLX.app"}
    [[ -d $src ]] || { echo "없음: $src"; exit 1; }
    echo "[swap] $(ver $APP) → $(ver $src)"
    [[ -d $BACKUPS/oMLX-$(ver $APP).app ]] || { echo "먼저 backup 을 실행하세요"; exit 1; }
    stop_all
    rm -rf "$APP.old"; mv "$APP" "$APP.old"
    cp -R "$src" "$APP"; xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
    echo "[swap] 설치된 버전: $(ver $APP)"; rm -rf "$APP.old"
    start_server
    ;;
  rollback-app)
    src=$(ls -d $BACKUPS/oMLX-*.app 2>/dev/null | head -1)
    [[ -n $src ]] || { echo "백업 없음: $BACKUPS"; exit 1; }
    echo "[rollback] $(ver $APP) → $(ver $src)"
    stop_all
    rm -rf "$APP"; cp -R "$src" "$APP"
    start_server
    ;;
  native-on)
    [[ -f $NATIVE ]] || { echo "없음: $NATIVE"; exit 1; }
    [[ -f $SUPPORT/serve.bak-native ]] && { echo "이미 네이티브 ON"; exit 0; }
    cp -p $SUPPORT/serve $SUPPORT/serve.bak-native
    cp -p $NATIVE $SUPPORT/serve
    echo "[native-on] serve 교체 완료 → 재시작"; launchctl kickstart -k gui/$UID_/$LABEL; start_server
    ;;
  native-off)
    [[ -f $SUPPORT/serve.bak-native ]] || { echo "네이티브 OFF 상태(백업 없음)"; exit 0; }
    mv -f $SUPPORT/serve.bak-native $SUPPORT/serve
    echo "[native-off] serve 원복 → 재시작"; launchctl kickstart -k gui/$UID_/$LABEL; start_server
    ;;
  *)
    sed -n 2,7p "$0"; exit 1 ;;
esac
