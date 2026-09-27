#!/usr/bin/env python3
"""window-map.py - the calls of a time window, as one aggregated sequence diagram (archify).

Takes the same window as sweep-errors.py, samples the traces that had an error span in it (the same
TraceQL search), downloads each one whole, and aggregates every call the traces recorded - caller,
callee and masked operation - into one sequence diagram: services are participants, each kind of
call is one arrow labelled with how many times it happened in the sample and how many of those had
error status, arrows in order of first occurrence. Nothing is inferred: an arrow exists because
spans recorded that call, the counts are counts of spans, and no cause is drawn.

Why a sequence and not a node-and-edge graph: archify places sequence participants and messages by
rule (column, then time), while its data-flow and architecture diagrams need per-edge routing
decisions (ports, channels, label segments) to pass their clean-route checks - the general-purpose
auto-layout archify keeps out of scope. A sequence keeps this script deterministic. (D22)

Usage:
  window-map.py (--last 2h | --start <t> --end <t>) [--service <name>] [--sample 50]
                [--fixture <dir>] [--out <file.html>] [--quality showcase|standard] [--json]
                [--grafana-url U] [--grafana-tempo-uid U]

Exit codes: 0 for every map outcome (generated, skipped, failed) - it never fails the sweep it
complements; 2 for bad arguments.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ct = _load("collect_trace", "collect-trace.py")
sw = _load("sweep_errors", "sweep-errors.py")
td = _load("trace_diagram", "trace-diagram.py")

DEFAULT_SAMPLE = 50
MAX_ARROWS = 40
LABEL_CHARS = 52


# --------------------------------------------------------------------------- collection

def sample_trace_ids(search_doc: dict, sample: int) -> tuple[list, int]:
    """Trace ids of the search, newest first, capped. Returns (sample, total seen)."""
    seen = {}
    for tr in search_doc.get("traces") or []:
        tid = sw.normalise_tid(tr.get("traceID"))
        if tid:
            seen[tid] = max(seen.get(tid, 0), int(tr.get("startTimeUnixNano") or 0))
    ordered = [t for t, _ in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))]
    return ordered[:sample], len(ordered)


def fetch_traces(ep: dict, ids: list, window: dict, timeout: int, fixture_dir) -> tuple[list, list]:
    """Whole traces by id. Fixture mode reads <fixture>/traces/<id>/tempo.json."""
    found, missing = [], []
    for tid in ids:
        fdir = None
        if fixture_dir:
            fdir = Path(fixture_dir) / "traces" / tid
        t = ct.fetch_tempo(ep, tid, os.environ.get("TEMPO_API", "v1"),
                           window["start_ns"] // 1_000_000_000 - 3600, window["end_ns"] // 1_000_000_000 + 3600,
                           timeout, fdir, None, False, 800)
        if t["status"] == "found":
            found.append((tid, t))
        else:
            missing.append((tid, t.get("error") or t.get("note") or t["status"]))
    return found, missing


# --------------------------------------------------------------------------- aggregation

def calls_of(tid: str, tempo: dict, agg: dict, stats: dict) -> None:
    """Add the calls one trace recorded to `agg`, keyed by (caller, callee, operation).

    Same reading of spans as trace-diagram.py: a CLIENT span is a call to its SERVER child's service
    or, without one, to the destination its attributes name (peer_of); an outbound span nested in a
    drawn outbound span is the same call one layer down and is folded; PRODUCER/CONSUMER spans are
    sends to / deliveries from the named queue or topic. A SERVER span whose parent is not in the
    trace was called from outside it: counted, never drawn as an invented caller.
    """
    spans = [s for s in tempo.get("spans") or [] if s.get("span_id")]
    by_id = {s["span_id"]: s for s in spans}
    server_child = {}
    for s in spans:
        if s.get("kind") == "SERVER" and s.get("parent_span_id") in by_id:
            server_child.setdefault(s["parent_span_id"], s)
    drawn = set()
    for s in spans:
        a = s.get("attributes") or {}
        kind, svc = s.get("kind"), s.get("service") or "unknown"
        start = s.get("start_ns") or (ct.parse_time(s["start"]) if s.get("start") else None)
        if s.get("error") and kind in ("SERVER", "INTERNAL"):
            stats["errored_services"].add(svc)
        if kind == "SERVER" and s.get("parent_span_id") not in by_id:
            stats["entries"][svc] = stats["entries"].get(svc, 0) + 1
            continue
        if kind == "CLIENT":
            parent = by_id.get(s.get("parent_span_id") or "")
            if parent and parent.get("kind") in ("CLIENT", "PRODUCER") and parent["span_id"] in drawn:
                stats["folded"] += 1
                continue
            child = server_child.get(s["span_id"])
            if child:
                callee, ptype, sub = child["service"], "backend", None
            else:
                callee, ptype, sub = td.peer_of(a)
            if not callee:
                stats["unnamed"] += 1
                continue
            drawn.add(s["span_id"])
            error = bool(s.get("error") or (child or {}).get("error"))
            code = td._status_code(child or {}, s)
            key = (svc, callee, sw.op_signature(s.get("operation")))
            _add(agg, key, "call", ptype, sub, error, code, tid, start)
        elif kind in ("PRODUCER", "CONSUMER"):
            bus = td._first(a, td.BUS_KEYS) or td._first(a, td.PEER_KEYS)
            if not bus:
                stats["unnamed"] += 1
                continue
            drawn.add(s["span_id"])
            key = (svc, bus, sw.op_signature(s.get("operation"))) if kind == "PRODUCER" else (bus, svc, sw.op_signature(s.get("operation")))
            _add(agg, key, "publish" if kind == "PRODUCER" else "consume", "messagebus", None,
                 bool(s.get("error")), None, tid, start)


def _add(agg, key, kind, ptype, sub, error, code, tid, start):
    g = agg.setdefault(key, {"kind": kind, "ptype": ptype, "sub": sub, "count": 0, "errors": 0,
                             "codes": {}, "traces": set(), "first_ns": None})
    g["count"] += 1
    if error:
        g["errors"] += 1
        if code and str(code) not in ("nil", "<nil>", "None"):
            g["codes"][str(code)] = g["codes"].get(str(code), 0) + 1
    g["traces"].add(tid)
    if start:
        g["first_ns"] = start if g["first_ns"] is None else min(g["first_ns"], start)


# --------------------------------------------------------------------------- specification

def arrow_label(key, g) -> str:
    _, _, op = key
    label = f"{g['count']}× {op}"
    if g["errors"]:
        codes = ",".join(sorted(g["codes"])) if g["codes"] else ""
        label += f" · {g['errors']} err" + (f" {codes}" if codes else "")
    return td.clean(label, LABEL_CHARS)


def build_map(agg: dict, stats: dict, window: dict, n_search: int, n_drawn: int, missing: list,
              quality: str = "showcase", compact: bool = False) -> dict | None:
    if not agg:
        return None
    items = sorted(agg.items(), key=lambda kv: (kv[1]["first_ns"] or 0, kv[0]))
    omitted = 0
    if len(items) > MAX_ARROWS:
        keep = sorted(items, key=lambda kv: (-kv[1]["errors"], -kv[1]["count"], kv[0]))[:MAX_ARROWS]
        omitted = len(items) - len(keep)
        items = sorted(keep, key=lambda kv: (kv[1]["first_ns"] or 0, kv[0]))

    # participants in order of first appearance, caller before callee
    order, ptypes, subs = [], {}, {}
    for (caller, callee, _), g in items:
        for name, ptype, sub in ((caller, "messagebus" if g["kind"] == "consume" else "backend", None),
                                 (callee, g["ptype"] if g["kind"] != "consume" else "backend", g["sub"] if g["kind"] != "consume" else None)):
            if name not in ptypes:
                order.append(name)
                ptypes[name], subs[name] = ptype, sub
    width, chars = td.canvas_and_label_cap(len(order), compact)
    taken, ids, participants = set(), {}, []
    for name in order:
        ids[name] = td.slug(name, taken)
        label, auto_sub = td.participant_label(name, chars)
        sub = td.clean(subs[name], chars) if subs[name] else auto_sub
        participants.append({"id": ids[name], "type": ptypes[name], "label": label, **({"sublabel": sub} if sub else {})})

    messages = []
    for i, ((caller, callee, op), g) in enumerate(items):
        m = {"from": ids[caller], "to": ids[callee], "y": td.Y0 + i * td.STEP, "label": arrow_label((caller, callee, op), g),
             "variant": "security" if g["errors"] else ("dashed" if g["kind"] != "call" else "default"),
             "note": f"{len(g['traces'])} trace(s) · first {sw.iso_ms(g['first_ns'])[11:23] if g['first_ns'] else '?'}"}
        messages.append(m)
    last_y = td.Y0 + td.STEP * max(len(messages) - 1, 0)

    cards = [{"dot": "cyan", "title": "FACT · window", "items": [
        f"{window['start'][:19]}Z → {window['end'][:19]}Z",
        f"{n_search} trace(s) with an error span · {n_drawn} drawn",
        f"{sum(g['count'] for _, g in items)} call(s) in {len(items)} kind(s)"]},
        {"dot": "cyan", "title": "FACT · how to read", "items": [
        "one arrow per kind of call recorded in those traces",
        "n× calls, k err = spans with error status",
        "order = first seen; calls of error-free traces are not here"]}]
    unknown = []
    ent = stats["entries"]
    if ent:
        unknown.append("called from outside the traces: " + ", ".join(f"{k} ×{v}" for k, v in sorted(ent.items()))[:90])
    if missing:
        unknown.append(f"{len(missing)} sampled trace(s) not returned by Tempo")
    if stats["unnamed"]:
        unknown.append(f"{stats['unnamed']} outbound span(s) without a named destination")
    alone = sorted(stats["errored_services"] - set(order))
    if alone:
        unknown.append("errors inside a service, no call recorded: " + ", ".join(alone)[:80])
    if unknown:
        cards.append({"dot": "orange", "title": "UNKNOWN · not in this map", "items": unknown})
    policy = []
    if n_search > n_drawn + len(missing):
        policy.append(f"sample: {n_drawn + len(missing)} newest of {n_search} error traces")
    if stats["folded"]:
        policy.append(f"{stats['folded']} nested outbound span(s) folded into their call")
    if omitted:
        policy.append(f"{omitted} call kind(s) omitted: most errors, then most calls, kept")
    if policy:
        cards.append({"dot": "cyan", "title": "FACT · not drawn, on purpose", "items": policy})

    err_parts = []
    for (caller, callee, _), g in items:
        if g["errors"]:
            for p in (ids[caller], ids[callee]):
                if p not in err_parts:
                    err_parts.append(p)
    views = [{"id": "all-calls", "label": "All calls in the sample", "focus": [p["id"] for p in participants][:6],
              "note": "Every kind of call the sampled traces recorded, in order of first occurrence."}]
    if err_parts:
        views.append({"id": "calls-with-errors", "label": "Calls with error status", "focus": err_parts[:6],
                      "note": "Arrows with errors. Sharing a window is a fact; being related is the investigator's call."})
    return {
        "schema_version": td.SCHEMA_VERSION, "diagram_type": "sequence",
        "meta": {"title": td.clean(f"Window map · {window['start'][:16].replace('T', ' ')} → {window['end'][11:16]} UTC", 80),
                 "subtitle": "evidence only: calls as recorded in traces with an error span; no causal claim",
                 "viewBox": [width, last_y + 300], "animation": "trace", "quality_profile": quality,
                 "column_fit": "spread", "views": views,
                 "legend": td.legend({"default": "call", "security": "has errors", "dashed": "queue"})},
        "participants": participants, "messages": messages, "activations": [], "cards": cards,
    }


# --------------------------------------------------------------------------- main

def window_map(args) -> dict:
    ct.apply_config_flags(args)
    fixture = args.fixture
    window = sw.resolve_window(args, fixture)
    if args.service and not sw.SERVICE_RE.match(args.service):
        raise ValueError("--service must be a plain service name (letters, digits, . _ : / -)")
    result = {"status": None, "html": None, "reason": None, "window": {k: v for k, v in window.items() if not k.endswith("_ns")},
              "search_traces": 0, "drawn_traces": 0, "missing_traces": 0, "arrows": 0, "quality": None, "version": VERSION}
    ep = ct.resolve_endpoint("tempo")
    search = sw.fetch_tempo_search(ep, sw.build_traceql(args.service), window["start_ns"], window["end_ns"],
                                   sw.DEFAULT_LIMIT, args.timeout, fixture)
    if search["status"] != "ok":
        result.update(status="failed", reason=f"Tempo search: {search['error']}")
        return result
    ids, total = sample_trace_ids(search["doc"], args.sample)
    result["search_traces"] = total
    if not ids:
        result.update(status="skipped", reason="no trace with an error span in this window: nothing to map")
        return result
    found, missing = fetch_traces(ep, ids, window, args.timeout, fixture)
    agg, stats = {}, {"entries": {}, "folded": 0, "unnamed": 0, "errored_services": set()}
    for tid, t in found:
        calls_of(tid, t, agg, stats)
    result.update(drawn_traces=len(found), missing_traces=len(missing))
    spec = build_map(agg, stats, window, total, len(found), missing, args.quality)
    if spec is None:
        result.update(status="skipped", reason="the sampled traces recorded no call between services: nothing to map")
        return result
    result["arrows"] = len(spec["messages"])
    names = {pp["id"]: pp["label"] for pp in spec["participants"]}
    result["arrow_lines"] = [f"{names[m['from']]} → {names[m['to']]}   {m['label']}" for m in spec["messages"]]
    result["not_in_map"] = [i for c in spec["cards"] if c["title"].startswith("UNKNOWN") for i in c["items"]]
    result["on_purpose"] = [i for c in spec["cards"] if c["title"] == "FACT · not drawn, on purpose" for i in c["items"]]
    out = Path(args.out) if args.out else sw.default_out({"window": result["window"]}).with_suffix(".map.html")
    status = td.archify_status(args.archify)
    seq_path = out.with_suffix(".sequence.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    seq_path.write_text(json.dumps(spec, indent=1, ensure_ascii=False), encoding="utf-8")
    result["sequence"] = str(seq_path)
    if not status["available"]:
        result.update(status="skipped", reason=status["reason"])
        return result
    last = ""
    for q, compact in [(args.quality, False)] + ([("standard", False)] if args.quality != "standard" else []) + [("standard", True)]:
        s = build_map(agg, stats, window, total, len(found), missing, q, compact) if compact else spec
        s["meta"]["quality_profile"] = q
        seq_path.write_text(json.dumps(s, indent=1, ensure_ascii=False), encoding="utf-8")
        r = td.deliver(Path(status["archify"]), status["node"], seq_path, out, q)
        if r["ok"]:
            result.update(status="generated", html=str(out), quality=q)
            return result
        last = r["error"]
    result.update(status="failed", reason=f"archify rejected the map: {last}")
    return result


def summary_line(r: dict) -> str:
    """The text a conversation relays. Everything worth saying about the map is here, so nobody has
    to open the HTML to describe it - and a sweep row count is not mistaken for an arrow count."""
    if r["status"] != "generated":
        return f"map: not generated - {r['reason']}"
    L = [f"map: generated {r['html']} ({r['arrows']} kind(s) of call from {r['drawn_traces']} trace(s) with errors; "
         f"quality {r['quality']}; evidence only: calls as recorded, no causal claim)"]
    L += [f"  {i}. {a}" for i, a in enumerate(r.get("arrow_lines") or [], start=1)]
    if r.get("not_in_map"):
        L.append("  not in this map: " + " · ".join(r["not_in_map"]))
    if r.get("on_purpose"):
        L.append("  not drawn, on purpose: " + " · ".join(r["on_purpose"]))
    L.append("  reading: one arrow per kind of call, counted once; a sweep row counts one side of it (the caller's or "
             "the callee's span), so rows outnumber arrows")
    return "\n".join(L)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--last"); p.add_argument("--start"); p.add_argument("--end")
    p.add_argument("--service")
    p.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, help="error traces to download, newest first")
    p.add_argument("--fixture", help="window fixture: tempo-search.json, window.json and traces/<id>/tempo.json")
    p.add_argument("--out", help="HTML to write (default $TMPDIR/trace-debug/sweep-<start>-<end>.map.html)")
    p.add_argument("--quality", default="showcase", choices=["showcase", "standard"])
    p.add_argument("--archify", help="path to archify.mjs (overrides ARCHIFY_BIN and discovery)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=int, default=int(os.environ.get("HTTP_TIMEOUT", "30")))
    cfg = p.add_argument_group("configuration (same as collect-trace.py)")
    cfg.add_argument("--grafana-url", metavar="URL"); cfg.add_argument("--grafana-tempo-uid", metavar="UID")
    args = p.parse_args(argv)
    if args.sample < 1:
        print("error: --sample must be >= 1", file=sys.stderr)
        return 2
    try:
        r = window_map(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=2) if args.json else summary_line(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
