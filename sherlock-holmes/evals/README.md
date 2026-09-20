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
