#!/usr/bin/env bash
# Verifies what the aws-cost-audit skill needs on this machine and in the account. Read-only.
# Usage: check_requirements.sh --profiles p1[,p2,...] [--region us-east-1] [--kube-context <ctx>]
set -u
REGION="us-east-1"; PROFILES=""; KCTX=""
while [ $# -gt 0 ]; do
  case "$1" in
    --profiles) PROFILES="$2"; shift 2;;
    --region) REGION="$2"; shift 2;;
    --kube-context) KCTX="$2"; shift 2;;
    *) echo "unknown arg $1"; exit 2;;
  esac
done
export AWS_PAGER=""
pass=0; warn=0; fail=0
ok()   { echo "PASS  $*"; pass=$((pass+1)); }
wrn()  { echo "WARN  $*"; warn=$((warn+1)); }
bad()  { echo "FAIL  $*"; fail=$((fail+1)); }

echo "== tools"
if command -v aws >/dev/null 2>&1; then
  v=$(aws --version 2>&1 | head -1)
  case "$v" in aws-cli/2*) ok "aws cli v2 ($v)";; *) bad "aws cli must be v2 (found: $v)";; esac
else bad "aws cli not found (install AWS CLI v2)"; fi
if command -v python3 >/dev/null 2>&1; then
  pv=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
  python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' && ok "python3 $pv" || bad "python3 >= 3.8 required (found $pv)"
else bad "python3 not found"; fi
command -v kubectl >/dev/null 2>&1 && ok "kubectl $(kubectl version --client 2>/dev/null | head -1 | tr -d '\n')" || wrn "kubectl not found (only needed for the EKS branch)"
for t in curl getent dig; do command -v $t >/dev/null 2>&1 && ok "$t available" || wrn "$t not found (optional, used to resolve vendor hosts)"; done

if [ -z "$PROFILES" ]; then echo; echo "No --profiles given; skipping AWS access checks."; else
IFS=',' read -r -a PLIST <<< "$PROFILES"
for P in "${PLIST[@]}"; do
  echo; echo "== profile $P (region $REGION)"
  if id=$(aws sts get-caller-identity --profile "$P" --output text --query 'Account' 2>/tmp/awscheck.err); then ok "credentials valid, account $id"; else bad "sts get-caller-identity: $(head -c 200 /tmp/awscheck.err)"; continue; fi
  end=$(date -u +%Y-%m-%d); start=$(date -u -d '3 days ago' +%Y-%m-%d 2>/dev/null || date -u -v-3d +%Y-%m-%d)
  if out=$(aws ce get-cost-and-usage --profile "$P" --time-period Start=$start,End=$end --granularity DAILY --metrics UnblendedCost --output text 2>/tmp/awscheck.err); then
    ok "cost explorer readable (ce:GetCostAndUsage)"
  else
    e=$(head -c 300 /tmp/awscheck.err)
    case "$e" in
      *"not enabled"*|*"DataUnavailable"*) bad "cost explorer not enabled in this (payer) account: enable it in Billing → Cost Explorer and wait up to 24h";;
      *AccessDenied*|*"not authorized"*) bad "no permission for ce:GetCostAndUsage (attach AWSBillingReadOnlyAccess, and in the payer activate 'IAM user and role access to Billing information')";;
      *) bad "cost explorer: $e";;
    esac
  fi
  if aws ce get-cost-and-usage --profile "$P" --time-period Start=${start}T00:00:00Z,End=${start}T02:00:00Z --granularity HOURLY --metrics UnblendedCost --output text >/dev/null 2>&1; then ok "hourly granularity enabled (optional)"; else wrn "hourly granularity not enabled (optional; CloudWatch is used for hour-of-day profiles)"; fi
  if aws savingsplans describe-savings-plans --profile "$P" --region us-east-1 --output text --query 'length(savingsPlans)' >/tmp/awscheck.out 2>/tmp/awscheck.err; then ok "savings plans readable ($(cat /tmp/awscheck.out) plan(s) visible from this account)"; else wrn "savingsplans:DescribeSavingsPlans failed (expected on member accounts; run on the payer): $(head -c 120 /tmp/awscheck.err)"; fi
  aws ce get-savings-plans-utilization --profile "$P" --time-period Start=$start,End=$end --output text >/dev/null 2>&1 && ok "ce:GetSavingsPlansUtilization" || wrn "ce:GetSavingsPlansUtilization failed (run on the payer)"
  aws cloudwatch list-metrics --profile "$P" --region "$REGION" --namespace AWS/EC2 --max-items 1 --output text >/dev/null 2>&1 && ok "cloudwatch:ListMetrics" || bad "cloudwatch:ListMetrics denied"
  aws ec2 describe-nat-gateways --profile "$P" --region "$REGION" --output text --query 'length(NatGateways)' >/tmp/awscheck.out 2>/dev/null && ok "ec2:Describe* ($(cat /tmp/awscheck.out) NAT gateway(s))" || bad "ec2:DescribeNatGateways denied"
  aws autoscaling describe-auto-scaling-groups --profile "$P" --region "$REGION" --max-items 1 --output text >/dev/null 2>&1 && ok "autoscaling:Describe*" || wrn "autoscaling:Describe* denied or no ASGs"
  aws eks list-clusters --profile "$P" --region "$REGION" --output text --query 'length(clusters)' >/tmp/awscheck.out 2>/dev/null && ok "eks:ListClusters ($(cat /tmp/awscheck.out) cluster(s))" || wrn "eks:ListClusters denied (fine if the account has no EKS)"
  aws ecs list-clusters --profile "$P" --region "$REGION" --output text --query 'length(clusterArns)' >/tmp/awscheck.out 2>/dev/null && ok "ecs:ListClusters ($(cat /tmp/awscheck.out) cluster(s))" || wrn "ecs:ListClusters denied (fine if the account has no ECS)"
  aws rds describe-db-instances --profile "$P" --region "$REGION" --output text --query 'length(DBInstances)' >/tmp/awscheck.out 2>/dev/null && ok "rds:Describe* ($(cat /tmp/awscheck.out) instance(s))" || wrn "rds:DescribeDBInstances denied"
  aws lambda list-functions --profile "$P" --region "$REGION" --max-items 1 --output text >/dev/null 2>&1 && ok "lambda:ListFunctions" || wrn "lambda:ListFunctions denied"
  aws ecr describe-repositories --profile "$P" --region "$REGION" --max-items 1 --output text >/dev/null 2>&1 && ok "ecr:DescribeRepositories" || wrn "ecr:DescribeRepositories denied or no ECR"
  aws compute-optimizer get-enrollment-status --profile "$P" --region "$REGION" --output text --query 'status' >/tmp/awscheck.out 2>/dev/null && ok "compute optimizer status: $(cat /tmp/awscheck.out) (Active gives right-sizing recommendations for free)" || wrn "compute-optimizer not readable (optional)"
done
fi

if [ -n "$KCTX" ]; then
  echo; echo "== kubernetes context $KCTX"
  if kubectl --context "$KCTX" get nodes --no-headers >/tmp/awscheck.out 2>/tmp/awscheck.err; then ok "kubectl get nodes ($(wc -l < /tmp/awscheck.out) nodes)"; else bad "kubectl get nodes: $(head -c 200 /tmp/awscheck.err)"; fi
  kubectl --context "$KCTX" get hpa -A --no-headers >/dev/null 2>&1 && ok "HPAs readable" || wrn "cannot list HPAs"
  kubectl --context "$KCTX" auth can-i create pods/portforward -A >/dev/null 2>&1 && ok "port-forward allowed" || wrn "port-forward may be denied (needed for Prometheus/Mimir profiles)"
  m=$(kubectl --context "$KCTX" get svc -A --no-headers 2>/dev/null | grep -i -E 'mimir.*query-frontend|prometheus-operator-kube-p-prometheus|thanos-query|prometheus-server' | awk '{print $1"/"$2}' | head -3 | tr '\n' ' ')
  [ -n "$m" ] && ok "metrics endpoint candidates: $m" || wrn "no Prometheus/Mimir service found by name (hour-of-day cluster profiles will need another source)"
fi

echo; echo "== summary: $pass pass, $warn warn, $fail fail"
[ "$fail" -eq 0 ] && echo "Ready to run the audit (warnings only limit optional steps)." || echo "Fix the FAIL items before running the audit."
exit $fail
