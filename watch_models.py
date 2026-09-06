#!/usr/bin/env python3
"""
Qwen3.8-Flash-Next 파생 모델 추적 스크립트 (2026-09-04 작성).

HuggingFace의 무인증 공개 API(/api/models/..., /api/models?author=..., /discussions)로
좋아요/다운로드/새 업로드/이슈를 "숫자"로 직접 받아온다 — HTML을 LLM이 읽고 해석할
필요가 없어서 환각 위험 없이 결정론적으로 변화를 감지할 수 있다.

이 스크립트는 숫자 비교(무엇이 바뀌었는지)까지만 하고, "그래서 테스트해볼 만한가"
같은 판단과 한국어 요약/텔레그램 전송은 Hermes(로컬 LLM)가 이 출력을 보고 수행한다.

사용법:
  python3 watch_models.py            # 사람이 읽기 좋은 텍스트로 diff 출력, 상태 갱신
  python3 watch_models.py --json     # JSON으로 diff 출력 (LLM 파싱용)
  python3 watch_models.py --dry-run  # 상태 파일 갱신 없이 확인만
"""
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

STATE_PATH = Path(__file__).parent / "state" / "model_watch_state.json"

# 직접 추적하는 저장소 (좋아요/다운로드/삭제 여부 확인)
TRACKED_REPOS = [
    "jedisct1/Qwen3.8-Flash-Next-oQ4e-128k",          # 우리가 지금 쓰는 것 — 복구되는지 확인
    "jedisct1/Qwen3.8-Flash-Next-oQ4e-100K-MTP",       # 후속 빌드 (비전+MTP)
    "jedisct1/Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP",
    "sh0wie/Qwen3.8-Flash-Next-REAP-288-MLX-4bit",     # 최우선 후보
    "Vontra/Qwen3.8-Flash-Next-MLX-oQ4-MTP",
    "Vontra/Qwen3.8-Flash-Next-MLX-oQ3-MTP",
    "Vontra/Qwen3.8-Flash-Next-MLX-oQ4",               # 비전 유지 빌드
]

# 새 업로드 감지용 — 이 작성자의 Qwen3.8-Flash-Next 계열 전체를 조회해서
# TRACKED_REPOS에 없는 새 저장소가 있는지 확인
WATCHED_AUTHORS = ["jedisct1", "sh0wie", "Vontra"]
AUTHOR_SEARCH_TERM = "Qwen3.8-Flash-Next"

# 이슈/버그 트래커까지 확인하는 저장소 (신뢰도 판단에 중요한 것만, API 호출 아끼기 위해 제한)
DISCUSSIONS_WATCH = [
    "sh0wie/Qwen3.8-Flash-Next-REAP-288-MLX-4bit",
]

UA = {"User-Agent": "boomco-model-watch/1.0"}


def fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.getcode(), json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return None, str(e)


def get_repo_info(repo_id):
    code, data = fetch_json(f"https://huggingface.co/api/models/{repo_id}")
    if code == 200 and data:
        return {
            "exists": True,
            "private": data.get("private", False),
            "gated": data.get("gated", False),
            "downloads": data.get("downloads", 0),
            "likes": data.get("likes", 0),
            "lastModified": data.get("lastModified"),
        }
    return {"exists": False, "http_status": code}


def get_author_models(author):
    code, data = fetch_json(
        f"https://huggingface.co/api/models?author={author}&search={AUTHOR_SEARCH_TERM}"
    )
    if code == 200 and isinstance(data, list):
        return [m["id"] for m in data]
    return []


def get_open_discussions(repo_id):
    code, data = fetch_json(f"https://huggingface.co/api/models/{repo_id}/discussions")
    if code == 200 and data:
        threads = data.get("discussions", [])
        return [
            {"num": t["num"], "title": t["title"], "status": t["status"],
             "createdAt": t["createdAt"], "numComments": t.get("numComments", 0)}
            for t in threads if t.get("status") == "open"
        ]
    return []


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"repos": {}, "known_uploads": {}, "known_open_discussions": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def main():
    dry_run = "--dry-run" in sys.argv
    as_json = "--json" in sys.argv

    prev = load_state()
    now = {"checked_at": datetime.now(timezone.utc).isoformat(), "repos": {}, "known_uploads": {}, "known_open_discussions": {}}
    diff = {"repo_changes": [], "new_uploads": [], "new_discussions": [], "resolved_discussions": []}

    for repo in TRACKED_REPOS:
        info = get_repo_info(repo)
        now["repos"][repo] = info
        old = prev["repos"].get(repo)
        if old is None:
            continue  # 첫 실행 — 베이스라인만 기록
        if old.get("exists") != info.get("exists"):
            diff["repo_changes"].append({
                "repo": repo, "change": "existence",
                "from": "exists" if old.get("exists") else "gone",
                "to": "exists" if info.get("exists") else "gone",
            })
        elif info.get("exists"):
            dl_delta = info["downloads"] - old.get("downloads", 0)
            like_delta = info["likes"] - old.get("likes", 0)
            if dl_delta != 0 or like_delta != 0:
                diff["repo_changes"].append({
                    "repo": repo, "change": "metrics",
                    "downloads": info["downloads"], "downloads_delta": dl_delta,
                    "likes": info["likes"], "likes_delta": like_delta,
                })

    for author in WATCHED_AUTHORS:
        found = get_author_models(author)
        now["known_uploads"][author] = found
        old_known = set(prev["known_uploads"].get(author, []))
        new_ones = [m for m in found if m not in old_known and m not in TRACKED_REPOS]
        if new_ones and old_known:  # 첫 실행 때는 전부 "새것"으로 안 뜨게
            diff["new_uploads"].extend(new_ones)

    for repo in DISCUSSIONS_WATCH:
        open_now = get_open_discussions(repo)
        now["known_open_discussions"][repo] = open_now
        old_open = {d["num"]: d for d in prev["known_open_discussions"].get(repo, [])}
        new_now = {d["num"]: d for d in open_now}
        for num, d in new_now.items():
            if num not in old_open:
                diff["new_discussions"].append({"repo": repo, **d})
        for num, d in old_open.items():
            if num not in new_now:
                diff["resolved_discussions"].append({"repo": repo, "num": num, "title": d["title"]})

    if not dry_run:
        save_state(now)

    if as_json:
        print(json.dumps(diff, indent=2, ensure_ascii=False))
    else:
        has_change = any(diff.values())
        if not has_change:
            print("변화 없음 — 모든 추적 저장소/작성자/이슈가 이전 확인과 동일함.")
        else:
            for c in diff["repo_changes"]:
                if c["change"] == "existence":
                    print(f"[저장소 상태변화] {c['repo']}: {c['from']} → {c['to']}")
                else:
                    print(f"[지표변화] {c['repo']}: 다운로드 {c['downloads']}({c['downloads_delta']:+d}), 좋아요 {c['likes']}({c['likes_delta']:+d})")
            for u in diff["new_uploads"]:
                print(f"[신규 업로드] {u}")
            for d in diff["new_discussions"]:
                print(f"[신규 이슈] {d['repo']} #{d['num']}: {d['title']} (댓글 {d['numComments']})")
            for d in diff["resolved_discussions"]:
                print(f"[이슈 종료] {d['repo']} #{d['num']}: {d['title']}")

        print("\n--- 현재 스냅샷 ---")
        for repo, info in now["repos"].items():
            if info.get("exists"):
                print(f"{repo}: 다운로드 {info['downloads']}, 좋아요 {info['likes']}")
            else:
                print(f"{repo}: 존재하지 않음 (HTTP {info.get('http_status')})")


if __name__ == "__main__":
    main()
