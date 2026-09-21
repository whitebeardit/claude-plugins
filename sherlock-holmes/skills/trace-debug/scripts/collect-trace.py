#!/usr/bin/env python3
"""collect-trace.py - deterministic evidence collector for the trace-debug skill.

Given a trace ID, fetch the trace from Grafana Tempo and the matching log lines
from Grafana Loki, normalise both into one JSON document, and print a compact
cut for the investigating agent.

Design rules (see README):
  * The script collects FACTS. It never decides a root cause.
  * Tempo is queried first because a trace ID carries no timestamp: the trace's
    own start/end bounds the Loki window. The agent still reads logs first.
  * Truncation is always explicit (`truncated`, `returned`, `max_lines`) so the
    agent never confuses "cut" with "absent".
  * A trace missing from Tempo is reported as `not_found`, never as an error:
    unsampled or unexported traces are normal.
  * Standard library only. Never prints credentials. Only HTTP GET.

Access modes (auto-detected per source, direct wins over proxy):
  direct : LOKI_URL / TEMPO_URL (+ *_TOKEN or *_USERNAME/*_PASSWORD, *_ORG_ID)
  proxy  : GRAFANA_URL + GRAFANA_LOKI_UID / GRAFANA_TEMPO_UID
           (+ GRAFANA_TOKEN | GRAFANA_SERVICE_ACCOUNT_TOKEN | GRAFANA_USERNAME/PASSWORD)

Loki query shape (LOKI_TRACE_FILTER):
  substring (default) : {SELECTOR} |= "<trace-id>"
  metadata            : {SELECTOR} | <LOKI_TRACE_FIELD>="<trace-id>"
  json                : {SELECTOR} | json | <LOKI_TRACE_FIELD>="<trace-id>"

Exit codes: 0 collected (at least one source usable), 1 every source failed,
2 invalid input or configuration.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"

DEFAULT_SERVICE_LABELS = "service_name,service,app,container,k8s_container_name,job"
LEVEL_KEYS = ("level", "severity", "lvl", "log.level", "severity_text", "loglevel", "log_level")
MESSAGE_KEYS = ("msg", "message", "event", "body", "text", "log")
SPAN_KEYS = ("span_id", "spanId", "spanID", "span.id", "SpanId")
TRACE_KEYS = ("trace_id", "traceId", "traceID", "trace.id", "TraceId")
SERVICE_KEYS = ("service", "service.name", "service_name", "serviceName", "app", "application", "logger")
EXC_KEYS = ("exception", "exception.stacktrace", "stack", "stack_trace", "stacktrace", "error", "err", "error.stack")
ATTR_KEEP = (
    "http.method", "http.route", "http.target", "http.url", "http.status_code",
    "http.request.method", "http.response.status_code", "url.path",
    "rpc.system", "rpc.service", "rpc.method", "db.system", "db.operation", "db.statement",
    "messaging.system", "messaging.destination.name", "messaging.operation",
    "error.type", "peer.service", "net.peer.name", "server.address",
)
LEVEL_ORDER = {"trace": 0, "debug": 1, "info": 2, "warn": 3, "error": 4, "fatal": 5}
LEVEL_ALIASES = {"warning": "warn", "err": "error", "critical": "fatal", "crit": "fatal",
                 "panic": "fatal", "information": "info", "informational": "info", "dbg": "debug"}
KIND_NAMES = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
HEX16 = re.compile(r"^[0-9a-f]{16}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
LEVEL_IN_TEXT = re.compile(r"\b(FATAL|ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE)\b", re.IGNORECASE)


class CollectError(Exception):
    """Failure of one source. Never carries credentials."""


# --------------------------------------------------------------------------- utils

def now_ns() -> int:
    return time.time_ns()


def iso(ns) -> str | None:
    if ns is None:
        return None
    ns = int(ns)
    dt = datetime.fromtimestamp(ns // 1_000_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % ((ns // 1_000_000) % 1000)


def clock(ns) -> str:
    """HH:MM:SS.mmm for compact timelines."""
    return iso(ns)[11:23] if ns is not None else "??:??:??.???"


def parse_duration(text: str) -> int:
    """'30s' | '15m' | '2h' | '3d' -> seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", text or "")
    if not m:
        raise ValueError(f"invalid duration '{text}' (use e.g. 30s, 15m, 2h, 3d)")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def parse_time(text: str) -> int:
    """ISO-8601 (with Z or offset) or epoch in s/ms/ns -> nanoseconds."""
    t = (text or "").strip()
    if re.fullmatch(r"\d+", t):
        v = int(t)
        if v < 10_000_000_000:            # seconds
            return v * 1_000_000_000
        if v < 10_000_000_000_000:        # milliseconds
            return v * 1_000_000
        return v                          # nanoseconds
    iso_t = t.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_t)
    except ValueError as exc:
        raise ValueError(f"invalid time '{text}' (use ISO-8601 or epoch)") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def normalise_trace_id(raw: str) -> str:
    t = (raw or "").strip().lower()
    m = TRACEPARENT.match(t)
    if m:
        t = m.group(1)
    if HEX16.match(t) or HEX32.match(t):
        return t
    if re.fullmatch(r"\d{11}|\d{14}", t):
        raise ValueError("input looks like a numeric document/account identifier, not a trace id; "
                         "trace ids are 16 or 32 hex characters")
    raise ValueError("invalid trace id: expected 16 or 32 hex characters (or a W3C traceparent)")


def norm_id(value, nbytes: int) -> str | None:
    """OTLP JSON encodes ids as base64 of raw bytes; some proxies return hex."""
    if not value:
        return None
    v = str(value)
    if re.fullmatch(r"[0-9a-fA-F]{%d}" % (2 * nbytes), v):
        return v.lower()
    try:
        return base64.b64decode(v, validate=True).hex()
    except (binascii.Error, ValueError):
        return v


def truncate(text, n: int) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= n else s[: n - 1] + "…"


def redact(text: str) -> str:
    """Belt and braces: hide anything that looks like a bearer token or password in messages."""
    return re.sub(r"(?i)(authorization|password|token)(=|:\s*)(\S+)", r"\1\2***", text or "")


# --------------------------------------------------------------------------- access

def auth_headers(token, username, password) -> dict:
    if token:
        return {"Authorization": "Bearer " + token}
    if username and password is not None:
        pair = f"{username}:{password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode()}
    return {}


def resolve_endpoint(source: str) -> dict:
    env = os.environ
    p = source.upper()
    direct = env.get(f"{p}_URL")
    if direct:
        headers = auth_headers(env.get(f"{p}_TOKEN"), env.get(f"{p}_USERNAME"), env.get(f"{p}_PASSWORD"))
        org = env.get(f"{p}_ORG_ID")
        if org:
            headers["X-Scope-OrgID"] = org
        return {"mode": "direct", "base": direct.rstrip("/"), "headers": headers, "auth": bool(headers)}
    grafana, uid = env.get("GRAFANA_URL"), env.get(f"GRAFANA_{p}_UID")
    if grafana and uid:
        headers = auth_headers(env.get("GRAFANA_TOKEN") or env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN"),
                               env.get("GRAFANA_USERNAME"), env.get("GRAFANA_PASSWORD"))
        base = f"{grafana.rstrip('/')}/api/datasources/proxy/uid/{uid}"
        return {"mode": "proxy", "base": base, "headers": headers, "auth": bool(headers)}
    return {"mode": "none", "base": None, "headers": {}, "auth": False,
            "error": f"no access configured for {source}: set {p}_URL (direct) "
                     f"or GRAFANA_URL + GRAFANA_{p}_UID (proxy)"}


def http_get(url: str, headers: dict, timeout: int):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": f"trace-debug/{VERSION}", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise CollectError(f"connection failed: {exc.reason}") from None
    except Exception as exc:  # timeouts, TLS errors, etc.
        raise CollectError(f"{type(exc).__name__}: {exc}") from None


def describe_http_failure(status: int, body: str) -> str:
    hint = ""
    if status in (401, 403):
        hint = " (authentication rejected: check credentials with `doctor`)"
    elif status == 400:
        hint = " (bad request: usually a LogQL/selector problem)"
    elif status == 429:
        hint = " (rate limited)"
    elif status >= 500:
        hint = " (backend error)"
    return f"HTTP {status}{hint}: {truncate(redact(body.strip()), 200)}"


# --------------------------------------------------------------------------- tempo

def _attr_value(v):
    if not isinstance(v, dict):
        return v
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            val = v[k]
            if k == "intValue":
                try:
                    return int(val)
                except (TypeError, ValueError):
                    return val
            return val
    if "arrayValue" in v:
        return [_attr_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {kv.get("key"): _attr_value(kv.get("value")) for kv in v["kvlistValue"].get("values", [])}
    return v


def attrs_to_dict(items) -> dict:
    out = {}
    for it in items or []:
        if isinstance(it, dict) and "key" in it:
            out[it["key"]] = _attr_value(it.get("value"))
    return out


def status_of(span: dict):
    st = span.get("status") or {}
    code = st.get("code", 0)
    if isinstance(code, str):
        code_name = code.replace("STATUS_CODE_", "").upper()
    else:
        code_name = {0: "UNSET", 1: "OK", 2: "ERROR"}.get(int(code), str(code))
    return code_name, st.get("message")


def kind_of(span: dict) -> str:
    k = span.get("kind", 0)
    if isinstance(k, str):
        return k.replace("SPAN_KIND_", "").upper()
    try:
        return KIND_NAMES.get(int(k), str(k))
    except (TypeError, ValueError):
        return str(k)


def parse_otlp(doc: dict, keep_all_attrs: bool, max_chars: int) -> list:
    batches = None
    for key in ("batches", "resourceSpans"):
        if isinstance(doc.get(key), list):
            batches = doc[key]
            break
    if batches is None and isinstance(doc.get("trace"), dict):
        batches = doc["trace"].get("resourceSpans") or doc["trace"].get("batches") or []
    spans = []
    for batch in batches or []:
        res_attrs = attrs_to_dict((batch.get("resource") or {}).get("attributes"))
        service = res_attrs.get("service.name") or "unknown"
        scopes = batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []
        for scope in scopes:
            for raw in scope.get("spans") or []:
                attrs = attrs_to_dict(raw.get("attributes"))
                code, message = status_of(raw)
                start = int(raw.get("startTimeUnixNano") or 0) or None
                end = int(raw.get("endTimeUnixNano") or 0) or None
                events = []
                exception = None
                for ev in raw.get("events") or []:
                    ev_attrs = attrs_to_dict(ev.get("attributes"))
                    item = {"name": ev.get("name"), "timestamp": iso(ev.get("timeUnixNano")) if ev.get("timeUnixNano") else None}
                    if ev.get("name") == "exception" or "exception.type" in ev_attrs:
                        exception = {
                            "type": ev_attrs.get("exception.type"),
                            "message": truncate(ev_attrs.get("exception.message"), max_chars),
                            "stacktrace": truncate(ev_attrs.get("exception.stacktrace"), max_chars),
                        }
                        item["exception"] = exception
                    elif ev_attrs:
                        item["attributes"] = {k: truncate(v, 200) for k, v in list(ev_attrs.items())[:10]}
                    events.append(item)
                kept = attrs if keep_all_attrs else {k: attrs[k] for k in ATTR_KEEP if k in attrs}
                kept = {k: (truncate(v, 200) if isinstance(v, str) else v) for k, v in kept.items()}
                spans.append({
                    "span_id": norm_id(raw.get("spanId"), 8),
                    "parent_span_id": norm_id(raw.get("parentSpanId"), 8),
                    "service": service,
                    "operation": raw.get("name"),
                    "kind": kind_of(raw),
                    "start": iso(start), "end": iso(end),
                    "start_ns": start, "end_ns": end,
                    "duration_ms": round((end - start) / 1_000_000, 3) if start and end else None,
                    "status": code, "status_message": truncate(message, 300) if message else None,
                    "error": code == "ERROR",
                    "attributes": kept,
                    "exception": exception,
                    "events": events[:20],
                    "attribute_keys_total": len(attrs),
                })
    return spans


def derive_tree(spans: list) -> dict:
    by_id = {s["span_id"]: s for s in spans if s.get("span_id")}
    children: dict = {}
    roots, missing_parent = [], []
    for s in spans:
        p = s.get("parent_span_id")
        if p and p in by_id:
            children.setdefault(p, []).append(s)
        else:
            roots.append(s)
            if p:
                missing_parent.append(s["span_id"])
    for lst in children.values():
        lst.sort(key=lambda x: (x["start_ns"] or 0))
    roots.sort(key=lambda x: (x["start_ns"] or 0))
    order = []

    def walk(span, depth, guard):
        if span["span_id"] in guard:
            return
        guard.add(span["span_id"])
        span["depth"] = depth
        span["child_count"] = len(children.get(span["span_id"], []))
        order.append(span)
        for c in children.get(span["span_id"], []):
            walk(c, depth + 1, guard)

    guard: set = set()
    for r in roots:
        walk(r, 0, guard)
    for s in spans:                       # spans unreachable through cycles
        if s["span_id"] not in guard:
            s["depth"] = 0
            s["child_count"] = 0
            order.append(s)
    starts = [s["start_ns"] for s in spans if s["start_ns"]]
    ends = [s["end_ns"] for s in spans if s["end_ns"]]
    root = roots[0] if roots else None
    return {
        "ordered": order,
        "root_span": root["span_id"] if root else None,
        "trace_start_ns": min(starts) if starts else None,
        "trace_end_ns": max(ends) if ends else None,
        "spans_missing_parent": missing_parent,
        "services": sorted({s["service"] for s in spans}),
    }


def fetch_tempo(ep: dict, trace_id: str, api: str, start_s, end_s, timeout: int,
                fixture_dir, dump_dir, keep_all_attrs: bool, max_chars: int) -> dict:
    out = {"status": "skipped", "error": None, "access": ep.get("mode"), "api": api,
           "spans": [], "span_count": 0, "services": [], "root_span": None,
           "trace_start": None, "trace_end": None, "duration_ms": None,
           "errored_spans": [], "slowest_spans": [], "spans_missing_parent": []}
    try:
        if fixture_dir:
            path = Path(fixture_dir) / "tempo.json"
            if not path.exists():
                out["status"] = "not_found"
                out["note"] = "fixture has no tempo.json (simulated HTTP 404)"
                return out
            doc = json.loads(path.read_text(encoding="utf-8"))
        else:
            if ep.get("mode") == "none":
                out.update(status="error", error=ep.get("error"))
                return out
            path_part = f"/api/traces/{trace_id}" if api == "v1" else f"/api/v2/traces/{trace_id}"
            params = {}
            if start_s is not None and end_s is not None:
                params = {"start": str(int(start_s)), "end": str(int(end_s))}
            url = ep["base"] + path_part + ("?" + urllib.parse.urlencode(params) if params else "")
            status, body = http_get(url, ep["headers"], timeout)
            if status == 404:
                out["status"] = "not_found"
                out["note"] = ("trace id not found in Tempo: not sampled/exported, outside retention, "
                               "or wrong tenant. This is not evidence of a failure by itself.")
                return out
            if status != 200:
                out.update(status="error", error=describe_http_failure(status, body))
                return out
            try:
                doc = json.loads(body)
            except json.JSONDecodeError:
                out.update(status="error", error="Tempo returned a non-JSON body")
                return out
            if dump_dir:
                (Path(dump_dir) / "tempo.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
        spans = parse_otlp(doc, keep_all_attrs, max_chars)
        if not spans:
            out["status"] = "not_found"
            out["note"] = "Tempo answered but the document contains no spans"
            return out
        tree = derive_tree(spans)
        ordered = tree["ordered"]
        out.update(
            status="found", spans=ordered, span_count=len(ordered), services=tree["services"],
            root_span=tree["root_span"],
            trace_start=iso(tree["trace_start_ns"]), trace_end=iso(tree["trace_end_ns"]),
            trace_start_ns=tree["trace_start_ns"], trace_end_ns=tree["trace_end_ns"],
            duration_ms=(round((tree["trace_end_ns"] - tree["trace_start_ns"]) / 1_000_000, 3)
                         if tree["trace_start_ns"] and tree["trace_end_ns"] else None),
            errored_spans=[s["span_id"] for s in ordered if s["error"]],
            slowest_spans=[s["span_id"] for s in sorted(ordered, key=lambda s: -(s["duration_ms"] or 0))[:8]],
            spans_missing_parent=tree["spans_missing_parent"],
        )
        if tree["spans_missing_parent"]:
            out["note"] = ("some spans reference a parent that is not in the trace: the trace is partial "
                           "(the caller's span was not exported or belongs to another tenant)")
        return out
    except CollectError as exc:
        out.update(status="error", error=str(exc))
        return out
    except Exception as exc:  # never a traceback for the agent
        out.update(status="error", error=f"unexpected {type(exc).__name__}: {truncate(str(exc), 200)}")
        return out


# --------------------------------------------------------------------------- loki

def build_logql(selector: str, trace_id: str, mode: str, field: str) -> str:
    sel = selector.strip()
    if not (sel.startswith("{") and sel.endswith("}")):
        raise ValueError("LOKI_SELECTOR must be a stream selector like {env=\"prod\"}")
    if mode == "metadata":
        return f'{sel} | {field}="{trace_id}"'
    if mode == "json":
        return f'{sel} | json | {field}="{trace_id}"'
    return f'{sel} |= "{trace_id}"'


def _first(d: dict, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def norm_level(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):        # pino/bunyan numeric levels
        n = int(value)
        return "fatal" if n >= 60 else "error" if n >= 50 else "warn" if n >= 40 else "info" if n >= 30 else "debug"
    v = str(value).strip().lower()
    v = LEVEL_ALIASES.get(v, v)
    return v if v in LEVEL_ORDER else v or None


def parse_log_line(ts_ns: int, line: str, labels: dict, service_labels: list, max_chars: int) -> dict:
    ev = {"timestamp": iso(ts_ns), "ts_ns": ts_ns, "service": None, "level": None, "message": None,
          "span_id": None, "trace_id": None, "exception": None, "fields": {}, "labels": labels, "format": "text"}
    for lab in service_labels:
        if labels.get(lab):
            ev["service"] = labels[lab]
            break
    parsed = None
    stripped = line.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict):
        ev["format"] = "json"
        ev["level"] = norm_level(_first(parsed, LEVEL_KEYS))
        msg = _first(parsed, MESSAGE_KEYS)
        ev["message"] = truncate(msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False) if msg is not None else stripped, max_chars)
        ev["span_id"] = (str(_first(parsed, SPAN_KEYS)) or None) if _first(parsed, SPAN_KEYS) else None
        ev["trace_id"] = (str(_first(parsed, TRACE_KEYS)) or None) if _first(parsed, TRACE_KEYS) else None
        if not ev["service"]:
            svc = _first(parsed, SERVICE_KEYS)
            ev["service"] = str(svc) if svc else None
        exc = _first(parsed, EXC_KEYS)
        if exc:
            ev["exception"] = truncate(exc if isinstance(exc, str) else json.dumps(exc, ensure_ascii=False), max_chars)
        skip = set(LEVEL_KEYS + MESSAGE_KEYS + SPAN_KEYS + TRACE_KEYS + SERVICE_KEYS + EXC_KEYS) | {"time", "timestamp", "ts", "@timestamp"}
        fields = {}
        for k, v in parsed.items():
            if k in skip or len(fields) >= 20:
                continue
            fields[k] = truncate(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), 200)
        ev["fields"] = fields
    else:
        if not ev["level"]:
            lvl = labels.get("level") or labels.get("severity") or labels.get("detected_level")
            if not lvl:
                m = LEVEL_IN_TEXT.search(line)
                lvl = m.group(1) if m else None
            ev["level"] = norm_level(lvl)
        ev["message"] = truncate(stripped, max_chars)
        m = re.search(r"\b(?:span_id|spanId|spanID)[=:\"\s]+([0-9a-f]{16})\b", line)
        if m:
            ev["span_id"] = m.group(1)
    if not ev["level"]:
        ev["level"] = norm_level(labels.get("level") or labels.get("detected_level")) or "unknown"
    if not ev["service"]:
        ev["service"] = "unknown"
    ev["message"] = redact(ev["message"] or "")
    return ev


def dedupe(events: list) -> list:
    out = []
    for ev in events:
        if out:
            prev = out[-1]
            if (prev["service"], prev["level"], prev["message"]) == (ev["service"], ev["level"], ev["message"]):
                prev["repeat"] = prev.get("repeat", 1) + 1
                prev["last_timestamp"] = ev["timestamp"]
                continue
        ev["repeat"] = 1
        out.append(ev)
    return out


def _entries_of(page: dict):
    data = page.get("data") or {}
    for stream in data.get("result") or []:
        labels = stream.get("stream") or stream.get("metric") or {}
        for ts, line in stream.get("values") or []:
            yield int(ts), line, labels


def fetch_loki(ep: dict, query: str, start_ns: int, end_ns: int, limit: int, max_lines: int, timeout: int,
               fixture_dir, dump_dir, service_labels: list, max_chars: int) -> dict:
    out = {"status": "skipped", "error": None, "access": ep.get("mode"), "query": query,
           "window": {"start": iso(start_ns), "end": iso(end_ns)},
           "returned": 0, "pages": 0, "truncated": False, "limit_per_page": limit, "max_lines": max_lines,
           "events": [], "services": {}, "level_counts": {}}
    pages = []
    try:
        if fixture_dir:
            path = Path(fixture_dir) / "loki.json"
            if not path.exists():
                out.update(status="error", error="fixture has no loki.json")
                return out
            pages.append(json.loads(path.read_text(encoding="utf-8")))
        else:
            if ep.get("mode") == "none":
                out.update(status="error", error=ep.get("error"))
                return out
            cursor, total = start_ns, 0
            while True:
                params = {"query": query, "start": str(cursor), "end": str(end_ns),
                          "limit": str(limit), "direction": "forward"}
                url = f"{ep['base']}/loki/api/v1/query_range?{urllib.parse.urlencode(params)}"
                status, body = http_get(url, ep["headers"], timeout)
                if status != 200:
                    msg = describe_http_failure(status, body)
                    if pages:
                        out["error"] = "pagination stopped early: " + msg
                        out["truncated"] = True
                        break
                    out.update(status="error", error=msg)
                    return out
                try:
                    page = json.loads(body)
                except json.JSONDecodeError:
                    out.update(status="error", error="Loki returned a non-JSON body")
                    return out
                pages.append(page)
                entries = list(_entries_of(page))
                total += len(entries)
                if len(entries) < limit:
                    break
                max_ts = max(e[0] for e in entries)
                if max_ts <= cursor or total >= max_lines or len(pages) >= 50:
                    out["truncated"] = True
                    break
                cursor = max_ts          # inclusive: entries sharing the boundary ns are deduped below
            if dump_dir:
                merged = {"status": "success", "data": {"resultType": "streams", "result": []}}
                for p in pages:
                    merged["data"]["result"].extend((p.get("data") or {}).get("result") or [])
                (Path(dump_dir) / "loki.json").write_text(json.dumps(merged, indent=1), encoding="utf-8")
        seen, raw_events = set(), []
        for page in pages:
            for ts, line, labels in _entries_of(page):
                key = (ts, line)
                if key in seen:
                    continue
                seen.add(key)
                raw_events.append((ts, line, labels))
        raw_events.sort(key=lambda e: e[0])
        if len(raw_events) > max_lines:
            out["truncated"] = True
            raw_events = raw_events[:max_lines]
        events = [parse_log_line(ts, line, labels, service_labels, max_chars) for ts, line, labels in raw_events]
        events = dedupe(events)
        services: dict = {}
        level_counts: dict = {}
        for ev in events:
            s = services.setdefault(ev["service"], {"lines": 0, "first": ev["timestamp"], "last": ev["timestamp"], "levels": {}})
            s["lines"] += ev["repeat"]
            s["last"] = ev["last_timestamp"] if ev.get("last_timestamp") else ev["timestamp"]
            s["levels"][ev["level"]] = s["levels"].get(ev["level"], 0) + ev["repeat"]
            level_counts[ev["level"]] = level_counts.get(ev["level"], 0) + ev["repeat"]
        out.update(status="ok", pages=len(pages), returned=len(raw_events), events=events,
                   services=services, level_counts=level_counts)
        return out
    except CollectError as exc:
        out.update(status="error", error=str(exc))
        return out
    except Exception as exc:
        out.update(status="error", error=f"unexpected {type(exc).__name__}: {truncate(str(exc), 200)}")
        return out


# --------------------------------------------------------------------------- facts

def join_logs_to_spans(loki: dict, tempo: dict) -> None:
    by_id = {s["span_id"]: s for s in tempo.get("spans", []) if s.get("span_id")}
    if not by_id:
        return
    for ev in loki.get("events", []):
        sp = by_id.get(ev.get("span_id"))
        if sp:
            ev["span"] = {"service": sp["service"], "operation": sp["operation"], "status": sp["status"]}


def build_facts(trace_id: str, loki: dict, tempo: dict) -> dict:
    services = set(tempo.get("services") or []) | set((loki.get("services") or {}).keys())
    services.discard("unknown") if len(services) > 1 else None
    signals = []
    for ev in loki.get("events", []):
        if LEVEL_ORDER.get(ev["level"], -1) >= LEVEL_ORDER["error"]:
            signals.append({"kind": "log", "timestamp": ev["timestamp"], "service": ev["service"],
                            "level": ev["level"], "summary": truncate(ev["message"], 200)})
            break
    err_spans = [s for s in tempo.get("spans", []) if s["error"] and s["start_ns"]]
    if err_spans:
        # The errored span that ENDED first: in a propagation chain the origin fails before its callers do.
        first = min(err_spans, key=lambda s: (s["end_ns"] or s["start_ns"], -(s.get("depth") or 0)))
        signals.append({"kind": "span_first_to_fail", "timestamp": first["start"], "ended": first["end"],
                        "service": first["service"], "operation": first["operation"], "duration_ms": first["duration_ms"],
                        "summary": first.get("status_message") or (first.get("exception") or {}).get("message") or "status=ERROR"})
        deepest = max(err_spans, key=lambda s: (s.get("depth", 0), -(s["start_ns"] or 0)))
        signals.append({"kind": "span_deepest_error", "timestamp": deepest["start"], "service": deepest["service"],
                        "operation": deepest["operation"], "depth": deepest.get("depth"),
                        "summary": deepest.get("status_message") or (deepest.get("exception") or {}).get("message") or "status=ERROR"})
    evs = loki.get("events", [])
    foreign = [ev for ev in evs if ev.get("trace_id") and ev["trace_id"].lower() != trace_id]
    return {
        "services": sorted(services),
        "earliest_error_signals": signals,
        "first_log": {"timestamp": evs[0]["timestamp"], "service": evs[0]["service"]} if evs else None,
        "last_log": {"timestamp": evs[-1]["timestamp"], "service": evs[-1]["service"]} if evs else None,
        "log_lines_with_other_trace_id": len(foreign),
        "services_only_in_logs": sorted(set((loki.get("services") or {}).keys()) - set(tempo.get("services") or [])),
        "services_only_in_spans": sorted(set(tempo.get("services") or []) - set((loki.get("services") or {}).keys())),
    }


# --------------------------------------------------------------------------- prompt rendering

def select_events(events: list, budget: int) -> tuple:
    n = len(events)
    if n <= budget:
        return events, 0
    score = [0.0] * n
    first_seen, last_seen = {}, {}
    for i, ev in enumerate(events):
        lvl = LEVEL_ORDER.get(ev["level"], -1)
        if lvl >= LEVEL_ORDER["error"]:
            score[i] += 4
            for j in (i - 2, i - 1, i + 1, i + 2):
                if 0 <= j < n:
                    score[j] += 1
        elif lvl == LEVEL_ORDER["warn"]:
            score[i] += 2.5
        if ev.get("exception"):
            score[i] += 1
        first_seen.setdefault(ev["service"], i)
        last_seen[ev["service"]] = i
    for i in list(first_seen.values()) + list(last_seen.values()):
        score[i] += 1.5
    score[0] += 2
    score[-1] += 2
    chosen = sorted(range(n), key=lambda i: (-score[i], i))[:budget]
    chosen.sort()
    return [events[i] for i in chosen], n - budget


def render_prompt(doc: dict, budget_lines: int, budget_spans: int, out_path) -> str:
    L = []
    tid, loki, tempo, facts, win = doc["trace_id"], doc["loki"], doc["tempo"], doc["facts"], doc["window"]
    L.append(f"TRACE {tid}")
    t_desc = {"found": f"found ({tempo['span_count']} spans)", "not_found": "NOT FOUND",
              "error": f"ERROR: {tempo.get('error')}", "skipped": "skipped"}[tempo["status"]]
    l_desc = {"ok": f"ok ({loki['returned']} lines" + (", TRUNCATED" if loki["truncated"] else "") + ")",
              "error": f"ERROR: {loki.get('error')}", "skipped": "skipped"}[loki["status"]]
    L.append(f"Sources: tempo={t_desc} | loki={l_desc}")
    L.append(f"Window: {win['start']} -> {win['end']} (bounded by: {win['source']})")
    if win.get("note"):
        L.append(f"  note: {win['note']}")
    if tempo.get("note"):
        L.append(f"  tempo: {tempo['note']}")
    if loki.get("error") and loki["status"] == "ok":
        L.append(f"  loki: {loki['error']}")
    L.append("")
    L.append("SERVICES")
    svc_rows = []
    for s in facts["services"]:
        ls = (loki.get("services") or {}).get(s)
        in_spans = s in (tempo.get("services") or [])
        parts = []
        if ls:
            lv = ", ".join(f"{k}={v}" for k, v in sorted(ls["levels"].items(), key=lambda kv: -LEVEL_ORDER.get(kv[0], -1)))
            parts.append(f"logs={ls['lines']} [{lv}] {clock(parse_time(ls['first']))}->{clock(parse_time(ls['last']))}")
        else:
            parts.append("logs=0")
        parts.append("spans=yes" if in_spans else "spans=no")
        svc_rows.append(f"  {s}: " + "  ".join(parts))
    L.extend(svc_rows or ["  (none)"])
    if facts["services_only_in_spans"]:
        L.append(f"  only in spans (no log lines): {', '.join(facts['services_only_in_spans'])}")
    if facts["services_only_in_logs"] and tempo["status"] == "found":
        L.append(f"  only in logs (no spans): {', '.join(facts['services_only_in_logs'])}")
    L.append("")
    if tempo["status"] == "found":
        root = next((s for s in tempo["spans"] if s["span_id"] == tempo["root_span"]), None)
        head = f"SPAN TREE ({tempo['span_count']} spans, total {tempo['duration_ms']} ms"
        if root:
            head += f", root [{root['service']}] {root['operation']} {root['status']}"
        L.append(head + ")")
        shown = 0
        for s in tempo["spans"]:
            if shown >= budget_spans:
                L.append(f"  ... {tempo['span_count'] - shown} more spans omitted (full list in JSON)")
                break
            shown += 1
            ind = "  " * (s.get("depth", 0) + 1)
            attrs = " ".join(f"{k}={v}" for k, v in s["attributes"].items()
                             if k in ("http.status_code", "http.response.status_code", "db.system", "rpc.service", "peer.service", "error.type"))
            line = f"{ind}{clock(s['start_ns'])} [{s['service']}] {s['operation']} {s['kind']} {s['duration_ms']} ms {s['status']}"
            if attrs:
                line += f"  {attrs}"
            if s.get("status_message"):
                line += f"  msg={truncate(s['status_message'], 120)}"
            if s.get("exception"):
                ex = s["exception"]
                line += f"  exception={ex.get('type')}: {truncate(ex.get('message'), 160)}"
            if s.get("parent_span_id") and s["span_id"] in tempo["spans_missing_parent"]:
                line += "  (parent span missing from trace)"
            L.append(line)
        L.append("")
    elif tempo["status"] == "not_found":
        L.append("SPAN TREE: trace not found in Tempo (see note above). Timeline below is log-only.")
        L.append("")
    L.append("EARLIEST ERROR SIGNALS (chronological facts, not a verdict on cause)")
    if facts["earliest_error_signals"]:
        for sig in facts["earliest_error_signals"]:
            where = sig["service"] + (f" {sig['operation']}" if sig.get("operation") else "")
            when = f"failed at {sig['ended']} (started {sig['timestamp']})" if sig.get("ended") else sig["timestamp"]
            L.append(f"  {sig['kind']}: {when} [{where}] {sig['summary']}")
    else:
        L.append("  none: no error-level log line and no errored span")
    L.append("")
    events = loki.get("events", [])
    if loki["status"] == "ok":
        chosen, omitted = select_events(events, budget_lines)
        rule = "all error/warn lines, lines around errors, first/last per service, then chronological fill"
        L.append(f"LOG TIMELINE ({len(chosen)} of {len(events)} distinct lines shown; selection: {rule})")
        prev_ts = None
        for ev in chosen:
            gap = ""
            if prev_ts is not None and ev["ts_ns"] - prev_ts >= 5_000_000_000:
                gap = f"  [+{(ev['ts_ns'] - prev_ts) / 1e9:.1f}s gap]"
            prev_ts = ev["ts_ns"]
            rep = f" (x{ev['repeat']})" if ev.get("repeat", 1) > 1 else ""
            span = f"  <span {ev['span']['service']}:{ev['span']['operation']}>" if ev.get("span") else ""
            msg = truncate(ev["message"], 300)
            L.append(f"  {clock(ev['ts_ns'])} [{ev['service']}] {ev['level'].upper():5} {msg}{rep}{span}{gap}")
            if ev.get("exception"):
                L.append(f"      exception: {truncate(ev['exception'].splitlines()[0] if ev['exception'] else '', 200)}")
        if omitted:
            L.append(f"  ... {omitted} lines omitted from this cut (full timeline in JSON)")
        if facts["log_lines_with_other_trace_id"]:
            L.append(f"  warning: {facts['log_lines_with_other_trace_id']} lines carry a different trace_id (substring match); check before using them")
    L.append("")
    L.append("TRUNCATION / GAPS")
    notes = []
    if loki["status"] == "ok" and loki["truncated"]:
        notes.append(f"Loki cut at {loki['max_lines']} lines: absence of later lines is NOT evidence")
    if loki["status"] == "error":
        notes.append("Loki failed: the log side of the story is missing entirely")
    if tempo["status"] == "not_found":
        notes.append("No spans: service relationships come only from log text")
    if tempo["status"] == "found" and tempo["spans_missing_parent"]:
        notes.append("Partial trace: some spans have a parent outside the trace")
    if win["source"] == "now":
        notes.append("Window is relative to now, not to the trace: an older incident will be missing from Loki")
    if not notes:
        notes.append("none detected")
    L.extend("  " + n for n in notes)
    L.append("")
    L.append(f"FULL JSON: {out_path}")
    return "\n".join(L)


# --------------------------------------------------------------------------- doctor

def doctor(args) -> int:
    report = {"collector_version": VERSION, "fixture": None, "loki": {}, "tempo": {}, "config": {}}
    ignored = apply_config_flags(args)
    if ignored:
        report["ignored_empty_flags"] = ignored
    env = os.environ
    report["config"] = {
        "LOKI_SELECTOR": env.get("LOKI_SELECTOR") or "(missing: required for Loki)",
        "LOKI_TRACE_FILTER": env.get("LOKI_TRACE_FILTER", "substring"),
        "LOKI_TRACE_FIELD": env.get("LOKI_TRACE_FIELD", "trace_id"),
        "LOKI_SERVICE_LABELS": env.get("LOKI_SERVICE_LABELS", DEFAULT_SERVICE_LABELS),
        "TEMPO_API": env.get("TEMPO_API", "v1"),
    }
    fixture_dir = args.fixture or env.get("TRACE_DEBUG_FIXTURE_DIR")
    if fixture_dir:
        fd = Path(fixture_dir)
        report["fixture"] = {"dir": str(fd), "loki.json": (fd / "loki.json").exists(), "tempo.json": (fd / "tempo.json").exists()}
    # When Grafana is the route, list its datasources: a wrong or missing UID is the
    # most common first-run mistake, and the answer is one call away.
    grafana = env.get("GRAFANA_URL")
    if grafana and not fixture_dir:
        headers = auth_headers(env.get("GRAFANA_TOKEN") or env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN"),
                               env.get("GRAFANA_USERNAME"), env.get("GRAFANA_PASSWORD"))
        try:
            status, body = http_get(grafana.rstrip("/") + "/api/datasources", headers, args.timeout)
            if status == 200:
                found = []
                for ds in json.loads(body):
                    if ds.get("type") in ("loki", "tempo"):
                        found.append({"uid": ds.get("uid"), "type": ds.get("type"), "name": ds.get("name")})
                report["grafana_datasources"] = found or "none of type loki/tempo"
                if found:
                    report["hint"] = ("set GRAFANA_LOKI_UID / GRAFANA_TEMPO_UID to the uid values above "
                                      "that match your environment")
            else:
                report["grafana_datasources"] = describe_http_failure(status, body)
        except CollectError as exc:
            report["grafana_datasources"] = str(exc)

    for source in ("loki", "tempo"):
        ep = resolve_endpoint(source)
        entry = {"access": ep["mode"], "auth": "configured" if ep.get("auth") else "none", "check": None}
        if ep["mode"] == "none":
            entry["check"] = "not configured: " + ep["error"]
        elif fixture_dir:
            entry["check"] = "skipped (fixture mode)"
        else:
            if source == "loki":
                end = now_ns()
                start = end - 3600 * 1_000_000_000
                url = f"{ep['base']}/loki/api/v1/labels?start={start}&end={end}"
            else:
                url = f"{ep['base']}/api/echo"
            try:
                status, body = http_get(url, ep["headers"], args.timeout)
                entry["check"] = "ok" if status == 200 else describe_http_failure(status, body)
                if source == "loki" and status == 200:
                    try:
                        labels = json.loads(body).get("data") or []
                        entry["labels_sample"] = labels[:15]
                    except json.JSONDecodeError:
                        pass
            except CollectError as exc:
                entry["check"] = str(exc)
        report[source] = entry
    print(json.dumps(report, indent=2))
    ok = all(report[s]["check"] in ("ok", "skipped (fixture mode)") for s in ("loki", "tempo"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="collect-trace.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="collect", choices=["collect", "doctor"], help="collect (default) or doctor")
    p.add_argument("--trace-id", help="trace id: 16 or 32 hex chars, or a W3C traceparent")
    p.add_argument("--source", default="all", choices=["all", "loki", "tempo"])
    p.add_argument("--lookback", default="24h", help="window width when Tempo cannot bound it (default 24h)")
    p.add_argument("--start", help="explicit window start (ISO-8601 or epoch)")
    p.add_argument("--end", help="explicit window end (ISO-8601 or epoch)")
    p.add_argument("--around", help="centre the window on this time (ISO-8601 or epoch)")
    p.add_argument("--pad", default="30s", help="padding around the Tempo trace bounds (default 30s)")
    p.add_argument("--limit", type=int, default=1000, help="Loki lines per page (keep below the server's max_entries_limit_per_query)")
    p.add_argument("--max-lines", type=int, default=5000, help="stop paginating after this many lines (default 5000)")
    p.add_argument("--max-message-chars", type=int, default=800)
    p.add_argument("--prompt-lines", type=int, default=80, help="log lines in the prompt cut (default 80)")
    p.add_argument("--prompt-spans", type=int, default=60, help="spans in the prompt cut (default 60)")
    p.add_argument("--all-attributes", action="store_true", help="keep every span attribute instead of the curated set")
    p.add_argument("--format", default="prompt", choices=["prompt", "json"])
    p.add_argument("--out", help="where to write the full JSON (default: $TMPDIR/trace-debug/<trace-id>.json)")
    p.add_argument("--fixture", help="directory with loki.json / tempo.json instead of HTTP (also TRACE_DEBUG_FIXTURE_DIR)")
    p.add_argument("--dump-raw", help="directory where raw API responses are saved (to build fixtures)")
    p.add_argument("--timeout", type=int, default=int(os.environ.get("HTTP_TIMEOUT", "20")))
    cfg = p.add_argument_group(
        "configuration",
        "Each overrides the matching environment variable. Meant to be filled from the plugin's "
        "install dialog; an empty value, or one still holding an unsubstituted ${...} placeholder, "
        "is ignored rather than used. Credentials are NOT accepted here and must come from the "
        "environment, so they never appear in a command line or a process list.")
    cfg.add_argument("--grafana-url", metavar="URL", help="overrides GRAFANA_URL")
    cfg.add_argument("--grafana-loki-uid", metavar="UID", help="overrides GRAFANA_LOKI_UID")
    cfg.add_argument("--grafana-tempo-uid", metavar="UID", help="overrides GRAFANA_TEMPO_UID")
    cfg.add_argument("--selector", metavar="SEL", help="overrides LOKI_SELECTOR")
    cfg.add_argument("--trace-filter", choices=["substring", "metadata", "json"], help="overrides LOKI_TRACE_FILTER")
    cfg.add_argument("--trace-field", metavar="FIELD", help="overrides LOKI_TRACE_FIELD")
    return p


CONFIG_FLAGS = {
    "grafana_url": "GRAFANA_URL",
    "grafana_loki_uid": "GRAFANA_LOKI_UID",
    "grafana_tempo_uid": "GRAFANA_TEMPO_UID",
    "selector": "LOKI_SELECTOR",
    "trace_filter": "LOKI_TRACE_FILTER",
    "trace_field": "LOKI_TRACE_FIELD",
}


def apply_config_flags(args) -> list:
    """Fold the configuration flags into the environment the rest of the script reads.

    A value that is empty, or that still contains an unsubstituted ${...} placeholder, is
    discarded: a plugin install dialog left blank must not turn into a literal setting.
    """
    ignored = []
    for attr, var in CONFIG_FLAGS.items():
        value = getattr(args, attr, None)
        if value is None:
            continue
        value = value.strip()
        if not value or "${" in value:
            ignored.append(var)
            continue
        os.environ[var] = value
    return ignored


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return doctor(args)
    apply_config_flags(args)
    if not args.trace_id:
        print("error: --trace-id is required", file=sys.stderr)
        return 2
    try:
        trace_id = normalise_trace_id(args.trace_id)
        lookback_s = parse_duration(args.lookback)
        pad_s = parse_duration(args.pad)
        explicit = (parse_time(args.start), parse_time(args.end)) if (args.start or args.end) else None
        if explicit and (not args.start or not args.end):
            raise ValueError("--start and --end must be given together")
        around = parse_time(args.around) if args.around else None
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    env = os.environ
    fixture_dir = args.fixture or env.get("TRACE_DEBUG_FIXTURE_DIR")
    if fixture_dir and not Path(fixture_dir).is_dir():
        print(f"error: fixture directory not found: {fixture_dir}", file=sys.stderr)
        return 2
    dump_dir = args.dump_raw
    if dump_dir:
        Path(dump_dir).mkdir(parents=True, exist_ok=True)
    service_labels = [s.strip() for s in env.get("LOKI_SERVICE_LABELS", DEFAULT_SERVICE_LABELS).split(",") if s.strip()]
    filter_mode = env.get("LOKI_TRACE_FILTER", "substring").strip().lower()
    trace_field = env.get("LOKI_TRACE_FIELD", "trace_id").strip()
    tempo_api = env.get("TEMPO_API", "v1").strip().lower()
    if filter_mode not in ("substring", "metadata", "json"):
        print("error: LOKI_TRACE_FILTER must be substring, metadata or json", file=sys.stderr)
        return 2

    ns = 1_000_000_000
    want_tempo = args.source in ("all", "tempo")
    want_loki = args.source in ("all", "loki")

    # 1. Tempo first: it bounds the window.
    tempo_ep = resolve_endpoint("tempo") if want_tempo else {"mode": "none"}
    t_start_s = t_end_s = None
    if explicit:
        t_start_s, t_end_s = explicit[0] // ns, explicit[1] // ns
    elif around:
        t_start_s, t_end_s = (around - lookback_s * ns // 2) // ns, (around + lookback_s * ns // 2) // ns
    if want_tempo:
        tempo = fetch_tempo(tempo_ep, trace_id, tempo_api, t_start_s, t_end_s, args.timeout,
                            fixture_dir, dump_dir, args.all_attributes, args.max_message_chars)
    else:
        tempo = {"status": "skipped", "error": None, "spans": [], "span_count": 0, "services": [],
                 "root_span": None, "spans_missing_parent": [], "duration_ms": None}

    # 2. Window.
    if explicit:
        window = {"start_ns": explicit[0], "end_ns": explicit[1], "source": "explicit"}
    elif around:
        window = {"start_ns": around - lookback_s * ns // 2, "end_ns": around + lookback_s * ns // 2, "source": "around"}
    elif tempo.get("status") == "found" and tempo.get("trace_start_ns"):
        window = {"start_ns": tempo["trace_start_ns"] - pad_s * ns, "end_ns": tempo["trace_end_ns"] + pad_s * ns, "source": "tempo"}
    else:
        end = now_ns()
        window = {"start_ns": end - lookback_s * ns, "end_ns": end, "source": "now",
                  "note": f"no trace bounds available; using the last {args.lookback}. "
                          f"For an older incident pass --around <time> or --start/--end."}
    window["start"], window["end"] = iso(window["start_ns"]), iso(window["end_ns"])

    # 3. Loki.
    if want_loki:
        loki_ep = resolve_endpoint("loki")
        selector = env.get("LOKI_SELECTOR", "")
        if fixture_dir and not selector:
            selector = '{fixture="true"}'
        try:
            query = build_logql(selector, trace_id, filter_mode, trace_field) if selector else None
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not query:
            loki = {"status": "error", "error": "LOKI_SELECTOR is not set (e.g. {env=\"prod\"}); refusing to query all streams",
                    "events": [], "services": {}, "returned": 0, "truncated": False, "access": loki_ep.get("mode")}
        else:
            loki = fetch_loki(loki_ep, query, window["start_ns"], window["end_ns"], args.limit, args.max_lines,
                              args.timeout, fixture_dir, dump_dir, service_labels, args.max_message_chars)
    else:
        loki = {"status": "skipped", "error": None, "events": [], "services": {}, "returned": 0, "truncated": False}

    # 4. Facts.
    join_logs_to_spans(loki, tempo)
    facts = build_facts(trace_id, loki, tempo)
    doc = {
        "trace_id": trace_id, "collector_version": VERSION, "generated_at": iso(now_ns()),
        "access": {"loki": loki.get("access", "none"), "tempo": tempo.get("access", "none")},
        "fixture": str(fixture_dir) if fixture_dir else None,
        "window": {k: v for k, v in window.items() if not k.endswith("_ns")},
        "tempo": tempo, "loki": loki, "facts": facts,
    }
    for s in doc["tempo"].get("spans", []):
        s.pop("start_ns", None), s.pop("end_ns", None)
    for ev in doc["loki"].get("events", []):
        ev.pop("ts_ns", None)
    out_path = Path(args.out) if args.out else Path(os.environ.get("TMPDIR", "/tmp")) / "trace-debug" / f"{trace_id}.json"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not write {out_path}: {exc}", file=sys.stderr)
    # prompt rendering needs ns again
    for s in tempo.get("spans", []):
        s["start_ns"] = parse_time(s["start"]) if s.get("start") else None
    for ev in loki.get("events", []):
        ev["ts_ns"] = parse_time(ev["timestamp"])
    if args.format == "json":
        print(json.dumps(doc, indent=1, ensure_ascii=False))
    else:
        print(render_prompt(doc, args.prompt_lines, args.prompt_spans, out_path))
    usable = (loki.get("status") == "ok") or (tempo.get("status") in ("found", "not_found") and loki.get("status") != "error")
    return 0 if usable else 1


if __name__ == "__main__":
    sys.exit(main())
