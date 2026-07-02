# ── 1단계: 빌드 ──────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# 시스템 패키지 (psycopg2 빌드에 필요)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 설치 (캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── 2단계: 실행 이미지 ────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# psycopg2 런타임 의존성
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# 빌드 결과물 복사
COPY --from=builder /install /usr/local

# 앱 소스 복사
COPY main.py config.py ./
COPY app/ ./app/
COPY templates/ ./templates/
COPY fonts/ ./fonts/

# 데이터 파일 저장용 디렉터리
RUN mkdir -p /data
ENV SESSIONS_PATH=/data/sessions.json
ENV RISK_HISTORY_PATH=/data/risk_history.json

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
