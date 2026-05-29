.PHONY: up down down-all rebuild status logs curl

# Bring the full stack up.
#
# Post-terraform bootstrap runs automatically:
#   1. kubeconfig updated so kubectl/helm can reach the new cluster
#   2. ALB Controller installed — required before any Ingress object is created
#   3. kube-prometheus-stack installed — monitoring up before app workloads land
#
# The platformcore app itself is deployed by the CI pipeline (push to main),
# not by make up, so image tagging stays owned by CI.
#
# RDS IAM auth user bootstrap is automated via scripts/rds-bootstrap.sh —
# runs as the final make up step, idempotent, safe on every cluster recreate.
up:
	cd terraform && terraform apply -auto-approve
	@echo "==> Updating kubeconfig..."
	aws eks update-kubeconfig --name platformcore --region us-east-1 --no-cli-pager
	@echo "==> Adding Helm repos..."
	helm repo add eks https://aws.github.io/eks-charts --force-update 2>/dev/null || true
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update 2>/dev/null || true
	helm repo add grafana https://grafana.github.io/helm-charts --force-update 2>/dev/null || true
	helm repo update
	@echo "==> Installing ALB Controller..."
	@ALB_ROLE=$$(cd terraform && terraform output -raw alb_controller_role_arn); \
	VPC_ID=$$(cd terraform && terraform output -raw vpc_id); \
	helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
	  -n kube-system \
	  --set clusterName=platformcore \
	  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=$$ALB_ROLE" \
	  --set vpcId=$$VPC_ID \
	  --set region=us-east-1 \
	  --set replicaCount=1 \
	  --wait
	@echo "==> Installing kube-prometheus-stack..."
	helm upgrade --install kps prometheus-community/kube-prometheus-stack \
	  -n monitoring --create-namespace \
	  -f helm/monitoring/values.yaml \
	  --wait --timeout 10m
	@echo "==> Installing Loki..."
	helm upgrade --install loki grafana/loki \
	  -n monitoring \
	  -f helm/monitoring/loki-values.yaml \
	  --wait --timeout 5m
	@echo "==> Installing Promtail..."
	helm upgrade --install promtail grafana/promtail \
	  -n monitoring \
	  -f helm/monitoring/promtail-values.yaml \
	  --wait --timeout 5m
	@echo "==> Bootstrapping RDS IAM auth user..."
	@bash scripts/rds-bootstrap.sh
	@echo "==> Bootstrap complete. Push to main to deploy the platformcore app."



# Tear down billable resources between learning sessions.
#
# What gets destroyed (billable, post-Day-22):
#   - EKS node group (~$0.02/hr for 1x t3.small) + cluster (~$0.10/hr)
#   - NAT Gateway (~$0.045/hr ≈ $33/mo) + Elastic IP (~$0.005/hr when
#     detached - destroy NAT first, then EIP releases cleanly)
#   - ALB + listener + target group + attachment (~$0.025/hr for the ALB)
#   - EC2 instance (~$0.01/hr for t3.micro)
#   - 9 interface VPC endpoints (~$0.01/hr each per AZ; the STS endpoint
#     was added Day 22 for IRSA token exchange)
#   - RDS db.t3.micro (~$0.017/hr; destroy takes 5-10 min, slowest step)
#   - ALB Controller IRSA (IAM role + custom policy + attachment) and
#     EBS CSI IRSA (IAM role + attachment) - free in AWS, but their
#     trust policies reference the cluster's OIDC issuer URL which
#     becomes stale on cluster recreate, so destroy them with the
#     cluster to avoid carrying broken refs across rebuilds
#   - EBS CSI add-on (auto-destroyed with cluster anyway, listed for
#     explicit dependency order)
#
# What stays (free or effectively free):
#   - VPC, subnets (now with kubernetes.io/role/elb=1 and internal-elb=1
#     tags from Day 22 for ALB Controller subnet discovery)
#   - IGW, route tables (private RT keeps its NAT default route until
#     NAT is destroyed; aws_route.private_default is destroyed below)
#   - Security groups (including endpoints_sg which gates the interface
#     endpoint ENIs)
#   - IAM roles for EKS cluster + node + EC2 SSM, instance profile
#   - ECR repository + images (~fractions of a cent/month for our image)
#   - CloudWatch log group + retained streams
#   - RDS subnet group + parameter group (no charge when no instance)
#   - S3 gateway endpoint (gateway endpoints are free)
#
# *** PHASE 4+ NOTE ***
# Partial-teardown gets brittle as the resource graph grows. For Phase 4
# and beyond, `make down-all` is the recommended teardown - the small
# extra cost of recreating "free" resources is worth the predictability
# of a fresh-from-scratch graph every morning. Real production teams use
# the same shape via CI. `make down` is preserved here for short
# learning iterations within a day.
#
# Teardown order: eks (children first, OIDC last) -> edge -> compute ->
# data -> network endpoints -> network NAT (route first, then GW, then
# EIP - reverse of creation order so each dependency unblocks the next).
down:
	@echo "==> Pre-destroy: removing Helm releases so the ALB controller cleans up its ALB..."
	@if aws eks describe-cluster --name platformcore --region us-east-1 --no-cli-pager >/dev/null 2>&1; then \
	  aws eks update-kubeconfig --name platformcore --region us-east-1 --no-cli-pager 2>/dev/null || true; \
	  helm uninstall kps -n monitoring --ignore-not-found 2>/dev/null || true; \
	  kubectl delete pvc --all -n monitoring --wait=true --ignore-not-found 2>/dev/null || true; \
	  kubectl delete namespace monitoring --ignore-not-found 2>/dev/null || true; \
	  helm uninstall platformcore -n platformcore --ignore-not-found 2>/dev/null || true; \
	  kubectl delete ingress --all -A --ignore-not-found 2>/dev/null || true; \
	  echo "  Waiting 60s for ALB controller to de-register and delete the ALB..."; \
	  sleep 60; \
	  helm uninstall aws-load-balancer-controller -n kube-system --ignore-not-found 2>/dev/null || true; \
	else \
	  echo "  Cluster not found - skipping Helm cleanup."; \
	fi
	cd terraform && terraform destroy -auto-approve \
	  -target=module.eks.aws_eks_addon.ebs_csi \
	  -target=module.eks.aws_iam_role_policy_attachment.alb_controller \
	  -target=module.eks.aws_iam_role.alb_controller \
	  -target=module.eks.aws_iam_policy.alb_controller \
	  -target=module.eks.aws_iam_role_policy_attachment.ebs_csi \
	  -target=module.eks.aws_iam_role.ebs_csi \
	  -target=module.eks.aws_iam_role_policy_attachment.fastapi_rds \
	  -target=module.eks.aws_iam_role.fastapi \
	  -target=module.eks.aws_iam_policy.fastapi_rds \
	  -target=module.eks.aws_eks_node_group.main \
	  -target=module.eks.aws_eks_access_policy_association.console_admin \
	  -target=module.eks.aws_eks_access_entry.console_admin \
	  -target=module.eks.aws_iam_openid_connect_provider.eks \
	  -target=module.eks.aws_eks_cluster.main \
	  -target=module.eks.aws_iam_role_policy_attachment.eks_cluster \
	  -target=module.eks.aws_iam_role_policy_attachment.eks_worker_node \
	  -target=module.eks.aws_iam_role_policy_attachment.eks_cni \
	  -target=module.eks.aws_iam_role_policy_attachment.eks_ecr_readonly \
	  -target=module.eks.aws_iam_role.eks_cluster \
	  -target=module.eks.aws_iam_role.eks_node \
	  -target=module.edge.aws_lb_listener.http \
	  -target=module.edge.aws_lb_listener.https \
	  -target=module.edge.aws_lb_target_group_attachment.app \
	  -target=module.edge.aws_lb_target_group.app \
	  -target=module.edge.aws_lb.app \
	  -target=module.compute.aws_instance.app \
	  -target=module.data.aws_db_instance.main \
	  -target=module.network.aws_vpc_endpoint.ssm \
	  -target=module.network.aws_vpc_endpoint.ssmmessages \
	  -target=module.network.aws_vpc_endpoint.ec2messages \
	  -target=module.network.aws_vpc_endpoint.ecr_api \
	  -target=module.network.aws_vpc_endpoint.ecr_dkr \
	  -target=module.network.aws_vpc_endpoint.logs \
	  -target=module.network.aws_vpc_endpoint.secretsmanager \
	  -target=module.network.aws_vpc_endpoint.ec2 \
	  -target=module.network.aws_vpc_endpoint.sts \
	  -target=module.network.aws_route.private_default \
	  -target=module.network.aws_nat_gateway.main \
	  -target=module.network.aws_eip.nat


# Full destroy: everything, including ECR repo + images and the free VPC
# resources. Use at the end of a phase or when switching projects.
#
# Why the pre-destroy block exists:
#   The ALB Controller watches Ingress objects and provisions a real AWS ALB
#   outside Terraform's state. terraform destroy has no record of it, so when
#   TF tries to delete the VPC subnets AWS rejects the call ("has dependent
#   object"). We must let the controller clean up its own ALB before TF runs.
#   Sequence: uninstall the app chart (deletes Ingress) -> wait for controller
#   to delete the ALB -> uninstall the controller chart (stops it recreating
#   anything) -> terraform destroy.
#   ECR images are handled by force_delete=true on the aws_ecr_repository
#   resource (no longer needs manual image deletion before destroy).
down-all:
	@echo "==> Pre-destroy: removing Helm releases and waiting for ALB controller to clean up the ALB..."
	@if aws eks describe-cluster --name platformcore --region us-east-1 --no-cli-pager >/dev/null 2>&1; then \
	  aws eks update-kubeconfig --name platformcore --region us-east-1 --no-cli-pager 2>/dev/null || true; \
	  helm uninstall kps -n monitoring --ignore-not-found 2>/dev/null || true; \
	  kubectl delete pvc --all -n monitoring --wait=true --ignore-not-found 2>/dev/null || true; \
	  kubectl delete namespace monitoring --ignore-not-found 2>/dev/null || true; \
	  helm uninstall platformcore -n platformcore --ignore-not-found 2>/dev/null || true; \
	  kubectl delete ingress --all -A --ignore-not-found 2>/dev/null || true; \
	  echo "  Waiting 60s for ALB controller to de-register and delete the ALB..."; \
	  sleep 60; \
	  helm uninstall aws-load-balancer-controller -n kube-system --ignore-not-found 2>/dev/null || true; \
	else \
	  echo "  Cluster not found - skipping Helm cleanup."; \
	fi
	cd terraform && terraform destroy -auto-approve



# End-of-day teardown + morning rebuild shortcut.
rebuild: down up



# What's currently provisioned?
status:
	cd terraform && terraform state list



# Tail container logs from CloudWatch (live).
logs:
	aws logs tail /platformcore/app --region us-east-1 --follow



# Smoke-test the ALB. Hits root, /health, /version, /ready. Post-Day-10,
# /ready expects 200 (RDS deployed and reachable from the EC2 SG).
curl:
	@ALB=$$(cd terraform && terraform output -raw alb_dns_name); \
	echo "Hitting http://$$ALB ..."; \
	curl -sS -w "\nHTTP %{http_code}\n" http://$$ALB/; \
	echo "---"; \
	curl -sS -w "\nHTTP %{http_code}\n" http://$$ALB/health; \
	echo "---"; \
	curl -sS -w "\nHTTP %{http_code}\n" http://$$ALB/version; \
	echo "---"; \
	echo "Readiness (DB-backed; expected 200 post-Day-10):"; \
	curl -sS -w "\nHTTP %{http_code}\n" http://$$ALB/ready
