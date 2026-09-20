# Design decisions

Record of the decisions that shaped V1, and where they depart from the original plan
(`sherlock-holmes-agent-plan.md`, September 2026). The plan's method survived intact; the execution changed.

## D1. Generic plugin, Loki + Tempo only

Scope: any Grafana Loki and any Grafana Tempo, no assumptions about a specific company, stack, log format or label scheme. Everything environment-specific is configuration (`LOKI_SELECTOR`, service labels, trace-id filter mode, Tempo API version, access mode) or a per-project priors file. No CloudWatch, no service registry, no domain rules in the plugin.

## D2. Tempo bounds the window; the agent still reads logs first

The plan said "Loki-first, Tempo-on-demand". For *collection* that is backwards: a trace id has no timestamp, a substring search over a wide Loki window scans every chunk of every stream, and it misses yesterday's incident with a lookback from now. Tempo lookup by id is indexed, cheap and returns exact bounds. So the collector always asks Tempo first and queries Loki inside `[start - pad, end + pad]`. When Tempo has nothing, it uses `--around`/`--start`/`--end` or a lookback and flags the window as `now`-relative. The agent's *reading* order is unchanged: logs narrate, spans settle structure.

Consequence: "use Tempo only when necessary" from the plan's success criteria no longer applies to collection. It applies to reasoning: the span tree is presented, the agent uses it to settle call direction and where time went.

## D3. Python collector over the Grafana MCP server

The official Grafana MCP has no native trace tool; Tempo tools are proxied from Tempo's own MCP server, which must be enabled and only exists in recent Tempo versions. Its Loki tool caps lines per call at a server-start flag (default 100). It ships write tools. And it cannot give the deterministic shaping the method depends on: truncation flags, span tree, log-span join, dedupe, fixtures. The collector is stdlib Python, GET-only, works against every Tempo via `/api/traces/<id>`, and through Grafana's datasource proxy or direct URLs. The MCP remains an optional complement for follow-ups.

## D4. No `permissionMode` in the agent; pre-approval lives in the skill

Plugin agents cannot set `permissionMode`, `hooks` or `mcpServers` (Claude Code security rule). The plan's `dontAsk` would in any case have auto-denied the collector's Bash call. V1 instead pre-approves one command shape in the skill's `allowed-tools`: `Bash(python3 *collect-trace.py*)` and `Read`. The skill runs with `context: fork` in the `sherlock-holmes` agent, so `/trace-debug` and "use sherlock-holmes" both land in the same investigator with the same tools.

## D5. Truncation and provenance are first-class facts

The plan's central rule ("never fabricate") fails silently if the agent cannot tell "cut" from "absent". The collector reports `returned`, `truncated`, `max_lines`, pages, per-source status and error, and what bounded the window. The prompt cut ends with a TRUNCATION / GAPS block.

## D6. A trace missing from Tempo is `not_found`, never an error

Unsampled or unexported traces are normal (a caller sending `traceparent ...-00` is enough). The collector distinguishes 404 from failure, and the agent is told not to read it as retention or export trouble without evidence.

## D7. Offline fixtures and plugin evals

`--fixture <dir>` reads recorded `loki.json` / `tempo.json` instead of HTTP; `--dump-raw` records them. Six synthetic cases under `evals/` reproduce the plan's test matrix and are graded with `claude plugin eval` rubrics derived from each case's `expected.json`. The success criterion becomes measurable: origin correct, no fabricated cause, confidence and gaps present.

## D8. Priors file per project

Generic tooling produces generic hypotheses. Each installation can add `.claude/trace-debug/priors.md` with known non-anomalies. The skill reads it as team context, not as evidence.

## D9. Language

Plugin text in English (public artifact). The agent writes the report in the user's language.

## D10. Dropped or deferred from the plan

- `Grep`/`Glob` in the agent: not needed in V1 (no source code as evidence); smaller tool surface for a read-only agent.
- Source code correlation (V2), metrics (V3), deploys (V4), incident memory (V5): unchanged as roadmap. Memory, when it comes, should be a curated priors file or the team's knowledge base, never a substitute for current evidence.
- `.claude/agents` layout: replaced by the plugin layout (`agents/`, `skills/`, `evals/` at the plugin root).

## D11. Sandbox prerequisite documented, and discipline grader relaxed for split confidence

The eval suite's `Bash` grant runs under Claude Code's OS sandbox, which requires `bubblewrap` and
`socat`, and on Ubuntu 24.04+ also requires an AppArmor profile granting `bwrap` the `userns`
capability (the default policy blocks unprivileged user namespaces). Without it, `claude plugin
eval` refuses to run any Bash command unconfined and every case falls back to the agent reading
fixtures directly with `Read` - which still produces a correct report (validating the reasoning)
but does not exercise `collect-trace.py` (the actual collector). See `evals/README.md` for the
one-time fix (`/etc/apparmor.d/bwrap`) and the `collector-output-used` grader added to catch this
silently in the future: it fails a case whose final report says the collector could not run.

Once the sandbox worked, five of six cases scored 1.00 on the first full run with the collector
genuinely invoked. The sixth (`04-contradictory-logs`) scored 0.86 on `discipline` alone, even
though its report was arguably the best of the six: it named two labelled, evidence-weighed
hypotheses, refused to pick one, and split its confidence ("LOW for the cause, HIGH for the
localization"). The rubric asked for "a confidence level" in the singular and "at most three next
checks" without allowing a short closing remark after that list, so a genuinely disciplined,
non-committal answer read as non-compliant. Relaxed `discipline.md` (all six cases, identical
file) to explicitly accept a split, justified confidence and a non-actionable closing note. The
lesson generalises: when a grader and a good answer disagree, read the transcript before assuming
the agent is wrong.

## D12. Two wrong diagnoses of a failing grader, and what the data actually says

`04-contradictory-logs` fails the `discipline` grader. Three attempts to explain it, two of them
confidently wrong, are recorded here because the failure mode is the exact one this plugin exists
to prevent.

**Attempt 1 - "the judge is miscalibrated".** Reaction to a 6/6 FAIL: narrow the grader, drop its
weight to 0.5, add two regex graders verified against the six reports already in hand. That
verification was circular: a check confirmed to pass on every output available has not been shown
to fail anything. Weight management is score management, not quality management. Reverted.

**Attempt 2 - "the agent fabricates timestamps".** The failing reports corrected `checkout-service`
timestamps for ~3 s of clock skew and printed the corrected values in the timeline's primary
column, against the agent's own "never fabricate timestamps" rule. The correlation looked perfect:
5 passing cases without it, 1 failing case with it. It was confounded - case 04 is the only fixture
with clock skew, so *any* unique feature of its report would show the same perfect correlation.
Correlation mistaken for cause, which is precisely what section 2.3 of the original plan warns
against. The product fix was made anyway (it is correct on its own merits: recorded values stay
recorded, skew is reported as a measured offset), the agent complied fully on the next run, and
`discipline` still failed 3/3. Hypothesis dead.

**Attempt 3 - "too many next checks".** The latest failing report lists four numbered checks
against a rubric that allows three. But `03-error-only-in-tempo` passed `discipline` with four
items and an actionable closing remark, and case 04 has failed with three items and no remark. Not
the discriminator either.

**What the data does establish.** Cross-tabulating every stored run: under the default judge
(haiku, the 2026-09-18 runs) case 04 *passed* `discipline` with the original rubric. Under
`--judge-model sonnet` (every 2026-09-20 run) it has failed three times, 9 votes out of 9, across
three materially different reports and three different rubric versions. A stronger judge
consistently failing what a weaker one passed points at the judge seeing something, not inventing
it. Under sonnet the only feature that separates the failing runs from the twelve passing ones is
the scenario itself - the one case deliberately built so that the correct answer is "the evidence
cannot say which service is right".

`claude plugin eval` persists only the boolean vote, never the judge's reasoning, in neither
`aggregate-result.json` nor `report.html`. The rationale was therefore obtained by reproducing the
grading outside the harness: same judge model, same rubric text, same reply, asking for the
verdict *and* the criterion-by-criterion justification. That result is recorded in D13.

**Standing decision until D13 settles it:** the grader stays strict and at full weight. A gate that
fails is not evidence that the gate is wrong.

## D13. The answer: the cap of three next checks, enforced only by a strong judge

The grading was reproduced outside the harness - same judge model (sonnet), same rubric text, same
reply - asking for the verdict *and* a criterion-by-criterion justification, because
`claude plugin eval` persists only the boolean vote. Result: FAIL on exactly one criterion.
Criteria 1, 2, 3, 4 and the trailing clause all MET, with the judge explicitly confirming that the
timeline now carries only as-recorded values and that derived times sit in a separate block marked
"do not quote as evidence" (the D12 product fix landed and works). The single violation:

> "## Next checks (in priority order)" followed by four numbered items, against criterion 5's
> "It proposes at most three next checks".

Controlling for judge model - which the earlier analysis failed to do, and which is why the
"case 03 passed with four items" counterexample was worthless: that run was graded by haiku - all
three sonnet failures of case 04 share one cause in three disguises:

| Run | Form of the violation |
| --- | --- |
| 14:15 | three numbered items plus an actionable closing recommendation |
| 14:31 | three numbered items, the third bundling a second check behind "separately" |
| 14:43 | four numbered items outright |

The agent keeps exceeding the cap on exactly the richest scenario, which is where a cap is supposed
to bind. `04-contradictory-logs` offers three competing hypotheses, a clock-skew defect, an
error-swallowing defect and an open financial-reconciliation question - more than three things
worth doing, every time.

The cap itself is not arbitrary: it comes from the original plan ("Maximum 3 concrete checks") and
its purpose is to force prioritisation instead of handing over a backlog. So the fix is in the
product, not the rubric: `SKILL.md` gains protocol step 11 and a stricter report-format note, and
the agent's output rule now states that the three includes bundled actions and any recommendation
appended after the list, with instructions to rank and cut and to end on the third item.

Three hypotheses, two of them confidently wrong, one dismissed on a confounded counterexample. Each
error was the same shape: a feature that correlated perfectly with the failing case, in a
comparison that did not hold the other variables fixed. The plugin's own rule 2.3 - "correlation is
not causation, look for the mechanism" - would have caught all three. The mechanism here was
obtainable the whole time by simply asking a judge to explain itself.

**Confirmed.** Re-run of `04-contradictory-logs` after the product fix, same strict rubric, same
judge model: three items in "Next checks", nothing actionable after the list, no bundled second
check, `discipline` PASS 3/3, case score 1.00 with all eight graders green. The prediction was
made from an identified mechanism rather than a correlation, and it held.
