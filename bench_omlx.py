#!/usr/bin/env python3
"""oMLX 서버 벤치마크 — 업그레이드/설정 변경 전후 비교용 (2026-09-17).

    /usr/bin/python3 bench_omlx.py                  # 기본: 2K/8K/32K 프롬프트, 256 토큰 생성
    /usr/bin/python3 bench_omlx.py --sizes 2000     # 빠른 스모크
    /usr/bin/python3 bench_omlx.py --label before-0.6.4

측정 항목(프롬프트 크기별): 콜드 TTFT·프리필 tok/s, 캐시 적중 TTFT, 디코드 tok/s, 그리고 도구 호출
1건(finish_reason=tool_calls 여부)과 한국어 짧은 답변 정합성. 결과는 state/bench-<label>-<ts>.json 에
저장하고 표로 출력한다. 스트리밍으로 첫 청크 시각을 재서 TTFT 를 구한다.

주의: 서버는 동시 요청 1개라 다른 요청(붐엘·Hermes·붐코)이 있으면 그 뒤에 줄을 선다 — TTFT 에 대기 시간이
통째로 섞여 수치가 무의미해진다(9/17 스모크: 2K 프롬프트 TTFT 175초). 반드시 유휴 시간에 돌릴 것.
oMLX 스트리밍 usage 에는 cached_tokens 가 안 실릴 수 있어 캐시 효과는 '캐시 TTFT' 열로만 본다.
"""
import argparse
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

import requests

BASE = os.environ.get("OMLX_BASE", "http://127.0.0.1:8766")
KEY = os.environ.get("OMLX_API_KEY", "omlx")
HEADERS = {"Authorization": "Bearer %s" % KEY, "Content-Type": "application/json"}
STATE = Path(__file__).resolve().parent / "state"

FILLER = ("서울의 가을은 짧고 선명하다. 은행나무가 노랗게 물들면 골목마다 낙엽 냄새가 난다. "
          "회사 근처 카페는 아침 여덟 시에 문을 열고, 단골들은 창가 자리를 두고 조용히 경쟁한다. ")


def model_name():
    r = requests.get(BASE + "/health", headers=HEADERS, timeout=10)
    return r.json().get("default_model")


def build_prompt(target_tokens, nonce):
    # 한국어는 대략 1.6자/토큰. 바늘(nonce)을 중간에 심어 캐시 우회 + 이해도 확인에 쓴다.
    chars = int(target_tokens * 1.6)
    body = (FILLER * (chars // len(FILLER) + 1))[:chars]
    # 바늘을 **맨 앞**에 둔다: 중간에 두면 앞 절반이 이전 실행과 같은 접두어라 SSD 프리픽스 캐시에 맞아
    # "콜드"가 콜드가 아니게 된다 (9/17 실측: 32K 콜드 TTFT 84초→42초로 오염). 맨 앞이 다르면 전부 콜드.
    mid = len(body) // 2
    body = "[세션 %s] " % nonce + body[:mid] + " [비밀 코드: %s] " % nonce + body[mid:]
    return body + "\n\n위 글 중간에 있는 '비밀 코드'를 그대로 적고, 글의 분위기를 한 문장으로 말해줘."


def stream_chat(model, messages, max_tokens, tools=None):
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0,
            "reasoning_effort": "low", "stream": True, "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    t0 = time.time()
    ttft = None
    content, tool_calls, usage, finish = "", [], {}, None
    with requests.post(BASE + "/v1/chat/completions", headers=HEADERS, data=json.dumps(body).encode("utf-8"),
                       stream=True, timeout=1800) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line or not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                delta = ch.get("delta") or {}
                if ttft is None and (delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls")):
                    ttft = time.time() - t0
                content += delta.get("content") or ""
                if delta.get("tool_calls"):
                    tool_calls.extend(delta["tool_calls"])
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
    total = time.time() - t0
    pt = usage.get("prompt_tokens") or 0
    ct = usage.get("completion_tokens") or 0
    cached = usage.get("cached_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    ttft = ttft if ttft is not None else total
    decode_tps = (ct - 1) / (total - ttft) if ct > 1 and total > ttft else 0.0
    prefill_tps = (pt - cached) / ttft if ttft > 0 else 0.0
    return {"prompt_tokens": pt, "completion_tokens": ct, "cached_tokens": cached, "ttft_s": round(ttft, 2),
            "total_s": round(total, 2), "prefill_tps": round(prefill_tps, 1), "decode_tps": round(decode_tps, 1),
            "finish": finish, "content": content[-300:], "tool_calls": len(tool_calls)}


def bench_size(model, tokens, gen):
    nonce = uuid.uuid4().hex[:6].upper()
    msgs = [{"role": "user", "content": build_prompt(tokens, nonce)}]
    cold = stream_chat(model, msgs, gen)
    warm = stream_chat(model, msgs, gen)  # 같은 프롬프트 → 프리픽스 캐시
    ok = nonce in (cold["content"] + warm["content"])
    return {"target_tokens": tokens, "cold": cold, "cached": warm, "needle_found": ok}


TOOL = [{"type": "function", "function": {"name": "get_weather", "description": "도시의 현재 날씨",
         "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]


def bench_tool(model):
    r = stream_chat(model, [{"role": "system", "content": "도구가 있으면 반드시 도구를 써서 답한다."},
                            {"role": "user", "content": "부산 날씨 알려줘"}], 300, tools=TOOL)
    return {"finish": r["finish"], "tool_calls": r["tool_calls"], "decode_tps": r["decode_tps"], "ttft_s": r["ttft_s"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="*", default=[2000, 8000, 32000])
    ap.add_argument("--gen", type=int, default=256)
    ap.add_argument("--label", default="bench")
    args = ap.parse_args()
    if args.gen < 200:
        # 사고(thinking)가 켜진 모델은 첫 100~200 토큰을 추론에 쓰므로 그보다 작으면 바늘 판정이 무조건 실패한다
        # (9/17 스모크: --gen 32 → content 가 "The user is asking me..." 로 끝남). 속도만 볼 때도 200 이상 권장.
        print("주의: --gen %d 은 바늘/도구 판정에 부족 — 200 으로 올림" % args.gen)
        args.gen = 200
    model = model_name()
    print("서버 %s / 모델 %s / 생성 %d토큰 / 프롬프트 %s" % (BASE, model, args.gen, args.sizes))
    out = {"label": args.label, "model": model, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "sizes": [], "tool": None}
    for tokens in args.sizes:
        print("... %d 토큰 프롬프트 (콜드→캐시)" % tokens, flush=True)
        res = bench_size(model, tokens, args.gen)
        out["sizes"].append(res)
        c, w = res["cold"], res["cached"]
        print("  프롬프트 %5d tok | 콜드 TTFT %6.1fs 프리필 %6.0f tok/s | 캐시 TTFT %5.1fs (cached %d) | 디코드 %.1f/%.1f tok/s | 바늘 %s"
              % (c["prompt_tokens"], c["ttft_s"], c["prefill_tps"], w["ttft_s"], w["cached_tokens"],
                 c["decode_tps"], w["decode_tps"], "OK" if res["needle_found"] else "FAIL"))
    print("... 도구 호출 1건", flush=True)
    out["tool"] = bench_tool(model)
    print("  finish=%s tool_calls=%d decode %.1f tok/s" % (out["tool"]["finish"], out["tool"]["tool_calls"], out["tool"]["decode_tps"]))
    dec = [s["cold"]["decode_tps"] for s in out["sizes"] if s["cold"]["decode_tps"]]
    out["summary"] = {"decode_median": round(statistics.median(dec), 1) if dec else None,
                      "tool_ok": out["tool"]["finish"] == "tool_calls",
                      "needles_ok": all(s["needle_found"] for s in out["sizes"])}
    STATE.mkdir(exist_ok=True)
    path = STATE / ("bench-%s-%s.json" % (args.label, time.strftime("%Y%m%d-%H%M%S")))
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("요약: 디코드 중앙값 %s tok/s, 도구 %s, 바늘 %s → %s" % (
        out["summary"]["decode_median"], "OK" if out["summary"]["tool_ok"] else "FAIL",
        "OK" if out["summary"]["needles_ok"] else "FAIL", path))
    return 0 if out["summary"]["tool_ok"] and out["summary"]["needles_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
