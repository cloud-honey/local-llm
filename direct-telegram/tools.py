"""맥붐 다이렉트 봇의 도구 구현 (Hermes 미경유).

"능력·파이프라인 동일"을 재구현이 아니라 최대한 '같은 코드 재사용'으로 맞춘다.

- 붐코 X 분석: `hermes-agent/data/plugins/boomco-x/__init__.py`를 그대로 import 해서
  `_analyze()`/`_format_result()`/`_append_audit()`를 호출한다. 즉 실제 분석은 Hermes가
  돌리는 것과 완전히 동일한 `sns-tracker/scripts/boomco_analyzer.py` 서브프로세스이고,
  감사로그(`data/logs/boomco-x-audit.jsonl`)도 같은 파일에 같은 형식으로 남는다.
- 토스 조회: `plugins/toss-query/tools.py`는 3.10+ 문법(`dict | None` 기본값 어노테이션)이라
  시스템 파이썬 3.9에서 import가 안 된다. 그래서 같은 XPS 프록시(:8092)에 같은 GET을 날리는
  얇은 사본만 둔다. 프록시 주소가 바뀌면 이 파일도 같이 고칠 것.
"""
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

HOME = Path.home()
HERMES_DATA = Path("/Users/sykim/Claude_works/hermes-agent/data")
BOOMCO_PLUGIN = HERMES_DATA / "plugins" / "boomco-x" / "__init__.py"
BOOMCO_QUEUE_STATE = HERMES_DATA / "state" / "boomco-x-queue.json"
TOSS_PROXY_BASE = "http://100.116.65.86:8092"

MAX_OUTPUT = 6000  # 도구 결과를 모델에 돌려줄 때의 상한 (컨텍스트 낭비 방지)


def _clip(text, limit=MAX_OUTPUT):
    text = text or ""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return "%s\n\n...[%d자 생략]...\n\n%s" % (head, len(text) - limit, tail)


# ---------------------------------------------------------------- 붐코 (모듈 재사용)

_boomco_mod = None


def _boomco():
    """Hermes의 boomco-x 플러그인 모듈을 그대로 로드 (1회 캐싱)."""
    global _boomco_mod
    if _boomco_mod is None:
        spec = importlib.util.spec_from_file_location("boomco_x_shared", str(BOOMCO_PLUGIN))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _boomco_mod = mod
    return _boomco_mod


def hermes_boomco_busy():
    """Hermes 쪽 붐코 큐에 처리 중/대기 중인 링크가 있는지 (모델 경합 경고용)."""
    try:
        data = json.loads(BOOMCO_QUEUE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if isinstance(data, dict):
        return len(data)
    if isinstance(data, list):
        return len(data)
    return 0


class _AuditSource(object):
    """`_append_audit`가 기대하는 Hermes source 오브젝트의 최소 스텁."""

    class _Platform(object):
        value = "telegram-direct"

    def __init__(self, chat_id):
        self.platform = self._Platform()
        self.chat_id = chat_id


def extract_x_urls(text):
    """메시지가 X 링크만으로 이루어졌으면 링크 목록 (Hermes 훅과 완전히 같은 판정)."""
    return _boomco()._standalone_x_urls(text or "")


def boomco_analyze(url, chat_id, notify=None):
    """X 링크 1건을 Hermes와 동일한 파이프라인으로 분석.

    Hermes의 `_process_one`을 그대로 옮긴 흐름 — 같은 `_run_pipeline`(analyzer 서브프로세스),
    같은 감사로그, 같은 리포트 포맷(`_format_result`), 같은 3회 재시도 + 마지막 시도는
    `--reduced`. 다른 점 하나: Hermes는 실패 건을 큐 맨 뒤로 보내지만 여기선 큐가 없으므로
    그 자리에서 바로 재시도한다.

    반환: (모델에게 줄 요약 문자열, 마스터에게 그대로 보낼 리포트 문자열 또는 None)
    """
    import asyncio

    mod = _boomco()
    src = _AuditSource(chat_id)
    busy = hermes_boomco_busy()
    note = ""
    if busy:
        note = ("\n(참고: Hermes 쪽 붐코 큐에 %d건이 있어 같은 로컬 모델을 나눠 쓰는 중 — "
                "더 느릴 수 있음)" % busy)

    max_attempts = getattr(mod, "MAX_PIPELINE_ATTEMPTS", 3)
    last_error = "알 수 없는 오류"
    for attempt in range(1, max_attempts + 1):
        reduced = attempt >= max_attempts  # 마지막 시도는 축소 모드 (Hermes와 같은 규칙)
        try:
            result = asyncio.run(mod._run_pipeline(url, reduced=reduced))
        except Exception as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
            mod._append_audit(src, url, None, last_error)
            if attempt < max_attempts and notify:
                notify("⚠️ 분석 %d차 시도 실패 — 바로 재시도합니다.%s\n원인: %s"
                       % (attempt, " (다음이 마지막 — 축소 모드로 돕니다)"
                          if attempt + 1 >= max_attempts else "", last_error[:300]))
            continue

        mod._append_audit(src, url, result, None)
        report = mod._format_result(result)
        public = mod._public_result(result)
        summary = json.dumps(
            {
                "status": public.get("status"),
                "category": public.get("category"),
                "scores": public.get("scores"),
                "saved": public.get("saved"),
                "series": public.get("series"),
                "warnings": public.get("warnings", []),
                "reduced_mode": reduced,
                "note": "상세 리포트는 이미 마스터에게 그대로 전송됨 — 다시 길게 옮겨적지 말 것.",
            },
            ensure_ascii=False,
        )
        return (summary + note, report)

    return ("분석 %d회 시도 모두 실패: %s%s" % (max_attempts, last_error, note), None)


# ---------------------------------------------------------------- 토스 (프록시 사본)


def _toss_get(path, params=None):
    try:
        resp = requests.get(TOSS_PROXY_BASE + path, params=params, timeout=8.0)
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": "toss-query-proxy 호출 실패: %s: %s" % (type(exc).__name__, exc)},
                          ensure_ascii=False)


# ---------------------------------------------------------------- 셸 / 파이썬 / 파일


def _killpg(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_proc(argv, workdir, timeout, cancel=None):
    """서브프로세스를 새 세션으로 띄우고 0.5초마다 /stop(cancel 이벤트)·타임아웃을 확인한다.

    2026-09-13: 예전 subprocess.run 은 끝날 때까지 블로킹이라 `sleep 25` 같은 명령 중엔
    /stop 이 먹지 않았다. 새 프로세스 그룹으로 띄워 취소 시 자식까지 통째로 죽인다.
    """
    try:
        # errors="replace": 도구 출력에 UTF-8 이 아닌 바이트(0xb0 등)가 섞이면 reader 스레드가 죽어
        # 결과가 "(출력 없음)" 으로 돌아가던 사고(9/16 bot.err.log) 방지.
        proc = subprocess.Popen(argv, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", start_new_session=True)
    except Exception as exc:
        return "실행 실패: %s: %s" % (type(exc).__name__, exc)
    box = {}

    def reader():
        box["out"] = proc.communicate()[0]

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    deadline = time.time() + timeout
    while th.is_alive():
        th.join(0.5)
        if cancel is not None and cancel.is_set():
            _killpg(proc)
            th.join(5)
            return "마스터가 /stop 으로 중단함 — 프로세스를 종료했습니다."
        if time.time() > deadline:
            _killpg(proc)
            th.join(5)
            return "시간 초과(%ds)로 중단됨. 명령: %s" % (timeout, " ".join(argv)[:200])
    out = box.get("out") or ""
    return "exit=%d\n%s" % (proc.returncode, _clip(out.strip()) or "(출력 없음)")


def run_shell(command, workdir, timeout=180, cancel=None):
    return _run_proc(["/bin/zsh", "-lc", command], workdir, timeout, cancel)


def run_python(code, workdir, timeout=180, cancel=None):
    return _run_proc([sys.executable, "-c", code], workdir, timeout, cancel)


def read_file(path, offset=0, limit=400):
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return "없는 경로: %s" % p
    if p.is_dir():
        return "디렉터리입니다. list_dir을 쓰세요: %s" % p
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return "읽기 실패: %s: %s" % (type(exc).__name__, exc)
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 400), 2000))
    chunk = lines[offset : offset + limit]
    body = "\n".join("%d\t%s" % (offset + i + 1, ln) for i, ln in enumerate(chunk))
    tail = "" if offset + limit >= len(lines) else "\n...(총 %d줄 중 %d~%d줄)" % (
        len(lines), offset + 1, offset + len(chunk))
    return _clip(body) + tail


def write_file(path, content):
    p = Path(os.path.expanduser(path))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content, encoding="utf-8")
    except Exception as exc:
        return "쓰기 실패: %s: %s" % (type(exc).__name__, exc)
    return "%s: %s (%d바이트)" % ("덮어씀" if existed else "새로 만듦", p, len(content.encode("utf-8")))


def edit_file(path, old, new):
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return "없는 경로: %s" % p
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as exc:
        return "읽기 실패: %s: %s" % (type(exc).__name__, exc)
    count = text.count(old)
    if count == 0:
        return "일치하는 문자열이 없습니다. 파일을 먼저 read_file로 확인하세요."
    if count > 1:
        return "%d곳이 일치합니다 — 더 긴 고유한 문자열로 다시 시도하세요." % count
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return "수정 완료: %s" % p


def list_dir(path="."):
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return "없는 경로: %s" % p
    rows = []
    try:
        for entry in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            rows.append("%s%s\t%s" % (entry.name, "/" if entry.is_dir() else "", size))
    except Exception as exc:
        return "목록 실패: %s: %s" % (type(exc).__name__, exc)
    return _clip("\n".join(rows) or "(비어 있음)")


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def web_fetch(url, max_chars=5000):
    try:
        resp = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0 (macboom-direct)"})
        resp.raise_for_status()
    except Exception as exc:
        return "요청 실패: %s: %s" % (type(exc).__name__, exc)
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "html" in ctype:
        text = _TAG_RE.sub(" ", text)
        text = _ANY_TAG_RE.sub(" ", text)
        text = re.sub(r"&nbsp;?", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
    return _clip(text.strip(), int(max_chars or 5000))


def system_status(llm_base, model_id, api_key="omlx", queue_note=""):
    """현재 상태 보고용 — 로컬 모델/게이트웨이/봇 프로세스 실태."""
    parts = []
    t0 = time.time()
    try:
        r = requests.get(llm_base + "/models", timeout=5,
                         headers={"Authorization": "Bearer %s" % api_key})
        ids = [m.get("id") for m in (r.json().get("data") or [])]
        parts.append("oMLX(%s): HTTP %d, 로드된 모델 %s, %.2fs" % (llm_base, r.status_code, ids, time.time() - t0))
    except Exception as exc:
        parts.append("oMLX(%s): 연결 실패 %s: %s" % (llm_base, type(exc).__name__, exc))
    parts.append("이 채널이 쓰는 모델: %s" % model_id)
    try:
        ps = subprocess.run(["/bin/zsh", "-lc",
                             "ps ax -o pid=,command= | grep -E 'hermes_cli.main gateway|telegram-bot-api|omlx' | grep -v grep"],
                            capture_output=True, text=True, timeout=10)
        parts.append("관련 프로세스:\n" + (ps.stdout.strip() or "(없음)"))
    except Exception as exc:
        parts.append("프로세스 조회 실패: %s" % exc)
    busy = hermes_boomco_busy()
    parts.append("Hermes 붐코 큐: %d건" % busy)
    if queue_note:
        parts.append(queue_note + " (/queue 로 상세)")
    return _clip("\n".join(parts))


# ---------------------------------------------------------------- 스키마


def tool_schemas():
    def fn(name, desc, props, required):
        return {"type": "function", "function": {"name": name, "description": desc,
                "parameters": {"type": "object", "properties": props, "required": required}}}

    return [
        fn("run_shell", "이 Mac에서 zsh 명령을 실행하고 stdout/stderr와 종료코드를 돌려준다. "
                        "상태 확인·조회·빌드 등 대부분의 작업에 쓴다.",
           {"command": {"type": "string", "description": "실행할 셸 명령"},
            "timeout": {"type": "integer", "description": "초 단위 제한(기본 180)"}},
           ["command"]),
        fn("run_python", "파이썬 코드를 별도 프로세스로 실행한다(시스템 python3).",
           {"code": {"type": "string"}, "timeout": {"type": "integer"}}, ["code"]),
        fn("read_file", "파일 내용을 줄 번호와 함께 읽는다.",
           {"path": {"type": "string"}, "offset": {"type": "integer", "description": "0부터 시작하는 시작 줄"},
            "limit": {"type": "integer", "description": "읽을 줄 수(기본 400)"}}, ["path"]),
        fn("write_file", "파일을 새로 만들거나 통째로 덮어쓴다.",
           {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
        fn("edit_file", "파일에서 정확히 한 번 등장하는 문자열을 치환한다.",
           {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
           ["path", "old", "new"]),
        fn("list_dir", "디렉터리 목록(이름/크기)을 본다.", {"path": {"type": "string"}}, []),
        fn("web_fetch", "URL을 가져와 본문 텍스트로 변환한다.",
           {"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"]),
        fn("boomco_analyze_x", "X(트위터) 게시물 링크를 로컬 2단계 분석 파이프라인으로 분석하고 "
                               "붐코 피드에 저장한다. 수 분~20분 걸린다.",
           {"url": {"type": "string", "description": "https://x.com/<user>/status/<id>"}}, ["url"]),
        fn("toss_quote", "토스증권 시세 조회.",
           {"symbols": {"type": "string", "description": "쉼표 구분 종목코드/티커 (예: '005930,AAPL')"}},
           ["symbols"]),
        fn("toss_holdings", "토스증권 보유 종목 조회.", {}, []),
        fn("toss_exchange_rate", "USD/KRW 환율 조회.", {}, []),
        fn("send_file_to_master", "만든 파일을 마스터에게 텔레그램 문서로 보낸다.",
           {"path": {"type": "string"}, "caption": {"type": "string"}}, ["path"]),
        fn("system_status", "로컬 모델·게이트웨이·붐코 큐 등 현재 인프라 상태를 실제로 확인한다.", {}, []),
    ]
