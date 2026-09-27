#!/usr/bin/env python3
"""Regenerate the recorded responses for the error-sweep fixtures. Deterministic, synthetic data only.

  cascade   three services failing together (a DB timeout surfacing as 503 -> 502 -> 500) plus one
            unrelated error; Tempo and Loki. Its most recent trace is 2aa803b2e40c97a2490d754a465fe9de,
            the trace recorded in evals/fixtures/01-downstream-503, so "diagram 1" works offline.
  timeouts  one outbound call timing out at 30 s, three times; Tempo only (a service whose logs are
            not in Loki).
  silence   no errors at all.
"""
import base64
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
S = 1_000_000_000


def ns(hms: str, day="2026-09-17") -> int:
    from datetime import datetime, timezone
    return int(datetime.fromisoformat(f"{day}T{hms}+00:00").replace(tzinfo=timezone.utc).timestamp() * S)


def attr(k, v):
    return {"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": str(v)}}


def span(sid, name, t0, dur_ms, **attrs):
    a = [attr("status", "error")] + [attr(k.replace("__", "."), v) for k, v in attrs.items()]
    return {"spanID": sid, "name": name, "startTimeUnixNano": str(t0), "durationNanos": str(int(dur_ms * 1e6)), "attributes": a}


def trace(tid, t0, spans, root="api"):
    return {"traceID": tid, "rootServiceName": root, "startTimeUnixNano": str(t0), "durationMs": 2180,
            "spanSets": [{"spans": spans, "matched": len(spans)}], "serviceStats": {}}


def cascade_trace(tid, t0, customer):
    sp = lambda o: t0 + int(o * 1e6)
    return trace(tid, t0, [
        span("a1", "POST /payment", sp(0), 2180, service__name="api", kind="server", http__status_code=500),
        span("a2", "POST payment-service/charge", sp(5), 2165, service__name="api", kind="client",
             http__status_code=502, peer__service="payment-service"),
        span("b1", "POST /charge", sp(8), 2157, service__name="payment-service", kind="server", http__status_code=502,
             statusMessage="upstream customer-service returned 503"),
        span("b2", f"GET customer-service/customers/{customer}", sp(20), 2130, service__name="payment-service",
             kind="client", http__status_code=503, peer__service="customer-service"),
        span("c1", f"GET /customers/{customer}", sp(24), 2121, service__name="customer-service", kind="server",
             http__status_code=503, statusMessage="database unavailable"),
        span("c2", "SELECT customer", sp(30), 2002, service__name="customer-service", kind="client",
             db__system="postgresql", statusMessage=f"connection timeout after 2000ms (pool 10/10, customer {customer})"),
    ])


def loki(streams):
    return {"status": "success", "data": {"resultType": "streams", "result": [
        {"stream": labels, "values": [[str(t), json.dumps(line)] for t, line in values]} for labels, values in streams]}}


def write(case, window, tempo=None, loki_doc=None):
    d = HERE / case
    d.mkdir(exist_ok=True)
    (d / "window.json").write_text(json.dumps(window, indent=1) + "\n")
    for name, doc in (("tempo-search.json", tempo), ("loki.json", loki_doc)):
        if doc is not None:
            (d / name).write_text(json.dumps(doc, indent=1) + "\n")


# cascade -------------------------------------------------------------------
ids = ["5b1e0c2a9f7d4e3b8a6c1d2e3f4a5b6c", "6c2f1d3b0a8e5f4c9b7d2e3f4a5b6c7d", "7d3a2e4c1b9f6a5d0c8e3f4a5b6c7d8e",
       "2aa803b2e40c97a2490d754a465fe9de"]
times = ["13:10:04", "13:25:47", "13:40:12", "14:02:31"]
customers = [7, 19, 33, 42]
cascade = [cascade_trace(t, ns(h), c) for t, h, c in zip(ids, times, customers)]
cascade.append(trace("0e1f2a3b4c5d6e7f8091a2b3c4d5e6f7", ns("13:51:09"), [
    span("d1", "GET /stock/88123", ns("13:51:09"), 14, service__name="inventory-service", kind="server",
         http__status_code=500, statusMessage="sku 88123 not found in warehouse 3")], root="inventory-service"))
cascade_loki = []
for tid, h, c in zip(ids, times, customers):
    t = ns(h)
    cascade_loki += [
        ({"service_name": "customer-service"}, [(t + 2_031_000_000, {"level": "error", "msg": f"PostgreSQL connection timeout after 2000ms (customer {c})", "trace_id": tid})]),
        ({"service_name": "payment-service"}, [(t + 2_160_000_000, {"level": "error", "msg": "upstream customer-service returned 503", "trace_id": tid}),
                                                (t + 2_161_000_000, {"level": "info", "msg": "charge failed, will not retry after error", "trace_id": tid})]),
        ({"service_name": "api"}, [(t + 2_178_000_000, {"level": "error", "msg": f"HTTP 500 for POST /payment request_id=af{c:02d}cd3490ab cpf=123.456.789-{c:02d}", "trace_id": tid})]),
    ]
write("cascade", {"start": "2026-09-17T13:00:00Z", "end": "2026-09-17T14:10:00Z", "source": "all"},
      {"traces": cascade, "metrics": {"completedJobs": 12, "totalJobs": 12}}, loki(cascade_loki))

# timeouts ------------------------------------------------------------------
tt = []
for i, (tid, day, h) in enumerate([("4c6d267f572b39fd37c4461da9948f0c", "2026-09-21", "00:12:39"),
                                   ("54c13ffdf5dbc353f82b8493c0576953", "2026-09-23", "13:59:15"),
                                   ("6705bd940faa23c5b097e8bf4f980931", "2026-09-25", "00:36:24")]):
    t = ns(h, day)
    tt.append(trace(tid, t, [span(f"e{i}", "POST", t + 800_000_000, 30_001, service__name="enrichment-service",
                                  kind="client", http__status_code="nil", net__peer__name="provider-x.example.com",
                                  statusMessage="The operation was aborted due to timeout")], root="<root span not yet received>"))
write("timeouts", {"start": "2026-09-20T00:00:00Z", "end": "2026-09-27T00:00:00Z", "source": "tempo"},
      {"traces": tt, "metrics": {"completedJobs": 384, "totalJobs": 384}})

# silence -------------------------------------------------------------------
write("silence", {"start": "2026-09-17T13:00:00Z", "end": "2026-09-17T14:00:00Z", "source": "all"},
      {"traces": [], "metrics": {"completedJobs": 12, "totalJobs": 12}}, loki([]))
print("fixtures written:", sorted(p.name for p in HERE.iterdir() if p.is_dir()))
