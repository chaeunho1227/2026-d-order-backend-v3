#!/bin/bash
# CloudWatch CPU/메모리 알람 생성
# m7i-flex.large (vCPU 2, RAM 8GB, burstable baseline ~40%) EC2 모니터링용
#
# 실행 방법:
#   EC2_INSTANCE_ID=i-xxxxxxxx \
#   SNS_TOPIC_ARN=arn:aws:sns:ap-northeast-2:123456789012:d-order-alerts \
#   ./scripts/setup-cloudwatch-alarms.sh
#
# SNS_TOPIC_ARN 이 비어 있으면 알람만 생성되고 알림 액션은 연결되지 않는다.
# 메모리 알람은 EC2 에 CloudWatch Agent + IAM(CloudWatchAgentServerPolicy) 가 설치되어
# CWAgent 네임스페이스로 mem_used_percent 가 보고되고 있어야 동작한다.

set -e

REGION="${AWS_REGION:-ap-northeast-2}"
INSTANCE_ID="${EC2_INSTANCE_ID:?EC2_INSTANCE_ID env not set (e.g., i-0abc...) }"
SNS_TOPIC_ARN="${SNS_TOPIC_ARN:-}"

PREFIX="d-order-prod"

ACTION_ARGS=()
if [ -n "$SNS_TOPIC_ARN" ]; then
  ACTION_ARGS=(--alarm-actions "$SNS_TOPIC_ARN" --ok-actions "$SNS_TOPIC_ARN")
  echo "SNS 알림 액션: $SNS_TOPIC_ARN"
else
  echo "경고: SNS_TOPIC_ARN 미설정 — 알람만 생성되고 알림 액션은 비어 있습니다."
fi

echo "=== CloudWatch 알람 생성 ==="
echo "Region:      $REGION"
echo "Instance:    $INSTANCE_ID"
echo ""

# 알람 1: 평균 CPU 가 baseline 근처에 장기 체류 → m7i-flex throttle 위험 신호
echo "[1/3] ${PREFIX}-ec2-cpu-baseline (5분 평균 >= 40% 가 30분 지속)"
aws cloudwatch put-metric-alarm \
  --region "$REGION" \
  --alarm-name "${PREFIX}-ec2-cpu-baseline" \
  --alarm-description "EC2(m7i-flex) baseline CPU(40%)를 30분 이상 초과 — throttle 위험 / 업스케일 검토" \
  --namespace AWS/EC2 \
  --metric-name CPUUtilization \
  --statistic Average \
  --period 300 \
  --evaluation-periods 6 \
  --datapoints-to-alarm 6 \
  --threshold 40 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --dimensions "Name=InstanceId,Value=${INSTANCE_ID}" \
  --treat-missing-data notBreaching \
  "${ACTION_ARGS[@]}"

# 알람 2: spike CPU 한계 도달
echo "[2/3] ${PREFIX}-ec2-cpu-spike (1분 평균 >= 90% 가 5분 지속)"
aws cloudwatch put-metric-alarm \
  --region "$REGION" \
  --alarm-name "${PREFIX}-ec2-cpu-spike" \
  --alarm-description "EC2 CPU 가 5분간 90% 이상 지속 — 입장 폭주/장애 가능성" \
  --namespace AWS/EC2 \
  --metric-name CPUUtilization \
  --statistic Average \
  --period 60 \
  --evaluation-periods 5 \
  --datapoints-to-alarm 5 \
  --threshold 90 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --dimensions "Name=InstanceId,Value=${INSTANCE_ID}" \
  --treat-missing-data notBreaching \
  "${ACTION_ARGS[@]}"

# 알람 3: 메모리 (CWAgent 필요)
echo "[3/3] ${PREFIX}-ec2-mem (5분 평균 mem_used_percent >= 80%)"
aws cloudwatch put-metric-alarm \
  --region "$REGION" \
  --alarm-name "${PREFIX}-ec2-mem" \
  --alarm-description "EC2 메모리 사용률이 5분 평균 80% 이상 — 워커 누수/배포 윈도우 위험 감지" \
  --namespace CWAgent \
  --metric-name mem_used_percent \
  --statistic Average \
  --period 300 \
  --evaluation-periods 3 \
  --datapoints-to-alarm 3 \
  --threshold 80 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --dimensions "Name=InstanceId,Value=${INSTANCE_ID}" \
  --treat-missing-data notBreaching \
  "${ACTION_ARGS[@]}"

echo ""
echo "=== 완료 ==="
echo "확인: AWS 콘솔 → CloudWatch → Alarms → ${PREFIX}-ec2-*"
