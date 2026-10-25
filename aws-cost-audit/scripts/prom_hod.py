#!/usr/bin/env python3
"""Hour-of-day profiles and top-N tables from a Prometheus-compatible API (Mimir, Prometheus).

Usage:
  kubectl port-forward -n grafana-mimir svc/grafana-mimir-query-frontend 3998:8080 &
  python3 -I prom_hod.py --base http://127.0.0.1:3998/prometheus [--tenant anonymous] [--days 14] [--tz -3] QUERIES_JSON

QUERIES_JSON is a JSON list; each item is either
  {"kind":"hod","expr":"sum(rate(container_cpu_usage_seconds_total{container!=\"\"}[1h]))","label":"cores used","fmt":"%.1f","scale":1}
or
  {"kind":"top","expr":"sort_desc(sum by(namespace)(...))","label":"...","n":20,"fmt":"%.2f","scale":1}

Why: the Grafana MCP returns raw series (336 points for 14 days at 1h) that cannot be
averaged reliably by eye; a port-forward to the query frontend needs no token and
this script does the hour-of-day aggregation. Mimir needs the X-Scope-OrgID header
(often "anonymous"). In-cluster kube-prometheus Prometheus usually keeps only a few
days; prefer the long-term store. Caveat baked in: pods with hostNetwork (aws-node,
kube-proxy, node-exporter, ingress DaemonSets) report the whole node's NIC traffic
under container_network_*, so exclude them (`unless on(namespace,pod) kube_pod_info{host_network="true"}`)
and use node_exporter eth*/ens* for per-node truth.
"""
import argparse, collections, datetime, json, socket, sys, time, urllib.parse, urllib.request

def wait(base):
    host = urllib.parse.urlparse(base).hostname; port = urllib.parse.urlparse(base).port or 80
    for _ in range(40):
        try: socket.create_connection((host, port), 1).close(); return True
        except OSError: time.sleep(0.5)
    return False

def q(base, tenant, expr, kind="instant", days=14, step=3600):
    now = int(time.time())
    if kind == "range":
        url = base + "/api/v1/query_range?" + urllib.parse.urlencode({"query": expr, "start": now - days*86400, "end": now, "step": step})
    else:
        url = base + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    req = urllib.request.Request(url, headers={"X-Scope-OrgID": tenant} if tenant else {})
    try: return json.load(urllib.request.urlopen(req, timeout=180))["data"]["result"]
    except Exception as e: print("ERR", expr[:70], str(e)[:160]); return []

def hod(base, tenant, item, days, tz):
    r = q(base, tenant, item["expr"], "range", days)
    if not r: print(f"\n## {item['label']}: no data"); return
    zone = datetime.timezone(datetime.timedelta(hours=tz)); fmt = item.get("fmt", "%.1f"); scale = item.get("scale", 1)
    byh, wk, we = (collections.defaultdict(list) for _ in range(3))
    for t, v in r[0]["values"]:
        try: v = float(v) * scale
        except ValueError: continue
        lt = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).astimezone(zone)
        byh[lt.hour].append(v); (we if lt.weekday() >= 5 else wk)[lt.hour].append(v)
    vals = [x for l in byh.values() for x in l]
    print(f"\n## {item['label']} (n={len(vals)} min={fmt % min(vals)} avg={fmt % (sum(vals)/len(vals))} max={fmt % max(vals)})")
    avg = lambda l: fmt % (sum(l)/len(l)) if l else "-"
    print(f"h(UTC{tz:+d})   all   wkday  wkend")
    for h in range(24): print("%02d  %7s %7s %7s" % (h, avg(byh[h]), avg(wk[h]), avg(we[h])))

def top(base, tenant, item):
    r = q(base, tenant, item["expr"]); fmt = item.get("fmt", "%.2f"); scale = item.get("scale", 1)
    rows = sorted(((float(x["value"][1]) * scale, x["metric"]) for x in r), key=lambda z: -z[0])[: item.get("n", 20)]
    print(f"\n## {item['label']}")
    for v, m in rows: print(fmt % v, {k: v2 for k, v2 in m.items() if k != "__name__"})

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--base", required=True); ap.add_argument("--tenant", default="anonymous")
    ap.add_argument("--days", type=int, default=14); ap.add_argument("--tz", type=int, default=-3); ap.add_argument("queries")
    a = ap.parse_args()
    if not wait(a.base): sys.exit("endpoint not reachable (is the port-forward up?)")
    items = json.load(open(a.queries)) if a.queries.endswith(".json") else json.loads(a.queries)
    for it in items:
        (hod(a.base, a.tenant, it, a.days, a.tz) if it.get("kind", "hod") == "hod" else top(a.base, a.tenant, it))
