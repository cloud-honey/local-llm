#!/bin/zsh
# 새 봇을 로컬 telegram-bot-api 서버(:8081)에서 쓰기 위한 1회성 준비.
#
# 텔레그램은 봇 하나가 클라우드 API 서버와 로컬 API 서버 양쪽에 동시에 붙는 걸 막는다.
# 로컬 서버로 옮기려면 먼저 클라우드 쪽에서 logOut 해야 한다(공식 절차).
#
# 사용법: ./setup_bot.sh <BOT_TOKEN>
set -e
TOKEN="$1"
[[ -z "$TOKEN" ]] && { echo "사용법: ./setup_bot.sh <BOT_TOKEN>"; exit 1; }

echo "1) 클라우드 API에서 logOut..."
curl -s "https://api.telegram.org/bot${TOKEN}/logOut" || true
echo

echo "2) 로컬 서버(:8081)가 이 봇을 인식하는지 확인 (getMe)..."
sleep 2
curl -s "http://127.0.0.1:8081/bot${TOKEN}/getMe"
echo
echo "위 응답에 \"ok\":true 와 봇 username이 보이면 준비 완료."
echo "logOut 직후 401이 나오면 10초쯤 기다렸다 다시 실행하세요."
