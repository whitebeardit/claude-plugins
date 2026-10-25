"""Read-only guard (D1): reads pass, writes are denied with a reason, opt-out works."""
import importlib.util
import io
import json
import pathlib
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "hooks" / "guard.py"
spec = importlib.util.spec_from_file_location("guard", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

ALLOWED = [
    "aws sts get-caller-identity --profile payer",
    "aws ce get-cost-and-usage --profile payer --time-period Start=2026-10-01,End=2026-10-25 --granularity MONTHLY --metrics UnblendedCost",
    "aws cloudwatch get-metric-data --metric-data-queries '[{\"Id\":\"m1\"}]' --start-time x --end-time y",
    "aws cloudwatch list-metrics --namespace AWS/NATGateway",
    "aws ec2 describe-nat-gateways --region us-east-1 --output json",
    "aws ec2 wait describe-instances",
    "aws eks list-clusters && aws eks describe-nodegroup --cluster-name c --nodegroup-name n",
    "aws eks update-kubeconfig --name prod --profile infra",
    "aws eks get-token --cluster-name prod",
    "aws s3 ls",
    "aws s3api list-buckets && aws s3api get-bucket-lifecycle-configuration --bucket b",
    "aws savingsplans describe-savings-plans --region us-east-1",
    "aws logs start-query --log-group-name g --query-string 'fields @message' --start-time 1 --end-time 2",
    "aws cloudtrail lookup-events --max-results 50",
    "aws sso login --profile p",
    "aws configure list",
    "aws --version",
    "AWS_PROFILE=infra aws rds describe-db-instances",
    "export AWS_PAGER=''; aws autoscaling describe-auto-scaling-groups | head",
    "rtk proxy aws ce get-savings-plans-utilization --time-period Start=a,End=b",
    "ID=$(aws ec2 describe-instances --query 'x' --output text); echo $ID",
    "kubectl --context ctx get nodes -o wide",
    "kubectl -n monitoring port-forward svc/prometheus 3997:9090",
    "kubectl describe nodes | grep -A5 'Allocated resources'",
    "kubectl auth can-i create pods/portforward -A",
    "kubectl top pods -A",
    "helm list -A && helm get values release -n ns",
    "eksctl get nodegroup --cluster c",
    "python3 -I scripts/ce_breakdown.py --profile payer map",
    "ls -la && cat file.txt",
]

DENIED = [
    ("aws ec2 modify-instance-attribute --instance-id i --instance-type t3.small", "aws ec2 modify-instance-attribute"),
    ("aws ec2 terminate-instances --instance-ids i-1", "aws ec2 terminate-instances"),
    ("aws ec2 delete-nat-gateway --nat-gateway-id nat-1", "aws ec2 delete-nat-gateway"),
    ("aws autoscaling set-desired-capacity --auto-scaling-group-name g --desired-capacity 0", "aws autoscaling set-desired-capacity"),
    ("aws autoscaling update-auto-scaling-group --auto-scaling-group-name g --min-size 0", "aws autoscaling update-auto-scaling-group"),
    ("aws eks update-nodegroup-config --cluster-name c --nodegroup-name n --scaling-config minSize=0", "aws eks update-nodegroup-config"),
    ("aws rds modify-db-instance --db-instance-identifier d --manage-master-user-password", "aws rds modify-db-instance"),
    ("aws s3 cp file s3://bucket/", "aws s3 cp"),
    ("aws s3 rm s3://bucket/key", "aws s3 rm"),
    ("aws s3api put-bucket-lifecycle-configuration --bucket b", "aws s3api put-bucket-lifecycle-configuration"),
    ("aws cloudwatch put-metric-data --namespace x", "aws cloudwatch put-metric-data"),
    ("aws ecr put-lifecycle-policy --repository-name r", "aws ecr put-lifecycle-policy"),
    ("aws ec2 create-flow-logs --resource-ids vpc-1", "aws ec2 create-flow-logs"),
    ("aws ssm start-session --target i-1", "aws ssm start-session"),
    ("aws ecs execute-command --cluster c --task t --command bash --interactive", "aws ecs execute-command"),
    ("aws ec2 describe-instances && aws ec2 stop-instances --instance-ids i-1", "aws ec2 stop-instances"),
    ("sudo aws iam create-user --user-name x", "aws iam create-user"),
    ("rtk proxy aws lambda update-function-configuration --function-name f --memory-size 256", "aws lambda update-function-configuration"),
    ("kubectl apply -f svc.yaml", "kubectl apply"),
    ("kubectl --context ctx scale deploy/app --replicas=0 -n ns", "kubectl scale"),
    ("kubectl delete pod x -n ns", "kubectl delete"),
    ("kubectl -n ns patch svc haproxy -p '{}'", "kubectl patch"),
    ("kubectl edit hpa app", "kubectl edit"),
    ("kubectl exec -it pod -- sh", "kubectl exec"),
    ("kubectl drain node-1", "kubectl drain"),
    ("kubectl rollout restart deploy/app", "kubectl rollout"),
    ("helm upgrade --install rel chart -n ns", "helm upgrade"),
    ("helm uninstall rel", "helm uninstall"),
    ("eksctl scale nodegroup --cluster c --name n --nodes 0", "eksctl scale nodegroup"),
    ("eksctl delete cluster --name c", "eksctl delete cluster"),
]


class Offending(unittest.TestCase):
    def test_allowed_commands_pass(self):
        for cmd in ALLOWED:
            self.assertEqual(guard.offending(cmd), [], cmd)

    def test_denied_commands_name_the_operation(self):
        for cmd, expected in DENIED:
            found = guard.offending(cmd)
            self.assertTrue(found, cmd)
            self.assertIn(expected, found, cmd)

    def test_unknown_verb_is_denied_not_allowed(self):
        self.assertEqual(guard.offending("aws ec2 frobnicate-instances"), ["aws ec2 frobnicate-instances"])
        self.assertEqual(guard.offending("kubectl frobnicate"), ["kubectl frobnicate"])


class Decide(unittest.TestCase):
    def payload(self, command, tool="Bash"):
        return {"tool_name": tool, "tool_input": {"command": command}}

    def test_write_is_denied_with_reason_and_way_out(self):
        reason = guard.decide(self.payload("aws ec2 delete-volume --volume-id v"), env={})
        self.assertIn("aws ec2 delete-volume", reason)
        self.assertIn("AWS_COST_AUDIT_ALLOW_WRITES=1", reason)

    def test_read_is_allowed(self):
        self.assertIsNone(guard.decide(self.payload("aws ec2 describe-volumes"), env={}))

    def test_opt_out(self):
        self.assertIsNone(guard.decide(self.payload("aws ec2 delete-volume --volume-id v"), env={"AWS_COST_AUDIT_ALLOW_WRITES": "1"}))

    def test_other_tools_are_ignored(self):
        self.assertIsNone(guard.decide(self.payload("aws ec2 delete-volume", tool="Read"), env={}))

    def test_main_prints_deny_json_only_when_denying(self):
        with mock.patch("sys.stdin", io.StringIO(json.dumps(self.payload("kubectl delete ns prod")))), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(guard.main(), 0)
        decision = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        with mock.patch("sys.stdin", io.StringIO(json.dumps(self.payload("kubectl get ns")))), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(guard.main(), 0)
        self.assertEqual(out.getvalue(), "")

    def test_malformed_input_never_blocks(self):
        with mock.patch("sys.stdin", io.StringIO("not json")), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(guard.main(), 0)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
