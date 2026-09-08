#!/usr/bin/env python3
"""텔레그램 토큰 없이 봇 내부(안전장치 + 에이전트 루프)를 검증하는 자체 테스트.

  /usr/bin/python3 selftest.py            # 안전장치만 (빠름)
  /usr/bin/python3 selftest.py --llm      # 실제 oMLX까지 태워서 도구 루프 검증 (수 분)

텔레그램 API는 가짜 객체로 대체하고, 보낸 메시지를 콘솔에 출력한다.
"""
import sys
import macboom_direct as M

CFG = {
    "telegram": {"token": "dummy", "api_base": "http://127.0.0.1:8081", "allowed_chat_ids": [1]},
    "llm": {"base_url": "http://127.0.0.1:8766/v1", "api_key": "omlx",
            "reasoning_effort": "low", "max_tokens": 2500, "temperature": 0.3},
    "soul_path": "~/Claude_works/hermes-agent/data/SOUL.md",
    "workdir": "~/Claude_works",
    "agent": {"max_tool_steps": 5, "show_tool_progress": True},
    "safety": {"approval_timeout": 5},
}


class FakeTelegram(object):
    def __init__(self, *_a, **_kw):
        self.sent = []

    def send(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        print("\n--- 봇 → 마스터 ---\n%s\n" % text[:2000])
        return 1

    def edit(self, chat_id, mid, text):
        print("   [진행] %s" % text[:160])

    def typing(self, chat_id):
        pass

    def send_document(self, chat_id, path, caption=""):
        return "전송 완료(가짜): %s" % path

    def _post(self, *a, **kw):
        return {}


def test_safety():
    print("== 안전장치 ==")
    safety = M.Safety(CFG)
    cases = [
        ("run_shell", {"command": "ls -la ~/Claude_works"}, False),
        ("run_shell", {"command": "rm -rf ~/Claude_works/local-llm"}, True),
        ("run_shell", {"command": "git push origin main"}, True),
        ("run_shell", {"command": "git status && git log --oneline -3"}, False),
        ("run_python", {"code": "import shutil; shutil.rmtree('/tmp/x')"}, True),
        ("write_file", {"path": "~/Claude_works/local-llm/direct-telegram/state/x.txt"}, False),
        ("write_file", {"path": "~/Claude_works/hermes-agent/data/config.yaml"}, True),
        ("write_file", {"path": "~/Library/LaunchAgents/foo.plist"}, True),
        ("write_file", {"path": "/etc/hosts"}, True),
        ("read_file", {"path": "~/Claude_works/hermes-agent/data/.env"}, True),
        ("read_file", {"path": "~/Claude_works/local-llm/SETUP.md"}, False),
        ("boomco_analyze_x", {"url": "https://x.com/a/status/1"}, False),
    ]
    failed = 0
    for name, args, expect in cases:
        got, reason = safety.check(name, args)
        mark = "OK " if got == expect else "FAIL"
        if got != expect:
            failed += 1
        print("%s %-18s %-58s 승인필요=%s %s" % (mark, name, str(args)[:56], got, reason))
    print("안전장치: %d/%d 통과" % (len(cases) - failed, len(cases)))
    return failed


def test_x_detection():
    print("\n== X 링크 감지 (Hermes 훅과 같은 함수) ==")
    import tools as T
    cases = [("https://x.com/foo/status/12345", 1),
             ("이거 봐 https://x.com/foo/status/12345", 0),
             ("https://x.com/a/status/1 https://twitter.com/b/status/2", 2),
             ("안녕", 0)]
    failed = 0
    for text, expect in cases:
        got = len(T.extract_x_urls(text))
        if got != expect:
            failed += 1
        print("%s %-52s → %d건 (기대 %d)" % ("OK " if got == expect else "FAIL", text[:50], got, expect))
    return failed


def test_llm():
    print("\n== 실제 oMLX 에이전트 루프 ==")
    M.Telegram = FakeTelegram
    bot = M.Bot(CFG)
    bot.api = FakeTelegram()
    print("시스템 프롬프트 %d자 (SOUL.md 포함)" % len(bot.system_prompt()))
    for prompt in ["안녕, 너 누구야? 한 줄로만 답해.",
                   "도구를 써서 이 맥의 oMLX에 지금 로드된 모델 이름을 실제로 확인하고 알려줘."]:
        print("\n>>> 마스터: %s" % prompt)
        bot.run_agent("1", prompt)
    return 0


if __name__ == "__main__":
    fails = test_safety() + test_x_detection()
    if "--llm" in sys.argv:
        fails += test_llm()
    print("\n총 실패: %d" % fails)
    sys.exit(1 if fails else 0)
