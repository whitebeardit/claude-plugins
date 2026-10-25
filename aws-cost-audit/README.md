# AWS Cost Audit

A read-only cost audit of an AWS account, with or without Kubernetes, that ends in a phased cuts
plan: the bill classified into fixed categories, the biggest lines of spend, what can be cut now,
what needs engineering work, what needs architecture or contract changes, with a monthly savings
forecast and the production impact of every action. **It never changes the account.**

```
/plugin marketplace add whitebeardit/claude-plugins
/plugin install aws-cost-audit@whitebeard-plugins
/aws-cost-audit:check                       # what you need, and whether you have it
/aws-cost-audit:bill-map                    # where the money goes, in two minutes
/aws-cost-audit:idle                        # what costs money and does nothing, with proof
/aws-cost-audit:audit                       # the full audit → HTML report
```

## What you get

An HTML report (published as an artifact when the environment supports it, otherwise a file)
with, in this order:

1. **Where the money goes** — the bill in the same categories for every company: Compute,
   Network, Data & databases, Storage, SaaS & Marketplace, Commitments, Observability, Security,
   Tax, Other — plus the biggest lines (service or usage type).
2. **The largest category in detail** — network, compute, data or SaaS, whichever dominates.
3. **The plan in four stages** — *do now* (1–2 weeks, no architecture change), *engineering work*,
   *architecture*, *contracts and platform* — each with run-rate, effort, risk, prerequisite, who
   decides and **the impact on what is running**.
4. **Savings forecast per month**, conservative and optimistic.
5. **Levers one by one**, **safe execution** rules, **what not to do**, assumptions and limits.

Two rules are built into the method. Commitments are checked before any conclusion about time of
day: under a Savings Plan at 100% utilization, shutting down at night saves nothing, and the
report says so with the number. And every recommendation declares its impact on production
(`none`, `staging only`, `window + rollback`, `rollout per service`, `migration; own plan`).

## How it works

Scripts collect the facts; the model does the judgement.

| Script | What it does |
| --- | --- |
| `scripts/check_requirements.sh` / `.ps1` | tools, versions, credentials per profile, Cost Explorer enabled, the read-only API families, the cluster metrics endpoint |
| `scripts/ce_breakdown.py` | Cost Explorer: `map` (fixed categories + top lines + template JSON), `services`, `accounts`, `usage`, `regional` (InterZone × PublicIP), `regional-trend`, `sp` (Savings Plans utilization and coverage), `marketplace` |
| `scripts/cw_hod.py` | hour-of-day profile of any CloudWatch metric, local timezone, weekdays × weekends |
| `scripts/prom_hod.py` | the same from a Prometheus/Mimir reached by port-forward, plus top-N tables |
| `scripts/nat_idle_check.py` | proves a NAT gateway idle with three independent evidences |
| `scripts/k8s_scaling_snapshot.sh` | how an EKS cluster scales: nodegroups, autoscaler, HPAs at their cap, PDBs, requests per node, zone-aware routing |

Everything is Python standard library plus the AWS CLI already authenticated on your machine; no
pip, no boto3, no MCP required. `references/method.md` has the recipe for every step and how to
read each result; `references/report.md` the report contract.

## Prerequisites

| Tool | Version | Needed for | Required |
| --- | --- | --- | --- |
| AWS CLI | v2 | Cost Explorer, CloudWatch, EC2, EKS, RDS, ECR… | yes |
| Python | 3.8+ (standard library only) | the scripts | yes |
| bash | any | `k8s_scaling_snapshot.sh`, `check_requirements.sh` (Windows: Git Bash or WSL; `check_requirements.ps1` exists for PowerShell) | for the `.sh` only |
| kubectl | matching the cluster | the EKS branch: HPAs, nodes, port-forward | only with EKS |

**Access.** AWS CLI profiles for the **payer** (consolidated billing) account and for each member
account you will inspect. Cost Explorer run from the payer sees every account; from a member it
sees only itself. Cost Explorer must be enabled in the payer (first enablement takes up to 24 h),
and for IAM users the payer must have "IAM user and role access to Billing information" activated.
Hourly Cost Explorer granularity is optional and paid; the plugin works without it.

**Permissions.** Simplest: the managed policies `ReadOnlyAccess` and `AWSBillingReadOnlyAccess`.
Minimal policy, if you prefer:

```
ce:GetCostAndUsage, ce:GetSavingsPlansUtilization, ce:GetSavingsPlansCoverage,
ce:GetReservationCoverage, savingsplans:DescribeSavingsPlans,
cloudwatch:GetMetricData, cloudwatch:ListMetrics,
ec2:Describe*, autoscaling:Describe*, elasticloadbalancing:Describe*,
eks:Describe*, eks:List*, ecs:Describe*, ecs:List*, application-autoscaling:Describe*,
rds:Describe*, ecr:DescribeRepositories, ecr:GetLifecyclePolicy,
lambda:ListFunctions, lambda:ListProvisionedConcurrencyConfigs,
compute-optimizer:Get*, logs:DescribeLogGroups,
s3:ListAllMyBuckets, s3:GetLifecycleConfiguration, cloudtrail:LookupEvents,
sts:GetCallerIdentity
```

No `Put`, `Create`, `Modify`, `Delete` or `Update`: the audit does not need them and should not have them.

**With EKS.** `kubectl` authenticated on the cluster (`aws eks update-kubeconfig --name <cluster>
--profile <p>`), with `get`/`list` on nodes, pods, deployments, HPAs, PDBs, services and
configmaps, and `port-forward`. A Prometheus or Mimir reachable by port-forward, without a token;
kube-prometheus-stack usually keeps a few days, a Mimir/Thanos gives the 14 days the profiles use.

### Windows

| Path | What runs | How |
| --- | --- | --- |
| Native PowerShell | the `.ps1` checker and every Python script | install AWS CLI v2 (MSI) and Python 3 (python.org or `winget install Python.Python.3.12`); run `py -3 -I scripts\<script>.py …` or `python -I …` |
| Git Bash | all of the above plus the two `.sh` scripts | install Git for Windows; open "Git Bash" in the plugin folder and use the Linux commands |
| WSL2 (Ubuntu) | everything, identical to Linux | `wsl --install`; inside Ubuntu install AWS CLI v2, Python 3 and kubectl; profiles live in the Linux `~/.aws`, separate from Windows |

`kubectl port-forward` works in PowerShell: leave it in one window and run `prom_hod.py` in
another. AWS CLI profiles live in `%USERPROFILE%\.aws\config` and `credentials`; SSO:
`aws configure sso`, then `aws sso login --profile <p>`. If PowerShell refuses the script, use
`powershell -ExecutionPolicy Bypass -File .\scripts\check_requirements.ps1 …`.

## Quick setup

1. Install the plugin (top of this page) and fill the configuration when asked: payer profile,
   member profiles, region, the kubectl context if there is EKS, and your timezone offset.
   Values can be changed later with `/config`.
2. Run `/aws-cost-audit:check`. Fix the FAIL lines it prints; WARN lines only limit optional steps.
3. Run `/aws-cost-audit:audit`. Expect 30 to 90 minutes of collection and analysis depending on
   the size of the account. Claude will ask permission to run commands: all of them are reads
   (`describe`, `get`, `list`, `get-metric-data`, `get-cost-and-usage`, `kubectl get`,
   `port-forward`). If one ever contains `create`, `put`, `modify`, `delete` or `update`, refuse it.

## Use

```
/aws-cost-audit:check                                  # requirements and access
/aws-cost-audit:bill-map                               # the bill in fixed categories + biggest lines
/aws-cost-audit:idle                                   # idle NAT, volumes, EIPs, LBs, instances, with proof
/aws-cost-audit:audit                                  # full audit, uses the plugin configuration
/aws-cost-audit:audit --profiles billing,prod --region eu-west-1 --kube-context prod-eks
```

The `cost-auditor` agent runs the full audit in its own context when the task is delegated
("ask the cost auditor to…"); it has Bash, Read and Write, and the same guard applies to it.

Or just ask: "where does our AWS money go?", "how are we scaled at dinner peak?", "is there
anything to cut at night?", "are these NAT gateways idle?". The `audit` skill triggers on those.

## Security

- Read-only by construction: the scripts only issue `describe`/`get`/`list`/`lookup` calls and
  `get-metric-data`/`get-cost-and-usage`; the hour-of-day cluster profiles go through a
  `kubectl port-forward` and plain HTTP GETs.
- Credentials come from your own AWS CLI profiles and kubeconfig and are never printed. Scripts
  that scan configmaps for vendor hostnames strip anything that looks like a credential before
  printing.
- Vendor hostnames are resolved locally and compared with the published AWS IP ranges, nothing is
  sent anywhere but AWS.
- Idleness is only called proven when metrics have datapoints at zero, never when data is missing.
- **Read-only guard.** While the plugin is enabled, a `PreToolUse` hook (`hooks/guard.py`) denies
  any `aws`, `kubectl`, `helm` or `eksctl` write verb before it runs, naming the operation and the
  way out. Reads (`describe-*`, `get-*`, `list-*`, `lookup-*`, `sts`, `s3 ls`, `kubectl get/describe/
  port-forward`…) and local-only calls (`eks update-kubeconfig`, `sso login`) pass. So "read-only"
  does not depend on the model following an instruction. The whole session becomes read-only on
  AWS and Kubernetes, which is the point of an audit; to operate in the same session set
  `AWS_COST_AUDIT_ALLOW_WRITES=1` or disable the plugin. The guard never blocks on malformed input.

## Test

```bash
cd aws-cost-audit
python3 -m unittest discover -s tests -v          # bill classification, timezone handling, the read-only guard; no network
echo '{"tool_name":"Bash","tool_input":{"command":"aws ec2 delete-volume --volume-id v"}}' | python3 hooks/guard.py   # prints a deny
claude plugin validate .
```

Live smoke tests need an AWS profile: `python3 -I scripts/ce_breakdown.py --profile <payer> map`
and `bash scripts/check_requirements.sh --profiles <payer>`.

## Roadmap

Shipped:

- 0.2 (first release): `audit`, `check`, `bill-map` and `idle` skills, the `cost-auditor` agent,
  the read-only guard hook (on by default, opt-out `AWS_COST_AUDIT_ALLOW_WRITES=1`), the
  collection scripts, the report mould (pt-BR labels).

Next:

- 0.3: `build_plan.py` (levers JSON → report HTML, with sum checks), synthetic fixtures for three
  account profiles, offline evals, English report mould.
- 0.4: Compute Optimizer and Cost Anomaly Detection as optional sources, multi-region, ECS and
  Lambda hour-of-day profiles ready-made, Reserved Instances beside Savings Plans, demo GIF.

## License

MIT
