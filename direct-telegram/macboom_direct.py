#!/usr/bin/env python3
"""맥붐 다이렉트 — Hermes 게이트웨이를 거치지 않고 oMLX에 직접 붙는 텔레그램 봇.

설계 의도(2026-09-07):
  Hermes(맥붐)와 "같은 영혼·같은 능력·같은 파이프라인"을 쓰되, 게이트웨이 프로세스는
  전혀 거치지 않는 두 번째 채널. Hermes가 다른 작업으로 바빠도(busy_input_mode 인터럽트
  경로 등) 영향을 받지 않고, 샘플링 파라미터(reasoning_effort/max_tokens)를 이 채널이
  직접 통제한다.

  - 영혼: hermes-agent/data/SOUL.md 를 같은 파일 그대로 읽는다(수정하면 /soul 로 리로드).
  - 파이프라인: 붐코 X 분석은 Hermes 플러그인 모듈을 import 해서 같은 코드로 돌린다.
  - 두뇌: oMLX(127.0.0.1:8766) OpenAI 호환 API에 직접 요청. 모델명은 하드코딩하지 않고
    서버의 default_model 을 따른다(30초 캐싱).
  - 실행: 시스템 파이썬 3.9(/usr/bin/python3). Hermes venv 의존성 없음.

2026-09-13 구조 변경 (스투시 재고 모니터 작업에서 드러난 문제 대응):
  - **폴링 스레드와 작업 스레드 분리.** 예전엔 한 스레드가 폴링과 에이전트 실행을 같이
    했기 때문에 작업 중(수 분~수십 분)에는 /status 같은 명령이 작업이 끝날 때까지 답을
    못 받았다. 이제 메인 스레드는 폴링·명령·승인 응답만 처리하고, 일반 요청과 붐코 분석은
    디스크 큐(state/task-queue.json)에 적재된 뒤 워커 스레드가 순차 처리한다.
    → 작업 중에도 /status 로 "몇 단계째, 무슨 도구를 돌리는지" 보이고 /stop 으로 끊는다.
  - **도구 반복 한도는 '중단'이 아니라 '정리 보고'.** 8단계에서 뚝 끊고 "요청을 나눠서
    다시 시켜라"고 하던 걸, 기본 40단계로 늘리고 한도에 닿으면 모델에게 도구 없이 한 번 더
    물어 "여기까지 했고 이게 남았다"를 보고하게 한다. /set steps N 으로 런타임 조정.
  - **승인은 파괴적 작업에만.** launchctl·chmod·mv·~/Library/LaunchAgents 쓰기·
    ~/Claude_works 밖 쓰기 같은 일상 작업이 전부 승인을 요구해 흐름을 끊었다. 이제
    재귀/강제 삭제·강제 push·디스크/시스템 파괴·핵심 서비스 종료·비밀정보 접근·
    붐엘 자신의 코드/영혼 수정만 승인 대상이다(Safety 클래스 주석 참고).
  - **진행 표시는 사람이 읽는 한 줄.** "run_shell {"command": "cd ~ && python3 - <<'PY'\\n..."
    같은 JSON 원문 대신 "🔧 3/40 run_shell · cd ~/stock-monitor && python3 - <<'PY' (+12줄)".
  - **텔레그램 '/' 메뉴 등록(setMyCommands).** /status /stop /queue /log /set /restart 등.
"""
import json
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

import requests
import yaml

import tools as T
from memory import Memory

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
LOG_PREFIX = "[macboom-direct]"
LOG_PATH = BASE_DIR / "bot.out.log"


def log(*parts):
    msg = " ".join(str(p) for p in parts)
    sys.stdout.write("%s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), LOG_PREFIX, msg))
    sys.stdout.flush()


# ------------------------------------------------------------------ 설정


def load_config():
    path = BASE_DIR / "config.yaml"
    if not path.exists():
        sys.exit("config.yaml이 없습니다. config.example.yaml을 복사해 토큰을 채우세요: %s" % path)
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    token = ((cfg.get("telegram") or {}).get("token") or "").strip()
    if not token or token.startswith("<"):
        sys.exit("config.yaml의 telegram.token이 비어 있습니다 (BotFather에서 받은 새 봇 토큰).")
    return cfg


# ------------------------------------------------------------------ 텔레그램


_MD_CODEBLOCK = re.compile(r"```(?:[\w+-]*)\n?(.*?)```", re.S)
_MD_INLINE = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*$", re.M)
_MD_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")


def md_to_html(text):
    """모델/붐코 리포트의 마크다운을 텔레그램이 이해하는 최소 HTML로 변환."""
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out = _MD_CODEBLOCK.sub(lambda m: "<pre>%s</pre>" % m.group(1), out)
    out = _MD_INLINE.sub(lambda m: "<code>%s</code>" % m.group(1), out)
    out = _MD_LINK.sub(lambda m: '<a href="%s">%s</a>' % (m.group(2), m.group(1)), out)
    out = _MD_BOLD.sub(lambda m: "<b>%s</b>" % m.group(1), out)
    # 제목 줄은 통째로 굵게. 안쪽에 이미 <b>가 있으면 중첩되지 않게 제거한다.
    out = _MD_HEAD.sub(lambda m: "<b>%s</b>" % m.group(1).replace("<b>", "").replace("</b>", ""), out)
    return out


# 텔레그램 '/' 메뉴에 뜨는 목록 (setMyCommands). 설명은 256자 이내.
BOT_COMMANDS = [
    ("status", "붐엘 상태 — 작업 중인지·몇 단계째·대기열·로컬 모델"),
    ("stop", "진행 중인 작업 중단 (/stop all: 대기열까지 비움)"),
    ("queue", "대기열 보기 (일반 요청·X 링크)"),
    ("log", "최근 도구 실행 로그 (/log 30)"),
    ("set", "런타임 설정 보기/변경 (steps·effort·tokens·progress)"),
    ("new", "대화 기록 초기화"),
    ("model", "현재 로컬 모델"),
    ("soul", "SOUL.md 다시 읽기"),
    ("plan", "진행 중/멈춘 계획 현황 (/plan resume 재개 · /plan cancel 취소)"),
    ("memory", "자동 요약 기억 보기 (/memory clear: 비우기)"),
    ("notes", "프로젝트 노트 목록 (~/Claude_works/boomel-notes)"),
    ("restart", "붐엘 프로세스 재시작 (코드 반영, launchd가 다시 띄움)"),
    ("help", "도움말"),
]


class Telegram(object):
    def __init__(self, cfg):
        tg = cfg.get("telegram") or {}
        base = (tg.get("api_base") or "http://127.0.0.1:8081").rstrip("/")
        self.url = "%s/bot%s" % (base, tg["token"])
        self.session = requests.Session()
        self._last_edit = {}  # message_id -> 마지막으로 보낸 텍스트 ("not modified" 오류 회피)

    def _post(self, method, payload=None, files=None, timeout=60):
        try:
            resp = self.session.post("%s/%s" % (self.url, method), json=payload if files is None else None,
                                     data=None if files is None else payload, files=files, timeout=timeout)
            data = resp.json()
        except Exception as exc:
            log("telegram %s 실패: %s: %s" % (method, type(exc).__name__, exc))
            return None
        if not data.get("ok"):
            log("telegram %s 오류: %s" % (method, data))
            return None
        return data.get("result")

    def get_updates(self, offset, timeout):
        payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self._post("getUpdates", payload, timeout=timeout + 20) or []

    def set_my_commands(self, commands):
        return self._post("setMyCommands", {
            "commands": [{"command": c, "description": d[:256]} for c, d in commands]})

    def send(self, chat_id, text, reply_markup=None, rich=True):
        """4096자 제한을 고려해 3900자씩 잘라 보낸다. 마지막 메시지 id 반환.

        붐코 리포트 등이 마크다운으로 오므로 텔레그램 HTML로 바꿔 보내고,
        파싱 오류가 나면 같은 조각을 평문으로 다시 보낸다(메시지 유실 방지).
        """
        text = text if text.strip() else "(빈 응답)"
        last = None
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
        for idx, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
            if reply_markup is not None and idx == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            result = None
            if rich:
                html_payload = dict(payload)
                html_payload["text"] = md_to_html(chunk)
                html_payload["parse_mode"] = "HTML"
                result = self._post("sendMessage", html_payload)
            if result is None:
                result = self._post("sendMessage", payload)
            last = result
        return (last or {}).get("message_id")

    def edit(self, chat_id, message_id, text):
        if not message_id:
            return
        text = text[:3900]
        if self._last_edit.get(message_id) == text:
            return
        self._last_edit[message_id] = text
        self._post("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                       "text": text, "disable_web_page_preview": True})

    def typing(self, chat_id):
        self._post("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    def answer_callback(self, callback_id, text):
        self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def send_document(self, chat_id, path, caption=""):
        p = Path(os.path.expanduser(path))
        if not p.exists():
            return "없는 파일: %s" % p
        try:
            with p.open("rb") as fh:
                res = self._post("sendDocument", {"chat_id": chat_id, "caption": caption[:1000]},
                                 files={"document": (p.name, fh)}, timeout=300)
        except Exception as exc:
            return "전송 실패: %s: %s" % (type(exc).__name__, exc)
        return "전송 완료: %s" % p.name if res else "전송 실패 (텔레그램 응답 오류)"


OFFSET_STATE_PATH = STATE_DIR / "telegram-offset.json"


class Stream(object):
    """getUpdates 폴링 + offset 영속화.

    2026-09-10: offset을 메모리에만 들고 있으면 재시작 때 직전 메시지가 중복 recv 되고,
    반대로 fetch 즉시 디스크에 쓰면 처리 전에 죽은 메시지가 진짜 유실된다. 그래서 디스크
    offset은 **처리가 끝난(= 명령을 실행했거나 디스크 큐에 적재한) 업데이트까지만**
    전진시킨다(`ack`). 메모리상 `self.offset`은 fetch 즉시 전진(같은 세션 안 중복 방지).
    """

    def __init__(self, api):
        self.api = api
        self.offset = self._load_offset()

    @staticmethod
    def _load_offset():
        try:
            return json.loads(OFFSET_STATE_PATH.read_text(encoding="utf-8")).get("offset")
        except Exception:
            return None

    def ack(self, update_id):
        try:
            OFFSET_STATE_PATH.write_text(json.dumps({"offset": update_id + 1}), encoding="utf-8")
        except Exception as exc:
            log("offset 저장 실패:", exc)

    def poll(self, timeout=30):
        updates = self.api.get_updates(self.offset, timeout)
        for upd in updates:
            self.offset = upd["update_id"] + 1
        return updates


# ------------------------------------------------------------------ 로컬 LLM


class LocalLLM(object):
    def __init__(self, cfg):
        llm = cfg.get("llm") or {}
        self.base = (llm.get("base_url") or "http://127.0.0.1:8766/v1").rstrip("/")
        self.key = llm.get("api_key") or "omlx"
        self.reasoning_effort = llm.get("reasoning_effort") or "low"
        self.max_tokens = int(llm.get("max_tokens") or 8000)
        self.temperature = float(llm.get("temperature") or 0.4)
        self.timeout = int(llm.get("request_timeout") or 900)
        # config 의 llm.model 을 채우면 그 모델로 고정한다 (교체 시험 중 고정용).
        self.pinned = (llm.get("model") or "").strip()
        self._model = None
        self._model_at = 0.0
        self.session = requests.Session()

    @property
    def headers(self):
        return {"Authorization": "Bearer %s" % self.key, "Content-Type": "application/json"}

    def model(self):
        """모델명을 하드코딩하지 않되 **서버가 정한 기본 모델**을 따른다 (30초 캐싱).

        2026-09-09 사고: 여기서 /v1/models 의 data[0] 을 집었던 탓에, 승인 대기 중이던
        새 모델을 등록하자마자 이 봇만 조용히 그쪽으로 넘어갔다 — oMLX 의 default_model
        은 구모델 그대로였는데도. 목록 순서는 어느 모델이 기본인지 뜻하지 않는다.
        """
        if self._model and time.time() - self._model_at < 30:
            return self._model
        name = self.pinned
        if not name:
            root = self.base[:-3].rstrip("/") if self.base.endswith("/v1") else self.base
            try:
                name = (self.session.get(root + "/health", headers=self.headers,
                                         timeout=10).json().get("default_model") or "").strip()
            except Exception as exc:
                log("default_model 조회 실패 — 목록 첫 항목으로 폴백:", exc)
                name = ""
        if not name:
            resp = self.session.get(self.base + "/models", headers=self.headers, timeout=10)
            resp.raise_for_status()
            data = (resp.json().get("data") or [])
            if not data:
                raise RuntimeError("oMLX에 로드된 모델이 없습니다.")
            name = data[0]["id"]
        self._model = name
        self._model_at = time.time()
        return self._model

    def complete(self, messages, tool_schemas=None, tool_choice="auto"):
        body = {
            "model": self.model(),
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            # 메모리 기록: reasoning_effort medium은 대화가 길어질수록 응답이 기하급수적으로
            # 느려진다(47K 토큰 세션에서 인사말 342초). low가 사실상 필수.
            "reasoning_effort": self.reasoning_effort,
        }
        if tool_schemas and tool_choice != "none":
            body["tools"] = tool_schemas
            body["tool_choice"] = tool_choice
        resp = self.session.post(self.base + "/chat/completions", headers=self.headers,
                                 data=json.dumps(body).encode("utf-8"), timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError("oMLX HTTP %d: %s" % (resp.status_code, resp.text[:500]))
        return resp.json()


# ------------------------------------------------------------------ 안전장치
#
# 2026-09-13 원칙 변경: "위험해 보이는 것"이 아니라 **"되돌릴 수 없는 파괴"** 만 승인 대상.
#   승인 필요  — 재귀/강제/와일드카드 삭제(임시 디렉터리 밖), 강제 push·hard reset 류,
#               디스크·시스템 파괴(dd/diskutil/shutdown), 원격 스크립트 파이프 실행,
#               붐엘·Hermes·oMLX·telegram-bot-api 등 핵심 서비스 종료, 비밀정보 읽기/쓰기,
#               붐엘 자신의 코드·영혼(SOUL.md)·핵심 plist 수정(자기 파괴 방지).
#   승인 불필요 — 그 밖의 모든 셸/파이썬/파일 작업: launchctl load/kickstart, chmod, mv,
#               홈 디렉터리 어디든 쓰기(~/Library/LaunchAgents 포함), 단순 rm(임시 디렉터리 안).
# config.yaml `safety.danger_patterns`는 목록을 통째로 교체, `extra_danger_patterns`는 추가.

_CRIT = r"(hermes|omlx|macboom|telegram-bot-api|llama-server|llama-canary|boom-steward|local-llm-mcp)"
_SEG = r"[^\n;&|]*"  # 같은 셸 세그먼트 안

DEFAULT_DANGER = [
    r"\bsudo\b",
    r"\bgit\s+push\b" + _SEG + r"(\s--force\b|\s--force-with-lease\b|\s-f\b|\s--delete\b|\s\+\S)",
    r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*[fd]|checkout\s+--\s|restore\s+\.|branch\s+-D|stash\s+(drop|clear))\b",
    r"\bfind\b" + _SEG + r"\s-delete\b",
    r"\bxargs\b" + _SEG + r"\brm\b",
    r"\b(diskutil\s+(erase\w*|partition\w*|reformat|zero\w*|secureErase)|mkfs\w*|newfs\w*)\b",
    r"\bdd\s+if=",
    r"\b(shutdown|reboot|halt)\b",
    r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(ba|z)?sh\b",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    # 2026-09-16: os.remove/unlink·Path.unlink() 같은 단일 파일 삭제는 제외 — 붐엘이 자기 임시 파일을
    # 지우는 코드가 밤새 승인 요청→5분 타임아웃을 3번 반복했다. 재귀 삭제(rmtree/removedirs)만 승인.
    r"\bshutil\.rmtree\b", r"\bos\.removedirs\b",
    r"\b(brew\s+(uninstall|remove)|pip3?\s+uninstall|npm\s+(publish|unpublish))\b",
    r"\bdefaults\s+(write|delete)\b",
    r"\bcrontab\s+-r\b",
    r"\bkill\s+(-\S+\s+)*-1\b",
    r"\b(pkill|killall)\s+(-\S+\s+)*(python3?|Python)\b",
    r"\b(kill|pkill|killall)\b" + _SEG + r"\b" + _CRIT,
    r"\blaunchctl\s+(bootout|unload|remove|disable|kill|stop|kickstart\s+-k)\b" + _SEG + _CRIT,
    r"(>{1,2}|\btee\b|\bsed\s+-i\b|\bcp\b|\bmv\b)" + _SEG + r"(SOUL\.md|\.env\b|auth\.json|secrets\S*\.json|id_rsa|id_ed25519|\.ssh/)",
    r"(~|\$HOME|/Users/\w+)/\.(ssh|aws|gnupg|config/gcloud)\b",
]

# 읽기·쓰기 모두 승인 (비밀정보). 경로 전체 또는 파일명 패턴.
DEFAULT_SECRET_PATHS = [
    "~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gcloud",
    "~/Claude_works/hermes-agent/data/config.yaml",
    "~/Claude_works/hermes-agent/data/.env",
    "~/Claude_works/hermes-agent/data/auth.json",
    "~/Claude_works/local-llm/direct-telegram/config.yaml",
    "~/sns-tracker/scripts/.env",
]
SECRET_NAME_PATTERNS = [
    r"^\.env(\..+)?$", r"^secrets?(\.|-|_).*\.(json|ya?ml|toml)$", r"^secrets?\.(json|ya?ml|toml)$",
    r"^auth\.json$", r"^credentials(\.|-|_|$).*", r"(^|[_.-])tokens?([_.-]|$)", r"\.pem$",
    r"^id_(rsa|ed25519|ecdsa|dsa)(\.pub)?$", r"\.(key|p12|pfx)$",
]

# 쓰기만 승인 (붐엘 자신·형제 서비스의 생명줄 — 실수로 자기 코드/영혼을 깨는 걸 막는다).
DEFAULT_WRITE_PROTECTED = [
    "~/Claude_works/hermes-agent/data/SOUL.md",
    "~/Claude_works/local-llm/direct-telegram/macboom_direct.py",
    "~/Claude_works/local-llm/direct-telegram/tools.py",
    "~/Library/LaunchAgents/com.sykim.macboom-direct.plist",
    "~/Library/LaunchAgents/ai.hermes.gateway.plist",
    "~/Library/LaunchAgents/ai.boomco.omlx-server.plist",
    "~/Library/LaunchAgents/com.sykim.telegram-bot-api.plist",
]

# 승인 없이 쓸 수 있는 루트. 이 밖(/etc, /Library, /usr 등)은 승인.
DEFAULT_WRITE_ROOTS = ["~", "/tmp", "/private/tmp"]

# rm 이 승인 없이 지울 수 있는 곳 (임시 디렉터리).
RM_FREE_ROOTS = ["/tmp", "/private/tmp", "/var/folders", "/private/var/folders"]


def _real(path):
    return os.path.realpath(os.path.expandvars(os.path.expanduser(str(path))))


def _under(real, roots):
    return any(real == r or real.startswith(r + os.sep) for r in roots)


class Safety(object):
    def __init__(self, cfg):
        s = cfg.get("safety") or {}
        pats = list(s.get("danger_patterns") or DEFAULT_DANGER) + list(s.get("extra_danger_patterns") or [])
        self.patterns = [re.compile(p, re.I) for p in pats]
        self.secret_paths = [_real(p) for p in (s.get("protected_paths") or s.get("secret_paths") or DEFAULT_SECRET_PATHS)]
        self.secret_names = [re.compile(p, re.I) for p in SECRET_NAME_PATTERNS]
        self.write_protected = [_real(p) for p in (s.get("write_protected") or DEFAULT_WRITE_PROTECTED)]
        self.write_roots = [_real(p) for p in (s.get("write_roots") or DEFAULT_WRITE_ROOTS)]
        self.rm_free_roots = [_real(p) for p in RM_FREE_ROOTS]
        self.approval_timeout = int(s.get("approval_timeout") or 300)

    # ---- 경로 판정
    def is_secret(self, path):
        real = _real(path)
        if _under(real, self.secret_paths):
            return True
        name = os.path.basename(real)
        return any(p.search(name) for p in self.secret_names)

    def is_write_protected(self, path):
        return _under(_real(path), self.write_protected)

    def in_write_roots(self, path):
        return _under(_real(path), self.write_roots)

    # ---- rm 판정: 임시 디렉터리 안의 명시적 경로만 자유. 와일드카드·그 밖 경로는 승인.
    def rm_reason(self, command):
        for seg in re.split(r"\n|;|&&|\|\||\|", command):
            seg = seg.strip()
            if not seg or not re.search(r"(^|\s|/)rm(\s|$)", seg):
                continue
            try:
                toks = shlex.split(seg)
            except ValueError:
                toks = seg.split()
            for i, tok in enumerate(toks):
                if tok != "rm" and not tok.endswith("/rm"):
                    continue
                targets = [t for t in toks[i + 1:] if not t.startswith("-")]
                if not targets:
                    return "인수 없는 rm (파이프/xargs 삭제?): %s" % seg[:80]
                for tgt in targets:
                    if any(c in tgt for c in "*?[{"):
                        return "와일드카드 삭제: rm %s" % tgt
                    if not _under(_real(tgt), self.rm_free_roots):
                        return "임시 디렉터리 밖 파일 삭제: rm %s" % tgt
        return ""

    def check(self, name, args):
        """(승인 필요 여부, 사유) 반환."""
        if name in ("run_shell", "run_python"):
            body = str(args.get("command") or args.get("code") or "")
            for pat in self.patterns:
                m = pat.search(body)
                if m:
                    return True, "파괴적/보호 패턴 감지: `%s`" % m.group(0).strip()[:80]
            if name == "run_shell":
                r = self.rm_reason(body)
                if r:
                    return True, r
            return False, ""
        if name in ("write_file", "edit_file"):
            path = args.get("path") or ""
            if self.is_secret(path):
                return True, "비밀정보 파일 수정: %s" % path
            if self.is_write_protected(path):
                return True, "붐엘/Hermes 핵심 파일 수정(자기 파괴 방지): %s" % path
            if not self.in_write_roots(path):
                return True, "홈 디렉터리 밖에 쓰기: %s" % path
            return False, ""
        if name in ("read_file", "send_file_to_master"):
            if self.is_secret(args.get("path") or ""):
                return True, "비밀정보 포함 가능 경로 접근: %s" % args.get("path")
            return False, ""
        return False, ""


# ------------------------------------------------------------------ 디스크 작업 큐


TASK_QUEUE_PATH = STATE_DIR / "task-queue.json"
LEGACY_BOOMCO_QUEUE = STATE_DIR / "macboom-boomco-queue.json"


class TaskQueue(object):
    """받은 요청을 디스크에 먼저 적고 워커가 처리 — 크래시해도 유실 없음, /queue 로 조회.

    엔트리: {id, kind: "chat"|"boomco", chat_id, received_at, status: pending|processing,
            text(chat) | url(boomco)}
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(self.path))

    def all(self):
        with self.lock:
            return self._load()

    def add(self, kind, chat_id, **payload):
        entry = {"id": uuid.uuid4().hex, "kind": kind, "chat_id": str(chat_id),
                 "received_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "pending"}
        entry.update(payload)
        with self.lock:
            items = self._load()
            items.append(entry)
            self._save(items)
            return entry, len(items)

    def set_status(self, eid, status):
        with self.lock:
            items = self._load()
            for e in items:
                if e.get("id") == eid:
                    e["status"] = status
            self._save(items)

    def remove(self, eid):
        with self.lock:
            items = [e for e in self._load() if e.get("id") != eid]
            self._save(items)
            return len(items)

    def clear_pending(self):
        with self.lock:
            items = self._load()
            kept = [e for e in items if e.get("status") == "processing"]
            self._save(kept)
            return len(items) - len(kept)


class Cancelled(Exception):
    pass


SOUL_SKIP_DEFAULT = ["GPT 폴백", "무거운 코드 작성", "음성·영상 전사", "현재 상태 보고"]


def strip_soul_sections(text, skip):
    """'## 제목' 단위로 나눠 skip 에 부분 일치하는 섹션을 뺀다."""
    if not skip:
        return text
    out, drop = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            drop = any(k in line for k in skip)
        if not drop:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


# ------------------------------------------------------------------ 봇 본체


DIRECT_ADDENDUM = """
---

## 이 채널의 실행 환경 (반드시 지킬 것)

- 지금 너는 **Hermes 게이트웨이를 거치지 않고** oMLX에 직접 붙은 다이렉트 채널에서 돌고 있다.
  영혼(이 지침)과 붐코 분석 파이프라인은 Hermes 쪽과 완전히 같은 것을 쓰지만, 도구 목록은 다르다.
- **이 채널에서 너의 이름은 '{persona}'.** 마스터가 이 다이렉트 채널에 붙여준 이름이니,
  자기소개나 서명에는 맥붐 대신 '{persona}'을 쓴다. 성격·말투·원칙은 위 지침 그대로다.
  텔레그램의 다른 봇(맥붐)은 Hermes를 거치는 형제 채널이고, 둘은 같은 로컬 모델을 나눠 쓴다.
- **이 채널에서 쓸 수 있는 도구는 아래가 전부다**: {tool_names}.
- Hermes에만 있는 기능(delegate_task 클라우드 위임, GPT 폴백, 브라우저 도구, 이미지 생성,
  크론/칸반/스킬, 음성 전사)은 **이 채널에 없다**. 없는 도구를 썼다고 말하지 말고, 필요하면
  "그 작업은 Hermes 쪽 맥붐에게 시켜야 한다"고 안내한다.
- GPT 폴백이 없으므로 로컬 모델이 실패하면 실패했다고 그대로 보고한다.
- **승인은 파괴적 작업에만 걸린다**: 재귀/강제/와일드카드 삭제, 강제 push·hard reset,
  디스크/시스템 파괴, 핵심 서비스(Hermes·oMLX·텔레그램 API·붐엘 자신) 종료, 비밀정보 접근,
  붐엘 자신의 코드·SOUL.md 수정. 그 밖의 셸·파이썬·파일 작업(launchctl, chmod, mv,
  ~/Library/LaunchAgents 나 홈 디렉터리 어디든 쓰기)은 **승인 없이 바로 실행**되니
  "승인을 기다리겠다"고 말하지 마라. 거부되면 우회하지 말고 다른 방법을 제안한다.
- 한 요청 안에서 도구를 **{max_steps}번**까지 쓸 수 있다. 긴 작업도 요청을 쪼갤 필요 없이
  끝까지 진행하라. 한도에 닿으면 시스템이 정리 보고를 요청하니 그때 "한 일 / 확인된 결과 /
  남은 일"을 정리하면 되고, 마스터가 '이어서 진행해'라고 하면 남은 일부터 재개한다.
  마스터는 언제든 /stop 으로 작업을 끊을 수 있다.
- 같은 수정을 두 번 하지 마라. edit_file 이 "일치하는 문자열이 없다"고 하면 read_file 로
  실제 내용을 확인한 뒤 고친다. 파일을 통째로 다시 쓰기보다 edit_file 로 부분 수정한다.
- 상태를 물으면 추측하지 말고 `system_status`나 `run_shell`로 실제 확인한 뒤 답한다.
- 답변은 텔레그램으로 나간다. **굵게**, ## 제목, `코드`, [링크](url) 정도는 그대로 렌더링되지만
  마크다운 표는 깨지니 쓰지 마라.
- **한국어로 답한다.** 이 채널은 도구 출력이 대부분 영어(셸 로그·README·설정 파일)라
  영어로 끌려가기 쉽다. 무엇을 읽었든 마스터에게 나가는 문장은 한국어로 쓴다.
  명령어·경로·로그 원문은 그대로 인용하되, 설명은 한국어다. **중국어·한자를 섞지 마라**
  (코드 주석·문서 포함 — 한국어 문장에 한자 단어가 끼어드는 실수가 실제로 있었다).
- **사고 과정을 그대로 보내지 마라.** 도구를 여러 번 쓴 뒤에는 생각을 정리한 초안이 아니라
  정리된 결론만 보낸다. 영어로 스스로에게 묻고 답하는 문장이 답변에 섞이면 실패다.

- **큰 일은 계획으로 쪼개라.** 하위 작업 4개 이상이 예상되거나(여러 파일 수정·서비스 구축·조사+구현),
  30분 넘게 걸릴 것 같으면 바로 시작하지 말고 `plan_create` 로 3~8개 단계를 정의한다. 각 단계는 새
  컨텍스트에서 따로 실행되니 제목만 보고 할 수 있게 구체적으로 쓴다. 단순 질문·조회·한두 파일 수정은
  계획 없이 바로 처리한다. 계획 단계 안에서는 그 단계만 하고 `plan_step_done` 으로 끝낸다.

작업 디렉터리: {workdir} / 로컬 모델: {model} / 메시지 앞의 [날짜 시각]이 그 메시지를 받은 시각(KST)이다.
"""

YES_WORDS = ("y", "yes", "ok", "ㅇ", "ㅇㅇ", "응", "네", "승인", "실행", "해", "해줘", "고")
NO_WORDS = ("n", "no", "ㄴ", "ㄴㄴ", "아니", "아니오", "거부", "취소", "하지마", "stop")

TOOL_ICON = {"run_shell": "🔧", "run_python": "🐍", "read_file": "📖", "write_file": "✍️",
             "edit_file": "✏️", "list_dir": "📂", "web_fetch": "🌐", "boomco_analyze_x": "🔍",
             "send_file_to_master": "📎", "system_status": "🩺"}


def _short_path(p):
    p = str(p or "")
    home = os.path.expanduser("~")
    return ("~" + p[len(home):]) if p.startswith(home) else p


def describe_call(name, args):
    """진행 표시용 한 줄 — JSON 원문 대신 사람이 읽는 요약."""
    if name in ("run_shell", "run_python"):
        body = str(args.get("command") or args.get("code") or "").strip()
        lines = [ln for ln in body.splitlines() if ln.strip()]
        first = lines[0].strip() if lines else "(빈 명령)"
        first = first if len(first) <= 110 else first[:107] + "..."
        return first + (" (+%d줄)" % (len(lines) - 1) if len(lines) > 1 else "")
    if name in ("read_file", "list_dir", "send_file_to_master"):
        return _short_path(args.get("path") or "")
    if name == "write_file":
        return "%s (%d바이트)" % (_short_path(args.get("path")), len(str(args.get("content") or "").encode("utf-8")))
    if name == "edit_file":
        old = str(args.get("old") or "").strip().splitlines()
        return "%s (교체: %s)" % (_short_path(args.get("path")), (old[0][:50] if old else "?"))
    if name == "web_fetch":
        return str(args.get("url") or "")[:110]
    if name == "boomco_analyze_x":
        return str(args.get("url") or "")
    if name.startswith("toss_"):
        return json.dumps(args, ensure_ascii=False)[:110]
    return json.dumps(args, ensure_ascii=False)[:110]


def approval_preview(name, args):
    """승인 요청 본문 — 셸/파이썬은 코드 블록으로, 파일은 경로+크기로."""
    if name in ("run_shell", "run_python"):
        body = str(args.get("command") or args.get("code") or "")
        if len(body) > 1500:
            body = body[:1500] + "\n...(%d자 생략)" % (len(body) - 1500)
        return "```\n%s\n```" % body
    if name == "write_file":
        return "파일: %s (%d바이트)" % (args.get("path"), len(str(args.get("content") or "").encode("utf-8")))
    if name == "edit_file":
        return "파일: %s\n\n교체 전:\n```\n%s\n```\n교체 후:\n```\n%s\n```" % (
            args.get("path"), str(args.get("old") or "")[:600], str(args.get("new") or "")[:600])
    return json.dumps(args, ensure_ascii=False)[:1200]


def _fmt_dur(sec):
    sec = int(sec)
    if sec < 60:
        return "%d초" % sec
    if sec < 3600:
        return "%d분 %d초" % (sec // 60, sec % 60)
    if sec < 86400:
        return "%d시간 %d분" % (sec // 3600, (sec % 3600) // 60)
    return "%d일 %d시간" % (sec // 86400, (sec % 86400) // 3600)


class Bot(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.api = Telegram(cfg)
        self.stream = Stream(self.api)
        self.llm = LocalLLM(cfg)
        self.safety = Safety(cfg)
        self.soul_path = Path(os.path.expanduser(
            (cfg.get("soul_path") or "~/Claude_works/hermes-agent/data/SOUL.md")))
        self.workdir = os.path.expanduser(cfg.get("workdir") or "~/Claude_works")
        self.allowed = set(str(x) for x in ((cfg.get("telegram") or {}).get("allowed_chat_ids") or []))
        agent = cfg.get("agent") or {}
        self.persona = agent.get("persona_name") or "맥붐 다이렉트"
        self.max_steps = int(agent.get("max_tool_steps") or 120)
        # 진행 중인 턴이 이 글자 수를 넘으면 오래된 도구 결과를 생략/요약해 컨텍스트를 줄인다 (128K 모델 기준)
        self.compact_chars = int(agent.get("turn_compact_chars") or 200000)
        self.compact_keep = int(agent.get("compact_keep_steps") or 10)
        self.plan_step_max_steps = int(agent.get("plan_step_max_steps") or 30)
        # 붐엘에 없는 기능을 다루는 SOUL.md 섹션(## 제목 부분 일치)은 프롬프트에서 뺀다 — 토큰 40% 절약 +
        # "delegate_task 를 써라"/"없다"는 모순 제거. 파일 자체는 Hermes 와 공유하므로 건드리지 않는다.
        self.soul_skip = list(agent.get("soul_skip_sections") or SOUL_SKIP_DEFAULT)
        self._loop_exit = None
        self.history_chars = int(agent.get("history_max_chars") or 40000)
        self.show_progress = bool(agent.get("show_tool_progress", True))
        self.auto_boomco = bool(agent.get("auto_boomco_on_x_link", True))
        self._soul_cache = (None, 0.0)
        STATE_DIR.mkdir(parents=True, exist_ok=True)

        # 작업 스레드 관련
        self.tasks = TaskQueue(TASK_QUEUE_PATH)
        self.work = queue.Queue()
        self.cancel = threading.Event()
        self.approvals = {}          # token -> {chat_id, event, decision, name}
        self.approvals_lock = threading.Lock()
        self.current = None          # 워커가 처리 중인 작업 상태 (dict) 또는 None
        self.current_lock = threading.Lock()
        self.history_reset_at = {}   # chat_id -> /new 시각 (작업 중 /new 처리용)
        self.started_at = time.time()
        # 장기 기억: 자동 요약 + 프로젝트 노트 + Hermes 기억 읽기 (memory.py)
        self.memory = Memory(cfg, lambda: self.llm, STATE_DIR, log)

    # -------------------------------------------------- 영혼 / 시스템 프롬프트

    def soul(self):
        try:
            mtime = self.soul_path.stat().st_mtime
        except OSError:
            return "(SOUL.md를 읽을 수 없습니다: %s)" % self.soul_path
        if self._soul_cache[0] is None or self._soul_cache[1] != mtime:
            self._soul_cache = (strip_soul_sections(self.soul_path.read_text(encoding="utf-8"), self.soul_skip), mtime)
        return self._soul_cache[0]

    def tool_schemas(self):
        return T.tool_schemas() + self.memory.note_tool_schemas() + self.plan_tool_schemas()

    def system_prompt(self, chat_id=None):
        names = ", ".join(s["function"]["name"] for s in self.tool_schemas())
        try:
            model = self.llm.model()
        except Exception as exc:
            model = "(조회 실패: %s)" % exc
        return self.soul() + DIRECT_ADDENDUM.format(
            persona=self.persona, tool_names=names,
            workdir=self.workdir, model=model, max_steps=self.max_steps) + self.memory.prompt_block(chat_id)

    # -------------------------------------------------- 대화 기록

    def _hist_path(self, chat_id):
        return STATE_DIR / ("history-%s.json" % chat_id)

    def load_history(self, chat_id):
        path = self._hist_path(chat_id)
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def save_history(self, chat_id, turns):
        """오래된 '턴' 단위로 잘라낸다 — tool 메시지가 짝 잃고 남지 않도록 통째로 버린다.

        2026-09-14: 잘려 나가는 턴은 그냥 버리지 않고 memory.digest 로 요약해 기억 파일에 남긴다
        (워커 스레드에서 동기 실행 — 최종 답변은 이미 보낸 뒤라 마스터가 기다리진 않는다).
        """
        total = 0
        kept = []
        for turn in reversed(turns):
            size = len(json.dumps(turn, ensure_ascii=False))
            if total + size > self.history_chars and kept:
                break
            kept.insert(0, turn)
            total += size
        self._hist_path(chat_id).write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
        dropped = turns[:len(turns) - len(kept)]
        if dropped:
            self.memory.digest(chat_id, dropped, "기록 상한으로 오래된 대화 정리")
        return kept

    # -------------------------------------------------- 도구 실행

    def execute_tool(self, chat_id, name, args):
        if name == "run_shell":
            return T.run_shell(str(args.get("command") or ""), self.workdir,
                               int(args.get("timeout") or 180), cancel=self.cancel)
        if name == "run_python":
            return T.run_python(str(args.get("code") or ""), self.workdir,
                                int(args.get("timeout") or 180), cancel=self.cancel)
        if name == "read_file":
            return T.read_file(args.get("path"), args.get("offset") or 0, args.get("limit") or 400)
        if name == "write_file":
            return T.write_file(args.get("path"), str(args.get("content") or ""))
        if name == "edit_file":
            return T.edit_file(args.get("path"), str(args.get("old") or ""), str(args.get("new") or ""))
        if name == "list_dir":
            return T.list_dir(args.get("path") or self.workdir)
        if name == "web_fetch":
            return T.web_fetch(str(args.get("url") or ""), args.get("max_chars") or 5000)
        if name == "boomco_analyze_x":
            notify = lambda text: self.api.send(chat_id, text)
            summary, report = T.boomco_analyze(str(args.get("url") or ""), chat_id, notify)
            if report:
                self.api.send(chat_id, report)
            return summary
        if name == "toss_quote":
            return T._toss_get("/quote", {"symbols": str(args.get("symbols") or "")})
        if name == "toss_holdings":
            return T._toss_get("/holdings")
        if name == "toss_exchange_rate":
            return T._toss_get("/exchange-rate")
        if name == "send_file_to_master":
            return self.api.send_document(chat_id, args.get("path"), str(args.get("caption") or ""))
        if name == "system_status":
            return T.system_status(self.llm.base, self.llm._model or "(미조회)", self.llm.key,
                                   self._queue_note())
        if name.startswith("note_"):
            out = self.memory.run_note_tool(name, args)
            if out is not None:
                return out
        if name.startswith("plan_"):
            out = self.run_plan_tool(chat_id, name, args)
            if out is not None:
                return out
        return "알 수 없는 도구: %s" % name

    # -------------------------------------------------- 승인 (워커 ↔ 메인 스레드)

    def approve(self, chat_id, name, args, reason):
        token = uuid.uuid4().hex[:8]
        ev = threading.Event()
        with self.approvals_lock:
            self.approvals[token] = {"chat_id": str(chat_id), "event": ev, "decision": None,
                                     "name": name, "at": time.time()}
        with self.current_lock:
            cur = dict(self.current) if self.current else {}
        task_text = ((cur.get("entry") or {}).get("text") or "").replace("\n", " ")
        ctx = ""
        if task_text:
            ctx = "작업: \"%s\" (단계 %d/%d, %s 경과)\n" % (
                task_text[:100] + ("…" if len(task_text) > 100 else ""), cur.get("step", 0), self.max_steps,
                _fmt_dur(time.time() - cur.get("started", time.time())))
        text = ("⚠️ 승인 요청 — %s\n%s사유: %s\n\n%s\n\n"
                "버튼을 누르거나 '응'/'아니'로 답해주세요. (%d초 후 자동 거부 → 붐엘은 다른 방법을 찾거나 멈춤)"
                % (name, ctx, reason, approval_preview(name, args), self.safety.approval_timeout))
        markup = {"inline_keyboard": [[{"text": "✅ 실행", "callback_data": token + ":y"},
                                       {"text": "⛔️ 취소", "callback_data": token + ":n"}]]}
        self.api.send(chat_id, text, reply_markup=markup)
        log("approval ask", chat_id, name, reason[:80])
        deadline = time.time() + self.safety.approval_timeout
        while time.time() < deadline and not self.cancel.is_set():
            if ev.wait(1.0):
                break
        with self.approvals_lock:
            rec = self.approvals.pop(token, {})
        decision = rec.get("decision")
        if self.cancel.is_set():
            log("approval cancelled", chat_id, name)
            return False
        if decision is None:
            log("approval timeout", chat_id, name)
            self.api.send(chat_id, "⏳ 승인 시간이 지나 자동 거부했습니다.")
            return False
        log("approval", "yes" if decision else "no", chat_id, name)
        self.api.send(chat_id, "✅ 승인됨 — 실행합니다." if decision else "⛔️ 거부됨.")
        return decision

    def _resolve_approval_token(self, token, decision):
        with self.approvals_lock:
            rec = self.approvals.get(token)
            if rec is None:
                return False
            rec["decision"] = decision
            rec["event"].set()
            return True

    def _resolve_approval_chat(self, chat_id, decision):
        """버튼 대신 '응/아니' 텍스트로 답한 경우 — 그 chat 의 가장 오래된 대기 건에 적용."""
        with self.approvals_lock:
            cands = [(r["at"], t) for t, r in self.approvals.items()
                     if r["chat_id"] == str(chat_id) and r["decision"] is None]
            if not cands:
                return False
            token = sorted(cands)[0][1]
            self.approvals[token]["decision"] = decision
            self.approvals[token]["event"].set()
            return True

    def _pending_approval(self, chat_id=None):
        with self.approvals_lock:
            for r in self.approvals.values():
                if r["decision"] is None and (chat_id is None or r["chat_id"] == str(chat_id)):
                    return r
        return None

    # -------------------------------------------------- 에이전트 루프

    def _keep_typing(self, chat_id, stop_event):
        while not stop_event.wait(6):
            self.api.typing(chat_id)

    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _complete_cancellable(self, messages, schemas, tool_choice="auto"):
        """LLM 호출을 보조 스레드에서 돌리며 1초마다 /stop 을 확인한다.

        requests 는 진행 중인 요청을 끊을 수 없으므로 취소 시 보조 스레드는 버린다 —
        oMLX 는 그 응답 생성을 끝날 때까지 계속하므로(수 분) 그동안 다음 요청이 느릴 수 있다.
        """
        box = {}

        def go():
            try:
                box["resp"] = self.llm.complete(messages, schemas, tool_choice)
            except Exception as exc:  # noqa
                box["err"] = exc

        th = threading.Thread(target=go, daemon=True)
        th.start()
        while th.is_alive():
            th.join(1.0)
            if self.cancel.is_set():
                raise Cancelled()
        if "err" in box:
            raise box["err"]
        return box["resp"]

    def _set_current(self, **kw):
        with self.current_lock:
            if self.current is not None:
                self.current.update(kw)

    def _progress(self, chat_id, line):
        if not self.show_progress:
            return
        with self.current_lock:
            status_id = (self.current or {}).get("status_id")
        if status_id:
            self.api.edit(chat_id, status_id, line)
        else:
            mid = self.api.send(chat_id, line, rich=False)
            self._set_current(status_id=mid)

    @staticmethod
    def _parse_args(fn):
        """모델이 준 인수 JSON 파싱. 키 앞의 '_' 는 떼준다(`_caption` 같은 환각 방지)."""
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            return None
        if not isinstance(args, dict):
            return None
        return dict((str(k).lstrip("_"), v) for k, v in args.items())

    # -------------------------------------------------- 턴 중간 컨텍스트 압축
    #
    # 2026-09-16: 한 요청 안에서는 도구 호출·결과가 전부 메시지에 쌓이고 정리하는 장치가 없었다.
    # 도구 결과는 6000자까지라 80단계면 20만 자를 넘겨 128K 컨텍스트에 닿는다(실측: 76단계 70분 통과가
    # 최대). 그래서 매 LLM 호출 전에 진행 중인 턴 크기를 재고, 상한을 넘으면
    #   1단계: 최근 compact_keep 단계를 제외한 오래된 도구 결과·큰 도구 인수를 한 줄 스텁으로 교체
    #   2단계: 그래도 크면 오래된 부분을 로컬 모델로 요약해 assistant 메시지 하나로 접는다
    # this_turn 과 messages 는 같은 dict 객체를 가리키므로 in-place 수정이 둘 다에 반영되고,
    # 저장되는 기록도 압축본이 된다.

    @staticmethod
    def _msgs_chars(msgs):
        return sum(len(m.get("content") or "") + sum(len((c.get("function") or {}).get("arguments") or "")
                                                     for c in (m.get("tool_calls") or [])) for m in msgs)

    def _compact_turn(self, chat_id, messages, this_turn):
        total = self._msgs_chars(messages)
        if total <= self.compact_chars:
            return
        # 도구 호출 단계 경계: assistant(tool_calls) 메시지 인덱스
        step_idx = [i for i, m in enumerate(this_turn) if m.get("role") == "assistant" and m.get("tool_calls")]
        if len(step_idx) <= self.compact_keep:
            return
        cutoff = step_idx[-self.compact_keep]  # 이 인덱스부터는 그대로 둔다
        stubbed = 0
        for m in this_turn[1:cutoff]:
            if m.get("role") == "tool" and len(m.get("content") or "") > 200 and not m.get("_stub"):
                m["content"] = "(도구 결과 생략 — 원래 %d자. 필요하면 같은 도구를 다시 실행할 것)" % len(m["content"])
                m["_stub"] = True
                stubbed += 1
            elif m.get("role") == "assistant":
                for c in m.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    a = fn.get("arguments") or ""
                    if len(a) > 600:
                        try:
                            d = json.loads(a)
                            for k in ("content", "new", "old", "code", "command"):
                                if isinstance(d.get(k), str) and len(d[k]) > 200:
                                    d[k] = "(생략 %d자)" % len(d[k])
                            fn["arguments"] = json.dumps(d, ensure_ascii=False)
                        except Exception:
                            fn["arguments"] = a[:200] + "…(생략)"
                        stubbed += 1
        after = self._msgs_chars(messages)
        log("compact stub", chat_id, "%d건 %d→%d자" % (stubbed, total, after))
        if after <= self.compact_chars:
            self._progress(chat_id, "🗜 컨텍스트 압축: 오래된 도구 결과 %d건 생략 (%d→%d자)" % (stubbed, total, after))
            return
        # 2단계: 오래된 부분을 요약 한 덩어리로
        old = this_turn[1:cutoff]
        try:
            summary = self.memory.summarize_turns([old]) or "(요약 실패 — 앞부분 생략)"
        except Exception as exc:
            summary = "(요약 실패: %s — 앞부분 생략)" % exc
        folded = {"role": "assistant", "content": "[이 요청의 앞부분 %d단계는 컨텍스트 절약을 위해 요약됨]\n%s"
                  % (len(step_idx) - self.compact_keep, summary), "_folded": True}
        new_turn = [this_turn[0], folded] + this_turn[cutoff:]
        # messages 에서도 같은 구간을 교체 (this_turn 은 messages 의 꼬리)
        head = len(messages) - len(this_turn)
        messages[head:] = new_turn
        this_turn[:] = new_turn
        final = self._msgs_chars(messages)
        log("compact fold", chat_id, "%d단계 요약 %d→%d자" % (len(step_idx) - self.compact_keep, after, final))
        self._progress(chat_id, "🗜 컨텍스트 압축: 앞 %d단계를 요약으로 접음 (%d→%d자)" % (
            len(step_idx) - self.compact_keep, total, final))

    def _run_loop(self, chat_id, messages, this_turn, schemas, max_steps, label=""):
        """도구 루프 본체. 반환 (status, payload):
          ("final", 답변) / ("limit", 정리보고) / ("cancelled", (step, tools)) /
          ("error", 메시지) / ("exit", 도구가 남긴 payload — plan_create/plan_step_done)
        this_turn 은 messages 의 꼬리(같은 dict 객체)라 여기 append 하면 둘 다에 반영된다.
        """
        step = 0
        tool_count = 0
        self._loop_exit = None
        try:
            while step < max_steps:
                self._check_cancel()
                self._compact_turn(chat_id, messages, this_turn)
                stop = threading.Event()
                th = threading.Thread(target=self._keep_typing, args=(chat_id, stop), daemon=True)
                th.start()
                try:
                    resp = self._complete_cancellable(messages, schemas)
                except Cancelled:
                    raise
                except Exception as exc:
                    return ("error", "로컬 모델 호출 실패: %s: %s" % (type(exc).__name__, exc))
                finally:
                    stop.set()

                choice = (resp.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                calls = msg.get("tool_calls") or []
                finish_reason = choice.get("finish_reason")
                assistant = {"role": "assistant", "content": msg.get("content") or ""}
                if calls:
                    assistant["tool_calls"] = calls
                messages.append(assistant)
                this_turn.append(assistant)

                if not calls:
                    final = (msg.get("content") or "").strip()
                    if not final:
                        # 사고 과정만 쓰다 끝난 경우(메모리에 기록된 Qwen 계열 함정) 그대로 알린다.
                        reason = (msg.get("reasoning_content") or "").strip()
                        final = ("(모델이 최종 답변 없이 사고 과정만 반환했습니다. finish_reason=%s)\n\n%s"
                                 % (finish_reason, reason[-1500:]))
                    return ("final", final)

                step += 1
                for idx, call in enumerate(calls):
                    fn = call.get("function") or {}
                    name = fn.get("name") or "?"
                    call_id = call.get("id") or ("%s-%d" % (name, tool_count))
                    args = self._parse_args(fn)

                    if self.cancel.is_set():
                        # 남은 호출엔 결과를 채워야 다음 요청에서 tool_call 짝이 맞는다.
                        for rest in calls[idx:]:
                            this_turn.append({"role": "tool", "tool_call_id": rest.get("id") or name,
                                              "name": (rest.get("function") or {}).get("name") or name,
                                              "content": "(마스터가 /stop 으로 중단 — 실행 안 함)"})
                        raise Cancelled()

                    if args is None:
                        result = ("도구 인수 JSON 을 파싱할 수 없습니다"
                                  + (" — 출력 토큰 한도(max_tokens=%d)로 잘린 것 같습니다. 파일을 나눠서 쓰거나 "
                                     "edit_file 로 부분 수정하세요." % self.llm.max_tokens
                                     if finish_reason == "length" else ". 인수를 올바른 JSON 으로 다시 보내세요."))
                        log("tool", chat_id, name, "ARGS PARSE FAIL finish=%s" % finish_reason)
                    else:
                        tool_count += 1
                        desc = describe_call(name, args)
                        log("tool", chat_id, name, json.dumps(args, ensure_ascii=False)[:180])
                        self._set_current(step=step, tool=name, tool_desc=desc, tool_at=time.time())
                        self._progress(chat_id, "%s %s%d/%d %s · %s" % (
                            TOOL_ICON.get(name, "🔧"), label, step, max_steps, name, desc))

                        needs, reason = self.safety.check(name, args)
                        if needs and not self.approve(chat_id, name, args, reason):
                            if self.cancel.is_set():
                                for rest in calls[idx:]:
                                    this_turn.append({"role": "tool", "tool_call_id": rest.get("id") or name,
                                                      "name": (rest.get("function") or {}).get("name") or name,
                                                      "content": "(마스터가 /stop 으로 중단 — 실행 안 함)"})
                                raise Cancelled()
                            result = "마스터가 실행을 거부했습니다. 이 방법은 포기하고 다른 방법을 제안하세요."
                        else:
                            try:
                                result = self.execute_tool(chat_id, name, args)
                            except Exception as exc:
                                result = "도구 실행 예외: %s: %s" % (type(exc).__name__, exc)

                    tool_msg = {"role": "tool", "tool_call_id": call_id, "name": name, "content": result}
                    messages.append(tool_msg)
                    this_turn.append(tool_msg)

                    if self._loop_exit is not None:
                        # plan_create / plan_step_done 이 루프 종료를 요청 — 남은 호출은 실행하지 않는다
                        for rest in calls[idx + 1:]:
                            this_turn.append({"role": "tool", "tool_call_id": rest.get("id") or name,
                                              "name": (rest.get("function") or {}).get("name") or name,
                                              "content": "(계획 도구로 이 단계가 끝나 실행 안 함)"})
                        payload, self._loop_exit = self._loop_exit, None
                        return ("exit", payload)

            # ---- 소프트 한도: 뚝 끊지 않고 정리 보고를 받는다
            note = ("[시스템] 이번 %s의 도구 사용 한도(%d단계)에 도달했다. 도구를 더 쓰지 말고, "
                    "지금까지 **한 일 / 확인된 결과 / 남은 일**을 한국어로 정리해 보고하라."
                    % ("단계" if label else "요청", max_steps))
            messages.append({"role": "user", "content": note})
            this_turn.append({"role": "user", "content": note})
            self._progress(chat_id, "⏸ 도구 한도 %d단계 도달 — 정리 보고 작성 중" % max_steps)
            try:
                resp = self._complete_cancellable(messages, None, tool_choice="none")
                msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
                summary = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()[-1500:]
            except Cancelled:
                raise
            except Exception as exc:
                summary = "(정리 보고 생성 실패: %s: %s)" % (type(exc).__name__, exc)
            this_turn.append({"role": "assistant", "content": summary})
            return ("limit", summary)
        except Cancelled:
            log("agent cancelled", chat_id, "steps=%d tools=%d" % (step, tool_count))
            this_turn.append({"role": "assistant",
                              "content": "(마스터가 /stop 으로 이 작업을 중단시켰다. %d단계·도구 %d회까지 실행됨. "
                                         "이어서 하려면 어디까지 됐는지 먼저 확인할 것.)" % (step, tool_count)})
            return ("cancelled", (step, tool_count))

    def run_agent(self, chat_id, user_text):
        started = time.time()
        turns = self.load_history(chat_id)
        messages = [{"role": "system", "content": self.system_prompt(chat_id)}]
        for turn in turns:
            messages.extend(turn)
        # 시각은 시스템 프롬프트가 아니라 사용자 메시지에 붙인다 — 시스템 프롬프트를 고정해 oMLX 프리픽스
        # 캐시가 요청 사이에도 살아남게 (실측: 콜드 13.9초 vs 캐시 1.5초).
        this_turn = [{"role": "user", "content": "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M"), user_text)}]
        messages.extend(this_turn)

        def finish(text):
            self.api.send(chat_id, text)
            # 작업 도중 /new 가 들어왔으면 예전 기록은 버리고 이번 턴만 남긴다.
            base = [] if self.history_reset_at.get(str(chat_id), 0) > started else turns
            self.save_history(chat_id, base + [this_turn])

        status, payload = self._run_loop(chat_id, messages, this_turn, self.tool_schemas(), self.max_steps)
        if status == "final":
            finish(payload)
        elif status == "limit":
            finish("⏸ 도구 한도(%d단계)에 도달해 여기서 멈췄습니다. 계속하려면 '이어서 진행해'라고 보내거나 "
                   "/set steps N 으로 한도를 올려주세요. 큰 일이면 계획으로 쪼개 달라고 해도 됩니다.\n\n%s"
                   % (self.max_steps, payload))
        elif status == "cancelled":
            finish("⛔ 중단했습니다 (%d단계, 도구 %d회 실행 후). 진행 중이던 모델 응답은 서버에서 몇 분 더 "
                   "생성될 수 있습니다." % payload)
        elif status == "error":
            self.api.send(chat_id, payload)
        elif status == "exit" and payload.get("plan_created"):
            report = self._run_plan(chat_id, payload["plan_created"])
            this_turn.append({"role": "assistant", "content": report})
            finish(report)
        elif status == "exit":
            # 계획 밖에서 plan_step_done 을 부른 경우 등 — 그냥 보고로 마무리
            finish("(계획 도구 호출로 종료: %s)" % json.dumps(payload, ensure_ascii=False)[:300])

    # -------------------------------------------------- 계획 (큰 일을 쪼개서 순차 실행)
    #
    # 2026-09-17: "자체적으로 하기에 너무 큰 일은 알아서 계획을 세우고 쪼개서 진행하게" — 모델이
    # plan_create 로 3~8개 하위 작업을 정의하면, 하위 작업마다 **새 컨텍스트**(시스템 프롬프트 + 계획
    # 현황 + 이전 단계 결과 요약만)로 도구 루프를 돌린다. 한 요청에 모든 도구 결과가 쌓이는 문제를
    # 구조적으로 피하고, 단계마다 텔레그램에 진행 보고가 나가며, /stop 후 '이어서 진행해'·/plan resume
    # 로 멈춘 단계부터 재개된다. 계획은 state/plan-<chat>.json 에 영속화.

    def _plan_path(self, chat_id):
        return STATE_DIR / ("plan-%s.json" % chat_id)

    def load_plan(self, chat_id):
        try:
            return json.loads(self._plan_path(chat_id).read_text(encoding="utf-8"))
        except Exception:
            return None

    def save_plan(self, plan):
        plan["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._plan_path(plan["chat_id"]).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    PLAN_ICON = {"pending": "⬜", "running": "▶️", "done": "✅", "partial": "🟡", "blocked": "⛔", "skipped": "⏭"}

    def plan_text(self, plan, current=None):
        lines = ["📋 계획: %s [%s]" % (plan.get("goal"), plan.get("status"))]
        for st in plan["steps"]:
            icon = "▶️" if st["n"] == current else self.PLAN_ICON.get(st["status"], "⬜")
            line = "%s %d. %s" % (icon, st["n"], st["title"])
            if st.get("result") and st["status"] != "pending":
                line += " — " + st["result"].replace("\n", " ")[:160]
            lines.append(line)
        return "\n".join(lines)

    def plan_tool_schemas(self):
        def fn(name, desc, props, required):
            return {"type": "function", "function": {"name": name, "description": desc,
                    "parameters": {"type": "object", "properties": props, "required": required}}}
        return [
            fn("plan_create", "큰 요청을 3~8개 하위 작업으로 쪼개 계획을 만들고 순차 실행을 시작한다. 각 하위 작업은 "
                              "새 컨텍스트에서 따로 실행되므로 제목만으로 무엇을 할지 알 수 있게 구체적으로 쓴다. "
                              "이 도구를 부르면 현재 턴은 끝나고 시스템이 1단계부터 실행한다.",
               {"goal": {"type": "string", "description": "목표 한 문장"},
                "steps": {"type": "array", "items": {"type": "string"}, "description": "하위 작업 제목 목록(순서대로)"},
                "note": {"type": "string", "description": "관련 프로젝트 노트 이름(있으면). 완료 시 이력이 자동 추가된다"}},
               ["goal", "steps"]),
            fn("plan_step_done", "지금 실행 중인 계획 단계를 끝낸다. summary 에는 다음 단계가 알아야 할 사실"
                                 "(만든/바꾼 파일 경로, 결정, 남은 문제)을 2~5줄로 쓴다.",
               {"status": {"type": "string", "enum": ["done", "partial", "blocked"]},
                "summary": {"type": "string"}}, ["status", "summary"]),
            fn("plan_add_steps", "실행 중 새로 필요해진 하위 작업을 현재 단계 뒤에 추가한다.",
               {"steps": {"type": "array", "items": {"type": "string"}}}, ["steps"]),
        ]

    def run_plan_tool(self, chat_id, name, args):
        if name == "plan_create":
            steps = [str(s).strip() for s in (args.get("steps") or []) if str(s).strip()]
            if len(steps) < 2:
                return "계획은 2단계 이상이어야 합니다. 작은 일은 계획 없이 바로 처리하세요."
            plan = {"id": uuid.uuid4().hex[:8], "chat_id": str(chat_id), "goal": str(args.get("goal") or "")[:300],
                    "request": (self.current or {}).get("entry", {}).get("text", "")[:1000],
                    "note": str(args.get("note") or "").strip(), "status": "active",
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "steps": [{"n": i + 1, "title": t[:200], "status": "pending", "result": ""} for i, t in enumerate(steps[:12])]}
            self.save_plan(plan)
            self._loop_exit = {"plan_created": plan}
            return "계획 생성됨 (%d단계). 시스템이 1단계부터 순차 실행합니다." % len(plan["steps"])
        plan = self.load_plan(chat_id)
        if name == "plan_step_done":
            if not plan or plan.get("status") != "active":
                return "실행 중인 계획이 없습니다. 이 도구는 계획 단계 안에서만 씁니다."
            status = args.get("status") if args.get("status") in ("done", "partial", "blocked") else "done"
            self._loop_exit = {"step_done": {"status": status, "summary": str(args.get("summary") or "")[:1500]}}
            return "단계 종료 기록됨 (%s)." % status
        if name == "plan_add_steps":
            if not plan or plan.get("status") != "active":
                return "실행 중인 계획이 없습니다."
            new = [str(s).strip() for s in (args.get("steps") or []) if str(s).strip()]
            cur = next((s["n"] for s in plan["steps"] if s["status"] == "running"), len(plan["steps"]))
            idx = next((i for i, s in enumerate(plan["steps"]) if s["n"] == cur), len(plan["steps"]) - 1) + 1
            for t in new:
                plan["steps"].insert(idx, {"n": 0, "title": t[:200], "status": "pending", "result": ""})
                idx += 1
            for i, s in enumerate(plan["steps"]):
                s["n"] = i + 1
            self.save_plan(plan)
            return "단계 %d개 추가됨. 현재 계획:\n%s" % (len(new), self.plan_text(plan))
        return None

    def _plan_step_prompt(self, plan, step):
        lines = ["[계획 실행] 목표: %s" % plan["goal"], "원래 요청: %s" % plan.get("request", "")[:600], "", "전체 단계:"]
        for st in plan["steps"]:
            icon = "▶️" if st["n"] == step["n"] else self.PLAN_ICON.get(st["status"], "⬜")
            line = "%s %d. %s" % (icon, st["n"], st["title"])
            if st["status"] in ("done", "partial", "blocked") and st.get("result"):
                line += "\n   결과: " + st["result"].strip()[:700]
            lines.append(line)
        lines += ["", "지금 할 일: 단계 %d \"%s\". **이 단계만** 처리하라. 이전 단계 결과는 위 요약을 믿되 필요하면 "
                  "파일/셸로 확인한다. 다음 단계 일을 미리 하지 마라." % (step["n"], step["title"]),
                  "끝나면 반드시 plan_step_done(status, summary) 를 호출한다 — summary 는 다음 단계가 알아야 할 사실"
                  "(경로·결정·남은 문제) 2~5줄. 진행 중 새 하위 작업이 필요해지면 plan_add_steps 로 추가한다. "
                  "막히면 status=blocked 로 이유를 적는다."]
        return "\n".join(lines)

    def _run_plan(self, chat_id, plan):
        """계획의 pending 단계를 순서대로 새 컨텍스트에서 실행. 최종 보고 문자열 반환."""
        total = len(plan["steps"])
        self.api.send(chat_id, self.plan_text(plan) + "\n\n각 단계는 새 컨텍스트에서 따로 실행되고 끝날 때마다 보고합니다. "
                                                     "(/plan 현황 · /stop 중단 후 '이어서 진행해'로 재개)")
        log("plan start", chat_id, plan["id"], "%d단계" % total, plan["goal"][:80])
        self._set_current(plan=plan["id"])
        schemas = self.tool_schemas()
        paused = None
        while True:
            plan = self.load_plan(chat_id) or plan
            step = next((s for s in plan["steps"] if s["status"] == "pending"), None)
            if step is None:
                break
            total = len(plan["steps"])
            step["status"] = "running"
            self.save_plan(plan)
            self._set_current(plan_step="%d/%d %s" % (step["n"], total, step["title"]), status_id=None)
            self.api.send(chat_id, "▶️ 단계 %d/%d 시작: %s" % (step["n"], total, step["title"]), rich=False)
            messages = [{"role": "system", "content": self.system_prompt(chat_id)},
                        {"role": "user", "content": "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M"), self._plan_step_prompt(plan, step))}]
            this_turn = messages[1:]
            label = "단계%d " % step["n"]
            status, payload = self._run_loop(chat_id, messages, this_turn, schemas, self.plan_step_max_steps, label)
            plan = self.load_plan(chat_id) or plan  # plan_add_steps 로 바뀌었을 수 있음
            step = next(s for s in plan["steps"] if s["n"] == step["n"])
            if status == "exit" and payload.get("step_done"):
                step["status"] = payload["step_done"]["status"]
                step["result"] = payload["step_done"]["summary"]
            elif status == "final":
                step["status"] = "done"
                step["result"] = payload.strip()[:1500]
            elif status == "limit":
                step["status"] = "partial"
                step["result"] = "(단계 도구 한도 %d 도달) " % self.plan_step_max_steps + payload.strip()[:1200]
            elif status == "cancelled":
                step["status"] = "pending"
                plan["status"] = "paused"
                paused = "⛔ /stop 으로 계획을 일시정지했습니다 (단계 %d/%d 에서). '이어서 진행해' 또는 /plan resume 로 재개." % (step["n"], total)
            elif status == "error":
                step["status"] = "blocked"
                step["result"] = payload
                plan["status"] = "paused"
                paused = "⛔ 단계 %d 에서 오류로 계획을 일시정지했습니다: %s\n/plan resume 로 재개." % (step["n"], payload[:200])
            else:
                step["status"] = "blocked"
                step["result"] = "(예상 밖 종료: %s)" % status
            self.save_plan(plan)
            log("plan step", chat_id, plan["id"], "%d/%d" % (step["n"], total), step["status"], (step.get("result") or "")[:80])
            if paused:
                break
            icon = self.PLAN_ICON.get(step["status"], "✅")
            self.api.send(chat_id, "%s 단계 %d/%d %s\n%s" % (icon, step["n"], total, step["title"], (step.get("result") or "")[:900]))
            if step["status"] == "blocked":
                plan["status"] = "paused"
                self.save_plan(plan)
                paused = "⛔ 단계 %d 가 막혀 계획을 일시정지했습니다. 원인을 해결한 뒤 /plan resume 또는 '이어서 진행해'." % step["n"]
                break
        if paused:
            self._set_current(plan=None, plan_step=None)
            return paused + "\n\n" + self.plan_text(plan)
        # ---- 최종 보고
        plan["status"] = "done"
        self.save_plan(plan)
        digest = "\n".join("%d. %s [%s]\n%s" % (s["n"], s["title"], s["status"], (s.get("result") or "").strip()[:800]) for s in plan["steps"])
        try:
            resp = self._complete_cancellable(
                [{"role": "system", "content": "너는 붐엘이다. 아래는 마스터의 요청을 계획으로 쪼개 실행한 단계별 결과다. "
                                               "마스터에게 보낼 최종 보고를 한국어로 쓴다: 한 일, 검증된 결과, 부분 완료·막힌 것, "
                                               "마스터가 확인하거나 결정할 것. 짧은 불릿, 마크다운 표 금지, 한자 금지."},
                 {"role": "user", "content": "요청: %s\n목표: %s\n\n%s" % (plan.get("request", ""), plan["goal"], digest)}],
                None, tool_choice="none")
            report = (((resp.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
        except Exception as exc:
            report = "(최종 보고 생성 실패: %s)" % exc
        report = "🏁 계획 완료 (%d단계)\n\n%s" % (len(plan["steps"]), report.strip() or digest)
        if plan.get("note"):
            try:
                self.memory.note_append(plan["note"], "계획 '%s' 완료 (%d단계)" % (plan["goal"][:80], len(plan["steps"])))
            except Exception:
                pass
        log("plan done", chat_id, plan["id"])
        self._set_current(plan=None, plan_step=None)
        return report

    def resume_plan(self, chat_id):
        """/plan resume · '이어서 진행해' — 일시정지된 계획을 멈춘 단계부터."""
        plan = self.load_plan(chat_id)
        if not plan or plan.get("status") not in ("paused", "active"):
            self.api.send(chat_id, "재개할 계획이 없습니다.")
            return
        for s in plan["steps"]:
            if s["status"] in ("running", "blocked"):
                s["status"] = "pending"
        plan["status"] = "active"
        self.save_plan(plan)
        report = self._run_plan(chat_id, plan)
        turns = self.load_history(chat_id)
        self.save_history(chat_id, turns + [[{"role": "user", "content": "[%s] (계획 재개: %s)" % (time.strftime("%Y-%m-%d %H:%M"), plan["goal"][:100])},
                                            {"role": "assistant", "content": report}]])
        self.api.send(chat_id, report)

    # -------------------------------------------------- 명령어

    HELP = """{persona} (Hermes 미경유 · oMLX 직결)

일반 대화/지시를 그대로 보내면 SOUL.md 페르소나로 도구를 써서 처리합니다.
X 링크만 보내면 붐코 분석 파이프라인이 자동으로 돕니다 (Hermes와 같은 코드).
작업 중에도 명령은 바로 답합니다.

/status   붐엘 상태(작업 중인지·몇 단계째·대기열) + 로컬 모델·프로세스
/stop     진행 중인 작업 중단 (/stop all: 대기열까지 비움)
/queue    대기열(일반 요청·X 링크)
/log [n]  최근 도구 실행 로그 n줄 (기본 20)
/set      런타임 설정 보기 · /set steps 60 · /set effort low · /set tokens 8000 · /set progress off
/new      대화 기록 초기화
/model    현재 로드된 모델
/soul     SOUL.md 다시 읽기(앞부분 미리보기)
/plan     계획 현황 · /plan resume 멈춘 단계부터 재개 · /plan cancel 취소
/memory   자동 요약 기억 보기 (/memory clear 로 비우기 — 백업 남김)
/notes    프로젝트 노트 목록 (~/Claude_works/boomel-notes)
/restart  붐엘 프로세스 재시작(코드 반영) — 작업 중이면 /restart now
/id       내 chat id
/help     이 도움말"""

    def _queue_note(self):
        items = self.tasks.all()
        pend = [e for e in items if e.get("status") == "pending"]
        return "붐엘 대기열: %d건 (일반 %d, X링크 %d)" % (
            len(pend), sum(1 for e in pend if e.get("kind") == "chat"),
            sum(1 for e in pend if e.get("kind") == "boomco"))

    def status_text(self):
        with self.current_lock:
            cur = dict(self.current) if self.current else None
        lines = ["🤖 %s 상태" % self.persona]
        if cur:
            entry = cur["entry"]
            elapsed = _fmt_dur(time.time() - cur["started"])
            if entry.get("kind") == "boomco":
                lines.append("• 작업 중 (%s): X 링크 붐코 분석\n  %s" % (elapsed, entry.get("url")))
            else:
                text = (entry.get("text") or "").replace("\n", " ")
                lines.append("• 작업 중 (%s): \"%s\"" % (elapsed, text[:80] + ("…" if len(text) > 80 else "")))
                if cur.get("plan_step"):
                    lines.append("  📋 계획 단계 %s" % cur["plan_step"])
                if cur.get("tool"):
                    lines.append("  단계 %d/%d · 마지막 도구 %s (%s 전)\n  %s" % (
                        cur.get("step", 0), self.max_steps, cur["tool"],
                        _fmt_dur(time.time() - (cur.get("tool_at") or time.time())), cur.get("tool_desc", "")))
                else:
                    lines.append("  단계 0/%d · 모델 응답 대기 중" % self.max_steps)
            pend = self._pending_approval()
            if pend:
                lines.append("  ⚠️ 승인 대기 중: %s (%s 전) — 버튼 또는 '응'/'아니'" % (
                    pend["name"], _fmt_dur(time.time() - pend["at"])))
            if self.cancel.is_set():
                lines.append("  ⛔ 중단 요청됨 — 다음 단계 경계에서 멈춥니다")
        else:
            lines.append("• 대기 중 (진행 중인 작업 없음)")
        lines.append("• " + self._queue_note())
        lines.append("• 설정: steps=%d effort=%s tokens=%d progress=%s" % (
            self.max_steps, self.llm.reasoning_effort, self.llm.max_tokens, "on" if self.show_progress else "off"))
        lines.append("• 모델: %s" % (self.llm._model or "(미조회)"))
        lines.append("• 가동: %s (PID %d)" % (_fmt_dur(time.time() - self.started_at), os.getpid()))
        return "\n".join(lines)

    def _tail_log(self, n):
        try:
            with LOG_PATH.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 64 * 1024))
                data = fh.read().decode("utf-8", errors="replace")
        except Exception as exc:
            return "로그 읽기 실패: %s" % exc
        lines = [ln.replace(LOG_PREFIX + " ", "") for ln in data.splitlines() if ln.strip()]
        return "\n".join(lines[-n:]) or "(비어 있음)"

    def handle_command(self, chat_id, text, update_id=None):
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1:] if len(parts) > 1 else []
        if cmd in ("/help", "/start"):
            self.api.send(chat_id, self.HELP.format(persona=self.persona))
        elif cmd == "/status":
            body = self.status_text()
            try:
                infra = T.system_status(self.llm.base, self.llm._model or "(미조회)", self.llm.key,
                                        self._queue_note())
            except Exception as exc:
                infra = "인프라 조회 실패: %s" % exc
            self.api.send(chat_id, body + "\n\n— 인프라 —\n" + infra)
        elif cmd == "/stop":
            self._cmd_stop(chat_id, bool(arg and arg[0].lower() == "all"))
        elif cmd == "/queue":
            items = self.tasks.all()
            if not items:
                self.api.send(chat_id, "대기열 비어있음 (처리 중/대기 중인 요청 없음)")
            else:
                lines = ["대기열 %d건:" % len(items)]
                for i, e in enumerate(items, 1):
                    what = e.get("url") if e.get("kind") == "boomco" else (e.get("text") or "")[:60]
                    lines.append("%d. [%s·%s] %s (접수 %s)" % (
                        i, "X링크" if e.get("kind") == "boomco" else "요청", e.get("status"), what,
                        e.get("received_at")))
                self.api.send(chat_id, "\n".join(lines))
        elif cmd == "/log":
            try:
                n = max(1, min(int(arg[0]), 200)) if arg else 20
            except ValueError:
                n = 20
            self.api.send(chat_id, "```\n%s\n```" % self._tail_log(n))
        elif cmd == "/set":
            self._cmd_set(chat_id, arg)
        elif cmd == "/new":
            old = self.load_history(chat_id)
            self._hist_path(chat_id).unlink(missing_ok=True)
            self.history_reset_at[str(chat_id)] = time.time()
            note = ""
            if old and self.memory.enabled:
                # 지우기 전에 요약을 기억에 남긴다 (메인 스레드를 막지 않게 백그라운드)
                self.memory.digest_async(chat_id, old, "/new 로 기록 초기화")
                note = " 지운 대화 %d턴은 요약해서 기억(/memory)에 남깁니다." % len(old)
            self.api.send(chat_id, "대화 기록을 지웠습니다." + note + (
                " (진행 중인 작업이 끝나면 그 턴만 새 기록으로 남습니다)" if self.current else ""))
        elif cmd == "/plan":
            plan = self.load_plan(chat_id)
            sub = arg[0].lower() if arg else ""
            if not plan:
                self.api.send(chat_id, "계획이 없습니다. 큰 요청을 보내면 붐엘이 plan_create 로 쪼갭니다.")
            elif sub == "resume":
                if plan.get("status") == "done":
                    self.api.send(chat_id, "이미 완료된 계획입니다.")
                elif self.current is not None:
                    self.api.send(chat_id, "다른 작업이 진행 중입니다. 끝나면 다시 시도하세요.")
                else:
                    entry, _ = self.tasks.add("plan_resume", chat_id, text="(계획 재개) " + plan.get("goal", "")[:100])
                    self.work.put(entry)
                    self.api.send(chat_id, "계획을 멈춘 단계부터 재개합니다.")
            elif sub == "cancel":
                plan["status"] = "cancelled"
                self.save_plan(plan)
                if self.current and (self.current.get("plan") == plan.get("id")):
                    self._cmd_stop(chat_id, False)
                self.api.send(chat_id, "계획을 취소했습니다.\n" + self.plan_text(plan))
            else:
                self.api.send(chat_id, self.plan_text(plan))
        elif cmd == "/memory":
            if arg and arg[0].lower() == "clear":
                bak = self.memory.clear_summary(chat_id)
                self.api.send(chat_id, "기억을 비웠습니다." + (" 백업: %s" % bak if bak else " (원래 비어 있었음)"))
            else:
                text = self.memory.load_summary(chat_id)
                self.api.send(chat_id, ("🧠 자동 요약 기억 (%d자 / 상한 %d)\n\n%s" % (
                    len(text), self.memory.max_chars, text)) if text else
                    "🧠 아직 요약 기억이 없습니다. 대화 기록이 상한(%d자)을 넘거나 /new 를 하면 자동으로 쌓입니다."
                    % self.history_chars)
        elif cmd == "/notes":
            rows = self.memory.note_list()
            if not rows:
                self.api.send(chat_id, "프로젝트 노트가 아직 없습니다. (%s)" % self.memory.notes_dir)
            else:
                self.api.send(chat_id, "📒 프로젝트 노트 %d개 (%s)\n%s" % (
                    len(rows), self.memory.notes_dir, "\n".join("- %s (갱신 %s): %s" % r for r in rows)))
        elif cmd == "/soul":
            self._soul_cache = (None, 0.0)
            soul = self.soul()
            self.api.send(chat_id, "SOUL.md 리로드 완료 (%d자)\n\n%s..." % (len(soul), soul[:600]))
        elif cmd == "/model":
            try:
                self.api.send(chat_id, "현재 모델: %s" % self.llm.model())
            except Exception as exc:
                self.api.send(chat_id, "모델 조회 실패: %s" % exc)
        elif cmd == "/id":
            self.api.send(chat_id, "chat id: %s" % chat_id)
        elif cmd == "/restart":
            self._cmd_restart(chat_id, bool(arg and arg[0].lower() == "now"), update_id)
        else:
            self.api.send(chat_id, "모르는 명령입니다. /help 참고.")

    def _cmd_stop(self, chat_id, clear_all):
        with self.current_lock:
            cur = dict(self.current) if self.current else None
        msgs = []
        if cur:
            if cur["entry"].get("kind") == "boomco":
                msgs.append("붐코 분석 중에는 중단이 지원되지 않습니다 (파이프라인 자체 타임아웃 최대 25분). "
                            "대기열만 비우려면 /stop all.")
            else:
                self.cancel.set()
                # 승인 대기 중이면 깨워서 거부 처리
                with self.approvals_lock:
                    for r in self.approvals.values():
                        if r["decision"] is None:
                            r["decision"] = False
                            r["event"].set()
                msgs.append("⛔ 중단 요청했습니다 — 실행 중인 도구를 죽이고 다음 단계 경계에서 멈춥니다.")
                log("stop requested", chat_id)
        else:
            msgs.append("진행 중인 작업이 없습니다.")
        if clear_all:
            n = self.tasks.clear_pending()
            # 메모리 큐도 비운다 (워커가 꺼내기 전 항목)
            drained = 0
            while True:
                try:
                    self.work.get_nowait()
                    drained += 1
                except queue.Empty:
                    break
            msgs.append("대기열 %d건 비움." % n)
        self.api.send(chat_id, "\n".join(msgs))

    def _cmd_set(self, chat_id, arg):
        cur = ("현재 설정:\n• steps=%d (도구 한도)\n• effort=%s (reasoning_effort)\n• tokens=%d (max_tokens)\n"
               "• progress=%s (도구 진행 표시)\n\n예: /set steps 60 · /set effort low · /set tokens 8000 · "
               "/set progress off\n(런타임에만 적용, 재시작하면 config.yaml 값으로 돌아감)"
               % (self.max_steps, self.llm.reasoning_effort, self.llm.max_tokens,
                  "on" if self.show_progress else "off"))
        if len(arg) < 2:
            self.api.send(chat_id, cur)
            return
        key, val = arg[0].lower(), arg[1].lower()
        try:
            if key == "steps":
                self.max_steps = max(1, min(int(val), 1000))
            elif key == "effort":
                if val not in ("low", "medium", "high"):
                    raise ValueError("effort 는 low/medium/high")
                self.llm.reasoning_effort = val
            elif key == "tokens":
                self.llm.max_tokens = max(500, min(int(val), 64000))
            elif key == "progress":
                self.show_progress = val in ("on", "1", "true", "yes")
            else:
                raise ValueError("모르는 키: %s (steps/effort/tokens/progress)" % key)
        except ValueError as exc:
            self.api.send(chat_id, "설정 실패: %s" % exc)
            return
        log("set", key, val)
        self.api.send(chat_id, "적용됨: %s=%s" % (key, val))

    def _cmd_restart(self, chat_id, force, update_id):
        if self.current and not force:
            self.api.send(chat_id, "작업이 진행 중입니다. /stop 후 다시 하거나, 그래도 재시작하려면 /restart now "
                                   "(진행 중이던 요청은 재시작 후 '중단됨'으로 안내되고 자동 재개되지 않습니다).")
            return
        self.api.send(chat_id, "🔄 재시작합니다 — launchd(KeepAlive)가 몇 초 안에 다시 띄웁니다.")
        log("restart requested", chat_id, "force=%s" % force)
        if update_id is not None:
            self.stream.ack(update_id)  # 재시작 후 /restart 가 다시 배달돼 무한 재시작하지 않게
        sys.stdout.flush()
        os._exit(0)

    # -------------------------------------------------- 메인(폴링) 스레드

    def handle_update(self, upd):
        if "callback_query" in upd:
            cq = upd["callback_query"]
            data = cq.get("data") or ""
            token, _, verdict = data.rpartition(":")
            if token and self._resolve_approval_token(token, verdict == "y"):
                self.api.answer_callback(cq.get("id"), "확인")
            else:
                self.api.answer_callback(cq.get("id"), "이미 처리됐거나 만료된 요청입니다")
            return
        if "message" in upd:
            self.handle_message(upd["message"], upd.get("update_id"))

    def handle_message(self, msg, update_id=None):
        chat_id = str((msg.get("chat") or {}).get("id"))
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if self.allowed and chat_id not in self.allowed:
            log("허용되지 않은 chat:", chat_id, text[:60])
            self.api.send(chat_id, "이 봇은 허용된 사용자만 쓸 수 있습니다. chat id: %s" % chat_id)
            return
        if not text:
            self.api.send(chat_id, "텍스트만 처리할 수 있는 채널입니다.")
            return
        log("recv", chat_id, text[:120])

        if text.startswith("/"):
            self.handle_command(chat_id, text, update_id)
            return

        low = text.lower()
        if low in YES_WORDS or low in NO_WORDS:
            if self._resolve_approval_chat(chat_id, low in YES_WORDS):
                return  # 승인 응답으로 소비

        if self.auto_boomco:
            urls = T.extract_x_urls(text)
            if urls:
                self._enqueue_boomco(chat_id, urls)
                return

        # 일시정지된 계획이 있고 "이어서/계속/재개" 류면 계획 재개로 처리
        plan = self.load_plan(chat_id)
        if plan and plan.get("status") == "paused" and re.search(r"이어서|계속|재개|resume", text) and len(text) < 40:
            entry, total = self.tasks.add("plan_resume", chat_id, text="(계획 재개) " + plan.get("goal", "")[:100])
            self.work.put(entry)
            if self.current is not None:
                self.api.send(chat_id, "⏳ 현재 작업이 끝나면 멈춘 계획을 재개합니다.")
            return
        entry, total = self.tasks.add("chat", chat_id, text=text)
        self.work.put(entry)
        if self.current is not None:
            self.api.send(chat_id, "⏳ 지금 다른 작업 중 — 대기열 %d번째로 등록했습니다. 차례가 오면 바로 시작합니다. "
                                   "(/status 로 진행 상황, /stop 으로 현재 작업 중단)" % total)

    # ---------------------------------------------- 붐코 큐 (영속화 + 순차 처리)
    #
    # 2026-09-10: 바쁠 때(한 건 분석 중, 3~10분) 링크를 여러 통 따로 보내면 메시지마다
    # "1건 감지"만 찍혀서 "다 접수는 된 건가?"라는 불안을 유발한다는 실사용 피드백으로
    # 추가. 받는 즉시 디스크 큐에 기록해 ①"대기열에 몇 건째로 등록됐는지" 보여주고
    # ②크래시해도 재시작 시 이어서 처리하고 ③/queue 로 언제든 확인할 수 있게 한다.
    # 2026-09-13: 일반 요청과 같은 TaskQueue/워커로 통합 (처리는 여전히 완전 순차).

    def _enqueue_boomco(self, chat_id, urls):
        entries = []
        total = 0
        for url in urls:
            entry, total = self.tasks.add("boomco", chat_id, url=url)
            entries.append(entry)
        log("queue add", len(entries), "chat=%s" % chat_id, "queue_total=%d" % total,
            "urls=%s" % ",".join(urls))
        self.api.send(chat_id, (
            "🔗 X 링크 %d건 접수 (현재 대기열 총 %d건) — 순서대로 분석합니다. "
            "(1건당 보통 3~10분, 완료될 때마다 결과 전송 / 대기 현황은 /queue)"
        ) % (len(entries), total))
        for entry in entries:
            self.work.put(entry)

    def _process_one_boomco(self, entry):
        eid, url, chat_id = entry.get("id"), entry["url"], entry["chat_id"]
        log("queue start", url, "id=%s" % eid)
        started = time.monotonic()
        notify = lambda text: self.api.send(chat_id, text)
        try:
            summary, report = T.boomco_analyze(url, chat_id, notify)
        except Exception as exc:
            traceback.print_exc()
            summary, report = ("예외: %s: %s" % (type(exc).__name__, exc), None)
        elapsed = time.monotonic() - started
        ok = bool(report)
        log("queue done", url, "id=%s" % eid, "ok=%s" % ok, "elapsed=%.1fs" % elapsed)
        self.api.send(chat_id, report or ("분석 실패\n%s\n%s" % (url, summary)))

    # ---------------------------------------------- 워커 스레드

    def _worker(self):
        while True:
            entry = self.work.get()
            self.cancel.clear()
            with self.current_lock:
                self.current = {"entry": entry, "started": time.time(), "step": 0, "tool": None,
                                "tool_desc": "", "tool_at": None, "status_id": None}
            self.tasks.set_status(entry["id"], "processing")
            try:
                if entry.get("kind") == "boomco":
                    self._process_one_boomco(entry)
                elif entry.get("kind") == "plan_resume":
                    self.resume_plan(entry["chat_id"])
                else:
                    self.run_agent(entry["chat_id"], entry.get("text") or "")
            except Exception as exc:
                traceback.print_exc()
                try:
                    self.api.send(entry["chat_id"], "처리 중 예외: %s: %s" % (type(exc).__name__, exc))
                except Exception:
                    pass
            finally:
                self.tasks.remove(entry["id"])
                with self.current_lock:
                    self.current = None
                self.cancel.clear()

    def _recover_queue(self):
        """비정상 종료로 큐 파일에 남은 게 있으면 기동 시 처리 (유실 방지).

        - 구버전 붐코 큐 파일(macboom-boomco-queue.json)에 남은 건은 새 큐로 옮긴다.
        - X 링크: pending/processing 모두 다시 돌린다 (백엔드 409 가 중복 저장을 막는다).
        - 일반 요청: pending 은 그대로 실행, processing(작업 도중 죽음)은 자동 재실행하면
          파일 수정 등이 두 번 일어날 수 있어 **알리기만 하고 버린다**.
        """
        if LEGACY_BOOMCO_QUEUE.exists():
            try:
                legacy = json.loads(LEGACY_BOOMCO_QUEUE.read_text(encoding="utf-8"))
            except Exception:
                legacy = []
            for e in legacy if isinstance(legacy, list) else []:
                if e.get("url"):
                    self.tasks.add("boomco", e.get("chat_id"), url=e["url"])
            LEGACY_BOOMCO_QUEUE.unlink()
            if legacy:
                log("구 붐코 큐 이관:", len(legacy), "건")
        items = self.tasks.all()
        if not items:
            return
        log("큐 복구:", len(items), "건 남아있음")
        for e in items:
            chat_id = e.get("chat_id")
            if e.get("kind") == "chat" and e.get("status") == "processing":
                self.tasks.remove(e["id"])
                self.api.send(chat_id, "⚠️ 재시작 전에 처리 중이던 요청이 중단됐습니다 (자동 재개 안 함):\n\"%s\"\n"
                                       "필요하면 다시 보내주세요." % (e.get("text") or "")[:200])
                continue
            what = e.get("url") if e.get("kind") == "boomco" else "\"%s\"" % (e.get("text") or "")[:80]
            self.api.send(chat_id, "♻️ 재시작 전 대기열에 남아있던 건을 이어서 처리합니다: %s" % what)
            self.tasks.set_status(e["id"], "pending")
            self.work.put(e)

    def run(self):
        try:
            me = self.api._post("getMe", {})
        except Exception:
            me = None
        log("기동. 봇:", (me or {}).get("username") or "(getMe 실패)", "PID", os.getpid())
        try:
            log("모델:", self.llm.model())
        except Exception as exc:
            log("모델 조회 실패:", exc)
        if self.api.set_my_commands(BOT_COMMANDS) is not None:
            log("텔레그램 명령 메뉴 등록:", len(BOT_COMMANDS), "개")
        self._recover_queue()
        threading.Thread(target=self._worker, name="worker", daemon=True).start()
        while True:
            try:
                updates = self.stream.poll(30)
            except Exception as exc:
                log("폴링 오류:", type(exc).__name__, exc)
                time.sleep(3)
                continue
            for upd in updates:
                try:
                    self.handle_update(upd)
                except Exception as exc:
                    traceback.print_exc()
                    chat_id = str(((upd.get("message") or {}).get("chat") or {}).get("id") or "")
                    if chat_id:
                        self.api.send(chat_id, "처리 중 예외: %s: %s" % (type(exc).__name__, exc))
                finally:
                    # 명령은 실행됐고 일반 요청은 디스크 큐에 적재됐으므로 여기서 확인 처리.
                    self.stream.ack(upd["update_id"])


def main():
    os.chdir(str(BASE_DIR))
    Bot(load_config()).run()


if __name__ == "__main__":
    main()
