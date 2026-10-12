# On-call agent

An AI assistant for Kubernetes incident triage. It collects evidence from a cluster,
assembles a bounded picture of one incident, and asks a model on Amazon Bedrock for ranked
hypotheses with read-only commands to verify them. It diagnoses only: the model never sees
the cluster, and nothing it returns is executed.

## M6: the triage UI

```bash
make -C oncall ui            # http://127.0.0.1:8501, loopback only
```

- **Diagnose.** Subjects with a warning or worse in the last six hours are offered worst
  first. Preview assembles the evidence and states the prompt size with nothing sent;
  Diagnose opens or reuses the incident, calls Bedrock and journals, through the same
  `diagnosis/service.py` that `make diagnose` uses.
- **Reading order** is evidence gaps, the first command to run, the summary, then each
  hypothesis with its verify commands and the evidence it cites. Citations are resolved
  through the bundle snapshot, so each shows the finding, its log excerpt and its facts.
- **Failures are banners.** A rejected, invalid or failed diagnosis names its outcome and
  errors; it never renders as an empty answer.
- **History** merges the buffer and the store. An unreachable store is skipped for 30 s
  after one failed read, so the UI does not stall on every click during an incident.
- **Unknowns are journalled** from this release (`store/migrations/0005`). Earlier rows
  show them as not recorded, which is different from a diagnosis that named none.
- Commands are shown to copy, never executed. Collect now runs the registered
  collectors; nothing else in the UI reaches the cluster.

## M5: the reasoner

### How a diagnosis is made

```
collectors ─▶ SQLite buffer ─▶ assembler ─▶ bundle ─▶ prompt ─▶ Bedrock Converse
                  │                            │                  (forced tool call)
                  │                            ▼                         │
                  │                       snapshot                       ▼
                  │                 (data/bundles/<sha>.json)   schema validation,
                  │                                             citation and command guards
                  ▼                                                      │
            outbox ─▶ shipper ─▶ Postgres store ◀──── journal ◀──────────┘
                                       │
                                       ▼
                               eval corpus, replay
```

- **Evidence.** Collectors write signals to a local SQLite buffer. The assembler turns one
  subject's signals into a bundle: ranked findings, the sources that were blind, what was
  left out and why. Every finding is labelled F1, F2 ... so the model can cite it.
- **Prompt.** The bundle is rendered inside `<evidence>`. Anything the cluster wrote (log
  lines, event messages, any value containing spaces) sits in an `<untrusted>` fence that
  it cannot close, and the system prompt states that such text is never an instruction.
  Every cluster string is NFKC-normalised and stripped of zero-width and bidirectional
  characters before tags are escaped, so a look-alike tag cannot pass. Secrets are
  redacted where they are collected and once more on the finished text. A bundle whose
  sources last ran more than 15 minutes before assembly says its evidence may be stale.
- **Model call.** One Converse request with one tool, `record_diagnosis`, and `toolChoice`
  naming it, so the answer has to arrive as arguments in the tool's JSON schema. Temperature
  is 0. Retries for throttling are botocore's, in adaptive mode.
- **Checks in code.** The answer is validated against the Pydantic contract, every cited ID
  must be a finding in the bundle, and every suggested command must be a single read-only
  kubectl command on the allowlist, with no flag that switches cluster, identity or server,
  reads a raw API path, or raises verbosity, and no Secret printed in any spelling
  (`secrets.v1` included). The one exception is `--as` after `auth can-i`, which asks
  whether an identity may act and changes nothing. The prose may carry no links, and hypothesis 1 must cite
  evidence about the subject that is not only a report of blindness. A fault the model can
  correct earns one repair attempt with the specific errors; an unsafe command rejects the
  diagnosis with no retry. Nothing raises: evidence over the prompt cap is journalled as
  `prompt_too_large` with nothing sent, and every other call ends in a record too.
- **Journal.** Every attempt is written, failures included, with both receipts:
  `bundle_sha256` for the evidence and `prompt_sha256` for the exact bytes and tool
  definition sent. It goes through the same outbox as every other row, so a diagnosis made
  while Postgres is down reaches the store later.
- **Evaluation.** Bundles are frozen per lab fault and labelled with the cause the evidence
  supports. `make eval` runs them all and writes a report.

### Model and region

`us.anthropic.claude-sonnet-4-6`, called from `us-east-1`, the cluster's region. The `us.`
inference profile routes to us-east-1, us-east-2 and us-west-2 only, so:

- calls from the cluster reach Bedrock through the `bedrock-runtime` VPC endpoint and never
  through the NAT gateway;
- the IAM policy names the profile and the three foundation-model ARNs, with no wildcard.

Current Claude models have no single-region on-demand capacity, so a bare model ID is
refused; the profile is what lets Bedrock place a request in any of its regions.

### Running it

```bash
# one-time: Bedrock settings in oncall/.env, see .env.example
make -C oncall lab-all && make -C oncall collect

make -C oncall diagnose SUBJECT=crashloop ARGS="--dry-run"   # the exact prompt, no call
make -C oncall diagnose SUBJECT=crashloop                    # incident, call, journal
make -C oncall diagnose ARGS="--replay <diagnosis_id>"       # same evidence, today's prompt
make -C oncall bedrock-check                                 # config, identity, endpoint,
                                                             # routing vs IAM, one call

make -C oncall eval-freeze                                   # after stage 5 and a collect
make -C oncall eval                                          # live, costs money
make -C oncall eval ARGS="--repeat 3"
make -C oncall eval ARGS="--check"                           # gate for prompt or model changes
```

### Latest evaluation

Accepted report `eval/reports/2026-10-12T1315-v5-claude-sonnet-4-6.md`: model
`us.anthropic.claude-sonnet-4-6`, prompt v5, 17 frozen fixtures run three times (51 calls,
45 scored).

| Metric | v3 (2026-10-08) | v5 (2026-10-12) |
|---|---|---|
| Top-1 accuracy | 44/45 (98%) | 45/45 (100%) |
| Top-3 accuracy | 44/45 (98%) | 45/45 (100%) |
| Citation validity | 51/51 | 51/51 |
| Command safety | 50/51 | 51/51 |
| First-attempt valid | 46/51 (90%) | 51/51 (100%) |
| Latency p50 / p95 | 16.9 s / 34.5 s | 13.8 s / 18.3 s |
| Mean tokens in / out | 8,534 / 1,298 | 7,246 / 1,250 |
| Cost, mean / total | $0.045 / $2.30 | $0.041 / $2.06 |
| Self-agreement across repeats | 49/51 | 51/51 |

v5 raised the summary bound from 600 to 900 characters and the citation bound from 10 to
15 per side, and stated both in the prompt. In v3, three of five first-attempt failures
were those bounds alone; v5 used up to 789 characters and 11 citations with no repair.
Each repair re-sends the whole prompt, which is why p95 latency halved. The one v3
rejection was the guard refusing `auth can-i --as`, which is now allowed.

### Observability

A diagnose call is a short-lived process, so nothing in it is scraped. The journal is the
record, and `exporter/journal.py` aggregates it on every scrape: outcomes, first-attempt
validity, attempts, a latency histogram, tokens and list-price cost, plus
`oncall_journal_up` so an unreadable store never reads as zero failures.

```bash
make -C oncall obs-up        # store, Prometheus :9090, Grafana :3000 (compose profile)
make -C oncall exporter      # :9464/metrics on the host
make -C oncall rules-test    # promtool check and unit tests
make -C oncall obs-render    # PrometheusRule and dashboard ConfigMap for the cluster
```

Rules live once, in `observability/prometheus/rules/oncall-llm.rules.yml`: seven recording
rules and nine alerts covering exporter health, p95 latency, failure rate, Bedrock errors,
guard rejections, oversized prompts, first-attempt validity and daily spend. Every
alert has a volume guard, because a single slow call is otherwise a 100% rate. The
cluster manifests in `observability/k8s/` are rendered from the same sources, a test fails
when they drift, and they are applied together with the exporter's Deployment.

### Known limits

- **Small, synthetic golden set.** 15 scored lab faults, each a single clean failure.
  Production incidents overlap, cascade and arrive with noise the lab does not make.
- **One model.** Sonnet 4.6 through one profile. Haiku 4.5 is the comparison point; the
  Sonnet 5 family was not available to this account when M5 was built.
- **Costs are list prices** from the configured per-million-token rates, not the bill.
- **Latency is measured from a laptop.** A call from inside us-east-1 through the VPC
  endpoint avoids roughly 800 ms of connection setup per new client that a laptop in India
  pays.
- **Two faults the collectors cannot fully see.** No source reads Deployment conditions or
  a pod's deletionTimestamp, so a stuck rollout is diagnosed from its pods and a pod stuck
  terminating is reported but not scored.
- **Spacing is not rhythm.** Collection is manual until the background collector exists,
  so the interval between observations says nothing about the workload.
- **Metrics count arrival, not occurrence.** The exporter reads the store, so a diagnosis
  journalled while Postgres was down counts when it ships. The first diagnosis under a
  model or prompt version the exporter was not configured with is invisible to
  `increase()`, because its series is born at 1.
- **Snapshots live on local disk.** Replay needs the bundle a diagnosis saw; a deployed
  agent would keep those in object storage.
