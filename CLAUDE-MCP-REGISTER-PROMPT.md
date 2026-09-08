# 클로드에게 전달할 프롬프트: 맥붐(MacBoom) 로컬 AI를 MCP로 등록

이 문서의 **1절(등록)**은 Claude Code CLI 터미널에서 직접 실행하고,
**2절(전달 프롬프트)**는 그 후 열리는 클로드 대화창에 붙여넣는다.

---

## 0. 사전 확인

- 이 Mac(local-llm 서버): macOS, Tailscale IP `100.69.3.50`, LAN IP `192.168.219.119`
  (LAN IP는 DHCP라 바뀔 수 있음 — `ipconfig getifaddr en0`로 확인)
- 이미 등록된 것: 이 Mac의 Claude Desktop에는 `local-llm`(stdio)이 이미 등록돼 있음
  (`~/Library/Application Support/Claude/claude_desktop_config.json` 확인됨)
  → **이 Mac에서 쓸 추가 등록은 필요 없고, 아래 1-B만 적용.**
- Claude Code CLI는 원격 PC(XPS 등)에 설치돼 있다고 가정. 원격 PC에 `claude` 명령이
  있는지 먼저 확인: `claude --version`

## 1-A. 원격 PC(XPS 등)에 등록 — 추천: HTTP 경로 (port 4100)

터미널에서:

```bash
claude mcp add --transport http local-llm http://100.69.3.50:4100/mcp
```

검증:

```bash
claude mcp list          # local-llm: ✔ Connected
claude mcp get local-llm
```

이 경로로 노출되는 도구(로컬 LLM 직접 호출):
- `chat` — 로컬 모델에 질문 (긴 텍스트 생성·번역·요약 등)
- `review_code` — 코드 리뷰
- `analyze_image` — 이미지 분석 (이미지는 base64로 전달)
- `list_models` — oMLX에 로드된 모델 목록

## 1-B. 맥붐/붐엘 에이전트 자체(메시징 브리지)를 붙일 때

- **이 Mac에서 Claude Code/Desktop를 쓸 때**: 이미 `local-llm`(stdio, chat/review_code/
  analyze_image/list_models)과 Hermes 게이트웨이가 올라 있어 브리지 도구는 Hermes를
  거치는 맥붐 봇과 중복됨. 추가 등록은 이 Mac의 Claude Desktop 설정
  (`~/Library/Application Support/Claude/claude_desktop_config.json`)의 `mcpServers`에
  아래를 더 넣는다:

```json
{
  "mcpServers": {
    "hermes": {
      "command": "/Users/sykim/Claude_works/hermes-agent/hermes",
      "args": ["mcp", "serve"]
    }
  }
}
```

- **원격 PC에서 브리지까지 붙일 때는**: stdio 전용이라 직접 등록 불가. 이 Mac에
  stdio↔HTTP 어댑터(예: `mcp-proxy`)를 띄우고 HTTP로 등록해야 함 — 지금은
  1-A의 local-llm HTTP 경로만 운영 중이고, 브리지용 HTTP 어댑터는 미설치다.

이 경로로 노출되는 도구(브리지): `conversations_list`, `conversation_get`, `messages_read`,
`attachments_fetch`, `events_poll`, `events_wait`, `messages_send`, `channels_list`,
`permissions_list_open`, `permissions_respond` (총 10개, 검증 완료 2026-09-08)

## 2. 클로드 대화창에 붙여넣을 프롬프트

아래 블록을 그대로 복사해서 클로드(Code/Desktop) 첫 메시지에 붙여넣는다:

```text
당신은 지금부터 내 로컬 AI 인프라를 MCP 도구로 사용할 수 있습니다.

[등록된 도구]
1) 서버 이름 `local-llm` (HTTP, http://100.69.3.50:4100/mcp):
   - chat: 로컬 LLM(Qwen3.8-Flash-Next-oQ4e-128k, 128k 컨텍스트)에게 질문
   - review_code: 코드 리뷰 요청
   - analyze_image: 이미지 분석
   - list_models: 서버에 로드된 모델 목록 조회
2) 서버 이름 `hermes` (stdio, 이 Mac 위일 때만):
   - conversations_list / messages_read / messages_send 등 10개
     → 텔레그램의 '맥붐' 봇과 같은 세션·메시지를 공유하는 브리지

[사용 규칙]
- 간단한 계 산·코드 생성·1차 초안은 네가 직접 하고,
  대량 텍스트 생성·배치 요약·값싼 반복 작업은 `chat` 도구에 위임하라.
  (이유: 로컬 모델은 생성 속도가 초당 ~25토큰으로 느리지만 공짜다.
  수천 토큰짜리 코드 생성·긴 문서 초안은 chat에 넘기고,
  네가 결과를 검수·수정하는 '클로드 기획 + 로컬 실행' 역할을 맡아라.)
- chat 도구는 reasoning_effort 기본값이 low로 설정돼 있으니 별도의 지시가 없으면 그대로 두고,
  어려운 추론 요청일 때만 파라미터를 올려라.
- 이미지 분석은 이 로컬 모델이 vision을 지원하지 않으므로 당신이 직접 처리하고,
  analyze_image 도구는 쓰지 마라.
- files·git·로컬 셸 작업은 당신의 표준 도구(Bash 등)를 쓰고,
  local-llm 도구는 순수 생성·요약·리뷰에만 써라.
- tool 호출 결과가 이상하면(cloudflare류 타임아웃, 연결 거부) 1회 재시도 후
  실패하면 그대로 보고하고 자체 처리로 대체하라.

[작업 시작]
`list_models`를 먼저 호출해 연결과 모델명을 확인하고, 결과를 한 줄로 보고하라.
그 다음 내가 주는 후속 지시를 기다려라.
```

## 3. 운영 메모 (나=붐엘이 실제로 확인한 것, 2026-09-08)

- oMLX 헬스체크 정상: `curl http://127.0.0.1:8766/health -H "Authorization: Bearer omlx"`
  → `healthy`, 기본 모델 `Qwen3.8-Flash-Next-oQ4e-128k` (메모리 ~97.6GB 적재)
- `hermes mcp serve` 실측 정상: initialize + tools/list에서 10개 도구 확인
- local-llm HTTP 서버(포트 4100)는 launchd(`com.sykim.local-llm-mcp-http`, KeepAlive)로 상주
- 이 Mac의 Claude Desktop에는 이미 `local-llm`(stdio) 등록돼 있음 → duplicates 불필요
- 원격 PC 등록 시 LAN IP(192.168.219.x)보다 Tailscale IP(100.69.3.50)가 안전
- 롤백: 원격 PC에서는 `claude mcp remove local-llm`
