#!/usr/bin/env bash
#--------------------------------------------------------------------------------------------------------
# SWEEP ORPHANS - AWS RESOURCES THE CLUSTER CREATED AND TERRAFORM NEVER KNEW ABOUT
#--------------------------------------------------------------------------------------------------------
#
# Controllers inside the cluster create AWS resources that are absent from Terraform state:
#
#   - AWS Load Balancer Controller: an ALB, its target groups and security groups per Ingress
#   - EBS CSI driver:               a volume per PVC (gp3-retain volumes outlive their PVC by design)
#   - VPC CNI:                      ENIs that can stay "available" after their node is gone
#   - EKS itself:                   the eks-cluster-sg-<cluster>-* security group
#
# Kubernetes deletes them only while the cluster exists. When the cluster goes first (seen
# 2026-10-13: a network drop mid make down-all) they are orphaned, keep billing, and the VPC
# delete fails with DependencyViolation after a 20-minute retry.
#
#   scripts/sweep-orphans.sh            list only; exit 1 if anything is found
#   scripts/sweep-orphans.sh --delete   delete in dependency order, then list again
#
# Order matters: load balancers hold ENIs, ENIs hold security groups, security groups hold
# each other through rules. Deleting out of order only produces more DependencyViolation.
#
# Written for the bash 3.2 that macOS ships: no mapfile, no associative arrays.
#

set -euo pipefail

CLUSTER="${CLUSTER:-platformcore}"
REGION="${AWS_REGION:-us-east-1}"
VPC_NAME="${VPC_NAME:-${CLUSTER}-vpc}"
MODE="list"

case "${1:-}" in
  "")       ;;
  --delete) MODE="delete" ;;
  *)        echo "usage: $0 [--delete]" >&2; exit 2 ;;
esac

for tool in aws jq; do
  command -v "$tool" >/dev/null || { echo "$tool not found" >&2; exit 2; }
done

aws_() { aws --region "$REGION" --no-cli-pager --output json "$@"; }

# Discovery is by tag, not by liveness: while the cluster runs, its own ALBs, security
# groups and ENIs carry the same tags and would be deleted from under it. Listing is still
# allowed, as a preview of what the controllers own.
if aws_ eks describe-cluster --name "$CLUSTER" >/dev/null 2>&1; then
  if [ "$MODE" = "delete" ]; then
    echo "cluster $CLUSTER still exists: everything below is live, not orphaned. Refusing --delete." >&2
    echo "Run scripts/predestroy.sh and make down-all first." >&2
    exit 2
  fi
  echo "(cluster $CLUSTER exists: these are live resources owned by its controllers, not orphans)"
fi

VPC_ID=$(aws_ ec2 describe-vpcs --filters "Name=tag:Name,Values=$VPC_NAME" \
  | jq -r '.Vpcs[0].VpcId // empty')



#--------------------------------------------------------------------------------------------------------
# DISCOVERY
#--------------------------------------------------------------------------------------------------------
#
# Each function prints one id per line. Discovery is by the tags each controller writes, so
# nothing Terraform owns can match; the ENI and security group queries are also fenced to
# this VPC.

load_balancers() {
  local arns
  arns=$(aws_ elbv2 describe-load-balancers | jq -r '.LoadBalancers[].LoadBalancerArn')
  [ -n "$arns" ] || return 0
  # describe-tags takes at most 20 ARNs per call
  echo "$arns" | xargs -n 20 aws --region "$REGION" --no-cli-pager --output json \
      elbv2 describe-tags --resource-arns \
    | jq -r --arg c "$CLUSTER" \
        '.TagDescriptions[] | select(any(.Tags[]; .Key == "elbv2.k8s.aws/cluster" and .Value == $c)) | .ResourceArn'
}

target_groups() {
  local arns
  arns=$(aws_ elbv2 describe-target-groups | jq -r '.TargetGroups[].TargetGroupArn')
  [ -n "$arns" ] || return 0
  echo "$arns" | xargs -n 20 aws --region "$REGION" --no-cli-pager --output json \
      elbv2 describe-tags --resource-arns \
    | jq -r --arg c "$CLUSTER" \
        '.TagDescriptions[] | select(any(.Tags[]; .Key == "elbv2.k8s.aws/cluster" and .Value == $c)) | .ResourceArn'
}

# Any ENI left "available" in this VPC is attached to nothing. Interface endpoints and
# load balancers own theirs in "in-use" state, so they never match.
available_enis() {
  [ -n "$VPC_ID" ] || return 0
  aws_ ec2 describe-network-interfaces \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=status,Values=available" \
    | jq -r '.NetworkInterfaces[].NetworkInterfaceId'
}

security_groups() {
  [ -n "$VPC_ID" ] || return 0
  aws_ ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC_ID" \
    | jq -r --arg c "$CLUSTER" '.SecurityGroups[]
        | select(any(.Tags[]?; (.Key == "elbv2.k8s.aws/cluster" and .Value == $c)
                             or (.Key == "aws:eks:cluster-name" and .Value == $c)))
        | .GroupId'
}

# The CSI driver tags every volume it creates with ebs.csi.aws.com/cluster. Only volumes no
# instance holds are orphans; a live cluster's attached volumes never match.
volumes() {
  aws_ ec2 describe-volumes \
      --filters "Name=tag-key,Values=ebs.csi.aws.com/cluster" "Name=status,Values=available" \
    | jq -r '.Volumes[] | [.VolumeId, (.Size|tostring) + "GiB",
        ((.Tags // []) | map(select(.Key == "kubernetes.io/created-for/pvc/name")) | .[0].Value // "-")]
        | @tsv'
}

report() {
  local found=0 section items
  for section in load_balancers target_groups available_enis security_groups volumes; do
    items=$("$section")
    if [ -n "$items" ]; then
      found=1
      echo "== $section"
      echo "$items" | awk '{ print "   " $0 }'
    fi
  done
  [ -n "$VPC_ID" ] || echo "(no VPC named $VPC_NAME; ENIs and security groups not checked)"
  return "$found"
}



#--------------------------------------------------------------------------------------------------------
# DELETION
#--------------------------------------------------------------------------------------------------------

# Rules in other groups that reference this one must go before the group can.
revoke_references() {
  local sg="$1" holder perms
  aws_ ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC_ID" \
    | jq -c --arg sg "$sg" '
        # Keep only the reference to this group: a permission can also carry CIDR ranges
        # or other groups, and those rules belong to whatever still uses them.
        def only($g): .UserIdGroupPairs = [.UserIdGroupPairs[] | select(.GroupId == $g)]
                      | .IpRanges = [] | .Ipv6Ranges = [] | .PrefixListIds = [];
        .SecurityGroups[]
        | {id: .GroupId,
           ingress: [.IpPermissions[]       | select(any(.UserIdGroupPairs[]?; .GroupId == $sg)) | only($sg)],
           egress:  [.IpPermissionsEgress[] | select(any(.UserIdGroupPairs[]?; .GroupId == $sg)) | only($sg)]}
        | select((.ingress | length) + (.egress | length) > 0)' \
    | while read -r holder; do
        perms=$(echo "$holder" | jq -c '.ingress')
        if [ "$perms" != "[]" ]; then
          aws_ ec2 revoke-security-group-ingress --group-id "$(echo "$holder" | jq -r .id)" \
            --ip-permissions "$perms" >/dev/null
        fi
        perms=$(echo "$holder" | jq -c '.egress')
        if [ "$perms" != "[]" ]; then
          aws_ ec2 revoke-security-group-egress --group-id "$(echo "$holder" | jq -r .id)" \
            --ip-permissions "$perms" >/dev/null
        fi
      done
}

delete_all() {
  local arn eni sg vol attempt left

  for arn in $(load_balancers); do
    echo "deleting load balancer $arn"
    aws_ elbv2 delete-load-balancer --load-balancer-arn "$arn"
  done
  for arn in $(load_balancers); do
    aws_ elbv2 wait load-balancers-deleted --load-balancer-arns "$arn"
  done

  for arn in $(target_groups); do
    echo "deleting target group $arn"
    aws_ elbv2 delete-target-group --target-group-arn "$arn"
  done

  # A deleted load balancer releases its ENIs over a few minutes; they then show up as
  # available and are removed with the rest.
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    for eni in $(available_enis); do
      echo "deleting ENI $eni"
      aws_ ec2 delete-network-interface --network-interface-id "$eni" || true
    done
    for sg in $(security_groups); do
      revoke_references "$sg"
      echo "deleting security group $sg (attempt $attempt)"
      aws_ ec2 delete-security-group --group-id "$sg" 2>/dev/null || true
    done
    left=$(security_groups)
    [ -z "$left" ] && break
    echo "security groups still held, waiting 30 s: $(echo "$left" | tr '\n' ' ')"
    sleep 30
  done

  volumes | while IFS="$(printf '\t')" read -r vol _ _; do
    echo "deleting volume $vol"
    aws_ ec2 delete-volume --volume-id "$vol"
  done
}



#--------------------------------------------------------------------------------------------------------
# MAIN
#--------------------------------------------------------------------------------------------------------

if [ "$MODE" = "delete" ]; then
  echo "Orphans before deletion:"
  report || true
  echo
  delete_all
  echo
fi

if report; then
  echo "No orphans for cluster $CLUSTER in $REGION."
else
  echo "Orphans remain for cluster $CLUSTER in $REGION." >&2
  exit 1
fi
