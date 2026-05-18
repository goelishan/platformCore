# PlatformCore

> A production-shaped reference platform on AWS — a containerised FastAPI service backed by managed PostgreSQL, orchestrated on Amazon EKS, with every layer expressed as code and every architectural choice defensible from first principles.

[![Terraform](https://img.shields.io/badge/Terraform-1.9%2B-844FBA?logo=terraform&logoColor=white)](https://www.terraform.io/)
[![AWS](https://img.shields.io/badge/AWS-EKS%201.33-FF9900?logo=amazon-aws&logoColor=white)](https://aws.amazon.com/eks/)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-1.33-326CE5?logo=kubernetes&logoColor=white)](https://kubernetes.io/)
[![Helm](https://img.shields.io/badge/Helm-4-0F1689?logo=helm&logoColor=white)](https://helm.sh/)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](#licence)

---

## Table of contents

- [Synopsis](#synopsis)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Design principles](#design-principles)
- [Technology stack](#technology-stack)
- [The platform, layer by layer](#the-platform-layer-by-layer)
- [Architectural decisions](#architectural-decisions)
- [Security posture](#security-posture)
- [Out of scope, with reasoning](#out-of-scope-with-reasoning)
- [Deployment](#deployment)
- [Roadmap](#roadmap)
- [Licence](#licence)

---

## Synopsis

PlatformCore is a self-contained AWS platform organised around a single guiding principle: every resource must exist for a reason that the operator can articulate, defend and, if pressed, rebuild from memory. A FastAPI workload talks to Amazon RDS for PostgreSQL through short-lived IAM authentication tokens; the workload itself runs on Amazon EKS, fronted by an Application Load Balancer that registers Pod IPs directly; the surrounding infrastructure — network, identity, edge, storage, supply chain — is provisioned by Terraform and applied to the cluster through Helm and Kubernetes manifests.

The platform is intentionally not a tutorial topology. The awkward edges have been kept and named, the trade-offs are explained alongside the resources they shape, and the cost story attaches to every choice that has one. The result is a system where the answer to *"why does this exist?"* is documented for every component, and the answer to *"what would break this?"* is documented next to the same component.

A request enters through an internet-facing Application Load Balancer that lives in tag-discovered public subnets. The load balancer registers Pod IPs directly via the AWS VPC CNI, bypassing kube-proxy and any iptables hop. Workload Pods sit in private subnets and reach AWS service APIs through Interface VPC Endpoints — keeping that traffic off the NAT Gateway's per-gigabyte egress meter — while traffic destined for public container registries flows through the NAT itself. Per-Pod AWS identity is delegated through IAM Roles for Service Accounts, so static AWS credentials never enter the cluster. Persistent storage is provisioned on a custom StorageClass with a `Retain` reclaim policy and `WaitForFirstConsumer` binding, decoupling data lifecycle from workload lifecycle and matching the AZ-locality of EBS.

---

## Architecture

The data path is short and explicit. The control surfaces — IAM, OIDC federation, the EKS control plane — sit beside the data path rather than in it, and intervene only where a trust decision is required.

```
                              ┌───────────────────────────────────┐
                              │           Public internet         │
                              └────────────────┬──────────────────┘
                                               │
                                               ▼
                              ┌───────────────────────────────────┐
                              │       Amazon Route 53 (DNS)       │
                              └────────────────┬──────────────────┘
                                               │
                                               ▼
                              ┌───────────────────────────────────┐
                              │   AWS Application Load Balancer   │
                              │   • ACM-managed TLS               │
                              │   • Tag-discovered public subnets │
                              │   • target-type = ip              │
                              └────────────────┬──────────────────┘
                                               │  Pod-direct.
                                               │  No kube-proxy hop.
                                               ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │                          Amazon VPC  (10.0.0.0/16)                       │
   │                                                                          │
   │  Public subnets (2 Availability Zones)                                   │
   │  ┌──────────────────────────┐      ┌──────────────────────────────┐      │
   │  │ ALB ENIs                 │      │ NAT Gateway                  │      │
   │  │ Internet Gateway route   │      │ Egress to public registries  │      │
   │  └──────────────────────────┘      │ (Docker Hub, public.ecr.aws, │      │
   │                                    │ Quay, third-party APIs)      │      │
   │                                    └──────────────────────────────┘      │
   │                                                                          │
   │  Private subnets (2 Availability Zones)                                  │
   │  ┌────────────────────────────────────────────────────────────────────┐  │
   │  │                                                                    │  │
   │  │  EKS managed node group                                            │  │
   │  │   ├─ Workload Pods (VPC-native IPs via the AWS VPC CNI)            │  │
   │  │   ├─ AWS Load Balancer Controller   (IRSA-bound)                   │  │
   │  │   ├─ AWS EBS CSI Driver             (IRSA-bound, managed add-on)   │  │
   │  │   ├─ FastAPI Pods                   (IRSA → RDS IAM auth)          │  │
   │  │   └─ PostgreSQL StatefulSet on gp3-retain StorageClass             │  │
   │  │                                                                    │  │
   │  │  Amazon RDS for PostgreSQL  (encrypted, IAM-authenticated)         │  │
   │  │                                                                    │  │
   │  │  Interface VPC Endpoints with private DNS:                         │  │
   │  │    ssm, ssmmessages, ec2messages, ec2, ecr.api, ecr.dkr,           │  │
   │  │    logs, secretsmanager, sts                                       │  │
   │  │                                                                    │  │
   │  │  S3 Gateway Endpoint (prefix-list-driven, attached to private RT)  │  │
   │  └────────────────────────────────────────────────────────────────────┘  │
   │                                                                          │
   └──────────────────────────────────────────────────────────────────────────┘

   ┌──────────────────────────────────────────────────────────────────────────┐
   │  EKS control plane — AWS-managed, multi-AZ, hosted outside the VPC.      │
   │  The operational surface is reachability, not state: a misconfigured     │
   │  Security Group, route table or endpoint mode is the failure mode.       │
   └──────────────────────────────────────────────────────────────────────────┘
```

**Properties of the data path worth highlighting.** The Application Load Balancer targets Pod IPs directly through `target-type = ip`; the controller watches the Service's endpoints and synchronises live Pod IPs into the target group, which removes the kube-proxy hop, simplifies troubleshooting, and yields AZ-aware traffic distribution. Pods receive real VPC IP addresses via the AWS VPC CNI, which means AWS Security Groups apply natively to Pod-to-AWS-service traffic, exactly as they would to an EC2 instance. AWS-API traffic remains on private endpoints, so only third-party registry pulls and external API calls traverse the NAT Gateway, keeping the per-gigabyte egress charges proportional to genuinely public-bound traffic. The EKS control plane sits in an AWS-managed VPC the operator never touches, and the blast radius of any in-cluster mistake is bounded by the data plane.

---

## Repository layout

The repository is partitioned along the same boundaries as the running system. Each Terraform module owns one architectural concern; each Kubernetes directory owns one workload tier. Cross-module wiring lives at the composition root, never inside a module that would otherwise own only half of a relationship.

```
platformCore/
│
├── README.md                            ← you are here
├── Makefile                             Lifecycle helpers (up, down, status, curl, logs)
├── docker-compose.yml                   Local stack — production-shaped base
├── docker-compose.override.yml          Local stack — development overlay
├── .env.example                         Template; runtime .env is git-ignored
├── kind-config.yaml                     Local kind cluster for offline iteration
│
├── .github/
│   └── workflows/
│       └── ci.yml                       Compose validation (base + merged) → image build
│
├── app/                                 FastAPI application
│   ├── Dockerfile                       python:3.12-slim → uvicorn
│   ├── main.py                          Lifespan-managed startup; RDS IAM auth via boto3
│   └── requirements.txt
│
├── nginx/
│   └── default.conf                     Dual-stack listener; decoupled health route
│
├── db/
│   └── init.sql                         Idempotent schema bootstrap
│
├── terraform/
│   ├── main.tf                          Root composition; child modules in DAG order
│   ├── provider.tf                      AWS provider, pinned to ~> 5.0
│   ├── backend.tf                       S3 remote state + DynamoDB locking
│   ├── variables.tf · outputs.tf
│   ├── security_groups.tf               Cross-module SG rules (DAG-safe topology)
│   │
│   └── modules/
│       ├── network/                     VPC, subnets, IGW, NAT, route tables,
│       │                                Interface and Gateway VPC Endpoints
│       │
│       ├── data/                        RDS PostgreSQL, subnet/parameter groups,
│       │                                AWS Secrets Manager, encryption at rest
│       │
│       ├── compute/                     Legacy EC2 path, IAM, SSM-only access,
│       │                                IMDSv2-enforced user-data
│       │
│       ├── edge/                        ALB, target groups, listeners, ACM,
│       │                                conditional HTTP→HTTPS redirect, Route 53
│       │
│       └── eks/                         Cluster + managed node group, OIDC provider,
│                                        Access Entries, three IRSA bundles
│                                        (ALB Controller, EBS CSI, FastAPI)
│
└── k8s/
    ├── namespace.yaml
    ├── storageclass-gp3-retain.yaml     gp3 · Retain · WaitForFirstConsumer · encrypted
    │
    ├── fastapi/                         Deployment, ClusterIP Service, ServiceAccount
    │                                    (IRSA-annotated), ConfigMap
    │
    ├── nginx/                           Deployment, ClusterIP Service, ALB Ingress,
    │                                    ConfigMap, Secret, HorizontalPodAutoscaler
    │
    └── postgres/                        StatefulSet (volumeClaimTemplates),
                                         headless Service, ConfigMap, Secret
```

---

## Design principles

Five principles shaped the platform. Each one is visible across multiple layers of the codebase, and each one resolves a recurring class of trade-off the same way every time it appears.

**Identity over secrets.** Every workload that needs to call an AWS API does so via a per-Pod IAM identity, brokered through OIDC federation and exchanged for short-lived credentials via `sts:AssumeRoleWithWebIdentity`. Static credentials are not provisioned into the cluster, not for the controllers, not for the application.

**Defence in depth at the network layer.** Segmentation is enforced at four independent levels — subnet partitioning by route table, Security Group source-group references between tiers, private VPC Endpoints for AWS-internal traffic, and route-table-driven egress through NAT for everything else. A failure at any one layer is contained by the next.

**Deliberate boundaries between managed and self-managed responsibilities.** Where AWS offers a managed equivalent (RDS, the EKS control plane, the EBS CSI add-on, ACM-managed certificates), the platform consumes it. Where it does not, the platform owns the configuration explicitly and pins every version in the dependency chain.

**Operational reversibility.** Teardown is a first-class operation, not an afterthought. Every billable resource can be destroyed in a known dependency order, leaving the free resources intact for a rapid rebuild; alternatively, the entire graph can be removed in a single command. Disposability of environment is preferred over preservation of state — the same shape adopted by mature continuous-deployment pipelines.

**Cost as a first-class concern.** Every architectural choice that has a cost story carries it explicitly. Where VPC Endpoints save egress charges, they are kept; where their per-hour cost outweighs the saving, they are scoped accordingly. The NAT Gateway is justified not against zero but against the alternatives that were considered and rejected.

---

## Technology stack

| Concern                       | Implementation                                                                                                            |
| :---------------------------- | :------------------------------------------------------------------------------------------------------------------------ |
| Cloud provider                | Amazon Web Services, region `us-east-1`                                                                                   |
| Infrastructure as code        | Terraform 1.9+, `hashicorp/aws ~> 5.0`, S3 remote state with DynamoDB locking                                             |
| Container runtime, local      | Docker Engine, Docker Compose v2                                                                                          |
| Container runtime, managed    | Amazon EKS 1.33 with managed node groups (`t3.small`, scaling 1–3)                                                        |
| Image registry                | Amazon ECR (private), version-pinned third-party charts and images                                                        |
| Networking                    | Amazon VPC across two Availability Zones, NAT Gateway, ten VPC Endpoints, VPC CNI for Pod networking                      |
| Edge                          | AWS Application Load Balancer, ACM-managed certificates, conditional HTTP→HTTPS redirect, Route 53                        |
| Compute, baseline path        | Amazon EC2 `t3.micro`, IMDSv2 enforced, SSM Session Manager (no SSH key, no port 22 ingress)                              |
| Identity and access           | IAM roles, EKS Access Entries, OIDC federation, IRSA for the AWS Load Balancer Controller, the EBS CSI Driver and FastAPI |
| Storage                       | Amazon EBS gp3 (encrypted), custom `gp3-retain` StorageClass with `Retain` reclaim policy and `WaitForFirstConsumer`      |
| Data layer                    | Amazon RDS for PostgreSQL 17, `db.t3.micro`, encryption at rest, RDS IAM Authentication for the application path         |
| Application                   | FastAPI on Python 3.12, uvicorn, nginx 1.27-alpine reverse proxy                                                          |
| Orchestration                 | Kubernetes 1.33, Helm 4                                                                                                   |
| Cluster add-ons               | AWS Load Balancer Controller (chart 1.8.4, controller v2.8.3), AWS EBS CSI Driver via `aws_eks_addon`                     |
| Secrets management            | AWS Secrets Manager with VPC Endpoint access; Kubernetes Secret and ConfigMap separation                                  |
| Continuous integration        | GitHub Actions — Compose validated in base and merged modes; image build gated on validation                              |

---

## The platform, layer by layer

The narrative below walks the stack in dependency order — the order in which Terraform applies the modules, and the order in which a request encounters them at runtime.

### Network foundation

The network module establishes a Virtual Private Cloud spanning two Availability Zones, with subnets partitioned by route table rather than by name. A subnet is *public* because its route table carries a default route to the Internet Gateway; remove that route and the subnet is effectively private. The platform never relies on a naming convention to communicate behaviour — the route table is the source of truth.

Subnets are tagged for downstream discovery. Public subnets carry `kubernetes.io/role/elb = 1`; private subnets carry `kubernetes.io/role/internal-elb = 1`. Tag-driven discovery is the mechanism by which the AWS Load Balancer Controller decides where to materialise internet-facing and internal load balancers — a deliberate choice over name-based conventions, because a single VPC can host multiple EKS clusters without their controllers consuming each other's subnets.

The egress topology combines two distinct mechanisms in deliberate concert. Interface VPC Endpoints carry traffic to AWS service APIs — SSM, EC2, ECR API and ECR Docker registry, CloudWatch Logs, Secrets Manager, STS — through private routes that never leave the AWS network. A single NAT Gateway carries everything else: pulls from public container registries, calls to third-party APIs, and any destination for which AWS does not publish an endpoint. Once the NAT exists, the endpoints do not become redundant; they continue to keep AWS-API traffic off the NAT's per-gigabyte egress meter, which is the dominant cost component at any meaningful traffic volume.

The S3 Gateway Endpoint is treated as a separate concern because it operates on a different mechanism — a route-table prefix list rather than an ENI. It carries no per-hour cost, and is structurally required for ECR image-layer fetches, which are themselves backed by S3 buckets behind the scenes.

### Identity and access

The identity layer rests on the AWS IAM trust model, with three concerns separated cleanly.

*Service principals* — the principle by which an AWS service is authorised to assume a role — appear in trust policies authored as `aws_iam_policy_document` data sources rather than inline `jsonencode` strings. The trust policies remain type-safe and reviewable, and the same data-source pattern is reused across every role in the codebase.

*Cluster access* is delegated to Access Entries rather than the legacy `aws-auth` ConfigMap. Access Entries are an AWS-managed API surface in which identity (`aws_eks_access_entry`) and permissions (`aws_eks_access_policy_association`) are separate resources, mirroring the IAM-user-versus-policy-attachment shape. The decisive operational property is lockout resistance: a corrupted `aws-auth` ConfigMap can only be repaired through `kubectl`, which the same corruption may have disabled. Access Entries are repaired through the AWS API itself, which is always reachable as long as AWS account access is intact.

*Per-Pod AWS identity* is delegated through IRSA. Every workload that calls AWS APIs does so via an IAM role whose trust policy is locked to a specific Kubernetes ServiceAccount via the `sub` claim, with the `aud` claim further restricted to `sts.amazonaws.com` to prevent token replay across audiences. The cluster's OIDC issuer is registered as a federated identity provider on the AWS side; the Pod's projected ServiceAccount token is exchanged for short-lived AWS credentials through `sts:AssumeRoleWithWebIdentity`. The pattern is applied identically three times — to the AWS Load Balancer Controller, to the EBS CSI Driver, and to the FastAPI application — and each instance comprises the same three resources: an IAM role, a policy document, and an attachment.

### Compute and orchestration

The platform supports two compute paths, chosen by workload shape rather than preference.

The *baseline path* runs a single EC2 instance behind the ALB. It exists to establish the network and security topology in isolation, before Kubernetes is layered on top. The instance has no SSH key and no port 22 ingress on its Security Group; operator access is exclusively through SSM Session Manager, which itself transits the SSM, SSM-Messages and EC2-Messages VPC Endpoints. IMDSv2 is enforced with `http_tokens = "required"`, which defeats the SSRF-to-credential-theft attack against `169.254.169.254`.

The *managed path* runs an EKS cluster with a managed node group, pinned to Kubernetes 1.33. The cluster authentication mode is `API` (Access Entries only), and `bootstrap_cluster_creator_admin_permissions` is explicitly set to `false`: the console administrator is declared as a distinct, auditable Terraform resource rather than inheriting silent admin rights from whichever identity ran `CreateCluster`. The OIDC provider, registered as `aws_iam_openid_connect_provider` against the cluster's issuer, is the trust anchor for every IRSA role in the platform.

Workload orchestration uses standard Kubernetes primitives, chosen by the shape of the workload. Stateless tiers are Deployments; stateful tiers are StatefulSets with `volumeClaimTemplates` and a headless Service for per-Pod DNS. Resource requests are set on every container — this is non-negotiable for Horizontal Pod Autoscaling, whose formula is `currentUsage / requestedAmount` and which silently refuses to operate without a denominator. Liveness and readiness probes are scoped to each container's own primary process rather than its upstream dependencies, which prevents cascading false positives in which a database outage restarts the application tier.

### Data and storage

Persistent state is split deliberately across two systems.

*Amazon RDS for PostgreSQL* is the runtime data store. Running PostgreSQL on managed infrastructure rather than in-cluster externalises backups, point-in-time recovery, Multi-AZ failover, and minor-version patching to AWS. The application authenticates to RDS via IAM Authentication: the FastAPI Pod's IRSA role carries an `rds-db:connect` permission scoped to a specific `dbuser` ARN, and the application calls `boto3.client('rds').generate_db_auth_token` per connection. The token is short-lived (fifteen minutes) and is supplied to `psycopg` as the connection password. Static database passwords are eliminated from the application path entirely.

*The in-cluster PostgreSQL StatefulSet* sits alongside RDS as the Kubernetes-primitive substrate for any future stateful workload — an operator-managed database, a queue, a cache — and as the proof of correctness for the cluster's storage layer. It uses a custom StorageClass — `gp3-retain` — provisioned by the AWS EBS CSI Driver. Three properties of this StorageClass are load-bearing. `reclaimPolicy: Retain` keeps the underlying EBS volume after a PVC is deleted; the volume enters `Released` state for manual rescue, rather than vanishing along with the workload. `volumeBindingMode: WaitForFirstConsumer` defers EBS provisioning until a Pod is scheduled, so the volume lands in the same Availability Zone as the chosen node — essential on multi-AZ clusters, where EBS is AZ-local and cross-AZ attachment is structurally impossible. Encryption is enabled at the StorageClass level.

The headless Service in front of the StatefulSet — `clusterIP: None` — is the prerequisite for per-Pod DNS. With `kube-proxy` programming zero iptables rules for a headless Service, DNS resolution returns one A record per Ready Pod, plus stable per-Pod names of the form `postgres-0.postgres-service.platformcore.svc.cluster.local`. Any peer-to-peer state-sync protocol — write-ahead-log streaming, leader election, gossip — depends on stable peer addresses; the platform's Kubernetes shape supports them, even where the application-level wiring is left to a future PostgreSQL operator.

### Edge and ingress

The edge layer terminates TLS at the Application Load Balancer through an ACM-managed certificate, with an HTTP-to-HTTPS redirect listener that activates conditionally when a domain has been configured. Route 53 holds the public DNS records. Health checks against the workload use a dedicated path (`/health`) rather than the application's root, decoupling target-group health from transient routing changes in the application itself.

Ingress for the Kubernetes workload is provided by the AWS Load Balancer Controller, installed via Helm and authorised through IRSA. Ingress objects in the cluster carry annotations declaring the desired ALB shape — internet-facing or internal, listen ports, target type, optional SSL settings — and the controller materialises a real AWS ALB in response. The decisive choice here is `target-type: ip`, which configures the ALB to target Pod IPs directly. The alternative, `target-type: instance`, routes through a NodePort on each node and then through kube-proxy; this adds a kernel-level iptables hop and requires opening the node Security Group across the NodePort range. The IP target type is cleaner, faster, AZ-aware in its traffic distribution, and consistent with how the rest of the platform treats Pods — as first-class VPC citizens.

A second ingress path — the in-cluster `ingress-nginx` controller fronted by a Service of type `ClusterIP` — is retained for local development under kind. It exists as a parity check: the same Ingress object that resolves via `ingress-nginx` locally resolves via the AWS Load Balancer Controller on EKS, with no application-side awareness of which controller is in play.

### Application and supply chain

The FastAPI application is intentionally minimal. It exposes four routes — root, `/health`, `/ready`, `/version` — and follows the canonical liveness-vs-readiness split: `/health` is database-free by design, so a database outage will not restart the Pod; `/ready` exercises a round-trip to RDS and removes the Pod from rotation when the database is unreachable, without forcing a restart. The application opens a fresh PostgreSQL connection per request, regenerating the IAM authentication token on every connection — safe with per-request connections, and a constraint the codebase carries explicitly should connection pooling be introduced.

The application image is built locally and pushed to a private Amazon ECR repository. The continuous-integration pipeline validates the image build on every change to `main`. Third-party images consumed by the platform — nginx, postgres, the AWS Load Balancer Controller, the EBS CSI Driver — are version-pinned at both the chart level and the image level, with the corresponding IAM policy documents pinned to matching Git tags on the upstream repositories. The pinning is deliberate. A chart version's bundled image version is not part of any guaranteed contract, so every version triple (chart, image, IAM policy) is verified before promotion; otherwise an upstream chart bump can silently re-grant permissions or alter behaviour.

The trade-off accepted at this stage is that third-party images are pulled at runtime from their upstream registries through the NAT Gateway, rather than mirrored into the platform's private ECR. A mirroring pipeline would interpose a supply-chain scanning checkpoint at the registry boundary, and would eliminate runtime dependence on upstream availability; it is sized as a separate piece of future work, scoped to the continuous-deployment layer rather than the platform foundation.

### Operational tooling

A `Makefile` at the repository root encapsulates the most common lifecycle operations. `make up` runs `terraform apply` non-interactively. `make down` performs a targeted teardown of billable resources in a dependency-correct order, leaving free resources (VPC, IAM roles, subnets, route tables) intact for a rapid rebuild. `make down-all` performs a full destroy. The targeted teardown becomes progressively brittle as the resource graph grows, so the recommended default for the EKS-bearing stack is `make down-all`, with `make up` rebuilding the environment from scratch when it is next needed — the same shape mature continuous-deployment pipelines adopt.

GitHub Actions runs the continuous-integration gates. Compose configuration is validated twice on every change: once against the base file alone, and once against the merged base-plus-override topology. The two-mode validation catches a class of bugs where an override-only typo passes single-mode validation but breaks developer environments, or where a base typo passes merged validation but breaks production. The image build job runs only after validation passes, with explicit `needs:` dependencies, so cheap failures surface in seconds without burning CI minutes on a doomed image build.

---

## Architectural decisions

The following choices meaningfully shaped the platform. Each is paired with the alternative considered and the reason the alternative was rejected.

### Interface Endpoints and a NAT Gateway, in deliberate combination

The original network topology ran without a NAT Gateway. Every egress destination at the time — SSM, the application's own private ECR, CloudWatch Logs, S3 layer storage, Secrets Manager — had a corresponding VPC Endpoint. The result was a sealed network with no public egress path at all, saving the NAT's fixed monthly cost and tightening the security perimeter.

That topology held until the workload introduced a new class of destination: third-party container registries. The AWS Load Balancer Controller's image is published to `public.ecr.aws`, which despite the name is a distinct service from private ECR and has no VPC Endpoint equivalent. Workload images on Docker Hub and Quay sit in the same category. The structural insight is that VPC Endpoints match by service, not by account: the EBS CSI Driver's image is hosted in AWS's private regional ECR mirror, which the existing `ecr.api` and `ecr.dkr` endpoints cover transparently, but the Load Balancer Controller's image is structurally unreachable from a NAT-less private subnet.

Three responses were weighed. Mirroring every external image into a private ECR repository is the production-mature answer, because it interposes a supply-chain scanning checkpoint at the registry boundary; this was rejected for the current scope on the basis that the mirror's natural home is the continuous-deployment pipeline, not a side detour from the platform foundation. Using AWS-published mirrors of specific community images was rejected as a per-image fix to a class-of-image problem; nginx, postgres and most observability-stack components are not in those mirrors. Adding a NAT Gateway was the third option: three resources (Elastic IP, gateway, default route on the private route table), predictable cost, and a single change that unblocks every current and future public-registry pull.

The NAT was the chosen path. The point worth dwelling on is that NAT did not replace the endpoints — both remain in the topology, working in deliberate combination. Endpoints continue to keep AWS-API traffic off the NAT's per-gigabyte egress meter, which is where the operational cost lives at scale. The combined topology is more expensive than endpoints-only by the NAT's fixed monthly charge, and meaningfully cheaper than NAT-only at any non-trivial AWS-API traffic level. In a stricter compliance environment the choice would flip — the registry-boundary scan provided by a mirroring pipeline becomes load-bearing, and the NAT goes away — but the choice is context-dependent, not universal.

### Explicit configuration over relaxed metadata-service constraints

The platform enforces IMDSv2 across both EC2 and EKS managed nodes (`http_tokens = "required"`). EKS managed nodes additionally default to a metadata hop limit of one, which is sufficient for Pods on the host network but blocks Pods on the cluster's Pod network from reaching the instance metadata service at all — the IMDSv2 session-token PUT request expires before traversing the extra network namespace.

The AWS Load Balancer Controller, by default, discovers its VPC ID and region by querying the instance metadata service. With the platform's hop-limit configuration in place, those queries return 401 and the controller crashes on startup. Two responses were available. The hop limit could be increased to two, which would allow every Pod in the cluster to reach metadata — including Pods that should not need that access; this was rejected as a deliberate weakening of the security posture. Alternatively, the controller could be told its VPC ID and region through explicit Helm values, bypassing metadata discovery entirely; this was chosen.

The wider pattern is general. Where a controller's default discovery mechanism is blocked by an intentional security constraint, the correct response is usually to supply the discovery values explicitly, rather than to relax the constraint. The same shape recurs in the choice to prefer pinned image references over the registries' `latest` tag, and in the choice to declare the console administrator IAM principal explicitly, rather than inherit silent admin rights from the cluster's creator identity.

### StatefulSet over Deployment for stateful workloads

The PostgreSQL tier inside the cluster is a StatefulSet, not a Deployment. The distinction is rarely a matter of preference; it is a matter of which set of guarantees the workload requires. Deployments provide none of the three properties that stateful workloads typically depend on: stable identity (Pods have random hash suffixes), per-replica storage (all replicas share the same PersistentVolumeClaim), and ordered operations (replicas come and go in parallel).

The StatefulSet provides all three. Pod names are stable (`postgres-0`, `postgres-1`); each replica receives its own auto-provisioned PVC named deterministically (`data-postgres-0`); start-up and termination are strictly ordered, with replica *N* waiting for replica *N − 1* to reach Ready before launching, and replicas terminating in reverse ordinal order. The headless Service in front of the StatefulSet provides the per-Pod DNS that any peer-to-peer state-sync protocol requires.

The Kubernetes shape now supports streaming replication, leader election, and any other primary-and-replica topology. The application-level wiring — PostgreSQL's write-ahead-log streaming, the operator software that automates failover — is a separate concern, deliberately scoped to a future PostgreSQL operator deployment.

### Access Entries over the legacy `aws-auth` ConfigMap

The cluster's mapping between AWS IAM identities and Kubernetes RBAC subjects uses Access Entries — the AWS-managed API surface introduced as the modern alternative to the `aws-auth` ConfigMap. The two mechanisms are functionally interchangeable in steady state; they differ sharply in their failure modes.

The `aws-auth` ConfigMap is an in-cluster Kubernetes resource. A typo or misconfiguration that revokes access for all administrators leaves the cluster unrecoverable, because `kubectl` cannot edit the ConfigMap if `kubectl` cannot authenticate. The historical workaround — opening a support case with AWS — is unsuitable for any meaningful operational maturity.

Access Entries are AWS-API resources. Recovery operates through the same API channel the operator uses to provision the cluster in the first place, which is always reachable as long as AWS account access is intact. The same pattern the platform uses for declarative provisioning (`aws_eks_access_entry` and `aws_eks_access_policy_association`) is the recovery path; there is no operational divergence between bootstrap and break-glass.

### Modular Terraform with explicit cross-module wiring

The Terraform codebase is partitioned into five modules — `network`, `data`, `compute`, `edge`, `eks` — with module outputs forming versioned contracts between layers. The composition root (`main.tf`) instantiates each module in dependency order and wires module outputs into the inputs of downstream modules.

Cross-module Security Group rules are the one exception that deserves a note. Two modules each owning a Security Group and referencing the other's Security Group as a rule source forms a circular dependency that Terraform's module-level DAG cannot sort. The pattern used here is to keep each Security Group inside its owning module, but to declare the cross-module rules as standalone `aws_vpc_security_group_ingress_rule` and `aws_vpc_security_group_egress_rule` resources at the composition root. The cycle breaks at the rule layer, and the conceptual cleanliness of the modules themselves is preserved.

### Pinned versions across the dependency chain

Every external dependency is pinned: the Terraform provider (`hashicorp/aws ~> 5.0`), Helm chart versions (`--version 1.8.4`), container image tags (`nginx:1.27-alpine`, `postgres:17`), GitHub Actions versions (`actions/checkout@v5`), and the IAM policy documents fetched from upstream project repositories (pinned to release tags, not `main`). The pinning is enforced for two reasons.

The first is reproducibility: a fresh `terraform apply` against this repository produces the same infrastructure today as it would in six months. The second is supply-chain hygiene: upstream changes that re-grant permissions, swap dependencies, or alter behaviour arrive on the operator's schedule rather than upstream's. The cost of pinning is the obligation to actively bump versions; this is the correct cost to bear, because the failure mode of not pinning is silent regression at a time the operator did not choose.

---

## Security posture

| Concern                                          | Mitigation                                                                                                                                                                                                                                                                              |
| :----------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Credential exfiltration via instance metadata    | IMDSv2 enforced on every compute node, with the metadata hop limit set deliberately rather than relaxed for convenience.                                                                                                                                                                |
| Per-Pod least privilege on AWS APIs              | IRSA. Every Pod that calls an AWS API does so through a ServiceAccount-bound IAM role whose trust policy is locked to that ServiceAccount via the `sub` claim and to the `sts.amazonaws.com` audience via the `aud` claim.                                                              |
| Database authentication on the application path  | RDS IAM Authentication. The FastAPI Pod's IRSA role carries `rds-db:connect` on a specific `dbuser`; the application calls `generate_db_auth_token` per connection. No static database password reaches the workload Pods.                                                              |
| Network segmentation in transit                  | Four independent layers — subnet partitioning by route table, Security Group source-group references between tiers, VPC Endpoints for AWS-internal traffic, and controlled egress through the NAT Gateway for everything else.                                                          |
| Compute access without SSH                       | No SSH keys are provisioned. No port 22 ingress exists on any Security Group. Operator access traverses SSM Session Manager via private endpoints.                                                                                                                                      |
| Public exposure of workloads                     | Only the Application Load Balancer's ENIs sit in public subnets. Workload nodes, the data layer and the EKS control plane all sit on private addressing.                                                                                                                                |
| Secrets handling                                 | Application secrets are stored in AWS Secrets Manager with VPC Endpoint access. Kubernetes Secrets are namespaced and consumed through `secretKeyRef`, never inline. The static administrative database password is contained to bootstrap-time use; the runtime path uses IAM tokens.  |
| Image supply chain                               | All image references are pinned at the version level. The IAM policy documents that grant cluster controllers their AWS permissions are pinned to the same upstream release tag as the controller image itself, preventing version-skew vulnerabilities.                               |
| Encryption at rest                               | gp3 EBS volumes are encrypted at the StorageClass level. RDS encryption is enabled at instance creation.                                                                                                                                                                                |
| Encryption in transit                            | TLS termination at the ALB uses an ACM-managed certificate. HTTP-to-HTTPS redirect is enforced when a domain is configured.                                                                                                                                                             |
| Cluster lockout resistance                       | The mapping between AWS IAM and Kubernetes RBAC uses Access Entries, recoverable through the AWS API channel. The legacy in-cluster `aws-auth` ConfigMap is not deployed.                                                                                                               |

---

## Out of scope, with reasoning

A platform that does not name its own boundaries is overclaiming. The following are deliberately not part of the current scope, and each is paired with the route by which it would land in a production deployment.

| Capability                                           | Production route                                                                                                                                                                                                                                                                                |
| :--------------------------------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Multi-AZ high availability for the in-cluster data tier | A managed PostgreSQL operator such as CloudNativePG or Zalando, installed via Helm, providing primary-replica replication, automated failover and connection routing. The cluster's StatefulSet topology already supports the underlying Kubernetes primitives.                                 |
| Per-AZ NAT redundancy                                | A NAT Gateway per Availability Zone, with per-AZ route tables routing egress through the local gateway. The current single-NAT topology accepts the loss of egress for the surviving zone if the gateway's AZ fails.                                                                            |
| Centralised observability                            | Prometheus and Grafana via `kube-prometheus-stack`, Loki for log aggregation, OpenTelemetry collectors for distributed tracing. The platform emits structured logs and exposes metrics ready for collection; the collection plane is the natural extension.                                     |
| GitOps reconciliation                                | Argo CD or Flux reconciling cluster state from this repository. The current cadence applies manifests through `kubectl apply` and Helm releases — suitable for development, not for production change management.                                                                               |
| Image supply-chain scanning                          | A CI step that pulls each upstream image, scans it through Trivy or an equivalent, and pushes the scanned artefact into private ECR. Every Helm release would override `image.repository` to point at the private mirror. The NAT-based runtime path is forward-compatible with this design.    |
| Secret rotation                                      | External Secrets Operator with AWS Secrets Manager as the backend, with automatic rotation hooks for credentials that support them. The Kubernetes Secret plus ConfigMap pattern is the manual baseline; the operator layer is the next step.                                                   |
| Backup and disaster recovery testing                 | RDS automated backups are enabled. A full disaster-recovery posture adds tested point-in-time recovery procedures and cross-region replication for compliance scenarios.                                                                                                                        |
| In-cluster network policies                          | Cilium or Calico providing Kubernetes NetworkPolicy enforcement, or SecurityGroupsForPods providing AWS-Security-Group enforcement at the Pod boundary. The current cluster relies on Security Groups and subnet partitioning for segmentation; intra-cluster Pod-to-Pod restrictions follow.   |
| Service mesh                                         | Istio or Linkerd, justified by requirements such as mutual TLS between services, traffic shaping for canary deployments, or cross-cluster service discovery. None of those requirements are present in the current scope.                                                                       |
| Workload identity beyond IRSA                        | EKS Pod Identity, the post-2023 successor to IRSA, for new workloads. IRSA is the current implementation, retained for the richer trust-policy model and the wider tooling support.                                                                                                             |

---

## Deployment

### Prerequisites

- An AWS account with administrative access for the bootstrap principal.
- Terraform 1.9 or newer.
- Helm 4 or newer.
- `kubectl` matching the cluster's Kubernetes version (1.33).
- AWS CLI v2 configured with credentials in `~/.aws/credentials`.

### Bootstrap, once per AWS account

The Terraform state backend is provisioned manually to avoid the chicken-and-egg problem of bootstrapping state-managing infrastructure.

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

### Local stack

```bash
cp .env.example .env
docker compose up --build
curl http://localhost/
```

### AWS infrastructure

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

The apply provisions the VPC, the EKS cluster, the data and edge layers, and the three IRSA roles. Outputs surface the cluster endpoint and the controller IRSA role ARNs for downstream consumption.

### Kubernetes workload

```bash
aws eks update-kubeconfig --name platformcore --region us-east-1
kubectl get nodes

kubectl apply -f k8s/storageclass-gp3-retain.yaml
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/postgres/
kubectl apply -f k8s/fastapi/
kubectl apply -f k8s/nginx/configmap.yaml
kubectl apply -f k8s/nginx/secret.yaml
kubectl apply -f k8s/nginx/deployment.yaml
kubectl apply -f k8s/nginx/service.yaml
kubectl apply -f k8s/nginx/hpa.yaml
```

### Load Balancer Controller

The chart is pinned to a version whose bundled image matches the IAM policy fetched by Terraform. The VPC identifier and region are passed explicitly through Helm values, bypassing the controller's default metadata-based discovery path.

```bash
helm repo add eks https://aws.github.io/eks-charts
helm repo update

helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --namespace kube-system \
  --version 1.8.4 \
  --set clusterName=platformcore \
  --set region=us-east-1 \
  --set vpcId=$(terraform -chdir=terraform output -raw vpc_id) \
  --set serviceAccount.create=true \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=$(terraform -chdir=terraform output -raw alb_controller_role_arn)"
```

### Ingress

```bash
kubectl apply -f k8s/nginx/ingress.yaml

ALB_DNS=$(kubectl get ingress nginx -n platformcore \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

curl -v http://$ALB_DNS/
```

### Teardown

```bash
make down-all
```

---

## Roadmap

The following extensions are scoped as natural next steps. Each maintains continuity with the architectural choices established in the current scope, rather than displacing them.

- **Custom Helm chart for the application stack.** All workload manifests collapsed into a parameterised chart, with values driving image tags, replica counts and secret references. Versioned chart releases land in an OCI registry for downstream consumption.
- **Continuous deployment to EKS.** Image build, tag and push to ECR on every change to `main`; `helm upgrade --install` against the cluster as the deployment step. The pipeline carries the supply-chain scan that justifies skipping the runtime registry mirror today.
- **Observability stack.** Prometheus and Grafana via `kube-prometheus-stack`, with pre-configured ServiceMonitors for the application, RDS and the cluster's control-plane metrics. Loki for log aggregation, with structured logging in the application.
- **GitOps reconciliation.** Argo CD reconciling cluster state from this repository, with `ApplicationSet` patterns for multi-environment promotion. The platform's manifest layout is already structured for this transition.
- **External Secrets Operator.** AWS Secrets Manager wired as the secret backend, with rotation hooks for credentials that support them.

---

## Licence

Released under the MIT Licence. See `LICENSE` for the full text.
