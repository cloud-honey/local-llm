#!/usr/bin/env python3
"""mlx-lm PR #1788 (qwen4_exp/Qwen3.8-Flash-Next 지원) 상태 추적.

GitHub REST API로 PR 메타데이터만 조회(비인증, rate limit 60/hr로 충분).
직전 상태와 비교해서 의미 있는 변화(머지/닫힘/라벨 변경/새 커밋·코멘트)가
있을 때만 사람이 읽을 출력을 내고, 변화 없으면 stdout을 비워서
`hermes cron --no-agent`가 조용히 넘어가게 한다.
"""
import json
import sys
import urllib.request
from pathlib import Path

PR_API_URL = "https://api.github.com/repos/ml-explore/mlx-lm/pulls/1788"
PR_HTML_URL = "https://github.com/ml-explore/mlx-lm/pull/1788"
STATE_FILE = Path(__file__).parent / "state" / "mlx_lm_pr1788_state.json"

TRACKED_FIELDS = [
    "state", "merged", "mergeable_state", "comments", "review_comments",
    "commits", "additions", "deletions", "changed_files", "updated_at",
    "merged_at", "closed_at",
]


def fetch_pr():
    req = urllib.request.Request(
        PR_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "boomco-pr-watch"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    snapshot = {k: data.get(k) for k in TRACKED_FIELDS}
    snapshot["labels"] = sorted(l["name"] for l in data.get("labels", []))
    return snapshot


def load_prev():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(snapshot):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))


def diff_lines(prev, cur):
    lines = []
    if prev is None:
        return lines
    if cur["merged"] and not prev.get("merged"):
        lines.append("✅ 머지됨! merged_at=" + str(cur.get("merged_at")))
    if cur["state"] != prev.get("state"):
        lines.append(f"state: {prev.get('state')} → {cur['state']}")
    if cur["closed_at"] and not prev.get("closed_at"):
        lines.append(f"closed_at: {cur['closed_at']} (머지 없이 닫혔을 수 있음 — merged={cur['merged']})")
    if cur["labels"] != prev.get("labels"):
        lines.append(f"labels: {prev.get('labels')} → {cur['labels']}")
    if cur["updated_at"] != prev.get("updated_at"):
        lines.append(
            f"업데이트 감지: {prev.get('updated_at')} → {cur['updated_at']} "
            f"(commits {prev.get('commits')}→{cur['commits']}, "
            f"comments {prev.get('comments')}→{cur['comments']}, "
            f"review_comments {prev.get('review_comments')}→{cur['review_comments']})"
        )
    return lines


def main():
    try:
        cur = fetch_pr()
    except Exception as e:
        print(f"⚠️ mlx-lm PR #1788 상태 조회 실패: {e}")
        return

    prev = load_prev()
    changes = diff_lines(prev, cur)
    save_state(cur)

    if "--json" in sys.argv:
        print(json.dumps({"current": cur, "changes": changes}, indent=2, ensure_ascii=False))
        return

    if not changes:
        return  # 변화 없음 → 출력 없음 (no-agent 크론에서 조용히 스킵)

    print(f"[mlx-lm PR #1788 상태 변화] {PR_HTML_URL}")
    print(f"제목: Add Qwen3.8-Flash-Next (qwen4_exp) model support")
    for line in changes:
        print("- " + line)
    print(
        f"\n현재: state={cur['state']}, merged={cur['merged']}, "
        f"labels={cur['labels']}, comments={cur['comments']}, "
        f"review_comments={cur['review_comments']}, commits={cur['commits']}"
    )
    if cur["merged"]:
        print("\n→ 텍스트 전용 qwen4_exp 지원이 정식 mlx-lm에 들어감. 비전은 별도(mlx-vlm)라 여전히 미포함일 수 있음 — 확인 필요.")


if __name__ == "__main__":
    main()
