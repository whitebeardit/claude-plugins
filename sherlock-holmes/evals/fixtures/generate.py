#!/usr/bin/env python3
"""Deterministic fixture generator for the sherlock-holmes plugin evals.

Run:  python3 generate.py

Creates six case directories next to this file. Each case contains:
  loki.json      body of Loki  GET /loki/api/v1/query_range   (resultType=streams)
  tempo.json     body of Tempo GET /api/traces/<traceId>      (OTLP JSON mapping)
                 -> absent when the case has no trace (consumer treats it as 404)
  expected.json  ground truth for the evaluator

Also writes cases.json and README.md at the fixtures root.

Standard library only. No wall-clock reads, no randomness: every id is derived
from a SHA-256 of a fixed label, every timestamp is an offset from a fixed
base instant. Running the script twice yields byte-identical output.
All identifiers, hostnames, ids and messages are synthetic.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# Fixed base instant: 2026-09-17T14:02:31.000Z
BASE = datetime(2026, 9, 17, 14, 2, 31, tzinfo=timezone.utc)
T0_NS = int(BASE.timestamp()) * 1_000_000_000
MS = 1_000_000  # nanoseconds per millisecond

# "11- or 14-digit numeric strings" (CPF/CNPJ-shaped) must never appear.
DIGIT_RUN_11 = re.compile(r"(?<!\d)\d{11}(?!\d)")
DIGIT_RUN_14 = re.compile(r"(?<!\d)\d{14}(?!\d)")
LONG_DIGIT_RUN = re.compile(r"\d{11,}")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def hex_id(label, nbytes):
    """Deterministic lowercase hex id of `nbytes` bytes derived from a label.

    Ids whose hex or base64 form contains a run of 11+ digits are skipped
    (re-salted) so the output never contains CPF/CNPJ-shaped strings.
    """
    salt = 0
    while True:
        digest = hashlib.sha256(
            "sherlock-holmes-fixture:{}:{}".format(label, salt).encode()
        ).hexdigest()
        h = digest[: nbytes * 2]
        b64 = base64.b64encode(bytes.fromhex(h)).decode()
        if not LONG_DIGIT_RUN.search(h) and not LONG_DIGIT_RUN.search(b64):
            return h
        salt += 1


def hex_to_b64(h):
    """Protobuf JSON mapping: bytes fields are base64 of the raw bytes."""
    return base64.b64encode(bytes.fromhex(h)).decode()


def iso_ms(ns):
    secs, rem = divmod(ns, 1_000_000_000)
    dt = datetime.fromtimestamp(secs, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".{:03d}Z".format(rem // MS)


def attr_value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}  # int64 -> JSON string per proto3 mapping
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def attrs_list(pairs):
    return [{"key": k, "value": attr_value(v)} for k, v in pairs]


# --------------------------------------------------------------------------- #
# case model
# --------------------------------------------------------------------------- #
class Case:
    def __init__(self, name, services, loki_service_label="service_name", tempo=True):
        self.name = name
        self.services = list(services)
        self.trace_id = hex_id("{}:trace".format(name), 16)
        self.loki_service_label = loki_service_label
        self.tempo = tempo
        self.spans = {}      # key -> span record (ms offsets, hex ids)
        self.logs = []       # log records
        self.skew_ms = {}    # service -> ms added to that service's LOG timestamps
        self.expected = None

    # ids ------------------------------------------------------------------
    def span_id(self, key):
        return hex_id("{}:span:{}".format(self.name, key), 8)

    # spans ----------------------------------------------------------------
    def span(self, key, service, name, kind, start_ms, end_ms,
             parent=None, attrs=None, status=None, events=None):
        assert key not in self.spans, key
        assert service in self.services, service
        assert end_ms > start_ms, key
        if parent is not None:
            p = self.spans[parent]
            assert p["start_ms"] <= start_ms and end_ms <= p["end_ms"], \
                "span {} not nested in parent {}".format(key, parent)
        self.spans[key] = {
            "key": key,
            "service": service,
            "name": name,
            "kind": "SPAN_KIND_" + kind,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "parent": parent,
            "span_id": self.span_id(key),
            "attrs": list(attrs or []),
            "status": status,            # None | ("OK", None) | ("ERROR", msg)
            "events": list(events or []),  # (at_ms, name, [(k, v), ...])
        }
        for at_ms, _n, _a in self.spans[key]["events"]:
            assert start_ms <= at_ms <= end_ms, "event outside span {}".format(key)

    # logs -----------------------------------------------------------------
    def log(self, service, level, msg, at_ms, span_key, fields=None):
        assert service in self.services, service
        assert level in ("info", "warn", "error"), level
        if self.tempo:
            assert span_key in self.spans, "log references unknown span {}".format(span_key)
            s = self.spans[span_key]
            assert s["service"] == service, "log service != span service for {}".format(span_key)
            assert s["start_ms"] <= at_ms <= s["end_ms"], \
                "log '{}' at {}ms outside span {}".format(msg, at_ms, span_key)
        ts_ns = T0_NS + (at_ms + self.skew_ms.get(service, 0)) * MS
        line = {"level": level, "msg": msg, "trace_id": self.trace_id,
                "span_id": self.span_id(span_key), "service": service}
        for k, v in (fields or {}).items():
            line[k] = v
        self.logs.append({"service": service, "level": level, "ts_ns": ts_ns,
                          "true_ms": at_ms, "line": line})

    # renderers ------------------------------------------------------------
    def render_loki(self):
        streams = {}
        for rec in self.logs:
            key = (rec["service"], rec["level"])
            streams.setdefault(key, []).append(rec)
        result = []
        # Streams are emitted sorted by label set, NOT by time: the first
        # stream may hold the last event. Consumers must sort globally.
        for (service, level) in sorted(streams):
            recs = sorted(streams[(service, level)], key=lambda r: r["ts_ns"])
            values = [[str(r["ts_ns"]), json.dumps(r["line"], separators=(",", ":"))]
                      for r in recs]
            result.append({
                "stream": {self.loki_service_label: service, "level": level, "env": "prod"},
                "values": values,
            })
        return {"status": "success",
                "data": {"resultType": "streams", "result": result, "stats": {}}}

    def render_tempo(self):
        batches = []
        for service in self.services:
            spans = [s for s in self.spans.values() if s["service"] == service]
            if not spans:
                continue  # no batch for a service that produced no spans
            spans.sort(key=lambda s: (s["start_ms"], s["key"]))
            out = []
            for s in spans:
                span = {
                    "traceId": hex_to_b64(self.trace_id),
                    "spanId": hex_to_b64(s["span_id"]),
                }
                if s["parent"] is not None:
                    span["parentSpanId"] = hex_to_b64(self.spans[s["parent"]]["span_id"])
                span["name"] = s["name"]
                span["kind"] = s["kind"]
                span["startTimeUnixNano"] = str(T0_NS + s["start_ms"] * MS)
                span["endTimeUnixNano"] = str(T0_NS + s["end_ms"] * MS)
                span["attributes"] = attrs_list(s["attrs"])
                if s["status"] is not None:
                    code, message = s["status"]
                    st = {"code": "STATUS_CODE_" + code}
                    if message:
                        st["message"] = message
                    span["status"] = st
                if s["events"]:
                    span["events"] = [
                        {"timeUnixNano": str(T0_NS + at_ms * MS), "name": name,
                         "attributes": attrs_list(a)}
                        for at_ms, name, a in s["events"]
                    ]
                out.append(span)
            batches.append({
                "resource": {"attributes": attrs_list([
                    ("service.name", service),
                    ("deployment.environment", "prod"),
                ])},
                "scopeSpans": [{"scope": {"name": "otel-instrumentation"}, "spans": out}],
            })
        return {"batches": batches}

    def set_expected(self, first_anomaly_service, first_anomaly_summary, first_anomaly_ms,
                     hypothesis, confidence, accepted_confidence, tempo_needed,
                     must_not_claim, notes):
        self.expected = {
            "case": self.name,
            "trace_id": self.trace_id,
            "services": self.services,
            "first_anomaly": {
                "service": first_anomaly_service,
                "summary": first_anomaly_summary,
                "timestamp": iso_ms(T0_NS + first_anomaly_ms * MS),
            },
            "root_cause_hypothesis": hypothesis,
            "expected_confidence": confidence,
            "accepted_confidence": accepted_confidence,
            "tempo_available": self.tempo,
            "tempo_needed": tempo_needed,
            "must_not_claim": must_not_claim,
            "notes": notes,
        }


# --------------------------------------------------------------------------- #
# case 01: downstream 503 caused by a PostgreSQL connection timeout
# --------------------------------------------------------------------------- #
def build_case_01():
    c = Case("01-downstream-503", ["api", "payment-service", "customer-service"])

    c.span("api.server", "api", "POST /payment", "SERVER", 0, 2180,
           attrs=[("http.method", "POST"), ("http.route", "/payment"),
                  ("http.status_code", 500)],
           status=("ERROR", "upstream failure: payment-service returned 502"))
    c.span("api.client", "api", "POST payment-service/charge", "CLIENT", 5, 2170,
           parent="api.server",
           attrs=[("http.method", "POST"), ("peer.service", "payment-service"),
                  ("http.url", "http://payment-service:8080/charge"),
                  ("http.status_code", 502)],
           status=("ERROR", "HTTP 502"))
    c.span("payment.server", "payment-service", "POST /charge", "SERVER", 8, 2165,
           parent="api.client",
           attrs=[("http.method", "POST"), ("http.route", "/charge"),
                  ("http.status_code", 502)],
           status=("ERROR", "upstream customer-service returned 503"))
    c.span("payment.client", "payment-service", "GET customer-service/customers/42",
           "CLIENT", 20, 2150, parent="payment.server",
           attrs=[("http.method", "GET"), ("peer.service", "customer-service"),
                  ("http.url", "http://customer-service:8080/customers/42"),
                  ("http.status_code", 503)],
           status=("ERROR", "HTTP 503"))
    c.span("customer.server", "customer-service", "GET /customers/42", "SERVER", 24, 2145,
           parent="payment.client",
           attrs=[("http.method", "GET"), ("http.route", "/customers/{id}"),
                  ("http.status_code", 503)],
           status=("ERROR", "database unavailable"))
    c.span("customer.db", "customer-service", "SELECT customer", "CLIENT", 30, 2032,
           parent="customer.server",
           attrs=[("db.system", "postgresql"), ("db.name", "customers"),
                  ("db.operation", "SELECT"),
                  ("db.statement", "SELECT id, name, tier FROM customers WHERE id = $1"),
                  ("net.peer.name", "customers-db"), ("net.peer.port", 5432)],
           status=("ERROR", "connection timeout after 2000ms"),
           events=[(2031, "exception", [
               ("exception.type", "ConnectionTimeoutError"),
               ("exception.message", "PostgreSQL connection timeout after 2000ms"),
           ])])

    c.log("api", "info", "POST /payment", 1, "api.server",
          {"http.method": "POST", "http.route": "/payment"})
    c.log("payment-service", "info", "POST /charge", 9, "payment.server",
          {"http.method": "POST"})
    c.log("payment-service", "info", "calling customer-service GET /customers/42", 21,
          "payment.client", {"http.method": "GET", "upstream": "customer-service"})
    c.log("customer-service", "info", "GET /customers/42", 25, "customer.server",
          {"http.method": "GET"})
    c.log("customer-service", "error", "PostgreSQL connection timeout after 2000ms", 2031,
          "customer.db", {"db.system": "postgresql", "duration_ms": 2000,
                          "exception": "ConnectionTimeoutError"})
    c.log("customer-service", "warn", "HTTP 503", 2144, "customer.server",
          {"http.status_code": 503, "duration_ms": 2120})
    c.log("payment-service", "error", "upstream customer-service returned 503", 2160,
          "payment.server", {"upstream": "customer-service", "upstream.status_code": 503})
    c.log("payment-service", "warn", "HTTP 502", 2163, "payment.server",
          {"http.status_code": 502, "duration_ms": 2155})
    c.log("api", "error", "HTTP 500", 2178, "api.server",
          {"http.status_code": 500, "duration_ms": 2178})

    c.set_expected(
        "customer-service",
        "PostgreSQL connection timeout after 2000ms while serving GET /customers/42",
        2031,
        "customer-service could not obtain a PostgreSQL connection within 2000ms "
        "(db CLIENT span 'SELECT customer' ended in error); it answered 503, "
        "payment-service propagated the failure as 502 and api surfaced HTTP 500 "
        "to the caller.",
        "HIGH", ["HIGH"], False,
        ["connection pool exhaustion", "API gateway is the cause",
         "payment-service is the root cause", "api is the root cause"],
        "A correct investigation walks the chain api -> payment-service -> "
        "customer-service -> PostgreSQL and identifies the earliest error as the "
        "PostgreSQL connection timeout inside customer-service (2000ms db span in "
        "error). Every later error (customer 503, payment 502, api 500) is a "
        "consequence, not a cause. Logs alone are sufficient; Tempo only confirms "
        "the timing. The data says 'connection timeout', not 'pool exhaustion': "
        "the investigation must not upgrade the symptom into a mechanism the "
        "evidence does not show.",
    )
    return c


# --------------------------------------------------------------------------- #
# case 02: DB timeout with retries, Loki only (no trace in Tempo)
# --------------------------------------------------------------------------- #
def build_case_02():
    c = Case("02-db-timeout-retries", ["order-service", "inventory-service"], tempo=False)

    c.log("order-service", "info", "POST /orders", 0, "order.server",
          {"http.method": "POST", "http.route": "/orders"})
    c.log("order-service", "info", "calling inventory-service POST /reserve", 12,
          "order.client", {"http.method": "POST", "upstream": "inventory-service"})
    c.log("inventory-service", "info", "POST /reserve", 15, "inventory.server",
          {"http.method": "POST"})
    c.log("inventory-service", "error", "DB connection timeout (attempt 1/3)", 1015,
          "inventory.db", {"attempt": 1, "duration_ms": 1000, "db.system": "postgresql"})
    c.log("inventory-service", "info", "retrying in 200ms", 1016, "inventory.server",
          {"attempt": 1, "backoff_ms": 200})
    c.log("inventory-service", "error", "DB connection timeout (attempt 2/3)", 2216,
          "inventory.db", {"attempt": 2, "duration_ms": 1000, "db.system": "postgresql"})
    c.log("inventory-service", "info", "retrying in 200ms", 2217, "inventory.server",
          {"attempt": 2, "backoff_ms": 200})
    c.log("inventory-service", "error", "DB connection timeout (attempt 3/3)", 3417,
          "inventory.db", {"attempt": 3, "duration_ms": 1000, "db.system": "postgresql"})
    c.log("inventory-service", "warn", "giving up, returning 503", 3418,
          "inventory.server", {"http.status_code": 503, "attempts": 3})
    c.log("order-service", "error", "inventory-service returned 503, order rejected", 3425,
          "order.server", {"upstream": "inventory-service", "upstream.status_code": 503})
    c.log("order-service", "warn", "HTTP 503", 3427, "order.server",
          {"http.status_code": 503, "duration_ms": 3427})

    c.set_expected(
        "inventory-service",
        "DB connection timeout (attempt 1/3)",
        1015,
        "inventory-service could not reach its database: three consecutive "
        "connection timeouts (attempts 1/3, 2/3 and 3/3 with 200ms backoff) "
        "exhausted the retry budget, inventory-service answered 503 and "
        "order-service rejected the order with HTTP 503. Why the database did "
        "not answer is not visible in the available data.",
        "MEDIUM", ["MEDIUM"], False,
        ["connection pool exhaustion", "Tempo retention problem",
         "three independent failures", "the database crashed"],
        "Only Loki has data for this trace; Tempo returns 404. A correct "
        "investigation identifies the first anomaly as the attempt-1 DB "
        "connection timeout in inventory-service and recognises attempts 2 and 3 "
        "as retries of the same failure, not separate incidents. The retry "
        "exhaustion explains the 503 chain. The root cause of the timeout itself "
        "(network, database load, DNS, credentials) cannot be established from "
        "logs alone, so confidence stays MEDIUM. The missing trace is simply "
        "absent: nothing in the data supports a claim about Tempo retention or "
        "sampling, and nothing supports 'pool exhaustion' over a plain timeout.",
    )
    return c


# --------------------------------------------------------------------------- #
# case 03: the real error is only visible in Tempo (S3 AccessDenied)
# --------------------------------------------------------------------------- #
def build_case_03():
    c = Case("03-error-only-in-tempo", ["api", "report-service", "cache-service"])

    c.span("api.server", "api", "GET /reports/9", "SERVER", 0, 1260,
           attrs=[("http.method", "GET"), ("http.route", "/reports/{id}"),
                  ("http.status_code", 500)],
           status=("ERROR", "report-service returned 500"))
    c.span("api.client", "api", "GET report-service/reports/9", "CLIENT", 4, 1252,
           parent="api.server",
           attrs=[("http.method", "GET"), ("peer.service", "report-service"),
                  ("http.url", "http://report-service:8080/reports/9"),
                  ("http.status_code", 500)],
           status=("ERROR", "HTTP 500"))
    c.span("report.server", "report-service", "GET /reports/9", "SERVER", 7, 1248,
           parent="api.client",
           attrs=[("http.method", "GET"), ("http.route", "/reports/{id}"),
                  ("http.status_code", 500)],
           status=("ERROR", "request failed"))
    c.span("report.cache", "report-service", "GET cache-service/cache/reports/9", "CLIENT",
           10, 28, parent="report.server",
           attrs=[("http.method", "GET"), ("peer.service", "cache-service"),
                  ("http.url", "http://cache-service:8080/cache/reports/9"),
                  ("http.status_code", 200)],
           status=("OK", None))
    c.span("cache.server", "cache-service", "GET /cache/reports/9", "SERVER", 12, 27,
           parent="report.cache",
           attrs=[("http.method", "GET"), ("http.route", "/cache/reports/{id}"),
                  ("http.status_code", 200), ("cache.hit", True)],
           status=("OK", None))
    c.span("report.s3", "report-service", "S3 GetObject", "CLIENT", 40, 1240,
           parent="report.server",
           attrs=[("rpc.system", "aws-api"), ("rpc.service", "S3"),
                  ("rpc.method", "GetObject"),
                  ("aws.s3.bucket", "synthetic-reports-bucket"),
                  ("aws.s3.key", "reports/9.pdf"), ("aws.region", "eu-central-1")],
           status=("ERROR", "AccessDenied: User is not authorized to perform s3:GetObject"),
           events=[(1239, "exception", [
               ("exception.type", "AccessDenied"),
               ("exception.message", "User is not authorized to perform s3:GetObject"),
           ])])

    c.log("api", "info", "GET /reports/9", 1, "api.server",
          {"http.method": "GET", "http.route": "/reports/{id}"})
    c.log("report-service", "info", "GET /reports/9", 8, "report.server",
          {"http.method": "GET"})
    c.log("cache-service", "info", "GET /cache/reports/9", 13, "cache.server",
          {"http.method": "GET"})
    c.log("cache-service", "info", "HTTP 200", 26, "cache.server",
          {"http.status_code": 200, "duration_ms": 14, "cache.hit": True})
    c.log("report-service", "info", "cache metadata found for report 9, fetching object", 29,
          "report.server", {"report_id": 9})
    c.log("report-service", "error", "request failed", 1245, "report.server",
          {"http.status_code": 500})
    c.log("api", "error", "HTTP 500", 1258, "api.server",
          {"http.status_code": 500, "duration_ms": 1258})

    c.set_expected(
        "report-service",
        "S3 GetObject failed: AccessDenied: User is not authorized to perform s3:GetObject",
        1240,
        "report-service's S3 GetObject call for reports/9.pdf was rejected with "
        "AccessDenied (the credentials used by report-service lack s3:GetObject "
        "on the bucket); the handler logged only 'request failed' and returned "
        "500, which api surfaced as HTTP 500. The cache-service lookup succeeded "
        "and is unrelated.",
        "HIGH", ["HIGH"], True,
        ["cache-service is the cause", "the logs are sufficient to determine the cause",
         "S3 is unavailable", "the S3 request timed out"],
        "Loki shows only 'request failed' in report-service with no cause and a "
        "500 at the api edge: logs alone cannot explain the failure. A correct "
        "investigation notices that gap, consults Tempo and finds the CLIENT span "
        "'S3 GetObject' (rpc.system=aws-api, rpc.service=S3) in report-service "
        "ending in STATUS_CODE_ERROR with message 'AccessDenied: User is not "
        "authorized to perform s3:GetObject' after 1200ms. The cache-service "
        "server span is OK (15ms) and must not be blamed. Once Tempo is consulted "
        "the cause is explicit, so confidence is HIGH; without Tempo the honest "
        "answer is 'insufficient evidence'.",
    )
    return c


# --------------------------------------------------------------------------- #
# case 04: contradictory logs + clock skew, Loki labels use `app`
# --------------------------------------------------------------------------- #
def build_case_04():
    c = Case("04-contradictory-logs",
             ["checkout-service", "payment-service", "gateway-adapter"],
             loki_service_label="app")
    # checkout-service's clock is 3 seconds behind everyone else's.
    c.skew_ms["checkout-service"] = -3000

    c.span("checkout.server", "checkout-service", "POST /checkout", "SERVER", 0, 150,
           attrs=[("http.method", "POST"), ("http.route", "/checkout"),
                  ("http.status_code", 200)],
           status=("OK", None))
    # Instrumentation gap: the outbound client span carries no status and no
    # http.status_code, so it does not settle the contradiction either way.
    c.span("checkout.client", "checkout-service", "POST payment-service/charge", "CLIENT",
           5, 135, parent="checkout.server",
           attrs=[("http.method", "POST"), ("peer.service", "payment-service"),
                  ("http.url", "http://payment-service:8080/charge")],
           status=None)
    c.span("payment.server", "payment-service", "POST /charge", "SERVER", 8, 130,
           parent="checkout.client",
           attrs=[("http.method", "POST"), ("http.route", "/charge"),
                  ("payment.gateway_code", "51"), ("payment.outcome", "declined")],
           status=("ERROR", "charge declined by gateway (code 51)"))
    c.span("payment.client", "payment-service", "POST gateway-adapter/authorize", "CLIENT",
           15, 110, parent="payment.server",
           attrs=[("http.method", "POST"), ("peer.service", "gateway-adapter"),
                  ("http.url", "http://gateway-adapter:8080/authorize"),
                  ("http.status_code", 200)],
           status=("OK", None))
    c.span("adapter.server", "gateway-adapter", "POST /authorize", "SERVER", 20, 105,
           parent="payment.client",
           attrs=[("http.method", "POST"), ("http.route", "/authorize"),
                  ("http.status_code", 200), ("payment.auth_code", "ABC123"),
                  ("payment.outcome", "approved")],
           status=("OK", None))

    c.log("checkout-service", "info", "POST /checkout", 1, "checkout.server",
          {"http.method": "POST", "http.route": "/checkout"})
    c.log("payment-service", "info", "POST /charge", 9, "payment.server",
          {"http.method": "POST"})
    c.log("gateway-adapter", "info", "POST /authorize", 21, "adapter.server",
          {"http.method": "POST"})
    c.log("gateway-adapter", "info", "charge approved, auth=ABC123", 100, "adapter.server",
          {"http.status_code": 200, "auth_code": "ABC123", "outcome": "approved"})
    c.log("payment-service", "error", "charge declined by gateway (code 51)", 120,
          "payment.server", {"gateway_code": "51", "outcome": "declined"})
    c.log("checkout-service", "info", "payment confirmed", 140, "checkout.server",
          {"outcome": "confirmed"})
    c.log("checkout-service", "info", "HTTP 200", 148, "checkout.server",
          {"http.status_code": 200, "duration_ms": 148})

    c.set_expected(
        "payment-service",
        "charge declined by gateway (code 51) contradicts gateway-adapter "
        "'charge approved, auth=ABC123' and checkout-service 'payment confirmed'",
        120,
        "Undetermined. The evidence is contradictory: gateway-adapter reports the "
        "charge approved (auth=ABC123, span OK), payment-service reports it "
        "declined (gateway code 51, span ERROR), and checkout-service reports "
        "'payment confirmed' with HTTP 200 (span OK). In addition, "
        "checkout-service's log timestamps are about 3 seconds behind its own "
        "spans and behind the other services, so its lines sort first although "
        "they belong to the same request. A correct investigation reports the "
        "contradiction and the clock skew and asks for the authoritative payment "
        "record instead of picking a side.",
        "LOW", ["LOW"], True,
        ["the payment was declined", "the payment succeeded",
         "checkout-service ran before payment-service", "the logs are consistent"],
        "Two traps. First, Loki streams here are labelled with `app` instead of "
        "`service_name`, so a consumer that only reads `service_name` sees no "
        "services. Second, the three services disagree about the outcome of the "
        "same charge and checkout-service's log clock is skewed by -3s (its spans "
        "in Tempo are not skewed, which is how the skew can be detected). The "
        "correct output states the contradiction explicitly (declined vs approved "
        "vs confirmed), flags the skew, refuses to assert either outcome as fact, "
        "and reports LOW confidence. Tempo is needed to demonstrate the skew and to "
        "show that the payment-service span is the only ERROR in an otherwise OK "
        "tree.",
    )
    return c


# --------------------------------------------------------------------------- #
# case 05: incomplete trace, fulfillment-service has no spans and no logs
# --------------------------------------------------------------------------- #
def build_case_05():
    c = Case("05-incomplete-trace", ["api", "order-service", "fulfillment-service"])

    c.span("api.server", "api", "POST /orders", "SERVER", 0, 30000,
           attrs=[("http.method", "POST"), ("http.route", "/orders"),
                  ("http.status_code", 504)],
           status=("ERROR", "deadline exceeded"))
    c.span("api.client", "api", "POST order-service/orders", "CLIENT", 3, 29999,
           parent="api.server",
           attrs=[("http.method", "POST"), ("peer.service", "order-service"),
                  ("http.url", "http://order-service:8080/orders")],
           status=("ERROR", "context deadline exceeded"))
    c.span("order.server", "order-service", "POST /orders", "SERVER", 6, 29992,
           parent="api.client",
           attrs=[("http.method", "POST"), ("http.route", "/orders"),
                  ("http.status_code", 202)],
           status=("OK", None))
    c.span("order.db", "order-service", "INSERT order", "CLIENT", 10, 32,
           parent="order.server",
           attrs=[("db.system", "postgresql"), ("db.name", "orders"),
                  ("db.operation", "INSERT"),
                  ("db.statement", "INSERT INTO orders (id, status) VALUES ($1, $2)"),
                  ("net.peer.name", "orders-db"), ("net.peer.port", 5432)],
           status=("OK", None))
    c.span("order.client", "order-service", "POST fulfillment-service/fulfil", "CLIENT",
           40, 29840, parent="order.server",
           attrs=[("http.method", "POST"), ("peer.service", "fulfillment-service"),
                  ("http.url", "http://fulfillment-service:8080/fulfil")],
           status=("ERROR", "context deadline exceeded"))
    # fulfillment-service: deliberately NO server span and NO log lines.

    c.log("api", "info", "POST /orders", 1, "api.server",
          {"http.method": "POST", "http.route": "/orders"})
    c.log("order-service", "info", "POST /orders", 7, "order.server",
          {"http.method": "POST"})
    c.log("order-service", "info", "order 77 persisted", 33, "order.server",
          {"order_id": 77})
    c.log("order-service", "info", "calling fulfillment-service POST /fulfil", 41,
          "order.client", {"http.method": "POST", "upstream": "fulfillment-service"})
    # ~30 s gap
    c.log("api", "warn", "upstream order-service did not respond within 30000ms", 29998,
          "api.server", {"upstream": "order-service", "timeout_ms": 30000})
    c.log("api", "error", "HTTP 504 gateway timeout", 29999, "api.server",
          {"http.status_code": 504, "duration_ms": 29999})

    c.set_expected(
        "order-service",
        "context deadline exceeded on CLIENT span 'POST fulfillment-service/fulfil' "
        "after 29800ms; no server span and no log line from fulfillment-service",
        29840,
        "The call from order-service to fulfillment-service never completed: the "
        "client span 'POST fulfillment-service/fulfil' ended with 'context "
        "deadline exceeded' after 29800ms, and there is no server span and no log "
        "line from fulfillment-service for this trace, so whether "
        "fulfillment-service received the request cannot be established. api hit "
        "its 30s deadline and returned HTTP 504. Order 77 itself was persisted.",
        "MEDIUM", ["LOW", "MEDIUM"], True,
        ["fulfillment-service crashed", "fulfillment-service received the request",
         "fulfillment-service is healthy", "order-service is the root cause",
         "the order was not persisted"],
        "The trace is incomplete on purpose: the last hop has a CLIENT span in "
        "order-service but no matching SERVER span in fulfillment-service, and "
        "fulfillment-service emitted no logs. A correct investigation points at "
        "the deadline exceeded on that client span as the first anomaly, states "
        "plainly that evidence for fulfillment-service is missing, and does not "
        "fabricate what happened there (crash, overload, network partition and "
        "'never received it' are all unproven). Loki alone only shows a 30 s gap "
        "followed by a 504; Tempo is needed to locate the hop where time was "
        "lost. Confidence LOW or MEDIUM is acceptable.",
    )
    return c


# --------------------------------------------------------------------------- #
# case 06: intermediate service throws while the downstream is healthy
# --------------------------------------------------------------------------- #
def build_case_06():
    c = Case("06-intermediate-service-error",
             ["api", "gateway-bff", "pricing-service", "tax-service"])

    stack = ("java.lang.NullPointerException: Cannot invoke \"Discount.rate()\" "
             "because \"discount\" is null\n"
             "\tat com.synthetic.pricing.PriceCalculator.compute(PriceCalculator.java:88)\n"
             "\tat com.synthetic.pricing.PriceController.getPrice(PriceController.java:41)")

    c.span("api.server", "api", "GET /quotes/5", "SERVER", 0, 95,
           attrs=[("http.method", "GET"), ("http.route", "/quotes/{id}"),
                  ("http.status_code", 500)],
           status=("ERROR", "gateway-bff returned 500"))
    c.span("api.client", "api", "GET gateway-bff/quotes/5", "CLIENT", 3, 90,
           parent="api.server",
           attrs=[("http.method", "GET"), ("peer.service", "gateway-bff"),
                  ("http.url", "http://gateway-bff:8080/quotes/5"),
                  ("http.status_code", 500)],
           status=("ERROR", "HTTP 500"))
    c.span("bff.server", "gateway-bff", "GET /quotes/5", "SERVER", 5, 88,
           parent="api.client",
           attrs=[("http.method", "GET"), ("http.route", "/quotes/{id}"),
                  ("http.status_code", 500)],
           status=("ERROR", "upstream pricing-service 500"))
    c.span("bff.client", "gateway-bff", "GET pricing-service/price", "CLIENT", 8, 84,
           parent="bff.server",
           attrs=[("http.method", "GET"), ("peer.service", "pricing-service"),
                  ("http.url", "http://pricing-service:8080/price?quote=5"),
                  ("http.status_code", 500)],
           status=("ERROR", "HTTP 500"))
    c.span("pricing.server", "pricing-service", "GET /price", "SERVER", 10, 82,
           parent="bff.client",
           attrs=[("http.method", "GET"), ("http.route", "/price"),
                  ("http.status_code", 500)],
           status=("ERROR", "java.lang.NullPointerException"),
           events=[(78, "exception", [
               ("exception.type", "java.lang.NullPointerException"),
               ("exception.message",
                "Cannot invoke \"Discount.rate()\" because \"discount\" is null"),
               ("exception.stacktrace", stack),
               ("exception.escaped", True),
           ])])
    c.span("pricing.client", "pricing-service", "GET tax-service/tax/rate", "CLIENT", 15, 30,
           parent="pricing.server",
           attrs=[("http.method", "GET"), ("peer.service", "tax-service"),
                  ("http.url", "http://tax-service:8080/tax/rate?region=synthetic"),
                  ("http.status_code", 200)],
           status=("OK", None))
    c.span("tax.server", "tax-service", "GET /tax/rate", "SERVER", 17, 29,
           parent="pricing.client",
           attrs=[("http.method", "GET"), ("http.route", "/tax/rate"),
                  ("http.status_code", 200)],
           status=("OK", None))

    c.log("api", "info", "GET /quotes/5", 1, "api.server",
          {"http.method": "GET", "http.route": "/quotes/{id}"})
    c.log("gateway-bff", "info", "GET /quotes/5", 6, "bff.server",
          {"http.method": "GET"})
    c.log("pricing-service", "info", "GET /price", 11, "pricing.server",
          {"http.method": "GET", "quote_id": 5})
    c.log("tax-service", "info", "GET /tax/rate", 18, "tax.server",
          {"http.method": "GET"})
    c.log("tax-service", "info", "GET /tax/rate 200 OK 12ms", 29, "tax.server",
          {"http.status_code": 200, "duration_ms": 12})
    c.log("pricing-service", "error", "NullPointerException at PriceCalculator.java:88", 79,
          "pricing.server", {"exception": "java.lang.NullPointerException",
                             "stack": stack})
    c.log("pricing-service", "warn", "HTTP 500", 81, "pricing.server",
          {"http.status_code": 500, "duration_ms": 71})
    c.log("gateway-bff", "error", "upstream pricing-service 500", 86, "bff.server",
          {"upstream": "pricing-service", "upstream.status_code": 500})
    c.log("gateway-bff", "warn", "HTTP 500", 87, "bff.server",
          {"http.status_code": 500, "duration_ms": 82})
    c.log("api", "error", "HTTP 500", 93, "api.server",
          {"http.status_code": 500, "duration_ms": 93})

    c.set_expected(
        "pricing-service",
        "NullPointerException at PriceCalculator.java:88",
        79,
        "pricing-service threw java.lang.NullPointerException in "
        "PriceCalculator.compute (PriceCalculator.java:88) after tax-service had "
        "already answered GET /tax/rate with 200 OK in 12ms; pricing-service "
        "returned HTTP 500, which gateway-bff and then api propagated as HTTP 500.",
        "HIGH", ["HIGH"], False,
        ["tax-service failed", "tax-service is the root cause",
         "gateway-bff is the root cause", "api is the root cause"],
        "The failure originates in the middle of the chain. tax-service, the "
        "deepest downstream, is healthy (200 OK, 12ms, span OK) and its healthy "
        "response precedes the exception in pricing-service. A correct "
        "investigation attributes the first anomaly to pricing-service, states "
        "that the downstream tax-service is not the cause, and describes the "
        "propagation direction correctly: pricing-service 500 -> gateway-bff 500 "
        "-> api 500 (upstream toward the caller). Logs alone suffice because the "
        "exception and stack are logged; Tempo confirms the same with the "
        "exception event.",
    )
    return c


# --------------------------------------------------------------------------- #
# writing + validation
# --------------------------------------------------------------------------- #
def dump(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")


README = """# sherlock-holmes eval fixtures

Synthetic Grafana Loki and Grafana Tempo responses for six production-incident
cases, each with a ground-truth `expected.json`. Everything here is generated by
`generate.py`; do not edit the JSON files by hand.

## Layout

```
fixtures/
  generate.py            deterministic generator (Python 3, stdlib only)
  cases.json             index: case, trace_id, tempo_available, dir
  README.md              this file
  <case>/loki.json       body of Loki  GET /loki/api/v1/query_range (resultType=streams)
  <case>/tempo.json      body of Tempo GET /api/traces/<traceId> (OTLP JSON mapping)
                         absent when the case has no trace: treat as HTTP 404
  <case>/expected.json   ground truth: first anomaly, hypothesis, confidence,
                         claims that must NOT be made, notes
```

## Conventions

- `trace_id` / `span_id` in log lines are lowercase hex (32 / 16 chars).
  In `tempo.json` the same ids are base64 of the raw bytes, as the protobuf
  JSON mapping requires.
- Loki `values` are `[nanosecond-epoch-string, log-line-string]`; each log line
  is a JSON document. Streams are one per (service, level) and are NOT globally
  sorted; the consumer must merge and sort.
- Stream labels are `service_name` + `env="prod"`, except case 04 which uses
  `app` instead of `service_name`.
- Case 04 also has checkout-service log timestamps deliberately 3 s behind its
  own spans (clock skew).
- All timestamps are around 2026-09-17T14:02:31Z.

## Synthetic data

All service names, hostnames, ids, bucket names, order ids and messages are
made up. There are no real hostnames, no personal data and no real identifiers.

## Regenerate

```
python3 generate.py
```

The output is byte-identical on every run.
"""


def validate(root, cases):
    """Reload everything from disk and check the invariants the spec demands."""
    problems = []

    for c in cases:
        d = os.path.join(root, c.name)
        with open(os.path.join(d, "loki.json")) as fh:
            loki = json.load(fh)
        with open(os.path.join(d, "expected.json")) as fh:
            expected = json.load(fh)
        tempo_path = os.path.join(d, "tempo.json")
        tempo = None
        if os.path.exists(tempo_path):
            with open(tempo_path) as fh:
                tempo = json.load(fh)
        if c.tempo != (tempo is not None):
            problems.append("{}: tempo presence mismatch".format(c.name))

        # --- tempo -----------------------------------------------------
        spans_by_id = {}
        if tempo is not None:
            for batch in tempo["batches"]:
                res = {a["key"]: a["value"] for a in batch["resource"]["attributes"]}
                svc = res["service.name"]["stringValue"]
                for ss in batch["scopeSpans"]:
                    for sp in ss["spans"]:
                        tid = base64.b64decode(sp["traceId"]).hex()
                        if tid != c.trace_id:
                            problems.append("{}: span traceId mismatch".format(c.name))
                        sid = base64.b64decode(sp["spanId"]).hex()
                        if len(sid) != 16:
                            problems.append("{}: spanId not 8 bytes".format(c.name))
                        spans_by_id[sid] = (svc, sp)
            for sid, (svc, sp) in spans_by_id.items():
                if "parentSpanId" in sp:
                    pid = base64.b64decode(sp["parentSpanId"]).hex()
                    if pid not in spans_by_id:
                        problems.append("{}: orphan parent for {}".format(c.name, sp["name"]))
                    else:
                        parent = spans_by_id[pid][1]
                        if not (int(parent["startTimeUnixNano"]) <= int(sp["startTimeUnixNano"])
                                and int(sp["endTimeUnixNano"]) <= int(parent["endTimeUnixNano"])):
                            problems.append("{}: span {} not nested".format(c.name, sp["name"]))
                if int(sp["endTimeUnixNano"]) <= int(sp["startTimeUnixNano"]):
                    problems.append("{}: non-positive span {}".format(c.name, sp["name"]))
            roots = [s for _, s in spans_by_id.values() if "parentSpanId" not in s]
            if len(roots) != 1:
                problems.append("{}: expected exactly one root span".format(c.name))

        # --- loki ------------------------------------------------------
        if loki["status"] != "success" or loki["data"]["resultType"] != "streams":
            problems.append("{}: loki envelope".format(c.name))
        for stream in loki["data"]["result"]:
            labels = stream["stream"]
            if c.loki_service_label not in labels or labels.get("env") != "prod":
                problems.append("{}: stream labels {}".format(c.name, labels))
            prev = -1
            for ts, line in stream["values"]:
                if not (isinstance(ts, str) and ts.isdigit()):
                    problems.append("{}: ts not a digit string".format(c.name))
                t = int(ts)
                if t < prev:
                    problems.append("{}: values not ascending".format(c.name))
                prev = t
                rec = json.loads(line)
                for k in ("level", "msg", "trace_id", "span_id", "service"):
                    if k not in rec:
                        problems.append("{}: log line missing {}".format(c.name, k))
                if rec["trace_id"] != c.trace_id:
                    problems.append("{}: log trace_id mismatch".format(c.name))
                if not re.fullmatch(r"[0-9a-f]{16}", rec["span_id"]):
                    problems.append("{}: bad span_id {}".format(c.name, rec["span_id"]))
                if rec["service"] != labels[c.loki_service_label]:
                    problems.append("{}: service label != line service".format(c.name))
                if tempo is not None:
                    if rec["span_id"] not in spans_by_id:
                        problems.append("{}: log span_id {} not in tempo".format(
                            c.name, rec["span_id"]))
                    elif rec["service"] not in c.skew_ms:
                        svc, sp = spans_by_id[rec["span_id"]]
                        if svc != rec["service"]:
                            problems.append("{}: log/span service mismatch".format(c.name))
                        if not (int(sp["startTimeUnixNano"]) <= t <= int(sp["endTimeUnixNano"])):
                            problems.append("{}: log '{}' outside span".format(
                                c.name, rec["msg"]))

        # --- expected --------------------------------------------------
        for k in ("case", "trace_id", "services", "first_anomaly", "root_cause_hypothesis",
                  "expected_confidence", "tempo_available", "tempo_needed",
                  "must_not_claim", "notes"):
            if k not in expected:
                problems.append("{}: expected.json missing {}".format(c.name, k))
        if expected["trace_id"] != c.trace_id or expected["case"] != c.name:
            problems.append("{}: expected.json identity".format(c.name))
        if expected["first_anomaly"]["service"] not in c.services:
            problems.append("{}: first_anomaly service unknown".format(c.name))
        if expected["expected_confidence"] not in ("LOW", "MEDIUM", "HIGH"):
            problems.append("{}: bad confidence".format(c.name))

    # --- forbidden digit runs anywhere in the generated tree --------------
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn == os.path.basename(__file__):
                continue
            p = os.path.join(dirpath, fn)
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
            for pat, label in ((DIGIT_RUN_11, "11"), (DIGIT_RUN_14, "14")):
                m = pat.search(text)
                if m:
                    problems.append("{}: contains a {}-digit numeric string: {}".format(
                        os.path.relpath(p, root), label, m.group(0)))

    return problems


def print_tree(root):
    print(os.path.basename(root) + "/")
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if rel != ".":
            print("  " * depth + os.path.basename(dirpath) + "/")
        for fn in sorted(files):
            print("  " * (depth + 1) + fn)


def main():
    cases = [build_case_01(), build_case_02(), build_case_03(),
             build_case_04(), build_case_05(), build_case_06()]

    for c in cases:
        d = os.path.join(HERE, c.name)
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)
        dump(os.path.join(d, "loki.json"), c.render_loki())
        if c.tempo:
            dump(os.path.join(d, "tempo.json"), c.render_tempo())
        dump(os.path.join(d, "expected.json"), c.expected)

    dump(os.path.join(HERE, "cases.json"), [
        {"case": c.name, "trace_id": c.trace_id, "tempo_available": c.tempo, "dir": c.name}
        for c in cases
    ])
    with open(os.path.join(HERE, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(README)

    problems = validate(HERE, cases)
    print_tree(HERE)
    print()
    for c in cases:
        print("{:<32} trace_id={}  tempo={}".format(
            c.name, c.trace_id, "yes" if c.tempo else "no (404)"))
    if problems:
        print("\nVALIDATION FAILED:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        sys.exit(1)
    print("\nvalidation: ok")


if __name__ == "__main__":
    main()
