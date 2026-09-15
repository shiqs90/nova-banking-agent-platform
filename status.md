# Nova — Status

Living to-do tracker. Update the checkbox and date when a step lands. Design rationale lives in
`IMPLEMENTATION-PLAN.md`; the interview-facing summary is `README.md`. This file is just
"what's done, what's next."

Last updated: 2026-08-28

**Rescoped 2026-08-15/16** — from a DistilBERT/banking77 classifier pipeline to an MLOps
platform for a tool-calling agent, where evaluation is the deployment gate. GitOps delivery was
moved out of the core build into enhancements: a deployment gate is only meaningful once there
is something worth gating. **Partly reversed 2026-08-24** — Argo CD sync was pulled back into
the core build as Phase 4a because the remaining phases are the most manifest-heavy in the
project. The gating half (Argo Rollouts + CI) stays open as 4b.

## Carried forward — still valid

- [x] Platform decision: GCP / GKE, no GPU, standalone
- [x] `terraform-gcp/` — VPC, subnet, secondary ranges, Cloud Router, IAP-only SSH ($0, applied)
- [x] `terraform-gcp/gke.tf` + `artifact-registry.tf` written, **not yet applied**
- [x] GCS infra in place
- [x] GitOps + blue-green rollout strategy and its rationale
- [x] Gate layering (CI contract → CI smoke → pre-promotion → post-promotion)
- [x] `IMPLEMENTATION-PLAN.md` rewritten for the new scope
- [x] `README.md` written — coverage table, architecture, gates, metrics, time estimate
- [x] Repo renamed to `mlops-agent-platform`

## Superseded

- [x] ~~DistilBERT + banking77 as the product~~ — returns in **E4** as the cheap router, with a
      cost justification instead of being the deliverable
- [x] ~~MLflow as the central registry from day one~~ — deferred to **E4**, where a trained
      artifact exists
- [x] ~~Seeded retail/ops dataset~~ — replaced by banking, which makes PII masking and human
      approval genuinely required rather than decorative
- [ ] Azure/AKS track (`terraform/`) — parked, kept as a JD-B asset, not deleted
- [ ] `docs/architecture.md`, `docs/mlops-lifecycle.md` — still describe the classifier design,
      revise after Phase 2

## Housekeeping

- [x] `git init` — repo live, 7 commits through 2026-08-18
- [ ] Check `authorized_cidr` (`variables.tf:90`) against current public IP before applying —
      `curl -s -4 ifconfig.me`. Stale value = `kubectl` hangs with no clear error.

---

# CORE BUILD (~47.5h) — ~46h done (**97%**), ~1.5h left as of 2026-09-15

Every load-bearing piece is built and proven on real runs: CI builds → git holds the tag →
Argo CD syncs → Argo Rollouts gates on the golden set → promotes or rolls back unattended →
the one write tool halts for a human and cannot execute twice.

| remaining | est |
|---|---|
| Judge calibration — hand-label 18, measure agreement with Haiku | 0.5h |
| Phase 5.6 — Langfuse dataset runs | 1h |
| Phase 2.5 loose ends — PII trace check, `/chat` paused-session guard, transfer eval case | ~0.5h |

None of it changes the story. The project is demonstrable as it stands.

End state: a working agent with its safety controls in place (PII masking, approval on writes),
plus an automated evaluation service that scores it on demand and on a schedule, and alerts when
quality drifts.

## Phase 0 — Infrastructure (~2h) — DONE 2026-08-16

- [x] GKE zonal cluster `mlops-lifecycle` applied — `RUNNING`, us-west1-b
- [x] Node pool `primary` — `e2-standard-4`, 50 GB, GKE 1.35.6
- [x] Artifact Registry `mlops-lifecycle` — DOCKER, us-west1, terraform-provisioned
- [x] kubectl context configured via `get-credentials`
- [x] Nodes scaled to 0 between sessions (cluster + pool retained)

## Phase 1 — Data and MCP connectors (~10h)

*Scope grew by ~2h: secrets management was added rather than using `kubectl create secret`,
because an imperative Secret lives nowhere in git and breaks the reconstruct-from-repo property
Phase 4 depends on.*

- [x] **Scale nodes up** — 2 × `e2-standard-4` Ready (2026-08-16). Nothing schedules at 0:
      ```bash
      gcloud container clusters resize mlops-lifecycle --node-pool=primary --num-nodes=2 \
        --zone=us-west1-b --project=mlops-lifecycle-p7-gke --quiet
      ```

### 1a — Secrets (Secret Manager → ESO → cluster) — DONE 2026-08-16

- [x] `terraform-gcp/secrets.tf` applied — Secret Manager secret `nova-postgres-password`,
      ESO service account, scoped `secretAccessor`, Workload Identity binding
- [x] External Secrets Operator installed via Helm with the `iam.gke.io/gcp-service-account`
      annotation
- [x] CRDs registered as **`external-secrets.io/v1`**, not `v1beta1` — both apiVersion lines in
      `k8s/external-secrets.yaml` were edited to match. Re-check after any chart upgrade; the
      CRDs decide, not the docs.
- [x] `k8s/external-secrets.yaml` applied — SecretStore + ExternalSecret reached `SecretSynced`

### 1b — Postgres — DONE 2026-08-16

- [x] `k8s/postgres.yaml` — StatefulSet, headless Service, 10Gi PVC, `postgres-0` Running
- [x] `db/schema.sql` loaded — 6 tables incl. `ingest_runs` lineage

### 1c — Seed data — DONE 2026-08-16

- [x] `db/seed.py` + `k8s/seed-job.yaml` (ConfigMap-mounted script, no image build)
- [x] Loaded: `ingest_run_id=ING-20260801-42` — 2,000 customers / 3,000 accounts /
      ~200,397 transactions / 2,488 cards / 589 loans
- [x] verify: 0 balance mismatches (balance == transaction net)
- [x] **Bug found and fixed:** 397 accounts (13%) held balances their type doesn't permit —
      savings and fixed deposits overdrawn against a 0 limit, worst −163k. Cause: transactions
      assigned to random accounts regardless of account type. Fixed by emitting an
      `opening_balance` credit for any account below its floor, **not** by clamping the balance
      — clamping would break `balance == sum(transactions)` and let Nova contradict itself
      across two questions in one conversation. Current accounts legitimately overdrawn within
      their limit were left alone; they're the interesting cases for overdraft questions.

### 1d — MCP connectors — DONE 2026-08-16

- [x] One image, three entrypoints: `mcp-servers/{common,accounts,transactions,products}.py`
- [x] Built via **Cloud Build** (`gcloud builds submit`) — local Docker daemon wasn't running,
      and Cloud Build also builds natively on amd64, sidestepping the arm64 `exec format error`
      that bites when building on Apple silicon for GKE nodes
- [x] All three Deployments Running, serving StreamableHTTP on :8080
- [x] verify: `tools/list` returns the expected tools per server — 9 tools across the three
      servers, confirmed via `/healthz`
- [x] verify: a SQL query and the equivalent tool call agree — `check_balance(ACC-00004)`
      returns 185,254.95 AED, matching the DB exactly

## Phase 2 — Nova agent (~8h) — mostly done 2026-08-17

- [x] `nova/app.py` — FastAPI + LangChain `create_agent`, four runaway limits
      (recursion, max_tokens, request timeout, client timeout), agent/judge models as
      separate env vars so the judge can move to Sonnet without a code change
- [x] `nova/Dockerfile`, `nova/requirements.txt` — **`mcp` pinned explicitly**, not left
      to transitive resolution (see war story #10)
- [x] `k8s/redis.yaml` — **`redis/redis-stack-server`**, not `redis:7-alpine`; the
      checkpointer needs RediSearch (war story #11)
- [x] `k8s/nova.yaml` — Deployment + Service, plain Deployment on purpose (becomes an
      Argo Rollout in 4b, not before)
- [x] Built via Cloud Build, deployed, **9 tools loaded** across all three MCP servers
- [x] Redis checkpointer connects — `session memory: redis at ...`, no fallback
- [x] **verify: single-tool question returns a correct balance** — `185,254.95 AED`,
      matches the DB exactly, correct tool and args
- [x] **verify: follow-up resolves account + date from memory** — "that account" → `ACC-00004`,
      "July 2026" → `2026-07-01`/`2026-07-31`, `category: salary`
- [x] **Reporting bug fixed** (war story #17) — `result["messages"]` is the whole thread, so
      `tool_calls` and `usage` were accumulating across turns. Would have failed every
      multi-turn Phase 5 eval case against a correctly-behaving agent. Now sliced per turn;
      `turn_messages` / `history_messages` added so the cost gate can tell an inefficient
      agent from a long conversation.
- [x] **Stale-answer bug fixed** (war story #18) — the agent sometimes answered repeat
      questions from a previous turn's tool result instead of re-querying, non-deterministically.
      System prompt now requires a tool call for any balance/transaction/card/loan question.
      Verified: Haiku calls the tool 100% of the time after the fix, which also answers
      "is Haiku not smart enough?" — it was an unwritten rule, not a capability limit.
- [x] Lockfiles — `nova/requirements.lock`, `mcp-servers/requirements.lock` via
      `uv pip compile --python-platform linux`; both Dockerfiles install from the lock.
      Three outages in this build came from dependency resolution, not code.

**Live tags:** `nova:20260817-1245-nocache`, `mcp-servers:20260817-1148-lock`
**Rollback:** `nova:20260817-1115`, `mcp-servers:20260816-1924`
- [x] `docs/DEBUGGING.md` written — command playbook by symptom
- [x] War stories #7–14 recorded in `PROJECT7-SUMMARY.md`

### Deferred from Phase 2 — cleared 2026-08-19

This list was stale; every item had in fact landed. Retained rather than deleted because one
of them shipped differently than planned.

- [x] FastAPI + LangChain `create_agent`
- [x] MCP adapter binding the three servers as tools
- [x] Redis StatefulSet + LangGraph checkpointer (session memory)
- [x] `trace_id` issued on request entry — **returned in the response BODY, not a header.**
      The plan said header; the code never did that. The body is the right place here: the
      eval runner already parses the JSON for `tool_calls` and `usage`, so a header would be
      a second thing to read for no benefit. Fixed the doc, not the code.
- [x] verify: single-tool question correct; follow-up resolves from memory

## Phase 2.5 — PII masking + human approval middleware (~2h) — DONE 2026-09-15

LangChain prebuilt middleware (`langchain==1.3.15`). Banking makes both **required, not
decorative**.

- [x] `HumanInTheLoopMiddleware(interrupt_on={"initiate_transfer": True})` — per-tool, so
      only the write pauses and the 18-case golden set is untouched
- [x] `PIIMiddleware("email", strategy="redact", apply_to_tool_results=True)` — on **tool
      results**, not input. The original plan said "mask account numbers before the prompt
      leaves the boundary"; that would strip `ACC-00004` before the agent could pass it to a
      tool and break every case. The PII arrives FROM `get_customer`; redacting it there is
      the correct boundary. Email only — `full_name` has no reliable regex, and a detector
      that misses half of them is worse than not claiming one.
- [x] `POST /approve` — resumes by `session_id`, which IS the token: LangGraph keyed the
      paused state on `thread_id` in Redis. `approve` | `reject`; `edit` and `respond` exist
      in the API and are unused.
- [x] verify: **transfer halts** — `/chat` returned `pending_approval` with the full
      `action_requests`; balances unchanged
- [x] verify: **approve executes** — `initiate_transfer` ran, ACC-00004 `185,254.95 →
      184,754.95`, ACC-00002 `242,477.92 → 242,977.92`
- [x] verify: **double-approve refused** — second `/approve` returned `nothing_pending`;
      the transfer cannot run twice
- [ ] verify: PII — `get_customer` in a Langfuse trace shows `[REDACTED_EMAIL]` in the
      ToolMessage (gs-013 is the case)
- [ ] `/chat` guard: refuse a new message on a paused session instead of corrupting it
      (troubleshooting #28 — a retried `/chat` wedged `hitl-demo` permanently)
- [ ] golden set: add the reserved `refuse`-shape case asserting a transfer request halts

**Two things learned the expensive way** (troubleshooting #28): the resume payload is
`Command(resume={"decisions": [...]})` — a dict, stated by one line of library source at
`human_in_the_loop.py:450`, inferred wrongly from type names and deployed unverified. And a
`/chat` retry on a paused session appends a `HumanMessage` after a dangling `tool_use`,
which Anthropic rejects and nothing can repair.

## Phase 3 — Observability (~5h) — DONE 2026-08-19

- [x] **Decided: Langfuse cloud free tier.** Self-hosting Langfuse means running its own
      Postgres and ClickHouse — more billable cluster than the thing being observed.
- [x] Langfuse + LangChain callback handler — one handler instruments the whole agent.
      Deliberately optional: missing keys leave Nova running untraced rather than refusing to
      start (`optional: true` on the Secret refs). Observability must not take down what it
      observes.
- [x] kube-prometheus-stack installed via Helm. The two `...SelectorNilUsesHelmValues=false`
      flags are load-bearing — without them the operator only picks up monitors carrying its
      own release label, and the ServiceMonitor is ignored silently, with no error and an
      empty graph.
- [x] Nova metrics at `/metrics`: `nova_requests_total{status}`, `nova_request_duration_seconds`,
      `nova_tool_calls_total{tool}`, `nova_tokens_total{direction}`, `nova_cost_usd_total`,
      `nova_tools_per_turn`, plus `nova_memory_persistent` / `nova_tracing_enabled` gauges
- [x] `charts/nova/templates/monitoring.yaml` applied — ServiceMonitor + 6 alert rules, target `up=1`
- [x] verify: one question → one Langfuse trace; metadata `trace_id` matches the response body
- [x] verify: counters move correctly — 3 requests → `requests_total{ok}=3`,
      `check_balance=2`, `get_cards=1`, cost $0.0185

### What the first real numbers showed

**93% of spend is INPUT tokens** (17,209 in / 264 out across 3 turns, ~$0.0062/request). The
answers are short; what costs money is what gets sent *to* the model every turn — system
prompt, nine tool schemas, and the whole history resent each call. The cost levers are
therefore tool-schema verbosity, history truncation, and prompt caching. Shortening answers
would move nothing.

Consequence for the cost gate: **cost-per-request is only comparable at equal conversation
depth.** A longer conversation costs more per turn with identical behaviour. The golden set is
mostly single-turn so the Phase 5 baseline is clean, but E2's live-traffic scoring will see
real multi-turn sessions and the distribution will look alarming until it is normalised by
`history_messages`.

### Counters are not durable state

An in-process Prometheus counter resets to zero on every pod restart — by design, since
`rate()` and `increase()` detect the reset. So `sum(nova_requests_total)` lies after a restart;
`sum(increase(nova_requests_total[24h]))` does not. The alert rules all use `rate`, so they are
correct as written. Related trap seen live: a *labelled* counter does not exist at all until
its first `.inc()`, so `nova_requests_total` is absent from `/metrics` on a fresh process while
the unlabelled `nova_cost_usd_total` already reads 0.0. An absent series and a zero series mean
different things.

## Phase 4 — GitOps delivery (~11h) — 4a + 4b DONE, 4c manifests written

**The phase that makes evaluation an actual deployment gate.** Until this lands, the Hub
produces a verdict but nothing consumes it.

### 4a — Argo CD sync — DONE 2026-08-24

Pulled ahead of Phase 2.5/5 deliberately: the phases that remain are the most manifest-heavy
in the project (Rollout, preview Service, two AnalysisTemplates, runner Job), which is the
worst possible time to still be applying by hand.

- [x] Argo CD installed via Helm, **chart pinned 10.4.0 → v3.5.1**. dex + notifications
      disabled; `applicationSet.enabled: false` does NOT exist in 10.x and was silently
      ignored (war story #20) — left at the chart default of 1 replica.
- [x] `k8s/` manifests packaged as `charts/nova` — a Helm chart, NOT Kustomize, because Helm
      was already in the stack (ESO, kube-prometheus-stack, Argo CD) and Kustomize would have
      been a fourth way to render YAML. Deliberately thin: only the 4 image refs templated.
- [x] `charts/nova/values.yaml` holds the image tags — the file CI writes in 4b. `git log
      --follow` on it is the deploy record.
- [x] `k8s/seed-job.yaml` deliberately EXCLUDED from the chart. `ttlSecondsAfterFinished: 3600`
      makes it self-delete hourly; Argo CD with selfHeal reads that as drift and recreates it,
      and the job opens with TRUNCATE. Self-deleting resources and auto-sync are incompatible.
- [x] `gitops/bootstrap/root-app.yaml` (app-of-apps) + `gitops/apps/nova.yaml`. No
      `resources-finalizer` on nova on purpose — it would make `delete application` cascade
      into deleting the Postgres StatefulSet.
- [x] verify: both Applications `Synced` / `Healthy`; adoption of the kubectl-created
      resources was clean, no OutOfSync
- [x] verify: **selfHeal** — `kubectl scale deploy/nova --replicas=3` reverted in **1 second**
- [x] verify: **git drives the cluster** — `replicas: 2` committed, deployed with no kubectl,
      ~1 minute after the push

**Two clocks, and they are not the same** (this was measured, not assumed):

| Drift | Detected by | Latency |
|---|---|---|
| Cluster edited by hand | Kubernetes **watch** on managed resources | ~1s |
| Git commit | **Poll**, `timeout.reconciliation` 180s | 0–3 min, ~1 min typical |

selfHeal never re-reads git — the controller already holds the rendered desired state, so a
watch event is enough. The 180s interval only governs noticing git changed. Webhooks (4b)
fix the second clock, not the first.

**Nova at 2 replicas stays correct only because of the Redis checkpointer** — session memory
lives outside the pod. On the in-process fallback, turn 2 would miss its history whenever it
landed on the other replica. `nova_memory_persistent` is the gauge standing between the
platform and that.

### 4b — CI build-and-deploy (~2h) — DONE 2026-08-27

- [x] `.github/workflows/build-and-deploy.yml` — path-filtered matrix builds `nova`,
      `mcp-servers` and `eval` via Cloud Build, `yq`-bumps the tag in
      `charts/nova/values.yaml`, commits. **CI holds no Kubernetes credential**; the
      commit is the deploy.
- [x] **Build and deploy are separate triggers.** `push` builds and stops; only a manual
      `workflow_dispatch` commits the tag. A tag commit starts a rollout that runs both
      gates and costs ~$0.27 in tokens, so auto-deploying every prompt tweak spends that.
      Building on push stays free and works with the node pool at zero.
- [x] **Tag derived from the commit** (`<commit-date>-<sha7>`), not from `date` at run
      time, plus a registry existence check before building. A deploy therefore reuses the
      image the push already built — build once, deploy that same artifact.
- [x] `terraform-gcp/cicd.tf` — `gha-cicd` SA and roles. Key minted out-of-band, NOT by
      Terraform (`google_service_account_key` would put it in HCP state in plaintext).
      Long-lived key is an accepted tradeoff; WIF migration path documented in the file.
- [x] verify: **full loop proven** — workflow built nova, committed the tag, Argo CD rolled
      the pods, no `kubectl`. Fixed the `tool_results` gap that broke faithfulness.
- [x] War stories #21–23 recorded (deployed-image drift, bucket-vs-object IAM, green build
      reported red)

### 4c — Argo Rollouts blue-green + both gates (~5h) — **DONE 2026-08-30**

**Cut, then reinstated the same day.** It was cut as the last unstarted heavy piece, with a
`git revert` gate proposed as a 1h substitute. Reinstated because the JD names it —
*"blue/green and canary rollout strategies for ML models"* — and because industry sources are
explicit that a bad model version reaching 100% of traffic is exactly what progressive
delivery exists to prevent. The revert substitute limits exposure; it does not prevent it.

**Why Argo Rollouts over Flagger:** Flagger is Flux-native and needs a service mesh for traffic
splitting. Rollouts is from the Argo ecosystem already installed and does blue-green without a
mesh. On a 2-node cluster with no Istio that is decisive. Running Argo CD **and** Argo Rollouts
together is the canonical pairing, not redundancy — CD syncs the `Rollout` manifest, Rollouts
decides promote or abort.

```
Rollout pauses
  |- prePromotionAnalysis   -> JOB provider: replay the golden set against nova-preview.
  |                            No traffic at risk. Fail = never promoted.
  |- promote                -> active Service switches. 100% traffic on the new version.
  \- postPromotionAnalysis  -> JOB fires 20 synthetic requests, then PROMETHEUS measures
                               error rate + p95. Fail = abort, active switches back.
```

- [x] `charts/nova/templates/analysis.yaml` — both AnalysisTemplates
- [x] `charts/nova/templates/nova.yaml` — `Deployment` → `Rollout`, `nova-preview` Service
- [x] `eval/Dockerfile`, `requirements.txt`, `requirements.lock` — the runner image
- [x] Argo Rollouts controller installed — chart **pinned 2.41.1 → v1.9.1**, cluster-scoped,
      2 replicas with leader election (only the lease holder reconciles; a second *install*
      would fight, two replicas of one Deployment do not)
- [x] Eval image built and its real tag committed over `REPLACE_AFTER_FIRST_BUILD`
- [x] verify: **promote path** — revision 4 passed both gates unattended and went
      `stable,active`. Post-promotion `✔ 7` = 1 synthetic-load + 3 error-rate + 3 latency.
- [x] verify: **abort path, POST-promotion** — revision 2 passed pre, failed post
      (`✔ 3, ✗ 2`), active Service switched back to revision 1 with no human involved
- [x] verify: **abort path, PRE-promotion** — revision 3's golden set failed, the new
      ReplicaSet never became active and **never took traffic**

**All three paths exercised on real runs**, which is the demo. Most progressive-delivery
write-ups only show the happy path.

**Two bugs the gates found, both in things we built rather than in Nova:**

1. **The error-rate query failed a rollout because Nova was perfect** (revision 2).
   `sum(rate(nova_requests_total{status!="ok"}[2m]))` over zero errors returns EMPTY, not 0 —
   a labelled counter does not exist until its first increment, so `{status!="ok"}` matched no
   series and `empty / anything = empty`. Fixed with `or vector(0)` on the numerator. The
   `status.md` Phase 3 note *"an absent series and a zero series mean different things"* was
   written about `/metrics` output three weeks earlier and bit inside a PromQL gate.
2. **`gs-003` failed faithfulness on `['AED currency assumption']`** (revision 3) — see **E5**.
   A real catch, and the source of intermittent gate flakiness.

**Measured, not assumed:** post-promotion p95 came back at **2.55s** against the 8s gate.
The golden set measures 3.7s because it mixes question shapes; the synthetic load repeats one
simple question, so it is faster. Two different numbers, both correct.

**Three things known in advance, so they are not mistaken for bugs:**

1. **A placeholder eval tag does not break the sync.** An `AnalysisTemplate` is a definition,
   not a workload — no pod, no image pull. It bites when the first `AnalysisRun` is created.
2. **Argo Rollouts skips analysis on the INITIAL rollout.** There is no previous version to
   promote from, so the first sync just deploys. Gates engage on the next image change.
3. **`scaleDownDelaySeconds: 600`.** Abort works by switching the active Service back to the
   old ReplicaSet. If it has already scaled down there is nothing to switch to and rollback
   becomes a cold redeploy.

**Post-promotion needs traffic that does not exist here**, hence the synthetic-load Job — 20
requests, ~$0.12 per rollout. Its `successCondition` is `len(result) > 0 && ...`, deliberately
NOT the defensive `len(result) == 0 || ...` idiom: we generate the traffic ourselves, so an
empty result means the load Job failed and must not read as success. With no traffic at all, a
post-promotion gate either always passes or always blocks — it tells you nothing about the code.

## Phase 5 — Evaluation Hub (~9.5h) — NEXT

**Designed 2026-08-25 against what the field actually does** — Langfuse, LangChain, Arize and
Promptfoo all converge on the same shape, and nothing here is bespoke.

The eval runs as a plain Kubernetes `Job`. It starts, scores, exits — and the exit code is the
verdict, which is all any consumer needs.

### The tie-up — one runner, three consumers

```
eval/golden/questions.yaml   (git = the dataset version)
            |
            v
      run_eval.py            (one runner, same code locally and in-cluster)
            |
  +---------+-------------------+-------------------+
  v         v                   v                   v
exit code  exit code       Langfuse scores     results JSON
  |         |                    |
  v         v                    v
GitHub   Argo Rollouts    per-case trend + one click to the trace
Actions  prePromotion
(merge   Analysis
 gate)   (promote gate)
```

Both gates read the **same signal** — a non-zero exit. That is why `run_eval.py` was written
to exit non-zero from the start, and why neither gate needs Prometheus.

### Metrics — six, three of them judged, ONE judge call

Renamed 2026-08-25 to the field's names — computation unchanged in all three cases:

| was | now | source |
|---|---|---|
| `groundedness` | `faithfulness` | RAGAS / LangChain |
| `tool_selection` | `tool_correctness` | DeepEval `ToolCorrectnessMetric` |
| `parameter_accuracy` | `argument_correctness` | DeepEval `ArgumentCorrectnessMetric` |

DeepEval judges argument correctness with an LLM and no reference; this does it as a
deterministic key-value compare, which is better HERE because the golden set already
declares the expected arguments — no reason to pay a model to decide `"2026-07-01" ==
"2026-07-01"`.

| Metric | Type | Catches what nothing else does |
|---|---|---|
| `tool_correctness` | code | routing broke |
| `argument_correctness` | code | argument extraction broke |
| `faithfulness` | judge | fabrication on top of correct data |
| `answer_relevance` | judge | **NEW** — every number right, wrong question answered |
| `answer_correctness` | judge, 3 cases | **NEW** — true but INCOMPLETE |
| `cost_per_request` + `latency_p95` | code | run-level budgets; quality flat, spend up |

The three judged metrics share ONE API call — three calls would triple judge spend and let
the judge contradict itself on the same answer.

**`answer_correctness` was challenged and survived, but narrowed 6 cases → 3.** The
objection is fair: faithfulness + relevance already cover most of it. Checked case by
case, 5 of the original 6 references were redundant — fabricated balances, invented cards,
and quoted exchange rates are all unsupported claims that faithfulness catches, and
inventing an unnamed account is caught by `tool_correctness` (expected `[]`). Even the
"wrong verdict from correct figures" case is caught, because RAGAS faithfulness scores
INFERENCE, not quoting: *"the account has exceeded its limit"* cannot be inferred from a
+185k balance against a 7,126.26 limit.

**The one gap faithfulness structurally cannot close is OMISSION.** It scores the claims
that are IN the answer and has no opinion about claims that should have been there and are
not. An answer listing 2 of a customer's 5 accounts is 100% faithful and 100% wrong, and
no judge prompt fixes it — the missing content isn't there to judge. That is why RAGAS
defines answer_correctness as *coverage* against a reference.

Surviving references: **gs-016** (a balance per account), **gs-004** (every category), and
**gs-008** (a two-part question where answering one half is the likely failure).

**Each reference states what must be COVERED, never what the values are** — which also
kills the maintenance objection. `"a balance for every account list_accounts returned"`
survives a reseed; `"8,200 AED on groceries"` does not. Writing a number into
`expect_answer` is a signal that faithfulness already has the case.

**Per-metric thresholds, not one number** (`THRESHOLDS` in `run_eval.py`). Code metrics are
exact comparisons so the floor is 1.0; judged metrics are opinions and a 0.9 from a judge is
a pass, not a near-miss. One global 0.99 would fail the suite on judge noise instead of on
agent regressions, and a gate that cries wolf gets switched off inside a week.

### Done 2026-08-25

- [x] `run_eval.py` rewritten — 6 metrics, one judge call, per-metric `THRESHOLDS`,
      run-level cost/latency budgets, Pushgateway push (env-gated, inert locally)
- [x] Golden set 12 → **18 cases**, shapes single 9 / refuse 4 / memory 3 / multi 2
- [x] `gs-002` fixed — moved `single` → `refuse`. ACC-00004 has zero cards, so it was never
      testing card fields; as a refuse case it guards inventing a card. gs-015 does the real
      card test.
- [x] **Tool-coverage hole found and closed:** `get_customer`, `find_transactions`, and
      `get_cards`-with-data had NO case at all — a regression in any of them would have
      shipped green. Now 8 of 9 tools covered.
- [x] `gs-018` added — "what's my balance?" with no account named anywhere. Guards the
      nastiest failure available: inventing a plausible account ID and answering
      confidently about someone else's money.
- [x] Staleness case was **already covered** by gs-010 — the earlier note claiming otherwise
      was stale.
- [x] `eval/golden/README-placeholders.sql` — resolves the values only the DB knows
- [x] **Placeholders resolved 2026-08-26** — `gs-014` search term `Talabat` (counterparty,
      7 rows), `gs-015` account `ACC-00002` (active credit card, non-NULL credit_limit)
- [x] Result rows carry `question` and `answer`, so a JSON reads without the fixture
- [x] **18/18 green 2026-08-26** — AGG tool 1.00 / args 1.00 / faith 1.00 / relev 1.00 /
      corr 1.00, $0.0060 per request, **p95 3742ms** (the measured LATENCY_BUDGET_MS)

### The suite was green and meaningless — four bugs found by making it honest

Every one of these PASSED before it was fixed. None would have failed loudly.

| what | why it passed | fix |
|---|---|---|
| `gs-002` cards | ACC-00004 has 0 cards — empty result is trivially faithful | moved to `refuse`; `gs-015` does the real card test |
| `gs-006/007/016` | **CUS-00012 has 0 accounts and 0 loans** — three cases scoring 1.00 against empty tool results | repointed to `CUS-00034` (5 accounts, 1 loan) |
| `gs-018` relevance 0.70 | judge marked down a clarifying question; relevance had no carve-out for unanswerable questions, though faithfulness always had one | added the carve-out to the judge prompt |
| `gs-016` tool 0.50 | expected `[list_accounts, check_balance]`, but `list_accounts` already returns balance per row — the fixture demanded the exact waste `tool_correctness` penalises | `expect_tools: [list_accounts]`, shape `multi` → `single` |

**A case whose data goes empty keeps PASSING — it does not fail.** That is the argument for
the preflight fixture check below, and it is a better interview answer than "I built an eval
suite": the suite was green three times while asserting nothing.

Coverage gap this leaves: `gs-008` is now the ONLY genuine `multi` case (single 10 / refuse 4 /
memory 3 / multi 1). Multi-tool sequencing is a real failure mode and one case is thin.

### Open

- [x] Placeholders resolved — `gs-014` → `Talabat`, `gs-015` → `ACC-00002`
- [x] Runner image — `eval/Dockerfile`, same `run_eval.py`, no fork. Fixture ships INSIDE the
      image so an image tag identifies one exact suite. `ENTRYPOINT` carries no `--target`,
      so the same image scores live Nova or a rollout's preview Service.
- [x] `eval/requirements.txt` + `.lock` — **pinned to `anthropic==0.122.0`**, the version the
      18/18 baseline ran on. `>=0.40` had resolved to `1.1.0`, a major bump onto a different
      HTTP stack (`httpx2`). Third time resolution picked something untested; see war story #10.
- [x] Two container-only bugs fixed before they shipped: `--set` now resolves against
      `__file__` (`eval/golden/...` only works from the repo root), and `--out-dir` was added
      because `/app` is root-owned while the image runs as uid 10001 — the write would have
      failed *after* a paid run completed.
- [x] `eval` added to the CI matrix — dir `eval` → image `nova-eval` → key `images.eval.tag`
- [ ] **Judge calibration** — hand-label the 18 cases once, measure agreement with Haiku,
      record the number in the README. Everyone says "I use LLM-as-a-judge"; almost nobody can
      answer *"how do you know your judge is right?"*, and it is the standard follow-up. Also
      settles the open question of whether Haiku grades `refuse` noisily, with data.
- [ ] verify: the eval runs as a `Job` in-cluster and reproduces the local 18/18

**Pushgateway and the scheduled CronJob are NOT needed for the gate** — decided 2026-08-27.
`prePromotionAnalysis` uses the Argo Rollouts **job provider**, which reads the exit code
directly, so scores never have to reach Prometheus for a deploy to be gated. Pushgateway
remains the right answer for *drift alerting on a schedule*, and moves to E6 with the CronJob
and the `PrometheusRule`. Dropped from the critical path, not from the plan.

- [ ] *(E6)* Pushgateway + `CronJob` scheduled replay + `PrometheusRule` on score thresholds
- [ ] *(E6)* Empty regression set scaffolded

### Phase 5.6 — Langfuse dataset runs (~1h) — AFTER Phase 5 is signed off

Deferred deliberately: finish the scoring and the gate first, then add the trend view.

Langfuse Datasets + dataset runs are the industry-standard answer to "is the system improving
or degrading" — the same shape as LangSmith Experiments and Braintrust Experiments. Not a
custom results store; nobody hand-rolls one.

- [ ] Push the 18 golden cases as a Langfuse Dataset (idempotent, safe to re-run)
- [ ] Each eval run becomes a dataset run; scores attach per item
- [ ] **Make Nova's `trace_id` BE the Langfuse trace ID.** Today `app.py:264` mints its own
      UUID and passes it as plain metadata, so pasting it into Langfuse's trace search
      returns nothing — it is findable only via a Metadata filter. `langfuse_session_id`
      works because that key IS predefined; `trace_id` is not. Fix: `uuid.uuid4().hex`
      (32 hex, no dashes) and `CallbackHandler(trace_context={"trace_id": ...})`, which
      means building the handler per request instead of once at startup (`app.py:196`).
      Cheap, but it changes the response contract, so it ships with the dataset-run work
      and one rebuild rather than on its own.
- [ ] verify: compare view with a baseline shows per-item green/red deltas on score, cost and
      latency; the Charts tab plots average score across runs

What this replaces: comparing runs by hand with `json.load` on files in the repo root, which is
how the 17 Aug vs 26 Aug faithfulness regression was actually diagnosed.

- [x] **Decided 2026-08-24: the judge stays `claude-haiku-4-5`.** Sonnet is 3x Haiku on both
      input and output (`eval/run_eval.py` `RATES`), and the baseline gets regenerated often
      during Phase 5. What matters is that the judge is **pinned**, not which model it is: the
      judge is part of what an eval run is versioned by, so scores are only comparable across
      runs judged by the same model. Revisit if Haiku grades the `refuse` shape noisily.
      Set at `charts/nova/templates/nova.yaml:47` — the chart, not `k8s/`, since Phase 4a.
      **Reconcile the docs:** README and IMPLEMENTATION-PLAN still say Sonnet.

## Phase 5.5 — absorbed into Phase 5 (was ~2h)

The CronJob and the PrometheusRule are two checkboxes above, not a phase. Once the runner
pushes to Pushgateway, "scheduled replay" is a `CronJob` wrapping the same image and
"alert on drift" is a `PrometheusRule` over `nova_eval_score` — neither needs its own build
step. Live-traffic scoring stays out; that is E2 and it is genuinely different work.

---

# ENHANCEMENTS (~5.5h)

## E5 — Return `currency` from every money-returning tool (~0.5h)

**Found by the gate on 2026-08-30, revision 3.** `gs-003` failed `faithfulness` with
`unsupported: ['AED currency assumption']` — the agent wrote "AED" from its own context
because `query_transactions` returned bare amounts. The judge was right: an amount with no
unit is incomplete data, and filling that gap by assumption is exactly the failure
faithfulness exists to catch. In a multi-currency bank it is a live bug.

`query_transactions` was fixed (joins `accounts` for `currency`). Four remain, and the API
is inconsistent as a result — two account tools already return currency, five money-returning
tools do not:

| tool | money field | currency? |
|---|---|---|
| `check_balance`, `list_accounts` | balance, overdraft | yes |
| `query_transactions` | amount | **fixed 2026-08-31** |
| `spending_by_category` | `SUM(amount)` | no — gs-004 |
| `find_transactions` | amount | no — gs-014 |
| `get_cards` | `credit_limit` | no — gs-015 |
| `get_loans` | principal, outstanding | no — gs-006 |
| `initiate_transfer` | balance | no — not eval-covered until Phase 2.5 |

- [x] **All eight money-returning tools fixed 2026-09-11.** `query_transactions`,
      `find_transactions`, `get_cards` join `accounts`; `spending_by_category` returns
      currency once at the top rather than per row (one account, one currency — repeating
      it per category is tokens the model pays for every turn); `initiate_transfer` carries
      it alongside `amount`, which matters most because that is the tool that moves money.
- [x] **Schema gap found and stated rather than hidden:** `loans` has no `currency` column
      AND no `account_id` — it references `customer_id` directly, so a loan's currency is
      recorded nowhere. `get_loans` infers it from the customer's accounts and returns
      **null when they disagree**. An absent unit beats a confidently wrong one — which is
      the entire lesson of this bug family. Real fix is `ALTER TABLE loans`.
- [ ] Rebuild `mcp-servers`, deploy, re-run the suite

**Why this matters more than a formatting nit: it is GATE FLAKINESS.** The agent states the
currency only sometimes — revision 4 passed the identical 18 cases that revision 3 failed. So
four cases can intermittently block a good deploy. Fixing the judge instead would have been
the wrong move: it would teach the metric to tolerate the exact class of unsupported claim it
exists to find. **A gate that blocks good deploys at an unknown rate is a gate that gets
`|| true`'d within a month** — this is the top item to close before trusting it.

## E4 — Hardening (~5h)

- [ ] Break it at all five stages, document each diagnosis path
- [ ] Verify teardown commands
- [ ] Final README pass

---

# PARKED — drift monitoring + model registry (~14h)

Grouped 2026-08-28. These are the MLflow / drift-tracking items, pulled out of the
enhancement list so the remaining core work is not read alongside them.

Not deleted, because two things here are directly asked for in the JD — *"model registries,
experiment tracking"* and *"observability for ... system health"* — and because the
infrastructure they need already exists in this cluster. If they get built in a separate
project instead, that project starts by rebuilding Postgres, GCS, Prometheus and a serving
layer that are already running here. Worth weighing before starting fresh.

## P-E6 — Scheduled drift detection (~2h)

*Split out of Phase 5 on 2026-08-27 once the gate stopped needing Prometheus.* The job
provider gates deploys on an exit code; this is the separate question of "did quality drift
while nobody deployed anything". **Closest to done — everything it needs already exists.**

- [ ] Pushgateway (`prometheus-pushgateway.enabled: true` on kube-prometheus-stack) — a Job
      dies before Prometheus scrapes it, so it posts its scores to a permanent target instead
- [ ] `CronJob` running the eval image nightly against live Nova (~$0.15/run, ~$4.50/month)
- [ ] `PrometheusRule` alerting on `nova_eval_score` thresholds
- [ ] Empty regression set scaffolded — failures found here get promoted into it

## P-E2 — Drift Tier 2, live-traffic scoring (~4h)

Depends on P-E6's regression set existing.

- [ ] Sample production traces, score `faithfulness` + `answer_relevance`
      (both reference-free — that is exactly why live traffic can be scored at all)
- [ ] Track score distribution over time
- [ ] Promote failures into the regression set
- [ ] verify: a question shape absent from the golden set appears in sampled scores and lands
      in the regression set

## P-E3 — Model registry + cheap router (~8h)

*Model distillation — a small "student" model trained on the LLM "teacher's" past decisions.
This is where MLflow lives: the registry and experiment tracking only become real once there
is a trained artifact to register, which is why it was never in the core build.*

- [ ] Export eval-passing traces → `(question, tool_chosen)` training set
- [ ] In-cluster training Job (PyTorch / HF Transformers)
- [ ] MLflow + Postgres backend + GCS artifacts (registry + experiment tracking)
- [ ] Nova routes via classifier above a confidence threshold, falls back to Claude
- [ ] verify: promotes only if `tool_correctness` ≥ Claude baseline; record cost-per-request delta

## Notes

- **Metrics:** six — `tool_correctness`, `argument_correctness`, `faithfulness`,
  `answer_relevance`, `answer_correctness` (conditional), `cost_per_request`, plus a
  run-level `latency_p95`. `groundedness` was renamed to `faithfulness` on 2026-08-25;
  the computation never changed.
- **Models:** `claude-haiku-4-5` for the agent, `claude-haiku-4-5` for the LLM judge (decided 2026-08-24 — cost; revisit if grading proves noisy). Pin the
  judge — an upgrade changes scores with no change to the agent.
- **Secrets — accepted tradeoff.** Terraform generates the Postgres password with
  `random_password`, so it sits in HCP state in plaintext (twice: `result` and `secret_data`).
  Chosen for apply-completeness — `terraform apply` alone leaves a working system. Fine on a
  personal workspace; on a shared one, "anyone who can read state" is a much wider set than
  "anyone who should know the DB password". Two tiers above this, worth being able to name:
  inject the value out-of-band so state never holds it, or have no static password at all
  (Cloud SQL IAM auth / Vault dynamic credentials — ruled out by the in-cluster Postgres
  cost decision).
- **Rotation:** add a new Secret Manager version (console or CLI), then
  `kubectl rollout restart statefulset/postgres -n nova`. ESO refreshes hourly, but Postgres
  reads the password once at startup — a rotation nothing restarts is a silent no-op.
- **Between sessions:** scale the node pool to zero, don't destroy.
  ```bash
  gcloud container clusters resize mlops-lifecycle --node-pool=primary --num-nodes=0 \
    --zone=us-west1-b --project=mlops-lifecycle-p7-gke --quiet
  ```
- **Budget:** no GPU. ~$0.29–0.38/hr node time + ~$0.17/eval run. $10 ceiling.
- **GCP project stays `mlops-lifecycle-p7-gke`** — project IDs are immutable; the repo rename
  doesn't touch it. Same for the HCP workspace `mlops-model-lifecycle-gcp`.
- **Total:** ~62.5h work. Core build alone is ~45.5h and is a demonstrable milestone on
  its own — 29h of it is done (64%), leaving ~16.5h: Phase 2.5 (2h), Phase 4b (5h),
  Phase 5 (9.5h).
