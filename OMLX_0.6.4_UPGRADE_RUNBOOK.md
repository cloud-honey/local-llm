# oMLX 0.6.3rc3 → 0.6.4 업그레이드 런북 (2026-09-17 작성 · **00:31~00:42 실행 완료**)

## 실행 결과 (2026-09-17)

| 단계 | 결과 |
|---|---|
| 기준 벤치 (0.6.3rc3) | 디코드 26~30 tok/s, 콜드 프리필 ≈300~340 tok/s, 32K 캐시 TTFT 1.7s |
| 1단계 앱 교체 | **성공.** 0.6.4 + 커스텀 로더 + .mlx-runtime 0.32.1, 모델 로드 16초, 한국어·도구 호출 정상. 벤치 동일(디코드 27~29, 콜드 TTFT 7.0/22.1/96.1s @2K/8K/32K) — 예상대로 속도 변화 없음. 커널 폴백 WARNING 그대로. |
| 2단계 네이티브 (override=llm) | 실패: `Model type qwen4_exp not supported` — text-only 체크포인트가 batched(LLM) 엔진으로 라우팅되고 그 엔진엔 qwen4 없음 |
| 2단계 네이티브 (override=vlm) | 실패: `VLM load failed: Received 2851 parameters not in model: model.embed_tokens…` — 네이티브 로더는 HF 중첩(`language_model.*`) 키를 기대하는데 jedisct1 128k 빌드는 평면 `model.*` 키. **체크포인트 형식 불일치**, 코드 수정 없인 불가 |
| 복귀 | `native-off` + model_settings 원복 → 0.6.4 + 커스텀 로더로 정상 서빙 (00:41 로드 63.4GB). 총 중단 약 11분 |

**현재 상태**: 앱 0.6.4 (백업 0.6.3rc3 보존), 서빙 구성은 업그레이드 전과 동일(커스텀 로더). 얻은 것은 엔진
레벨 수정(배칭 크래시·캐시 재구성·메모리 가드)뿐이고 속도 이득은 없다.

**속도 이득을 받으려면** 네이티브 로더가 읽는 체크포인트가 필요하다 — 후보: jundot 공식 `Qwen3.8-Flash-Next-oQ4e-mtp`
(106GB, 0.6.4 벤치 기준 빌드) 또는 키 이름을 `language_model.*` 로 재매핑한 변환본. 둘 다 디스크 정리(47GB 여유) 선행.
벤치 파일: `state/bench-before-0.6.4-*.json`, `state/bench-after-0.6.4-customloader-v2-*.json`.

---


목표: Qwen3.8-Flash-Next 디코드 속도 개선(제작자 M3 Ultra 실측 +10~15%, 32K 프리필 +33%) + 0.6.4의
연속배칭·프리픽스 캐시 복원·메모리 가드 버그 수정을 받되, **되돌릴 수 있게** 진행한다.

## 시험 A 결과 (2026-09-17 07:24~07:40) — 네이티브 경로 **성공**, 결정 대기로 원복

디스크 정리 선행: LM Studio 6월 이후 미사용 모델 3개(59GB) + 미참조 HF 캐시 Z-Image-Turbo(31GB) 삭제 → 46GB→135GB 여유.

`Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP`(런타임 형식 키, 이미 디스크에 있음)를 `omlx_serve_native.sh` +
model_settings(`model_type_override: vlm`, `qwen4_ple_ssd_offload: true`, `is_default: true`)로 띄움. 9/10 의 drafters
오류는 0.6.4 에서 사라짐. 로드 19초, PLE mmap, 상주 70.9GB, 커널 폴백 경고 없음, 시스템 메모리 여유 32%.

| 구성 | 디코드 (2K/8K/32K) | 콜드 프리필 | 32K 콜드 TTFT | 도구 호출 디코드 |
|---|---|---|---|---|
| 128k + 커스텀 로더 (전/후 동일) | 29 / 28 / 27 tok/s | ≈300 tok/s | 96s | 34 |
| Uncensored 네이티브, MTP 끔 | 30.5 / 31.7 / 31.1 | 407~640 | 45s | 34 |
| Uncensored 네이티브, Lightning MTP 깊이 3 | 33.6 / 39.6 / 33.5 (캐시 41) | 608~650 | 47s | **57.6** |

바늘 찾기·도구 호출 전부 OK. 첫 2K 콜드 TTFT 27.6s 는 재시작 직후 모델 로드 포함.

**원복한 이유**: 상시 적용하면 기본 모델이 abliterated 변종이 되고, 네이티브 모드에선 128k 빌드를 못 올리므로 128k
이름을 고정한 클라이언트 6곳이 409 로 죽는다(9/10 사고 재현). 마스터 결정("유지"/"원복") 대기.
model_settings 의 A 구성은 `.omlx/model_settings.json.A-tested` 로 보관.

**"유지" 시 전환 체크리스트** (Hermes 재시작 포함, 사전 공지):
1. `.omlx/model_settings.json` ← `model_settings.json.A-tested`, `omlx_upgrade_helper.sh native-on`
2. 고정 모델명 교체 `Qwen3.8-Flash-Next-oQ4e-128k` → `Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP`:
   - `~/Claude_works/hermes-agent/data/config.yaml` 2행(`default:`)·192행(`model:`) → Hermes 게이트웨이 재시작
   - `~/Library/LaunchAgents/ai.hermes.gateway.plist` BOOMCO_TEXT/CRITIC/SERIES_MODEL 3개 (bootout+bootstrap)
   - `~/Library/LaunchAgents/com.sykim.macboom-direct.plist` 같은 3개 (bootout+bootstrap, 붐엘 재시작)
   - `~/Library/LaunchAgents/com.sykim.local-llm-mcp-http.plist` DEFAULT_MODEL, `mcp_server*.mjs` 기본값
   - `~/sns-tracker/scripts/boomco_analyzer.py:571`, `series_detector.py:63` 폴백 문자열
3. 붐코 1건·맥붐 대화·붐엘 도구 루프 스모크, 컨텍스트 상한 100K 확인(128k 빌드는 131K 였음)
4. 128k 빌드 폴더(214GB, 캐시 127GB 포함)는 1주 관찰 후 삭제 판단

## 0. 조사 결과 요약 — 왜 "앱만 바꾸면 끝"이 아닌가

| 항목 | 현재 | 의미 |
|---|---|---|
| 앱 | `/Applications/oMLX.app` 0.6.3rc3 (1.6GB, Sparkle 자동갱신 없음) | dmg 수동 교체 |
| 서버 실행 | launchd `ai.boomco.omlx-server` → 모델 폴더 `omlx_support/serve` → `with-omlx-python` | 앱 번들의 CPython 3.11 사용 |
| 모델 로더 | **jedisct1 번들 커스텀 로더** `omlx_support/qwen4_exp.py` (sitecustomize 가 `mlx_lm.models` 경로 맨 앞에 끼워 넣음) + `qwen4_cache_integration` | oMLX 네이티브 qwen4 코드가 **가려진다** |
| MLX 런타임 | 모델 폴더 `.mlx-runtime` 의 **mlx 0.32.1** 이 앱 번들 0.32.0 을 PYTHONPATH 로 덮음 (jedisct1 README 요구) | 앱의 커스텀 Metal 커널이 ABI 불일치로 `dlopen` 실패 → 느린 경로 (로그 WARNING 확인) |
| 0.6.4 의 Flash-Next 가속 | PR #3244 — `omlx/patches/mlx_vlm_qwen4_exp_compat` (네이티브 VLM 엔진 경로) + `glm_moe_dsa` 커널 패키지 안의 `qwen4_qsa_sparse_gqa` Metal 커널, mlx 0.32.0 핀 | 커스텀 로더 + 0.32.1 조합에선 **둘 다 적용 안 됨** |
| 0.6.4 의 그 외 수정 | late-join 배칭 크래시, 프리픽스 캐시 재구성, GLM/ANE 경로, 설정 저장 버그 | 엔진 레벨이라 앱 교체만으로도 적용 |
| 0.7.0.dev1/dev2 | Flash-Next 가 도구 호출 대신 즉시 EOS 내는 회귀 (#3660, 토글 없음) | **0.7 dev 는 쓰지 않는다** |
| 네이티브 경로 시도 이력 | 9/9 0.6.3rc3 에서 네이티브 로드 실패 (`No module named mlx_vlm.speculative.drafters.qwen4_exp`, LLM 폴백도 실패) — MODEL_SWAP_RUNBOOK.md | 0.6.4 에선 compat 패키지가 vendored 돼 있어 재시험 가치 있음 |
| jedisct1 README | "oMLX 0.6.3rc3 + MLX 0.32.1 에서만 검증", PLE 메타데이터를 이 체크포인트용으로 개조 | 네이티브 로더가 이 PLE 형식을 못 읽을 가능성 있음 |

결론: **2단계로 나눈다.** ① 앱만 0.6.4 로 바꾸고 지금 구성 그대로(커스텀 로더 + 0.32.1) 띄워 호환성만
확인 → 속도는 거의 그대로일 것. ② 같은 창에서 `serve.native` 로 네이티브 경로를 시험 → 로드되면 벤치 비교,
실패하면 1분 만에 ①로 복귀. 속도 이득은 ②가 성공해야 나온다.

## 1. 기준선 (현재, 0.6.3rc3 + 커스텀 로더, 9/10~9/16 로그 1,193건)

| 프롬프트 | 디코드 중앙값 | p25~p75 |
|---|---|---|
| <2K | 26.7 tok/s | 23.6~28.1 |
| 2~8K | 23.9 | 20.6~25.4 |
| 8~20K | 21.0 | 19.2~25.1 |
| 20K+ | 22.9 | 19.2~25.6 |

프리필(캐시 혼재) 약 2,100 prompt tok/s. 상주 63GB(로드 91GB, PLE는 SSD mmap). 캐시 적중 시
시스템 프롬프트 7K 토큰 TTFT 1.5초, 콜드 14초.

정확한 전후 비교는 **`bench_omlx.py`** 로 같은 조건에서 잰다 (2K/8K/32K 콜드·캐시 TTFT, 프리필·디코드
tok/s, 바늘 찾기, 도구 호출 1건). 결과는 `state/bench-<label>-<ts>.json`.

## 2. 사전 준비 (무중단, 지금 해도 됨)

1. **디스크**: 여유 49GB. dmg 806MB + 앱 백업 1.6GB 면 충분. (모델 폴더 `.omlx/cache/_gdn_sidecars` 가 114GB —
   캡 128GB 안이라 손대지 않음. 필요하면 서버 정지 상태에서 삭제 가능, 콜드 프리필만 늘어남.)
2. **dmg 다운로드** (macOS 26.6.2 → `oMLX-0.6.4-macos26-27.dmg`, 806MB) — 마스터 승인 후:
   ```zsh
   curl -L -o ~/Downloads/oMLX-0.6.4-macos26-27.dmg \
     https://github.com/jundot/omlx/releases/download/v0.6.4/oMLX-0.6.4-macos26-27.dmg
   ```
   릴리스 페이지에 체크섬이 없으므로 마운트 후 `Info.plist` 버전(0.6.4)과 코드서명(`codesign -dv`)만 확인.
3. **앱 백업**: `zsh omlx_upgrade_helper.sh backup` → `~/Claude_works/local-llm/backups/oMLX-0.6.3rc3.app` — **9/17 00:05 완료**
4. **기준 벤치**: 유휴 시간에 `/usr/bin/python3 bench_omlx.py --label before-0.6.4` (약 3~5분, 모델 점유).
5. **창 잡기**: 모든 클라이언트(Hermes 맥붐, 붐엘, 붐코 파이프라인, MCP, XPS)가 1~5분 끊긴다.
   - 붐엘 `/status` → 대기열 0, 진행 중 작업 없음. Hermes `state/boomco-x-queue.json` 이 `{}`.
   - 포트폴리오 크론(08:20/13:20/16:30/22:00 근방)과 겹치지 않게 — **22:40 이후 또는 07:00 이전** 권장.
   - 텔레그램에 "oMLX 업그레이드로 5분간 응답 없음" 예고.

## 3. 실행 — 1단계: 앱 교체 (구성 그대로, 예상 중단 3~5분)

```zsh
cd ~/Claude_works/local-llm
zsh omlx_upgrade_helper.sh status                 # 현재 버전·PID 확인
hdiutil attach ~/Downloads/oMLX-0.6.4-macos26-27.dmg
defaults read /Volumes/oMLX*/oMLX.app/Contents/Info.plist CFBundleShortVersionString   # 0.6.4
codesign -dv /Volumes/oMLX*/oMLX.app 2>&1 | grep -i "authority\|team"
zsh omlx_upgrade_helper.sh swap-app "/Volumes/oMLX 0.6.4/oMLX.app"   # 메뉴바 앱 종료 → 서버 정지 → 번들 교체 → 서버 kickstart
hdiutil detach /Volumes/oMLX*
```
검증 (3분 안):
```zsh
zsh omlx_upgrade_helper.sh status      # 앱 0.6.4, /health healthy, default_model 그대로
tail -50 ~/Claude_works/hermes-agent/data/logs/omlx-server.stderr.log | grep -i "loaded model\|error\|traceback"
curl -s http://127.0.0.1:8766/v1/chat/completions -H "Authorization: Bearer omlx" -H "Content-Type: application/json" \
  -d '{"model":"Qwen3.8-Flash-Next-oQ4e-128k","messages":[{"role":"user","content":"한 문장으로 인사"}],"max_tokens":60}'
/usr/bin/python3 bench_omlx.py --label after-0.6.4-customloader
```
실패(로드 안 됨·응답 없음·에러) → `zsh omlx_upgrade_helper.sh rollback-app` (백업 앱 원복 + kickstart, 2분).
성공이면 여기서 멈춰도 된다: 엔진 버그 수정분은 얻었고 속도는 기준선과 같을 것.

## 4. 실행 — 2단계: 네이티브 qwen4 경로 시험 (예상 중단 5~10분, 실패 시 1분 복귀)

`omlx_serve_native.sh`(저장소 루트, native-on 이 모델 폴더 `serve` 로 복사) 는 커스텀 로더(`omlx_support/*.py`)와 `.mlx-runtime`(0.32.1)을 PYTHONPATH 에서
빼고 앱 번들 mlx 0.32.0 만 쓴다 → 네이티브 `mlx_vlm_qwen4_exp_compat` + Metal 커널이 살아난다.
```zsh
zsh omlx_upgrade_helper.sh native-on      # serve 를 serve.native 로 교체(백업 serve.bak-native) + kickstart
```
로드 로그에서 볼 것 (`stderr.log`):
- `Loaded model: Qwen3.8-Flash-Next-oQ4e-128k (actual: ~63GB …)` — 상주 메모리가 91GB 를 크게 넘으면 PLE 가
  SSD 가 아니라 RAM 에 올라간 것 → 위험, 중단.
- `custom_kernels … failed to load` WARNING 이 **사라졌는지**.
- `qwen4_exp` / `drafters` / `PLE` 관련 Traceback 이 없는지. `model_type_override: llm` 때문에 LLM 엔진으로
  가서 실패하면 `.omlx/model_settings.json` 에서 그 키를 지우고(백업 있음) 다시 kickstart.
성공 판정: 위 curl 응답 정상 + `bench_omlx.py --label after-0.6.4-native` 에서 바늘·도구 호출 OK, 디코드 중앙값이
기준선(≈24) 이상. 8K 이상 프롬프트에서 한국어 출력이 이상하면(한자 폭주·반복) 품질 실패로 간주.
실패 또는 이득 없음 → `zsh omlx_upgrade_helper.sh native-off` (serve 원복 + kickstart).

2단계가 성공하면 이후 확인할 것: (a) 24시간 SSD 프리픽스 캐시가 실제로 쌓이는지(`.omlx/cache` 새 파일 —
0.7 dev 에서 qwen4 하이브리드가 안 쌓이는 버그 #3699 보고됨, 0.6.4 는 미확인) (b) 메모리 압축·스왑
(`vm_stat`, 활동 모니터) (c) 붐코 파이프라인 1건 정상.

## 5. 롤백 요약

| 상황 | 조치 |
|---|---|
| 1단계 로드 실패 | `omlx_upgrade_helper.sh rollback-app` |
| 2단계 실패 | `omlx_upgrade_helper.sh native-off` (앱은 0.6.4 유지) |
| 둘 다 되돌림 | `native-off` 후 `rollback-app` |
| model_settings 건드렸으면 | `.omlx/model_settings.json.bak-*` 원복 |

## 6. 그 다음 선택지 (이번 창에서 하지 않음)

- **jundot 공식 `Qwen3.8-Flash-Next-oQ4e-mtp`** (106GB, 0.6.4 벤치의 기준 체크포인트, Lightning MTP 로 M3 Ultra
  48~55 tok/s). 이 Mac(128GB)에서 상주 메모리가 얼마인지 미확인(M3 Ultra 보고는 111.6GB) — 디스크 49GB 라
  다운로드 전에 `_gdn_sidecars` 정리 필요. 2단계가 성공해 네이티브 경로가 검증된 뒤에만 검토.
- **0.7.0**: 도구 호출 EOS 회귀(#3660)가 고쳐진 정식 릴리스가 나오면 재검토 (0.7.0.dev1 실측 +61%).

## 7. 관련 파일

- `bench_omlx.py` — 전후 벤치 (state/bench-*.json)
- `omlx_upgrade_helper.sh` — backup / swap-app / rollback-app / native-on / native-off / status
- `omlx_serve_native.sh` — 2단계용 실행 스크립트 (native-on 때 모델 폴더 `omlx_support/serve` 로 복사, 원본은 `serve.bak-native`)
- `MODEL_SWAP_RUNBOOK.md` — 9/9 네이티브 경로 실패 기록, `MODEL_EVALUATION.md` — 런타임 고정 사유
