# PlatformCore

A reference AWS platform that runs a containerised FastAPI workload on Amazon EKS, backed by managed PostgreSQL, fronted by an ALB, and provisioned end to end through Terraform and Helm. The repository is structured the way a small platform team would structure a real product: modules along architectural boundaries, contracts at the edges, and no resource present without a stated reason.

[![Terraform](https://img.shields.io/badge/Terraform-1.9%2B-844FBA?logo=terraform&logoColor=white)](https://www.terraform.io/)
[![AWS EKS](https://img.shields.io/badge/EKS-1.33-FF9900?logo=amazon-aws&logoColor=white)](https://aws.amazon.com/eks/)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-1.33-326CE5?logo=kubernetes&logoColor=white)](https://kubernetes.io/)
[![Helm](https://img.shields.io/badge/Helm-3-0F1689?logo=helm&logoColor=white)](https://helm.sh/)
[![CI](https://img.shields.io/badge/CI-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white)](.github/workflows/ci.yml)
[![Licence](https://img.shields.io/badge/Licence-MIT-green.svg)](#licence)


## Overview

PlatformCore provisions a small but representative production topology on AWS. A FastAPI service runs as a Kubernetes Deployment behind an internet-facing Application Load Balancer, talks to Amazon RDS for PostgreSQL using short-lived IAM tokens, and ships logs and metrics into an in-cluster Prometheus and Loki stack. Every billable resource is managed by Terraform. Every workload manifest lives inside a single Helm chart. CI builds the application image, pushes it to ECR, and rolls the release out through `helm upgrade`.

The project exists as a working substrate for the patterns a platform engineer is expected to defend at interview: VPC layout and egress economics, IRSA and OIDC federation, EKS access entries over `aws-auth`, IP target-type ALBs, retain-policy storage classes for stateful workloads, and the cost trade-offs that drive each of those choices.


## Architecture

```
                       Internet
                           │
                           ▼
                    Route 53 (DNS)
                           │
                           ▼
              Application Load Balancer
              ACM TLS, target-type = ip
                           │
                           ▼
   VPC 10.0.0.0/16 across two Availability Zones
   ┌──────────────────────────────────────────────────────────┐
   │ Public subnets   ALB ENIs, NAT Gateway                   │
   │ Private subnets  EKS managed nodes, RDS, Endpoints       │
   │                                                          │
   │   EKS 1.33 managed node group                            │
   │     • FastAPI Deployment        (IRSA → RDS IAM auth)    │
   │     • nginx Deployment          (sidecar reverse proxy)  │
   │     • Postgres StatefulSet      (gp3-retain volumes)     │
   │     • AWS Load Balancer Ctrlr   (IRSA)                   │
   │     • AWS EBS CSI Driver        (IRSA, managed add-on)   │
   │     • kube-prometheus-stack     (metrics, Grafana)       │
   │     • Loki + Promtail           (log aggregation)        │
   │                                                          │
   │   Amazon RDS for PostgreSQL 17, encrypted, IAM auth      │
   │                                                          │
   │   VPC Endpoints (Interface)                              │
   │     ssm, ssmmessages, ec2messages, ec2,                  │
   │     ecr.api, ecr.dkr, logs, secretsmanager, sts          │
   │   VPC Endpoint (Gateway)                                 │
   │     s3 (route-table prefix list)                         │
   └──────────────────────────────────────────────────────────┘
```

Pod traffic to AWS APIs leaves the cluster on Interface Endpoints, which keeps it off the NAT Gateway and off the per-gigabyte egress meter. Traffic destined for public container registries and third-party APIs uses the NAT. The ALB targets Pod IPs directly through the AWS VPC CNI, which removes the kube-proxy hop entirely and makes AWS security groups behave the same on Pods as they would on an EC2 instance.


## Technology stack

| Concern | Tooling |
| :--- | :--- |
| Cloud | AWS, `us-east-1` |
| Infrastructure as code | Terraform 1.9, `hashicorp/aws ~> 5.0`, S3 remote state, DynamoDB locking |
| Container orchestration | Amazon EKS 1.33, managed node group on `t3.small` |
| Local runtime | Docker Engine, Docker Compose v2, kind for cluster parity |
| Edge | Application Load Balancer, ACM, Route 53, AWS Load Balancer Controller (chart 1.8.4) |
| Storage | Amazon EBS gp3 through a `Retain` StorageClass, EBS CSI Driver as a managed add-on |
| Data | Amazon RDS for PostgreSQL 17, `db.t3.micro`, encryption at rest, RDS IAM Authentication |
| Application | FastAPI on Python 3.12, uvicorn, nginx 1.27-alpine |
| Identity | IAM, EKS Access Entries, OIDC federation, IRSA for the controller, the CSI driver and FastAPI |
| Packaging | A single Helm chart that ships nginx, FastAPI and Postgres tiers under one release |
| Observability | kube-prometheus-stack, Grafana, Loki, Promtail |
| Secrets | AWS Secrets Manager via VPC Endpoint, Kubernetes Secrets consumed by `secretKeyRef` |
| Continuous integration | GitHub Actions. Compose validated in base and merged modes, image built and pushed to ECR, Helm release upgraded on `main` |


## Repository layout

```
platformCore/
├── app/                       FastAPI application, Dockerfile, requirements
├── nginx/                     nginx reverse-proxy configuration
├── db/                        Idempotent SQL bootstrap
├── docker-compose.yml         Production-shaped local stack
├── docker-compose.override.yml  Developer overlay
├── kind-config.yaml           Offline cluster topology for iteration
│
├── terraform/
│   ├── main.tf                Composition root, module DAG, cross-module wiring
│   ├── provider.tf            AWS provider pinned to ~> 5.0
│   ├── backend.tf             S3 state and DynamoDB lock table
│   ├── security_groups.tf     Cross-module rules that would otherwise form a cycle
│   ├── storageclass.tf        gp3-retain StorageClass
│   └── modules/
│       ├── network/           VPC, subnets, IGW, NAT, route tables, VPC endpoints
│       ├── data/              RDS instance, parameter and subnet groups, Secrets Manager
│       ├── compute/           EC2 baseline path, IMDSv2-enforced, SSM-only access
│       ├── edge/              ALB, listeners, ACM, Route 53
│       └── eks/               Cluster, node group, OIDC provider, Access Entries, IRSA bundles
│
├── charts/
│   └── platformcore/          Umbrella chart that ships the full application stack
│       ├── Chart.yaml
│       ├── values.yaml        Public API of the chart
│       └── templates/         nginx, fastapi and postgres tiers, plus shared helpers
│
├── helm/
│   └── monitoring/            Values overrides for kube-prometheus-stack, Loki, Promtail
│
├── scripts/
│   └── rds-bootstrap.sh       Idempotent RDS IAM user provisioning
│
├── .github/workflows/ci.yml   Validate, build and push to ECR, helm upgrade on main
└── Makefile                   up, down, status, curl, logs lifecycle helpers
```


## Prerequisites

You will need an AWS account with administrative access for bootstrap, Terraform 1.9 or newer, Helm 3, `kubectl` matching the cluster Kubernetes minor version, the AWS CLI v2 configured with credentials, and Docker Engine with Compose v2 for the local stack.


## Local development

The Compose topology mirrors the in-cluster shape. nginx fronts FastAPI, which talks to a local Postgres container. The override file layers on watchfiles reload and bind mounts for the application source.

```bash
cp .env.example .env
docker compose up --build
curl http://localhost/
```


## Bootstrap, once per AWS account

The remote state backend is provisioned out of band, before the first `terraform init`, to avoid the chicken and egg problem of state-managing infrastructure managing its own state.

```bash
aws s3api create-bucket \
  --bucket platformcore-tf-state \
  --region us-east-1

aws s3api put-bucket-versioning \
  --bucket platformcore-tf-state \
  --versioning-configuration Status=Enabled

aws s3api put-public-access-block \
  --bucket platformcore-tf-state \
  --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws dynamodb create-table \
  --table-name platformcore-tf-locks \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1
```


## Bringing the platform up

`make up` is the single entry point. It applies Terraform, refreshes kubeconfig, adds the relevant Helm repositories, installs the AWS Load Balancer Controller with the cluster name, VPC ID and IRSA role wired through values, installs the kube-prometheus-stack, Loki and Promtail releases, and finally runs the RDS bootstrap script to provision the IAM-authenticated database user.

```bash
make up
```

After `make up` finishes, a push to `main` triggers the CI pipeline, which builds the application image, tags it with the commit SHA, pushes it to ECR, and rolls the release out through `helm upgrade --install`. The CI workflow rolls back on failure and bounds itself to a five-minute timeout.

To inspect the running stack:

```bash
kubectl get pods -n platformcore
kubectl get ingress -n platformcore
make status
make logs
```

To tear the platform down:

```bash
make down-all
```


## Design choices worth calling out

A short list of decisions that meaningfully shaped the platform, paired with the alternative they displaced.

**Interface Endpoints alongside a NAT Gateway, not instead of one.** The original network had no NAT and relied entirely on endpoints. That topology broke the day a workload needed an image from `public.ecr.aws`, which is a distinct service from private ECR and has no VPC Endpoint. The NAT was added, the endpoints were kept, and AWS API traffic still avoids the NAT's per-gigabyte meter. The combined topology is more expensive than endpoints alone by the NAT's fixed hourly cost and meaningfully cheaper than NAT alone at any non-trivial AWS API traffic level.

**Access Entries over the `aws-auth` ConfigMap.** Both mechanisms map IAM identities onto Kubernetes RBAC subjects. They diverge at the failure mode. A corrupted `aws-auth` ConfigMap can only be repaired through `kubectl`, which the same corruption may have rendered unreachable. Access Entries live on the AWS API surface and recover through the same channel that provisioned the cluster.

**IRSA for every workload that calls an AWS API.** No static credentials live in the cluster. The FastAPI Deployment, the AWS Load Balancer Controller, and the EBS CSI Driver each carry their own IAM role, with trust policies locked to a specific ServiceAccount via the `sub` claim and to `sts.amazonaws.com` via the `aud` claim. The same three-resource pattern (role, policy, attachment) is repeated identically.

**`target-type = ip` on every ALB.** The controller registers Pod IPs into the target group directly, which removes the kube-proxy hop, removes the requirement to open node security groups across the NodePort range, and yields AZ-aware traffic distribution out of the box.

**`Retain` reclaim policy with `WaitForFirstConsumer` binding on the storage class.** The reclaim policy keeps EBS volumes after a PVC is deleted, so stateful workloads have a manual rescue path. The binding mode defers EBS provisioning until a Pod is scheduled, so the volume lands in the same Availability Zone as the chosen node. EBS is AZ-local, so this is not a stylistic preference. It is structurally required on multi-AZ clusters.

**Pinned versions across the entire dependency chain.** Terraform provider, Helm chart, container image tags, GitHub Actions, and the IAM policy documents fetched from upstream repositories. A fresh `make up` against this repository builds the same infrastructure today as it would in six months. The cost is the obligation to actively bump versions. The alternative, silent regression at a moment the operator did not choose, is the failure mode pinning exists to prevent.


## Observability

`kube-prometheus-stack` runs in the `monitoring` namespace and ships with the standard ServiceMonitor and Alertmanager primitives. Grafana is exposed as a `ClusterIP` Service and accessed through `kubectl port-forward` during development. Loki ingests Promtail-shipped logs from every Pod stdout stream. The application emits Prometheus metrics through `prometheus-fastapi-instrumentator`, surfacing request latency, request volume and per-route status code distributions without any application code change.


## Continuous integration

GitHub Actions runs three jobs on every change. The validate job parses the Compose configuration twice, once against the base file alone (the production-shaped topology) and once against the merged base plus override (the developer topology). Catching both modes on every change closes a class of bug where an override-only typo would have shipped without ever being parsed in CI.

On `main`, the build job tags the application image with the commit SHA and pushes it to ECR. The deploy job updates kubeconfig and runs `helm upgrade --install` against the `platformcore` release, with `--rollback-on-failure` and a five-minute timeout. The deploy job is gated by explicit `needs:` dependencies so failures surface cheaply.


## Roadmap

The next extensions stay continuous with the choices established above rather than displacing them.

GitOps reconciliation through Argo CD, replacing the push-based `helm upgrade` step with a pull-based controller reading from this repository. External Secrets Operator with AWS Secrets Manager as the backend, with rotation hooks for credentials that support them. Image supply-chain scanning at the CI boundary, where every upstream image gets pulled, scanned through Trivy, and republished into private ECR before any cluster pulls it. A managed PostgreSQL operator such as CloudNativePG to layer streaming replication and automated failover on top of the StatefulSet substrate that the cluster already supports. Per-AZ NAT redundancy for production-grade egress availability.


## Licence

Released under the MIT Licence. See `LICENSE` for the full text.
