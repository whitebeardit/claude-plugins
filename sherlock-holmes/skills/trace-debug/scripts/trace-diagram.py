#!/usr/bin/env python3
"""trace-diagram.py - render the collected trace as an interactive sequence diagram.

Reads the full JSON that collect-trace.py wrote and, when Tempo returned the trace, writes an
archify `sequence` specification next to it and asks the archify CLI to validate and deliver one
self-contained HTML file. Deterministic and evidence-only: every participant, message, activation
and card comes from spans as recorded, plus log lines the collector already joined to spans.
Nothing is inferred and no cause is drawn. The agent never authors the diagram.

archify (https://github.com/tt-a1i/archify, MIT, 2.17 or 3.x) is an optional runtime dependency:
Node >= 18 and the archify skill installed (`npx skills add tt-a1i/archify -g`, which itself needs
Node >= 22), or ARCHIFY_BIN pointing at its
`archify.mjs`. Without it the investigation is unchanged; this script only says why the diagram was
not generated. It never reaches the network: the archify update check is disabled for the call.

Usage:
  trace-diagram.py --input /tmp/trace-debug/<trace-id>.json [--out <file.html>]
                   [--quality showcase|standard] [--max-messages N] [--archify <archify.mjs>] [--json]
  trace-diagram.py doctor [--archify <archify.mjs>] [--json]

Exit codes: 0 for any diagram outcome (generated, skipped, failed) - a diagram must never fail an
investigation; 2 for bad arguments or an unreadable input.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VERSION = "0.1.0"
SCHEMA_VERSION = 1
MAX_MESSAGES = 40            # above this the diagram stops being readable; see cap()
Y0, STEP = 170, 44           # archify requires messages[].y >= 160
VIEWBOX_WIDTH = 900
# archify's sequence schema: at least 2 participants and 1 message, and a viewBox at least 480 high.
MIN_PARTICIPANTS, MIN_MESSAGES, MIN_VIEWBOX_H = 2, 1, 480


def utf8_stdio() -> None:
    """Windows consoles and pipes default to a legacy code page (cp1252) that cannot print the
    arrows and dots in these reports; force UTF-8 so the output never raises UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def work_dir() -> Path:
    """Where reports and diagrams go by default: $TMPDIR/trace-debug, or the OS temp dir (Windows)."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / "trace-debug"


def output_name(html_path: Path) -> str:
    """archify 3 requires meta.output: the HTML file name, as a portable relative path. archify 2.17
    accepts it too. The command line still says where the file goes."""
    return html_path.name


def drawable(seq: dict) -> str | None:
    """None when archify's sequence schema can take `seq`, else the reason it cannot, in words."""
    n_parts, n_msgs = len(seq.get("participants") or []), len(seq.get("messages") or [])
    if n_parts >= MIN_PARTICIPANTS and n_msgs >= MIN_MESSAGES:
        return None
    return (f"nothing to draw as a sequence: {n_parts} participant(s) and {n_msgs} call(s) between them "
            f"(archify needs at least {MIN_PARTICIPANTS} and {MIN_MESSAGES}); the span tree above is the whole trace")


# Measured on archify 2.17 (2026-09-27): the participant box is ~viewBox/n - 47px, a label costs
# ~6.8px per character, and the showcase profile refuses a viewBox wider than ~1050px (it must fit
# a 1440px desktop at >= 0.85 scale). So the canvas grows 210px per participant up to that ceiling,
# and the label cap adapts to the box that is left. Standard quality has no width ceiling.
PARTICIPANT_WIDTH = 210
SHOWCASE_MAX_WIDTH = 1050
BOX_MARGIN_PX = 47
PX_PER_CHAR = 6.8
MIN_NODE_MAJOR = 18
ARCHIFY_TIMEOUT_S = 120
LABEL_CHARS = 48
NOTE_CHARS = 120

# The HTML travels further than the JSON (it is made to be shared), so labels are built only from
# these attributes. Everything else - db.statement, http.url with ids, headers, message bodies -
# stays in the JSON, which the agent reads with Read when it needs the detail.
PEER_KEYS = ("peer.service", "net.peer.name", "server.address")
RPC_KEYS = ("rpc.service",)   # AWS SDK / gRPC spans name the callee here and record no host
PARTICIPANT_CHARS = 24        # upper cap; the real cap per diagram comes from canvas_and_label_cap()
COMPACT_CHARS = 16            # last-resort pass when the layout check still rejects a label
BUS_KEYS = ("messaging.destination.name", "messaging.destination", "messaging.system")
STATUS_KEYS = ("http.status_code", "http.response.status_code", "rpc.grpc.status_code")
DB_KEY = "db.system"
LOG_LEVELS_AS_NOTES = ("warn", "warning", "error", "fatal", "critical")

# Kept identical to collect-trace.py (a unit test asserts it) so that the two scripts redact alike.
def truncate(text, n: int) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= n else s[: n - 1] + "…"


def redact(text: str) -> str:
    return re.sub(r"(?i)(authorization|password|token)(=|:\s*)(\S+)", r"\1\2***", text or "")


def clean(text, n: int = LABEL_CHARS) -> str:
    return truncate(redact(str(text)), n)


def clock(iso: str | None) -> str:
    return iso[11:23] if iso and len(iso) >= 23 else (iso or "?")


def slug(name: str, taken: set) -> str:
    """archify ids must match ^[a-zA-Z][a-zA-Z0-9_-]*$; service names need not."""
    s = re.sub(r"[^a-zA-Z0-9_-]", "-", name or "unknown").strip("-") or "unknown"
    if not re.match(r"[a-zA-Z]", s):
        s = "p-" + s
    base, n = s, 2
    while s in taken:
        s = f"{base}-{n}"
        n += 1
    taken.add(s)
    return s


# --------------------------------------------------------------------------- specification

def _first(attrs: dict, keys) -> str | None:
    for k in keys:
        v = attrs.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def peer_of(attrs: dict) -> tuple[str | None, str | None, str | None]:
    """Callee of an outbound span: (name, participant type, sublabel).

    A named peer first; else the RPC service (the AWS SDK emits `DynamoDB.GetItem`
    spans with `rpc.service` and no host, and puts the host on a nested HTTP span);
    else the database system alone. Nothing else is ever used as a name.
    """
    named = _first(attrs, PEER_KEYS)
    if named:
        return named, ("database" if attrs.get(DB_KEY) else "backend"), (attrs.get(DB_KEY) or None)
    rpc = _first(attrs, RPC_KEYS)
    if rpc:
        ptype = "database" if attrs.get(DB_KEY) else ("cloud" if attrs.get("rpc.system") == "aws-api" else "backend")
        return rpc, ptype, (attrs.get(DB_KEY) or attrs.get("rpc.system") or None)
    if attrs.get(DB_KEY):
        return str(attrs[DB_KEY]), "database", None
    return None, None, None


def canvas_and_label_cap(n_participants: int, compact: bool = False) -> tuple[int, int]:
    """(viewBox width, max label chars) for n participants.

    Normal: 210px per participant, never below 900 and never above the showcase ceiling; the label
    cap is whatever fits the resulting box. Compact (last resort, standard quality only): shorter
    labels and a canvas 1.5x wider, past the showcase ceiling since standard does not check it.
    """
    n = max(1, n_participants)
    if compact:
        return int(max(VIEWBOX_WIDTH, PARTICIPANT_WIDTH * n) * 1.5), COMPACT_CHARS
    width = max(VIEWBOX_WIDTH, min(SHOWCASE_MAX_WIDTH, PARTICIPANT_WIDTH * n))
    box = width / n - BOX_MARGIN_PX
    return width, max(COMPACT_CHARS, min(PARTICIPANT_CHARS, int(box / PX_PER_CHAR)))


def participant_label(name: str, chars: int = PARTICIPANT_CHARS) -> tuple[str, str | None]:
    """Fit the participant box: a long hostname keeps its first DNS label and moves the rest
    to the sublabel (`dynamodb` / `us-east-2.amazonaws.com`). Anything else is truncated."""
    if len(name) > chars and "." in name and " " not in name:
        head, _, tail = name.partition(".")
        return clean(head, chars), clean(tail, chars)
    return clean(name, chars), None


def _status_code(*spans) -> str | None:
    for s in spans:
        v = _first(s.get("attributes") or {}, STATUS_KEYS)
        if v:
            return v
    return None


# Error semantics, as recorded (D23). archify has no free colour: its `security` message variant is the
# one red in every preset, and the legend may rename it - message variants are visual keys, not lens
# facts. The call itself stays neutral; its outcome carries the colour. A span's own error status wins;
# otherwise the status code class decides (OpenTelemetry: a 4xx is an error for the caller only).
LEGEND_LABELS = {                    # short: archify under-measures legend labels, long ones overlap
    "default": "call",
    "return": "ok",
    "security": "5xx / error",
    "emphasis": "4xx",
    "dashed": "queue",
}


def outcome_variant(error: bool, code) -> str:
    c = str(code or "")
    if error or (len(c) == 3 and c.startswith("5")):
        return "security"
    if len(c) == 3 and c.startswith("4"):
        return "emphasis"
    return "return"


def legend(labels: dict = LEGEND_LABELS) -> dict:
    return {"mode": "auto", "entries": {k: {"label": v} for k, v in labels.items()}}


def _error_path(spans: list, by_id: dict) -> set:
    """Spans with error status plus every ancestor: the path a reader must be able to follow."""
    keep = set()
    for s in spans:
        if not s.get("error"):
            continue
        cur = s
        guard = 0
        while cur and guard < 64:
            keep.add(cur["span_id"])
            cur = by_id.get(cur.get("parent_span_id"))
            guard += 1
    return keep


def build_sequence(doc: dict, max_messages: int = MAX_MESSAGES, quality: str = "showcase",
                   compact: bool = False) -> dict | None:
    """Pure: collector document -> archify sequence specification, or None when there are no spans.

    `compact` is the last-resort layout: shorter participant labels and a wider canvas, used only
    after archify rejected the normal spec (its participant box shrinks with the participant count).
    """
    tempo = doc.get("tempo") or {}
    spans = [s for s in (tempo.get("spans") or []) if s.get("span_id")]
    if not spans:
        return None
    by_id = {s["span_id"]: s for s in spans}
    server_child = {}                       # client span id -> its SERVER child (the callee)
    for s in spans:
        if s.get("kind") == "SERVER" and s.get("parent_span_id") in by_id:
            server_child.setdefault(s["parent_span_id"], s)

    # participants, in order of first appearance in the (chronological, depth-first) span list
    ids: dict = {}                          # display name -> id
    taken: set = set()
    parts: list = []

    def participant(name: str, ptype: str, sub: str | None = None) -> str:
        if name not in ids:
            ids[name] = slug(name, taken)
            parts.append({"id": ids[name], "type": ptype, "name": name, "sub": sub})   # labelled at the end
        return ids[name]

    for s in spans:
        participant(s["service"], "backend")

    # events: one per message, chronological; role/span keep call+return together for the cap
    events: list = []
    unnamed = folded = 0
    drawn_client: set = set()
    for s in spans:                          # tree order: a parent comes before its children
        a = s.get("attributes") or {}
        kind = s.get("kind")
        dur = s.get("duration_ms")
        dur_txt = f"{dur:.0f} ms" if isinstance(dur, (int, float)) else "? ms"
        if kind == "CLIENT":
            # An outbound span nested in an outbound span is the same call one layer
            # down (SDK operation → its HTTP request, queue send → its HTTP POST):
            # draw the outer one only.
            parent = by_id.get(s.get("parent_span_id") or "")
            if parent and parent.get("kind") in ("CLIENT", "PRODUCER") and parent["span_id"] in drawn_client:
                folded += 1
                continue
            child = server_child.get(s["span_id"])
            if child:
                peer, ptype, sub = child["service"], "backend", None
            else:
                peer, ptype, sub = peer_of(a)
            if not peer:
                unnamed += 1
                continue
            pid = participant(peer, ptype, sub)
            drawn_client.add(s["span_id"])
            events.append({"ts": s.get("start") or "", "from": ids[s["service"]], "to": pid,
                           "label": f'{clean(s.get("operation"))}  {clock(s.get("start"))}',
                           "variant": "default",
                           "span": s["span_id"], "callee": child["span_id"] if child else None,
                           "role": "call", "dur": dur or 0})
            numeric = _status_code(child or {}, s)
            failed = bool(s.get("error") or (child or {}).get("error"))
            code = numeric or (child or s).get("status") or "?"
            label = f'{clean(code, 12)}  {clock(s.get("end"))}  {dur_txt}'
            msg = (child or s).get("status_message")
            if s.get("error") and msg:
                label += "  " + clean(msg, 40)
            events.append({"ts": s.get("end") or "", "from": pid, "to": ids[s["service"]],
                           "label": label, "variant": outcome_variant(failed, numeric), "span": s["span_id"],
                           "callee": child["span_id"] if child else None, "role": "return", "dur": dur or 0})
        elif kind in ("PRODUCER", "CONSUMER"):
            bus = _first(a, BUS_KEYS) or _first(a, PEER_KEYS)
            if not bus:
                unnamed += 1
                continue
            bid = participant(bus, "messagebus")
            drawn_client.add(s["span_id"])
            me = ids[s["service"]]
            events.append({"ts": s.get("start") or "", "from": me if kind == "PRODUCER" else bid,
                           "to": bid if kind == "PRODUCER" else me,
                           "label": f'{clean(s.get("operation"))}  {clock(s.get("start"))}',
                           "variant": "dashed", "span": s["span_id"], "callee": None,
                           "role": "publish" if kind == "PRODUCER" else "consume", "dur": dur or 0})
        # SERVER and INTERNAL spans are activations, not messages.

    events.sort(key=lambda e: e["ts"])       # stable: ties keep span order
    omitted = 0
    if len(events) > max_messages:
        keep_spans = _error_path(spans, by_id)
        kept = [e for e in events if e["span"] in keep_spans or (e["callee"] and e["callee"] in keep_spans)]
        if len(kept) > max_messages:         # even the error path is too long: slowest first
            kept.sort(key=lambda e: -e["dur"])
            kept = kept[:max_messages]
        else:
            rest = sorted((e for e in events if e not in kept), key=lambda e: -e["dur"])
            for e in rest:
                if len(kept) + 1 > max_messages:
                    break
                kept.append(e)
        kept.sort(key=lambda e: e["ts"])
        omitted = len(events) - len(kept)
        events = kept

    # notes: the first warn/error log line the collector joined to the span behind each message
    notes: dict = {}
    for ev in (doc.get("loki") or {}).get("events") or []:
        sid = ev.get("span_id")
        if sid and (ev.get("level") or "").lower() in LOG_LEVELS_AS_NOTES and sid not in notes:
            notes[sid] = f'[{ev.get("level")}] {clock(ev.get("timestamp"))} {clean(ev.get("message"), NOTE_CHARS)}'

    messages = []
    y_of: dict = {}
    for i, e in enumerate(events):
        y = Y0 + i * STEP
        m = {"from": e["from"], "to": e["to"], "y": y, "label": e["label"], "variant": e["variant"]}
        note = None
        if e["role"] == "return":
            note = notes.get(e["callee"]) or notes.get(e["span"])
        elif e["role"] in ("call", "publish", "consume"):
            note = notes.get(e["span"]) if e["role"] != "call" else None
        if note:
            m["note"] = note
        messages.append(m)
        y_of.setdefault(e["span"], []).append(y)
    last_y = Y0 + STEP * max(len(events) - 1, 0)

    activations = []
    for s in spans:
        if s.get("kind") != "SERVER":
            continue
        ys = y_of.get(s.get("parent_span_id") or "")
        if ys and len(ys) >= 2:
            activations.append({"participant": ids[s["service"]], "from": ys[0] - 6, "to": ys[-1] + 6,
                                "type": "security" if s.get("error") else "backend"})
        elif s["span_id"] == tempo.get("root_span") and events:
            # The root span's caller is not in the trace: an activation, never an invented message.
            activations.append({"participant": ids[s["service"]], "from": Y0 - 30, "to": last_y + 20,
                                "type": "security" if s.get("error") else "backend"})

    root = by_id.get(tempo.get("root_span")) or spans[0]
    facts = doc.get("facts") or {}
    first_fail = next((x for x in facts.get("earliest_error_signals") or [] if x.get("kind") == "span_first_to_fail"), None)
    dur = tempo.get("duration_ms")
    cards = [{"dot": "cyan", "title": "FACT · trace", "items": [
        f'root span: {clean(root.get("service"), 24)} {clean(root.get("operation"), 40)}',
        f'{tempo.get("span_count", len(spans))} spans · {len(tempo.get("errored_spans") or [])} with error status'
        + (f' · {dur:.0f} ms' if isinstance(dur, (int, float)) else ""),
        f'window {clock(tempo.get("trace_start"))} → {clock(tempo.get("trace_end"))} (as recorded)']}]
    if first_fail:
        cards.append({"dot": "rose", "title": "FACT · first span to fail", "items": [
            f'{clean(first_fail.get("service"), 24)} · {clean(first_fail.get("operation"), 40)}',
            f'{clock(first_fail.get("timestamp"))} → {clock(first_fail.get("ended"))} · '
            + (f'{first_fail["duration_ms"]:.0f} ms' if isinstance(first_fail.get("duration_ms"), (int, float)) else "? ms"),
            clean(first_fail.get("summary") or "no status message", 60)]})
    unknown = []
    if tempo.get("spans_missing_parent"):
        unknown.append(f'{len(tempo["spans_missing_parent"])} span(s) whose parent is missing from the trace')
    if unnamed:
        unknown.append(f'{unnamed} outbound span(s) without a named destination: not drawn')
    if unknown:
        cards.append({"dot": "orange", "title": "UNKNOWN · not in this diagram", "items": unknown})
    policy = []
    if folded:
        policy.append(f'{folded} nested outbound span(s) folded into the call that contains them')
    if omitted:
        policy.append(f'{omitted} of {omitted + len(events)} messages omitted to stay readable: error path and slowest calls kept')
    if policy:
        cards.append({"dot": "cyan", "title": "FACT · not drawn, on purpose", "items": policy})

    views = [{"id": "request-path", "label": "Request path", "focus": [p["id"] for p in parts][:6],
              "note": "Calls as recorded in spans. The caller of the root span is not in the trace."}]
    if first_fail and first_fail.get("service") in ids:
        focus = [ids[first_fail["service"]]]
        focus += [p["id"] for p in parts if p["type"] == "database" and p["id"] not in focus][:1]
        views.append({"id": "first-span-to-fail", "label": "First span to fail", "focus": focus,
                      "note": "Earliest span with error status. Being first is a fact; being the cause is the investigator's call."})

    width, chars = canvas_and_label_cap(len(parts), compact)
    participants = []
    for p in parts:
        label, auto_sub = participant_label(p["name"], chars)
        sublabel = clean(p["sub"], chars) if p["sub"] else auto_sub
        participants.append({"id": p["id"], "type": p["type"], "label": label,
                             **({"sublabel": sublabel} if sublabel else {})})

    tid = str(doc.get("trace_id") or "")
    return {
        "schema_version": SCHEMA_VERSION, "diagram_type": "sequence",
        "meta": {"title": clean(f'Trace {tid[:16]}… · {root.get("service")} {root.get("operation")}', 80),
                 "subtitle": f"trace {tid} · evidence only: spans as recorded, no causal claim",
                 "viewBox": [width, max(MIN_VIEWBOX_H, last_y + 300)], "animation": "trace",
                 "quality_profile": quality, "column_fit": "spread", "views": views, "legend": legend()},
        "participants": participants, "messages": messages, "activations": activations, "cards": cards,
    }


# --------------------------------------------------------------------------- archify

def node_version() -> tuple[str | None, str | None]:
    node = shutil.which("node")
    if not node:
        return None, f"node not found (archify needs Node >= {MIN_NODE_MAJOR})"
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"node --version failed: {exc}"
    m = re.match(r"v?(\d+)", out)
    if not m or int(m.group(1)) < MIN_NODE_MAJOR:
        return None, f"node {out or '?'} is older than v{MIN_NODE_MAJOR}"
    return out, None


def find_archify(explicit: str | None = None) -> tuple[Path | None, str]:
    """The archify CLI, or why it was not found. Never installs anything."""
    cands = []
    if explicit:
        cands.append(Path(explicit))
    if os.environ.get("ARCHIFY_BIN"):
        cands.append(Path(os.environ["ARCHIFY_BIN"]))
    home = Path.home()
    for base in (home / ".claude" / "skills", Path.cwd() / ".agents" / "skills", home / ".agents" / "skills",
                 home / ".codex" / "skills", home / ".cursor" / "skills"):
        cands.append(base / "archify" / "bin" / "archify.mjs")
        cands.append(base / "archify" / "archify" / "bin" / "archify.mjs")   # git-clone layout
    for p in cands:
        if p.is_file():
            return p, "found"
    w = shutil.which("archify")
    if w:
        return Path(w), "found"
    return None, "archify not found: set ARCHIFY_BIN to its archify.mjs or install it with `npx skills add tt-a1i/archify -g` (the installer needs Node 22+)"


def archify_status(explicit: str | None = None) -> dict:
    node, why = node_version()
    path, found = find_archify(explicit)
    status = {"available": bool(node and path), "archify": str(path) if path else None, "node": node, "reason": None}
    if not node:
        status["reason"] = why
    elif not path:
        status["reason"] = found
    return status


def archify_command(archify: Path, node: str | None) -> list:
    if str(archify).endswith(".mjs"):
        return [shutil.which("node") or "node", str(archify)]
    return [str(archify)]


def deliver(archify: Path, node: str | None, seq_path: Path, html_path: Path, quality: str) -> dict:
    cmd = archify_command(archify, node) + ["deliver", "sequence", str(seq_path), str(html_path), "--json", "--quality", quality]
    env = dict(os.environ, ARCHIFY_UPDATE_CHECK_DISABLED="1")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=ARCHIFY_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"archify timed out after {ARCHIFY_TIMEOUT_S}s"}
    except OSError as exc:
        return {"ok": False, "error": f"could not run archify: {exc}"}
    receipt = None
    try:
        receipt = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        pass
    errors = (receipt or {}).get("validation", {}).get("errors") if receipt else None
    if r.returncode == 0 and receipt and errors == 0 and html_path.is_file():
        return {"ok": True, "receipt": receipt}
    text = " ".join((r.stdout + " " + r.stderr).split())
    return {"ok": False, "error": truncate(text, 400) or f"archify exit {r.returncode}", "receipt": receipt}


def render(doc: dict, out_html: Path, archify: str | None = None, quality: str = "showcase",
           max_messages: int = MAX_MESSAGES) -> dict:
    result = {"status": None, "html": None, "sequence": None, "reason": None, "quality": None,
              "compact": False, "messages": 0, "omitted": 0, "archify": None, "version": VERSION}
    seq = build_sequence(doc, max_messages, quality)
    if seq is None:
        result.update(status="skipped", reason=f"no spans to draw: Tempo status is {(doc.get('tempo') or {}).get('status', 'unknown')}")
        return result
    reason = drawable(seq)
    if reason:
        result.update(status="skipped", reason=reason)
        return result
    result["messages"] = len(seq["messages"])
    for c in seq["cards"]:
        for item in c["items"]:
            m = re.match(r"(\d+) of \d+ messages omitted", item)
            if m:
                result["omitted"] = int(m.group(1))
    status = archify_status(archify)
    result["archify"] = status["archify"]
    seq_path = out_html.with_suffix(".sequence.json")
    seq["meta"]["output"] = output_name(out_html)
    try:
        out_html.parent.mkdir(parents=True, exist_ok=True)
        seq_path.write_text(json.dumps(seq, indent=1, ensure_ascii=False), encoding="utf-8")
        result["sequence"] = str(seq_path)
    except OSError as exc:
        result.update(status="failed", reason=f"could not write {seq_path}: {exc}")
        return result
    if not status["available"]:
        result.update(status="skipped", reason=status["reason"])
        return result
    last = ""
    attempts = [(quality, False)] + ([("standard", False)] if quality != "standard" else []) + [("standard", True)]
    for q, compact in attempts:
        spec = build_sequence(doc, max_messages, q, compact=compact) if compact else seq
        spec["meta"]["quality_profile"] = q
        spec["meta"]["output"] = output_name(out_html)
        seq_path.write_text(json.dumps(spec, indent=1, ensure_ascii=False), encoding="utf-8")
        r = deliver(Path(status["archify"]), status["node"], seq_path, out_html, q)
        if r["ok"]:
            result.update(status="generated", html=str(out_html), quality=q, compact=compact)
            return result
        last = r["error"]
    result.update(status="failed", reason=f"archify rejected the diagram: {last}")
    return result


def summary_line(result: dict) -> str:
    if result["status"] == "generated":
        extra = f", {result['omitted']} omitted" if result.get("omitted") else ""
        return (f"diagram: generated {result['html']} ({result['messages']} messages{extra}; "
                f"quality {result['quality']}; evidence only: spans as recorded, no causal claim)")
    return f"diagram: not generated - {result['reason']}"


# --------------------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="render", choices=["render", "doctor"])
    p.add_argument("--input", help="full JSON written by collect-trace.py (the FULL JSON line of the cut)")
    p.add_argument("--out", help="HTML to write (default: next to the input, same stem)")
    p.add_argument("--quality", default="showcase", choices=["showcase", "standard"])
    p.add_argument("--max-messages", type=int, default=MAX_MESSAGES)
    p.add_argument("--archify", help="path to archify.mjs (overrides ARCHIFY_BIN and discovery)")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = p.parse_args()

    if args.command == "doctor":
        status = archify_status(args.archify)
        status["version"] = VERSION
        print(json.dumps(status, indent=2) if args.json else
              (f"diagram: ok (archify {status['archify']}, node {status['node']})" if status["available"]
               else f"diagram: unavailable - {status['reason']}"))
        return 0

    if not args.input:
        print("error: --input <full JSON from collect-trace.py> is required", file=sys.stderr)
        return 2
    src = Path(args.input)
    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {src}: {exc}", file=sys.stderr)
        return 2
    out_html = Path(args.out) if args.out else src.with_suffix(".html")
    result = render(doc, out_html, args.archify, args.quality, args.max_messages)
    print(json.dumps(result, indent=2) if args.json else summary_line(result))
    return 0


if __name__ == "__main__":
    utf8_stdio()
    sys.exit(main())
