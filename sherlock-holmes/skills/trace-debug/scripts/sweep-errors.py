#!/usr/bin/env python3
"""sweep-errors.py - list the errors of a time window, as facts, without a trace id.

Answers "were there errors between X and Y, where, how many, since when" deterministically: one
Tempo search for spans with error status (TraceQL `status = error`) and, when Loki is configured,
one query for error lines, grouped by a signature with every number, id, UUID, IP and e-mail
masked. The output is a numbered table with a fixed, documented order and a footer that says what
the data cannot show. No model runs, nothing is ranked by "severity" and no cause is stated: the
reader picks a row and chooses what to spend on - the sequence diagram of an example trace
(trace-diagram.py) or an investigation (/sherlock-holmes:trace-debug).

Read-only: HTTP GET to Tempo and Loki only, through the same access configuration as
collect-trace.py (Grafana datasource proxy or direct URLs), whose helpers it reuses.

Usage:
  sweep-errors.py (--last 2h | --start <t> --end <t>) [--service <name>] [--source all|tempo|loki]
                  [--limit 200] [--examples 3] [--log-filter substring|json] [--level-field level]
                  [--priors <file>] [--fixture <dir>] [--format table|json] [--out <file>]
                  [--grafana-url U] [--grafana-tempo-uid U] [--grafana-loki-uid U] [--selector S]

Exit codes: 0 when at least one source answered; 1 when every requested source failed; 2 for bad
arguments.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("collect_trace", HERE / "collect-trace.py")
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)

DEFAULT_LIMIT = 200          # traces per Tempo search; hitting it makes every count a floor
SPANS_PER_SPANSET = 50       # Tempo returns 3 matching spans per trace unless told otherwise
DEFAULT_EXAMPLES = 3
DEFAULT_MAX_LINES = 5000
MESSAGE_CHARS = 120
SERVICE_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")   # also what keeps TraceQL injection out
ERROR_LEVELS = ("error", "fatal")
SELECT = ("resource.service.name", "name", "kind", "statusMessage",
          "span.http.status_code", "span.http.response.status_code", "span.rpc.grpc.status_code",
          "span.http.route", "span.peer.service", "span.net.peer.name", "span.server.address",
          "span.rpc.service", "span.db.system", "span.messaging.destination.name")
CODE_KEYS = ("http.status_code", "http.response.status_code", "rpc.grpc.status_code")
CALLEE_KEYS = ("peer.service", "net.peer.name", "server.address", "rpc.service", "db.system",
               "messaging.destination.name")
NOT_COVERED = (
    "requests that were not sampled never reach Tempo, so their errors are only in the logs (if anywhere)",
    "late ingestion: data can arrive in Tempo and Loki minutes after the fact; a re-run can add rows",
    "spans without an error status (for example a 4xx the service did not flag) are not listed",
)

# Order matters: UUIDs before hex ids, e-mail and IPs before plain numbers.
_MASKS = (
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "<EMAIL>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}\b"), "<ID>"),
    (re.compile(r"\d+(?:[.,]\d+)*"), "<N>"),
)


# A status code right after one of these words is a fact about the failure, not a value that
# varies per request: "returned 503" and "returned 504" are different failure modes.
_STATUS_CODE = re.compile(r"(?i)\b((?:http|status|code|returned|responded(?: with)?)[\s:=]+)([1-5]\d\d)\b")


def _mask(segment: str) -> str:
    for rx, token in _MASKS:
        segment = rx.sub(token, segment)
    return segment


def signature(text) -> str:
    """A log message or status message with every value masked, so that one failure mode is one
    row no matter which customer, request or id it hit. Never carries the values themselves -
    except a 3-digit status code right after http/status/code/returned, which is kept."""
    s = ct.redact(str(text or "")).strip()
    pieces, last = [], 0
    for m in _STATUS_CODE.finditer(s):
        pieces += [_mask(s[last:m.start(2)]), m.group(2)]
        last = m.end(2)
    pieces.append(_mask(s[last:]))
    return ct.truncate(re.sub(r"\s+", " ", "".join(pieces)), MESSAGE_CHARS)


_OP_MASKS = (
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "<EMAIL>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}\b"), "<ID>"),
    (re.compile(r"(?<=/)\d+(?=/|$|\?)"), "<N>"),
    (re.compile(r"(?<==)[^&\s]+"), "<V>"),
)


def op_signature(name) -> str:
    """A span name with ids masked but its shape kept: `GET /customers/42` and `/customers/77` are
    one operation, while `/v1/...` and `us-east-2` stay readable (only whole path segments and query
    values are masked, not every digit)."""
    s = str(name or "?").strip()
    for rx, token in _OP_MASKS:
        s = rx.sub(token, s)
    return ct.truncate(s, MESSAGE_CHARS)


def _nil(v):
    return None if v in (None, "", "nil", "<nil>") else v


def iso_ms(ns: int | None) -> str | None:
    if ns is None:
        return None
    dt = datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def normalise_tid(raw) -> str | None:
    try:
        return ct.normalise_trace_id(str(raw))
    except (ValueError, TypeError):
        t = str(raw or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{17,31}", t):
            return t.zfill(32)
        return None


# --------------------------------------------------------------------------- window

def resolve_window(args, fixture_dir) -> dict:
    """Absolute [start, end] in ns, and how it was chosen. A bundled fixture may carry its own
    window (window.json) so that a demo is reproducible without --start/--end."""
    fw = {}
    if fixture_dir and (Path(fixture_dir) / "window.json").exists():
        fw = json.loads((Path(fixture_dir) / "window.json").read_text(encoding="utf-8"))
    if args.start or args.end:
        if not (args.start and args.end):
            raise ValueError("--start and --end go together")
        start, end, how = ct.parse_time(args.start), ct.parse_time(args.end), "explicit"
    elif args.last:
        end = ct.now_ns()
        start, how = end - ct.parse_duration(args.last) * 1_000_000_000, f"--last {args.last}"
    elif fw.get("start") and fw.get("end"):
        start, end, how = ct.parse_time(fw["start"]), ct.parse_time(fw["end"]), "fixture window"
    else:
        raise ValueError("give a window: --last <duration> or --start and --end")
    if end <= start:
        raise ValueError("the window ends before it starts")
    return {"start_ns": start, "end_ns": end, "start": iso_ms(start), "end": iso_ms(end), "chosen_by": how,
            "source": fw.get("source")}


# --------------------------------------------------------------------------- tempo

def build_traceql(service: str | None) -> str:
    cond = "status = error"
    if service:
        cond += f' && resource.service.name = "{service}"'
    return "{ " + cond + " } | select(" + ", ".join(SELECT) + ")"


def fetch_tempo_search(ep: dict, query: str, start_ns: int, end_ns: int, limit: int, timeout: int,
                       fixture_dir) -> dict:
    out = {"status": "skipped", "error": None, "access": ep.get("mode"), "query": query, "doc": None}
    try:
        if fixture_dir:
            path = Path(fixture_dir) / "tempo-search.json"
            if not path.exists():
                out.update(status="error", error="fixture has no tempo-search.json")
                return out
            out.update(status="ok", doc=json.loads(path.read_text(encoding="utf-8")))
            return out
        if ep.get("mode") == "none":
            out.update(status="error", error=ep.get("error"))
            return out
        params = {"q": query, "start": str(start_ns // 1_000_000_000), "end": str(-(-end_ns // 1_000_000_000)),
                  "limit": str(limit), "spss": str(SPANS_PER_SPANSET)}
        status, body = ct.http_get(f"{ep['base']}/api/search?{urllib.parse.urlencode(params)}", ep["headers"], timeout)
        if status != 200:
            out.update(status="error", error=ct.describe_http_failure(status, body))
            return out
        out.update(status="ok", doc=json.loads(body))
        return out
    except ct.CollectError as exc:
        out.update(status="error", error=str(exc))
    except json.JSONDecodeError:
        out.update(status="error", error="Tempo returned a non-JSON body")
    return out


def _attrs(span: dict) -> dict:
    return {a.get("key"): _nil(ct._attr_value(a.get("value"))) for a in span.get("attributes") or []}


def tempo_rows(doc: dict, limit: int, service: str | None) -> tuple[list, dict]:
    """Group the error spans of a search response by (service, operation, status code)."""
    traces = doc.get("traces") or []
    groups: dict = {}
    notes = []
    spans_total = undercounted = 0
    for tr in traces:
        tid = normalise_tid(tr.get("traceID"))
        sets = tr.get("spanSets") or ([tr["spanSet"]] if tr.get("spanSet") else [])
        for ss in sets:
            spans = ss.get("spans") or []
            if (ss.get("matched") or 0) > len(spans):
                undercounted += 1
            for sp in spans:
                a = _attrs(sp)
                if (a.get("status") or "error") != "error":
                    continue
                svc = a.get("service.name") or tr.get("rootServiceName") or "unknown"
                if service and svc != service:
                    continue
                name = op_signature(sp.get("name"))
                callee = next((a[k] for k in CALLEE_KEYS if a.get(k)), None)
                operation = name if not callee or str(callee) in name else f"{name} → {callee}"
                code = next((str(a[k]) for k in CODE_KEYS if a.get(k)), None)
                start = int(sp.get("startTimeUnixNano") or tr.get("startTimeUnixNano") or 0) or None
                dur_ms = round(int(sp.get("durationNanos") or 0) / 1e6, 1)
                key = (svc, operation, code or "")
                g = groups.setdefault(key, {"source": "tempo", "service": svc, "operation": operation,
                                            "code": code, "kind": a.get("kind"), "messages": {},
                                            "count": 0, "trace_ids": {}, "first_ns": None, "last_ns": None,
                                            "max_duration_ms": 0.0})
                g["count"] += 1
                spans_total += 1
                if a.get("statusMessage"):
                    m = signature(a["statusMessage"])
                    g["messages"][m] = g["messages"].get(m, 0) + 1
                if tid and start:
                    g["trace_ids"][tid] = max(g["trace_ids"].get(tid, 0), start)
                if start:
                    g["first_ns"] = start if g["first_ns"] is None else min(g["first_ns"], start)
                    g["last_ns"] = start if g["last_ns"] is None else max(g["last_ns"], start)
                g["max_duration_ms"] = max(g["max_duration_ms"], dur_ms)
    metrics = doc.get("metrics") or {}
    done, total = metrics.get("completedJobs"), metrics.get("totalJobs")
    truncated = len(traces) >= limit or (done is not None and total is not None and int(done) < int(total))
    if truncated:
        notes.append(f"Tempo search limit reached ({len(traces)} traces returned): every Tempo count is a floor, not a total")
    if undercounted:
        notes.append(f"{undercounted} trace(s) matched more error spans than the search returned: their span counts are a floor")
    summary = {"traces": len(traces), "spans": spans_total, "truncated": truncated, "notes": notes}
    return list(groups.values()), summary


# --------------------------------------------------------------------------- loki

def build_error_logql(selector: str, mode: str, level_field: str) -> str:
    sel = (selector or "").strip()
    if not (sel.startswith("{") and sel.endswith("}")) or sel.replace(" ", "") == "{}":
        raise ValueError('LOKI_SELECTOR must be a non-empty stream selector like {env="prod"}')
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", level_field or ""):
        raise ValueError("--level-field must be a plain field name")
    if mode == "json":
        return f'{sel} | json | {level_field}=~"(?i)(error|err|fatal|critical|crit|panic)"'
    return f'{sel} |~ "(?i)(error|fatal|panic|critical|exception)"'


def loki_rows(loki: dict, service: str | None) -> tuple[list, dict]:
    """Group error lines by (service, signature). Lines the parser reads as below error are
    dropped and counted: a substring match on "error" in an INFO line is not an error."""
    groups: dict = {}
    dropped = kept = 0
    for ev in loki.get("events") or []:
        n = ev.get("repeat") or 1
        level = ev.get("level") or "unknown"
        if level not in ERROR_LEVELS and level != "unknown":
            dropped += n
            continue
        svc = ev.get("service") or "unknown"
        if service and svc != service:
            continue
        sig = signature(ev.get("message"))
        key = (svc, sig)
        ts = ct.parse_time(ev["timestamp"]) if ev.get("timestamp") else None
        last = ct.parse_time(ev["last_timestamp"]) if ev.get("last_timestamp") else ts
        g = groups.setdefault(key, {"source": "loki", "service": svc, "operation": sig, "code": None,
                                    "kind": None, "messages": {}, "levels": {}, "count": 0, "trace_ids": {},
                                    "first_ns": None, "last_ns": None, "max_duration_ms": None})
        g["count"] += n
        kept += n
        g["levels"][level] = g["levels"].get(level, 0) + n
        tid = normalise_tid(ev.get("trace_id")) if ev.get("trace_id") else None
        if tid and last:
            g["trace_ids"][tid] = max(g["trace_ids"].get(tid, 0), last)
        if ts:
            g["first_ns"] = ts if g["first_ns"] is None else min(g["first_ns"], ts)
        if last:
            g["last_ns"] = last if g["last_ns"] is None else max(g["last_ns"], last)
    notes = []
    if loki.get("truncated"):
        notes.append("Loki result truncated at the line limit: every Loki count is a floor")
    if dropped:
        notes.append(f"{dropped} line(s) matched the error filter but parse as a lower level (for example INFO mentioning an error): not listed")
    return list(groups.values()), {"lines": kept, "dropped_below_error": dropped, "notes": notes}


# --------------------------------------------------------------------------- priors

def load_known(path: Path | None) -> list:
    """Backticked patterns under a `## Known errors` heading of the priors file. Anything else in
    the file is prose for the investigator and is ignored here."""
    if not path or not path.exists():
        return []
    pats, inside = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower().startswith("known errors")
            continue
        if inside and line.lstrip().startswith("-"):
            pats += [p.strip().lower() for p in re.findall(r"`([^`]+)`", line) if p.strip()]
    return pats


# --------------------------------------------------------------------------- assembly

def finalise(groups: list, examples: int, known: list, start_index: int) -> list:
    rows = []
    for g in groups:
        msg = None
        if g["messages"]:
            msg = sorted(g["messages"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        hay = " ".join(x for x in (g["service"], g["operation"], msg or "") if x).lower()
        rows.append({
            "source": g["source"], "service": g["service"], "operation": g["operation"], "code": g["code"],
            "kind": g.get("kind"), "message": msg, "levels": g.get("levels"),
            "count": g["count"], "traces": len(g["trace_ids"]),
            "first": iso_ms(g["first_ns"]), "last": iso_ms(g["last_ns"]),
            "max_duration_ms": g["max_duration_ms"],
            "examples": [t for t, _ in sorted(g["trace_ids"].items(), key=lambda kv: (-kv[1], kv[0]))[:examples]],
            "known": any(p in hay for p in known),
        })
    # The one ordering rule: count desc, first seen asc, then the grouping key.
    rows.sort(key=lambda r: (-r["count"], r["first"] or "~", r["service"], r["operation"], r["code"] or ""))
    for i, r in enumerate(rows, start=start_index):
        r["n"] = i
    return rows


def sweep(args) -> dict:
    ct.apply_config_flags(args)
    fixture_dir = args.fixture
    window = resolve_window(args, fixture_dir)
    source = args.source if args.source != "auto" else (window.get("source") or "all")
    if args.service and not SERVICE_RE.match(args.service):
        raise ValueError("--service must be a plain service name (letters, digits, . _ : / -)")
    known = load_known(Path(args.priors) if args.priors else Path(".claude/trace-debug/priors.md"))
    doc = {"tool": "sweep-errors", "version": VERSION, "window": {k: v for k, v in window.items() if not k.endswith("_ns")},
           "service": args.service, "fixture": str(fixture_dir) if fixture_dir else None,
           "sources": {}, "rows": [], "not_covered": list(NOT_COVERED)}
    rows_t, rows_l = [], []
    if source in ("all", "tempo"):
        ep = ct.resolve_endpoint("tempo")
        q = build_traceql(args.service)
        t = fetch_tempo_search(ep, q, window["start_ns"], window["end_ns"], args.limit, args.timeout, fixture_dir)
        entry = {"status": t["status"], "error": t["error"], "access": t["access"], "query": q}
        if t["status"] == "ok":
            groups, summary = tempo_rows(t["doc"], args.limit, args.service)
            rows_t = finalise(groups, args.examples, known, 1)
            entry.update(summary)
        doc["sources"]["tempo"] = entry
    if source in ("all", "loki"):
        ep = ct.resolve_endpoint("loki")
        entry = {"status": "error", "error": None, "access": ep.get("mode")}
        try:
            q = build_error_logql(os.environ.get("LOKI_SELECTOR", "") or ("{}" if not fixture_dir else '{fixture="true"}'),
                                  args.log_filter, args.level_field)
        except ValueError as exc:
            entry["error"] = str(exc) if not (ep.get("mode") == "none" and not fixture_dir) else ep.get("error")
            q = None
        if q:
            labels = [x.strip() for x in os.environ.get("LOKI_SERVICE_LABELS", ct.DEFAULT_SERVICE_LABELS).split(",") if x.strip()]
            lk = ct.fetch_loki(ep, q, window["start_ns"], window["end_ns"], min(1000, args.max_lines), args.max_lines,
                               args.timeout, fixture_dir, None, labels, 800)
            entry = {"status": lk["status"], "error": lk["error"], "access": lk.get("access"), "query": q}
            if lk["status"] == "ok":
                groups, summary = loki_rows(lk, args.service)
                rows_l = finalise(groups, args.examples, known, len(rows_t) + 1)
                entry.update(summary, truncated=bool(lk.get("truncated")))
        doc["sources"]["loki"] = entry
    doc["rows"] = rows_t + rows_l
    return doc


# --------------------------------------------------------------------------- rendering

def _clock(iso: str | None) -> str:
    return iso.replace("T", " ") if iso else "-"


def _dur(ms) -> str:
    if ms is None:
        return "-"
    return f"{ms / 1000:.2f}s" if ms >= 1000 else f"{ms:.0f}ms"


def _cell(text, width: int) -> str:
    return ct.truncate(str(text if text not in (None, "") else "-"), width).ljust(width)


def render_table(doc: dict) -> str:
    w = doc["window"]
    L = [f"Error sweep · window {w['start']} → {w['end']} ({w['chosen_by']}; absolute, re-run with --start/--end to reproduce)"]
    src = []
    for name, s in doc["sources"].items():
        if s["status"] != "ok":
            src.append(f"{name}=error ({s.get('error') or 'unavailable'})")
        elif name == "tempo":
            src.append(f"tempo=ok ({s['traces']} trace(s), {s['spans']} error span(s))")
        else:
            src.append(f"loki=ok ({s['lines']} error line(s))")
    L.append("Sources: " + "; ".join(src) + f" | service: {doc['service'] or 'all'}"
             + (" | recorded fixture, not a live backend" if doc.get("fixture") else ""))
    L.append("Order: count desc, then first seen. Facts only: no severity, no cause.")
    for source, title in (("tempo", "TEMPO · spans with error status, by service / operation / status code"),
                          ("loki", "LOKI · error lines, by service / message signature (values masked)")):
        if source not in doc["sources"]:
            continue
        rows = [r for r in doc["rows"] if r["source"] == source]
        L.append("")
        L.append(title)
        if doc["sources"][source]["status"] != "ok":
            L.append("  (source unavailable - see Sources)")
            continue
        if not rows:
            L.append("  no errors recorded in this window" + (" for this service" if doc["service"] else "")
                     + " - which says nothing about what was not recorded (see Not covered)")
            continue
        if source == "tempo":
            L.append(f"  {'#':>3}  {'service':<24} {'operation':<40} {'code':<5} {'n':>5} {'traces':>6}  {'first':<24} {'last':<24} {'max':>8}")
        else:
            L.append(f"  {'#':>3}  {'service':<24} {'signature':<52} {'n':>5} {'traces':>6}  {'first':<24} {'last':<24}")
        for r in rows:
            mark = " known" if r["known"] else ""
            if source == "tempo":
                L.append(f"  {r['n']:>3}  {_cell(r['service'], 24)} {_cell(r['operation'], 40)} {_cell(r['code'], 5)} "
                         f"{r['count']:>5} {r['traces']:>6}  {_clock(r['first']):<24} {_clock(r['last']):<24} {_dur(r['max_duration_ms']):>8}{mark}")
                if r["message"]:
                    L.append(f"       message: {r['message']}")
            else:
                L.append(f"  {r['n']:>3}  {_cell(r['service'], 24)} {_cell(r['operation'], 52)} "
                         f"{r['count']:>5} {r['traces']:>6}  {_clock(r['first']):<24} {_clock(r['last']):<24}{mark}")
                if len(r["operation"]) > 52:
                    L.append(f"       signature: {r['operation']}")
            if r["examples"]:
                L.append(f"       traces: {' '.join(r['examples'])}")
    L.append("")
    L.append("Not covered:")
    notes = []
    for s in doc["sources"].values():
        notes += s.get("notes") or []
    for n in notes + doc["not_covered"]:
        L.append(f"  - {n}")
    if any(r["examples"] for r in doc["rows"]):
        L.append("")
        L.append('Next: "diagram <#>" draws the first example trace of that row; "investigate <#>" hands it to '
                 "/sherlock-holmes:trace-debug.")
    return "\n".join(L)


def default_out(doc: dict) -> Path:
    w = doc["window"]
    stamp = lambda s: re.sub(r"[^0-9TZ]", "", s or "")
    return Path(os.environ.get("TMPDIR", "/tmp")) / "trace-debug" / f"sweep-{stamp(w['start'])}-{stamp(w['end'])}.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--last", help="window ending now, e.g. 30m, 2h, 1d")
    p.add_argument("--start", help="window start (ISO-8601 or epoch)")
    p.add_argument("--end", help="window end (ISO-8601 or epoch)")
    p.add_argument("--service", help="only this service (resource.service.name / the Loki service label)")
    p.add_argument("--source", default="auto", choices=["auto", "all", "tempo", "loki"],
                   help="auto = all, or what a fixture's window.json says")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="traces per Tempo search")
    p.add_argument("--examples", type=int, default=DEFAULT_EXAMPLES, help="example trace ids per row")
    p.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES, help="Loki lines to read at most")
    p.add_argument("--log-filter", default="substring", choices=["substring", "json"],
                   help="how error lines are selected in Loki")
    p.add_argument("--level-field", default="level", help="level field for --log-filter json")
    p.add_argument("--priors", help="priors file (default .claude/trace-debug/priors.md); only its '## Known errors' section is used")
    p.add_argument("--fixture", help="directory with tempo-search.json / loki.json / window.json instead of live backends")
    p.add_argument("--format", default="table", choices=["table", "json"])
    p.add_argument("--out", help="where to write the full JSON (default $TMPDIR/trace-debug/sweep-<start>-<end>.json)")
    p.add_argument("--timeout", type=int, default=int(os.environ.get("HTTP_TIMEOUT", "30")))
    cfg = p.add_argument_group("configuration (same as collect-trace.py; blank or ${...} values are ignored)")
    cfg.add_argument("--grafana-url", metavar="URL")
    cfg.add_argument("--grafana-loki-uid", metavar="UID")
    cfg.add_argument("--grafana-tempo-uid", metavar="UID")
    cfg.add_argument("--selector", metavar="SEL")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 1 or args.examples < 0 or args.max_lines < 1:
        print("error: --limit, --max-lines must be >= 1 and --examples >= 0", file=sys.stderr)
        return 2
    try:
        doc = sweep(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else default_out(doc)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        doc["json_path"] = str(out)
    except OSError as exc:
        print(f"warning: could not write {out}: {exc}", file=sys.stderr)
    print(json.dumps(doc, indent=1, ensure_ascii=False) if args.format == "json" else render_table(doc))
    ok = any(s["status"] == "ok" for s in doc["sources"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
