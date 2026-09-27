# Trace investigation

**Trace:** 2aa803b2e40c97a2490d754a465fe9de
**Services:** api -> payment-service -> customer-service -> PostgreSQL (db.system=postgresql, called by customer-service)
**Sources:** tempo=found loki=9 lines, window 2026-09-17T14:02:01.000Z-2026-09-17T14:03:03.180Z (bounded by tempo).
**Diagram:** diagram: generated /tmp/trace-debug/2aa803b2e40c97a2490d754a465fe9de.html (6 messages; quality showcase; evidence only: spans as recorded, no causal claim)

## Diagnosis
A PostgreSQL query in customer-service timed out after 2000 ms. customer-service answered with HTTP 503 ("database unavailable"), payment-service turned that into a 502, and the api returned a 500 to the client for `POST /payment`. The failure started in the deepest span (`customer-service SELECT customer`). Every layer above it only passed the error along.

## Timeline
- `14:02:31.000 [api] INFO POST /payment`
- `14:02:31.008 [payment-service] INFO POST /charge`
- `14:02:31.020 [payment-service] INFO calling customer-service GET /customers/42`
  …

## Causal chain
1. `customer-service SELECT customer` connection timeout after 2000ms -> `customer-service GET /customers/42` returns 503 "database unavailable". Evidence: parent/child span plus propagated status.
2. customer-service 503 -> `payment-service GET customer-service/customers/42` CLIENT span HTTP 503. Evidence: parent/child span (CLIENT -> SERVER) plus propagated status.
3. payment-service CLIENT 503 -> `payment-service POST /charge` returns 502. Evidence: explicit "upstream returned" line: `upstream customer-service returned 503`.
4. payment-service 502 -> `api POST payment-service/charge` CLIENT span HTTP 502. Evidence: parent/child span plus propagated status.
5. api CLIENT 502 -> `api POST /payment` returns 500. Evidence: explicit "upstream returned" line in the span status: `upstream failure: payment-service returned 502`.

## Probable cause
FACT: customer-service could not get a connection to PostgreSQL within its 2000 ms timeout while loading customer 42. The request failed at every layer because of that.
INFERENCE: the error was propagated, not produced locally by payment-service or api. Neither of them shows any error of its own before the upstream status arrived.
HYPOTHESIS, unproven: the connection timeout came from one of three things. The database was unreachable or overloaded, customer-service's connection pool was saturated, or there was a network problem between customer-service and PostgreSQL. This trace does not tell these apart. The only fact is "connection timeout after 2000ms".

## Confidence
HIGH for where the failure started and how it propagated; LOW for why PostgreSQL did not accept the connection.
The span tree and the logs agree on every link from the DB timeout to the 500, but nothing in this trace describes the state of the database, the connection pool or the network.