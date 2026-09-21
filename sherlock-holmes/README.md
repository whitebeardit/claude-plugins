# Sherlock Holmes

Evidence-first incident investigation by trace ID, for Claude Code. Give it a trace ID; it collects the trace from **Grafana Tempo** and the log lines from **Grafana Loki**, rebuilds the timeline, finds the first anomalous event, separates cause from consequence, and tells you how confident it is and what the evidence cannot show.

It is **read-only**. It never modifies production, dashboards, alerts or code.

```
/sherlock-holmes:trace-debug 4bf92f3577b34da6a3ce929d0e0e4736
```

## What you get

```
# Trace investigation
**Trace:** ...  **Services:** api -> payment-service -> customer-service
**Sources:** tempo=found loki=9 lines, window 14:02:01-14:03:03 (bounded by tempo)

## Diagnosis            two to four sentences
## Timeline             the lines that matter, gaps noted
## First anomalous event
## Causal chain         first anomaly -> ... -> final symptom, one evidence type per link
## Probable cause       labelled FACT / INFERENCE / HYPOTHESIS
## Confidence           HIGH | MEDIUM | LOW, one sentence why
## Evidence
## What the evidence does not show
## Next checks          at most three
```

The core rule: **never prefer a convincing story to incomplete evidence.** "There is not enough evidence to determine the root cause" is a valid answer, and the agent is built to give it.

## How it works

```
trace id
   |
   v
collect-trace.py  (deterministic, read-only, stdlib Python)
   |  1. Tempo  GET /api/traces/<id>      -> span tree, bounds the time window
   |  2. Loki   query_range |= "<id>"     -> log lines inside [start-pad, end+pad]
   |  3. normalise, join logs<->spans, dedupe, mark truncation, derive facts
   v
compact "prompt cut" + full JSON on disk
   |
   v
sherlock-holmes agent (forked subagent, opus)
   |  timeline -> first anomaly -> hypotheses -> elimination -> causal chain
   v
report with confidence and gaps
```

Three files do the work:

| File | Role |
| --- | --- |
| `skills/trace-debug/scripts/collect-trace.py` | Collects facts. Never decides a cause. |
| `skills/trace-debug/SKILL.md` | The procedure: how to collect, the investigation protocol, the report format. |
| `agents/sherlock-holmes.md` | The investigator: identity, rules, prohibitions, confidence rubric. |

Why Tempo first? A trace ID carries no timestamp. Looking a trace up by ID in Tempo is indexed and cheap, and it returns the exact start and end, so the Loki query can be tight instead of scanning hours of every stream. The agent still *reads* logs first; only the collection order is Tempo-first. When Tempo does not have the trace (unsampled, unexported, expired), the collector says so and falls back to a lookback window, or to `--around` / `--start` `--end` if you know roughly when it happened.

## Install

Requirements: Claude Code and Python 3.8+. A Loki and a Tempo are needed to investigate real
traces, but not to try the plugin out, which is covered below.

```
/plugin marketplace add whitebeardit/claude-plugins
/plugin install sherlock-holmes@whitebeard-plugins
```

If the install summary says `Run /reload-plugins to activate.`, do that.

To hack on the plugin instead of installing it, clone the repository and load it for one session
with `claude --plugin-dir ./claude-plugins/sherlock-holmes`.

## Try it without a Loki or a Tempo

The plugin ships the recorded Loki and Tempo responses for six failure scenarios, so you can see a
full investigation before wiring it to anything. Ask Claude, in a session where the plugin is
installed:

> Investigate trace `2aa803b2e40c97a2490d754a465fe9de` using the bundled fixtures for
> `01-downstream-503`.

The skill knows where those fixtures live inside the plugin, so you don't need to know where it was
installed or which directory you are in. The six scenarios, each a different shape of failure:

| Scenario | Trace ID | What it exercises |
| --- | --- | --- |
| `01-downstream-503` | `2aa803b2e40c97a2490d754a465fe9de` | A database timeout four services deep, surfacing as a 500 at the edge |
| `02-db-timeout-retries` | `df34d418bee785c2a1d46b9539d0ccc2` | Retries of one failure, and no trace in Tempo at all |
| `03-error-only-in-tempo` | `c9495e68967f71fcc212c8f6d80597b1` | Logs say only "request failed"; the cause is visible solely in the spans |
| `04-contradictory-logs` | `59d23822d96610672a554274570b5bd7` | Two services disagree about the same outcome, and a clock is skewed |
| `05-incomplete-trace` | `3d31213bebd065c9c19e80638a071b36` | A service with no logs and no spans at all |
| `06-intermediate-service-error` | `84c8614b9b6df5d3565fbdb3f2174e95` | The failure is in the middle of the chain; the downstream is healthy |

All fixture data is synthetic. Cases 04 and 05 are the interesting ones to judge it on, because the
correct answer there is partly "the evidence cannot say".

## Configure

Credentials live in environment variables, never in the plugin. Two access modes, auto-detected per source (direct wins when both are set):

**Through Grafana** (one token, two datasource UIDs; works with Grafana Cloud and self-hosted):

```bash
export GRAFANA_URL=https://your-stack.grafana.net
export GRAFANA_TOKEN=glsa_...            # or GRAFANA_SERVICE_ACCOUNT_TOKEN, or GRAFANA_USERNAME + GRAFANA_PASSWORD
export GRAFANA_LOKI_UID=grafanacloud-logs
export GRAFANA_TEMPO_UID=grafanacloud-traces
export LOKI_SELECTOR='{env="prod"}'
```

**Direct** (Loki and Tempo URLs; no Grafana needed):

```bash
export LOKI_URL=https://loki.example.com          # + LOKI_TOKEN, or LOKI_USERNAME + LOKI_PASSWORD, optional LOKI_ORG_ID
export TEMPO_URL=https://tempo.example.com        # + TEMPO_TOKEN, or TEMPO_USERNAME + TEMPO_PASSWORD, optional TEMPO_ORG_ID
export LOKI_SELECTOR='{cluster="prod", namespace="shop"}'
```

Grafana Cloud direct access uses basic auth: username is the numeric instance ID, password is an access policy token. The Grafana Cloud Tempo URL includes `/tempo`.

Tuning, all optional:

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOKI_SELECTOR` | required | Stream selector. Never `{}`: the collector refuses to scan everything. |
| `LOKI_TRACE_FILTER` | `substring` | `substring` = `\|= "<id>"` (works with any log format). `metadata` = `\| trace_id="<id>"` for Loki 3 structured metadata. `json` = `\| json \| trace_id="<id>"`. |
| `LOKI_TRACE_FIELD` | `trace_id` | Field name for the `metadata` and `json` modes. |
| `LOKI_SERVICE_LABELS` | `service_name,service,app,container,k8s_container_name,job` | Labels tried, in order, to name the service. Falls back to JSON fields in the line. |
| `TEMPO_API` | `v1` | `v1` = `/api/traces/<id>` (every Tempo version). `v2` = `/api/v2/traces/<id>`. |
| `HTTP_TIMEOUT` | `20` | Seconds per request. |

### First run against a real stack

Work up in three steps, checking after each. Each one tells you what the next needs.

**1. Point it at Grafana and find your datasources.** Create a service account token in Grafana
(*Administration → Users and access → Service accounts*, Viewer role is enough — the plugin only
reads), then:

```bash
export GRAFANA_URL=https://your-stack.grafana.net    # or your self-hosted Grafana
export GRAFANA_TOKEN=glsa_...
python3 .../collect-trace.py doctor
```

With the URL and token alone, `doctor` lists every Loki and Tempo datasource it can see, with their
UIDs. You don't have to hunt for them in the UI.

**2. Set the UIDs and find your log labels.**

```bash
export GRAFANA_LOKI_UID=<uid from step 1>
export GRAFANA_TEMPO_UID=<uid from step 1>
```

`doctor` now returns `check: ok` for both sources and a sample of the label names your Loki actually
uses. That sample is what you build the selector from — don't copy `{env="prod"}` from this README,
because your labels are almost certainly different.

**3. Set the selector, then confirm with a trace you already know.**

```bash
export LOKI_SELECTOR='{namespace="your-namespace"}'   # from the labels in step 2
```

Take a trace id from a request you understand and investigate it. If Tempo returns the trace but
Loki returns zero lines, the selector or the trace-id field is wrong, not the plugin: check
`LOKI_TRACE_FILTER` (`substring` works with any log format; use `metadata` for Loki 3 structured
metadata, or `json` for JSON logs) and `LOKI_TRACE_FIELD` (default `trace_id`; some stacks use
`traceId` or `traceID`).

Two things worth knowing before you blame the tool. Not every trace reaches Tempo: a caller that
sends `traceparent … -00` is never sampled, so a 404 there can be correct behaviour rather than a
retention problem. And some stacks do not ship application logs to Loki at all, in which case the
plugin will reconstruct the span tree and tell you, honestly, that it has no log narrative.

Check the wiring before the first investigation by asking Claude:

> Run the trace-debug doctor.

It reports the access mode per source, whether credentials are present, a labels sample from Loki
and an echo from Tempo. It never prints a token. Set the variables in the shell that launches
Claude Code, since the collector reads them from the environment it inherits.

To run it yourself against a clone, it is `python3 skills/trace-debug/scripts/collect-trace.py
doctor` from the plugin directory. An installed copy lives under
`~/.claude/plugins/cache/whitebeard-plugins/sherlock-holmes/<version>/`, but asking Claude avoids
having to find it.

## Use

```
/sherlock-holmes:trace-debug <trace-id>
/sherlock-holmes:trace-debug <trace-id> --around 2026-09-17T14:02:00Z
/sherlock-holmes:trace-debug 00-4bf92f35...-00f067aa0ba902b7-01      # a traceparent works too
```

Or ask in plain words: "investigate trace 4bf9..." and Claude delegates to the `sherlock-holmes` agent.

Flags after the trace id go to the collector unchanged: `--around <time>`, `--start`/`--end`, `--lookback 2h` (default 24h, used only when Tempo cannot bound the window), `--pad 30s`, `--fixture <dir>`.

**Project priors.** Put a `.claude/trace-debug/priors.md` in your project with the behaviours that are normal for your system: expected retries, known noisy lines, sampling rules ("ingestion traces are never sampled"), dependencies that time out by design. The agent reads it before forming hypotheses and treats it as team context, not as evidence about the trace.

**Permissions.** The skill pre-approves exactly one command shape, `python3 *collect-trace.py*`, plus `Read`. If your permission mode still prompts, allow that pattern in your settings. The agent has no other tools.

## Semantics the agent relies on

- **Truncation is explicit.** The JSON carries `returned`, `truncated`, `max_lines`, and the cut says "absence of later lines is NOT evidence". Default cap 5000 lines, paginated 1000 per request.
- **`not_found` in Tempo is not an error.** Unsampled traces are normal. The cut says the tree was unavailable; the agent reasons from logs.
- **Window provenance.** Every run says what bounded the window: `tempo`, `explicit`, `around` or `now`. A `now` window on an old incident is called out.
- **Facts, not verdicts.** The collector lists the earliest error log line, the errored span that ended first, and the deepest errored span. Those are starting points; the agent decides what is cause and what is consequence.
- **Logs and spans are joined by span id** when the log line carries one, so a line reads `<span customer-service:SELECT customer>`.
- **Repeated lines** are collapsed with `(xN)`. Messages are capped, tokens redacted.

## Optional: Grafana MCP

The collector does not need the Grafana MCP server. If you already run it, the agent can be given its read-only tools for follow-up checks (TraceQL search when you have no trace id, error patterns, metrics), but the timeline always comes from the collector. Do not give the agent the MCP's write tools.

## Test

```bash
python3 -m unittest discover -s tests -v                # collector, 20 tests, no network
python3 skills/trace-debug/scripts/collect-trace.py \
  --trace-id 2aa803b2e40c97a2490d754a465fe9de --fixture evals/fixtures/01-downstream-503
claude plugin validate .
claude plugin eval . --scaffold --allow-tools "Bash(python3 *collect-trace.py*)" --ablation none --judge-model sonnet
```

See `evals/README.md` for the sandbox prerequisites (bubblewrap needs unprivileged user namespaces; Ubuntu 24.04 restricts them by default).

`evals/` has six offline cases mirroring the classic failure shapes: downstream 503 chain, DB timeout with retries and no trace in Tempo, error visible only in spans, contradictory logs with clock skew, incomplete trace, error in an intermediate service. Each case seeds recorded Loki/Tempo responses into the workspace and grades the report with rubrics derived from `expected.json`. All fixture data is synthetic.

To record fixtures from a real incident: `collect-trace.py --trace-id <id> --dump-raw ./some-dir`. Review the dump for personal data before committing it.

## Security

- Read-only by construction: the collector only issues HTTP GET; the agent has `Bash` (pre-approved for the collector only) and `Read`.
- Credentials come from the environment and are never printed, not even in error messages.
- Trace ids are validated (16 or 32 hex chars, or a traceparent) before touching a query, which is also what prevents LogQL injection.
- The collector refuses to run without a Loki selector.

## Roadmap

- V1 (this): Loki + Tempo, timeline, first anomaly, causal chain, confidence, gaps, offline evals.
- V2: source code as complementary evidence (stack trace -> file:line), TraceQL search when no trace id is known.
- V3: metrics around the window (error rate, saturation, pool usage).
- V4: deploy correlation.

## License

MIT
