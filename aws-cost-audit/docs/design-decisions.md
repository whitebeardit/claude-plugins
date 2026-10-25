# Design decisions

Numbered so that reviews and issues can refer to them. D1 and D7 were decided by the founder on
2026-10-25; the rest are proposals carried by the plan.

## D1. Read-only by hook, not by instruction (0.2)

"Never change anything" is an instruction in 0.1 and a mechanism from 0.2: a `PreToolUse` hook for
`Bash` runs `hooks/guard.py` (Python, portable to Windows) that parses every `aws` and `kubectl`
invocation in the command and **denies** anything outside the read allowlist (`describe-*`, `get-*`,
`list-*`, `lookup-*`, `search-*`, `sts`, `sso login`, `configure`, `s3 ls`, `s3api list-*/get-*/head-*`,
`eks update-kubeconfig`, `--version`; `kubectl get/describe/top/logs/auth can-i/config/port-forward/version`).
The denial names the verb and the way out. Opt-out is explicit: `AWS_COST_AUDIT_ALLOW_WRITES=1`.
Trade-off accepted: with the plugin enabled, the whole session is read-only on AWS and
Kubernetes, and that is the point. Decided: on by default, with the opt-out.

## D2. Cost Explorer always from the payer

The `check` skill warns when a profile is not the payer; `map` never adds member accounts up as
if they were the bill. From a member account you see one account; from the payer you see all.

## D3. Fixed categories

The bill always comes out as Compute, Network, Data & databases, Storage, SaaS & Marketplace,
Commitments, Observability, Security & identity, Tax, Other. `EC2 - Other` is split by usage type
(EBS → Storage; transfer, NAT, IPv4 → Network). The rules live in one dictionary
(`CATEGORY_RULES` in `scripts/ce_breakdown.py`) covered by unit tests, so the report has the
same shape for every company and a reclassification is a one-line, tested change.

## D4. Commitments decide the conclusion about time of day

Before any compute lever the audit computes Savings Plan utilization and coverage. With
utilization at ~100% every day, the sentence "shrinking at night saves zero" goes in
automatically and compute levers get the on-demand overflow as their shared ceiling. Without this
step, every audit recommends "turn staging off at night" and most of them are wrong.

## D5. Python standard library only, AWS CLI through subprocess

No boto3, no pip: the scripts run on Linux, macOS, WSL, Git Bash and PowerShell (`py -3`
fallback as in sherlock-holmes 0.6.2). Calling the CLI through subprocess also sidesteps
command-line wrappers that reformat JSON, and parses the CLI's local-time timestamps explicitly
(the first audit lost an hour to a double timezone shift).

## D6. The report is born from a JSON, by script (0.3)

`scripts/build_plan.py levers.json → plan.html` renders the report from a file of levers (name,
front, nominal, realizable, effort, risk, stage, impact on what is running, forecast per month).
The model fills the JSON; the page is deterministic, checkable (stage sums, compute ceiling, last
month = potential) and gradable in evals. The 0.1 template becomes the mould of the generator.

## D7. Language

Repository, README, skills and `plugin.json` in English, like the rest of the marketplace. The
report comes out in the user's language; the levers JSON carries the text. The 0.1 mould keeps
its pt-BR labels (the skill tells the model to translate them); 0.3 ships an English mould with
the generator. Decided: English.

## D8. No mandatory MCP

The collector is the user's already-authenticated AWS CLI. The AWS Cost Explorer MCP server stays
an optional, documented alternative, as the Grafana MCP is in sherlock-holmes.

## D9. Idleness needs proof, not absence

A resource is called idle only when its metrics have datapoints present at zero for the window,
a control query on an active resource returns data by the same method, and the topology shows
nothing that could use it. "No data" is reported as unknown. The first audit found a regional NAT
gateway that looked idle only because its metrics live under an extra dimension.

## D10. Every recommendation carries its production impact

Five literal values: `none`, `staging only`, `window + rollback`, `rollout per service`,
`migration; own plan`. The report also carries a "Safe execution" section (off-peak windows
measured in the same audit, one change at a time through GitOps, observe 30 minutes, export
before deleting, measure before cutting). A plan the team cannot execute without an incident is
worth less than a smaller plan it can.
