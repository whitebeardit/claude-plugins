# Method per step — recipes and how to read the results

Everything here is read-only. Replace profiles, regions, contexts and resource names with the
customer's. Run Python scripts with `python3 -I` and pass paths as arguments.

## Contents

1. [Access and scope](#access)
2. [Bill and composition by category](#bill)
3. [Savings Plan and what "by time of day" can cut](#savings-plan)
4. [Hour-of-day profiles, per workload type](#profiles)
5. [How the workload scales: EKS](#cluster) · [ECS](#ecs) · [plain EC2](#ec2) · [Lambda](#lambda)
6. [Regional traffic and network](#regional-traffic)
7. [Data and storage](#data)
8. [Proven idleness](#idleness)
9. [Sizing levers](#levers)

## 1. Access and scope {#access}

```bash
for p in payer member-infra; do aws sts get-caller-identity --profile $p --output text; done
aws eks list-clusters --profile member-infra --region us-east-1
aws ecs list-clusters --profile member-infra --region us-east-1
kubectl config get-contexts
```

- The payer (consolidated billing) sees every account in Cost Explorer and in Savings Plans; a
  member account sees only itself. Run `ce_breakdown.py accounts` first to know where the money
  is per account.
- SSO sessions expire; static-key profiles do not. Say in the report which one was used.

## 2. Bill and composition by category {#bill}

```bash
python3 -I scripts/ce_breakdown.py --profile payer map          # fixed categories + top lines + template JSON
python3 -I scripts/ce_breakdown.py --profile payer services
python3 -I scripts/ce_breakdown.py --profile payer usage "EC2 - Other"
python3 -I scripts/ce_breakdown.py --profile payer usage "Amazon Relational Database Service"
python3 -I scripts/ce_breakdown.py --profile payer usage "Amazon Simple Storage Service"
python3 -I scripts/ce_breakdown.py --profile payer marketplace
```

`map` is what guarantees the same shape for every company: Compute, Network, Data & databases,
Storage, SaaS & Marketplace, Commitments, Observability, Security & identity, Tax, Other. It
splits `EC2 - Other` by usage type (EBS goes to Storage; transfer, NAT and IPv4 go to Network) and
uses the billing entity to find Marketplace vendors. Paste the two printed arrays (`comp`,
`lines`) into the template. If a relevant amount lands in "Other", open it with `usage` and
reclassify by hand in the report.

How to read:
- `EC2 - Other` = data transfer (`DataTransfer-Regional-Bytes`, `DataTransfer-Out-Bytes`), EBS
  (`EBS:VolumeUsage.*`), NAT (`NatGateway-Hours`, `NatGateway-Bytes`), public IPv4. It is often
  bigger than compute when there is a Savings Plan.
- With a Savings Plan, `EC2 - Compute` shows only the on-demand **overflow**; the covered compute
  appears as the SP fee in the payer ("Savings Plans for AWS Compute usage").
- Marketplace (Redis Cloud, Confluent, CloudAMQP, Atlas…) is on the AWS bill but depends neither
  on time of day nor on infrastructure: it is the contracts front.
- One row per month per key in `MONTHLY` when the range crosses a month. Sum them.

## 3. Savings Plan {#savings-plan}

```bash
python3 -I scripts/ce_breakdown.py --profile payer sp
```

The arithmetic that decides the answer about time of day:

- Commitment `C` USD/h (at SP rates). Effective discount `d` = 1 − (daily fee / daily covered
  on-demand equivalent). E.g. fee 62.16/day, covered OD-eq 126.5/day → d ≈ 51%.
- Night-time on-demand-equivalent usage `U_night` (sum instances × on-demand price). If
  `U_night × (1 − d) ≈ C`, the commitment is already full at night: **removing capacity at night
  creates unused commitment, saving zero**.
- What is cuttable by time of day is the daytime overflow = `OnDemandCost` from the coverage
  report (USD/day). Every compute lever competes for that ceiling. Reducing 24/7 baseline
  (staging, VPN hosts, infra nodes) only saves during the hours that have overflow.
- An extra Savings Plan only pays if its own utilization stays high; an overflow concentrated in
  ~15 h/day gives ~60% utilization and a small net gain. Do the arithmetic and show it.
- Daily utilization of 100% with `unused ≈ 0` every day proves 24-hour saturation (hours below
  the commitment would pull the daily average down).

## 4. Hour-of-day profiles, per workload type {#profiles}

Metrics worth profiling by hour, according to what the company runs:

| Workload | Namespace / metric | Dimension | What it shows |
|---|---|---|---|
| ASG (EKS nodes, EC2) | AWS/AutoScaling GroupInServiceInstances; AWS/EC2 CPUUtilization (Average and Maximum) | AutoScalingGroupName | nodes per hour, average load and hottest node |
| EC2 outside ASGs | AWS/EC2 CPUUtilization, NetworkIn/Out | InstanceId | idle or oversized instance |
| ECS | AWS/ECS CPUUtilization, MemoryUtilization; ECS/ContainerInsights RunningTaskCount | ClusterName, ServiceName | tasks per hour and reserved × used |
| Lambda | AWS/Lambda Invocations (Sum), Duration (Average), ConcurrentExecutions (Maximum) | FunctionName | concurrency peak and functions paid for nothing |
| ALB / NLB | AWS/ApplicationELB RequestCount; AWS/NetworkELB NewFlowCount, ProcessedBytes (Sum) | LoadBalancer | traffic by hour, peak/trough |
| RDS | AWS/RDS CPUUtilization, DatabaseConnections, ReadIOPS/WriteIOPS | DBInstanceIdentifier | night ETL, daytime BI, idle instance |
| NAT | AWS/NATGateway BytesOutToDestination, ConnectionAttemptCount | NatGatewayId (+ AvailabilityZone if regional) | NAT used or idle |

CloudWatch, 14 days, 1-hour period, local timezone:

```bash
python3 -I scripts/cw_hod.py --profile member-infra --tz -3 '[
 {"ns":"AWS/AutoScaling","metric":"GroupInServiceInstances","dims":{"AutoScalingGroupName":"<asg>"},"label":"prod apps nodes"},
 {"ns":"AWS/EC2","metric":"CPUUtilization","dims":{"AutoScalingGroupName":"<asg>"},"label":"prod apps avg CPU %"},
 {"ns":"AWS/EC2","metric":"CPUUtilization","dims":{"AutoScalingGroupName":"<asg>"},"stat":"Maximum","label":"hottest node CPU %"},
 {"ns":"AWS/NetworkELB","metric":"NewFlowCount","dims":{"LoadBalancer":"net/<name>/<id>"},"stat":"Sum","label":"ingress new flows/h","fmt":"%.0f"},
 {"ns":"AWS/RDS","metric":"CPUUtilization","dims":{"DBInstanceIdentifier":"<db>"},"label":"rds cpu"}]'
```

Cluster (Mimir or Prometheus) through a port-forward, no token:

```bash
kubectl --context <ctx> port-forward -n grafana-mimir svc/grafana-mimir-query-frontend 3998:8080 &
python3 -I scripts/prom_hod.py --base http://127.0.0.1:3998/prometheus --tenant anonymous '[
 {"expr":"sum(rate(container_cpu_usage_seconds_total{container!=\"\",container!=\"POD\"}[1h]))","label":"cores used"},
 {"expr":"sum(kube_pod_container_resource_requests{resource=\"cpu\"})","label":"cores requested"},
 {"expr":"sum(kube_node_status_allocatable{resource=\"cpu\"})","label":"cores allocatable"},
 {"expr":"sum(kube_horizontalpodautoscaler_status_desired_replicas)","label":"HPA desired replicas","fmt":"%.0f"},
 {"expr":"max_over_time(sum(kube_pod_status_phase{phase=\"Pending\"})[1h:1m])","label":"max pending pods / hour","fmt":"%.0f"},
 {"kind":"top","expr":"sort_desc(sum by(namespace)(kube_pod_container_resource_requests{resource=\"cpu\",container!=\"\"}) - max_over_time(sum by(namespace)(rate(container_cpu_usage_seconds_total{container!=\"\",container!=\"POD\"}[5m]))[7d:5m]))","label":"CPU requested minus 7d peak, by namespace"},
 {"kind":"top","expr":"sort_desc(max_over_time(kube_horizontalpodautoscaler_status_desired_replicas[14d]) / kube_horizontalpodautoscaler_spec_max_replicas > 0.8)","label":"HPAs near maxReplicas in 14d"}]'
kill %1
```

How to read: the peak is the hour with the highest CPU used and highest HPA desired; a
peak-to-trough ratio of nodes much smaller than that of CPU means a high floor of requests or
minReplicas. `requested ≈ 2× used` is the pattern to look for. In-cluster Prometheus usually keeps
3 days; prefer the long-term store (Mimir, Thanos).

## 5. How the workload scales

### EKS / Kubernetes {#cluster}

```bash
bash scripts/k8s_scaling_snapshot.sh <ctx> member-infra <cluster> us-east-1
aws autoscaling describe-scaling-activities --profile member-infra --auto-scaling-group-name <asg> --max-items 400
```

Look for: nodegroups all on-demand with no scheduled actions (reactive autoscaler), HPAs touching
`maxReplicas` with utilization above target (peak protection), requests at 96–99% of allocatable
with real CPU at 40–50% (bin packing by request, not by load), PDBs blocking scale-down, ArgoCD
selfHeal (any tool that changes replicas gets reverted), Services without `trafficDistribution`
(no zone-aware routing at all).

### ECS {#ecs}

```bash
aws ecs list-clusters; aws ecs describe-services --cluster <c> --services <s...> \
  --query 'services[].{name:serviceName,desired:desiredCount,running:runningCount,launch:launchType,strategy:capacityProviderStrategy}'
aws application-autoscaling describe-scalable-targets --service-namespace ecs
aws application-autoscaling describe-scaling-policies --service-namespace ecs
aws application-autoscaling describe-scheduled-actions --service-namespace ecs
aws ecs describe-task-definition --task-definition <td> --query 'taskDefinition.{cpu:cpu,mem:memory,containers:containerDefinitions[].{n:name,cpu:cpu,mem:memory}}'
```

Look for: services without an autoscaling policy (desired fixed at the peak 24 h), tasks with
CPU/memory reservation far above usage (`CPUUtilization`/`MemoryUtilization` of the service as %
of the reservation), on-demand Fargate where Fargate Spot would do (non-production, workers),
non-production environments with desired > 0 at night and on weekends (scheduled actions work
here and nothing reverts them).

### Plain EC2 {#ec2}

```bash
aws ec2 describe-instances --filters Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].{id:InstanceId,type:InstanceType,asg:Tags[?Key==`aws:autoscaling:groupName`]|[0].Value,name:Tags[?Key==`Name`]|[0].Value}'
aws compute-optimizer get-ec2-instance-recommendations --query 'instanceRecommendations[].{id:instanceArn,finding:finding,current:currentInstanceType,rec:recommendationOptions[0].instanceType}'
aws ec2 describe-addresses --query 'Addresses[?AssociationId==null]'   # loose EIPs
```

Look for: instances outside ASGs with 14-day average CPU under 10% (VPN, bastion, old jobs),
old families (t2, m4, c4) that cost more than current ones, Compute Optimizer "Over-provisioned",
ASGs without policy or scheduled action, loose EIPs (billed).

### Lambda and serverless {#lambda}

```bash
aws lambda list-functions --query 'Functions[].{f:FunctionName,mem:MemorySize,timeout:Timeout,arch:Architectures[0]}'
aws lambda list-provisioned-concurrency-configs --function-name <f>
aws compute-optimizer get-lambda-function-recommendations --query 'lambdaFunctionRecommendations[].{f:functionArn,finding:finding,mem:currentMemorySize,rec:memorySizeRecommendationOptions[0].memorySize}'
```

Look for: configured memory far above what is used (Compute Optimizer), x86 functions that would
run on arm64 (20% cheaper), idle provisioned concurrency outside business hours, functions never
invoked but with provisioned concurrency, CloudWatch log groups without retention (they grow
forever).

## 6. Regional traffic and network {#regional-traffic}

```bash
python3 -I scripts/ce_breakdown.py --profile payer --start 2026-10-01 regional
python3 -I scripts/ce_breakdown.py --profile payer --start 2026-07-01 regional-trend
```

Then attribute:

1. **By namespace, excluding hostNetwork** (otherwise kube-system/monitoring "lead" with the whole
   node's traffic):
   `sum by(namespace)(rate(container_network_receive_bytes_total{pod!=""}[7d]) unless on(namespace,pod) kube_pod_info{host_network="true"})`.
   Per-node truth: `node_network_*_bytes_total{device=~"eth.*|ens.*"}` from node_exporter.
2. **External destinations**: extract hostnames from the apps' configmaps (never print
   credentials), resolve with `getent ahostsv4`/`nslookup` and check them against
   `https://ip-ranges.amazonaws.com/ip-ranges.json` (download into an empty directory, read with
   `python3 -I`). A host inside an EC2 range of the same region → billed as `PublicIP-*`.
3. **Volume per vendor** when the vendor's own metrics are in the metrics store (e.g.
   `confluent_kafka_server_sent_bytes`), to isolate the rest by exclusion.
4. **Internal hops**: service-to-service calls through an internal ingress hostname
   (`*.local.<domain>` → ClusterIP of a hostNetwork HAProxy/nginx) take two hops, each ~50%
   cross-AZ in a two-zone cluster. Count the URLs in the configmaps.
5. **Telemetry**: a single collector, ingesters concentrated in one AZ, replication. Read the real
   replication factor before talking about zone awareness.
6. **Timeline**: compare 7-day windows before/after each jump in `regional-trend` to find the
   service that changed (replicas, migration into the cluster).

Without VPC Flow Logs, attribution by destination is an estimate; say so in the report and
recommend enabling Flow Logs for 7 days as the first step (it is a change: do not do it).

Network beyond regional: NAT by bytes (`NatGateway-Bytes`, USD 0.045/GB; S3/DynamoDB gateway
endpoints take that traffic off the NAT), public IPv4 (`PublicIPv4:InUseAddress`, USD 0.005/h per
address), CloudFront in front of origins that serve the same content many times, ELBs without
targets (`describe-target-health` empty).

## 7. Data and storage {#data}

```bash
aws rds describe-db-instances --query 'DBInstances[].{id:DBInstanceIdentifier,class:DBInstanceClass,multiaz:MultiAZ,storage:AllocatedStorage,type:StorageType,env:TagList[?Key==`Environment`]|[0].Value}'
aws rds describe-db-snapshots --snapshot-type manual --query 'length(DBSnapshots)'
aws ec2 describe-volumes --filters Name=status,Values=available --query 'Volumes[].{id:VolumeId,gb:Size,type:VolumeType}'
aws ec2 describe-snapshots --owner-ids self --query 'length(Snapshots)'
aws s3api list-buckets; aws s3api get-bucket-lifecycle-configuration --bucket <b>
aws logs describe-log-groups --query 'logGroups[?retentionInDays==null].[logGroupName,storedBytes]'
```

Look for: RDS with low 14-day average CPU (class too large), Multi-AZ in non-production, EBS gp2
(gp3 is 20% cheaper per GB, same base performance), old manual snapshots, loose volumes, log and
dump buckets without lifecycle (and S3 where the cost is requests/transfer, which lifecycle does
not fix), log groups without retention. ElastiCache and OpenSearch: reserved nodes × real usage.

## 8. Proven idleness {#idleness}

```bash
python3 -I scripts/nat_idle_check.py --profile member-infra --days 180
aws ec2 describe-volumes --profile member-infra --filters Name=status,Values=available --query 'Volumes[].{id:VolumeId,gb:Size,type:VolumeType}' --output table
aws ecr describe-repositories --profile member-infra --query 'repositories[].repositoryName' --output text | tr '\t' '\n' | while read r; do aws ecr get-lifecycle-policy --repository-name "$r" >/dev/null 2>&1 || echo "no lifecycle: $r"; done
```

Rules of proof: datapoints present and at zero (not "no data"); a control query on an active
resource with the same method; topology (who could use it) consistent with the metric; without
Flow Logs, say that fine attribution does not exist. For loose instances (VPN, bastion), 14-day
average CPU under 10% and a large type = resize.

## 9. Sizing levers {#levers}

Useful on-demand prices, us-east-1 (USD/h): c7i.2xlarge 0.357 · c7i.xlarge 0.1785 · m6i.large
0.096 · m6i.xlarge 0.192 · r6i.large 0.126 · c6i.2xlarge 0.34 · t3.medium 0.0416 · t4g.small
0.0168. Regional transfer USD 0.01/GB per direction. NAT USD 0.045/h. EBS gp3 USD 0.08/GB-month
(gp2 0.10). RDS gp2 and gp3 cost the same per GB (gp3 only improves IOPS). Public IPv4 USD
3.65/month per address. CloudWatch Logs ingest USD 0.50/GB, storage USD 0.03/GB-month. Lambda
arm64 ≈ 20% cheaper than x86.

- Nominal = on-demand price × hours. Realizable today = nominal × fraction of hours with
  on-demand overflow (for compute under a saturated SP), capped at the total overflow.
- Traffic: every GB avoided is worth USD 0.02 (both directions) if InterZone, USD 0.01–0.02 if
  PublicIP. Reduction estimates per lever: zone-aware routing on one hop removes ~50% of that
  hop; a private link to a vendor removes 100% of that vendor's PublicIP.
- Contracts: a percentage range only (30–50%) until real usage is measured; mark "to validate".
- Never add levers that compete for the same ceiling (compute) without applying the ceiling.
