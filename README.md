# PlatformCore

A reference AWS platform that runs a containerised FastAPI workload on Amazon EKS, backed by managed PostgreSQL, fronted by an Application Load Balancer, and provisioned from nothing through Terraform, Helm and Argo CD. The repository is structured the way a small platform team would structure a real product: modules along architectural boundaries, contracts at the edges, and no resource present without a stated reason.

[![Terraform](https://img.shields.io/badge/Terraform-1.9%2B-844FBA?logo=terraform&logoColor=white)](https://www.terraform.io/)
[![AWS EKS](https://img.shields.io/badge/EKS-1.33-FF9900?logo=amazon-aws&logoColor=white)](https://aws.amazon.com/eks/)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-1.33-326CE5?logo=kubernetes&logoColor=white)](https://kubernetes.io/)
[![Helm](https://img.shields.io/badge/Helm-3-0F1689?logo=helm&logoColor=white)](https://helm.sh/)
[![Argo CD](https://img.shields.io/badge/GitOps-Argo%20CD-EF7B4D?logo=argo&logoColor=white)](https://argo-cd.readthedocs.io/)
[![CI](https://img.shields.io/badge/CI-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white)](.github/workflows/ci.yml)
[![Licence](https://img.shields.io/badge/Licence-MIT-green.svg)](#licence)


## Overview

PlatformCore provisions a small but honest production topology on AWS. A FastAPI service runs as a Kubernetes Deployment behind a public Application Load Balancer, talks to Amazon RDS for PostgreSQL using IAM tokens that expire in minutes rather than stored passwords, and ships its logs and metrics into Prometheus, Grafana and Loki running inside the cluster.

Terraform owns every billable resource. A single Helm chart owns every workload manifest. Deployment is GitOps: CI builds the application image, pushes it to ECR and commits the new tag back into the chart, then Argo CD notices the commit and syncs the cluster to match. Nothing in CI ever holds cluster credentials.

The project exists as a working substrate for the patterns a platform engineer is expected to defend under questioning: VPC layout and egress economics, IRSA and OIDC federation, EKS Access Entries, load balancers that target Pods directly, storage that survives workload deletion, and the cost tradeoffs behind each of those choices.


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
   │     • nginx Deployment          (reverse proxy)          │
   │     • Postgres StatefulSet      (gp3-retain volumes)     │
   │     • AWS Load Balancer Ctrlr   (IRSA)                   │
   │     • AWS EBS CSI Driver        (IRSA, managed add-on)   │
   │     • External Secrets Operator (IRSA → Secrets Manager) │
   │     • Argo CD                   (GitOps sync from repo)  │
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

Pod traffic to AWS APIs leaves the cluster on Interface Endpoints, which keeps it off the NAT Gateway and off the metered egress path. Traffic bound for public container registries and external APIs uses the NAT. The ALB registers Pod IPs directly through the AWS VPC CNI, which removes the `kube-proxy` hop entirely and makes AWS security groups behave the same on Pods as they would on an EC2 instance.


## How a change reaches production

A push to `main` moves through three CI jobs. The first parses the Compose configuration twice, once against the base file alone (the shape that actually deploys) and once with the developer override merged in, so a typo that lives only in the override cannot ship unparsed. The second builds the application image, tags it with the commit SHA and pushes it to ECR. The third commits the new tag into `charts/platformcore/values.yaml`, marked `[skip ci]` so the pipeline does not feed itself.

That is where CI stops. Argo CD runs inside the cluster, watches this repository, sees the values change and syncs the release. Cluster credentials never leave AWS: CI can push images and commit to the repository, and the cluster pulls its own desired state. Rolling back a bad deploy is `git revert`.


## Technology stack

| Concern | Tooling |
| :--- | :--- |
| Cloud | AWS, `us-east-1` |
| Infrastructure as code | Terraform 1.9, `hashicorp/aws ~> 5.0`, S3 remote state, DynamoDB locking |
| Container orchestration | Amazon EKS 1.33, managed node group on `t3.small` |
| Local runtime | Docker Engine, Docker Compose v2, kind for cluster parity |
| Edge | Application Load Balancer, ACM, Route 53, AWS Load Balancer Controller (chart 3.3.0) |
| Storage | Amazon EBS gp3 through a `Retain` StorageClass, EBS CSI Driver as a managed add-on |
| Data | Amazon RDS for PostgreSQL 17, `db.t3.micro`, encryption at rest, RDS IAM Authentication |
| Application | FastAPI on Python 3.12, uvicorn, nginx `1.27-alpine` |
| Identity | IAM, EKS Access Entries, OIDC federation, IRSA for every workload that touches an AWS API |
| Packaging | A single Helm chart that ships the nginx, FastAPI and Postgres tiers under one release |
| Delivery | GitHub Actions for build and tag, Argo CD for sync, the repository as the source of truth |
| Secrets | External Secrets Operator backed by AWS Secrets Manager over a VPC Endpoint |
| Observability | `kube-prometheus-stack`, Grafana, Loki, Promtail |


## Repository layout

```
platformCore/
├── app/                       FastAPI application, Dockerfile, requirements
├── nginx/                     nginx reverse proxy configuration
├── db/                        Idempotent SQL bootstrap
├── docker-compose.yml         Local stack shaped like production
├── docker-compose.override.yml  Developer overlay
├── kind-config.yaml           Offline cluster topology for iteration
│
├── terraform/
│   ├── main.tf                Composition root, module DAG, cross module wiring
│   ├── provider.tf            AWS provider pinned to ~> 5.0
│   ├── backend.tf             S3 state and DynamoDB lock table
│   ├── security_groups.tf     Cross module rules that would otherwise form a cycle
│   ├── storageclass.tf        gp3-retain StorageClass
│   └── modules/
│       ├── network/           VPC, subnets, IGW, NAT, route tables, VPC endpoints
│       ├── data/              RDS instance, parameter and subnet groups, Secrets Manager
│       ├── compute/           EC2 baseline path, IMDSv2 enforced, SSM access only
│       ├── edge/              ALB, listeners, ACM, Route 53
│       └── eks/               Cluster, node group, OIDC provider, Access Entries, IRSA bundles
│
├── charts/
│   └── platformcore/          Umbrella chart that ships the full application stack
│       ├── Chart.yaml
│       ├── values.yaml        Public API of the chart, CI writes the image tag here
│       └── templates/         nginx, fastapi and postgres tiers, plus shared helpers
│
├── argocd/
│   └── application.yaml       Argo CD Application pointing at the chart
│
├── helm/
│   ├── monitoring/            Values for kube-prometheus-stack, Loki, Promtail
│   ├── eso/                   Values for the External Secrets Operator
│   └── argocd/                Values for Argo CD
│
├── scripts/
│   └── rds-bootstrap.sh       Idempotent RDS IAM user provisioning
│
├── .github/workflows/ci.yml   Validate, build and push, commit the new tag
└── Makefile                   up, down, rebuild, status, curl, logs
```


## Prerequisites

You will need an AWS account with administrative access for bootstrap, Terraform 1.9 or newer, Helm 3, `kubectl` matching the cluster Kubernetes minor version, the AWS CLI v2 configured with credentials, and Docker Engine with Compose v2 for the local stack.


## Local development

The Compose topology mirrors the shape running in the cluster. nginx fronts FastAPI, which talks to a local Postgres container. The override file layers on watchfiles reload and bind mounts for the application source.

```bash
cp .env.example .env
docker compose up --build
curl http://localhost/
```


## Bootstrap, once per AWS account

The remote state backend is provisioned by hand before the first `terraform init`. This avoids the circular problem of Terraform managing the bucket that stores its own state.

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

`make up` is the single entry point. It applies Terraform, refreshes kubeconfig, then installs the platform layer through Helm: the AWS Load Balancer Controller with the cluster name, VPC ID and IRSA role wired through values, the monitoring stack, the External Secrets Operator with its own IRSA role, and Argo CD. It finishes by running the RDS bootstrap script, which provisions the database user that authenticates with IAM.

```bash
make up
```

The application itself is deliberately not deployed here. Pushing to `main` starts the pipeline, and Argo CD carries the release into the cluster, so image tagging stays owned by CI and the running state stays owned by git.

To inspect the running stack:

```bash
kubectl get pods -n platformcore
kubectl get ingress -n platformcore
make status
make curl
make logs
```

To tear the platform down:

```bash
make down-all
```


## Cost

Two habits keep this affordable on a personal account: route AWS traffic around the NAT, and tear the platform down when it is idle.

Interface Endpoints carry the cluster's AWS API traffic (ECR pulls, STS token exchange, Secrets Manager reads, CloudWatch Logs, SSM sessions) inside the VPC, so none of it crosses the NAT Gateway's data processing meter. The S3 endpoint is a Gateway endpoint and costs nothing. The NAT exists only for destinations that have no endpoint, chiefly `public.ecr.aws` and Docker Hub. There is one NAT rather than one per Availability Zone; a second would buy availability that a reference platform does not need, and the omission is recorded on the roadmap rather than forgotten.

Compute is sized to the workload: a single `t3.small` node group, a `db.t3.micro` RDS instance, gp3 volumes that undercut gp2 on price per gigabyte, and a DynamoDB lock table billed per request so an idle repository locks for free.

The Makefile treats teardown as a routine operation rather than an emergency. `make down` destroys the billable resources (cluster, nodes, NAT, ALB, RDS, endpoints) while keeping the free scaffolding (VPC, subnets, route tables, IAM roles, ECR), and `make rebuild` brings everything back the next morning from the same state. `make down-all` removes the graph entirely. Left running, the largest line items are the EKS control plane at roughly $73 per month and the NAT Gateway at roughly $33 per month. Torn down between sessions, the bill shrinks to state storage and ECR pennies.


## Design choices worth calling out

A short list of decisions that meaningfully shaped the platform, each paired with the alternative it displaced.

**Interface Endpoints alongside a NAT Gateway, not instead of one.** The original network had no NAT and relied entirely on endpoints. That topology broke the day a workload needed an image from `public.ecr.aws`, which is a distinct service from private ECR and has no VPC Endpoint. The NAT was added, the endpoints were kept, and AWS API traffic still avoids the NAT meter. The combined topology costs more than endpoints alone by the fixed hourly price of the NAT, and costs meaningfully less than NAT alone once AWS API traffic amounts to anything.

**GitOps over push deploys.** An earlier pipeline ran `helm upgrade` from CI. It worked, and it meant cluster admin credentials living in GitHub. The current pipeline ends at a git commit; Argo CD, running in the cluster with no inbound exposure, pulls the change and applies it. The repository became the single source of truth for what runs, and rollback became `git revert`.

**Access Entries over the `aws-auth` ConfigMap.** Both mechanisms map IAM identities onto Kubernetes RBAC subjects. They diverge at the failure mode. A corrupted `aws-auth` ConfigMap can only be repaired through `kubectl`, which the same corruption may have rendered unreachable. Access Entries live on the AWS API surface and recover through the same channel that provisioned the cluster.

**IRSA for every workload that calls an AWS API.** No static credentials live in the cluster. The FastAPI Deployment, the AWS Load Balancer Controller, the EBS CSI Driver and the External Secrets Operator each carry their own IAM role, with trust locked to a specific ServiceAccount through the `sub` claim and to `sts.amazonaws.com` through the `aud` claim. The same pattern of role, policy and attachment repeats identically for each.

**`target-type = ip` on every ALB.** The controller registers Pod IPs into the target group directly, which removes the `kube-proxy` hop, removes the need to open node security groups across the NodePort range, and gives sensible traffic distribution across Availability Zones out of the box.

**`Retain` reclaim policy with `WaitForFirstConsumer` binding on the storage class.** The reclaim policy keeps EBS volumes after a PVC is deleted, so stateful workloads have a manual rescue path. The binding mode defers provisioning until a Pod is scheduled, so the volume lands in the same Availability Zone as the chosen node. An EBS volume lives in exactly one zone, so on a cluster spanning two of them this is not a stylistic preference. It is structurally required.

**Pinning, one mechanism per layer.** Reproducibility is not a single habit applied everywhere; it is four different mechanisms, each chosen by what the layer actually offers. Where a lockfile exists, the lockfile is the pin and the file beside it is only intent: `.terraform.lock.hcl` records provider versions with their platform hashes, `charts/platform-bootstrap/Chart.lock` records the six operator charts installed before the application release, and `app/requirements.txt` is compiled from `app/requirements.in` with hashes for every transitive dependency, then installed with `--require-hashes` so a re-uploaded artifact fails the image build instead of entering it. Where no lockfile mechanism exists, the digest is the lock: nginx, Postgres and the application's own base image are written as `tag@sha256:...`, the tag saying which version was intended and the digest deciding what actually resolves. Where the upgrade is driven by the cloud rather than by a dependency resolver, it takes an explicit provider argument: RDS defaults `auto_minor_version_upgrade` to true, so the database engine used to move in a maintenance window with nothing changing in this repository; it is now false against a fully qualified `engine_version`. And where a layer offers only a mutable reference, that is stated rather than dressed up — GitHub Actions are pinned to major tags, and a tag is a pointer its owner can repoint.

The honest residual is EKS. AWS force-upgrades control planes at the end of standard support for a Kubernetes minor version, and no amount of pinning prevents it: the cluster's `version` argument in `modules/eks/cluster.tf` decides when the upgrade happens up to that date, and after it AWS decides. RDS is the same story in miniature — turning off automatic minor upgrades moves the patch decision to a human, and AWS still forces the version at end of standard support. So the claim this section can honestly make is narrower than "nothing changes without a commit", and more useful: every version here moves because someone edited a line, right up to the boundary where the provider stops asking. The price is an upgrade obligation, since a pin that is never reviewed is just an old version with better paperwork. `make helm-pins` reports each pinned chart against what upstream has published since, which makes the review a command rather than a memory.


## Observability

`kube-prometheus-stack` runs in the `monitoring` namespace with the standard ServiceMonitor and Alertmanager primitives. Grafana is a `ClusterIP` Service reached through `kubectl port-forward` during development. Promtail ships every Pod's stdout into Loki. The application exposes Prometheus metrics through `prometheus-fastapi-instrumentator`, which surfaces request latency, request volume and the status code distribution per route without any change to application code.


## Roadmap

The next extensions stay continuous with the choices above rather than displacing them. Bump automation over the pins described above — Renovate or Dependabot opening one pull request per digest, chart or lockfile move, so that reviewing an upgrade stays deliberate without the pins quietly going stale, and so that action tags can become action SHAs without a standing manual chore. Image supply chain scanning at the CI boundary, where every upstream image is pulled, scanned with Trivy and republished into private ECR before the cluster touches it. A managed PostgreSQL operator such as CloudNativePG to layer streaming replication and automated failover onto the StatefulSet substrate already in place. A NAT Gateway in each Availability Zone for production grade egress availability. Beyond that, the platform becomes the substrate for model serving workloads, which is a different story built on the same bones.


## Licence

Released under the MIT Licence. See `LICENSE` for the full text.
