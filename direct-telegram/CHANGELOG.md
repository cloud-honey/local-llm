# 붐엘 변경 이력

## 2026-09-17 — 프롬프트 최적화 4건 + 계획 실행(plan_create)

배경: "기본 MD 파일 용량이 너무 큰 것 같다" → 실측 시스템 프롬프트+도구 8,548토큰(6.7%), 콜드 프리필 13.9초 /
캐시 1.5초. 용량보다 캐시 안정성이 관건. 추가 요청: "너무 큰 일은 알아서 계획 세우고 쪼개서 진행".

- **시각을 시스템 프롬프트에서 제거** → 사용자 메시지 앞 `[날짜 시각]`. 시스템 프롬프트가 요청 사이에 고정돼
  oMLX 프리픽스 캐시 적중.
- **SOUL.md 섹션 건너뛰기** `strip_soul_sections` / `agent.soul_skip_sections`(기본: GPT 폴백·무거운 코드 작성·
  음성·영상 전사·현재 상태 보고). 파일은 Hermes 와 공유라 그대로, 붐엘이 읽을 때만 제외. 모순 지시 제거.
- **비밀값 가림** `memory.redact_secrets`: 요약·압축·노트 쓰기 전부 통과. 기존 기억 파일의 대시보드 관리자
  비밀번호 가림(백업 `memory-<chat>.md.bak-20260917-secret`). 첫 패턴이 `docs/BOOML-GUIDE.md` 를 오탐해
  "점 없는 8자+대소문자 혼합"으로 조임.
- **기억 다이어트**: 파일 상한 6000→4000, 프롬프트 삽입 3500자(최신부터), 압축 프롬프트에 "끝난 일은 한 줄",
  빈 항목 삭제, 노트 안내문 축약.
- **계획 실행**: `plan_create/plan_step_done/plan_add_steps` 도구, `_run_loop` 로 도구 루프를 분리해 단계마다 새
  컨텍스트로 실행, `state/plan-<chat>.json`, `/plan [resume|cancel]`, `/stop` → 일시정지 → '이어서 진행해' 재개,
  단계 blocked 시 자동 일시정지, 최종 보고는 단계 결과를 모아 모델이 작성. 기록엔 요청·계획·최종 보고만.
- selftest: 프롬프트 고정·SOUL 제외·기억 상한·가림·계획(생성→단계→추가→완료, /stop→재개) 추가 — 47+80건 통과.

## 2026-09-16 — 턴 중간 컨텍스트 압축 · 파이썬 단일 파일 삭제 승인 제외 · 승인 메시지에 작업 맥락

배경: `/set steps 80` 으로도 부족한 경우가 있다는 질문. 로그 분석 결과 단계 수가 아니라 한 요청 안에
쌓이는 컨텍스트(128K)가 실제 한계(실측 최대 76단계·70분). 별개로 9/16 새벽 `.unlink(missing_ok=True)` 가
승인 패턴에 걸려 5분 타임아웃 3회 반복, 아침에 "이게 어떤 작업에 대한 승인 요청이야?"라는 질문.

- `_compact_turn`: 매 LLM 호출 전 진행 중인 턴이 `agent.turn_compact_chars`(20만 자)를 넘으면 최근
  `compact_keep_steps`(10)단계 밖의 도구 결과·큰 인수를 스텁으로, 그래도 크면 앞부분을 요약으로 접음.
  in-place 수정이라 저장 기록도 압축본. 기본 한도 40→120, `/set steps` 상한 1000.
- DEFAULT_DANGER 에서 `os.remove/unlink/rmdir`, `.unlink()`, `.rmdir()` 제거 (rmtree·removedirs 만 유지).
- 승인 요청에 `작업: "<요청 문장>" (단계 n/N, 경과)` 줄 추가. 타임아웃 시 동작도 명시.
- `_run_proc` 에 `errors="replace"`: 비 UTF-8 바이트가 섞인 도구 출력에 reader 스레드가 죽어 결과가
  "(출력 없음)" 이 되던 문제(bot.err.log 의 UnicodeDecodeError) 수정.
- selftest: 압축(스텁/요약) 흐름, 승인 맥락, unlink 자유 케이스, 비 UTF-8 출력 추가 — 47+60건 통과.

## 2026-09-14 — 장기 기억 (memory.py)

배경: 붐엘의 기억은 4만 자짜리 대화 기록 파일 하나뿐이라 오래된 턴이 잘리면 맥락이 통째로 사라졌다.
claude-mem 같은 장기 기억이 있냐는 질문에서 시작.

- **자동 요약 기억** `state/memory-<chat>.md`: 기록 상한으로 턴이 잘리거나 `/new` 할 때 잘리는 턴을
  로컬 모델로 요약(한 일·바꾼 파일·결정·남은 일, 불릿 5~12줄)해 날짜와 함께 누적. 6000자 넘으면 재압축.
  매 요청 시스템 프롬프트에 포함. `/memory`, `/memory clear`(백업 남김).
- **프로젝트 노트** `~/Claude_works/boomel-notes/<이름>.md`: `note_list/note_read/note_write/note_append`
  도구. 프롬프트에는 목록만 넣고 관련 요청이면 먼저 읽게 함. `최종 갱신`·`## 이력` 자동. `/notes`.
  첫 노트 `stussy-stock-monitor.md` 생성.
- **Hermes 기억 공유(읽기 전용)**: `hermes-agent/data/memories/MEMORY.md`·`USER.md`(`§` 구분) 읽기 코드.
  **기본 off** (`memory.hermes_shared: false`) — 마스터 지시 "아직 적용 말고, 하더라도 섞이지 않게".
  켜도 `[Hermes 공유]` 별도 블록으로만 표시. 쓰기 공유는 Hermes 드리프트 감지·세션 스냅샷 방식 때문에 안 함.
- 함정: Memory 가 봇 생성 시점의 LLM 객체를 붙들고 있어 selftest 에서 가짜 LLM 대신 실제 oMLX 를 호출함 →
  `lambda: self.llm` 로 지연 참조.
- 적용: 00:36 재시작 (대시보드 작업 종료 확인 후). 명령 메뉴 12개.

## 2026-09-13 — 워커 분리 · 승인 완화 · 명령 메뉴 (스투시 재고 모니터 작업 사고 대응)

배경: 마스터가 `~/stock-monitor` 재고 모니터 구축을 시켰더니 ① 8단계 도구 한도에 세 번 연속 걸려
"요청을 나눠서 다시 시켜라"로 중단 ② launchctl·`~/Library/LaunchAgents` 쓰기·`~/Claude_works` 밖 쓰기마다
승인 버튼 ③ 진행 표시가 `run_shell {"command": "cd ~ && python3 - <<'PY'\n...` JSON 원문.

- **폴링(메인)/작업(워커) 스레드 분리** + 디스크 큐 `state/task-queue.json`(일반 요청·X 링크 통합).
  작업 중에도 `/status`(단계·도구·승인 대기·대기열) `/stop`(셸 프로세스 그룹 kill, 승인 대기 거부) 즉답.
  재시작 시 처리 중이던 일반 요청은 자동 재개 안 하고 알림만(중복 수정 방지), 대기 건과 X 링크는 재개.
- **도구 한도 8 → 40**, 닿으면 `tool_choice: none` 으로 정리 보고를 받고 멈춤. `/set steps N`.
- **승인은 파괴적 작업만**: 임시 디렉터리 밖 rm/와일드카드, force push/hard reset, sudo/dd/diskutil/shutdown,
  핵심 서비스 kill/bootout, 비밀정보(경로+파일명 패턴), SOUL.md·붐엘 코드·핵심 plist 쓰기, 홈 밖 쓰기.
  일반 `git push` 는 자유(되돌리려면 `safety.extra_danger_patterns`).
- 진행 표시 `🔧 3/40 run_shell · <명령 첫 줄> (+N줄)`, 승인 요청은 코드 블록.
- `setMyCommands` 로 `/` 메뉴 등록. `/restart`(os._exit → launchd KeepAlive 재기동), `/log`, `/set`, `/queue`.
- `max_tokens` 4000 → 8000 (6~7KB write_file 인수 잘림 함정), `_caption` 류 언더스코어 키 정리,
  인수 JSON 파싱 실패 시 빈 인수로 실행하지 않고 모델에게 되돌림.
- `selftest.py` 재작성: 안전장치 45건 + 가짜 LLM/텔레그램으로 워커·승인·/stop·/status·큐 복구 흐름.
- 적용: 9/14 00:00 재시작.
