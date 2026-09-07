#--------------------------------------------------------------------------------------------------------
# ON-CALL LAB
#--------------------------------------------------------------------------------------------------------
#
#   - a three-node kind cluster shaped like the EKS node group
#
#   - the fault library applied one stage at a time
#
#   - one collection cycle on demand, writing to the SQLite buffer
#
# Separate from the repo-root Makefile on purpose. oncall/.gitignore already states
# that this subproject is meant to be liftable into its own repository, and targets
# that only make sense here would break that the moment they lived at the root.
#
#   make -C oncall lab-up
#   make -C oncall lab-stage STAGE=1
#   make -C oncall collect
#
# No Postgres is needed for any of this. Collection writes only to the local SQLite
# buffer; the store is reached by shipper.py alone, so dev/compose.yaml stays down.

.PHONY: lab-up lab-down lab-stage lab-all lab-clean lab-status venv collect env help

CLUSTER    ?= oncall
LAB_NS     ?= oncall-lab
WORKLOADS  := dev/broken-workloads.yaml
VENV       ?= .venv
PY         ?= python3.11

# Pinned to the EKS control plane's minor. Object shapes move between Kubernetes
# releases — 1.33 is where v1 Endpoints started warning as deprecated — so a lab on a
# different minor tests a different API surface than the one production answers with.
#
# A tag rather than a digest, matching the reasoning in docker-compose.yml: this
# cluster is recreated from scratch several times an hour and the cost of a stale
# reference is a rebuild, not an unreviewed artifact reaching a cluster that matters.
K8S_VERSION ?= v1.33.0


help:
	@echo "lab-up      create the $(CLUSTER) kind cluster ($(K8S_VERSION), 3 nodes)"
	@echo "lab-down    delete it"
	@echo "lab-stage   apply one stage of the fault library   (STAGE=1..5)"
	@echo "lab-all     apply every fault at once"
	@echo "lab-clean   delete the $(LAB_NS) namespace, keep the cluster"
	@echo "lab-status  what is running and what is broken"
	@echo "venv        build $(VENV) with $(PY)"
	@echo "collect     one collection cycle for every registered source"
	@echo "env         the environment this expects, ready to eval"


#--------------------------------------------------------------------------------------------------------
# CLUSTER
#--------------------------------------------------------------------------------------------------------


# The node labels are printed rather than assumed. kubelet may only self-apply labels
# the NodeRestriction plugin allows, and if that ever changes these come back empty,
# which would silently make the placement fields null on every local row while
# staying populated on EKS.
lab-up:
	@command -v kind >/dev/null || { echo "kind not found - brew install kind"; exit 1; }
	kind create cluster --name $(CLUSTER) --config dev/kind-config.yaml \
	  --image kindest/node:$(K8S_VERSION) --wait 120s
	@echo "==> Nodes and the labels k8s_nodes reads:"
	@kubectl --context kind-$(CLUSTER) get nodes \
	  -o custom-columns='NODE:.metadata.name,ZONE:.metadata.labels.topology\.kubernetes\.io/zone,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type,MAXPODS:.status.allocatable.pods'


lab-down:
	kind delete cluster --name $(CLUSTER)


#--------------------------------------------------------------------------------------------------------
# FAULTS
#--------------------------------------------------------------------------------------------------------
#
# The namespace is created separately because `kubectl apply -l` filters on object
# labels and the Namespace carries no stage, deliberately: it belongs to every stage.
#
# Stage 5 needs two actions rather than an apply, because the transition is the fault.
# They are printed rather than run, so the moment they happen is chosen.


lab-stage:
	@test -n "$(STAGE)" || { echo "usage: make lab-stage STAGE=1"; exit 1; }
	@kubectl get namespace $(LAB_NS) >/dev/null 2>&1 || kubectl create namespace $(LAB_NS)
	kubectl apply -f $(WORKLOADS) -l stage=$(STAGE)
	@if [ "$(STAGE)" = "5" ]; then \
	  echo ""; \
	  echo "==> Stage 5 is transitions. Nothing is broken until you run these:"; \
	  echo "    kubectl -n $(LAB_NS) set image deployment/stuckrollout app=registry.invalid/app:v2"; \
	  echo "    kubectl -n $(LAB_NS) delete pod -l app=stuckterminating"; \
	  echo "    kubectl cordon \$$(kubectl get nodes -o name | tail -1 | cut -d/ -f2)"; \
	fi


lab-all:
	@kubectl get namespace $(LAB_NS) >/dev/null 2>&1 || kubectl create namespace $(LAB_NS)
	kubectl apply -f $(WORKLOADS)


# The namespace rather than the objects, because stuckterminating holds a 120s grace
# period and deleting it by name blocks on the same wait either way.
lab-clean:
	kubectl delete namespace $(LAB_NS) --ignore-not-found
	@echo "note: stuckterminating holds a 120s grace period, so this is not instant"


lab-status:
	@kubectl -n $(LAB_NS) get pods -o wide 2>/dev/null || echo "namespace $(LAB_NS) does not exist"
	@echo ""
	@kubectl -n $(LAB_NS) get svc,endpointslice 2>/dev/null || true
	@echo ""
	@kubectl get nodes -o custom-columns='NODE:.metadata.name,READY:.status.conditions[-1].type,SCHEDULABLE:.spec.unschedulable'


#--------------------------------------------------------------------------------------------------------
# THE AGENT
#--------------------------------------------------------------------------------------------------------
#
# 3.11 is a floor, not a preference: landing_zone/blobs.py imports datetime.UTC, which
# does not exist before it. Naming the interpreter here turns a confusing ImportError
# deep in a collector into a refusal at setup.


venv:
	@command -v $(PY) >/dev/null || { echo "$(PY) not found. Set PY=... to a 3.11+ interpreter"; exit 1; }
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements.txt
	@$(VENV)/bin/python -c "import sys; print('venv ready on', '.'.join(map(str, sys.version_info[:3])))"


# Scoped to the lab namespace and named for the cluster it came from. cluster is part
# of every fingerprint, so a kind run and an EKS run can share one buffer and still be
# told apart — which is the whole point of running both.
env:
	@echo "export ONCALL_CLUSTER=kind-$(CLUSTER)"
	@echo "export ONCALL_NAMESPACES=$(LAB_NS)"


collect:
	@test -d $(VENV) || { echo "no $(VENV) - run: make -C oncall venv"; exit 1; }
	cd .. && ONCALL_CLUSTER=$${ONCALL_CLUSTER:-kind-$(CLUSTER)} \
	  ONCALL_NAMESPACES=$${ONCALL_NAMESPACES:-$(LAB_NS)} \
	  oncall/$(VENV)/bin/python oncall/dev/collect.py
