#!/bin/bash
# CloudWatch Logs 로그 그룹 생성 및 보존 기간 설정
# EC2에서 1회 실행 (IAM Role 연결 후)
#
# 실행 방법:
#   chmod +x scripts/setup-cloudwatch.sh
#   ./scripts/setup-cloudwatch.sh

set -e

REGION="ap-northeast-2"
LOG_GROUP="/d-order/prod"
RETENTION_DAYS=30

echo "=== CloudWatch Logs 설정 ==="
echo "Region: $REGION"
echo "Log Group: $LOG_GROUP"
echo "Retention: ${RETENTION_DAYS}일"
echo ""

# 로그 그룹 생성 (이미 존재하면 무시)
echo "[1/2] 로그 그룹 생성..."
aws logs create-log-group \
  --log-group-name "$LOG_GROUP" \
  --region "$REGION" 2>/dev/null && echo "  생성 완료" || echo "  이미 존재함 (정상)"

# 보존 기간 설정
echo "[2/2] 보존 기간 ${RETENTION_DAYS}일 설정..."
aws logs put-retention-policy \
  --log-group-name "$LOG_GROUP" \
  --retention-in-days "$RETENTION_DAYS" \
  --region "$REGION"
echo "  설정 완료"

echo ""
echo "=== 완료 ==="
echo "AWS 콘솔에서 확인: CloudWatch → Log groups → $LOG_GROUP"
