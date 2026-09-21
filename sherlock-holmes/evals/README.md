# Evals

One directory per case, all offline: each case seeds `./fixtures` with recorded Loki and Tempo
responses (from `fixtures/<case>/`) and asks for an investigation of that trace. Ground truth for
each case is in `fixtures/<case>/expected.json`; the graders encode it as PASS/FAIL rubrics.

Run from the plugin root (Bash must be granted, the collector is a Python script):

```bash
claude plugin eval . --scaffold --allow-tools "Bash(python3 *collect-trace.py*)" --ablation none
claude plugin eval . --scaffold --allow-tools "Bash(python3 *collect-trace.py*)" --ablation none --judge-model sonnet --case 04-* --runs 3
```

Requirements: Claude Code with `claude plugin eval`, and an OS sandbox backend for Bash
(`bubblewrap` and `socat` on Linux). Results land in `evals/results/` (git-ignored).

Run from a normal terminal, not from inside another Claude Code session's Bash tool: the eval's
sandbox is bubblewrap and it cannot be nested. On Ubuntu 24.04 and later, AppArmor restricts
unprivileged user namespaces by default and every Bash call inside the eval fails with
`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`. The `collector-output-used`
grader fails in that situation so a run that only *attempted* the collector is not counted as
a pass. Lift the restriction for the session with
`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (or an AppArmor profile for
bwrap) and re-run.

Use `--judge-model sonnet` for the two nuanced cases (contradictory logs, incomplete trace):
the default judge is a small model and the rubrics there ask it to accept split confidence
and labelled hypotheses.

Regenerate fixtures with `python3 evals/fixtures/generate.py` (deterministic, synthetic data only).

## What the plugin contributes

Measured on 2026-09-21 with the default two-arm run (each case also runs with no
plugin loaded), judge model `sonnet`, one run per arm:

| | with | without | Δ |
| --- | --- | --- | --- |
| Mean score over the six cases | 1.00 | 0.50 | **+0.50** |

Two graders are excluded from scoring because the baseline cannot pass them by
construction, not because it reasons worse: `skill-fired` (Claude Code excludes
`tool_used: Skill` automatically) and `collector-ran`, marked `arm: with-only`
since `collect-trace.py` does not exist without the plugin. Counting the latter
would have reported +0.56.

The interesting part is where the difference actually comes from. Given the same
recorded Loki and Tempo responses, a plain session already identifies the first
anomalous event correctly in four of six cases. What it does not do is say how
confident it is, separate what it observed from what it inferred, or hold the
line when the evidence is contradictory.

| Grader | without | with |
| --- | --- | --- |
| `first-anomaly` — right origin, no fabricated cause | 4/6 | 6/6 |
| `discipline` — no unproven cause stated as fact | 1/6 | 6/6 |
| `confidence-stated` — states HIGH/MEDIUM/LOW with a reason | 0/6 | 6/6 |
| `labels-used` — FACT separated from INFERENCE/HYPOTHESIS | 0/6 | 6/6 |

So the plugin's contribution is less "finds the answer" and more "reports it
honestly": it turns a usually-correct guess into a diagnosis that states its own
confidence and its own gaps.
