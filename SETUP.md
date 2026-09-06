# 로컬 LLM 인프라 설정 문서

> **2026-08-29 대규모 개편**: 메인 엔진이 LM Studio → **oMLX**로 바뀌었습니다.
> 최신 아키텍처(Qwen4 계열 등) 지원이 LM Studio보다 훨씬 빠르고, 자체 launchd로 상시
> 구동됩니다. LM Studio는 앱 자체를 껐고, 이 문서의 "LM Studio" 관련 섹션은 대부분
> 과거 기록으로 남겨두되 최신 상태를 위 배지로 표시합니다. 모델별 실측 성능/특이사항은
> [`MODEL_EVALUATION.md`](./MODEL_EVALUATION.md) 참고.

## 전체 구조 (2026-08-29 기준)

```
Mac (이 기기)
  ├─ oMLX 서버 :8766 ── launchd(ai.boomco.omlx-server)로 상시 구동, 0.0.0.0 바인딩
  │    └─ 현재 모델: Qwen3.8-Flash-Next-oQ4e-128k (자세한 건 MODEL_EVALUATION.md)
  ├─ Hermes 게이트웨이 ── 텔레그램 등 메신저 연동, 메인 모델은 oMLX 사용
  ├─ llama-canary :18080 ── qwen3.6-35b-a3b, LM Studio와 무관한 자체 바이너리.
  │                          헬스체크/저지연 전용이라 그대로 유지
  └─ boomco-curator ── X 포스트 분석, oMLX 사용

XPS (OpenClaw / Claude Code CLI)
  │
  ├─ OpenAI 호환 API ─────────────────► Mac oMLX :8766  (직접 연결, LAN/Tailscale)
  │
  └─ Anthropic API (Claude Code CLI) ─► Mac 프록시 :4000 ──► Mac oMLX :8766
                                         (proxy.mjs, start.sh로 시작.
                                          2026-08-29: 모델 하드코딩 제거 —
                                          매 요청마다 oMLX에 로드된 모델을 자동 조회)

원격 PC의 MCP 클라이언트 (Claude Code 등)
  └─ MCP over HTTP ──────────────────► Mac :4100/mcp ──► Mac oMLX :8766
                                         (mcp_server_http.mjs)

Claude Code Desktop (이 Mac): Anthropic API 그대로 유지 + local-llm MCP(stdio, oMLX 연결)
```

---

## Mac 접속 주소

| 상황 | 주소 |
|------|------|
| 같은 Wi-Fi (로컬) | `192.168.219.119` (⚠️ DHCP라 바뀔 수 있음 — `ipconfig getifaddr en0`으로 확인) |
| 다른 네트워크 (Tailscale) | `100.69.3.50` |

> oMLX 포트 8766은 전체 인터페이스(`0.0.0.0`) 바인딩 + 방화벽 OFF 상태로 LAN/Tailscale
> 어디서든 접근 가능 (LM Studio 때와 동일 수준의 개방성). 단, oMLX는 **API 키 인증을
> 실제로 강제**함(LM Studio는 안 함) — 아래 인증 섹션 참고.
> 바인딩 주소는 모델 폴더의 `omlx_support/serve` 스크립트에서 `OMLX_HOST` 환경변수로
> 제어 (기본값 `0.0.0.0`).

---

## MCP over HTTP — 원격 PC에서 local-llm 도구 사용 (2026-07-06 추가)

Mac의 `mcp_server_http.mjs`(포트 4100)가 stdio MCP와 같은 도구
(`chat`, `list_models`, `review_code`)를 HTTP로 노출한다.
원격 PC에는 파일 복사나 Node 설치가 필요 없다 — URL만 등록하면 된다.

**서버 시작 (Mac):**
```bash
cd ~/Claude_works/local-llm
LM_STUDIO_URL=http://127.0.0.1:1234 MCP_HTTP_PORT=4100 node mcp_server_http.mjs
```

**클라이언트 등록 (원격 PC, Claude Code CLI):**
```bash
# Tailscale
claude mcp add --transport http local-llm http://100.69.3.50:4100/mcp
# 같은 Wi-Fi
claude mcp add --transport http local-llm http://192.168.219.102:4100/mcp
```

JSON 설정 방식 (`.mcp.json` 또는 클라이언트 설정):
```json
{ "mcpServers": { "local-llm": { "type": "http", "url": "http://100.69.3.50:4100/mcp" } } }
```

**확인:**
```bash
curl http://100.69.3.50:4100/health
```

> 인증 없음 — Tailscale 사설망/집 LAN 전용 전제 (LM Studio 1234와 동일한 보안 수준).

---

## 0단계: Mac — oMLX 서버 (신규 메인 엔진, 2026-08-29)

oMLX(`github.com/jundot/omlx`, 20.8k stars, Apache-2.0)는 Apple Silicon 전용 MLX 서빙
엔진으로, LM Studio보다 신생 아키텍처 지원이 빠르고 SSD 페이징 KV캐시·연속배치 같은
서빙 특화 기능이 있다. `/Applications/oMLX.app`로 설치, 모델 폴더별 격리된 런타임을 씀.

### 관리 (launchd 상시 구동)

```bash
# 상태 확인
launchctl print gui/501/ai.boomco.omlx-server | grep state

# 재시작 (모델 교체·설정 변경 후)
launchctl kickstart -k gui/501/ai.boomco.omlx-server

# 로그
tail -f ~/Claude_works/hermes-agent/data/logs/omlx-server.stderr.log
```

plist: `~/Library/LaunchAgents/ai.boomco.omlx-server.plist` (RunAtLoad + KeepAlive —
로그인 시 자동 시작, 죽으면 자동 재시작). 모델 폴더:
`~/Claude_works/local-llm/models/<모델폴더명>/`, 실행 스크립트는 그 안의
`omlx_support/serve`.

### 인증

LM Studio와 달리 **API 키를 실제로 검증함**. 현재 키는 `omlx` (모델 폴더 안
`.omlx/settings.json`의 `auth.api_key`). 모든 클라이언트가 `Authorization: Bearer omlx`
헤더를 보내야 함 — 안 보내면 401.

```bash
curl http://127.0.0.1:8766/v1/models -H "Authorization: Bearer omlx"
```

### 네트워크 바인딩

기본 `0.0.0.0`(전체 인터페이스)로 바인딩해서 LAN/Tailscale에서 바로 접근 가능
(LM Studio 1234와 동일한 개방 수준, `omlx_support/serve`의 `OMLX_HOST` 환경변수로 제어).
로컬 전용으로 좁히려면 해당 스크립트에서 `host=${OMLX_HOST:-0.0.0.0}`을 `127.0.0.1`로.

### 새 모델로 교체하는 법

1. `~/Claude_works/local-llm/models/`에 새 모델 폴더 추가 (HF에서 다운로드)
2. 모델 카드가 요구하는 런타임 버전 확인 후 필요시 `.mlx-runtime`에 격리 설치 —
   자세한 절차·주의사항은 [`MODEL_EVALUATION.md`](./MODEL_EVALUATION.md)의
   "평가 체크리스트" 참고 (아키텍처 지원 확인 → 벤치마크 4종 → 하드코딩 잔재 훑기 순)
3. `ai.boomco.omlx-server` 재시작 — oMLX가 `model_dirs` 안의 모델을 자동 디스커버리함
4. **하드코딩된 모델명 참조를 다시 확인할 것** — `proxy.mjs`는 매 요청마다 동적으로
   조회하므로 안 건드려도 되지만, `jobs.json`의 크론별 모델 오버라이드나 OpenClaw
   `config.json`의 `defaults.model` 처럼 정적으로 박아둔 곳들은 수동으로 갱신 필요

---

## 1단계 (레거시): Mac — LM Studio 서버 시작

> **2026-08-29부로 메인 엔진 아님.** LM Studio 앱은 현재 꺼져 있음. 아래는 과거 기록이며,
> GGUF 전용 모델이나 oMLX가 아직 지원 안 하는 모델을 돌릴 때만 참고.

### 앱에서 시작
1. LM Studio 앱 실행
2. 좌측 사이드바 **Local Server** (≡ 아이콘) 클릭
3. **Start Server** 버튼 클릭
4. 상태가 `Running on port 1234`로 바뀌면 완료

### CLI로 시작
```bash
/Users/sykim/.lmstudio/bin/lms server start
```

### 서버 상태 확인
```bash
# 모델 목록 조회 (로컬에서)
curl http://localhost:1234/v1/models

# XPS에서 접근 가능한지 확인 (Mac 터미널에서, LAN IP는 DHCP라 바뀔 수 있음)
curl http://192.168.219.119:1234/v1/models
```

---

## 2단계: XPS (Ubuntu) — OpenClaw 설치 및 설정

### OpenClaw 설치

```bash
# 1. Node.js 22 설치 (없는 경우)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs

# 2. OpenClaw 설치
sudo npm install -g openclaw

# 3. 설치 확인
openclaw --version
```

### oMLX 연결 설정 (최초 1회, 2026-08-29 갱신)

```bash
openclaw onboard
```

프롬프트에 아래 값 입력:

```
Provider:  lmstudio
Base URL:  http://192.168.219.119:8766/v1
API Key:   omlx
Model:     Qwen3.8-Flash-Next-oQ4e-128k
```

> LAN IP는 DHCP라 바뀔 수 있음 — Mac에서 `ipconfig getifaddr en0`으로 확인.
> API 키는 실제로 검증되므로 반드시 정확한 값(`omlx`)을 넣을 것 (LM Studio 때와 달리
> 아무 값이나 통하지 않음).

Tailscale 경유 시 (다른 네트워크에서 접속):
```
Base URL:  http://100.69.3.50:8766/v1
```

---

### config 파일 직접 편집

설정 파일 위치 (Ubuntu):
```
~/.openclaw/config.json
```

```bash
mkdir -p ~/.openclaw
cat > ~/.openclaw/config.json << 'EOF'
{
  "models": {
    "providers": {
      "lmstudio": {
        "baseUrl": "http://192.168.219.119:8766/v1",
        "apiKey": "omlx",
        "api": "openai-responses",
        "params": {
          "preload": false
        }
      }
    },
    "defaults": {
      "provider": "lmstudio",
      "model": "Qwen3.8-Flash-Next-oQ4e-128k",
      "contextWindow": 65536,
      "maxTokens": 8192
    }
  }
}
EOF
```

> `"preload": false` — JIT 로딩 충돌 방지 관행을 그대로 유지 (oMLX는 단일 모델만 서빙하므로
> LM Studio 때만큼 중요하진 않지만 해 될 것 없음).
>
> **모델을 바꿔 끼울 때마다 `defaults.model`을 수동으로 갱신해야 함** — OpenClaw는
> `proxy.mjs`처럼 로드된 모델을 자동 조회하는 기능이 없음(2026-08-29 기준 미확인,
> `openclaw --help`에서 다시 확인해볼 것). 바뀐 모델명은
> `curl http://192.168.219.119:8766/v1/models -H "Authorization: Bearer omlx"`로 확인.

---

### 모델 전환

oMLX는 폴더당 모델 하나를 서빙하는 구조라 LM Studio처럼 여러 모델을 동시에 올려두고
전환하는 방식이 아니다. 다른 모델을 쓰려면 Mac에서 oMLX 모델 폴더를 바꾸고 재시작한 뒤,
위 `defaults.model`을 새 모델명으로 갱신.

```bash
# 현재 oMLX에 로드된 모델 확인
curl http://192.168.219.119:8766/v1/models -H "Authorization: Bearer omlx"

# OpenClaw에 반영
openclaw models set lmstudio/<위에서 확인한 모델 id>
```

### 연결 확인

```bash
# Mac oMLX 서버 응답 확인
curl http://192.168.219.119:8766/v1/models -H "Authorization: Bearer omlx"

# OpenClaw 동작 테스트
openclaw "안녕, 작동하니?"
```

---

## 3단계: XPS (Ubuntu) — Claude Code CLI 설치 및 설정

### Claude Code CLI 설치

**방법 1 — 네이티브 인스톨러 (권장):**
```bash
curl -fsSL https://claude.ai/install.sh | bash
# 설치 후 PATH 적용
source ~/.bashrc
```

**방법 2 — apt 저장소:**
```bash
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL https://downloads.claude.ai/keys/claude-code.asc \
  -o /etc/apt/keyrings/claude-code.asc
echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] \
  https://downloads.claude.ai/claude-code/apt/stable stable main" \
  | sudo tee /etc/apt/sources.list.d/claude-code.list
sudo apt update && sudo apt install claude-code
```

```bash
# 설치 확인
claude --version
claude doctor
```

---

### 로컬 LLM 백엔드로 전환 (Mac 프록시 경유)

> Mac에서 프록시가 실행 중이어야 함 → `start.sh` 참고.

**임시 전환 (현재 세션만):**
```bash
export ANTHROPIC_BASE_URL=http://192.168.219.102:4000
export ANTHROPIC_API_KEY=local
claude
```

**Tailscale 경유 (다른 네트워크):**
```bash
export ANTHROPIC_BASE_URL=http://100.69.3.50:4000
export ANTHROPIC_API_KEY=local
claude
```

**원래 Claude(Anthropic)로 복귀:**
```bash
unset ANTHROPIC_BASE_URL
unset ANTHROPIC_API_KEY
claude
```

**~/.bashrc에 별칭 추가 (편의용):**
```bash
echo 'alias claude-local="ANTHROPIC_BASE_URL=http://192.168.219.102:4000 ANTHROPIC_API_KEY=local claude"' >> ~/.bashrc
echo 'alias claude-real="unset ANTHROPIC_BASE_URL ANTHROPIC_API_KEY && claude"' >> ~/.bashrc
source ~/.bashrc
```

이후 `claude-local` / `claude-real` 로 빠르게 전환.

### 모델 라우팅 (proxy.mjs) — 2026-08-29: 하드코딩 매핑 제거

예전엔 Claude 모델명(opus/sonnet/haiku)별로 실제 로컬 모델을 고정 매핑해뒀었는데,
oMLX에 계속 새 모델을 테스트/교체할 예정이라 **하드코딩을 없앴다**. 지금은 Claude가
어떤 모델명을 요청하든 상관없이, 매 요청마다 oMLX `/v1/models`를 조회해서 **현재
로드된 모델을 그대로 사용**한다 (30초 캐싱). 로그에 `claude-opus-4-8 → <실제모델명>`
식으로 실제 라우팅된 모델이 찍힘.

즉 **oMLX에 새 모델을 올리기만 하면 proxy.mjs는 코드 수정 없이 자동으로 그 모델을 씀.**
(다만 haiku처럼 원래 "빠른 응답" 용도였던 티어까지 무거운 모델로 통일되는 트레이드오프는
있음 — 속도 티어링이 다시 필요해지면 `proxy.mjs`의 `resolveModel()`을 모델별 분기로
되돌릴 것.)

---

## 4단계: Windows — OpenClaw 설치 및 설정

> WSL2 방식이 공식 권장. 네이티브 PowerShell은 도구 호환성 버그 있음.

### 방법 A — WSL2 (권장)

```powershell
# 1. WSL2 설치 (PowerShell 관리자 권한)
wsl --install
# 재부팅 후 Ubuntu 터미널 열기

# 2. WSL 내부에서 Ubuntu와 동일하게 설치
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g openclaw
```

WSL 내부에서 설정:
```bash
mkdir -p ~/.openclaw
cat > ~/.openclaw/config.json << 'EOF'
{
  "models": {
    "providers": {
      "lmstudio": {
        "baseUrl": "http://192.168.219.102:1234/v1",
        "apiKey": "lmstudio",
        "api": "openai-responses",
        "params": {
          "preload": false
        }
      }
    },
    "defaults": {
      "provider": "lmstudio",
      "model": "qwen/qwen3.6-35b-a3b",
      "contextWindow": 32768,
      "maxTokens": 8192
    }
  }
}
EOF
openclaw "안녕, 작동하니?"
```

---

### 방법 B — 네이티브 PowerShell

```powershell
# 1. Node.js 22 설치 (winget)
winget install OpenJS.NodeJS.LTS

# 2. PowerShell 재시작 후
npm install -g openclaw
openclaw onboard
```

온보딩 입력값:
```
Provider:  lmstudio
Base URL:  http://192.168.219.102:1234/v1
API Key:   lmstudio
Model:     qwen/qwen3.6-35b-a3b
```

config 파일 위치 (Windows):
```
%APPDATA%\openclaw\config.json
```

직접 편집:
```powershell
$config = @'
{
  "models": {
    "providers": {
      "lmstudio": {
        "baseUrl": "http://192.168.219.102:1234/v1",
        "apiKey": "lmstudio",
        "api": "openai-responses",
        "params": { "preload": false }
      }
    },
    "defaults": {
      "provider": "lmstudio",
      "model": "qwen/qwen3.6-35b-a3b",
      "contextWindow": 32768,
      "maxTokens": 8192
    }
  }
}
'@
New-Item -ItemType Directory -Force "$env:APPDATA\openclaw" | Out-Null
$config | Set-Content "$env:APPDATA\openclaw\config.json" -Encoding UTF8
```

---

## 5단계: Windows — Claude Code CLI 설치 및 설정

### 설치

**방법 1 — PowerShell 인스톨러 (권장, 관리자 권한 불필요):**
```powershell
irm https://claude.ai/install.ps1 | iex
```

**방법 2 — WinGet:**
```powershell
winget install Anthropic.ClaudeCode
```

**방법 3 — WSL 내부 (WSL 쓰는 경우):**
```bash
curl -fsSL https://claude.ai/install.sh | bash
```

```powershell
# 설치 확인
claude --version
claude doctor
```

> Git for Windows가 있으면 Bash 도구 사용 가능. 없으면 PowerShell 기본 사용.

---

### 로컬 LLM 백엔드로 전환

> Mac에서 프록시 실행 중이어야 함 → `start.sh` 참고.

**PowerShell — 세션 중 임시 전환:**
```powershell
$env:ANTHROPIC_BASE_URL = "http://192.168.219.102:4000"
$env:ANTHROPIC_API_KEY = "local"
claude
```

**PowerShell — 원래 Claude로 복귀:**
```powershell
Remove-Item Env:\ANTHROPIC_BASE_URL
Remove-Item Env:\ANTHROPIC_API_KEY
claude
```

**영구 환경변수 등록 (사용자 수준):**
```powershell
[System.Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", "http://192.168.219.102:4000", "User")
[System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "local", "User")
# PowerShell 재시작 후 적용
```

**영구 등록 해제:**
```powershell
[System.Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $null, "User")
[System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", $null, "User")
```

**Tailscale 경유 (다른 네트워크):**
```powershell
$env:ANTHROPIC_BASE_URL = "http://100.69.3.50:4000"
```

**WSL 내부 사용 시:** Ubuntu 방법과 동일 (`export` 명령어 사용).

---

## Mac 프록시 관리 (port 4000)

> OpenClaw는 LM Studio 직접 연결이므로 프록시 불필요.
> Claude Code CLI on XPS 사용 시에만 필요.

```bash
# 시작
~/Claude_works/local-llm/start.sh

# 종료
pkill -f "proxy.mjs"

# 상태 확인
lsof -iTCP:4000 -sTCP:LISTEN
```

### 프록시 자동 시작 설정 (Mac 로그인 시 항상 켜두기)

**1. plist 파일 생성:**

```bash
cat > ~/Library/LaunchAgents/com.sykim.llm-proxy.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sykim.llm-proxy</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/sykim/.nvm/versions/node/v24.16.0/bin/node</string>
        <string>/Users/sykim/Claude_works/local-llm/proxy.mjs</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LM_STUDIO_URL</key>
        <string>http://127.0.0.1:8766</string>
        <key>LLM_API_KEY</key>
        <string>omlx</string>
        <key>PORT</key>
        <string>4000</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/llm-proxy.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/llm-proxy-error.log</string>
</dict>
</plist>
EOF
```

**2. 등록:**
```bash
launchctl load ~/Library/LaunchAgents/com.sykim.llm-proxy.plist
```

**3. 확인:**
```bash
launchctl list | grep llm-proxy
curl http://localhost:4000/
```

**4. 해제:**
```bash
launchctl unload ~/Library/LaunchAgents/com.sykim.llm-proxy.plist
rm ~/Library/LaunchAgents/com.sykim.llm-proxy.plist
```

---

## 6단계: Mac — Hermes Agent 사용 (로컬 LLM 연결)

> NousResearch Hermes Agent v0.16.0 — 영구 메모리, 자체 개선 학습 루프, 멀티 에이전트 지원.
> **설치 위치**: `~/Claude_works/hermes-agent/` (완전히 자급자족, 삭제도 깔끔)

### 빠른 시작

```bash
# 편의 래퍼로 실행 (HERMES_HOME 자동 설정)
~/Claude_works/hermes-agent/hermes chat

# 단일 쿼리 (비대화형, 스크립트용)
~/Claude_works/hermes-agent/hermes chat -q "파이썬 코드 리뷰해줘" -Q

# 특정 모델 지정
~/Claude_works/hermes-agent/hermes chat -m "google/gemma-4-31b"
```

### 설치 구조

```
~/Claude_works/hermes-agent/
├── hermes          ← 편의 래퍼 (HERMES_HOME 자동 설정)
├── app/            ← 소스 코드 (git clone, 업데이트 시 교체됨)
│   └── venv/bin/hermes  ← 실제 실행 바이너리
├── data/           ← 설정/메모리/기록 (HERMES_HOME)
│   ├── config.yaml ← LM Studio 연결 설정
│   ├── .env        ← API 키 (LM Studio는 키 불필요)
│   └── node/       ← Hermes가 자체 관리하는 Node.js
└── install.sh      ← 공식 설치 스크립트 (참고용)

~/.local/bin/hermes ← PATH 래퍼 (5줄, app/venv를 가리킴)
```

### oMLX 연결 설정 (data/config.yaml) — 2026-08-29 갱신

현재 설정:
```yaml
model:
  provider: "lmstudio"
  default: "Qwen3.8-Flash-Next-oQ4e-128k"
  base_url: "http://127.0.0.1:8766/v1"
  api_key: "omlx"
  context_length: 65536
auxiliary:
  compression:
    provider: "lmstudio"
    model: "Qwen3.8-Flash-Next-oQ4e-128k"
    base_url: "http://127.0.0.1:8766/v1"
    api_key: "omlx"
delegation:
  model: "Qwen3.8-Flash-Next-oQ4e-128k"
```

> **⚠️ 중요한 함정 (2026-08-29 실제로 겪음)**: `config.yaml`의 `model.api_key`는
> `hermes chat` CLI 같은 단순 경로에서만 쓰이고, **게이트웨이(텔레그램 등)의 실제
> 대화 루프는 이 값을 안 읽는다.** 대신 `provider: lmstudio`는 내부적으로
> `LM_API_KEY` **환경변수**로 키를 조회한다(`hermes_cli/auth.py`의
> `PROVIDER_REGISTRY`). 즉 **게이트웨이 launchd plist(`ai.hermes.gateway.plist`)의
> `EnvironmentVariables`에 `LM_API_KEY`를 반드시 같이 넣어야** 텔레그램 등에서
> 401(Invalid API key)이 안 난다. `config.yaml`만 고치고 이걸 빼먹으면 CLI 테스트는
> 멀쩡히 통과하는데 실제 봇 응답만 계속 예전 모델/에러로 나오는 혼란스러운 증상이 생김.

Tailscale 경유 시 (다른 Mac/원격에서 이 Hermes를 쓰는 경우):
```bash
# data/config.yaml에서 base_url 변경
base_url: "http://100.69.3.50:8766/v1"
```

### 모델 전환

oMLX는 폴더당 모델 하나만 서빙하므로, "여러 모델 중 골라 쓰기"가 아니라 현재 oMLX에
로드된 모델명을 `config.yaml`의 `default`(및 `auxiliary.compression.model`,
`delegation.model`)에 그대로 맞춰주는 방식이다.

```bash
# 현재 oMLX 모델 확인
curl http://127.0.0.1:8766/v1/models -H "Authorization: Bearer omlx"

# 단일 세션만 다른 모델 지정 (예: 아직 켜져 있다면 LM Studio 쪽 canary)
~/Claude_works/hermes-agent/hermes chat -m "qwen/qwen3.6-35b-a3b" --provider lmstudio
```

크론 잡별로 모델을 고정해둔 경우(`data/cron/jobs.json`의 `model` 필드)는 `config.yaml`을
바꿔도 안 따라오니 **따로 갱신**해야 함 — 2026-08-29에 데일리 뉴스 크론 2개가 이 함정에
걸렸었음.

### PATH 설정 (선택사항)

`hermes` 명령어를 어디서든 쓰고 싶다면:

```bash
# ~/.zshrc에 추가
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# 또는 alias 방식
echo 'alias hermes="HERMES_HOME=$HOME/Claude_works/hermes-agent/data $HOME/.local/bin/hermes"' >> ~/.zshrc
```

### Hermes 업데이트

```bash
HERMES_HOME=~/Claude_works/hermes-agent/data ~/.local/bin/hermes update
```

### 완전 제거

외부로 나간 파일은 딱 2개:

```bash
# 1. 래퍼 바이너리 삭제
rm ~/.local/bin/hermes

# 2. PATH 라인 제거 (~/.zshrc에 추가한 경우)
# ~/.zshrc 열어서 hermes 관련 줄 삭제

# 3. 모든 데이터/코드 삭제
rm -rf ~/Claude_works/hermes-agent/
```

> `~/.hermes/` 같은 홈 디렉토리 잔재 없음. HERMES_HOME을 지정해 설치했기 때문.

---

## Claude Code Desktop (이 Mac) — MCP 도구

`~/Library/Application Support/Claude/claude_desktop_config.json`에 `mcpServers` 추가됨.
Claude는 Anthropic API 그대로. 로컬 LLM은 아래 도구로만 추가 (2026-08-29: oMLX 연결로 교체):

| 도구 | 설명 |
|------|------|
| `chat` | 로컬 LLM(oMLX)에 질문 |
| `review_code` | 로컬 LLM으로 코드 리뷰 |
| `list_models` | 현재 oMLX에 로드된 모델 목록 |
| `analyze_image` | **폐기됨** — 현재 oMLX 모델이 text-only라 비전 미지원. 이미지 분석은 Claude/GPT로 직접 처리 |

MCP 서버 실행 파일: `~/Claude_works/local-llm/mcp_server.mjs` (stdio, Claude Desktop 전용),
원격 PC용은 `mcp_server_http.mjs`(포트 4100, launchd `com.sykim.local-llm-mcp-http`).
설정 변경 후엔 **Claude Desktop 앱 재시작 필요** (stdio 서버는 앱 시작 시 한 번만 스폰됨).

---

## 비활성화된 자동화 스크립트 (2026-08-29)

메인 엔진이 oMLX로 바뀌면서, LM Studio 전용으로 짜여있던 아래 launchd 잡들을
**삭제하지 않고 `.disabled` 접미사만 붙여서** 껐다. 전부 `qwen3.6-35b-a3b`/
`qwen3.8-27b`가 LM Studio에 없으면 자동으로 다시 로드하는 로직이라, 켜둔 채로
oMLX로 넘어가면 계속 옛날 모델이 되살아나는 문제가 있었음 (실제로 겪었음).

| 잡 이름 | 원래 역할 | 비활성화 사유 |
|---|---|---|
| `ai.boomco.lmstudio-watchdog` | LM Studio API 헬스체크 후 문제 시 모델 재로드 | LM Studio 전용, oMLX엔 해당 없음 |
| `ai.boomco.lmstudio-models` | `qwen3.6-35b-a3b`+`qwen3.8-27b` 상시 로드 유지 | 위와 동일, 새 모델도 하드코딩돼 있어서 유지보수 필요 |
| `ai.boomco.cron-self-heal` | Hermes 크론 인프라 자가치유 (컨텍스트 길이·게이트웨이 상태·`qwen3.6-35b-a3b` 런타임 확인) | `MODEL = "qwen/qwen3.6-35b-a3b"`가 소스에 하드코딩돼 있어서, 이걸 켜두면 LM Studio에 그 모델이 없다고 판단하고 계속 되살림 |

**재활성화 방법** (LM Studio로 되돌아갈 경우):
```bash
cd ~/Library/LaunchAgents
mv ai.boomco.lmstudio-watchdog.plist.disabled ai.boomco.lmstudio-watchdog.plist
mv ai.boomco.lmstudio-models.plist.disabled ai.boomco.lmstudio-models.plist
mv ai.boomco.cron-self-heal.plist.disabled ai.boomco.cron-self-heal.plist
launchctl bootstrap gui/501 ai.boomco.lmstudio-watchdog.plist
launchctl bootstrap gui/501 ai.boomco.lmstudio-models.plist
launchctl bootstrap gui/501 ai.boomco.cron-self-heal.plist
```

`cron_self_heal.py`를 oMLX 기준으로 다시 쓰려면 `MODEL` 상수와 `lms ps` 기반 관찰 로직을
oMLX API(`/v1/models`) 기준으로 바꿔야 함 — 지금은 손 안 댐.

`ai.boomco.llama-canary` / `ai.boomco.llama-canary-probe`는 **그대로 유지** — LM Studio
앱과 무관하게 자체 llama.cpp 바이너리로 직접 도는 헬스체크/저지연 캐너리라 영향 없음.

---

## 원복 방법

### MCP 도구만 제거 (Claude Code Desktop)
```bash
python3 -c "
import json, os
path = os.path.expanduser('~/Library/Application Support/Claude/claude_desktop_config.json')
with open(path) as f: d = json.load(f)
d.pop('mcpServers', None)
with open(path, 'w') as f: json.dump(d, f, indent=2, ensure_ascii=False)
print('완료 — Claude Code Desktop 재시작 필요')
"
```

### 프록시 자동 시작 해제
```bash
launchctl unload ~/Library/LaunchAgents/com.sykim.llm-proxy.plist
rm ~/Library/LaunchAgents/com.sykim.llm-proxy.plist
```

### 전체 제거
```bash
# 1. MCP 제거
python3 -c "import json,os; p=os.path.expanduser('~/Library/Application Support/Claude/claude_desktop_config.json'); d=json.load(open(p)); d.pop('mcpServers',None); json.dump(d,open(p,'w'),indent=2,ensure_ascii=False)"
# 2. 프록시 종료 및 자동시작 해제
pkill -f "proxy.mjs"
launchctl unload ~/Library/LaunchAgents/com.sykim.llm-proxy.plist 2>/dev/null
rm -f ~/Library/LaunchAgents/com.sykim.llm-proxy.plist
# 3. 파일 삭제
rm -rf ~/Claude_works/local-llm
# 4. Node.js 제거 (선택 — 다른 용도로 쓰면 유지)
rm -rf ~/.nvm
```

---

## 파일 목록

| 파일 | 역할 |
|------|------|
| `proxy.mjs` | Anthropic ↔ OpenAI 변환 프록시 (port 4000, Claude Code CLI용). 2026-08-29: 모델 하드코딩 제거, oMLX 동적 조회로 전환 |
| `mcp_server.mjs` | Claude Code Desktop MCP 서버 (stdio: chat / review_code / list_models / analyze_image) |
| `mcp_server_http.mjs` | 원격 PC용 MCP HTTP 서버 (포트 4100, 같은 도구셋) |
| `start.sh` | 프록시 시작 스크립트 |
| `models/` | oMLX 모델 폴더들 (모델당 하위 디렉토리 하나) |
| `SETUP.md` | 이 문서 |
| `MODEL_EVALUATION.md` | 모델별 평가 체크리스트 + 실측 기록 (새 모델 테스트할 때 필독) |

> `litellm_config.yaml`(구 대안, 미사용)은 2026-08-29 삭제.
