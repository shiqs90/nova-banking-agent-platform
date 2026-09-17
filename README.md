# Nova: Banking Agent Platform

**A delivery platform for a banking agent, where the evaluation suite decides what ships.**

Video walkthrough: [youtu.be/bgJ9z-MRmNw](https://youtu.be/bgJ9z-MRmNw)

Nova is a banking assistant that answers customer questions by calling tools. This repo is the
platform around it (connectors, memory, tracing, evaluation, and GitOps delivery), built so
that **no change to Nova reaches production without proving it didn't make the answers worse.**

For a normal web service the deploy gate is "does it return HTTP 200?" An LLM returns 200 all
day while quietly being wrong: a prompt tweak makes it pick the wrong tool, a model swap makes
it invent numbers the tool never returned. Nothing in a health check, a status code, or a
latency graph catches that. So the evaluation *is* the health check.

---

## What it does

```
Customer: "Did my salary land this month, and am I over my overdraft limit?"

Nova:  → query_transactions(account_id=ACC-2291, month=2026-03)
       → check_balance(account_id=ACC-2291)
       → "Your salary of AED 18,400 credited on 25 March. Your balance is
          AED -1,240 against an overdraft limit of AED 5,000, so you're within it."
```

```
Customer: "And last month?"

Nova:  → resolves "last month" from session memory, re-queries, answers
```

```
Customer: "Transfer AED 2,000 to my savings."

Nova:  → pauses for human approval before executing the write
```

## The loop this project builds

```
1. You change something          prompt, model, tool description, connector
2. Push                          CI builds the image, tags it by commit. Stops there.
3. Dispatch                      CI commits the tag to charts/nova/values.yaml
4. Argo CD syncs                 sees the commit, patches the Rollout. Never touches
                                 a pod itself; holds no CI credential either way.
5. Argo Rollouts, pre-promotion  new pods behind a PREVIEW Service, no traffic.
                                 A Job replays 18 golden cases against it: six
                                 scores, one judge call per case.
   Fail → new pods scaled down, active Service never moved, nothing shipped
6. Promote                       active Service selector flips to the new hash
7. Argo Rollouts, post-promotion 20 synthetic requests, then Prometheus: error
                                 rate < 5%, p95 < 8s, three samples each
   Fail → selector flips back to the old ReplicaSet, kept alive for 10 min
```

Every path in that diagram has run for real. The rollout history holds a pre-promotion
abort (revision 3), a post-promotion rollback (revision 2), and clean promotions.

Separately from any deploy: the one tool that moves money, `initiate_transfer`, halts for
a human. `/chat` returns `pending_approval` with the exact call; `/approve` resumes it; a
second `/approve` is refused so it cannot run twice. Verified on balances, not on logs.

---

## What it covers:

| Capability | How it's covered here |
|---|---|
| **Agentic systems** | LangChain `create_agent`, multi-step tool loop, four runaway limits |
| **Tool calling** | Nine tools across three connectors, incl. one consequential write |
| **Enterprise connectors / MCP** | Three real MCP servers, consumed via LangChain's MCP adapter |
| **Agent memory** | LangGraph checkpointer, Redis-backed; also what makes HITL resume-by-session work |
| **Evaluation** | 18-case golden set replayed as a Kubernetes `Job`, the pre-promotion gate |
| **LLM-as-judge** | `claude-haiku-4-5`, pinned; faithfulness + relevance reference-free, one call per case |
| **Progressive delivery** | Argo Rollouts blue-green; pre-promotion `job` provider, post-promotion `prometheus` provider; all three outcomes exercised |
| **GitOps** | Argo CD pull-based reconciliation; CI never holds a cluster credential |
| **CI/CD** | GitHub Actions: build on push, deploy on dispatch; commit-derived tags; build once, deploy the same artifact |
| **Kubernetes** | Rollout + AnalysisTemplate CRDs (consumed, not authored), StatefulSets, Jobs, RBAC, scoped ServiceAccounts |
| **Observability** | Langfuse traces + Prometheus metrics, joined by one `trace_id`; 6 alert rules |
| **Security** | PII redaction on tool results; human approval on the write tool; least-privilege node SA; Workload Identity |
| **IaC** | Terraform on GKE via HCP Terraform |

**Scoped but not built**, listed so the gap is stated, not discovered:

| Capability | State |
|---|---|
| **Drift monitoring** | Pushgateway + nightly `CronJob` + `PrometheusRule` on score thresholds. Everything it needs exists; ~2h. |
| **Model registry / training** | MLflow + a distilled router classifier. There is no trained artifact yet, so a registry would be theatre. |
| **Regression set** | Append-only never-again set. Empty until drift monitoring produces its first failure. |
| **Cost gate** | `run_eval.py` supports a per-request budget (`NOVA_EVAL_COST_BUDGET_USD`); not yet wired into the Rollout gate. |

There is no `EvaluationRun` CRD or custom controller. One was designed and rejected: the eval
runs as a plain `Job`, its exit code is the verdict, and two control planes already consume
that: CI and Argo Rollouts. A third would have had no consumers of its own.

## Architecture

![Nova on GKE](docs/diagrams/nova-gke.png)

One customer question, numbered 1 to 9: in through the GKE control plane, context from Redis,
the model decides a tool, the MCP connectors query Postgres, the answer goes back, the trace goes
out. Prometheus scrapes `/metrics`; secrets reach the pods through Workload Identity with no
static credential on the path.

The diagram is drawn by [docs/diagrams/nova-gke.py](docs/diagrams/nova-gke.py) from what
`terraform-gcp/` and `charts/nova/` actually provision. Design notes and tradeoffs are in
[docs/architecture-gke.md](docs/architecture-gke.md).

## Tracing
![Langfuse tracing](docs/diagrams/langfuse-tracing.png)

## Guardrails

Both are LangChain's [built-in guardrail middleware](https://docs.langchain.com/oss/python/langchain/guardrails),
attached to `create_agent` in `nova/app.py`. Neither is decorative in a banking agent: one
keeps customer data out of the model, the other keeps the model away from the money.

### PII detection

```python
PIIMiddleware("email", strategy="redact", apply_to_tool_results=True)
PIIMiddleware("phone", strategy="mask", detector=r"\+\d{9,15}", apply_to_tool_results=True)
```

| Type | Detector | Strategy | Result in the prompt |
|---|---|---|---|
| `email` | built in | `redact` | `[REDACTED_EMAIL]` |
| `phone` | custom regex, E.164 as seeded (`+971...`) | `mask` | last digits kept, rest masked |

`PIIMiddleware` decides *which messages get scanned*, not just what counts as PII. Three
independent switches, and the defaults are not the interesting ones:

| Flag | Default | What it scans | When it runs |
|---|---|---|---|
| `apply_to_input` | `True` | `HumanMessage`, what the customer typed | `before_model` |
| `apply_to_output` | `False` | `AIMessage`, model text and tool-call arguments | `after_model` |
| `apply_to_tool_results` | `False` | `ToolMessage`, what the MCP tools returned | `before_model`, on messages after the last `AIMessage` |

Nova sets `apply_to_tool_results=True` and leaves the rest at their defaults, so **input and
tool results are both scanned; model output is not.**

`apply_to_tool_results` is the one that earns its keep. The PII in this system arrives *from*
`get_customer` (`full_name`, `email`, `phone`), none of which the agent needs to answer a
balance or transaction question. Scrubbing the `ToolMessage` before the model sees it means the
address and number never reach the prompt, the Langfuse trace, or the checkpointed session in
Redis. The database still holds the real values; the boundary being protected is the prompt
leaving the cluster, not the store.

`apply_to_input` is inherited rather than chosen. It costs nothing on the golden set: every
case keys on an account ID like `ACC-00004`, which neither detector can match. But it does
mean a customer who types their own email or number hands a placeholder to any tool expecting
one. No current MCP tool takes either as an argument; revisit if one does.

The phone detector is anchored on the leading `+`, which is what keeps it safe: balances,
dates, and account IDs contain digits too, and none of them start with a plus sign.

Not covered: `full_name`. There is no reliable regex for arbitrary names, and a detector that
misses half of them is worse than not claiming one. That needs an NER model, which is out of
scope.

### Human-in-the-loop

```python
HumanInTheLoopMiddleware(interrupt_on={"initiate_transfer": True})
```

Per tool. Only the write pauses; every read tool runs untouched, which is why the golden set
is unaffected. When the model decides to call `initiate_transfer`, the middleware interrupts
the graph *before* the tool node runs. Nothing has executed.

Nova is request/response with no UI, so approval is a second HTTP call rather than a button:

| Step | Call | What happens |
|---|---|---|
| 1 | `POST /chat` "Transfer 500 from ACC-00004 to ACC-00002" | returns `status: pending_approval` with the exact tool call and its arguments. Balances unchanged. |
| 2 | `POST /approve {session_id, decision: approve}` | graph resumes, tool executes, model writes the confirmation. Balances moved. |
| 2' | `POST /approve {..., decision: reject, reason}` | tool skipped; the model is told why and answers the customer instead of erroring |
| 3 | `POST /approve` again | refused with `nothing_pending`. A transfer cannot run twice because someone clicked twice. |

`session_id` is the resume token. LangGraph keyed the paused state on `thread_id` in Redis, so
there is nothing extra to store or expire. The approver supplies a *decision*, not arguments;
the amount and accounts were fixed when the graph paused and cannot be changed on the way back
in. (`edit` and `respond` exist as decision types for exactly that, and are unused here.)

Verified on balances: ACC-00004 `185,254.95 → 184,754.95`, ACC-00002 `242,477.92 → 242,977.92`,
with a query between step 1 and step 2 showing both unchanged.

## The two gates

| Gate | Provider | Against | Signal | Catches |
|---|---|---|---|---|
| **Pre-promotion** | Argo Rollouts `job` | **preview** Service, no traffic | eval `Job` exit code | routing, argument, faithfulness, relevance or coverage regression, before any user is exposed |
| **Post-promotion** | Argo Rollouts `prometheus` | **active** Service, live | error rate < 5%, p95 < 8s, 3 samples each | what only shows under real requests |

Pre-promotion **prevents** exposure: a failure means the new pods are scaled down and the
active Service never moved. Post-promotion **reacts** to it: a failure flips the selector back
to the old ReplicaSet, which `scaleDownDelaySeconds: 600` keeps alive for exactly this.

The post-promotion gate has to generate its own traffic (20 synthetic requests before the
first Prometheus sample), because a gate that measures live requests on a platform with none
either always passes or always blocks. Its error-rate query also floors the numerator with
`or vector(0)`: a labelled Prometheus counter doesn't exist until its first increment, so a
100% success rate returns *empty*, not zero, and the first version of this gate rolled back a
deploy for being perfect.

Two verifications that matter more than the green ones: **revision 3** failed pre-promotion
and never took traffic; **revision 2** failed post-promotion and was rolled back with nobody
watching.

## The six metrics, and why they're separate

Names are the field's names, not house style: `tool_correctness` and
`argument_correctness` are [DeepEval](https://deepeval.com/docs/metrics-argument-correctness)'s,
`faithfulness` and `answer_correctness` are RAGAS/LangChain's. Being understood costs
nothing.

| Metric | Computed by | Needs a reference answer? |
|---|---|---|
| `tool_correctness` | Deterministic set compare | one-word annotation |
| `argument_correctness` | Deterministic key-value compare | one-line annotation |
| `faithfulness` | LLM judge: is every claim supported by the tool result? | **no, reference-free** |
| `answer_relevance` | LLM judge: does it answer the question asked? | **no** |
| `answer_correctness` | LLM judge: is anything required missing? | yes, 3 of 18 cases |
| `cost_per_request`, `latency_p95` | Tokens × model rate; run-level | no |

The three judged metrics come from **one** API call. Three calls would triple judge
spend and let the judge contradict itself on the same answer.

One blended score tells you *something* broke. Six tell you *where*:

| tool | args | faith | relev | corr | cost | Diagnosis |
|---|---|---|---|---|---|---|
| **↓** | ok | ↓ | ok | ok | ok | Routing regressed: prompt or tool descriptions |
| ok | **↓** | ↓ | ok | ok | ok | Routing fine, argument extraction broke |
| ok | ok | **↓** | ok | ok | ok | Right data fetched, model fabricated on top of it |
| ok | ok | ok | **↓** | ok | ok | Answering a different question, correctly |
| ok | ok | ok | ok | **↓** | ok | True but incomplete, the failure faithfulness cannot see |
| ok | ok | ok | ok | ok | **↑** | Quality held, agent is looping or over-calling |

**Why `answer_correctness` is not redundant.** The obvious objection is that faithfulness
plus relevance already cover it, and for most cases they do. That was checked case by
case, and references were cut from 6 cases to 3 as a result. The one gap neither can
close is **omission**: faithfulness scores the claims that are *in* the answer and has no
opinion about claims that should have been there and are not. An answer listing 2 of a
customer's 5 accounts is 100% faithful and 100% wrong. No judge prompt fixes that,
because the missing content isn't there to judge, which is why RAGAS defines
answer_correctness as *coverage* against a reference.

Each surviving reference therefore states **what must be covered**, never what the values
are. `"a balance for every account list_accounts returned"` survives a reseed;
`"8,200 AED on groceries"` is wrong the next time `db/seed.py` runs.

`faithfulness` and `answer_relevance` being reference-free is what makes drift monitoring
on live traffic possible: you can score a real customer question with no expected answer
to compare against. `answer_correctness` cannot go there, by construction.

## Golden set vs regression set

| | Golden set | Regression set |
|---|---|---|
| Purpose | Coverage | Never-again |
| Size at start | ~18 | 0 |
| Grows | Deliberately, with new capability | Automatically, from every bug found |
| Source | Generated from tool schemas, curated by hand | Failed drift runs and incident postmortems |

Neither is hand-authored as question/answer pairs, and **no case asserts a balance.** A
banking agent reads mutable data, so a stored *"the balance is 185,254.95"* would measure
how recently someone refreshed the fixture, not how well the agent works. Cases assert
structure (right tool, right arguments, every returned row covered) while the judged
metrics score against what the tool returned on that run.

`expect_answer` is on **3 of 18 cases**. It started on 6; each was checked against *"would
faithfulness catch this anyway?"* and 5 failed that test: fabricated balances, invented
cards, and quoted exchange rates are all unsupported claims, and inventing an unnamed
account is caught by `tool_correctness` expecting `[]`. Even a wrong verdict built from
real figures is caught, because faithfulness scores **inference**, not quoting.

The survivors describe **what must be covered**, never what the values are: *"a balance
for every account `list_accounts` returned"*. Coverage references survive a reseed; value
references don't. A number in `expect_answer` is a signal that faithfulness already has
the case.

One coupling to know about: `gs-002` is a `refuse` case premised on `ACC-00004` having no
cards. A reseed that gives it one breaks the case silently; re-check with
`eval/golden/README-placeholders.sql`.

## Cost

No GPU at any point.

| | |
|---|---|
| GKE node pool | ~$0.29-0.38/hr; **scale to zero between sessions** |
| GKE control plane | Free tier (zonal cluster) |
| Claude API per 18-question eval run | ~$0.15: `haiku-4-5` agent ~$0.11 (21 turns; 3 cases have a `setup`) + `haiku-4-5` judge ~$0.05 (one call per case) |
| **Per deploy**, both gates | ~$0.27: the eval run above + 20 synthetic post-promotion requests. This is why deploy is a manual dispatch, not automatic on push. |

```bash
# Scale down between sessions. The cluster, Argo CD, PVCs and seeded data all survive;
# only the node VMs are removed, so this is ~$1/month idle instead of ~$0.30/hr.
gcloud container clusters resize mlops-lifecycle --node-pool=primary --num-nodes=0 \
  --zone=us-west1-b --project=mlops-lifecycle-p7-gke --quiet
```
