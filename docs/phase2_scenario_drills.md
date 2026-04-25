# Phase 2 — Scenario Drills (Terraform + VPC)

**Purpose:** Convert the Terraform / VPC concepts from Days 6–7 into interview-grade reflexes. Not "do you know what S3 is" — "*why* this service, in this slot, at this scale, and what breaks first."

---

## How to use this doc

For each scenario:

1. **Read the setup once.** Don't skim past the numbers — they're load-bearing.
2. **Answer the five prompts out loud, in order.** Don't skip "Doesn't fit" — that's where most candidates lose the room.
3. **Only then expand the scaffold.** The scaffold is *trigger words and traps*, not a model answer. You build the answer.
4. **If you couldn't generate three of the five prompts unprompted → re-read the relevant Day's notes in `PROGRESS.md` and re-attempt the scenario tomorrow.**

The five prompts every time:

- **Fit:** Why does this choice belong in this slot?
- **Doesn't fit:** Where does this choice break down or become wrong?
- **Better:** What's the next-tier alternative, and what does it cost you to get it?
- **Scale:** How far does this go before it stops working?
- **Failure modes:** What breaks first, and how does the on-call engineer find out?

---

## Scenario 1 — The console-clicking team

You join a 12-person engineering org. They have ~80 AWS resources across 3 accounts (dev, staging, prod), all created by clicking in the console. The CTO says: "We want IaC. Pick a tool and convince me." You say: Terraform.

- **Fit:** Why Terraform here?
- **Doesn't fit:** Name a real situation where Terraform would be the *wrong* answer for this team.
- **Better:** Compare Terraform vs CloudFormation vs Pulumi vs CDK. When does each win?
- **Scale:** This team grows to 200 engineers in 12 accounts. Does the same Terraform setup hold? What changes?
- **Failure modes:** What goes wrong in the *first month* of adopting Terraform on top of 80 click-ops resources?

<details><summary>Scaffold</summary>

- *Fit triggers:* declarative, multi-cloud, plan-before-apply, state-as-source-of-truth, large provider ecosystem, HCL is readable, free.
- *Doesn't fit triggers:* single-cloud AWS-only shop with deep CloudFormation StackSet usage; teams that need *imperative* control flow (CDK/Pulumi); when "infra" is really one Lambda and one bucket (overkill).
- *Better:* CloudFormation = AWS-native, no state file to manage, but vendor lock + slower service coverage. Pulumi/CDK = real programming language (loops, classes, tests) but you trade auditability of HCL for the hazards of arbitrary code in your infra layer.
- *Scale:* one monolithic state file dies fast — split by blast radius (network/data/compute), workspaces or directory-per-env, remote state per stack, then atlantis/terraform-cloud/spacelift for governance. Module registry becomes mandatory.
- *Failure modes month-1:* `terraform import` hell on existing resources; drift between console-clicked state and TF state; someone runs `apply` with stale state and deletes something; no remote state → laptop becomes single point of failure.
- *Trap:* "Terraform is better than CloudFormation" — wrong framing for an interview. "Terraform fits *this* shop because…" is the right framing. Always tie the choice to the constraints in the scenario.

</details>

---

## Scenario 2 — The disappearing state file

A junior engineer ran `terraform apply` from their laptop. The `.tfstate` file is on their MacBook. They go on vacation. You need to ship a hotfix that touches the same VPC.

- **Fit:** Why is local state an acceptable default for `terraform init`?
- **Doesn't fit:** Why is local state catastrophically wrong for *this* situation?
- **Better:** What does S3 + DynamoDB actually solve? Why not just S3?
- **Scale:** A team of 30 engineers. Does S3 + DynamoDB still hold? What's the next tier above it?
- **Failure modes:** Someone deletes `terraform.tfstate` from S3. Now what?

<details><summary>Scaffold</summary>

- *Fit (local state):* zero setup, fine for solo learning, fine for ephemeral throwaway demos.
- *Doesn't fit:* shared infra + multiple humans = state divergence, no locking, secrets in plaintext on a laptop, no audit trail, no rollback.
- *Better:* S3 = durable + versioned + encrypted shared blob. DynamoDB = pessimistic lock so two `apply`s don't race and write conflicting state. **S3 alone does not give you locking** — that's the interview gotcha. Without the lock, two simultaneous applies can both read the same state, both modify infra, both write — last writer wins, intermediate resources orphaned in cloud.
- *Scale:* S3+DDB scales to dozens of engineers per stack but you also start splitting state per stack (one per service/env) so blast radius shrinks. Above that → Terraform Cloud / Spacelift / Atlantis with policy as code (OPA, Sentinel) and PR-driven plans.
- *Failure modes:* enable S3 versioning → restore prior version. If versioning was off → `terraform import` every resource one by one (slow, error-prone). If lock is stuck (process killed mid-apply) → `terraform force-unlock <id>` (only after confirming nobody is actually running).
- *Trap:* "DynamoDB stores the state" — no, DynamoDB stores *only the lock*. State lives in S3. Mixing those up signals you've never run it.

</details>

---

## Scenario 3 — The provider version question

You wrote `version = "~> 5.0"` for the AWS provider. Your CTO says: "Pin it exactly to `5.100.0`. Floating versions are dangerous."

- **Fit:** Defend `~> 5.0`. Why is it the right pin for *most* infra repos?
- **Doesn't fit:** When is the CTO right — when should you actually pin to an exact version?
- **Better:** What does `.terraform.lock.hcl` give you that the constraint in `provider.tf` does not?
- **Scale:** 50 repos all using AWS provider. Some pinned to 4.x, some to 5.x. What's the operational pain?
- **Failure modes:** Provider releases a breaking change in `5.101.0`. What's your blast radius with each pinning strategy?

<details><summary>Scaffold</summary>

- *Fit (`~> 5.0`):* "Get me 5.x but never silently jump to 6.0." Patch + minor upgrades are usually safe; major version is where break risk lives.
- *Doesn't fit:* highly regulated infra (banking, healthcare) where every provider version must be SOC-audited before promotion → exact pin.
- *Better:* the **lock file** records the *exact resolved version + checksum* across every dev's machine and CI. The `provider.tf` constraint is the *acceptable range*; the lock file is the *current reality*. They are not redundant — they are upper bound + actual snapshot. Commit the lock file.
- *Scale:* 50 repos with mixed versions = different bug-fix availability, different attribute support, runbooks that "work on my repo." Centralised module registry forces alignment; bumps become PRs across repos via Renovate/Dependabot.
- *Failure modes:* `~> 5.0` + missing lockfile + 5.101.0 breakage = simultaneous prod break across every repo on next `init -upgrade`. Lockfile blocks that — forces a deliberate `terraform init -upgrade` + commit.
- *Trap:* "I pin `>= 5.0`" — that's an unbounded floor. Never use it in prod.

</details>

---

## Scenario 4 — Why two AZs, not one, not six

Your VPC spans `us-east-1a` and `us-east-1b`. Why not just `1a` (cheaper)? Why not `1a/1b/1c/1d/1e/1f` (more durable)?

- **Fit:** Why is two the right default for most workloads?
- **Doesn't fit:** When is *one* AZ correct? When is *three+* correct?
- **Better:** When you go from 2 AZs to 3 AZs, what new properties do you unlock?
- **Scale:** 1M req/s across 2 AZs. One AZ goes down. What's the *application-layer* impact, not just the infra layer?
- **Failure modes:** Cross-AZ data transfer pricing — when does this become the dominant cost?

<details><summary>Scaffold</summary>

- *Fit (2 AZs):* survives single-AZ failure, half the cross-AZ data transfer cost of 3 AZs, satisfies most SLAs (99.9%), matches RDS Multi-AZ default model.
- *Doesn't fit:* throwaway dev = 1 AZ. Production EKS control plane = 3 AZs (AWS forces it). Quorum-based systems (etcd, Zookeeper, Kafka with min.insync.replicas=2) need *odd* AZ counts to avoid split-brain — 3 minimum.
- *Better (3 AZs):* odd quorum, can lose any one AZ and still have majority, mandatory for stateful distributed systems.
- *Scale:* 50% capacity loss in single-AZ outage if you ran 50/50. App-layer impact: connection storms to surviving AZ, RDS failover takes 60-120s, in-flight requests fail, autoscaler reacts but cold-start latency spikes. SLA budget burns in minutes.
- *Failure modes:* cross-AZ traffic is **$0.01/GB each way** in AWS. A chatty microservice mesh (10 services × 1KB calls × 50K req/s × cross-AZ) = thousands/month invisible bill. Topology-aware routing (EKS: `topologyKeys`, AWS: `Local Zone routing`) is the fix.
- *Trap:* "More AZs = more reliable, always." False — diminishing returns past 3, and cost climbs linearly.

</details>

---

## Scenario 5 — What actually makes a subnet "public"

You set `map_public_ip_on_launch = true` on a subnet. An EC2 in that subnet still can't reach the internet. Why?

- **Fit:** What is `map_public_ip_on_launch` actually for? When is it the right flag to flip?
- **Doesn't fit:** Why is it not sufficient on its own to make a subnet "public"?
- **Better:** What is the *single* thing that defines a subnet as public? (One sentence.)
- **Scale:** You have 50 subnets across 5 environments. Half need internet, half don't. How do you organise route tables to keep this auditable?
- **Failure modes:** Junior engineer creates a new subnet. Forgets the route table association. EC2 has a public IP but no internet. What does the symptom look like, and how do you diagnose it in under 60 seconds?

<details><summary>Scaffold</summary>

- *Fit:* `map_public_ip_on_launch` = "any EC2 launched in this subnet automatically gets a public IPv4 attached." It's an ergonomics flag, not a capability flag.
- *Doesn't fit:* a public IP without a route to `0.0.0.0/0` via an IGW is **a public IP that goes nowhere**. Like having a phone number with no phone line.
- *Better (definition):* **A subnet is public if and only if its route table has a route `0.0.0.0/0 → IGW`.** Nothing else. Not a tag, not the name, not the IP-on-launch flag.
- *Scale:* one shared public RT for all public subnets in a VPC (what you did) is fine. One private RT per AZ is the norm because each AZ gets its own NAT GW (Day 8+ topic). Tag religiously: `Tier=public/private`, `Environment=...`. Auditing tool: AWS Config rule `subnet-route-table-public`.
- *Failure modes:* symptom = `curl https://google.com` from EC2 hangs forever (not refused, *hangs* — packets leave, no return). 60-second diagnosis: VPC Reachability Analyzer, or check `aws ec2 describe-route-tables --filters Name=association.subnet-id,Values=<id>` and look for the IGW route.
- *Trap:* candidates say "I named it public-subnet so it's public." Names are documentation. Routes are reality.

</details>

---

## Scenario 6 — `cidrsubnet` and the IP math

Your VPC is `10.0.0.0/16` (65,536 IPs). You used `cidrsubnet(var.vpc_cidr, 8, count.index)` for public subnets and `cidrsubnet(var.vpc_cidr, 8, count.index + 10)` for private.

- **Fit:** What does `cidrsubnet(prefix, newbits, netnum)` actually compute? Walk through `cidrsubnet("10.0.0.0/16", 8, 10)` step by step.
- **Doesn't fit:** Why is `/24` (256 IPs per subnet) usually fine for app tiers, but disastrous for an EKS cluster?
- **Better:** When would you reach for `/22` or `/20` subnets instead?
- **Scale:** Your VPC is `10.0.0.0/16`. AWS reserves 5 IPs per subnet. EKS pods consume IPs from the subnet (not the pod CIDR) when using the AWS VPC CNI. How many pods fit in a `/24` subnet? When do you run out?
- **Failure modes:** You merge a second org via VPC peering. Their VPC is also `10.0.0.0/16`. What happens? Why is CIDR planning a *day-zero* decision?

<details><summary>Scaffold</summary>

- *Fit:* `cidrsubnet(prefix, newbits, netnum)` = take `prefix`, extend the mask by `newbits`, return the `netnum`-th subnet of that size. `cidrsubnet("10.0.0.0/16", 8, 10)` = mask becomes /24, 10th /24 = `10.0.10.0/24`. That's why your private subnets are `10.0.10.0/24` and `10.0.11.0/24`.
- *Doesn't fit:* /24 = 256 IPs, AWS reserves 5, leaves 251. EKS with VPC CNI assigns *one ENI IP per pod* — 251 pods/subnet ceiling, way less than nodes can theoretically hold. Cluster grows → IP exhaustion → pods stuck in `Pending` with `failed to assign an IP address`.
- *Better:* /22 = 1024 IPs, /20 = 4096. EKS data plane subnets = /22 minimum, /20 if you expect heavy autoscaling. Or switch CNI to use *prefix delegation* (each ENI gets a /28 of pod IPs), which 16x's pod density.
- *Scale:* 251 pods/subnet × 2 subnets = 502 pods cluster-wide on /24s. A 100-node cluster running 30 pods/node = 3000 pods needed → you hit the wall at ~17 nodes.
- *Failure modes:* overlapping CIDRs cannot peer. Period. There is no NAT-around-it. You'd have to recreate one VPC with a non-overlapping range and re-IP every resource. **Day-zero CIDR plan = pick a /16 that nobody else in your org uses, document it in a registry.**
- *Trap:* "I'll just use 192.168.0.0/16" — fine until you peer with the office VPN that uses the same range.

</details>

---

## Scenario 7 — Splat vs for-expression vs hardcode

Your output is `value = aws_subnet.public[*].id`. Interviewer asks: "Why splat? What else could you write?"

- **Fit:** Why is splat the right call here?
- **Doesn't fit:** When does splat break down? Show a case where you must use a `for` expression instead.
- **Better:** When would you use `for_each` (map) instead of `count` (list)? Why is `for_each` generally preferred today?
- **Scale:** You have 30 subnets. You want to output a map of `{az_name => subnet_id}`. Splat alone can't do this. What expression do you need?
- **Failure modes:** You used `count` and someone deletes the *first* subnet from your input list. What does Terraform plan show?

<details><summary>Scaffold</summary>

- *Fit:* splat (`[*]`) is concise, idiomatic for "give me attribute X from every element of this list."
- *Doesn't fit:* splat only extracts one attribute per element. Anything that needs *transformation* (e.g., `lower(s.name)`, conditional filter, build a map) needs `for`.
- *Better:* `for_each` keys resources by a stable identifier (map key or set element), not a positional index. Adding/removing one element only touches that element. With `count`, removing index 0 *renumbers everything* and Terraform plans destroy+recreate for every subnet from index 1 onward — a mass-destroy event. **`for_each` for anything you'll edit; `count` only for "make N identical copies."**
- *Scale:* `{ for s in aws_subnet.public : s.availability_zone => s.id }` — for-expression producing a map. Splat is list-only.
- *Failure modes:* `count`-based delete-from-middle = positional shift = plan shows N-1 destroys + N-1 creates. Terraform doesn't know "the third one is gone" — it knows "list of length 3 became list of length 2." Catastrophic on stateful resources.
- *Trap:* candidate uses `count` for everything because it's what they learned first. Modern Terraform = `for_each` by default, `count` only for `count = var.create ? 1 : 0` toggles.

</details>

---

## Scenario 8 — Data source vs hardcoded list

You used `data "aws_availability_zones" "available"` instead of hardcoding `["us-east-1a", "us-east-1b"]`.

- **Fit:** What does the data source give you that the hardcode doesn't?
- **Doesn't fit:** When does a data source bite you? Name a scenario.
- **Better:** What's the *third* option between dynamic data and hardcoded literal?
- **Scale:** You add a third subnet via `count = 3`. AWS region returns 6 AZs in alphabetical order. Are you guaranteed `1a, 1b, 1c`? Why or why not?
- **Failure modes:** Data source resolves at *plan time*. AWS deprecates an AZ. Your plan changes unexpectedly. How do you detect and freeze?

<details><summary>Scaffold</summary>

- *Fit:* portability across regions (different regions have different AZ names), tolerant of AWS adding/removing AZs, no manual edit when you change region.
- *Doesn't fit:* data sources hit live AWS at every plan — adds latency, requires credentials, and the *result can change between plans*. If AWS marks an AZ as `impaired`, your filter `state = "available"` silently changes the AZ list and Terraform plans subnet recreation. That's a destroy-everything event for a network change you didn't author.
- *Better:* hybrid — data source provides the *catalog*, but you pin a *specific subset* via `var.azs = ["us-east-1a", "us-east-1b"]` and validate against the data source. Best of both: portable + stable.
- *Scale:* AWS does not guarantee alphabetical order of `data.aws_availability_zones.available.names`. You may get `["us-east-1c", "us-east-1a", "us-east-1d", ...]` because each AWS account is randomly mapped to physical AZs to spread load. Your `1a` is not your colleague's `1a`.
- *Failure modes:* AZ deprecation → `state = "available"` filter drops it → plan now shows subnet destruction. Detection: `terraform plan` in CI on every PR + alert on unexpected destroys. Freeze: switch to `var.azs = [...]` (hardcoded subset) before deprecation date.
- *Trap:* "AZs are alphabetical." Wrong. Per-account randomisation. Cross-account references must use the AZ *ID* (`use1-az1`), not the name (`us-east-1a`).

</details>

---

## Scenario 9 — The dependency graph

You ran `terraform apply` and saw it create the IGW, VPC, subnets, and route tables in parallel where possible.

- **Fit:** How does Terraform decide what to parallelise?
- **Doesn't fit:** Name two cases where Terraform's auto-detected dependencies miss real-world ordering.
- **Better:** What does `depends_on` give you that implicit references don't?
- **Scale:** A 500-resource plan. Default parallelism is 10. Should you bump it to 100? What goes wrong?
- **Failure modes:** Apply dies halfway. State has 14 of 30 resources, cloud has the same 14. What's the next move?

<details><summary>Scaffold</summary>

- *Fit:* implicit graph from references (`aws_vpc.main.id` referenced in `aws_subnet.public` → subnet depends on VPC). Independent branches run concurrently up to `-parallelism` (default 10).
- *Doesn't fit:* (1) IAM eventual consistency — role created, attached to EC2, EC2 boots and policy hasn't propagated yet → boot-time auth failure. Need explicit `depends_on` + retries. (2) Lambda + log group — Lambda auto-creates a log group on first invoke; if you also declare it in TF, race condition. Need `depends_on` on the log group from the Lambda.
- *Better:* `depends_on` declares ordering Terraform can't infer (no attribute reference exists). Use sparingly — every `depends_on` is a serialisation point that slows plans.
- *Scale:* parallelism 100 = 100 concurrent AWS API calls. AWS rate limits per-account-per-service (e.g., EC2 = ~100 req/s burst). Above default → throttling errors → Terraform retries → slow apply or partial failure. Better fix = split state by stack rather than crank parallelism.
- *Failure modes:* state + cloud both have 14 → re-running `apply` is safe; Terraform sees 16 missing and creates them. Lock should auto-release on clean Ctrl-C; on `kill -9` mid-apply, lock may stick → `force-unlock` after confirming no other run.
- *Trap:* "Bigger parallelism = faster" — wrong. AWS rate limits + DependsOn graph mean returns diminish hard above ~20.

</details>

---

## Scenario 10 — Outputs as the contract

Your `outputs.tf` exposes `vpc_id`, `public_subnet_ids`, `private_subnet_ids`. Why these three? Why not `vpc_cidr`, `igw_id`, `route_table_id`?

- **Fit:** What's the design rule for "what should be an output"?
- **Doesn't fit:** When is exposing too many outputs harmful?
- **Better:** Outputs in a single root module vs `terraform_remote_state` data source vs SSM Parameter Store — when do you reach for each?
- **Scale:** 12 stacks (network, EKS, RDS, IAM, ECR, ALB, ...). Each consumes outputs from others. How do you avoid a tangled web?
- **Failure modes:** You change an output type from `list(string)` to `map(string)`. What breaks downstream? How do you migrate without an outage?

<details><summary>Scaffold</summary>

- *Fit:* outputs are the **public API of a stack/module**. Expose what other stacks/humans legitimately need — IDs they reference, not internals they shouldn't reach into.
- *Doesn't fit:* exposing internals (route table IDs, NAT GW IDs) creates coupling — consumers start depending on internals, you can't refactor without breaking them. Same as exposing private fields in OOP.
- *Better:* same-repo modules → outputs. Cross-repo, same-team → `terraform_remote_state` (read another stack's state directly). Cross-team / cross-tool → SSM Parameter Store or AWS Service Catalog (typed, audited, versioned). The further the consumer, the more formal the contract.
- *Scale:* dependency tree gets thick → adopt the **layer pattern**: foundation (network, IAM) → platform (EKS, RDS) → app (services). Each layer reads only from the layer below. No upward references.
- *Failure modes:* type change is a breaking change in your stack's API. Downstream consumers fail at plan time. Migration: add the new output under a new name, deprecate the old, update consumers, delete the old in a follow-up release. Same SemVer discipline as a public library.
- *Trap:* "I'll output everything just in case." Outputs are a contract — adding is cheap, removing breaks consumers.

</details>

---

## Scenario 11 — Drift

Someone clicks in the console and adds an inbound rule to a security group that Terraform manages. Next `terraform plan` shows the rule as a destroy.

- **Fit:** Why is this Terraform behaving correctly?
- **Doesn't fit:** When is "Terraform reverts manual changes" the *wrong* behaviour for the situation?
- **Better:** What are the four ways to handle drift, ranked from "best practice" to "last resort"?
- **Scale:** 200 engineers, IAM lets some of them touch the console for emergencies. How do you institutionalise drift control?
- **Failure modes:** Drift detected at 2am during an incident. Reverting it makes the incident worse. What's your move?

<details><summary>Scaffold</summary>

- *Fit:* state file says "this rule should not exist." Cloud says it does. Terraform's job is to converge cloud → state. Behaviour is correct.
- *Doesn't fit:* incident response — engineer added an emergency allow-rule to mitigate an attack. `terraform apply` would close that rule and re-expose the system. Process problem, not Terraform problem.
- *Better:* (1) **Codify the change** — add it to TF and apply, drift gone. (2) **Import the resource** — if it should be TF-managed but wasn't. (3) **`terraform state rm`** — if the resource should NOT be TF-managed (rare, dangerous). (4) **`-refresh-only` + accept** — record cloud reality into state without modifying cloud, only when you're sure cloud is right.
- *Scale:* IAM permissions boundary that blocks console *write* in prod, allows read. Drift detection in CI (nightly `terraform plan -detailed-exitcode` → alert if exit 2). PR-driven changes only. Break-glass role with audit logging for true emergencies.
- *Failure modes:* freeze the apply pipeline first (lock, don't unlock). Codify the emergency change in a hotfix branch. Apply the *combined* (existing + emergency) state. Never let a routine apply run during an active incident.
- *Trap:* "Just remove drift detection." Worst answer. Drift detection is a smoke alarm — disabling it doesn't put out the fire.

</details>

---

## Scenario 12 — The boss-level system design

**Prompt:** Design the network layer for a SaaS company running 4 microservices on EKS in 2 AWS regions, serving 50K req/s globally, with a Postgres cluster (writer + 2 read replicas), and an internal admin portal that should only be reachable from corporate VPN. You have 30 minutes at the whiteboard. Use *only* concepts covered in Days 6–7 (VPC, subnets, IGW, route tables, CIDR, AZs, IaC) plus their natural extensions (NAT GW, VPC peering, TGW, security groups — preview from Day 8+).

Sketch your answer, then check yourself against the scaffold.

- What CIDR do you pick for each region? Why? How do you avoid overlap?
- Public vs private subnet layout — what goes where?
- How many AZs per region, and why?
- How do the EKS pods talk to Postgres? How do they reach the internet for image pulls?
- How does the admin portal stay corp-VPN-only?
- How does Region A talk to Region B (DB replication, failover)?
- Where does Terraform state live, and how is it organised?
- Three things that break first under 10x load — name them.

<details><summary>Scaffold</summary>

- *CIDR:* Region A `10.0.0.0/16`, Region B `10.1.0.0/16`. Non-overlapping by construction. Document in a CIDR registry. Pod CIDR (if EKS VPC CNI prefix delegation) overlaps within VPC but not across — fine.
- *Subnet layout:* 3 AZs per region (odd, EKS-friendly). Per AZ: 1 public /24 (ALB, NAT GW), 1 private /22 (EKS data plane — IP-hungry), 1 private /24 (RDS). Total per region: 9 subnets.
- *AZs:* 3 per region. Two reasons — EKS control plane requires 3, and most stateful systems (etcd, Kafka, Postgres-via-Patroni) want odd quorum.
- *Pods → Postgres:* same VPC, private subnet → private subnet, security group on RDS allows EKS node SG. No internet involved.
- *Pods → internet:* private subnet → NAT GW in same-AZ public subnet → IGW → internet. One NAT GW per AZ (not shared) to avoid cross-AZ data fees and AZ-failure blast radius. Use VPC endpoints for S3/ECR/STS to bypass NAT entirely (cheaper + faster + private).
- *Admin portal corp-VPN-only:* internal ALB in private subnet, no public IP. Reach it via Site-to-Site VPN or AWS Client VPN. Security group limits source to VPN CIDR. Optionally Direct Connect for low-latency.
- *Cross-region:* Transit Gateway peering or VPC peering between regions for DB replication. Postgres replicas via logical replication or Aurora Global. Route 53 weighted/latency routing for failover.
- *Terraform state:* per-region S3 bucket (avoid cross-region dependency for state itself), per-stack state file (network / eks / rds / iam separate), DynamoDB lock table per region. Layered: foundation → platform → app.
- *10x breaks first:* (1) NAT GW bandwidth ceiling (5 Gbps/AZ, scales to 100 but $$); (2) IP exhaustion in EKS subnets if you sized for 1x; (3) cross-AZ data transfer cost dominates the bill before compute does.
- *Trap:* candidates draw a single AZ "for simplicity." Interviewer hears "I haven't run prod."

</details>

---

## Self-grading rubric

After working through the doc end-to-end:

- **Green** (interview-ready): you generated the trigger words for *Fit* and *Failure modes* in 9+ scenarios without scaffold.
- **Yellow** (one more pass): you got *Fit* but stumbled on *Doesn't fit* or *Failure modes* in 4+ scenarios.
- **Red** (re-read PROGRESS.md Days 6–7 + redo): you needed the scaffold to start in 4+ scenarios.

Track your colour at the top of `PROGRESS.md` after each pass. Two consecutive Greens = Phase 2 concepts (Days 6–7 slice) banked.

---

## Where this drill stops, and why

This doc covers Days 6–7 only — Terraform foundations + the VPC layer. Days 8–14 will add: security groups (source-SG references, layered defence), EC2 + ALB, ECR, IAM (roles vs users vs policies vs trust relationships), modules, workspaces, and the rest. **A second drill doc should be written after Day 14**, covering the same five-prompt format but for compute, identity, and module composition. Don't try to backfill those scenarios into this file — keep one drill per phase-slice so you can test recall against a stable surface.
