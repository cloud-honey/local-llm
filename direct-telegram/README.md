# 붐엘 — Hermes를 거치지 않는 두 번째 텔레그램 채널

봇: **@boomllm_bot** (표시 이름 '붐엘', id 8835560104) · 2026-09-08 가동

Hermes 게이트웨이를 **전혀 거치지 않고** 로컬 LLM(oMLX)에 직접 붙는 독립 텔레그램 봇.
영혼(SOUL.md)과 붐코 분석 파이프라인은 Hermes 쪽 맥붐과 **같은 것을 그대로 쓴다.**

```
[텔레그램]
   │
   ├─ 봇 A (기존) ──► Hermes 게이트웨이 ──┐
   │                                      ├──► oMLX 127.0.0.1:8766 (같은 모델)
   └─ 봇 B (이것) ──► macboom_direct.py ──┘
                          │
                          ├─ SOUL.md            (Hermes와 같은 파일을 읽음)
                          ├─ boomco-x 플러그인   (Hermes와 같은 모듈을 import)
                          └─ 자체 도구 루프       (OpenAI function calling)
```

## 왜 만들었나

- Hermes가 다른 작업으로 바쁠 때 생기는 인터럽트/큐 경로(`busy_input_mode`)에 영향받지 않는
  **독립 채널**이 필요했다.
- 샘플링 파라미터(`reasoning_effort`, `max_tokens`)를 채널이 직접 통제할 수 있다.
  MCP `local-llm chat` 도구가 이걸 못 열어줘서 무거운 작업에 못 쓰던 문제와 같은 맥락.
- 프로세스가 완전히 분리돼 있어 한쪽이 죽어도 다른 쪽은 산다.

## Hermes와 같은 것 / 다른 것

| | 맥붐 (Hermes) | 맥붐 다이렉트 (이것) |
|---|---|---|
| 이름 | 맥붐 | 붐엘 (`agent.persona_name`) |
| 영혼 | `hermes-agent/data/SOUL.md` | **같은 파일** (`/soul`로 리로드) |
| 두뇌 | oMLX 8766 | **같은 서버·같은 모델** (`/v1/models`로 매번 조회) |
| 붐코 X 분석 | `plugins/boomco-x` | **같은 모듈을 import** → 같은 `boomco_analyzer.py` 서브프로세스, 같은 감사로그 |
| 토스 조회 | `plugins/toss-query` | 같은 XPS 프록시(:8092)에 같은 GET (얇은 사본) |
| 셸·파이썬·파일·웹 | 있음 | 있음 (자체 구현) |
| 위험 명령 승인 | Hermes 승인 시스템 | 텔레그램 인라인 버튼 (`safety.danger_patterns`) |
| delegate_task(클라우드 위임) | 있음 | **없음** |
| GPT 폴백 | 있음 | **없음** (실패하면 실패라고 보고) |
| 브라우저·이미지생성·크론·칸반·스킬·음성전사 | 있음 | **없음** |
| 실행 인터프리터 | Hermes venv (3.11) | 시스템 `/usr/bin/python3` (3.9) |

없는 도구를 있는 척하지 않도록, 위 목록은 시스템 프롬프트 뒤에 그대로 덧붙여진다
(`macboom_direct.py`의 `DIRECT_ADDENDUM`).

## 설치

1. **새 봇 만들기** — 텔레그램에서 `@BotFather` → `/newbot` → 이름/username 입력 → 토큰 복사.
   기존 Hermes 봇 토큰을 재사용하면 **두 프로세스가 같은 메시지를 놓고 다퉈서 절반씩 사라진다.**

2. **로컬 API 서버로 옮기기** (이 Mac의 `telegram-bot-api :8081`을 쓰므로 필요):
   ```zsh
   ./setup_bot.sh <새-봇-토큰>
   ```
   클라우드 API에서 `logOut` 한 뒤 로컬 서버 `getMe`가 `"ok":true`면 성공.

   **함정**: 새 봇은 사용자가 먼저 말을 걸기 전에는 봇이 먼저 메시지를 보낼 수 없다
   (`Bad Request: chat not found`). 텔레그램에서 봇을 찾아 `/start`를 한 번 눌러야 한다.

3. **설정**:
   ```zsh
   cp config.example.yaml config.yaml
   # config.yaml 의 telegram.token 에 새 토큰을 넣는다
   ```

4. **띄우기**:
   ```zsh
   cp com.sykim.macboom-direct.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sykim.macboom-direct.plist
   launchctl print gui/$(id -u)/com.sykim.macboom-direct | head -20
   ```
   포그라운드로 먼저 확인하고 싶으면: `/usr/bin/python3 macboom_direct.py`

   **주의**: plist를 고쳤으면 `kickstart -k`로는 반영이 안 된다. `bootout` 후 `bootstrap` 할 것.

## 텔레그램 명령

| 명령 | 설명 |
|---|---|
| (그냥 대화) | SOUL.md 페르소나로 도구를 써서 처리 |
| X 링크만 보내기 | 붐코 파이프라인 자동 실행 (Hermes 훅과 같은 판정 함수) |
| `/new` | 대화 기록 초기화 |
| `/status` | oMLX·관련 프로세스·붐코 큐 실제 상태 |
| `/soul` | SOUL.md 다시 읽기 |
| `/model` | 현재 로드된 모델 |
| `/id` | 내 chat id |

## 안전장치

`config.yaml`의 `safety`로 조정한다. 기본값은 `macboom_direct.py`의
`DEFAULT_DANGER` / `DEFAULT_PROTECTED` / `DEFAULT_WRITE_ROOTS`.

- **위험 패턴**(`rm`, `sudo`, `git push`, `git reset --hard`, `launchctl`, `chmod`, `kill`,
  `curl | sh`, `shutil.rmtree` 등)이 걸린 셸/파이썬은 텔레그램 승인 버튼을 거친다.
- **보호 경로**(`hermes-agent/data/config.yaml`·`.env`·`auth.json`·`SOUL.md`, `~/Library/LaunchAgents`,
  `~/.ssh` 등)는 읽기도 쓰기도 승인이 필요하다.
- **작업 루트**(`~/Claude_works`, `~/sns-tracker`, `/tmp`) 밖에 파일을 쓰면 승인이 필요하다.
- 승인은 버튼 또는 `응`/`아니` 텍스트 답장 둘 다 된다. 300초 안에 답이 없으면 자동 거부.
- 거부되면 모델에게 "마스터가 거부했으니 우회하지 말고 다른 방법을 제안하라"는 결과가 돌아간다.

## 검증

```zsh
/usr/bin/python3 selftest.py         # 안전장치 + X링크 감지 (텔레그램/LLM 불필요, 수초)
/usr/bin/python3 selftest.py --llm   # 실제 oMLX 태워서 도구 루프까지 (수 분)
```

## 알아둘 것 (함정)

- **모델은 하나, 클라이언트는 여럿.** Hermes·이 봇·붐코 파이프라인·MCP가 같은 oMLX를 나눠 쓴다.
  동시에 무거운 작업을 시키면 둘 다 느려진다. 붐코 도구는 Hermes 큐(`state/boomco-x-queue.json`)에
  대기 건이 있으면 그 사실을 결과에 적어 알린다 — 다만 **프로세스 간 락은 없다.**
- **`reasoning_effort`는 `low` 유지.** `medium`은 대화가 길어질수록 응답이 기하급수적으로
  느려진다(실측: 47K 토큰 세션에서 인사말 하나에 342초 → low로 3.9초).
- Qwen 계열은 사고 과정이 토큰 예산을 다 먹고 본문 없이 끝나는 경우가 있다. 그러면 봇이
  `finish_reason`과 사고 과정 꼬리를 그대로 보여준다 — `max_tokens`를 올리거나 요청을 쪼갤 것.
- 붐코 분석 1건은 보통 3~10분, 길면 25분까지 간다(파이프라인 자체 타임아웃 1500초).
- 이 봇은 텍스트만 처리한다. 음성·이미지는 Hermes 쪽 맥붐에게.
- `config.yaml`에 토큰이 들어가므로 **커밋 금지** (`local-llm/.gitignore`에 등록돼 있음).
