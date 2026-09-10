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
    매번 /v1/models 로 조회(30초 캐싱) — proxy.mjs 와 같은 방식.
  - 실행: 시스템 파이썬 3.9(/usr/bin/python3). Hermes venv 의존성 없음.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import requests
import yaml

import tools as T

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
LOG_PREFIX = "[macboom-direct]"


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


class Telegram(object):
    def __init__(self, cfg):
        tg = cfg.get("telegram") or {}
        base = (tg.get("api_base") or "http://127.0.0.1:8081").rstrip("/")
        self.url = "%s/bot%s" % (base, tg["token"])
        self.session = requests.Session()

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
        self._post("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                       "text": text[:3900], "disable_web_page_preview": True})

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
    """업데이트 스트림 — 일반 메시지 큐와 승인 콜백을 한 폴링 루프에서 나눠 담는다.

    2026-09-10: offset을 메모리에만 들고 있으면 프로세스 재시작(예: plist 수정 후
    bootout+bootstrap) 때마다 Telegram이 "아직 확인 안 된" 업데이트를 다시 보내서
    직전에 받은 메시지가 중복 recv 되는 게 실측으로 확인됨(elder_plinius 링크가 재시작
    직후 다시 recv 로그에 찍힘) — 유실은 아니고 중복 재처리(붐코 쪽 409 백엔드가 중복
    저장은 막아줌)라 무해하지만 낭비. **다만 offset을 fetch 시점에 바로 영속화하면
    반대 방향 버그(진짜 유실)가 생긴다**: fetch 직후 ~ handle_message 완료 전 사이에
    크래시하면 그 메시지는 이미 "확인됨" 처리돼 Telegram이 재전송을 안 해준다 —
    이 봇이 지금 우선순위로 두는 건 "중복 없음"이 아니라 "유실 없음"이므로, 디스크에는
    **처리가 실제로 끝난 메시지까지만** 반영한다(`ack`). 메모리상의 `self.offset`은
    기존과 동일하게 fetch 시점에 즉시 전진(같은 폴링 세션 안에서 중복 fetch 방지용).
    """

    def __init__(self, api):
        self.api = api
        self.offset = self._load_offset()
        self.messages = []
        self.callbacks = {}
        self.texts = []  # 승인 대기 중 도착한 텍스트(예/아니오 판정용)
        self._pending_ack = None  # 아직 디스크에 반영 안 된, fetch만 된 update_id 중 최댓값

    @staticmethod
    def _load_offset():
        try:
            return json.loads(OFFSET_STATE_PATH.read_text(encoding="utf-8")).get("offset")
        except Exception:
            return None

    def ack(self, update_id):
        """update_id에 해당하는 메시지 처리가 실제로 끝난 뒤에만 호출 — 디스크 offset 전진."""
        try:
            OFFSET_STATE_PATH.write_text(
                json.dumps({"offset": update_id + 1}), encoding="utf-8")
        except Exception as exc:
            log("offset 저장 실패:", exc)

    def _pump(self, timeout):
        for upd in self.api.get_updates(self.offset, timeout):
            self.offset = upd["update_id"] + 1
            if "callback_query" in upd:
                cq = upd["callback_query"]
                self.callbacks[cq.get("data", "")] = cq
            elif "message" in upd:
                upd["message"]["_update_id"] = upd["update_id"]
                self.messages.append(upd["message"])

    def next_message(self, timeout=30):
        while not self.messages:
            self._pump(timeout)
        return self.messages.pop(0)

    def wait_approval(self, token, chat_id, timeout):
        """버튼 콜백 또는 '응/아니' 같은 텍스트 답장으로 승인 여부를 받는다."""
        deadline = time.time() + timeout
        yes = ("y", "yes", "ok", "ㅇ", "ㅇㅇ", "응", "네", "승인", "실행", "해", "해줘", "고")
        no = ("n", "no", "ㄴ", "ㄴㄴ", "아니", "아니오", "거부", "취소", "하지마", "stop")
        while time.time() < deadline:
            for key in list(self.callbacks):
                if key.startswith(token):
                    cq = self.callbacks.pop(key)
                    self.api.answer_callback(cq.get("id"), "확인")
                    return key.endswith(":y")
            for msg in list(self.messages):
                if str(msg.get("chat", {}).get("id")) != str(chat_id):
                    continue
                body = (msg.get("text") or "").strip().lower()
                if body in yes or body in no:
                    self.messages.remove(msg)
                    return body in yes
            self._pump(3)
        return None  # 시간 초과


# ------------------------------------------------------------------ 로컬 LLM


class LocalLLM(object):
    def __init__(self, cfg):
        llm = cfg.get("llm") or {}
        self.base = (llm.get("base_url") or "http://127.0.0.1:8766/v1").rstrip("/")
        self.key = llm.get("api_key") or "omlx"
        self.reasoning_effort = llm.get("reasoning_effort") or "low"
        self.max_tokens = int(llm.get("max_tokens") or 4000)
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

    def complete(self, messages, tool_schemas=None):
        body = {
            "model": self.model(),
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            # 메모리 기록: reasoning_effort medium은 대화가 길어질수록 응답이 기하급수적으로
            # 느려진다(47K 토큰 세션에서 인사말 342초). low가 사실상 필수.
            "reasoning_effort": self.reasoning_effort,
        }
        if tool_schemas:
            body["tools"] = tool_schemas
            body["tool_choice"] = "auto"
        resp = self.session.post(self.base + "/chat/completions", headers=self.headers,
                                 data=json.dumps(body).encode("utf-8"), timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError("oMLX HTTP %d: %s" % (resp.status_code, resp.text[:500]))
        return resp.json()


# ------------------------------------------------------------------ 안전장치


DEFAULT_DANGER = [
    r"\brm\s+-[a-z]*[rf]", r"\brm\s+", r"\bsudo\b", r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\b", r"\bmv\s+", r"\bchmod\b", r"\bchown\b",
    r"\b(kill|killall|pkill)\b", r"\blaunchctl\b", r"\bdiskutil\b", r"\bdd\s+if=",
    r"\b(shutdown|reboot)\b", r"\bbrew\s+(uninstall|remove)\b", r"\bnpm\s+publish\b",
    r"\bpip3?\s+uninstall\b", r"curl[^|]*\|\s*(ba|z)?sh", r"\bdefaults\s+write\b",
    r"\bcrontab\b", r"\bshutil\.rmtree\b", r"\bos\.(remove|unlink|rmdir)\b", r"\btruncate\b",
]

DEFAULT_PROTECTED = [
    "~/Library/LaunchAgents", "~/.ssh", "~/.aws", "~/.config/gcloud",
    "~/Claude_works/hermes-agent/data/config.yaml",
    "~/Claude_works/hermes-agent/data/.env",
    "~/Claude_works/hermes-agent/data/auth.json",
    "~/Claude_works/hermes-agent/data/SOUL.md",
    "~/Claude_works/local-llm/direct-telegram/config.yaml",
    "~/sns-tracker/scripts/.env",
]

DEFAULT_WRITE_ROOTS = ["~/Claude_works", "~/sns-tracker", "/tmp", "/private/tmp"]


class Safety(object):
    def __init__(self, cfg):
        s = cfg.get("safety") or {}
        self.patterns = [re.compile(p, re.I) for p in (s.get("danger_patterns") or DEFAULT_DANGER)]
        self.protected = [os.path.realpath(os.path.expanduser(p))
                          for p in (s.get("protected_paths") or DEFAULT_PROTECTED)]
        self.write_roots = [os.path.realpath(os.path.expanduser(p))
                            for p in (s.get("write_roots") or DEFAULT_WRITE_ROOTS)]
        self.approval_timeout = int(s.get("approval_timeout") or 300)

    def _is_protected(self, path):
        real = os.path.realpath(os.path.expanduser(str(path)))
        for p in self.protected:
            if real == p or real.startswith(p + os.sep):
                return True
        return False

    def _in_write_roots(self, path):
        real = os.path.realpath(os.path.expanduser(str(path)))
        return any(real == r or real.startswith(r + os.sep) for r in self.write_roots)

    def check(self, name, args):
        """(승인 필요 여부, 사유) 반환."""
        if name in ("run_shell", "run_python"):
            body = str(args.get("command") or args.get("code") or "")
            for pat in self.patterns:
                m = pat.search(body)
                if m:
                    return True, "위험 패턴 감지: `%s`" % m.group(0).strip()
            return False, ""
        if name in ("write_file", "edit_file"):
            path = args.get("path") or ""
            if self._is_protected(path):
                return True, "보호 대상 파일 수정: %s" % path
            if not self._in_write_roots(path):
                return True, "허용된 작업 루트 밖에 쓰기: %s" % path
            return False, ""
        if name in ("read_file", "send_file_to_master"):
            if self._is_protected(args.get("path") or ""):
                return True, "비밀정보 포함 가능 경로 접근: %s" % args.get("path")
            return False, ""
        return False, ""


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
- 위험한 명령(삭제·이동·push·권한 변경·시스템 설정 등)은 마스터의 텔레그램 승인 버튼을 거친
  뒤에만 실행된다. 거부되면 우회를 시도하지 말고 다른 방법을 제안한다.
- 상태를 물으면 추측하지 말고 `system_status`나 `run_shell`로 실제 확인한 뒤 답한다.
- 답변은 텔레그램으로 나간다. **굵게**, ## 제목, `코드`, [링크](url) 정도는 그대로 렌더링되지만\n  마크다운 표는 깨지니 쓰지 마라.
- **한국어로 답한다.** 이 채널은 도구 출력이 대부분 영어(셸 로그·README·설정 파일)라
  영어로 끌려가기 쉽다. 무엇을 읽었든 마스터에게 나가는 문장은 한국어로 쓴다.
  명령어·경로·로그 원문은 그대로 인용하되, 설명은 한국어다.
- **사고 과정을 그대로 보내지 마라.** 도구를 여러 번 쓴 뒤에는 생각을 정리한 초안이 아니라
  정리된 결론만 보낸다. 영어로 스스로에게 묻고 답하는 문장이 답변에 섞이면 실패다.

현재 시각: {now} (KST) / 작업 디렉터리: {workdir} / 로컬 모델: {model}
"""


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
        self.max_steps = int(agent.get("max_tool_steps") or 8)
        self.history_chars = int(agent.get("history_max_chars") or 40000)
        self.show_progress = bool(agent.get("show_tool_progress", True))
        self.auto_boomco = bool(agent.get("auto_boomco_on_x_link", True))
        self._soul_cache = (None, 0.0)
        STATE_DIR.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------- 영혼 / 시스템 프롬프트

    def soul(self):
        try:
            mtime = self.soul_path.stat().st_mtime
        except OSError:
            return "(SOUL.md를 읽을 수 없습니다: %s)" % self.soul_path
        if self._soul_cache[0] is None or self._soul_cache[1] != mtime:
            self._soul_cache = (self.soul_path.read_text(encoding="utf-8"), mtime)
        return self._soul_cache[0]

    def system_prompt(self):
        names = ", ".join(s["function"]["name"] for s in T.tool_schemas())
        try:
            model = self.llm.model()
        except Exception as exc:
            model = "(조회 실패: %s)" % exc
        return self.soul() + DIRECT_ADDENDUM.format(
            persona=self.persona, tool_names=names, now=time.strftime("%Y-%m-%d %H:%M"),
            workdir=self.workdir, model=model)

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
        """오래된 '턴' 단위로 잘라낸다 — tool 메시지가 짝 잃고 남지 않도록 통째로 버린다."""
        total = 0
        kept = []
        for turn in reversed(turns):
            size = len(json.dumps(turn, ensure_ascii=False))
            if total + size > self.history_chars and kept:
                break
            kept.insert(0, turn)
            total += size
        self._hist_path(chat_id).write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
        return kept

    # -------------------------------------------------- 도구 실행

    def execute_tool(self, chat_id, name, args):
        if name == "run_shell":
            return T.run_shell(str(args.get("command") or ""), self.workdir,
                               int(args.get("timeout") or 180))
        if name == "run_python":
            return T.run_python(str(args.get("code") or ""), self.workdir,
                                int(args.get("timeout") or 180))
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
            return T.system_status(self.llm.base, self.llm._model or "(미조회)", self.llm.key)
        return "알 수 없는 도구: %s" % name

    def approve(self, chat_id, name, args, reason):
        token = uuid.uuid4().hex[:8]
        preview = json.dumps(args, ensure_ascii=False)[:1200]
        text = ("⚠️ 승인 요청\n\n도구: %s\n사유: %s\n\n%s\n\n"
                "버튼을 누르거나 '응'/'아니'로 답해주세요. (%d초 후 자동 거부)"
                % (name, reason, preview, self.safety.approval_timeout))
        markup = {"inline_keyboard": [[{"text": "✅ 실행", "callback_data": token + ":y"},
                                       {"text": "⛔️ 취소", "callback_data": token + ":n"}]]}
        self.api.send(chat_id, text, reply_markup=markup)
        decision = self.stream.wait_approval(token, chat_id, self.safety.approval_timeout)
        if decision is None:
            self.api.send(chat_id, "⏳ 승인 시간이 지나 자동 거부했습니다.")
            return False
        self.api.send(chat_id, "✅ 승인됨 — 실행합니다." if decision else "⛔️ 거부됨.")
        return decision

    # -------------------------------------------------- 에이전트 루프

    def _keep_typing(self, chat_id, stop_event):
        while not stop_event.wait(6):
            self.api.typing(chat_id)

    def run_agent(self, chat_id, user_text):
        turns = self.load_history(chat_id)
        messages = [{"role": "system", "content": self.system_prompt()}]
        for turn in turns:
            messages.extend(turn)
        this_turn = [{"role": "user", "content": user_text}]
        messages.extend(this_turn)

        status_id = None
        schemas = T.tool_schemas()
        for step in range(self.max_steps):
            stop = threading.Event()
            th = threading.Thread(target=self._keep_typing, args=(chat_id, stop), daemon=True)
            th.start()
            try:
                resp = self.llm.complete(messages, schemas)
            except Exception as exc:
                stop.set()
                self.api.send(chat_id, "로컬 모델 호출 실패: %s: %s" % (type(exc).__name__, exc))
                return
            finally:
                stop.set()

            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            calls = msg.get("tool_calls") or []
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
                             % (choice.get("finish_reason"), reason[-1500:]))
                self.api.send(chat_id, final)
                self.save_history(chat_id, turns + [this_turn])
                return

            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name") or "?"
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {}

                brief = json.dumps(args, ensure_ascii=False)[:180]
                log("tool", chat_id, name, brief)
                if self.show_progress:
                    line = "🔧 %s %s" % (name, brief)
                    if status_id:
                        self.api.edit(chat_id, status_id, line)
                    else:
                        status_id = self.api.send(chat_id, line)

                needs, reason = self.safety.check(name, args)
                if needs and not self.approve(chat_id, name, args, reason):
                    result = "마스터가 실행을 거부했습니다. 이 방법은 포기하고 다른 방법을 제안하세요."
                else:
                    try:
                        result = self.execute_tool(chat_id, name, args)
                    except Exception as exc:
                        result = "도구 실행 예외: %s: %s" % (type(exc).__name__, exc)

                tool_msg = {"role": "tool", "tool_call_id": call.get("id") or name,
                            "name": name, "content": result}
                messages.append(tool_msg)
                this_turn.append(tool_msg)

        self.api.send(chat_id, "도구 반복 한도(%d단계)에 도달해 중단했습니다. 요청을 나눠서 다시 시켜주세요."
                      % self.max_steps)
        self.save_history(chat_id, turns + [this_turn])

    # -------------------------------------------------- 명령어

    HELP = """{persona} (Hermes 미경유 · oMLX 직결)

일반 대화/지시를 그대로 보내면 SOUL.md 페르소나로 도구를 써서 처리합니다.
X 링크만 보내면 붐코 분석 파이프라인이 자동으로 돕니다 (Hermes와 같은 코드).

/new      대화 기록 초기화
/status   로컬 모델·프로세스·붐코 큐 실제 상태
/queue    붐코 대기열(대기중/처리중 링크 목록)
/soul     SOUL.md 다시 읽기(앞부분 미리보기)
/model    현재 로드된 모델
/id       내 chat id
/help     이 도움말"""

    def handle_command(self, chat_id, text):
        cmd = text.split()[0].lower().split("@")[0]
        if cmd == "/help" or cmd == "/start":
            self.api.send(chat_id, self.HELP.format(persona=self.persona))
        elif cmd == "/new":
            self._hist_path(chat_id).unlink(missing_ok=True)
            self.api.send(chat_id, "대화 기록을 지웠습니다.")
        elif cmd == "/status":
            self.api.send(chat_id, T.system_status(self.llm.base, self.llm._model or "(미조회)",
                                                   self.llm.key))
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
        elif cmd == "/queue":
            items = T.queue_load()
            if not items:
                self.api.send(chat_id, "붐코 대기열 비어있음 (처리 중/대기 중인 링크 없음)")
            else:
                lines = ["붐코 대기열 %d건:" % len(items)]
                for i, e in enumerate(items, 1):
                    lines.append("%d. [%s] %s (접수 %s)" % (
                        i, e.get("status"), e.get("url"), e.get("received_at")))
                self.api.send(chat_id, "\n".join(lines))
        else:
            self.api.send(chat_id, "모르는 명령입니다. /help 참고.")

    # -------------------------------------------------- 메인 루프

    def handle_message(self, msg):
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
            self.handle_command(chat_id, text)
            return

        if self.auto_boomco:
            urls = T.extract_x_urls(text)
            if urls:
                self._enqueue_and_process_boomco(chat_id, urls)
                return

        self.run_agent(chat_id, text)

    # ---------------------------------------------- 붐코 큐 (영속화 + 순차 처리)
    #
    # 2026-09-10: 바쁠 때(한 건 분석 중, 3~10분) 링크를 여러 통 따로 보내면 메시지마다
    # "1건 감지"만 찍혀서 "다 접수는 된 건가?"라는 불안을 유발한다는 실사용 피드백으로
    # 추가. 메모리 for-loop만 쓰던 걸 디스크에 즉시 기록하는 큐로 바꿔서: ①받는 즉시
    # "대기열에 몇 건째로 등록됐는지" 보여주고 ②크래시해도 재시작 시 이어서 처리하고
    # ③`/queue`로 언제든 현재 대기 상태를 확인할 수 있게 한다. 처리 자체는 여전히
    # 완전 순차(단일 스레드 루프) — 동시 처리로 바꾼 게 아니다.

    def _enqueue_and_process_boomco(self, chat_id, urls):
        # id로 식별 (같은 url을 두 번 보내는 경우가 실제로 있었음 — url+chat_id만으로
        # 매칭하면 중복 건 중 하나를 처리한 뒤 나머지까지 같이 지워버리는 버그가 생김).
        items = T.queue_load()
        new_entries = []
        for url in urls:
            entry = {
                "id": uuid.uuid4().hex, "url": url, "chat_id": chat_id,
                "received_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "status": "pending",
            }
            items.append(entry)
            new_entries.append(entry)
        T.queue_save(items)
        pending_total = sum(1 for e in items if e.get("status") in ("pending", "processing"))
        log("queue add", len(new_entries), "chat=%s" % chat_id, "pending_total=%d" % pending_total,
            "urls=%s" % ",".join(urls))
        self.api.send(chat_id, (
            "🔗 X 링크 %d건 접수 (현재 대기열 총 %d건) — 순서대로 분석합니다. "
            "(1건당 보통 3~10분, 완료될 때마다 결과 전송 / 대기 현황은 /queue)"
        ) % (len(new_entries), pending_total))
        for entry in new_entries:
            self._process_one_boomco(entry)

    def _process_one_boomco(self, entry):
        eid, url, chat_id = entry.get("id"), entry["url"], entry["chat_id"]
        items = T.queue_load()
        for e in items:
            if e.get("id") == eid:
                e["status"] = "processing"
                break
        T.queue_save(items)
        remaining = sum(1 for e in items if e.get("status") in ("pending", "processing"))
        log("queue start", url, "id=%s" % eid, "remaining_incl_self=%d" % remaining)
        started = time.monotonic()

        notify = lambda text: self.api.send(chat_id, text)
        try:
            summary, report = T.boomco_analyze(url, chat_id, notify)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            summary, report = ("예외: %s: %s" % (type(exc).__name__, exc), None)

        elapsed = time.monotonic() - started
        items = T.queue_load()
        items = [e for e in items if e.get("id") != eid]
        T.queue_save(items)
        remaining = sum(1 for e in items if e.get("status") in ("pending", "processing"))
        ok = bool(report)
        log("queue done", url, "id=%s" % eid, "ok=%s" % ok, "elapsed=%.1fs" % elapsed,
            "remaining=%d" % remaining)
        self.api.send(chat_id, report or ("분석 실패\n%s\n%s" % (url, summary)))

    def _recover_boomco_queue(self):
        """비정상 종료로 큐 파일에 남은 게 있으면 기동 시 이어서 처리 (유실 방지)."""
        items = T.queue_load()
        if not items:
            return
        log("큐 복구:", len(items), "건 남아있음 — 이어서 처리")
        by_chat = {}
        for e in items:
            by_chat.setdefault(e.get("chat_id"), []).append(e.get("url"))
        for chat_id, urls in by_chat.items():
            self.api.send(chat_id, (
                "⚠️ 재시작 전에 처리하지 못하고 남아있던 X 링크 %d건을 이어서 처리합니다:\n%s"
            ) % (len(urls), "\n".join(urls)))
        for e in items:
            self._process_one_boomco(e)

    def run(self):
        try:
            me = self.api._post("getMe", {})
        except Exception:
            me = None
        log("기동. 봇:", (me or {}).get("username") or "(getMe 실패)")
        try:
            log("모델:", self.llm.model())
        except Exception as exc:
            log("모델 조회 실패:", exc)
        self._recover_boomco_queue()
        while True:
            try:
                msg = self.stream.next_message(30)
            except Exception as exc:
                log("폴링 오류:", type(exc).__name__, exc)
                time.sleep(3)
                continue
            try:
                self.handle_message(msg)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                chat_id = str((msg.get("chat") or {}).get("id"))
                self.api.send(chat_id, "처리 중 예외: %s: %s" % (type(exc).__name__, exc))
            finally:
                # handle_message가 정상/예외 어느 쪽으로든 "끝까지 시도"한 뒤에만 offset을
                # 디스크에 반영 — 그 전에 프로세스가 죽으면 Telegram이 재전송해주게 둔다
                # (유실 방지가 중복 방지보다 우선).
                update_id = msg.get("_update_id")
                if update_id is not None:
                    self.stream.ack(update_id)


def main():
    os.chdir(str(BASE_DIR))
    Bot(load_config()).run()


if __name__ == "__main__":
    main()
