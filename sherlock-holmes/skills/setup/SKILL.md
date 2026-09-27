---
name: setup
description: Guided first-run setup for Sherlock Holmes, in one conversation - checks Grafana access with the doctor, discovers the Loki and Tempo datasource UIDs, proposes the Loki stream selector from the labels your Loki really has, hands over the exact /config values, optionally installs archify for the trace diagram, and verifies the result. Use right after installing the plugin, whenever the doctor reports a configuration problem, or when the user asks how to configure or set up Sherlock Holmes. Changes nothing in production.
argument-hint: "[--fixture]"
allowed-tools: Bash(python3 *collect-trace.py*) Bash(python3 *trace-diagram.py*) Read
---

# Setup

Take the user from "plugin installed" to "first investigation" with as little hunting as possible.
Everything below is read-only except one thing: installing archify, and only after the user says yes
to that exact command. You never write files, never print a credential and never put one on a command
line. Run the collector's `doctor` at most three times in a session.

`$ARGUMENTS` may contain `--fixture`: then run every doctor in fixture mode with
`--fixture "${CLAUDE_PLUGIN_ROOT}/evals/fixtures/01-downstream-503"` to demonstrate the flow without
a backend, and say so.

## 0. What this installation already has

- Grafana URL: `${user_config.grafana_url}`
- Loki datasource UID: `${user_config.grafana_loki_uid}`
- Tempo datasource UID: `${user_config.grafana_tempo_uid}`
- Loki selector: `${user_config.loki_selector}`
- Trace-id filter mode: `${user_config.loki_trace_filter}`
- Trace-id field: `${user_config.loki_trace_field}`

A blank value or an unreplaced `${...}` placeholder means "not set". Pass the set ones to the doctor
as `--grafana-url`, `--grafana-loki-uid`, `--grafana-tempo-uid`, `--selector`, `--trace-filter`,
`--trace-field`.

## 1. Doctor

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/trace-debug/scripts/collect-trace.py" doctor [config flags]
```

One command, exactly that shape. Read its JSON and branch on what it says:

- **Both sources `access: none`** ("not configured"): nothing is wired yet. Explain the two routes and
  let the user pick: through Grafana (`GRAFANA_URL` + `GRAFANA_TOKEN`, a service account with the
  Viewer role is enough - *Administration → Users and access → Service accounts*), or directly
  (`LOKI_URL`/`TEMPO_URL` with `LOKI_TOKEN`/`TEMPO_TOKEN` or basic auth). The token is the one value
  the install dialog cannot carry: it must be exported in the shell that launches Claude Code, and
  Claude Code restarted so the collector inherits it. Give the exact `export` lines with placeholders,
  never with a value you invented, and end this step with: restart, then run `/sherlock-holmes:setup`
  again.
- **HTTP 401 / 403**: the credential is wrong, expired or lacks the Viewer role. Name the variable to
  fix. Do not guess at datasources.
- **`grafana_datasources` present**: the URL and token work. Pick the Loki and the Tempo entries. One of
  each: take them. Several: show the list and ask which belong to the environment being investigated.
- **Loki `check: ok` with `labels_sample`**: propose the selector from labels that really exist there.
  Prefer, in this order, `namespace`, `env`/`environment`, `cluster`, `app`/`service_name`. A selector
  needs a value, and the doctor lists only label names: ask the user for the value (for example
  `{namespace="prod"}`), never invent one, and never propose `{}` - the collector refuses it.
- **Trace-id filter**: keep `substring` unless the user says their logs are JSON with a trace-id field
  or use Loki 3 structured metadata; then propose `json` or `metadata` and ask for the field name
  (`trace_id`, `traceId`, `traceID` are the common ones).

## 2. Hand over the configuration

Give the values two ways, ready to paste, and say that either works (the collector reads `/config`
values passed by the skill first, environment variables second):

1. For `/config` → Plugins → Sherlock Holmes: one line per field, `grafana_url`, `grafana_loki_uid`,
   `grafana_tempo_uid`, `loki_selector`, `loki_trace_filter`, `loki_trace_field`.
2. As environment variables, for people who keep everything in the shell: `GRAFANA_URL`,
   `GRAFANA_LOKI_UID`, `GRAFANA_TEMPO_UID`, `LOKI_SELECTOR`, `LOKI_TRACE_FILTER`, `LOKI_TRACE_FIELD`.

Neither the skill nor you can write these settings: that is the user's step, by design of the plugin
system. Say so in one sentence.

## 3. Diagram (optional)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/trace-debug/scripts/trace-diagram.py" doctor
```

- `diagram: ok`: say so and move on.
- `node not found` or a Node older than 18: the diagram needs Node 18+. Say it, do not install Node,
  and move on; everything else works without the diagram.
- `archify not found`: explain in two lines what the diagram is (the trace as a self-contained
  interactive HTML, evidence only, delivered with every investigation that has a trace in Tempo) and
  **ask** whether to install archify now with exactly:

  ```bash
  npx skills add tt-a1i/archify -g
  ```

  Only after an explicit yes run that command. It is not pre-approved on purpose: the permission
  prompt the user sees is the consent. Mention that it also installs a companion skill,
  `archify-review`, which this plugin does not use. Then run the diagram doctor again and report the
  path it found. If the user says no, or later, tell them the command and that `Run the trace-debug
  doctor` will show `diagram: available` once it is done.

## 4. First investigation

- Backend configured and verified: suggest `/sherlock-holmes:trace-debug <trace-id>` with a trace the
  user already understands, so they can judge the report.
- Nothing configured yet, or the user wants to see it first: suggest, verbatim,
  *Investigate trace `2aa803b2e40c97a2490d754a465fe9de` using the bundled fixtures for
  `01-downstream-503`* - recorded Loki and Tempo responses, no backend needed.

## 5. Close

End with a short checklist in the user's language: what is verified (sources, UIDs, selector,
diagram) and what is still on the user (exports and restart, `/config` values). Nothing else: no
speculation about their system, no changes proposed to it.
