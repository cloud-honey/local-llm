# 붐엘 — Hermes를 거치지 않는 두 번째 텔레그램 채널

봇: **@boomllm_bot** (표시 이름 '붐엘', id 8835560104) · 2026-09-08 가동 · 2026-09-13 구조 개편

Hermes 게이트웨이를 **전혀 거치지 않고** 로컬 LLM(oMLX)에 직접 붙는 독립 텔레그램 봇.
영혼(SOUL.md)과 붐코 분석 파이프라인은 Hermes 쪽 맥붐과 **같은 것을 그대로 쓴다.**

```
[텔레그램]
   │
   ├─ 봇 A (기존) ──► Hermes 게이트웨이 ──┐
   │                                      ├──► oMLX 127.0.0.1:8766 (같은 모델)
   └─ 봇 B (이것) ──► macboom_direct.py ──┘
                          │
                          ├─ 메인 스레드: 폴링·/명령·승인 응답 (작업 중에도 즉답)
                          ├─ 워커 스레드: 디스크 큐(state/task-queue.json)를 순차 처리
                          │     ├─ 일반 요청 → 자체 도구 루프 (OpenAI function calling)
                          │     └─ X 링크   → boomco-x 플러그인 (Hermes와 같은 모듈 import)
                          └─ SOUL.md (Hermes와 같은 파일을 읽음)
```

## 왜 만들었나

- Hermes가 다른 작업으로 바쁠 때 생기는 인터럽트/큐 경로(`busy_input_mode`)에 영향받지 않는
  **독립 채널**이 필요했다.
- 샘플링 파라미터(`reasoning_effort`, `max_tokens`)를 채널이 직접 통제할 수 있다.
- 프로세스가 완전히 분리돼 있어 한쪽이 죽어도 다른 쪽은 산다.

## Hermes와 같은 것 / 다른 것

| | 맥붐 (Hermes) | 붐엘 (이것) |
|---|---|---|
| 이름 | 맥붐 | 붐엘 (`agent.persona_name`) |
| 영혼 | `hermes-agent/data/SOUL.md` | **같은 파일** (`/soul`로 리로드) |
| 두뇌 | oMLX 8766 | **같은 서버·같은 모델** (서버 `default_model`을 따름) |
| 붐코 X 분석 | `plugins/boomco-x` | **같은 모듈을 import** → 같은 `boomco_analyzer.py` 서브프로세스, 같은 감사로그 |
| 토스 조회 | `plugins/toss-query` | 같은 XPS 프록시(:8092)에 같은 GET (얇은 사본) |
| 셸·파이썬·파일·웹 | 있음 | 있음 (자체 구현, /stop 으로 중단 가능) |
| 위험 명령 승인 | Hermes 승인 시스템 | 텔레그램 인라인 버튼 — **파괴적 작업에만** |
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

## 코드 수정 반영 (재시작)

- **코드(.py)만 고쳤을 때**: 텔레그램에서 `/restart` (작업 중이면 `/stop` 후, 또는 `/restart now`).
  터미널이라면:
  ```zsh
  launchctl kickstart -k gui/$(id -u)/com.sykim.macboom-direct
  ```
  KeepAlive라 프로세스가 끝나면 launchd가 10초 안에 다시 띄운다. 기동 로그는 `bot.out.log`의
  `기동. 봇: boomllm_bot PID ...` 줄.
- **plist를 고쳤을 때**: `kickstart -k`로는 반영이 안 된다. `bootout` 후 `bootstrap` 할 것.
- 재시작 전에 큐에 남아 있던 X 링크는 이어서 처리하고, **처리 도중이던 일반 요청은 자동 재개하지
  않고** "중단됐다"고만 알린다(파일 수정이 두 번 일어나는 걸 막기 위해). 대기 중이던(아직 시작 안 한)
  일반 요청은 이어서 처리한다.

## 텔레그램 명령 (`/` 치면 메뉴에 뜸 — 기동 시 setMyCommands 로 등록)

| 명령 | 설명 |
|---|---|
| (그냥 대화) | SOUL.md 페르소나로 도구를 써서 처리. 작업 중에 보내면 대기열에 쌓인다 |
| X 링크만 보내기 | 붐코 파이프라인 자동 실행 (Hermes 훅과 같은 판정 함수) |
| `/status` | **붐엘 상태** — 작업 중인지, 몇 단계째, 마지막 도구, 승인 대기 여부, 대기열, 설정, 가동 시간 + oMLX·프로세스·Hermes 큐 |
| `/stop` | 진행 중인 작업 중단 (실행 중인 셸도 죽임). `/stop all` 은 대기열까지 비움. 붐코 분석 중엔 중단 불가 |
| `/queue` | 대기열(일반 요청·X 링크) 목록 |
| `/log [n]` | `bot.out.log` 마지막 n줄 (기본 20, 최대 200) — 무슨 도구를 돌렸는지 |
| `/set` | 런타임 설정 보기. `/set steps 60` · `/set effort low` · `/set tokens 8000` · `/set progress off` (재시작하면 config.yaml 값으로) |
| `/new` | 대화 기록 초기화 (작업 중이면 그 턴만 새 기록으로 남김) |
| `/model` | 현재 로드된 모델 |
| `/soul` | SOUL.md 다시 읽기 |
| `/plan` | 계획 현황. `/plan resume` 멈춘 단계부터 재개, `/plan cancel` 취소 |
| `/memory` | 자동 요약 기억 보기. `/memory clear` 로 비움(백업 남김) |
| `/notes` | 프로젝트 노트 목록 (`~/Claude_works/boomel-notes/`) |
| `/restart` | 붐엘 프로세스 재시작(코드 반영). 작업 중이면 거절 — `/restart now` 로 강제 |
| `/id` | 내 chat id |
| `/help` | 도움말 |

작업 중에는 텔레그램에 진행 표시 한 줄이 계속 갱신된다:
`🔧 3/40 run_shell · cd ~/stock-monitor && python3 - <<'PY' (+12줄)` — 단계/한도, 도구, 명령 첫 줄.

## 장기 기억 (2026-09-14, `memory.py`)

claude-mem 같은 별도 서비스 없이 세 층으로 기억한다. 전부 매 요청 시스템 프롬프트에 붙는다.

| 층 | 저장 위치 | 누가 쓰나 | 언제 |
|---|---|---|---|
| **자동 요약 기억** | `state/memory-<chat>.md` | 봇이 로컬 모델로 자동 | 대화 기록이 상한(4만 자)을 넘어 오래된 턴이 잘릴 때, `/new` 로 지울 때 |
| **프로젝트 노트** | `~/Claude_works/boomel-notes/<이름>.md` | 모델이 `note_list/read/write/append` 도구로, 마스터도 직접 편집 | 새 프로젝트 시작·작업 완료·중요 결정 시 (프롬프트 규칙) |
| **Hermes 기억 공유** | `hermes-agent/data/memories/MEMORY.md`, `USER.md` (읽기 전용) | Hermes 쪽 맥붐 | `memory.hermes_shared: true` 일 때만. **기본 off** (마스터 지시: 섞이지 않게) — 켜도 `[Hermes 공유]` 별도 블록으로만 표시 |

- 요약은 "무엇을 했고, 어떤 파일을 바꿨고, 무엇이 남았는지" 불릿 5~12줄. 도구 출력 원문은 넣지 않는다.
- 요약 파일이 `memory.max_chars`(6000)를 넘으면 모델로 한 번 더 압축한다. 모델이 실패하면 최신 부분만 남긴다.
- 프로젝트 노트는 목록(이름·갱신일·첫 줄)만 프롬프트에 들어가고, 본문은 모델이 `note_read` 로 읽는다.
  구조 권장: `# 제목 / 개요 / 현재 상태 / 다음 할 일 / 결정 사항 / ## 이력`. `최종 갱신:` 줄은 자동.
- 한계: 요약은 로컬 모델이 만들므로 빠뜨리거나 잘못 적을 수 있다. 프롬프트에 "현재 상태는 파일/셸로 다시
  확인하라"고 적어 두었고, 중요한 건 노트로 남기는 게 안전하다.
- 요약·압축·노트 쓰기 결과는 `redact_secrets` 를 거친다(`password: …`, `계정 user/pass`, 텔레그램·API 토큰,
  Bearer). 2026-09-17 대시보드 관리자 비밀번호가 요약에 평문으로 들어가 매 요청 모델에 가던 사고 대응.
- 프롬프트에는 요약 파일의 뒤쪽 `memory.prompt_max_chars`(3500)만 넣는다. 파일 상한 `max_chars` 4000.

## 프롬프트 크기와 oMLX 프리픽스 캐시 (2026-09-17 실측)

| 구성 | 토큰(개편 전) | 비고 |
|---|---|---|
| SOUL.md | 3,377 | 붐엘 무관 섹션(GPT 폴백·delegate_task·음성 전사·`hermes status`)을 `agent.soul_skip_sections` 로 제외 → 약 40% 절감 |
| 도구 스키마 | 약 1,700 | 그대로 |
| 기억 블록 | 약 2,500 | 요약 3,500자 상한 + 안내문 축약 |
| 부록 | 약 800 | 시각 제거 |
| 합계 | 8,548 → 약 5,500 | |

프리필은 콜드 13.9초 vs 프리픽스 캐시 적중 1.5초. 그래서 **시스템 프롬프트를 요청 사이에 고정**하는 게
용량 절감보다 중요하다 — 매분 바뀌던 `현재 시각` 을 시스템 프롬프트에서 빼고 사용자 메시지 앞에
`[YYYY-MM-DD HH:MM]` 로 붙인다. 시스템 프롬프트가 바뀌는 경우는 SOUL.md 수정, 기억 요약 갱신,
노트 목록 변경, 모델 교체뿐이다.

## 큰 일은 계획으로 쪼개서 (2026-09-17, `plan_create`)

하위 작업 4개 이상·30분 이상이 예상되는 요청은 모델이 `plan_create(goal, steps[], note?)` 로 3~8개
단계를 정의한다(프롬프트 규칙). 그러면 봇이 **단계마다 새 컨텍스트**(시스템 프롬프트 + 계획 현황 +
이전 단계 결과 요약)로 도구 루프를 돌리고, 단계가 끝날 때마다 텔레그램에 `✅ 단계 k/N 제목 — 결과`를
보낸다. 마지막에 단계 결과를 모아 최종 보고를 만든다.

- 단계 안에서 모델은 `plan_step_done(status, summary)` 로 끝낸다(summary 는 다음 단계에 넘어가는 사실).
  필요해진 작업은 `plan_add_steps` 로 끼워 넣는다. 단계당 도구 한도 `agent.plan_step_max_steps`(30).
- `/stop` 이면 계획이 일시정지되고, `이어서 진행해`·`/plan resume` 로 멈춘 단계부터 재개한다.
  단계가 막히면(blocked) 자동으로 일시정지하고 사유를 보고한다. `/plan` 으로 현황, `/plan cancel` 로 취소.
- 계획은 `state/plan-<chat>.json` 에 영속화. 대화 기록에는 요청·계획 생성·최종 보고만 남고 단계 내부의
  도구 왕래는 기록에 쌓이지 않는다(컨텍스트 폭증의 구조적 해결). `note` 를 주면 완료 시 노트 이력에 한 줄.

## 도구 반복 한도 (`agent.max_tool_steps`, 기본 120) 와 턴 중간 컨텍스트 압축

한 요청에서 모델이 도구를 부를 수 있는 왕복 횟수. 한도에 닿으면 **뚝 끊지 않고** 모델에게 도구
없이 한 번 더 물어 "한 일 / 확인된 결과 / 남은 일" 정리 보고를 받은 뒤 멈춘다. 마스터가
'이어서 진행해'라고 보내면 남은 일부터 재개한다. `/set steps N` 으로 런타임 조정(최대 1000).

**단계 수의 실제 제약은 컨텍스트(128K 토큰)였다** — 한 요청 안에서는 도구 결과(최대 6000자)가 전부
쌓이므로 80단계 근처에서 한계(실측 76단계·70분 통과가 최대). 2026-09-16부터 매 모델 호출 전에 진행
중인 턴 크기를 재고 `agent.turn_compact_chars`(기본 20만 자)를 넘으면:
1. 최근 `compact_keep_steps`(10)단계를 제외한 오래된 도구 결과와 큰 도구 인수(write_file 내용 등)를
   "(도구 결과 생략 — 원래 N자)" 한 줄로 교체
2. 그래도 크면 오래된 부분을 로컬 모델로 요약해 assistant 메시지 하나로 접음
텔레그램엔 `🗜 컨텍스트 압축: …` 진행 표시가 뜬다. 이후로는 단계 수보다 **시간**(단계당 30초~1분)이
실질 제약이다.

2026-09-13 이전엔 8단계에서 "요청을 나눠서 다시 시켜주세요"로 끊겼다 — 수정→검증 반복이
기본인 실제 작업(스투시 재고 모니터 구축)에서 세 번 연속 걸림.

## 안전장치 — 승인은 "되돌릴 수 없는 파괴"에만 (2026-09-13 원칙)

`config.yaml`의 `safety`로 조정한다. 기본값은 `macboom_direct.py`의 `DEFAULT_DANGER` /
`DEFAULT_SECRET_PATHS` / `DEFAULT_WRITE_PROTECTED` / `DEFAULT_WRITE_ROOTS`.

**승인 필요**
- 삭제: `rm` 이 임시 디렉터리(`/tmp`, `/var/folders`) 밖을 지우거나 와일드카드를 쓸 때,
  `find -delete`, `xargs rm`, `shutil.rmtree`, `os.removedirs` (파이썬 단일 파일 삭제 `os.remove`·
  `Path.unlink()` 는 2026-09-16부터 자유 — 임시 파일 정리 코드가 밤새 승인 타임아웃을 반복한 사고)
- 되돌릴 수 없는 git: `push --force/-f/--delete`, `reset --hard`, `clean -f`, `checkout --`, `branch -D`, `stash drop`
- 시스템 파괴: `sudo`, `dd if=`, `diskutil erase…`, `mkfs`, `shutdown/reboot`, `defaults write`, `crontab -r`,
  원격 스크립트 파이프 실행(`curl … | sh`)
- 핵심 서비스 종료: `kill/pkill/killall/launchctl bootout…` 이 hermes·omlx·macboom·telegram-bot-api·
  llama-server·boom-steward·local-llm-mcp 를 겨냥할 때, `killall python3` 류
- 비밀정보 읽기/쓰기: `~/.ssh`·`~/.aws`·`~/.gnupg`, Hermes `config.yaml`/`.env`/`auth.json`, 붐엘 `config.yaml`,
  그리고 파일명이 `.env`·`secrets*.json`·`auth.json`·`credentials*`·`*token*`·`*.pem`·`id_rsa` 류인 모든 파일
- 자기 파괴 방지(쓰기만): `SOUL.md`, `macboom_direct.py`, `tools.py`, 붐엘·Hermes·oMLX·telegram-bot-api plist
- 홈 디렉터리 밖(`/etc`, `/Library`, `/usr` …)에 쓰기

**승인 불필요** (예전엔 걸렸던 것들)
- `launchctl load/bootstrap/kickstart`(핵심 서비스 아닌 것), `chmod`, `chown`, `mv`, `kill %1`, `pkill -f 내스크립트`
- `~/Library/LaunchAgents` 를 포함해 **홈 디렉터리 어디든** 파일 쓰기 (`~/stock-monitor/…` 등)
- 일반 `git push` (강제가 아니면). 다시 승인받고 싶으면 `safety.extra_danger_patterns: ['\\bgit\\s+push\\b']`
- `/tmp` 안의 `rm -rf`

승인 요청 메시지에는 어떤 요청을 처리하다 걸렸는지(작업 문장·단계·경과)가 함께 표시된다.
승인은 버튼 또는 `응`/`아니` 텍스트 답장 둘 다 된다. 300초 안에 답이 없으면 자동 거부.
`/stop` 을 보내면 대기 중인 승인도 거부 처리되며 작업이 멈춘다.
거부되면 모델에게 "마스터가 거부했으니 우회하지 말고 다른 방법을 제안하라"는 결과가 돌아간다.
승인 요청·응답은 `bot.out.log`에 `approval ask/yes/no/timeout` 으로 남는다.

## 검증

```zsh
/usr/bin/python3 selftest.py         # 안전장치 45건 + 오프라인 봇 흐름(워커·승인·/stop·/status·큐 복구·기억·노트), 수 초
/usr/bin/python3 selftest.py -v      # 봇→마스터 메시지까지 출력
/usr/bin/python3 selftest.py --llm   # 실제 oMLX 태워서 도구 루프까지 (수 분, 모델 점유)
```

## 알아둘 것 (함정)

- **모델은 하나, 클라이언트는 여럿.** Hermes·이 봇·붐코 파이프라인·MCP가 같은 oMLX를 나눠 쓴다.
  동시에 무거운 작업을 시키면 둘 다 느려진다. 붐코 도구는 Hermes 큐(`state/boomco-x-queue.json`)에
  대기 건이 있으면 그 사실을 결과에 적어 알린다 — 다만 **프로세스 간 락은 없다.**
- **`/stop` 은 모델 응답 생성을 끊지 못한다.** requests 는 진행 중인 요청을 취소할 수 없어 보조
  스레드를 버릴 뿐이고, oMLX 는 그 응답을 끝까지 생성한다(수 분). 그동안 다음 요청이 느릴 수 있다.
  실행 중인 셸/파이썬 도구는 프로세스 그룹째 즉시 죽인다.
- **`reasoning_effort`는 `low` 유지.** `medium`은 대화가 길어질수록 응답이 기하급수적으로
  느려진다(실측: 47K 토큰 세션에서 인사말 하나에 342초 → low로 3.9초).
- **`max_tokens` 8000.** 4000이면 6~7KB 파일을 `write_file` 로 통째로 쓸 때 인수 JSON이 잘려
  (`finish_reason=length`) 빈 인수로 실행됐다. 지금은 잘리면 모델에게 "나눠 쓰라"고 돌려준다.
- Qwen 계열은 사고 과정이 토큰 예산을 다 먹고 본문 없이 끝나는 경우가 있다. 그러면 봇이
  `finish_reason`과 사고 과정 꼬리를 그대로 보여준다. 한국어 문장에 한자가 섞이는 실수도
  있어 시스템 프롬프트에서 금지해 뒀다(모델 한계라 완전히는 못 막는다).
- 붐코 분석 1건은 보통 3~10분, 길면 25분까지 간다(파이프라인 자체 타임아웃 1500초).
- 이 봇은 텍스트만 처리한다. 음성·이미지는 Hermes 쪽 맥붐에게.
- `config.yaml`에 토큰이 들어가므로 **커밋 금지** (`local-llm/.gitignore`에 등록돼 있음).
- 상태 파일: `state/task-queue.json`(대기열), `state/history-<chat>.json`(대화), `state/memory-<chat>.md`(요약 기억),
  `state/telegram-offset.json`.
  구버전 `state/macboom-boomco-queue.json` 은 첫 기동 때 새 큐로 옮기고 지운다.
