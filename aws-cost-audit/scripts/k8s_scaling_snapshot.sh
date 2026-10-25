#!/usr/bin/env bash
# Read-only snapshot of how an EKS cluster scales. Usage: k8s_scaling_snapshot.sh <kube-context> <aws-profile> <cluster-name> [region]
# Prints: nodegroups (min/max/desired/capacity type), ASG policies and scheduled actions, autoscaler
# component and flags, HPAs (min/max/current/target), PDBs blocking eviction, per-node requests vs
# allocatable, services with zone-aware routing, LoadBalancer services. Nothing here mutates state.
set -u
CTX="$1"; PROFILE="$2"; CLUSTER="$3"; REGION="${4:-us-east-1}"
export AWS_PAGER=""
K="kubectl --context $CTX"
echo "=== nodegroups ($CLUSTER)"
for ng in $(aws eks list-nodegroups --profile "$PROFILE" --region "$REGION" --cluster-name "$CLUSTER" --query 'nodegroups[]' --output text); do
  aws eks describe-nodegroup --profile "$PROFILE" --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$ng" \
    --query 'nodegroup.{ng:nodegroupName,types:instanceTypes,cap:capacityType,min:scalingConfig.minSize,max:scalingConfig.maxSize,desired:scalingConfig.desiredSize,asg:resources.autoScalingGroups[0].name,subnets:subnets,taints:taints}' --output json
done
echo "=== ASG scaling policies / scheduled actions (empty = none)"
aws autoscaling describe-policies --profile "$PROFILE" --region "$REGION" --query 'ScalingPolicies[].{asg:AutoScalingGroupName,name:PolicyName,type:PolicyType}' --output table
aws autoscaling describe-scheduled-actions --profile "$PROFILE" --region "$REGION" --query 'ScheduledUpdateGroupActions[].{asg:AutoScalingGroupName,rec:Recurrence,min:MinSize,max:MaxSize,desired:DesiredCapacity}' --output table
echo "=== node autoscaler"
$K get deploy -A 2>/dev/null | grep -i -E 'cluster-autoscaler|karpenter|keda' || echo "none found"
$K get deploy cluster-autoscaler -n kube-system -o jsonpath='{.spec.template.spec.containers[0].command}{"\n"}{.spec.template.spec.containers[0].image}{"\n"}' 2>/dev/null
$K get nodepools.karpenter.sh -A 2>/dev/null | head
echo "=== nodes"
$K get nodes -o custom-columns='NAME:.metadata.name,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type,CAP:.metadata.labels.eks\.amazonaws\.com/capacityType,NG:.metadata.labels.eks\.amazonaws\.com/nodegroup,ZONE:.metadata.labels.topology\.kubernetes\.io/zone,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory'
echo "=== requests vs allocatable per node (cpu, memory)"
$K describe nodes 2>/dev/null | awk '/^Name:/{n=$2} /Allocated resources/{f=1} f&&/^  cpu/{c=$2" "$3} f&&/^  memory/{print n, "cpu", c, "mem", $2, $3; f=0}'
echo "=== HPAs"
$K get hpa -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,MIN:.spec.minReplicas,MAX:.spec.maxReplicas,CUR:.status.currentReplicas,DES:.status.desiredReplicas,TARGET:.spec.metrics[0].resource.target.averageUtilization'
echo "=== PDBs with 0 disruptions allowed"
$K get pdb -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,ALLOWED:.status.disruptionsAllowed' | awk 'NR==1 || $3==0'
echo "=== Services: zone-aware routing / LoadBalancers"
$K get svc -A -o json | python3 -I -c '
import sys,json
d=json.load(sys.stdin)["items"]
print("services:",len(d),"| trafficDistribution/topology-mode:",sum(1 for s in d if s["spec"].get("trafficDistribution") or (s["metadata"].get("annotations") or {}).get("service.kubernetes.io/topology-mode")),"| internalTrafficPolicy=Local:",sum(1 for s in d if s["spec"].get("internalTrafficPolicy")=="Local"))
for s in d:
    if s["spec"].get("type")=="LoadBalancer": print("LB:",s["metadata"]["namespace"],s["metadata"]["name"],"externalTrafficPolicy=",s["spec"].get("externalTrafficPolicy"),(s["status"].get("loadBalancer",{}).get("ingress") or [{}])[0].get("hostname"))'
echo "=== ArgoCD apps with selfHeal (changes outside Git get reverted)"
$K get applications -n argocd -o json 2>/dev/null | python3 -I -c '
import sys,json
d=json.load(sys.stdin)["items"]; print("apps",len(d),"selfHeal",sum(1 for a in d if ((a["spec"].get("syncPolicy") or {}).get("automated") or {}).get("selfHeal")))' || echo "no argocd"
