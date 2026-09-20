---
name: 03-error-only-in-tempo
description: "logs only say 'request failed'; the S3 AccessDenied is visible only in the span tree"
tags: [trace-debug, offline]
runs: 1
max_turns: 40
timeout_seconds: 600
allowed_tools: [Read, Skill, Agent]
expected_outcome: "the report attributes the failure to the S3 GetObject AccessDenied (not authorized to perform s3:GetObject) inside report-service, states that cache-service succeeded, and relates the api 500 to it as a consequence."
---

We had an incident in production. Investigate trace `c9495e68967f71fcc212c8f6d80597b1` and tell me what happened, where it started, how it propagated and how confident you are.

Loki and Tempo are not reachable from this machine: the recorded API responses for this trace are in `./fixtures` (use them instead of the live services).

Reply with the full investigation report, not a summary of it.
