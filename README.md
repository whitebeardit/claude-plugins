# Whitebeard plugins

A [Claude Code](https://claude.com/claude-code) plugin marketplace.

```
/plugin marketplace add whitebeardit/claude-plugins
/plugin install sherlock-holmes@whitebeard-plugins
```

## Plugins

### Sherlock Holmes

Evidence-first incident investigation by trace ID, using **Grafana Loki** logs and **Grafana Tempo** traces. Give it a trace ID; it rebuilds the timeline across services, finds the first anomalous event, separates cause from consequence, and tells you how confident it is and what the evidence cannot show.

```
/sherlock-holmes:trace-debug 4bf92f3577b34da6a3ce929d0e0e4736
```

It is read-only by construction: the collector only issues HTTP GET, the agent has no write tools, and credentials are read from the environment and never printed. It works against any Loki and any Tempo, directly or through Grafana's datasource proxy.

The rule it is built around: **never prefer a convincing story to incomplete evidence.** "There is not enough evidence to determine the root cause" is a valid answer, and the agent is built to give it.

See [`sherlock-holmes/README.md`](sherlock-holmes/README.md) for setup, configuration and the full report format, and [`sherlock-holmes/docs/design-decisions.md`](sherlock-holmes/docs/design-decisions.md) for why it is built the way it is.

## Repository layout

```
.claude-plugin/marketplace.json   the marketplace manifest
sherlock-holmes/                  one directory per plugin
```

## Development

Each plugin carries its own tests and eval suite:

```bash
cd sherlock-holmes
python3 -m unittest discover -s tests -v
claude plugin validate .
claude plugin eval . --scaffold --no-publish --ablation none --judge-model sonnet \
  --allow-tools "Bash(python3 *collect-trace.py*)"
```

The eval suite runs fully offline against recorded Loki and Tempo responses, so it needs no live backend. It does need an OS sandbox for Bash; see [`sherlock-holmes/evals/README.md`](sherlock-holmes/evals/README.md) for the prerequisites.

### Pre-push gate

Verification runs locally, before a push, rather than in CI, so that no Claude Code credential has to live in a repository secret and the eval spend stays visible to whoever triggers it. Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

On every push the hook validates each plugin manifest, runs the unit tests and checks that regenerating the fixtures leaves the tree unchanged. That part is free and takes a couple of seconds.

The paid eval suite runs only when the push touches a plugin's `agents/`, `skills/`, `evals/` or `tests/` — the files that can change behaviour or change how it is graded. Pushing a README or a doc does not trigger it.

| Variable | Effect |
| --- | --- |
| `SKIP_EVALS=1` | Skip the eval suite for this push. The cheap checks still run. |
| `FORCE_EVALS=1` | Run the eval suite for every plugin, whatever changed. |
| `EVAL_BUDGET_USD` | Hard cost ceiling passed to `--max-cost-usd`. Default 8. |

Use `SKIP_EVALS=1` rather than `git push --no-verify`: it keeps the free checks running instead of disabling the whole gate.

## License

MIT. See [LICENSE](LICENSE).
