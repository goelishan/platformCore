.PHONY: up down down-all rebuild status logs curl helm-repos helm-relock helm-pins chart-check lock-python


#--------------------------------------------------------------------------------------------------------
# OPERATOR LAYER — VERSIONS COME FROM THE LOCK
#--------------------------------------------------------------------------------------------------------
#
# charts/platform-bootstrap/Chart.lock names the exact version of each operator
# chart. `helm dependency build` downloads those versions and refuses anything the
# lock does not cover, so the tarballs installed below are the pinned ones by
# construction rather than by a flag someone has to remember to pass. They are
# installed as six releases rather than one because they land in six different
# namespaces and must exist before Argo CD syncs the application chart.

BOOTSTRAP := charts/platform-bootstrap
VENDORED  := $(BOOTSTRAP)/charts

# Helm resolves a dependency's `repository:` URL against the repositories registered
# on the machine, not over the network: both `dependency build` and `dependency update`
# check every URL is known before downloading anything, and abort with "no repository
# definition for ..." otherwise. The URLs in Chart.lock are the pin; these lines are
# what makes the machine able to act on it, and they are why a fresh laptop or CI
# runner can bootstrap at all. --force-update keeps them idempotent. Moving the
# dependencies to OCI references would remove this step entirely.
helm-repos:
	@helm repo add eks https://aws.github.io/eks-charts --force-update >/dev/null
	@helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
	@helm repo add grafana https://grafana.github.io/helm-charts --force-update >/dev/null
	@helm repo add argo https://argoproj.github.io/argo-helm --force-update >/dev/null
	@helm repo add external-secrets https://charts.external-secrets.io --force-update >/dev/null
	@helm repo update >/dev/null

# Reads one operator's toggle out of the bootstrap chart's values.yaml, so turning
# a tier off is an edit to that file rather than a commented-out block in here.
enabled = $(shell awk '$$0 == "$(1):" {f=1;next} /^[a-zA-Z]/{f=0} f&&/enabled:/{print $$2;exit}' $(BOOTSTRAP)/values.yaml)

# Bring the full stack up.
#
# Post-terraform bootstrap runs automatically:
#   1. kubeconfig updated so kubectl/helm can reach the new cluster
#   2. ALB Controller installed — required before any Ingress object is created
#   3. kube-prometheus-stack installed — monitoring up before app workloads land
#   4. Loki installed — log aggregation backend
#   5. Promtail installed — DaemonSet log shipper, forwards pod stdout to Loki
#   6. External Secrets Operator (ESO) installed — IRSA-authenticated, pulls platformcore/*
#      secrets from AWS Secrets Manager and materialises them as K8s Secrets
#   7. ArgoCD installed — GitOps controller; watches repo, syncs app chart to cluster
#      (app deploy is NOT triggered here — CI push → image tag commit → ArgoCD sync)
#
# Every operator version above comes from charts/platform-bootstrap/Chart.lock,
# not from whatever upstream published this morning. `make helm-pins` reports the
# pins against upstream; `make helm-relock` is the only way to move one.
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
	@echo "==> Vendoring pinned operator charts..."
	@$(MAKE) --no-print-directory helm-repos
	helm dependency build $(BOOTSTRAP)
	@echo "==> Installing ALB Controller..."
	@if [ "$(call enabled,aws-load-balancer-controller)" != "true" ]; then echo "  disabled in $(BOOTSTRAP)/values.yaml - skipping"; else \
	  ALB_ROLE=$$(cd terraform && terraform output -raw alb_controller_role_arn); \
	  VPC_ID=$$(cd terraform && terraform output -raw vpc_id); \
	  helm upgrade --install aws-load-balancer-controller $(VENDORED)/aws-load-balancer-controller-*.tgz \
	    -n kube-system \
	    --set clusterName=platformcore \
	    --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=$$ALB_ROLE" \
	    --set vpcId=$$VPC_ID \
	    --set region=us-east-1 \
	    --set replicaCount=1 \
	    --wait; \
	fi
	@echo "==> Installing kube-prometheus-stack..."
	@if [ "$(call enabled,kube-prometheus-stack)" != "true" ]; then echo "  disabled in $(BOOTSTRAP)/values.yaml - skipping"; else \
	  helm upgrade --install kps $(VENDORED)/kube-prometheus-stack-*.tgz \
	    -n monitoring --create-namespace \
	    -f helm/monitoring/values.yaml \
	    --wait --timeout 10m; \
	fi
	@echo "==> Installing Loki..."
	@if [ "$(call enabled,loki)" != "true" ]; then echo "  disabled in $(BOOTSTRAP)/values.yaml - skipping"; else \
	  helm upgrade --install loki $(VENDORED)/loki-*.tgz \
	    -n monitoring \
	    -f helm/monitoring/loki-values.yaml \
	    --wait --timeout 5m; \
	fi
	@echo "==> Installing Promtail..."
	@if [ "$(call enabled,promtail)" != "true" ]; then echo "  disabled in $(BOOTSTRAP)/values.yaml - skipping"; else \
	  helm upgrade --install promtail $(VENDORED)/promtail-*.tgz \
	    -n monitoring \
	    -f helm/monitoring/promtail-values.yaml \
	    --wait --timeout 5m; \
	fi
	@echo "==> Installing External Secrets Operator..."
	@if [ "$(call enabled,external-secrets)" != "true" ]; then echo "  disabled in $(BOOTSTRAP)/values.yaml - skipping"; else \
	  ESO_ROLE=$$(cd terraform && terraform output -raw eso_role_arn); \
	  helm upgrade --install external-secrets $(VENDORED)/external-secrets-*.tgz \
	    -n external-secrets --create-namespace \
	    -f helm/eso/values.yaml \
	    --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=$$ESO_ROLE" \
	    --wait --timeout 5m; \
	fi
	@echo "==> Installing ArgoCD..."
	@if [ "$(call enabled,argo-cd)" != "true" ]; then echo "  disabled in $(BOOTSTRAP)/values.yaml - skipping"; else \
	  helm upgrade --install argocd $(VENDORED)/argo-cd-*.tgz \
	    -n argocd --create-namespace \
	    -f helm/argocd/values.yaml \
	    --wait --timeout 5m; \
	fi
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
	  helm uninstall external-secrets -n external-secrets --ignore-not-found 2>/dev/null || true; \
	  kubectl delete namespace external-secrets --ignore-not-found 2>/dev/null || true; \
	  helm uninstall argocd -n argocd --ignore-not-found 2>/dev/null || true; \
	  kubectl delete namespace argocd --ignore-not-found 2>/dev/null || true; \
	  helm uninstall kps -n monitoring --ignore-not-found 2>/dev/null || true; \
	  helm uninstall loki -n monitoring --ignore-not-found 2>/dev/null || true; \
	  helm uninstall promtail -n monitoring --ignore-not-found 2>/dev/null || true; \
	  kubectl delete pvc --all -n monitoring --wait=true --ignore-not-found 2>/dev/null || true; \
	  kubectl delete namespace monitoring --ignore-not-found 2>/dev/null || true; \
	  helm uninstall platformcore -n platformcore --ignore-not-found 2>/dev/null || true; \
	  kubectl delete ingress --all -A --ignore-not-found 2>/dev/null || true; \
	  echo "  Waiting 60s for ALB controller to de-register and delete the ALBs..."; \
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
	  helm uninstall external-secrets -n external-secrets --ignore-not-found 2>/dev/null || true; \
	  kubectl delete namespace external-secrets --ignore-not-found 2>/dev/null || true; \
	  helm uninstall argocd -n argocd --ignore-not-found 2>/dev/null || true; \
	  kubectl delete namespace argocd --ignore-not-found 2>/dev/null || true; \
	  helm uninstall kps -n monitoring --ignore-not-found 2>/dev/null || true; \
	  helm uninstall loki -n monitoring --ignore-not-found 2>/dev/null || true; \
	  helm uninstall promtail -n monitoring --ignore-not-found 2>/dev/null || true; \
	  kubectl delete pvc --all -n monitoring --wait=true --ignore-not-found 2>/dev/null || true; \
	  kubectl delete namespace monitoring --ignore-not-found 2>/dev/null || true; \
	  helm uninstall platformcore -n platformcore --ignore-not-found 2>/dev/null || true; \
	  kubectl delete ingress --all -A --ignore-not-found 2>/dev/null || true; \
	  echo "  Waiting 60s for ALB controller to de-register and delete the ALBs..."; \
	  sleep 60; \
	  helm uninstall aws-load-balancer-controller -n kube-system --ignore-not-found 2>/dev/null || true; \
	else \
	  echo "  Cluster not found - skipping Helm cleanup."; \
	fi
	cd terraform && terraform destroy -auto-approve



# End-of-day teardown + morning rebuild shortcut.
rebuild: down up



# Re-resolve the operator charts against Chart.yaml and rewrite Chart.lock.
#
# The only supported way to move an operator version: edit the version in
# charts/platform-bootstrap/Chart.yaml, run this, and review the Chart.lock diff.
# Running it without editing Chart.yaml is a no-op beyond the generated timestamp,
# which is the point - the lock does not drift on its own.
helm-relock: helm-repos
	helm dependency update $(BOOTSTRAP)


# What is pinned, and what has upstream published since?
helm-pins:
	@bash scripts/helm-pins.sh $(BOOTSTRAP)


# Render the application chart and assert the properties its templates promise.
#
# The unit suite imports app/main.py and cannot see the chart, so the defect that
# started this work was invisible to it. This is the layer that catches it, and it
# needs no cluster: probe wiring, probe separation, image digests, and the two
# tuning invariants that were previously only sentences in comments.
chart-check:
	@bash scripts/check-chart.sh charts/platformcore


# Recompile the Python locks from the .in files.
#
# Resolution targets the image's interpreter and platform rather than whichever
# laptop runs this, so the lock describes what will actually be installed in the
# container. --generate-hashes writes a digest per artifact, which is what gives
# the Dockerfile's --require-hashes something to check. requirements-dev.txt is a
# superset of the runtime lock rather than a second file beside it: pip refuses to
# mix hashed and unhashed requirement files, and two independently resolved locks
# would eventually disagree about a shared transitive dependency. Including the
# runtime intent is not enough to prevent that: --constraint pins the dev resolution
# to the versions the runtime lock already chose, so CI cannot test a stack the image
# does not ship. Order matters here - the runtime lock is written first, and the dev
# compile reads it.
lock-python:
	@command -v uv >/dev/null || { echo "uv not found - brew install uv"; exit 1; }
	uv pip compile app/requirements.in \
	  --generate-hashes \
	  --python-version 3.12 \
	  --python-platform x86_64-unknown-linux-gnu \
	  --custom-compile-command "make lock-python" \
	  -o app/requirements.txt
	uv pip compile app/requirements-dev.in \
	  --constraint app/requirements.txt \
	  --generate-hashes \
	  --python-version 3.12 \
	  --python-platform x86_64-unknown-linux-gnu \
	  --custom-compile-command "make lock-python" \
	  -o app/requirements-dev.txt


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
