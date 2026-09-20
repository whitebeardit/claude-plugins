---
name: 04-contradictory-logs
description: "gateway-adapter says approved, payment-service says declined, checkout says confirmed, checkout clock skewed"
tags: [trace-debug, offline]
runs: 1
max_turns: 40
timeout_seconds: 600
allowed_tools: [Read, Skill, Agent]
expected_outcome: "the report explicitly points out the contradiction between 'charge approved' (gateway-adapter), 'charge declined by gateway (code 51)' (payment-service) and 'payment confirmed' (checkout-service) and/or the ~3 s clock skew of checkout-service, gives LOW confidence, and does NOT assert as fact either that the payment was declined or that it succeeded."
---

We had an incident in production. Investigate trace `59d23822d96610672a554274570b5bd7` and tell me what happened, where it started, how it propagated and how confident you are.

Loki and Tempo are not reachable from this machine: the recorded API responses for this trace are in `./fixtures` (use them instead of the live services).

Reply with the full investigation report, not a summary of it.
