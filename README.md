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

The free half of that hook also runs in GitHub Actions (`.github/workflows/verify.yml`), so anything arriving by another route — a web edit, a pull request from a fork, a clone that never enabled the hook — is still checked. It needs no credentials.

### Releasing

Versions are managed by [release-please](https://github.com/googleapis/release-please) from [Conventional Commits](https://www.conventionalcommits.org/), one independently versioned release per plugin.

Push to `main` with a `feat:` or `fix:` commit and the action opens a Release PR for the affected plugin. Merging it bumps the version, writes the changelog, tags the commit and publishes a GitHub Release.

| Commit prefix | Version bump |
| --- | --- |
| `fix:` | patch |
| `feat:` | minor |
| `feat!:` or a `BREAKING CHANGE:` footer | major |
| `docs:`, `ci:`, `chore:`, `refactor:`, `test:` | none |

Tags are per plugin, in the form `sherlock-holmes-v0.2.0`, so one plugin releasing never moves another. That form is also what an external directory pins:

```json
{ "source": "git-subdir",
  "url": "https://github.com/whitebeardit/claude-plugins.git",
  "path": "sherlock-holmes", "ref": "sherlock-holmes-v0.2.0", "sha": "…" }
```

**The version lives in one place only:** each plugin's `.claude-plugin/plugin.json`. It is deliberately absent from the marketplace entry, because Claude Code always prefers the `plugin.json` value, so a version in both is a way for a stale one to silently mask the real one. `verify.yml` fails the build if a version reappears in a marketplace entry, or if `plugin.json` and `.release-please-manifest.json` disagree.

Behavioural verification is not part of the release job: the eval suite has already run locally, before the commits were pushed.

### Submitting to the community marketplace

Anthropic runs two public marketplaces. `claude-plugins-official` is curated at Anthropic's discretion and has no application process. `claude-community` is where third-party submissions land after review, and is the one to submit to, through [the claude.ai form](https://claude.ai/admin-settings/directory/submissions/plugins/new) (Team or Enterprise organisations with directory management access) or [the Console form](https://platform.claude.com/plugins/submit) (individual authors).

Run `claude plugin validate ./sherlock-holmes --strict` before submitting; the review pipeline runs the same check alongside automated safety screening. Approved plugins are pinned to a commit SHA in the community catalog, and their CI bumps the pin as new commits land, so no tag or SHA has to be supplied by hand.

## License

MIT. See [LICENSE](LICENSE).
