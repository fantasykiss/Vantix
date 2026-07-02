#!/bin/bash

PROJECT_DIR="/Users/min-sukkim/redrisk-app"

echo "🚀 Vantix 작업환경 시작..."

# 1. Docker Desktop
echo "[1/5] Docker Desktop 실행 중..."
open -a Docker

echo "      Docker 준비 대기 중..."
for i in $(seq 1 30); do
  if docker info &>/dev/null; then
    echo "      ✅ Docker 준비 완료"
    break
  fi
  sleep 2
  if [ $i -eq 30 ]; then
    echo "      ⚠️  Docker 시간 초과 — 수동으로 확인하세요"
  fi
done

# 2. VS Code
echo "[2/5] VS Code 열기..."
code "$PROJECT_DIR"
echo "      ✅ VS Code 실행"

# 3. 서버 시작
echo "[3/5] Vantix 서버 시작..."
cd "$PROJECT_DIR"
source venv/bin/activate
python3 main.py &
SERVER_PID=$!
echo "      ✅ 서버 시작 (PID: $SERVER_PID)"

# 4. 서버 응답 대기
echo "[4/5] 서버 응답 대기 중..."
for i in $(seq 1 15); do
  if curl -s http://localhost:8000 &>/dev/null; then
    echo "      ✅ 서버 응답 확인"
    break
  fi
  sleep 1
done

# 5. 브라우저 열기
echo "[5/5] 브라우저 열기..."
open http://localhost:8000
open https://github.com/fantasykiss/Vantix
open https://railway.app/dashboard
echo "      ✅ 브라우저 실행 (로컬 / GitHub / Railway)"

echo ""
echo "✅ Vantix 준비 완료"
echo "   · 로컬:    http://localhost:8000"
echo "   · GitHub:  https://github.com/fantasykiss/Vantix"
echo "   · Railway: https://railway.app/dashboard"
echo "   서버 종료: kill $SERVER_PID"
