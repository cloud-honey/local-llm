# 붐엘 변경 이력

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
