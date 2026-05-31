#!/bin/bash
# D-Order 프로덕션 전체 백업 — 로컬로 일괄 다운로드.
#
# 백업 대상:
#   1. PostgreSQL 덤프 (pg_dump custom format, 압축)
#   2. Redis RDB 스냅샷
#   3. .env 파일
#   4. S3 미디어 버킷 (옵션, --with-s3 플래그)
#
# 실행:
#   ./scripts/backup-from-prod.sh                # 1-3 만
#   ./scripts/backup-from-prod.sh --with-s3      # S3 미디어까지

set -euo pipefail

# ── 설정 ──────────────────────────────────────────────
PEM="${PEM:-d-order-prod-apiserver.pem}"
EC2_HOST="${EC2_HOST:-ubuntu@13.209.79.116}"
REMOTE_DIR="${REMOTE_DIR:-~/dorder}"
LOCAL_BACKUP_ROOT="${LOCAL_BACKUP_ROOT:-./backups}"

DATE=$(date +%F)
LOCAL_DIR="${LOCAL_BACKUP_ROOT}/${DATE}"
WITH_S3=false

for arg in "$@"; do
  case "$arg" in
    --with-s3) WITH_S3=true ;;
    -h|--help)
      grep -E '^# ' "$0" | head -20
      exit 0
      ;;
  esac
done

# PEM 존재 확인
if [ ! -f "$PEM" ]; then
  echo "❌ PEM 파일을 찾을 수 없음: $PEM"
  echo "   PEM=/path/to/key.pem $0 로 경로 지정하거나 현재 디렉토리에 두세요."
  exit 1
fi
chmod 400 "$PEM" 2>/dev/null || true

mkdir -p "$LOCAL_DIR"
echo "▶ 백업 디렉토리: $LOCAL_DIR"
echo ""

# ── Step 1. EC2 안에서 덤프 파일 생성 ───────────────────
echo "▶ [1/4] EC2 에서 덤프 생성 중..."
ssh -i "$PEM" "$EC2_HOST" bash <<EOF
set -e
cd ${REMOTE_DIR}

# .env 의 변수 추출
export \$(grep -E '^(POSTGRES_DB|POSTGRES_USER|REDIS_PASSWORD)=' .env | xargs)
D=${DATE}

# PostgreSQL 덤프
docker exec d-order-postgres-prod pg_dump \\
  -U "\$POSTGRES_USER" -d "\$POSTGRES_DB" -Fc -Z 9 \\
  -f /tmp/dorder-\${D}.dump
docker cp d-order-postgres-prod:/tmp/dorder-\${D}.dump ~/

# Redis BGSAVE
docker exec d-order-redis-prod redis-cli -a "\$REDIS_PASSWORD" --no-auth-warning BGSAVE >/dev/null
sleep 5
docker cp d-order-redis-prod:/data/dump.rdb ~/redis-\${D}.rdb

# .env 사본
cp .env ~/env-\${D}.bak

# 결과 출력
ls -lh ~/dorder-\${D}.dump ~/redis-\${D}.rdb ~/env-\${D}.bak
EOF
echo "✅ EC2 덤프 생성 완료"
echo ""

# ── Step 2. 로컬로 다운로드 ─────────────────────────────
echo "▶ [2/4] 로컬로 다운로드 중..."
scp -i "$PEM" "${EC2_HOST}:~/dorder-${DATE}.dump" "${LOCAL_DIR}/"
scp -i "$PEM" "${EC2_HOST}:~/redis-${DATE}.rdb"   "${LOCAL_DIR}/"
scp -i "$PEM" "${EC2_HOST}:~/env-${DATE}.bak"     "${LOCAL_DIR}/"
echo "✅ 로컬 다운로드 완료"
echo ""

# ── Step 3. EC2 임시 파일 정리 ──────────────────────────
echo "▶ [3/4] EC2 임시 파일 정리..."
ssh -i "$PEM" "$EC2_HOST" "rm -f ~/dorder-${DATE}.dump ~/redis-${DATE}.rdb ~/env-${DATE}.bak"
echo "✅ EC2 정리 완료"
echo ""

# ── Step 4. S3 미디어 동기화 (옵션) ─────────────────────
if [ "$WITH_S3" = true ]; then
  echo "▶ [4/4] S3 미디어 동기화 중..."
  BUCKET=$(ssh -i "$PEM" "$EC2_HOST" "grep AWS_STORAGE_BUCKET_NAME ${REMOTE_DIR}/.env | cut -d= -f2 | tr -d '\"'")
  REGION=$(ssh -i "$PEM" "$EC2_HOST" "grep AWS_S3_REGION_NAME ${REMOTE_DIR}/.env | cut -d= -f2 | tr -d '\"'")
  REGION="${REGION:-ap-northeast-2}"

  if [ -z "$BUCKET" ]; then
    echo "❌ AWS_STORAGE_BUCKET_NAME 을 .env 에서 못 읽음. 수동 sync 필요."
    exit 1
  fi

  echo "   버킷: s3://${BUCKET}/ (region: ${REGION})"
  mkdir -p "${LOCAL_DIR}/s3-media"
  aws s3 sync "s3://${BUCKET}/" "${LOCAL_DIR}/s3-media/" --region "$REGION"
  echo "✅ S3 동기화 완료"
else
  echo "▶ [4/4] S3 동기화 스킵 (--with-s3 로 활성화)"
fi
echo ""

# ── 요약 ────────────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 백업 완료"
echo ""
ls -lh "${LOCAL_DIR}/"
echo ""
du -sh "${LOCAL_DIR}/"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
