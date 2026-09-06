#!/usr/bin/env python3
"""
SNS API 키 유효성 매일 자가 점검 (2026-09-05 작성).

배경: XPS backend/.env의 API_SECRET_KEY가 로테이션됐는데 Mac sns-tracker/scripts/.env가
안 따라가서 며칠간 피드 저장이 401로 실패한 사고(2026-09-05) 재발 방지용.
XPS가 만든 GET /api/health/auth(자기 키로 호출 시에만 200)를 매일 이 스크립트가 호출해서,
로테이션 통보가 누락돼도 하루 안에 자체적으로 탐지한다.

성공(200)이면 아무 출력도 안 함 — Hermes --no-agent 크론이 "stdout 없으면 조용히 넘어감"
방식이라 평소엔 텔레그램에 아무것도 안 옴. 실패(401 등)면 사람이 바로 알아볼 경고를 출력.
"""
import sys
from pathlib import Path

import httpx

ENV_PATH = Path.home() / "sns-tracker" / "scripts" / ".env"


def load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main() -> int:
    env = load_env(ENV_PATH)
    base = env.get("BOOMCO_SNS_API_BASE", "http://100.116.65.86:3080")
    key = env.get("BOOMCO_SNS_API_KEY", "")

    if not key:
        print(f"⚠️ SNS API 키 자가점검 실패: {ENV_PATH}에 BOOMCO_SNS_API_KEY가 비어있음")
        return 1

    try:
        r = httpx.get(f"{base}/api/health/auth", headers={"X-API-Key": key}, timeout=10.0)
    except httpx.HTTPError as e:
        print(f"⚠️ SNS API 자가점검: XPS({base}) 연결 자체가 안 됨 — {e}")
        return 1

    if r.status_code == 200:
        return 0  # 정상 — 조용히 종료 (no_agent 모드라 stdout 없으면 알림 안 감)

    print(
        f"🚨 SNS API 키 불일치 감지 (HTTP {r.status_code}) — {ENV_PATH}의 BOOMCO_SNS_API_KEY가 "
        f"XPS backend/.env의 API_SECRET_KEY와 안 맞습니다. XPS에서 키가 로테이션됐을 가능성이 "
        f"높습니다. XPS에 SSH로 접속해 backend/.env의 API_SECRET_KEY를 확인하고 이 .env를 "
        f"갱신한 뒤, retry_pending_saves.py로 밀린 저장을 복구하세요."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
