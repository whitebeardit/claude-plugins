#!/usr/bin/env python3
"""Prove (or disprove) that NAT gateways are idle, with three independent evidences. Read-only.

Usage: python3 -I nat_idle_check.py --profile PROFILE [--region us-east-1] [--days 180]

Evidence 1: CloudWatch AWS/NATGateway metrics (BytesOut/In, Packets, ConnectionAttemptCount,
            ActiveConnectionCount) with datapoint counts, so "0" means measured zero, not missing
            data. Regional NAT gateways publish under an extra AvailabilityZone dimension, so the
            dimension sets are discovered with list-metrics instead of assumed.
Evidence 2: Route tables -> subnets whose default route is the NAT -> network interfaces in those
            subnets (type/description). Idle means nothing there can originate egress.
Evidence 3: Flow logs present? Fargate profiles / VPC Lambdas / nodegroup subnets (callers that
            would need a NAT). Printed for the human to judge.
"""
import argparse, collections, datetime, json, subprocess

def aws(profile, region, *args):
    out = subprocess.run(["aws", "--profile", profile, "--region", region, "--output", "json", "--no-cli-pager", *args], capture_output=True, text=True)
    if out.returncode: print("ERR", args[:2], out.stderr.strip()[:200]); return {}
    return json.loads(out.stdout or "{}")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--profile", required=True); ap.add_argument("--region", default="us-east-1"); ap.add_argument("--days", type=int, default=180)
    a = ap.parse_args(); P, R = a.profile, a.region
    nats = aws(P, R, "ec2", "describe-nat-gateways").get("NatGateways", [])
    metrics = aws(P, R, "cloudwatch", "list-metrics", "--namespace", "AWS/NATGateway", "--metric-name", "BytesOutToDestination").get("Metrics", [])
    dims_by_nat = collections.defaultdict(list)
    for m in metrics:
        ids = [d["Value"] for d in m["Dimensions"] if d["Name"] == "NatGatewayId"]
        if ids: dims_by_nat[ids[0]].append(m["Dimensions"])
    end = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0); start = end - datetime.timedelta(days=a.days)
    rts = aws(P, R, "ec2", "describe-route-tables").get("RouteTables", []); subs = {s["SubnetId"]: s for s in aws(P, R, "ec2", "describe-subnets").get("Subnets", [])}
    enis = aws(P, R, "ec2", "describe-network-interfaces").get("NetworkInterfaces", [])
    main_rt = {rt["VpcId"]: rt for rt in rts if any(x.get("Main") for x in rt["Associations"])}
    sub_rt = {x["SubnetId"]: rt for rt in rts for x in rt["Associations"] if x.get("SubnetId")}
    for sid, s in subs.items(): sub_rt.setdefault(sid, main_rt.get(s["VpcId"]))
    name = lambda tags: [t["Value"] for t in (tags or []) if t["Key"] == "Name"][:1]
    for n in nats:
        nid = n["NatGatewayId"]; print(f"\n=== {nid} {name(n.get('Tags'))} state={n['State']} vpc={n['VpcId']} created={n['CreateTime'][:10]} type={n.get('ConnectivityType')}")
        print("  -- evidence 1: CloudWatch, last %d days (period 1 day)" % a.days)
        for dims in dims_by_nat.get(nid) or [[{"Name": "NatGatewayId", "Value": nid}]]:
            for metric, stat in [("BytesOutToDestination", "Sum"), ("BytesInFromSource", "Sum"), ("PacketsOutToDestination", "Sum"), ("ConnectionAttemptCount", "Sum"), ("ActiveConnectionCount", "Maximum")]:
                q = [{"Id": "m1", "MetricStat": {"Metric": {"Namespace": "AWS/NATGateway", "MetricName": metric, "Dimensions": dims}, "Period": 86400, "Stat": stat}, "ReturnData": True}]
                r = aws(P, R, "cloudwatch", "get-metric-data", "--metric-data-queries", json.dumps(q), "--start-time", start.isoformat().replace("+00:00", "Z"), "--end-time", end.isoformat().replace("+00:00", "Z"))
                vals = r.get("MetricDataResults", [{}])[0].get("Values", [])
                print("     %-24s datapoints=%3d total=%.0f nonzero_days=%d dims=%s" % (metric, len(vals), sum(vals), sum(1 for v in vals if v > 0), [d["Value"] for d in dims]))
        print("  -- evidence 2: subnets routed through it and their ENIs")
        routed = [sid for sid, rt in sub_rt.items() if rt and any(r.get("NatGatewayId") == nid for r in rt["Routes"])]
        for sid in routed:
            s = subs[sid]; cnt = collections.Counter((e.get("InterfaceType"), (e.get("Description") or "")[:30]) for e in enis if e["SubnetId"] == sid)
            print("     ", sid, s["AvailabilityZone"], s["CidrBlock"], name(s.get("Tags")), "ENIs:", dict(cnt) or "NONE")
        print("     route tables referencing it:", [rt["RouteTableId"] for rt in rts if any(r.get("NatGatewayId") == nid for r in rt["Routes"])])
    print("\n=== evidence 3: potential NAT consumers")
    print("  flow logs:", [(f["ResourceId"], f["LogDestinationType"]) for f in aws(P, R, "ec2", "describe-flow-logs").get("FlowLogs", [])] or "none (no per-destination attribution available)")
    for c in aws(P, R, "eks", "list-clusters").get("clusters", []):
        fp = aws(P, R, "eks", "list-fargate-profiles", "--cluster-name", c).get("fargateProfileNames", [])
        ngs = aws(P, R, "eks", "list-nodegroups", "--cluster-name", c).get("nodegroups", [])
        ngsub = {ng: aws(P, R, "eks", "describe-nodegroup", "--cluster-name", c, "--nodegroup-name", ng).get("nodegroup", {}).get("subnets") for ng in ngs}
        print(f"  eks {c}: fargate={fp or 'none'} nodegroup subnets={ngsub}")
    lam = [f["FunctionName"] for f in aws(P, R, "lambda", "list-functions").get("Functions", []) if (f.get("VpcConfig") or {}).get("VpcId")]
    print("  VPC-attached lambdas:", lam or "none")
    pub = collections.Counter()
    for e in enis:
        if e.get("Attachment", {}).get("InstanceId"): pub["public-ip" if e.get("Association", {}).get("PublicIp") else "private-only"] += 1
    print("  instance ENIs:", dict(pub), "(instances with public IPs egress via IGW, not NAT)")

if __name__ == "__main__":
    main()
