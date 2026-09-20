# Trace-debug priors (example)

Copy to `.claude/trace-debug/priors.md` in your project and edit. The agent reads this before forming
hypotheses and treats it as team context, not as evidence about the trace under investigation.

## Known non-anomalies
- `inventory-service` retries DB connections 3 times with 200 ms backoff; three consecutive timeouts are one failure, not three.
- A single ~50 ms `DynamoDB.GetItem` with a nested `tls.connect` span is a cold connection, not a problem.
- `GET /health` lines from the load balancer appear every 5 s in every service.

## Sampling and export
- Ingestion traffic arrives with `traceparent ...-00` and is never exported: a Tempo 404 for an ingestion trace is expected. Logs are the only record.
- Query traffic is always sampled: a Tempo 404 for a query trace is worth escalating.

## Log conventions
- Level field is `severity`, message field is `msg`, trace id field is `trace_id` (hex), span id field is `span_id`.
- Business events are named `evento.<dominio>.<acao>` and carry `eventId`.

## Dependencies that time out by design
- `provider-x` has a 2 s budget; on timeout the service returns 202 and retries asynchronously. A 202 is degraded, not failed.
