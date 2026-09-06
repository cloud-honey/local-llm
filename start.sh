#!/bin/zsh
# 로컬 LLM 인프라 시작 스크립트

# 2026-08-29: LM Studio(LAN IP:1234) 하드코딩 → oMLX(로컬:8766)로 교체.
# 백엔드가 바뀌어도(oMLX/LM Studio 등) LM_URL만 바꾸면 됨 — 모델명은 하드코딩 안 함,
# proxy.mjs가 매 요청마다 현재 로드된 모델을 조회해서 그대로 씀.
LM_URL="${LM_URL:-http://127.0.0.1:8766}"
LLM_API_KEY="${LLM_API_KEY:-omlx}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NODE_BIN="/Users/sykim/.nvm/versions/node/v24.16.0/bin/node"

# 로컬 LLM 서버 상태 확인
echo "▶ 로컬 LLM 서버 연결 확인 ($LM_URL)..."
if ! curl -sf -H "Authorization: Bearer $LLM_API_KEY" "$LM_URL/v1/models" > /dev/null; then
  echo "✗ 로컬 LLM 서버에 접속할 수 없습니다: $LM_URL"
  echo "  oMLX(ai.boomco.omlx-server) 또는 LM Studio가 켜져 있는지 확인해주세요."
  exit 1
fi
echo "✓ 로컬 LLM 서버 연결됨 ($LM_URL)"

# Anthropic 프록시 시작 (Claude Code CLI 연결용)
echo "\n▶ Anthropic 프록시 시작 (port 4000)..."
if lsof -ti:4000 > /dev/null 2>&1; then
  echo "  이미 실행 중 (PID: $(lsof -ti:4000))"
else
  LM_STUDIO_URL="$LM_URL" LLM_API_KEY="$LLM_API_KEY" "$NODE_BIN" "$SCRIPT_DIR/proxy.mjs" > /tmp/llm-proxy.log 2>&1 &
  sleep 1
  if lsof -ti:4000 > /dev/null 2>&1; then
    echo "✓ 프록시 시작됨 (port 4000)"
  else
    echo "✗ 프록시 시작 실패 — 로그: /tmp/llm-proxy.log"
  fi
fi

echo "\n========================================="
echo "  로컬 LLM 인프라 준비 완료"
echo "========================================="
echo "  로컬 LLM API   : $LM_URL/v1 (모델은 로드된 걸 자동 조회)"
echo "  Anthropic Proxy : http://localhost:4000"
echo ""
echo "  Claude Code CLI:"
echo "    export ANTHROPIC_BASE_URL=http://localhost:4000"
echo "    export ANTHROPIC_API_KEY=local"
echo "    claude"
echo ""
echo "  Claude Code Desktop MCP:"
echo "    앱 재시작 후 local-llm 도구 사용 가능"
echo "========================================="
