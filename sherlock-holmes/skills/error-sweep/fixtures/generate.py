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


# full traces (window map) ------------------------------------------------------
# The map needs every span of the sampled traces, not only the error spans the search returns, so
# each window fixture carries traces/<trace id>/tempo.json in Tempo's OTLP JSON. Built here from a
# compact span list; the cascade's newest trace is copied byte for byte from evals/fixtures/01.
import hashlib
import shutil

KINDS = {"server": "SPAN_KIND_SERVER", "client": "SPAN_KIND_CLIENT", "producer": "SPAN_KIND_PRODUCER",
         "consumer": "SPAN_KIND_CONSUMER", "internal": "SPAN_KIND_INTERNAL"}


def sid(tid, key):
    return hashlib.sha256(f"{tid}:{key}".encode()).hexdigest()[:16]


def b64(h):
    return base64.b64encode(bytes.fromhex(h)).decode()


def otlp(tid, spans):
    """spans: (key, parent_key|None, service, name, kind, start_ns, end_ns, attrs dict, error, message)."""
    by_service = {}
    for key, parent, svc, name, kind, t0, t1, attrs, error, message in spans:
        span = {"traceId": b64(tid), "spanId": b64(sid(tid, key)), "name": name, "kind": KINDS[kind],
                "startTimeUnixNano": str(t0), "endTimeUnixNano": str(t1),
                "attributes": [attr(k, v) for k, v in attrs.items()],
                "status": {"code": "STATUS_CODE_ERROR", "message": message} if error else {"code": "STATUS_CODE_UNSET"}}
        if parent:
            span["parentSpanId"] = b64(sid(tid, parent))
        by_service.setdefault(svc, []).append(span)
    return {"batches": [{"resource": {"attributes": [attr("service.name", svc)]},
                         "scopeSpans": [{"scope": {"name": "fixture"}, "spans": sp}]} for svc, sp in by_service.items()]}


def write_trace(case, tid, doc):
    d = HERE / case / "traces" / tid
    d.mkdir(parents=True, exist_ok=True)
    (d / "tempo.json").write_text(json.dumps(doc, indent=1) + "\n")


def cascade_full(tid, t0, customer):
    ms = lambda o: t0 + int(o * 1e6)
    return otlp(tid, [
        ("a1", None, "api", "POST /payment", "server", ms(0), ms(2180), {"http.method": "POST", "http.route": "/payment", "http.status_code": 500}, True, "upstream failure: payment-service returned 502"),
        ("a2", "a1", "api", "POST payment-service/charge", "client", ms(5), ms(2170), {"http.method": "POST", "http.status_code": 502, "peer.service": "payment-service"}, True, "HTTP 502"),
        ("b1", "a2", "payment-service", "POST /charge", "server", ms(8), ms(2165), {"http.method": "POST", "http.route": "/charge", "http.status_code": 502}, True, "upstream customer-service returned 503"),
        ("b2", "b1", "payment-service", f"GET customer-service/customers/{customer}", "client", ms(20), ms(2150), {"http.method": "GET", "http.status_code": 503, "peer.service": "customer-service"}, True, "HTTP 503"),
        ("c1", "b2", "customer-service", f"GET /customers/{customer}", "server", ms(24), ms(2145), {"http.method": "GET", "http.route": "/customers/{id}", "http.status_code": 503}, True, "database unavailable"),
        ("c2", "c1", "customer-service", "SELECT customer", "client", ms(30), ms(2032), {"db.system": "postgresql", "db.operation": "SELECT", "net.peer.name": "customers-db"}, True, f"connection timeout after 2000ms"),
    ])



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
for tid, h, c in zip(ids[:3], times[:3], customers[:3]):
    write_trace("cascade", tid, cascade_full(tid, ns(h), c))
recorded = HERE.parents[2] / "evals" / "fixtures" / "01-downstream-503" / "tempo.json"
(HERE / "cascade" / "traces" / ids[3]).mkdir(parents=True, exist_ok=True)
shutil.copyfile(recorded, HERE / "cascade" / "traces" / ids[3] / "tempo.json")
t_inv = ns("13:51:09")
write_trace("cascade", "0e1f2a3b4c5d6e7f8091a2b3c4d5e6f7", otlp("0e1f2a3b4c5d6e7f8091a2b3c4d5e6f7", [
    ("d1", None, "inventory-service", "GET /stock/88123", "server", t_inv, t_inv + 14_000_000,
     {"http.method": "GET", "http.route": "/stock/{sku}", "http.status_code": 500}, True, "sku 88123 not found in warehouse 3")]))

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
for i, tr in enumerate(tt):
    tid = tr["traceID"]
    t0 = int(tr["startTimeUnixNano"])
    ms = lambda o, t0=t0: t0 + int(o * 1e6)
    ddb = lambda k, o, op: [
        (k, "q", "enrichment-service", f"DynamoDB.{op}", "client", ms(o), ms(o + 9),
         {"rpc.system": "aws-api", "rpc.service": "DynamoDB", "rpc.method": op, "db.system": "dynamodb", "http.status_code": 200}, False, None),
        (k + "h", k, "enrichment-service", "POST", "client", ms(o + 1), ms(o + 8),
         {"net.peer.name": "dynamodb.us-east-1.amazonaws.com", "http.status_code": 200}, False, None)]
    write_trace("timeouts", tid, otlp(tid, [
        ("q", "outside", "enrichment-service", "orders process", "consumer", ms(0), ms(30830),
         {"messaging.system": "aws_sqs", "messaging.destination.name": "orders"}, False, None),
        *ddb("g1", 5, "GetItem"), *ddb("g2", 20, "GetItem"),
        ("e", "q", "enrichment-service", "POST", "client", ms(800), ms(30801),
         {"http.status_code": "nil", "net.peer.name": "provider-x.example.com"}, True, "The operation was aborted due to timeout"),
    ]))

# silence -------------------------------------------------------------------
write("silence", {"start": "2026-09-17T13:00:00Z", "end": "2026-09-17T14:00:00Z", "source": "all"},
      {"traces": [], "metrics": {"completedJobs": 12, "totalJobs": 12}}, loki([]))
print("fixtures written:", sorted(p.name for p in HERE.iterdir() if p.is_dir()))
