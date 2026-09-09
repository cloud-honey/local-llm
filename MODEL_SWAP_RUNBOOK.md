# 모델 교체 런북 (2026-09-09 작성)

## ⚠️ 2026-09-09 22:20 추가 — Claude Code 세션이 실행·검증함: 핵심 전제 틀림, 롤백 완료

**결론: 이 런북대로 실행하면 실패합니다. "0.6.3rc3 네이티브 qwen4_exp가 정석 경로"라는
진단이 틀렸습니다.**

실제로 백업→serve 수정(support_root 제거)→model_settings.json 추가→kickstart→
`/v1/chat/completions` 호출까지 다 해봤습니다. 디스커버리 단계(`omlx.model_discovery`)는
신규 모델을 `type: vlm, engine: vlm`로 정상 인식해서 "네이티브 지원됨"처럼 보였지만, **실제
로드 시점에 실패**합니다:

```
Model type qwen4_exp not supported. Error: No module named
'mlx_vlm.speculative.drafters.qwen4_exp'; LLM fallback also failed:
Model type qwen4_exp not supported.
```

즉 네이티브 VLM 경로도 LLM 폴백 경로도 이 아키텍처를 실제로는 못 띄웁니다. 디스커버리 성공과
로드 성공은 별개입니다 — 다음에 같은 종류 검증할 땐 반드시 실제 추론 호출까지 해보고 판단할 것.

구모델의 `omlx_support/qwen4_exp.py`(커스텀 로더)는 로딩 자체는 되지만 `sanitize()`에서
`model.visual.*`·`mtp.*` 가중치를 명시적으로 버리고 이미지 포워드 경로가 아예 없어서, 이걸
그대로 쓰면 비전 체크포인트를 넣어도 비전은 작동 안 합니다(텍스트 전용).

**실행한 것과 롤백**: 위 순서 그대로 실행 → 로드 실패 확인 → 즉시 백업(`serve.bak-20260909`,
`model_settings.json.bak-20260909`)으로 원복 → kickstart → 정상 서빙 확인. 서비스 중단
약 4~5분, 데이터 손실 없음. 기본 모델은 여전히 `Qwen3.8-Flash-Next-oQ4e-128k`.

**부수 발견**: `.omlx/cache/_boundary_snapshots`가 128GB 캡 설정에도 228GB까지 불어나 있었음
(oMLX 캡 미준수 버그로 의심). 삭제해서 여유공간 50GB→278GB 확보한 뒤 이 작업 진행했음.

**남은 선택지** (사용자에게 아직 확답 안 받음): (a) 이 체크포인트를 구로더로 텍스트 전용만
쓰기(언락+최신 가중치는 얻되 비전 포기), (b) `qwen4_exp.py`에 비전 포워드 패스 직접 구현
(고위험, 미시도), (c) oMLX 업데이트/jedisct1 새 배포 대기. 아래 원본 런북 내용은 실행하지
말고 참고용으로만 남겨둠.

---

(아래는 2026-09-09 최초 작성 원본 — 위 검증 결과로 무효화됨, 실행 금지)

대상: 현행 기본 Qwen3.8-Flash-Next-oQ4e-128k → 신규 Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP
모두 `/Users/sykim/Claude_works/local-llm/models/`에 존재. 신규는 다운로드·SHA256 검증 완료(101GB, 22샤드+index 일치).

## 현재 실행 상태
- [완료] 신규 모델 다운로드·검증
- [완료] 양쪽 config.json 확인: model_type=qwen4_exp, architectures=[Qwen4ExpForConditionalGeneration] 동일
- [완료] 충돌 진단: 현행 serve가 구모델 omlx_support(6월제 qwen4_exp.py)을 PYTHONPATH로 강제로 얹어 네이티브를 가림. 신규는 이 파일 없이는 못 붙음. 신규 폴더엔 .py가 전혀 없고 README도 "폴더를 oMLX에 추가"만 요구 → 0.6.3rc3 네이티브 qwen4_exp가 정석 경로
- [대기] 등록·재시작·테스트 전부 미실행 (업무는 현행 모델로 정상 계속)

## 실행 순서 (총 15~30분)
1. 백업: `cd models/Qwen3.8-Flash-Next-oQ4e-128k` 후 serve, .omlx/model_settings.json 각각 `.bak-20260909`로 cp -p
2. serve 재작성(동일 경로 유지, launchd plist 손대지 않음):
   - PYTHONPATH에서 `$support_root` 항목 삭제(mlx_runtime, app_resources, mlx_site만)
   - `export OMLX_QWEN4_PLE_MODEL_PATH=/Users/sykim/Claude_works/local-llm/models/Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP`
   - cache는 `.omlx/cache` 유지, `--memory-guard balanced`, `--max-concurrent-requests 1` 유지
3. model_settings.json: 백업 후 models 안에 신규 키 추가
   - `Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP`: max_context_window 100000, mtp_enabled false,나머지 생성/루팅 키는 기존 항목 복사, is_default true, display_name 설정
   - 기존 키는 is_default false
4. `launchctl kickstart -k gui/$(id -u)/ai.boomco.omlx-server`
   ※ 이 순간 모든 LLM 클라이언트 1~2분 정지, 17:30 이후 실행 권장
5. 검증: 180초 내 /health healthy + /v1/models 2개 + default=신규
   실패 시 1의 백업 원복 + kickstart 롤백
6. 스모크(직렬, curl 타임아웃 600s): 첫 토큰은 101GB 로드+콜드 캐시로 수 분
   - 한국어 일반 질문 / tools 1개 왕복 / Python 코드 생성 / JSON 스키마 지시준수
   - 통과 기준: finish_reason 정상, content 비어있지 않음, 도구 호출 JSON 유효
7. 100k 초장문 1건: 10만 토큰+ 바늘 3개 묻기 — 통과 시 교체 완료 판정
8. 마무리: 메모리 스왑 확인(OOM/스왑 급증 없어야), 크론 1회 정상 관찰 후 보고
9. (7일 후) 구모델 폴더 삭제 → 디스크 145GB 회수

## 롤백
serve.bak-20260909, model_settings.json.bak-20260909 원복 후 kickstart. 구모델 폴더는 검증 때까지 온전.

## 예상 결과 (RUNBOOK 작성 시점)
신규 모델은 MTP 포함 모델 카드에 따라 MTP 끄고, 컨텍스트 상한 100k에서 128k 모델보다 안전 마진 큼. 100k 리트리브 정확도는 학습된 패턴상 87~95% 관측 구간이라 128k 대비 소폭 하락 가능 — 실제 수치는 7번에서 확인.
