"""붐엘의 장기 기억 (2026-09-14).

세 층으로 나뉜다. 전부 시스템 프롬프트에 붙어 매 요청 모델에게 보인다.

1. **자동 요약 기억** `state/memory-<chat>.md`
   대화 기록(history)은 4만 자를 넘으면 오래된 턴을 통째로 버렸다 — 그 순간 맥락이 사라졌다.
   이제 버리기 직전에 로컬 모델로 "무엇을 했고 어디까지 됐는지"를 요약해 이 파일에 날짜와
   함께 누적한다. /new 로 기록을 지울 때도 지우기 전에 요약을 남긴다. 파일이 상한을 넘으면
   모델로 한 번 더 압축한다(claude-mem 의 핵심과 같은 구조, 더 단순).

2. **프로젝트 노트** `~/Claude_works/boomel-notes/<이름>.md`
   사람이 읽고 고칠 수 있는 작업 문서. 모델이 note_list/note_read/note_write/note_append
   도구로 관리하고, 시스템 프롬프트에는 목록(이름·갱신일·첫 줄)만 넣어 관련 요청이 오면
   note_read 로 먼저 읽게 한다.

3. **맥붐(Hermes) 기억 공유 — 읽기 전용** `hermes-agent/data/memories/MEMORY.md`, `USER.md`
   Hermes 의 memory 도구가 `§` 로 구분한 엔트리를 쓰는 파일. 원자적 rename 으로 써서
   읽는 쪽은 락 없이 안전. 붐엘은 읽기만 한다 — 쓰기는 Hermes 쪽 드리프트 감지(엔트리
   길이 > 한도면 .bak 만들고 거부)와 "세션 시작 시 스냅샷" 방식 때문에 굳이 안 한다.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

ENTRY_DELIMITER = "\n§\n"
_NAME_RE = re.compile(r"[^A-Za-z0-9가-힣._-]+")

SUMMARY_SYSTEM = (
    "너는 대화 기록 압축기다. 아래는 마스터와 어시스턴트(붐엘)의 대화이며 도구 호출과 결과가 섞여 있다. "
    "나중에 이어서 작업할 때 필요한 사실만 한국어로 뽑아라: 무엇을 했는지, 만들거나 바꾼 파일·서비스"
    "(경로 포함), 결정과 그 이유, 아직 안 끝난 일, 마스터가 드러낸 선호나 지시. "
    "형식: '- ' 로 시작하는 불릿 5~12줄, 한 줄은 짧게. 도구 출력 원문·잡담·사고 과정·인사말은 빼라. "
    "중국어·한자를 섞지 마라. 불릿 외의 머리말이나 맺음말을 쓰지 마라."
)

COMPRESS_SYSTEM = (
    "너는 기억 파일 압축기다. 아래는 날짜별로 누적된 작업 요약 기억이다. 같은 내용은 합치고, "
    "지난 사실로 대체된 것은 최신 것만 남기며, 파일·경로·결정·미완료 항목은 반드시 보존해서 "
    "전체를 %d자 이내로 줄여라. 형식은 그대로 '## 날짜' 제목 아래 '- ' 불릿. 오래된 항목은 "
    "여러 날짜를 하나의 '## ~YYYY-MM-DD 이전' 제목으로 합쳐도 된다. 한국어, 한자 금지, 머리말·맺음말 금지."
)


def _render_turns(turns, per_tool=600, total=30000):
    """요약용으로 대화 턴을 평문으로 편다 (도구 결과는 앞부분만)."""
    lines = []
    for turn in turns:
        for m in turn:
            role = m.get("role")
            if role == "user":
                lines.append("[마스터] %s" % (m.get("content") or "").strip())
            elif role == "assistant":
                content = (m.get("content") or "").strip()
                if content:
                    lines.append("[붐엘] %s" % content)
                for call in m.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    lines.append("[도구 호출] %s %s" % (fn.get("name"), (fn.get("arguments") or "")[:300]))
            elif role == "tool":
                lines.append("[도구 결과 %s] %s" % (m.get("name"), (m.get("content") or "")[:per_tool]))
    text = "\n".join(lines)
    if len(text) > total:
        text = text[:total // 2] + "\n...(중략)...\n" + text[-total // 2:]
    return text


class Memory(object):
    def __init__(self, cfg, llm, state_dir, log=print):
        """llm: LocalLLM 인스턴스 또는 그것을 돌려주는 콜러블 (봇이 런타임에 바꿔 끼울 수 있게)."""
        mem = cfg.get("memory") or {}
        self._llm = llm
        self.state_dir = Path(state_dir)
        self.log = log
        self.enabled = bool(mem.get("auto_summary", True))
        self.max_chars = int(mem.get("max_chars") or 6000)
        self.notes_dir = Path(os.path.expanduser(mem.get("notes_dir") or "~/Claude_works/boomel-notes"))
        self.hermes_shared = bool(mem.get("hermes_shared", False))  # 기본 off (마스터 지시: 섞이지 않게)
        self.hermes_dir = Path(os.path.expanduser(
            mem.get("hermes_memories_dir") or "~/Claude_works/hermes-agent/data/memories"))
        self.hermes_max_chars = int(mem.get("hermes_max_chars") or 5000)
        self.lock = threading.Lock()

    # ------------------------------------------------------------ 1. 자동 요약 기억

    def summary_path(self, chat_id):
        return self.state_dir / ("memory-%s.md" % chat_id)

    def load_summary(self, chat_id):
        try:
            return self.summary_path(chat_id).read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def clear_summary(self, chat_id):
        p = self.summary_path(chat_id)
        if p.exists():
            bak = p.with_suffix(".md.bak-%s" % time.strftime("%Y%m%d-%H%M%S"))
            p.rename(bak)
            return str(bak)
        return ""

    @property
    def llm(self):
        return self._llm() if callable(self._llm) else self._llm

    def _ask(self, system, user):
        resp = self.llm.complete([{"role": "system", "content": system},
                                  {"role": "user", "content": user}], None, tool_choice="none")
        msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
        return (msg.get("content") or "").strip()

    def summarize_turns(self, turns):
        text = _render_turns(turns)
        if len(text) < 200:
            return ""
        out = self._ask(SUMMARY_SYSTEM, text)
        # 불릿만 남긴다 (모델이 머리말을 붙이는 경우 대비)
        bullets = [ln.strip() for ln in out.splitlines() if ln.strip().startswith(("-", "•", "*"))]
        return "\n".join("- " + b.lstrip("-•* ").strip() for b in bullets) if bullets else out[:1500]

    def digest(self, chat_id, turns, why="기록 정리"):
        """버려질 턴들을 요약해 기억 파일에 누적. 실패해도 봇 흐름을 막지 않는다."""
        if not self.enabled or not turns:
            return ""
        n_user = sum(1 for t in turns for m in t if m.get("role") == "user")
        try:
            summary = self.summarize_turns(turns)
        except Exception as exc:
            self.log("memory digest 실패:", type(exc).__name__, exc)
            return ""
        if not summary:
            return ""
        block = "## %s (%s, 대화 %d턴)\n%s\n" % (time.strftime("%Y-%m-%d %H:%M"), why, n_user, summary)
        with self.lock:
            cur = self.load_summary(chat_id)
            new = (cur + "\n\n" + block).strip() if cur else block.strip()
            if len(new) > self.max_chars:
                new = self._compress(new)
            self.summary_path(chat_id).write_text(new + "\n", encoding="utf-8")
        self.log("memory digest", chat_id, "%d턴 → %d자 (파일 %d자)" % (n_user, len(summary), len(new)))
        return summary

    def _compress(self, text):
        target = int(self.max_chars * 0.6)
        try:
            out = self._ask(COMPRESS_SYSTEM % target, text)
            if out and len(out) <= self.max_chars and "##" in out:
                return out.strip()
        except Exception as exc:
            self.log("memory compress 실패:", type(exc).__name__, exc)
        # 폴백: 뒤쪽(최신)만 남긴다
        cut = text[-target:]
        idx = cut.find("\n## ")
        return ("## (오래된 기억 잘림)\n" + cut if idx < 0 else cut[idx + 1:]).strip()

    def digest_async(self, chat_id, turns, why):
        threading.Thread(target=self.digest, args=(chat_id, turns, why), daemon=True).start()

    # ------------------------------------------------------------ 2. 프로젝트 노트

    def _note_path(self, name):
        name = _NAME_RE.sub("-", str(name or "").strip()).strip("-.")
        if not name:
            raise ValueError("노트 이름이 비었습니다 (영문/숫자/한글/./-/_ 만 허용)")
        return self.notes_dir / (name[:80] + ".md")

    def note_list(self):
        if not self.notes_dir.exists():
            return []
        rows = []
        for p in sorted(self.notes_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
            first = ""
            try:
                for ln in p.read_text(encoding="utf-8").splitlines():
                    s = ln.strip()
                    if s and not s.startswith("#") and not s.startswith("최종 갱신"):
                        first = s[:90]
                        break
            except Exception:
                pass
            rows.append((p.stem, time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime)), first))
        return rows

    def note_index_text(self):
        rows = self.note_list()
        if not rows:
            return "(아직 노트 없음 — 새 프로젝트를 시작하면 note_write 로 만들어라)"
        return "\n".join("- %s (갱신 %s): %s" % r for r in rows[:25])

    def note_read(self, name):
        p = self._note_path(name)
        if not p.exists():
            return "없는 노트: %s (목록: %s)" % (p.stem, ", ".join(r[0] for r in self.note_list()) or "없음")
        return p.read_text(encoding="utf-8")

    def note_write(self, name, content):
        p = self._note_path(name)
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        content = (content or "").rstrip() + "\n"
        if not content.lstrip().startswith("#"):
            content = "# %s\n\n" % p.stem + content
        content = re.sub(r"^최종 갱신:.*$", "", content, count=1, flags=re.M)
        content = content.replace("\n\n\n", "\n\n", 1)
        lines = content.split("\n", 1)
        content = lines[0] + "\n최종 갱신: %s (붐엘)\n" % time.strftime("%Y-%m-%d %H:%M") + (lines[1] if len(lines) > 1 else "")
        existed = p.exists()
        p.write_text(content, encoding="utf-8")
        return "%s: %s (%d자)" % ("갱신" if existed else "새로 만듦", p, len(content))

    def note_append(self, name, text):
        p = self._note_path(name)
        if not p.exists():
            return self.note_write(name, "## 이력\n- %s %s" % (time.strftime("%Y-%m-%d %H:%M"), text))
        body = p.read_text(encoding="utf-8")
        line = "- %s %s" % (time.strftime("%Y-%m-%d %H:%M"), (text or "").strip())
        if "## 이력" in body:
            body = body.rstrip() + "\n" + line + "\n"
        else:
            body = body.rstrip() + "\n\n## 이력\n" + line + "\n"
        body = re.sub(r"^최종 갱신:.*$", "최종 갱신: %s (붐엘)" % time.strftime("%Y-%m-%d %H:%M"),
                      body, count=1, flags=re.M)
        p.write_text(body, encoding="utf-8")
        return "이력 추가: %s ← %s" % (p.stem, line)

    # ------------------------------------------------------------ 3. Hermes 기억 (읽기 전용)

    def _hermes_entries(self, filename):
        try:
            raw = (self.hermes_dir / filename).read_text(encoding="utf-8")
        except Exception:
            return []
        return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]

    def hermes_text(self):
        if not self.hermes_shared:
            return ""
        mem = self._hermes_entries("MEMORY.md")
        usr = self._hermes_entries("USER.md")
        if not mem and not usr:
            return ""
        parts = []
        if usr:
            parts.append("마스터에 대해 (USER.md):\n" + "\n".join("- " + e.replace("\n", " ") for e in usr))
        if mem:
            parts.append("환경·프로젝트 사실 (MEMORY.md):\n" + "\n".join("- " + e.replace("\n", " ") for e in mem))
        text = "\n".join(parts)
        if len(text) > self.hermes_max_chars:
            text = text[:self.hermes_max_chars] + "\n...(Hermes 기억 일부 생략)"
        return text

    # ------------------------------------------------------------ 시스템 프롬프트 블록

    def prompt_block(self, chat_id):
        parts = ["\n---\n\n## 기억"]
        summary = self.load_summary(chat_id) if chat_id is not None else ""
        parts.append("### 이전 대화 자동 요약 (오래된 대화가 정리될 때 시스템이 남긴 것 — 사실로 신뢰하되 "
                     "현재 상태는 필요하면 파일/셸로 다시 확인)\n" + (summary or "(아직 없음)"))
        parts.append("### 프로젝트 노트 (%s/<이름>.md)\n%s\n"
                     "→ 요청이 기존 프로젝트와 관련되면 **note_read 로 먼저 읽고** 시작하라. 새 프로젝트를 만들거나 "
                     "작업을 끝냈거나 중요한 결정·변경이 있으면 note_write(전체 갱신) 또는 note_append(이력 한 줄)로 "
                     "남겨라. 노트는 마스터도 직접 읽고 고치는 문서다 — 구조: 제목, 개요, 현재 상태, 다음 할 일, "
                     "결정 사항, 이력." % (self.notes_dir, self.note_index_text()))
        hermes = self.hermes_text()
        if hermes:
            parts.append("### [Hermes 공유] 맥붐이 남긴 기억 — 붐엘의 기억이 아니다. 읽기 전용이며 note_*/자동 요약과 섞지 말 것\n" + hermes)
        return "\n\n".join(parts) + "\n"

    def note_tool_schemas(self):
        def fn(name, desc, props, required):
            return {"type": "function", "function": {"name": name, "description": desc,
                    "parameters": {"type": "object", "properties": props, "required": required}}}
        return [
            fn("note_list", "프로젝트 노트 목록(이름·갱신일·첫 줄)을 본다.", {}, []),
            fn("note_read", "프로젝트 노트 전체를 읽는다. 기존 프로젝트 관련 요청이면 작업 전에 먼저 읽을 것.",
               {"name": {"type": "string", "description": "노트 이름 (예: stussy-stock-monitor)"}}, ["name"]),
            fn("note_write", "프로젝트 노트를 새로 만들거나 전체를 갱신한다(마크다운). 구조: # 제목 / 개요 / "
                             "현재 상태 / 다음 할 일 / 결정 사항 / ## 이력. '최종 갱신' 줄은 자동으로 붙는다.",
               {"name": {"type": "string"}, "content": {"type": "string"}}, ["name", "content"]),
            fn("note_append", "프로젝트 노트의 '## 이력'에 날짜가 붙은 한 줄을 추가한다(작업 완료·변경·결정 기록용).",
               {"name": {"type": "string"}, "text": {"type": "string"}}, ["name", "text"]),
        ]

    def run_note_tool(self, name, args):
        try:
            if name == "note_list":
                rows = self.note_list()
                return "\n".join("%s (갱신 %s): %s" % r for r in rows) or "(노트 없음)"
            if name == "note_read":
                return self.note_read(args.get("name"))
            if name == "note_write":
                return self.note_write(args.get("name"), str(args.get("content") or ""))
            if name == "note_append":
                return self.note_append(args.get("name"), str(args.get("text") or ""))
        except Exception as exc:
            return "노트 도구 실패: %s: %s" % (type(exc).__name__, exc)
        return None
