#!/usr/bin/env bash
# 주 1회 WSL에서 실행: 재무 + 1층 검사 + 화면 데이터 -> GitHub 업로드
set -euo pipefail
cd "$(dirname "$0")"
TOKEN=$(tr -d '[:space:]' < ~/.gh_token)
AUTH="Authorization: Basic $(printf 'ljk1824:%s' "$TOKEN" | base64 -w0)"
gitp() { git -c http.extraHeader="$AUTH" "$@"; }

gitp pull --rebase --autostash origin main
source .venv/bin/activate
export DART_API_KEY=$(tr -d '[:space:]' < ~/.dart_key)
python scripts/screener.py --mode all

git add data docs/data
if git diff --cached --quiet; then echo "변경 없음"; exit 0; fi
git commit -m "주간 재무·1층 검사 갱신 $(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M') KST"
if ! gitp push origin main; then
  echo "원격이 먼저 갱신됨 -> 합친 뒤 다시 업로드 (로컬 결과 우선)"
  gitp pull --rebase -X theirs origin main
  gitp push origin main
fi
echo "업로드 완료"
