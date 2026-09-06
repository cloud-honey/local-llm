# 로컬 LLM 모델 평가 기록

새 모델(특히 새 아키텍처)을 로컬에 올릴 때마다 반복하는 절차와, 실제로 돌려보면서 확인한
결과를 여기에 누적 기록한다. 목적은 두 가지:

1. **재사용 가능한 평가 체크리스트** — 다음에 새 모델 나왔을 때 뭘 확인해야 하는지
2. **모델별 실측 기록** — 스펙표(모델 카드)에는 없는, 실제 이 Mac에서 돌려보고 체감한 것

---

## 평가 체크리스트 (재사용)

새 모델(GGUF/MLX) 하나를 실전 투입하기 전에 아래 순서로 확인한다.

### 1. 아키텍처 지원 여부부터 확인

새 아키텍처(예: 2026-08-26 나온 Qwen4/`qwen4_exp`)는 모델 파일이 있어도 런타임이 못 읽으면
무용지물. 순서대로 확인:

- llama.cpp: `github.com/ggml-org/llama.cpp/pulls`에서 아키텍처명(`qwen4_exp` 같은)으로 검색 — merged 됐는지
- LM Studio: 자체 번들 llama.cpp/MLX 런타임이 있어서, llama.cpp에 지원이 들어가도 **LM Studio 앱 자체 업데이트가 별도로 필요**. `lmstudio.ai/changelog`에서 확인
- MLX: `mlx-vlm`(Blaizzy) 저장소 PR 확인. 신생 아키텍처는 서드파티 커스텀 런타임(예: oMLX)이 먼저 지원하는 경우가 많음 — 모델 카드의 "requires ..." 문구를 꼭 확인

### 2. 하드웨어 적합성 — 양자화별 용량 확인

- 모델 업로더가 제공하는 양자화 종류/파일 크기를 HF 저장소 `tree/main`에서 직접 확인 (모델 카드 설명과 실제 파일 크기가 다른 경우 있음)
- macOS는 기본적으로 통합메모리의 ~75~84%만 GPU/Metal에 할당 (`iogpu.wired_limit_mb`로 조정 가능) — 이 Mac(128GB)은 실측 상한이 약 **107.5GB** (오늘 오MLX 로그 기준, Apple `max_recommended_working_set_size`)
- **같은 "N-bit" 라벨이어도 제작자마다 실제 용량이 크게 다르다** — 아래 "MLX 양자화 시 주의" 참고

### 3. 설치 후 바로 4종 테스트

모델 로드만 되고 실사용 검증 없이 프로덕션에 넣지 말 것. 최소 아래 4개를 돌려본다:

| 테스트 | 목적 | 방법 |
|---|---|---|
| 1. 기본 Q&A | 로드/응답 확인, 토큰 속도 기준선 | 짧은 질문 + `max_tokens` 충분히 (사고 모델은 200~800) |
| 2. 툴콜 | 에이전트 파이프라인(Hermes 등) 호환성 | OpenAI `tools`/`tool_choice` 스키마로 함수 하나 던져보기 |
| 3. 긴 컨텍스트 (needle-in-haystack) | 실제 컨텍스트 길이가 광고값만큼 나오는지 | 더미 텍스트 8천~1만 토큰 사이에 고유 문자열 숨기고 찾게 하기 |
| 4. 실전 규모 워크로드 | 실제 크론/파이프라인 투입 가능 여부 | 해당 파이프라인이 실제 처리하는 분량(예: boomco 기사 분석 9~18천자)으로 그대로 재현, 소요시간 측정 |

각 테스트에서 **메모리도 같이 관찰** (`top -l 1 -s 0 | grep PhysMem`, `vm_stat`) — 특히 긴 컨텍스트
처리 중 wired 메모리가 얼마나 튀는지, 끝나고 제대로 반환되는지.

### 4. 프로덕션 연동 전 — 하드코딩 잔재 훑기

모델 하나 바꾸는 게 생각보다 넓게 퍼져있다. 아래 grep을 습관적으로 돌릴 것:

```bash
grep -rl "<옛 모델명>\|<옛 base_url>" \
  ~/Claude_works/hermes-agent/data/config.yaml \
  ~/Claude_works/hermes-agent/data/scripts/ \
  ~/Claude_works/hermes-agent/data/cron/jobs.json \
  ~/Claude_works/boomco-curator/ \
  ~/Claude_works/local-llm/ \
  ~/Library/LaunchAgents/ \
  ~/Library/Application\ Support/Claude/claude_desktop_config.json \
  2>/dev/null
```

**특히 잘 놓치는 곳들 (2026-08-29 실제로 걸렸던 것들):**
- `jobs.json`의 크론 잡별 `model` 오버라이드 필드 — Hermes `config.yaml`의 기본 모델과 별개로 존재
- 각 launchd `.plist`의 `EnvironmentVariables` — 스크립트 코드의 기본값을 **덮어씀**. 스크립트만 고치고 plist를 안 고치면 반영 안 됨
- 인증 헤더 자체가 아예 없던 스크립트들 — LM Studio는 API 키를 사실상 검증 안 해서 다들 대충 `Bearer lm-studio`/헤더 없음으로 짜여 있었음. 새 백엔드(oMLX 등)가 인증을 실제로 강제하면 전부 401
- 원격 클라이언트(XPS 등)가 이 Mac의 **LAN IP**로 접속하던 경로 — 로컬 스크립트/설정에서도 `192.168.x.x`를 쓰던 관성이 있어서, 새 로컬 엔진이 `127.0.0.1`에만 바인딩돼 있으면 LAN IP로는 연결 자체가 안 됨 (로컬 호출은 `127.0.0.1`로 통일)

### 5. 앞으로 계속 모델을 바꿔 끼울 거라면 — 하드코딩 대신 동적 조회

`proxy.mjs`(XPS Claude Code CLI 브리지)에 적용한 패턴: Claude 모델명 → 로컬 모델명 고정 매핑을
없애고, 매 요청마다 백엔드 `/v1/models`를 조회해서 **현재 로드된 모델을 그대로 사용** (30초 캐싱).
이렇게 해두면 이후 모델을 바꿔 끼워도 코드 수정이 필요 없다. 다른 곳에도 필요하면 같은 패턴 적용.

---

## 모델별 평가 기록

### ⚠️ 2026-09-04 — 이 저장소 자체가 Hugging Face에서 사라짐 (jedisct1이 후속 빌드로 교체)

"후기 확인해줘" 요청으로 `jedisct1/Qwen3.8-Flash-Next-oQ4e-128k` 페이지에 다시 가보니 **404** —
브라우저 직접 접속도 404, jedisct1 본인 프로필의 모델 목록(85개)에도 더 이상 없음. 대신
같은 컬렉션(`Qwen3.8-Flash-Next`, 2개 항목, "6일 전 업데이트")에 다음 두 개로 교체돼 있음:

- `jedisct1/Qwen3.8-Flash-Next-oQ4e-100K-MTP` (700 다운로드, 좋아요 2)
- `jedisct1/Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP` (1.78k 다운로드, 좋아요 3)

**서비스 영향은 없음** — 우리는 이미 파일을 로컬에 받아 써서 지금 도는 서버엔 지장 없음.
다만 나중에 재설치/디스크 손상 복구 시 원래 경로로 다시 못 받는다는 뜻.

**후속 빌드가 실제로 더 나음 — 검토 가치 있음** (같은 제작자 jedisct1, 신뢰도 그대로):
- **비전 인코더 + MTP 헤드를 다시 살림** (`Qwen4 vision tower`, `MTP head` 보존) — 지금 우리
  `analyze_image` 도구가 죽어있는 문제를 같은 제작자·같은 계열 내에서 해결할 수 있는 선택지.
  실제로 "identified Half Dome from a real JPEG"로 비전 동작 검증됨.
- 컨텍스트는 262,144 네이티브지만 **128GiB Mac 안전선으로 100,000 권장** (지금 쓰는
  131,072보다 줄어듦 — 128K 캐시 사용 시 99.96GiB까지 치솟아 balanced guard에 걸림 확인됨).
- 파일 크기 108.8GB (22샤드) — 지금 86.6GB보다 22GB 큼.
- **`mtp_enabled: false` 권장** — MTP 켜면 툴콜 테스트 10건 중 9건만 가드 통과(1건은 정확한
  인자를 스펙큘레이티브가 바꿔버려 여분 호출 발생). MTP 꺼두면 50/50 통과 + 가드 10/10 통과.
- oMLX 0.6.3에서 로드 검증됨. 권장 설정: `max_context_window: 100000`,
  `qwen4_ple_ssd_offload: true`, `mtp_enabled: false`, `reasoning_effort: medium`(제작자
  권장값 — 우리 실측상 low가 나았던 것과 다름, 바꿀 경우 재검증 필요).
- **아직 다운로드/좋아요가 매우 적어(700/2) 실사용 후기는 전무** — 신뢰는 제작자 이력에서
  오는 것이고, 우리가 직접 검증 안 하면 위 스펙시트가 유일한 근거.
- **재검토 조건**: 이미지 분석이 실제로 필요해지는 시점에 이 빌드부터 먼저 테스트
  (Vontra oQ4 111.69GB보다 20GB 이상 가벼우면서 같은 제작자 신뢰도 보유). 그 전까진
  지금 빌드(로컬에 이미 있음) 계속 사용.

### Qwen3.8-Flash-Next-oQ4e-128k (jedisct1, oMLX 런타임) — 2026-08-28 평가, 2026-08-29 프로덕션 투입

**기반 모델**: `Qwen/Qwen3.8-Flash-Next` (Qwen팀, 2026-08-26 공개, Qwen4 프리뷰 아키텍처)
**양자화 제작자**: jedisct1 (libsodium/minisign 저자 — 검증된 보안 엔지니어, 신뢰도 높음)
**HF 경로**: `jedisct1/Qwen3.8-Flash-Next-oQ4e-128k`

#### 스펙

- 총 125B / 활성 6B 파라미터 (MoE, 전문가 512개 중 11개 활성 = 10 라우팅 + 1 공유)
- 추가로 51B 규모의 해시 n-gram 임베딩 테이블(PLE, 별도 취급), 4B MTP 헤드(**이 빌드엔 미포함** — text-only tool-use 프로파일)
- 아키텍처: Gated DeltaNet + Qwen Sparse Attention 하이브리드, 48레이어
- 컨텍스트: 원 아키텍처 최대 262K~1M, 이 빌드는 **131,072로 제한** (128GB Mac 프로필에 맞춰 제작자가 의도적으로 제한)
- 실제 파일 크기: 86.6GiB (safetensors 18샤드) — 4bit affine(토큰임베딩/LM head) + importance-matrix 기반 혼합정밀도(대형 전문가 가중치) + attention/공유전문가는 8bit 이상 유지 + n-gram 뱅크는 샤드별 2~3bit
- 라이선스: Qwen 커뮤니티 라이선스 1.0 계열 (원본 상속) — Apache 2.0이라고 나온 다른 소스도 있었으나 실제 리포 LICENSE 파일 기준으로 확인 필요

#### 런타임 — 표준 mlx-vlm이 아니라 oMLX 필요

이게 가장 까다로운 부분이었다. `qwen4_exp` 아키텍처는 2026-08-26 llama.cpp에 지원이 막 머지됐고,
표준 `mlx-vlm`(Blaizzy) 저장소엔 **정식 지원 PR이 아직 merge 안 된 상태**(관련 최적화 PR만 일부 머지).
그래서 이 빌드는 자체 번들 런타임을 요구한다:

- **oMLX.app** (`github.com/jundot/omlx`, 20.8k stars, Apache-2.0, 노터라이즈드 서명 확인됨) — `/Applications/oMLX.app`에 설치, 여기 번들된 CPython 3.11 프레임워크와 베이스 MLX 사이트패키지를 씀
- 모델 폴더 안에 격리된 `.mlx-runtime`을 따로 두고 `mlx==0.32.1` / `mlx-metal==0.32.1`을 **정확히 이 버전으로 고정 설치** (oMLX.app 번들 버전은 0.32.0이라 PYTHONPATH 우선순위로 모델 폴더 쪽이 덮어씀)
- 설치: `uv pip install --target .mlx-runtime mlx==0.32.1 mlx-metal==0.32.1` — `uv` 없으면 시스템 `pip3`로도 가능하나 시스템 python이 3.9라 **`--python-version 311 --abi cp311 --platform macosx_15_0_arm64 --only-binary=:all: --ignore-requires-python`** 플래그로 크로스 버전 설치해야 함
- 서버 실행: 모델 폴더 안 `omlx_support/serve` 스크립트가 전부 처리 (포트 기본 8766, API 키 자동생성 후 `.omlx/settings.json`에 저장 — 이 빌드는 `omlx` 고정)

**LM Studio는 이 아키텍처를 아직 못 읽는다** — 자체 llama.cpp/MLX 런타임 업데이트가 별도로 필요.
이번 조사 시점(2026-08-28) 최신 LM Studio(0.4.20)엔 아직 미반영.

#### 실측 테스트 결과

| 테스트 | 결과 |
|---|---|
| 1. 기본 Q&A (64 토큰) | 로드 11.7초, 8.3 tok/s (첫 요청이라 콜드) |
| 1-2. 기본 Q&A (재요청, 292 토큰) | 24.4 tok/s, 3문장 요청에 정확히 3문장, 자연스러운 한국어, `finish_reason=stop` |
| 2. 툴콜 | `get_weather(city="서울")` 정확히 호출, `reasoning_content`와 `tool_calls` 필드 분리 깔끔, 6.5초 |
| 3. 긴 컨텍스트 (11,695 토큰 중 문자열 찾기) | 정확히 추출 성공, 38.8초, 처리 중 wired 메모리 71GB까지 튀었다가 끝나고 4GB로 정상 반환 |
| 4. boomco 스케일 (9,520자 기사 분석) | 39.4초 (기존 파이프라인의 18,000자/126초 대비 준수한 속도), 인위적으로 3회 반복시킨 스팸성 문단을 정확히 잡아내고 "복제·중복 콘텐츠"로 판정 |

메모리: 오MLX 프로세스 자체는 idle 시 가볍고, 실제 가중치는 **mmap 지연로딩** — 모델 디스커버리 시
90.94GB로 잡히지만 첫 로드 직후 실제 상주는 63GB 정도였음 (요청 패턴에 따라 필요한 만큼만 페이지인).

#### 실사용 체감 / 특이사항 (모델 카드엔 안 나오는 것들)

- **`enable_thinking`이 `model_settings.json`에 강제(forced)로 켜져 있음** — API 요청에서 꺼도 무시되고 항상 영어로 사고 과정(`reasoning_content`)을 먼저 생성한다. `max_tokens`를 짧게 주면 사고만 하다 답변 전에 잘릴 수 있음 (실제로 `max_tokens=64`로 처음 테스트했을 때 이렇게 잘렸었음) — **최소 300~500 토큰은 줄 것**
- **`reasoning_effort: "low"` 파라미터는 정상 작동** — boomco처럼 짧고 빠른 답을 원할 때 이걸로 사고 분량을 짧게 유지 가능. "추론만 하다 본문 없이 끝나는" 옛 qwen3.8-27b 문제가 재현 안 됨
- 커스텀 Metal 커널 2개(`qwen35_prefill`, `glm_moe_dsa`)가 **mlx 0.32.1과 심볼 불일치로 로드 실패, 느린 경로로 자동 폴백**함 (로그에 WARNING으로 남음) — 정답 생성엔 문제없지만 잠재적으로 속도 최적화 여지가 남아있다는 뜻. mlx 버전을 올리거나 oMLX 업데이트를 기다리면 개선될 수 있음
- 인증을 **실제로 강제**함 (`skip_api_key_verification: false`) — LM Studio는 이런 검증을 안 해서 기존 스크립트들이 다 인증 헤더 없이 짜여 있었는데, 이 서버로 바꾸면서 전부 401 나던 걸 하나하나 찾아 고쳐야 했음
- 품질 체감: 반복/패딩된 스팸성 콘텐츠를 정확히 잡아내고, 실존하는 관련 모델명(Mamba/RWKV/Jamba/DBRX 등)을 문맥에 맞게 언급하는 등 이전 로컬 모델(qwen3.8-27b, qwen3.6-35b-a3b) 대비 비평/분석 깊이가 체감상 확실히 낫다
- 디스크: safetensors가 18샤드로 쪼개져 있고, 다운로드에 HF 비로그인 기준 **1시간 51분** 걸림 (레이트리밋 있음 — `HF_TOKEN` 설정하면 더 빠름)

#### 에이전틱 성능 — Hermes 연동 실측 (2026-08-29)

Hermes 게이트웨이(텔레그램 등)를 통한 실제 툴콜링·실시간 검색 테스트. Hermes는 모델에
독립적으로 이미 tool-calling 오케스트레이션(terminal, web_search, web_extract 등)을
갖추고 있어서, 모델이 OpenAI 스타일 함수 호출 스키마를 잘 따르기만 하면 별도 설정 없이
바로 붙는다.

| 테스트 | 명령/질문 | 결과 | 소요시간 |
|---|---|---|---|
| 터미널 툴콜 | "지금 몇 시야? date 명령어로 확인해서 알려줘" | `date` 실행 후 정확한 시각 응답 | 9.5초 |
| 실시간 웹검색 | "오늘 코스피 지수가 몇이야? 웹 검색해서 알려줘" | 실제 최근 거래일 수치 인용(출처 포함), 오늘이 주말이라 휴장이라는 것까지 스스로 판단해 덧붙임 | 14.8초 |
| 검색+요약 (복합) | "웹에서 오늘 실리콘밸리 AI 관련 최신 뉴스 하나 검색해서 3줄로 요약해줘" | 실제 기사 링크 인용 + 정확한 3줄 한국어 요약 | 16.0초 |

체감: 툴 선택·인자 구성·결과 종합까지의 흐름이 매끄럽고, 검색 결과에서 불필요한 내용을
잘 걸러내며, 요청한 형식(문장 수 등)을 정확히 지킨다. Claude 대비 속도는 당연히 느리지만
(로컬 6B-active MoE 특성상), 툴 사용 판단력 자체는 체감상 손색없었다.

**테스트 방법**: `~/Claude_works/hermes-agent/hermes chat -q "<질문>" -Q` (비대화형,
스크립트에서 바로 결과 확인 가능). `time` 붙여서 소요시간 같이 측정.

#### 공식 최적화 가이드 대조 (2026-08-29 조사)

**이건 공식 릴리스가 아니라 3단계 파생물**: `Qwen/Qwen3.8-Flash-Next`(공식 BF16) →
누군가의 oMLX 변환 → jedisct1의 importance-matrix 재양자화. Qwen 공식은 BF16과 FP8만
냄, MLX 계열은 전부 커뮤니티 작업.

Qwen 공식 가이드/블로그와 대조한 결과:

- **샘플링 파라미터는 이미 정확함** — 공식 Thinking 모드 권장값(`temperature=1.0,
  top_p=0.95, top_k=20, presence_penalty=0.0`)과 jedisct1의 `model_settings.json`이
  완전히 일치. (참고: Instruct/비사고 모드는 `temperature=0.7, top_p=0.80,
  presence_penalty=1.5`로 다름 — 이 모델은 `enable_thinking`이 강제로 켜져 있으니
  Thinking 모드 값이 맞는 선택)
- **`reasoning_effort`를 낮추는 게 항상 빠른 건 아님** — 공식 경고: 멀티턴 에이전트
  작업에서 effort를 낮추면 개별 응답은 빨라져도 분석 부족으로 재시도가 늘어 총
  소요시간이 오히려 늘 수 있음. → Hermes 메인 에이전트(`agent.reasoning_effort:
  medium`, 멀티턴)와 boomco/전사요약(`reasoning_effort: low`, 단발성)을 다르게 가져가는
  지금 방식이 이 권고와 일치함
- **출력 토큰 예산을 넉넉히**: 공식 권장은 에이전트 작업 시 내부 추론 최대 262K, 최종
  응답 최대 131K까지 허용. 첫 테스트에서 `max_tokens=64`로 답이 잘렸던 게 우연이 아니라
  이 모델의 알려진 특성 — 실전에서는 최소 300~500, 복잡한 작업은 그 이상 줄 것
- **n-gram(PLE) 테이블 오프로드가 "NVIDIA 전용"이라는 정보**가 있으나, 이건 vLLM/SGLang
  기준 설명으로 보임. 우리가 쓰는 jedisct1+oMLX 조합은 `OMLX_QWEN4_PLE_MODE=mmap` 자체
  구현으로 이미 정상 작동 확인됨(테스트 통과) — 소스마다 설명이 엇갈리니 참고만 할 것
- **커뮤니티 반응**: "64GB로 구성한 걸 후회한다"는 의견 다수 — 128GB Mac 선택이 맞았다는
  방증
- **라이선스**: `qwen-community-1.0`(Apache 아님) — 상업적 활용 전엔 라이선스 검토
  권장한다는 지적 있음. boomco 피드가 상업적 성격을 띠면 재확인 필요
- **llama.cpp 경로를 다시 시도한다면**: 표준 llama.cpp는 GDN/QSA 지원이 아직 불완전해서
  **Unsloth의 llama.cpp 포크**를 써야 한다는 정보 있음 (바닐라 빌드로 안 되면 이거 확인)
- jedisct1 저장소 자체는 토론/이슈 0건 — 아직 아무도 실사용 후기를 안 남긴 상태 (생태계
  전체가 3일밖에 안 됨, r/LocalLLaMA에도 관련 글 없음)

#### ⚠️ 실전 배포 중 겪은 가장 큰 함정 — 대화가 길어질수록 응답이 기하급수적으로 느려짐 (2026-08-29)

**증상**: 텔레그램에서 짧은 잡담(쇼핑몰 얘기)을 주고받다가, 턴이 쌓일수록 응답이 200초 →
394초 → 1026초 → 980초로 계속 느려지다가 결국 Hermes의 idle 타임아웃(600초)에 걸려
"멈춘 것처럼" 보였다. 정지된 게 아니라 실제로 계속 처리 중이었고, 결국 응답은 나왔다
(980.8초 뒤에).

**원인 규명 과정**: 처음엔 (1) oMLX `max_concurrent_requests: 1`로 인한 동시성 경합,
(2) 51B n-gram 테이블 mmap 페이지폴트로 인한 디스크 I/O 지연을 의심했다 — 둘 다 실제로
관찰되긴 했지만(디스크 I/O 최대 17MB/s 확인됨), 부수적 요인이었을 뿐 본질은 아니었다.

**진짜 원인**: `agent.reasoning_effort: medium`(Hermes 기본값)에서, **이 모델은 강제
사고모드(`enable_thinking`)의 내부 추론 분량이 대화 컨텍스트가 길어질수록 계속 늘어난다.**
실측: 47,333토큰짜리(프리필 캐시 91% 재사용됨에도) 세션에서 "고마워, 오늘은 여기까지"라는
**완전히 트리비얼한 마무리 인사 한마디에 342.25초**가 걸렸다. 캐시 재사용률이 높았다는 건
프리필 자체는 빠르다는 뜻이므로, 이 시간은 거의 전부 눈에 안 보이는 `reasoning_content`
생성에 쓰인 것으로 보인다.

**해결**: `agent.reasoning_effort: medium` → **`low`**로 변경 후 게이트웨이 재시작.
**같은 47K+ 토큰 세션, 같은 트리비얼한 인사말 재시도 → 342.25초 → 3.87초 (약 88배 개선).**
실제 도구 호출이 필요한 후속 질문(날씨 확인 + 가격 재검색, 도구 2개 사용)도 15초 만에
정확하게 처리 — 품질 저하 없음 확인.

- Qwen 공식 문서는 "낮은 reasoning_effort가 멀티턴 에이전트 작업에서 분석 부족으로
  재시도를 늘려 총 시간이 오히려 늘 수 있다"고 경고하지만, **이 배포 환경(Hermes 텔레그램
  대화)에서는 실측상 `low`가 압도적으로 유리했다** — 이론적 경고와 실측이 다를 수 있으니
  반드시 직접 테스트해서 판단할 것
- boomco 큐레이터는 애초부터 `reasoning_effort: low`를 썼었는데(예전 qwen3.8-27b 시절
  "추론만 하다 본문 없이 끝남" 문제 때문), 이 교훈이 Hermes 메인 에이전트 설정으로는
  이전되지 않아서 오늘 다시 겪은 것 — **새 모델 붙일 때 reasoning_effort 기본값을
  프로덕션 전체(메인 에이전트·크론·큐레이터 등)에 일관되게 검토할 것**
- 진단 순서 팁: `omlx-server.stderr.log`에서 `Chat completion: ... tokens in Ns (...tok/s), prompt: N`
  로그 라인을 보면 실제 소요시간과 프롬프트 크기를 바로 확인 가능 — "멈췄다"는 증상을 보면
  먼저 여기서 진짜 멈춘 건지 그냥 느린 건지부터 구분할 것

**후속 조치 — 무거운 코드 작성 작업은 클라우드로 위임 (2026-08-29)**: `reasoning_effort: low`로도
"파이프라인 하드코딩 고쳐줘" 같은 대용량 코드 생성 작업은 여전히 느렸다(실측 92분짜리 응답
1건 확인 — 원인은 사고 분량이 아니라 **완성 토큰 자체가 8,803~13,512개**로 컸던 것, 26 tok/s
로는 물리적으로 그만큼 걸림). 해결책:

1. `delegation.provider: openai-codex`, `delegation.model: gpt-5.6-terra`로 변경 — 에이전트가
   `delegate_task` 도구를 쓰면 로컬 대신 클라우드로 라우팅됨
2. SOUL.md에 "무거운 코드 작성/파이프라인 수정은 delegate_task로 위임" 지침 추가
3. **부작용 발견 및 수정**: 위임을 켜자 openai-codex 호출이 기본 90초 stale-timeout에 걸려
   Broken pipe로 3회 재시도 실패하는 경우가 있었음(작은 컨텍스트라 context-scaling 로직이
   적용 안 됨). `config.yaml`에 `providers.openai-codex.stale_timeout_seconds: 240.0` 추가로
   해결 — 재시도 실패 로그 완전히 사라지고 45초 만에 깔끔하게 성공

결과: 같은 종류의 코딩 요청이 92분 → (위임 성공 전, 로컬 폴백) 7분17초 → (stale timeout 수정 후,
위임 성공) **45초**로 개선. 로컬 모델은 일상 대화·툴콜 전용, 무거운 코드 생성은 클라우드로
분리하는 구조가 확정됨.

#### 실전 배포 중 겪은 함정 — Hermes 게이트웨이 인증 (2026-08-29)

CLI(`hermes chat`)는 문제없이 됐는데, **텔레그램 게이트웨이만 401(Invalid API key)로
계속 실패**하는 증상이 있었다. 원인: Hermes의 `provider: lmstudio`는 게이트웨이의
실제 대화 루프(`agent.conversation_loop`)에서 `config.yaml`의 `model.api_key`가 아니라
**`LM_API_KEY` 환경변수**로 키를 읽는다(`hermes_cli/auth.py` PROVIDER_REGISTRY 방식).
CLI는 이 레지스트리를 안 타는 다른 경로라 config.yaml만으로 충분했던 것.

**해결**: `ai.hermes.gateway.plist`의 `EnvironmentVariables`에 `LM_API_KEY: omlx` 추가
후 `launchctl bootout` + `bootstrap`으로 재로드. — 새 로컬 엔진을 Hermes에 연결할 때마다
**config.yaml만 고치지 말고 게이트웨이 plist의 env도 같이 확인**할 것. 자세한 내용은
[`SETUP.md`](./SETUP.md)의 "oMLX 연결 설정 (data/config.yaml)" 섹션 참고.

#### 모델 트렌드 추적 크론을 클라우드→로컬로 이관 (2026-09-04)

기존엔 Claude.ai 스케줄 루틴(RemoteTrigger) 2개가 매일/2일마다 모델 트렌드를 클라우드
토큰으로 조사했음. "로컬 LLM이 할 수 있으면 로컬이 직접 조사, 나는 감수만" 요청으로
Hermes 크론 2개로 이관 시도 → **둘 다 검증 성공**, 클라우드 루틴 2개는 pause
(`enabled:false`, 삭제 안 함 — trig_01TmFgDWmFhYUxoHmcmQoawp/trig_01ND47nFQE47BFWvQtoNgFaL).

- **`local-llm/watch_models.py`** — HuggingFace 무인증 API(`/api/models/<repo>`,
  `?author=X&search=Y`, `/discussions`)로 좋아요/다운로드/신규업로드/이슈를 정확한
  숫자로 받아 스냅샷 비교(환각 위험 없음). 크론용 사본은
  `hermes-agent/data/scripts/watch_models.py`(심볼릭 링크 불가 — 실제 파일 복사
  필요, 수정 시 양쪽 다 갱신). 상태 파일은 크론용 스크립트 기준
  `hermes-agent/data/scripts/state/model_watch_state.json`.
- **크론 잡 1** (job id `e0f910ab377f`, 매일 22:30 KST): `--script watch_models.py`로
  스크립트 출력을 로컬 모델 프롬프트에 주입 → 판단(임계값 비교)+한국어 요약만 LLM이
  담당. 검증: 84초, 클라우드 버전과 동일 결론(sh0wie REAP-288 MTP 이슈로 보류) 도출.
- **크론 잡 2** (job id `53b36bf560ab`, 2일마다 자정): agent+web_search(스크립트 없음),
  "검색 최대 5회/재시도 금지" 규칙으로 루프 방지, 기존 탈락 확정 모델 목록을 프롬프트에
  박아 중복 재검토 방지. 검증: 로컬 모델로 완주, "Muse Spark 1.3"(Meta, 비공개 웨이트)을
  정확히 후보 제외 — 품질 양호. **단, 검색결과 27~29K자가 턴마다 누적되며 응답이
  16s→241s로 급증, 총 7분 소요** (2일 주기라 허용 범위로 판단, 스케줄을 더 촘촘히
  바꾸면 부담될 수 있음).
- **테스트 함정**: `hermes cron run/tick`을 터미널에서 직접 실행하면 `LM_API_KEY` 없어
  oMLX 401 → gpt-5.6-terra로 **조용히 폴백**(경고 로그만, 에러로 안 보임). 실제 예약
  실행은 `ai.hermes.gateway.plist` 안에서 돌아 문제없지만, 수동 테스트 땐 반드시
  `LM_API_KEY=omlx ./hermes cron run <id>`로 넣어야 진짜 로컬 실행 검증됨.

#### 배포 현황 (2026-08-29 기준)

- Hermes 메인 모델 / compression / delegation
- boomco 큐레이터 (`llm.model`, `reasoning_effort: low` 유지)
- `transcribe_report.py` (음성/영상 전사 교정·요약, gpt-oss-120b에서 교체)
- 데일리 뉴스 크론 2개 (`jobs.json` model 오버라이드)
- `mcp_server.mjs` / `mcp_server_http.mjs` (chat/review_code MCP 도구)
- XPS `proxy.mjs` (동적 모델 조회 방식으로 전환 — 특정 모델명에 안 묶임)
- **비전(analyze_image)은 미지원** — 이 빌드가 text-only라 비전 파이프라인은 폐기, Claude/GPT로 직접 대체하기로 함
- `ai.boomco.llama-canary`(qwen3.6-35b-a3b, 포트 18080)는 **그대로 유지** — 헬스체크/저지연 캐너리 목적이라 대형 모델로 교체할 이유가 없음

---

## 자동 모니터링 (2026-08-29 설정)

새 모델을 놓치지 않으려고 클라우드 스케줄 루틴 2개를 걸어뒀다 (claude.ai/code/routines,
로컬 크론과 무관 — 결과는 그 페이지에서 확인하거나 다음 세션에 "루틴 결과 확인해줘"로 요청):

| 루틴 | 주기 | 범위 |
|---|---|---|
| `Qwen3.8-Flash-Next 파생모델 주간 체크` (`trig_01TmFgDWmFhYUxoHmcmQoawp`) | 매주 토요일 09:00 KST | Qwen3.8-Flash-Next 계열 양자화 파생판만 (jedisct1/Vontra 신규 업로드, 더 작은 MTP 빌드 등) |
| `로컬 LLM 더 좋은 모델 탐색` (`trig_01ND47nFQE47BFWvQtoNgFaL`) | 2일마다 (홀수일 09:00 KST) | 특정 계열에 국한 안 하고 전체 오픈웨이트 LLM 생태계 — 90~100GB 이내, MLX/oMLX 또는 GGUF 지원, 툴콜링 필수 조건으로 필터링 |

---

## 실시간 상태 모니터링 (2026-08-29 정리)

"지금 모델이 뭐 하고 있나" 확인하는 방법 4단계. 우리처럼 oMLX를 **launchd + jedisct1 커스텀
`serve` 스크립트로 헤드리스 실행**하는 구성에서는 oMLX.app의 메뉴바 퀵메뉴가 못 쓰이는
함정이 있으니 아래를 참고할 것.

### 1. oMLX 자체 API (제일 정확, 스크립트에서 바로 활용 가능)

```bash
# 헬스체크 — 모델 로드 상태, 실시간 메모리 사용량
curl http://127.0.0.1:8766/health -H "Authorization: Bearer omlx"

# 누적 통계 — 총 요청 수, 토큰 사용량, 캐시 히트율
cat ~/Claude_works/local-llm/models/Qwen3.8-Flash-Next-oQ4e-128k/.omlx/stats.json
```

### 2. oMLX.app GUI — ⚠️ 메뉴바 퀵메뉴는 못 씀, Settings 창은 씀

**함정**: oMLX.app을 실행해서 메뉴바 아이콘을 켜도(`omlx diagnose menubar`로 확인/복구 가능,
macOS 26 Tahoe에서 기본적으로 숨겨져 있어 System Settings → Control Center → 메뉴 막대
항목에서 따로 켜야 함), 드롭다운의 **"Serving Stats" 서브메뉴는 항상 "Server is off"만
표시**한다. 이유: 이 퀵메뉴는 "앱이 직접 시작·관리하는 서버"의 상태만 보는데, 우리 서버는
launchd로 외부에서 띄운 거라 앱 입장에선 "내가 켠 적 없음"으로 인식함(드롭다운 맨 위에도
`Server: failed — Port 8766 in use (oMLX server already running)`로 뜸 — 이건 사실 "이미
잘 떠 있다"는 뜻이라 무해함, **Force Restart/Start Server는 누르지 말 것** — 우리의 커스텀
PLE mmap 환경변수 없이 재시작될 위험).

**우회로**: 메뉴바 아이콘 클릭 → **Settings...(⌘,)** → 사이드바 "서빙 통계". 이 전체 창은
같은 "관리 안 됨" 상태여도 **실시간으로 정상 갱신**됨(확인함 — 두 스크린샷 사이 수치 변동
확인). 모델명·Generating/Idle 상태·GPU Wired Memory·System RAM·GPU Utilization·캐시
효율·평균 tok/s가 다 나옴. 창을 작게 줄여서 화면 구석에 상시 띄워두는 걸 추천.

### 3. 범용 Apple Silicon GPU 모니터 (실제 부하 확인용)

```bash
# pip로 설치됨 (Homebrew 없어도 됨), sudo 필요 (powermetrics 기반)
sudo /Users/sykim/Library/Python/3.9/bin/asitop
```

실시간 CPU/GPU/ANE 사용률·전력 그래프. "팬이 도는데 진짜 GPU가 일하는 중인지" 확인할 때 씀.

### 4. Stats.app (메뉴바 상시, 가벼움)

`/Applications/Stats.app` (exelban/stats, notarized, ~20-50MB) — GPU/CPU/메모리를 메뉴바에
항상 표시. GPU 위젯은 기본 비활성화라 앱 설정에서 따로 켜야 함.

---

## 지켜보는 후보 (Watch List)

아직 교체할 정도는 아니지만 발전 상황을 주기적으로 확인할 파생 모델들. 확인할 때마다
날짜/발견사항을 추가해나갈 것.

### Vontra/Qwen3.8-Flash-Next-MLX-oQ4-MTP (2026-08-29 발견)

- **용량**: 113.33GB — 지금 쓰는 jedisct1 빌드(86.6GB)보다 27GB 큼. Hermes/canary 동시
  구동 중인 이 Mac에선 여유가 빠듯해서 아직 안 바꿈
- **차별점**: **네이티브 MTP(multi-token prediction) 드래프트 블록 포함** — 지금 쓰는
  jedisct1 빌드는 `mtp_enabled: false`라 이 이점을 못 씀. 추측 디코딩으로 토큰을 미리
  여러 개 던져보고 맞으면 그대로 채택하는 방식이라, 맞는 비율이 높을수록 체감 속도가 빨라짐
- **실측(제작자, Apple M3 Studio)**: MTP 드래프트 수용률 58.3~89.5%, 일반 채팅 39.7 tok/s,
  기본 완성 29.4~34.4 tok/s (참고: 우리 jedisct1 빌드는 이 Mac에서 17~25 tok/s였음 — 다만
  M3 Studio와 M4 Max는 하드웨어가 달라 직접 비교는 부정확함)
  - `oQ6-MTP`(158GB) 버전도 있으나 128GB Mac엔 애초에 안 올라감
- **신뢰도**: 좋아요 7개로 아직 낮음, r/LocalLLaMA·HF 토론 어디에도 실사용 후기 없음
  (생태계 자체가 3일밖에 안 됨)
- **재검토 조건**: (a) 더 작은(60~80GB대) MTP 빌드가 나오거나, (b) 이 Mac에서 메모리
  여유를 추가로 확보하거나, (c) 좋아요/다운로드/커뮤니티 언급이 늘어나면 다시 검토
- **2026-08-31 업데이트**: 좋아요 7→9로 소폭 증가(20개 기준 아직 미달). 같은 제작자가
  더 작은 **oQ3-MTP(92.5GB)**를 새로 냄 — 예산(90~100GB) 안에 처음 들어온 MTP 빌드.
  M3 Studio 실측: MTP 켬 29.08 tok/s vs 끔 26.54 tok/s (+9.6%), 수용률 68.83%.
  https://huggingface.co/Vontra/Qwen3.8-Flash-Next-MLX-oQ3-MTP — oQ4-MTP보다 이쪽이 유력.

### ⭐ sh0wie/Qwen3.8-Flash-Next-REAP-288-MLX-4bit (2026-08-31 발견 — 최우선 후보)

- **2026-08-30 공개(하루 전), 다운로드 5,594 / 좋아요 26** — 신생치고 반응 빠름
- **용량**: 디스크 68GB, **oMLX NVMe 스트리밍 시 실제 상주 메모리 ~39GB** — 지금 쓰는
  jedisct1 86.6GB 대비 메모리 부담이 훨씬 적음 (Hermes/canary와 동시 구동 여유 커짐)
- **방식이 다름**: 단순 양자화가 아니라 **REAP 전문가 프루닝**(512개→288개 전문가로 축소).
  별도 드래프트 모델(`sh0wie/Qwen3.8-Flash-Next-MTP-Drafter-MLX-bf16`)로 MTP 스펙큘레이티브
  디코딩 지원
- **품질**: HumanEval 91.5%(풀모델 Q4 대비 93.9%에서 소폭 하락), 희귀 토큰/이름 생성
  안정성 9/10(풀모델 10/10) — 코딩 에이전트 용도로는 쓸만하나 완벽친화는 아님
- 링크: https://huggingface.co/sh0wie/Qwen3.8-Flash-Next-REAP-288-MLX-4bit
- **⚠️ 나온 지 하루라 검증 더 필요**. 계획: **2~3일 더 지켜보고(좋아요/다운로드 추이,
  실사용 후기, 이슈 리포트) 문제없어 보이면 실제 테스트 진행.** 판단 기준: 좋아요 50+ 또는
  구체적인 긍정 후기 등장 시 청신호, 버그 리포트/품질 불만 급증 시 보류.
- 참고: jedisct1도 신규 MTP 빌드(oQ4e-100K-MTP, 109GB) 냈지만 예산 초과 + "MTP 켜면
  툴콜 테스트 10건 중 1건 실패"라 후순위. Uncensored 버전도 동일 스펙으로 동시 출시됨.
- **2026-09-04 업데이트**: 다운로드 5,594→**11,713**, 좋아요 26→**36**(아직 50 미달),
  활성 커뮤니티 스레드 6개. **버그 수정판 나옴**: "RMSNorm 텐서가 un-centered(+1)로 저장된
  결함 재-centering으로 수정", "n-gram 테이블 네이밍 표준화로 로더 패치 불필요"— 초기
  변환 결함이 실제로 고쳐짐. 커뮤니티에서 mlx-vlm/omlx/pmlx 등 여러 런타임으로 배포 시도 +
  39GB 스트림 모드 논의 중(우리 구성과 동일한 관심사). **아직 좋아요 50+ 기준 미달이지만
  추세는 뚜렷이 긍정적** — 다음 체크(대략 1주 후)에서 50+ 넘거나 구체적 후기 나오면 실제
  테스트 진행 권장.

### 타 계열(GLM-5.3-Flash / DeepSeek V4-Flash) 대비 — 2026-09-04 조사

"지금보다 성능 좋은 로컬 모델 나왔나?" 확인 차 Qwen3.8-Flash-Next 계열 밖도 리서치.

- **DeepSeek V4-Flash**(304B 총/13B 활성, 1M 컨텍스트, 2026-07-31 출시)과
  **GLM-5.3-Flash**(320B 총/18B 활성, "1M 광고, 실측 검증은 30만" 컨텍스트, 2026-08-26
  출시) 둘 다 나왔지만 **최소 체크포인트가 166.9GB** — 128GB Mac에 4bit로도 안 들어감.
  GLM-5.3-Flash는 커뮤니티 1bit 양자(93GB)까지 내려가야 겨우 올라가는데 정확도 71%까지
  떨어짐. DeepSeek V4-Flash는 SSD 익스퍼트 스트리밍(48GB Mac 기준)으로 억지로 돌릴 수는
  있지만 **4.5~5 tok/s**로 지금(17~25 tok/s)보다 훨씬 느림.
- **세 모델을 동일 벤치마크로 비교한 자료 자체가 없음** — 업체별로 다른 테스트 세트만
  공개해서 "성능 비교"라는 게 사실상 스펙시트 비교 수준. DeepSeek V4-Flash가 출시 1개월
  지나 독립 검증 사례가 제일 많고, Qwen3.8-Flash-Next는 "완전히 미검증, 실험적"이라는
  꼬리표가 아직 붙어있음.
- **결론**: 이 Mac(128GB) 기준으론 Qwen3.8-Flash-Next 계열(125B 총/6B 활성)이 여전히
  크기 대비 유일하게 실용적인 선택지. 더 큰 최신 flagship들은 이 하드웨어에 안 맞음.
  **계열 내에서 더 나은 걸 찾는다면** 위 sh0wie REAP-288(더 가볍고 성장세)이나 Vontra
  MTP 계열을 계속 지켜보는 게 맞고, 계열을 통째로 바꿀 이유는 아직 없음.

### 비전(vision) 지원 빌드 — Vontra/Qwen3.8-Flash-Next-MLX-oQ4 (2026-09-02 조사)

**계기**: MCP `analyze_image` 도구가 죽은 LM Studio 비전 모델을 가리키고 있어 이미지 분석이
실제로 안 되는 걸 발견 → 애초에 Qwen3.8-Flash 계열에 비전 되는 빌드가 있는지 리서치.

- **중요 사실**: 원본 `Qwen/Qwen3.8-Flash-Next`는 텍스트 전용이 아니라 **비전 인코더가 내장된
  멀티모달 모델**("Causal Language Model with Vision Encoder", 이미지+비디오 지원)이다.
  지금 쓰는 `jedisct1/Qwen3.8-Flash-Next-oQ4e-128k`는 변환 과정에서 **비전 인코더를 의도적으로
  제거한** text-only 빌드였을 뿐 — 계열 자체의 한계가 아니었음.
- **비전 유지 빌드**: `Vontra/Qwen3.8-Flash-Next-MLX-oQ4` — vision processor 보존, 실제
  이미지 입력 예제 있음. 111.69GB(22 샤드), 컨텍스트 262,144, oQ4 혼합정밀(228개 모듈
  5/8bit, 그룹32). M3 Studio 실측 27~30 tok/s. **MTP 헤드는 미포함**.
- **⚠️ 아직 안 씀 — 두 가지 걸림돌**:
  1. **메모리 여유**: 111.69GB는 이 Mac(128GB, Hermes/canary 동시 구동) 기준 여유가 매우
     빠듯함 (지금 쓰는 86.6GB 빌드도 빠듯했던 걸 감안).
  2. **런타임 호환성 미검증**: 모델 카드는 "MLX-VLM(qwen4_exp 지원 최신 빌드) 필요, 구버전
     MLX-VLM은 로드 불가"라고 명시 — 지금 쓰는 `oMLX serve`(mlx-lm 기반, text-only 경로)로
     그대로 되는지, MLX-VLM으로 별도 전환/추가 설치가 필요한지 확인 안 됨.
  - 참고로 `pipenetwork/Qwen3.8-Flash-Next-MLX-4bit`도 비전 타워 가중치는 bf16으로
    남아있지만 "런타임은 text-only"라고 명시돼 있어 지금은 못 씀(기본 서빙 스크립트가
    비전 경로를 안 씀).
- **재검토 조건**: 이미지 분석이 실제로 필요해지는 시점에, 메모리 여유 확보 + MLX-VLM
  qwen4_exp 지원 여부부터 확인하고 테스트 진행. 그 전까지는 이미지 분석 요청은 Claude/GPT로
  직접 처리(기존 방침 유지).
- 링크: https://huggingface.co/Vontra/Qwen3.8-Flash-Next-MLX-oQ4

### 생태계 성숙도 변화 (2026-08-31)

지난주(8/29) "Mac/MLX 실사용 후기 전무"였던 상태가 완전히 해소됨 — r/LocalLLaMA에
"Qwen3.8-Flash-Next on a 96GB Mac Studio" 등 벤치마크 스레드 다수 등장, HuggingFace
신규 업로더도 급증(ddalcu, pipenetwork, sh0wie, ARC4NUM 등). 파생 모델 주간 체크 루틴은
**2026-08-31부터 매일 08:00 KST로 변경**(원래 주간) — 생태계가 빠르게 움직여서.

---

## MLX 양자화 시 주의 — "같은 N-bit인데 용량이 다른" 이유

동일 모델의 서로 다른 MLX 변환본을 비교한 결과 (2026-08-28, Qwen3.8-Flash-Next 기준):

| 제작자/빌드 | 라벨 | 실제 용량 | 비고 |
|---|---|---|---|
| orcarouter (원본 제작자, uncensored) | "4-bit" | **163GB** | group size 64, 라우터 게이트 8bit 고정, 실효 ~7.85 bpw — 라벨과 실제가 크게 다름 |
| Vontra | 4-bit (group32) | 111.6GB | |
| jedisct1 | oQ4e (importance-matrix 혼합) | 86.6GB | 가장 효율적, 실측 검증까지 있었음 |
| Vontra | oQ2 | 67.7GB | 제작자 본인이 "출력 불안정" 경고 |

**교훈**: 양자화 "N-bit" 라벨은 제작자마다 계산법이 다르다 (group size, 어떤 텐서를 고정밀도로
보호하는지에 따라 실효 bits-per-weight가 2배 가까이 차이 남). **HF 저장소의 실제 파일 크기와
모델 카드의 "실효 bpw" 문구를 반드시 직접 확인**하고, 라벨만 보고 판단하지 말 것. 같은 이유로
GGUF K-quant(`Q4_K_M` 등)는 슈퍼블록 구조로 메타데이터 오버헤드를 잘 압축해서, 종종 순진한
affine MLX 양자화보다 같은 "4bit" 라벨에서도 더 작다.
