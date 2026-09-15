# P7 — Troubleshooting

Every failure hit building Nova. Each entry is **Issue → How I found it → How I fixed it**,
with the actual commands.

Two themes run through these. The first is familiar: the symptom rarely pointed at the cause.
The second is specific to this build — **four failures were dependency resolution, and three
were instruments lying about a healthy system.** The second kind is worse: a wrong metric sends
you debugging a layer that was never broken.

| # | Symptom | Actual cause |
|---|---|---|
| 1 | console shows no clusters, `gcloud` sees them fine | browser and CLI on different Google accounts; org filter hides org-less projects |
| 2 | `no matches for kind "SecretStore"` (anticipated) | ESO CRDs registered `v1`; every example online shows `v1beta1` |
| 3 | savings account at −163,932 with a 0 overdraft limit | transactions assigned to random accounts regardless of account type |
| 4 | `ModuleNotFoundError: No module named 'mcp.shared.session'` | `mcp` unpinned → resolver chose a version predating `ProgressFnT` |
| 5 | `unknown command 'FT.INFO'` | checkpointer needs RediSearch; manifest used plain `redis:7-alpine` |
| 6 | every `/chat` returns `{"error":"error","detail":""}` | `RedisSaver` is sync-only; `ainvoke()` needs `AsyncRedisSaver` |
| 7 | `'super' object has no attribute 'dumps'` | `langgraph-checkpoint-redis` pinned to 0.1.x against langgraph 1.x |
| 8 | `tool_calls` grows 1, 2, 3, 4 across one session | `result["messages"]` is the whole thread, not this turn |
| 9 | repeat question sometimes skips the tool entirely | prompt never said "call a tool every time"; model reused a prior result |
| 10 | `uv pip install` populated the wrong project | `uv` honours `$VIRTUAL_ENV` over the `.venv` it just created |
| 11 | `Error: Argument definition required` on `terraform apply` | HCL forbids a nested block inside a single-line block |
| 12 | eval scores a working agent at 0.17 | judge was shown tool *calls*, not tool *results* |
| 13 | `docker build` → `failed to connect to the docker API` | Docker Desktop not running; Cloud Build is the escape hatch |
| 14 | `kubectl delete job` → NotFound; `set env` → immutable | `ttlSecondsAfterFinished` reaped it; Job pod templates are immutable |
| 15 | `kubectl` hangs, `dial tcp ...:443: i/o timeout` | home IP rotated; control-plane allowlist held the old one |
| 16 | `applicationSet.enabled: false` set, pod runs anyway | key removed in chart 10.x; Helm stores unknown values and ignores them |
| 17 | faithfulness 0.00 on every case, judge flags true claims | deployed image predates `tool_results` by 2.5h; eval only ever ran against local source |
| 18 | Cloud Build: "forbidden from accessing the bucket", blames `serviceusage` | `storage.objectAdmin` covers objects, not `buckets.get` |
| 19 | build succeeds, image pushed, gcloud exits 1 | SA can't stream the default logs bucket; needs project `roles/viewer` |
| 20 | golden set 18/18 three runs in a row | four cases asserted nothing — empty cards, customer with 0 accounts |
| 21 | lock resolves `anthropic==1.1.0`, venv runs 0.122.0 | `>=0.40` took newest; a major bump onto `httpx2` |
| 22 | (caught before running) runner would fail after a paid eval | `--set` path relative to CWD; `/app` root-owned under uid 10001 |
| 23 | `network is unreachable` on the same IP as #15 | laptop had no internet; not the allowlist |
| 24 | two `argo-rollouts` controller pods | chart default `replicas: 2` with leader election, not a second install |
| 25 | post-promotion gate aborts a rollout; Nova had zero errors | `sum()` over an absent counter series is empty, not 0 |
| 26 | faithfulness fails on `AED 782.99` — the number IS in the tool result | tools returned amounts with no currency; 5 of 9 tools, plus a schema gap in `loans` |
| 27 | six CI edits, validator says `parses OK`, nothing changed | heredoc `"""` collided with a trailing `"`; script died before the first replace |
| 28 | `/approve` → `list indices must be integers or slices, not str` | resume payload must be `{"decisions": [...]}`; sent a bare list |

---

## 1. GKE: console shows no clusters, `gcloud` sees them

### Issue

The Kubernetes Engine page showed an empty cluster list and a "create your first cluster"
splash. The cluster demonstrably existed — Terraform had applied it.

### How I found it

Read the URL in the browser rather than the page:

```
console.cloud.google.com/kubernetes/list/overview?project=mlops-model-lifecycle-p7
```

Then listed what the CLI could actually see:

```bash
gcloud config list
# account = shikha2531@gmail.com
# project = mlops-lifecycle-p7-gke

gcloud projects list
# mlops-lifecycle-p7-gke   P7 MLOps Model Lifecycle   166750278291
```

`mlops-model-lifecycle-p7` does not appear in that list — so the **browser was signed in as a
different Google account** (`shiqs90@gmail.com`), showing a different project whose display
name was near-identical.

A second cause stacked on top: even on the right account, the project picker was filtered to
`shikha2531-org`, and this project has **no organization**. Org-less projects are hidden by
that filter entirely.

### How I fixed it

Bypass the picker with an explicit project in the URL:

```
console.cloud.google.com/kubernetes/list/overview?project=mlops-lifecycle-p7-gke
```

and switch the picker's org dropdown to **No organization**, or use the **All** tab.

**Rule:** when the console and the CLI disagree, trust the CLI and check the identity on both.
`gcloud projects list` settles existence in one command; the console cannot, because it
silently filters.

---

## 2. External Secrets: CRDs registered `v1`, not `v1beta1`

### Issue

Every ESO example — docs, blog posts, GitHub issues — shows
`apiVersion: external-secrets.io/v1beta1`. Applying that would have failed with
`no matches for kind "SecretStore"`.

### How I found it

Checked before applying rather than after:

```bash
kubectl api-resources | grep external-secrets
# secretstores      ss   external-secrets.io/v1   true   SecretStore
# externalsecrets   es   external-secrets.io/v1   true   ExternalSecret
```

The project graduated the API to `v1` around its 1.0 release. The chart installs whichever
CRDs it ships with, and **the CRDs decide what the cluster accepts — not the documentation.**

### How I fixed it

Edited both `apiVersion` lines in `k8s/external-secrets.yaml` to `external-secrets.io/v1`.

A related ordering trap in the same install: ESO's webhook validates `SecretStore` and
`ExternalSecret` on admission, and the cert-controller must issue the webhook's serving
certificate before it passes readiness. Applying too early fails with a webhook connection
error that reads like a config problem.

```bash
kubectl wait --for=condition=Ready pod --all -n external-secrets --timeout=180s
```

---

## 3. Seed data: accounts holding balances their type cannot hold

### Issue

A spot check of the seeded data:

```
 account_id | account_type |  balance  | overdraft_limit | status
 ACC-00003  | savings      | -33477.30 |            0.00 | active
```

A savings account overdrawn against a zero overdraft limit. Impossible in real banking, and
`check_balance` would return `exceeds_overdraft: true` for it — so Nova would tell a customer
their savings account was overdrawn past a limit of zero.

### How I found it

Quantified it by account type rather than assuming it was a one-off:

```bash
kubectl exec -n nova postgres-0 -- psql -U nova -d nova -c "
  SELECT account_type, count(*) AS total,
         count(*) FILTER (WHERE balance < -overdraft_limit) AS impossible,
         min(balance) AS worst
  FROM accounts GROUP BY account_type ORDER BY account_type;"

#  account_type  | total | impossible |   worst
#  current       |  1656 |        206 | -143207.12
#  fixed_deposit |   292 |         39 | -163932.81
#  savings       |  1052 |        152 | -142552.64
```

397 of 3,000 accounts — 13%. Cause: the generator assigned transactions to random accounts with
no regard for account type. The average account drifts positive (salary credits are large, most
debits small) but the variance is wide, and the unlucky tail goes negative regardless of type.

### How I fixed it

**Not by clamping the balance.** That was the obvious fix and it was wrong: it would break
`balance == sum(transactions)`, and Nova could then answer *"your balance is 0"* and *"your
transactions sum to −33,477"* in the same conversation. The groundedness judge would mark a
correctly-behaving model wrong — a false signal sending you to debug the model instead of the
data.

Instead, emit the deposit a real account would have opened with:

```python
for account_id, _cust, acct_type, _ccy, _bal, overdraft, _status, opened_at, _run in accounts:
    floor = -overdraft if acct_type == "current" else Decimal(0)
    if net[account_id] >= floor:
        continue                      # already valid — leave it alone
    deposit = (floor - net[account_id]) + money(rng, 1_000, 80_000)
    # ... write an `opening_balance` credit dated at opened_at
```

Balance stays derived from transactions, so reconciliation holds. Current accounts legitimately
overdrawn *within* their limit are untouched — those are the interesting cases for overdraft
questions in the golden set.

After re-seeding: `impossible` = 0 across all three types, and 0 balance mismatches.

**Rule:** synthetic data needs domain invariants, not just plausible distributions. The
invariant here was *"balance ≥ −overdraft_limit, and non-current accounts have no overdraft"* —
nothing enforced it until it was written down.

---

## 4. Nova: `ModuleNotFoundError: No module named 'mcp.shared.session'`

### Issue

Crashloop at import, before the app ever started:

```
File ".../langchain_mcp_adapters/callbacks.py", line 7, in <module>
    from mcp.shared.session import ProgressFnT as MCPProgressFnT
ModuleNotFoundError: No module named 'mcp.shared.session'
```

### How I found it

`mcp` was never pinned in `nova/requirements.txt` — only `langchain-mcp-adapters==0.1.*` was.
pip resolved `mcp` to the oldest version that adapter's floor permitted, which predates
`ProgressFnT`.

The tell is that the failing import is *inside a dependency*, not in application code. That
almost always means a version mismatch rather than a bug.

### How I fixed it

Pin it explicitly instead of leaving it to transitive resolution:

```
langchain-mcp-adapters>=0.1.9,<1
mcp>=1.9,<2
```

**Rule:** an unpinned transitive dependency turns a resolver decision into a runtime crash.
Pinned, an incompatible pair fails at *build* time, in CI, where it reads as a dependency
problem instead of an application bug.

---

## 5. Redis: `unknown command 'FT.INFO'`

### Issue

Nova started cleanly, loaded all nine tools, then logged:

```
WARNING redis unavailable (Error while fetching checkpoints index info:
unknown command 'FT.INFO') — session memory is in-process only
```

Pod healthy. Requests worked. Session memory silently dying on every restart.

### How I found it

`FT.INFO` is a **RediSearch** command, not core Redis. `langgraph-checkpoint-redis` indexes
checkpoints with RediSearch; the manifest used `redis:7-alpine`, which ships no modules.

```bash
kubectl logs -n nova deploy/nova | grep -i "session memory"
```

### How I fixed it

```yaml
image: redis/redis-stack-server:latest
env:
  - {name: REDIS_ARGS, value: "--appendonly yes"}   # config via env, not argv
```

```bash
kubectl delete statefulset redis -n nova --cascade=foreground
kubectl apply -f k8s/redis.yaml
kubectl rollout restart deploy/nova -n nova
# INFO session memory: redis at redis://redis.nova.svc.cluster.local:6379
```

**The instructive part is the failure mode, not the fix.** The code falls back to
`InMemorySaver` on any checkpointer error — the right call, since a memory store shouldn't take
the service down. But that turned a hard failure into a WARNING plus a healthy pod: working
memory in a one-pod demo, silent data loss on restart or scale-out.

**Rule:** a degraded-mode fallback needs a signal louder than a log line. `/healthz` now reports
`memory_backend: redis | in-memory (DEGRADED)`.

---

## 6. Nova: every request returns `{"error": "error", "detail": ""}`

### Issue

Every `/chat` call failed with an error carrying **no message at all**.

### How I found it

Two attempts to read the pod log returned nothing — the requests had scrolled past the tail
window under readiness-probe noise (a 10s probe writes ~6 lines/minute, so `--tail=200` covers
only ~30 minutes). Filtering the probe out was what made it visible:

```bash
kubectl logs -n nova deploy/nova --tail=400 | grep -v "GET /healthz" | tail -60

#   File ".../langgraph/checkpoint/base/__init__.py", line 441, in aget_tuple
#     raise NotImplementedError
# NotImplementedError
```

`RedisSaver` implements the **synchronous** checkpointer interface. Nova calls
`agent.ainvoke()`, so LangGraph reaches for `aget_tuple` / `aput` — unimplemented on the base
class.

**This bug was masked by #5.** While the checkpointer was falling back to `InMemorySaver`
(which *does* implement the async methods), requests worked. Fixing Redis is what exposed it.

### How I fixed it

```python
from langgraph.checkpoint.redis.aio import AsyncRedisSaver

checkpointer_cm = AsyncRedisSaver.from_conn_string(redis_url)
checkpointer = await checkpointer_cm.__aenter__()
await checkpointer.asetup()
```

And the empty `detail` was its own bug — `str(NotImplementedError())` is the empty string:

```python
"detail": f"{type(exc).__name__}: {exc}"[:500]
```

**Two rules.** A fallback that is never exercised is a second untested code path, not a safety
net — the real path's bugs sit undiscovered until the day it engages. And always include the
exception *type* in an error response; several common exceptions carry no message.

---

## 7. Nova: `AttributeError: 'super' object has no attribute 'dumps'`

### Issue

With the error-detail fix in place, the real message finally surfaced in the API response
rather than only the logs:

```json
{"trace_id": "...", "error": "error",
 "detail": "AttributeError: 'super' object has no attribute 'dumps'"}
```

### How I found it

A serializer in `langgraph-checkpoint-redis` calls `super().dumps()`, which `langgraph`'s
checkpoint base class no longer defines. Rather than guess a version again, I resolved it:

```bash
uv pip install --dry-run "langgraph==1.*" "langgraph-checkpoint-redis"
# + langgraph==1.2.11
# + langgraph-checkpoint==4.2.0
# + langgraph-checkpoint-redis==0.5.1
```

**0.5.1** — my pin of `==0.1.*` had forced a version four minors behind, built against a
`langgraph-checkpoint` API that no longer exists.

### How I fixed it

```
langgraph-checkpoint-redis>=0.5,<1
```

**Rule, and this was the fourth time in one build:** a pin is a claim you have verified. Guessing
*down* is worse than not pinning — unpinned, the resolver finds a working set; a wrong pin
actively overrides that with a broken one. Resolve, don't recall:

```bash
uv pip install --dry-run "<pkg-a>" "<pkg-b>"
```

This is what drove the switch to lockfiles (`uv pip compile ... --python-platform linux`), with
both Dockerfiles installing from `requirements.lock` rather than `requirements.txt`.

---

## 8. Nova: `tool_calls` accumulates across turns

### Issue

Four requests in one session, watching the response:

| Turn | tool_calls reported | input_tokens | Actually new |
|---|---|---|---|
| 1 | 1 | 5,208 | check_balance |
| 2 | 2 | 10,884 | check_balance |
| 3 | 3 | 17,260 | query_transactions |
| 4 | 4 | 24,500 | check_balance |

### How I found it

Each turn adds exactly one call — so the agent was correct and the *report* was cumulative.
With a checkpointer attached, `result["messages"]` returns the **entire thread history**, not
just what this invocation produced. Iterating all of it reports every tool call ever made in
the session and sums `usage_metadata` across every past model call.

### How I fixed it

Read the thread length before invoking, then slice:

```python
snapshot = await state["agent"].aget_state(config)
prior_len = len(snapshot.values.get("messages", [])) if snapshot and snapshot.values else 0
...
turn = result["messages"][prior_len:]      # this turn only
```

Also added `turn_messages` and `history_messages` to the response. Input tokens legitimately
grow with conversation depth — the whole history is resent every turn — so cost analysis has to
distinguish *"the agent is inefficient"* from *"the conversation is long"*.

**Why it mattered more than it looked:** the evaluation scores `tool_selection` and
`parameter_accuracy` directly from this field. Left in, every multi-turn eval case would have
failed against a correctly-behaving agent, and the decomposed metrics would have pointed
confidently at "routing regressed."

**Rule:** with any stateful agent framework, be explicit about whether a returned collection is
*this invocation* or *accumulated state*. The two look identical on a single-turn test and
diverge from turn two — exactly where most manual testing stops.

---

## 9. Nova: repeat questions answered from memory instead of the tool

### Issue

With per-turn reporting honest, a repeated question showed:

| Turn | tool_calls | turn_messages | input_tokens |
|---|---|---|---|
| "balance on ACC-00004?" | 1 | 4 | 7,658 |
| same question again | 1 | 4 | 8,076 |
| "June 2026 transactions?" | 1 | 4 | 8,776 |
| same question again | **0** | **2** | **4,733** |

### How I found it

`turn_messages: 2` is the signature of a turn with no tool call — Human → AI, with no
Tool round trip. The model answered from the previous turn's result.

**The defect is the non-determinism, not the caching.** Row 2 re-queried; row 4 didn't. A golden
case asserting `expect_tool` would pass on some runs and fail on others with zero change to the
agent. A flaky metric is worse than a wrong one: you learn to ignore it.

Root cause was an unwritten rule. The system prompt said *"answer only from what the tools
return"* — it never said *"call a tool every time."* A model holding a valid tool result in
context is arguably obeying that instruction by reusing it.

### How I fixed it

Added to the system prompt:

> Always call a tool for questions about balances, transactions, cards, or loans — even if you
> answered the same question earlier in this conversation. Account data changes between turns,
> so an earlier tool result is not evidence for a later answer. Use the conversation history to
> work out what the question means (which account, which period), never to supply the answer
> itself.

Verified: same question twice in one session, `check_balance` called both times,
`turn_messages: 4` both times.

**"Is Haiku just not smart enough?" — no, and there's now evidence.** The same model follows the
rule reliably once the rule is written down. Smaller models *are* more sensitive to
under-specified prompts, which means this class of bug surfaces earlier on Haiku — useful during
development, not a reason to buy a larger model to reliably guess an unstated instruction.

**The distinction worth keeping:** session memory stores *what was said*, not *cached results*.
"And what about June?" is unanswerable without memory — nothing in it names an account. That is
memory telling the agent **what the question means**. Supplying **what the answer is** from the
same history is where it goes wrong.

---

## 10. `uv pip install` populated a different project's venv

### Issue

Creating this project's environment while another project's venv was still active:

```bash
uv venv                                    # "Creating virtual environment at: .venv"
uv pip install -r requirements-dev.txt     # installed 2 of 4 packages
./.venv/bin/python -c "import psycopg2"    # ModuleNotFoundError
```

### How I found it

**Two of four packages installed** was the tell. `httpx` and `pyyaml` were reported as already
satisfied — impossible in a venv created seconds earlier, but entirely expected in
`sovereign-rag-platform/.venv`, a RAG service that legitimately uses httpx.

```bash
echo "VIRTUAL_ENV=$VIRTUAL_ENV"
# VIRTUAL_ENV=/Users/.../sovereign-rag-platform/.venv
```

`uv pip install` honours `$VIRTUAL_ENV` over the directory-local `.venv` it just created.

### How I fixed it

Make the target explicit rather than ambient:

```bash
env -u VIRTUAL_ENV uv pip install --python .venv/bin/python -r requirements-dev.txt

# clean up the contaminated environment
env -u VIRTUAL_ENV uv pip uninstall \
  --python /path/to/other-project/.venv/bin/python psycopg2-binary ruff

# verify the target rather than inferring it
./.venv/bin/python -c "import sys; print(sys.prefix)"
```

Also worth noting: nothing was auto-activating the other venv. `grep` across `~/.zshrc`,
`~/.zprofile`, `~/.zshenv`, `~/.bash_profile` found only conda's init block — the other venv had
simply persisted in one long-lived shell since being sourced at session start. `deactivate` only
affects the shell it runs in.

**Rule:** ambient environment variables silently redirect tools that look well-scoped. Same
family as #1 — the command was right, the context was not. The tell is always that **the result
is inconsistent with what the command said it did.**

---

## 11. Terraform: `Error: Argument definition required`

### Issue

```
│ Error: Argument definition required
│   on secrets.tf line 88, in resource "google_secret_manager_secret" "langfuse_public_key":
│   88:   replication { auto {} }
```

### How I found it

The message names the exact line. HCL forbids a **nested block** inside a single-line block
definition — a one-line block may contain only a single *argument* assignment.

### How I fixed it

```hcl
replication {
  auto {}
}

lifecycle {
  ignore_changes = [labels, annotations]
}
```

```bash
terraform -chdir=terraform-gcp validate    # catches it in a second, before any apply
```

---

## 12. Evaluation: a working agent scores 0.17

### Issue

First full eval run against a verified-working Nova:

```
id       shape    tool  param  ground    cost$  tools
gs-001   single   1.00   1.00    0.00  0.00593  check_balance  <-- FAIL
gs-003   single   1.00   1.00    0.00  0.00650  query_transactions  <-- FAIL
...
AGG               1.00   1.00    0.17  0.07111  (12 cases, 55s)

  gs-001  unsupported: ['The balance on account ACC-00004 is 185,254.95 AED', ...]
```

The flagged claims were **correct** — 185,254.95 had been verified against Postgres directly.

### How I found it

The decomposed metrics located it immediately. `tool_selection` and `parameter_accuracy` were
both **1.00**; only the judged metric failed. Two deterministic metrics agreeing that the agent
was perfect, while the judged one said everything was unsupported, points at the *evidence given
to the judge* — not at the agent.

The harness was passing `resp["tool_calls"]`:

```json
[{"tool": "check_balance", "args": {"account_id": "ACC-00004"}}]
```

That is the tool **name and arguments**. It contains no result. The judge was asked whether
"the balance is 185,254.95" was supported by evidence that never contained a balance — and
correctly said no. Nova's response exposed which tools *ran*, never what they *returned*.

### How I fixed it

Capture `ToolMessage` contents in Nova and return them:

```python
if getattr(m, "type", "") == "tool":
    tool_results.append({
        "tool": getattr(m, "name", "unknown"),
        "result": m.content if isinstance(m.content, str) else str(m.content),
    })
```

and score against those instead:

```python
evidence = resp.get("tool_results", [])
gs, unsupported = score_groundedness(judge, resp.get("answer", ""),
                                     json.dumps(evidence, indent=2) if evidence else "")
```

Result: **0.17 → 0.92**, 11 of 12 passing. The remaining failure is judge flakiness on a derived
sum, and is the argument for moving `NOVA_JUDGE_MODEL` from Haiku to Sonnet.

**This is the best argument for decomposed metrics I could have asked for.** A single blended
score would have read ~0.7 — "something's wrong" — and sent me hunting the agent. Four separate
scores said *"routing and parameters are perfect, only the judged metric fails,"* which points
at one thing and nothing else.

**Rule, twice over in this build (see also #8):** an instrument that lies makes a healthy system
look broken. Validate the harness against a known-good case before trusting a low score.

---

## 13. Build: `failed to connect to the docker API`

### Issue

```
ERROR: failed to connect to the docker API at unix:///Users/.../docker.sock;
check if the path is correct and if the daemon is running
```

### How I found it

Docker Desktop wasn't running. Also spotted in the same paste: the `IMAGE` variable had lost its
`:$TAG` suffix, which would have broken the later `sed` substitution.

### How I fixed it

Built in GCP instead of starting Docker:

```bash
gcloud services enable cloudbuild.googleapis.com --project=mlops-lifecycle-p7-gke

TAG=$(date +%Y%m%d-%H%M)
IMAGE="us-west1-docker.pkg.dev/mlops-lifecycle-p7-gke/mlops-lifecycle/nova:$TAG"
echo "$IMAGE"          # always echo — a lost :$TAG silently breaks the sed step

gcloud builds submit nova/ --tag="$IMAGE" --project=mlops-lifecycle-p7-gke
```

Two bonuses worth remembering: **Cloud Build builds natively on amd64**, sidestepping the
`exec format error` you get from building on Apple silicon for GKE nodes without
`--platform linux/amd64`; and the first 120 build-minutes/day are free.

Trap on first use: enabling the API and immediately retrying returned `PERMISSION_DENIED`. **API
enablement lags IAM propagation by a minute or two** — retry before debugging roles.

---

## 14. Jobs: immutable templates and TTL reaping

### Issue

Two surprises re-running the seed Job:

```bash
kubectl set env job/seed-banking-data -n nova SOURCE_COMMIT=abc123
# fails — a Job's pod template cannot be changed after creation

kubectl delete job seed-banking-data -n nova
# Error from server (NotFound): jobs.batch "seed-banking-data" not found
```

### How I found it

The second is `ttlSecondsAfterFinished: 3600` doing exactly its job — the TTL controller had
reaped the completed Job an hour after it finished.

### How I fixed it

Make the delete unconditional so the same command works in both states:

```bash
kubectl delete job seed-banking-data -n nova --ignore-not-found
kubectl apply -f k8s/seed-job.yaml
```

And refresh the ConfigMap first, or the Job re-runs the old script — the code lives in the
ConfigMap, not the image:

```bash
kubectl create configmap seed-script -n nova --from-file=db/seed.py \
  --dry-run=client -o yaml | kubectl apply -f -
```

---

## 15. `kubectl` hangs: `dial tcp 35.230.96.96:443: i/o timeout`

### Issue

Every `kubectl` command hung for ~30s then failed with `i/o timeout`. No error, no refusal
— packets going nowhere.

### How I found it

`kubectl config` subcommands still worked, which proved the kubeconfig was fine and isolated
it to network reachability:

```bash
kubectl config current-context        # works — reads a local file
kubectl get nodes                      # hangs — needs the network
```

`i/o timeout` means silently dropped, not rejected. On GKE with master authorized networks,
that is the allowlist. Checked the current public IP against the one Terraform holds:

```bash
curl -s -4 ifconfig.me                 # 86.98.166.216
grep authorized_cidr terraform-gcp/variables.tf   # 83.110.175.141/32
```

Residential IP had rotated.

### How I fixed it

Edited the **default** in `terraform-gcp/variables.tf`, not `-var` — a `-var` does not persist,
and the next apply would reconcile back to the dead IP:

```bash
terraform -chdir=terraform-gcp apply
```

Durable alternative worth knowing: GKE's DNS-based endpoint moves auth from source-IP
(network layer) to IAM `container.clusters.connect` (identity layer), and the problem stops
existing.

---

## 16. Helm silently ignored `applicationSet.enabled: false`

### Issue

`gitops/bootstrap/argocd-values.yaml` disabled the ApplicationSet controller. Argo CD came up
with five pods including `argocd-applicationset-controller`. `dex` and `notifications`, disabled
in the same file, were correctly absent — so the file was being read.

### How I found it

`helm get values` showed the key as supplied, which was the trap — it reports what you
*sent*, not what took effect. `helm get values -a` is not decisive either: the computed merge
echoes unknown supplied keys straight back. The decisive test renders the chart twice:

```bash
helm template argocd argo/argo-cd --version 10.4.0 \
  | grep -c "applicationset-controller"           # 28
helm template argocd argo/argo-cd --version 10.4.0 --set applicationSet.enabled=false \
  | grep -c "applicationset-controller"           # 28
```

Expected `28` then `0`. A live `enabled` flag wraps a whole template in `{{- if }}`; equal
counts mean no such guard exists. Chart 10.x removed the toggle — the controller became core.

### How I fixed it

Removed the dead key, left the controller at the chart default. Helm gives no error for a
value that matches nothing in the templates; the only proof is the rendered output.

---

## 17. Faithfulness scored 0.00 on every case — and flagged true claims

### Issue

First eval run against the cluster: `faithfulness 0.00` across the board. The "unsupported"
list contained `The balance on account ACC-00004 is 185,254.95 AED` — which is exactly what
the tool returns.

### How I found it

The judge is shown no evidence when `tool_results` is empty. Checked the raw response:

```bash
curl -s -X POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"session_id":"probe","message":"What is the balance on ACC-00004?"}' \
  | python -c "import json,sys; print(sorted(json.load(sys.stdin).keys()))"
# ['answer','history_messages','latency_ms','model','session_id','tool_calls',
#  'trace_id','turn_messages','usage']          <- no tool_results
```

`nova/app.py:388` returns it. The pod did not, because pods run an image:

```bash
git log --format="%h %ad" --date=iso -S "tool_results" -- nova/app.py
# f967e18 2026-08-17 15:16:14     <- committed at 15:16
# live image tag: nova:20260817-1245-nocache   <- built at 12:45
```

The image predated the field by 2.5 hours. The earlier "successful" runs that scored 0.92 had
targeted `localhost:8000` — a **locally-run Nova on current source**, not a port-forward. The
stored `target` field could not tell the two apart.

### How I fixed it

Rebuilt and shipped through the pipeline that this exact incident motivated finishing — CI
builds, commits the tag, Argo CD rolls it. Faithfulness went to 1.00. The runner now records
the artifact tag alongside the target so this is a five-second check next time.

---

## 18. Cloud Build: "forbidden from accessing the bucket" — blames `serviceusage`

### Issue

```
ERROR: (gcloud.builds.submit) The user is forbidden from accessing the bucket
[mlops-lifecycle-p7-gke_cloudbuild]. Please check ... "serviceusage.services.use"
```

Service Usage Admin was already granted. Adding more of it changed nothing.

### How I found it

Read the resource named, not the permission suggested. It names a **bucket**.
`gcloud builds submit` calls `storage.buckets.get` on `<project>_cloudbuild` before uploading
source. The SA had `roles/storage.objectAdmin`, which covers objects and nothing on the bucket.

### How I fixed it

Bucket-scoped, not project-wide — project `storage.admin` would also hand CI the Terraform
state bucket:

```bash
gcloud storage buckets add-iam-policy-binding gs://mlops-lifecycle-p7-gke_cloudbuild \
  --member=serviceAccount:gha-cicd@mlops-lifecycle-p7-gke.iam.gserviceaccount.com \
  --role=roles/storage.admin
```

GCP funnels several distinct denials through one error string. The resource is the signal.

---

## 19. Cloud Build: green build, red pipeline

### Issue

```
Created [https://cloudbuild.googleapis.com/.../builds/f133ad2c-...]
ERROR: (gcloud.builds.submit) The build is running, and logs are being written to the
default logs bucket. This tool can only stream logs if you are Viewer/Owner of the project
Error: Process completed with exit code 1.
```

The image was in Artifact Registry. gcloud had exited 1 on a build that succeeded.

### How I found it

The message says what it wants: project Viewer/Owner. Two attempts to be cleverer than it
both failed — `roles/logging.viewer` (wrong role for that check) and `--suppress-logs` (does
not bypass the pre-stream permission check). A third, an `--async` polling loop, was reverted
as machinery to avoid one read-only role.

Why it mattered more than a red X: the job died **after** the expensive step and **before**
"Bump the chart and push" — an image nothing would deploy.

### How I fixed it

```bash
gcloud projects add-iam-policy-binding mlops-lifecycle-p7-gke \
  --member=serviceAccount:gha-cicd@... --role=roles/viewer
```

When an error names the fix, try it first and tighten afterwards.

---

## 20. The golden set was green three times while asserting nothing

### Issue

18/18 passed repeatedly. Four of those passes were vacuous.

### How I found it

`gs-002` (cards on ACC-00004) was known-empty. Checking the other pinned IDs:

```sql
SELECT customer_id, COUNT(*) FROM accounts WHERE customer_id='CUS-00012' GROUP BY 1;
-- (0 rows)
```

`gs-006`, `gs-007`, `gs-016` all keyed on `CUS-00012` — a customer with no accounts and no
loans, scoring 1.00 against empty tool results. An empty result is trivially faithful.

Two more found by reading the failures, not the passes: `gs-018` scored 0.70 on relevance for
correctly asking "which account?" (the metric had no carve-out for unanswerable questions);
`gs-016` expected `[list_accounts, check_balance]` when `list_accounts` already returns
balance per row — the fixture demanded the exact waste `tool_correctness` penalises.

### How I fixed it

Repointed to `CUS-00034` (5 accounts, 1 loan) via a query that picks by structure, not by
guess. Added the relevance carve-out to the judge prompt. Fixed `gs-016`'s expectation. Wrote
`README-placeholders.sql` to re-check the structural facts after any reseed — because **a case
whose data goes empty keeps passing; nothing in the suite tells you.**

---

## 21. Lock resolved `anthropic==1.1.0`; the validated venv ran 0.122.0

### Issue

`eval/requirements.txt` said `anthropic>=0.40`. `uv pip compile` produced `anthropic==1.1.0`
plus `httpx2` and `httpcore2` — a major version on a different HTTP stack, never run.

### How I found it

Compared against what produced the 18/18 baseline:

```bash
.venv/bin/python -c "import importlib.metadata as m; print(m.version('anthropic'))"
# 0.122.0
```

Same session, same shape: `helm search repo argo/argo-rollouts` returned "latest" from a
four-day-old cache. `helm search` never touches the network:

```bash
ls -la ~/Library/Caches/helm/repository/argo-index.yaml    # Aug 24
helm repo update argo
```

### How I fixed it

Pinned `anthropic==0.122.0`; regenerated; confirmed `httpx2` was gone from the lock. Fourth
resolution incident in this build — `helm search`, `helm install` without `--version`,
`pip install` without a lock, `uv pip compile` with a range: all resolve against something
you did not look at.

---

## 22. Two container-only bugs, caught before the first paid run

### Issue

`run_eval.py` worked on the laptop and would have failed in the image — after all 18 cases
and the judge calls were paid for.

### How I found it

Reading the Dockerfile against the script:

```python
ap.add_argument("--set", default="eval/golden/questions.yaml")   # relative to CWD
out = f"eval-results-{run_id}.json"                                 # written to CWD
```

Image puts the script at `/app` (path does not exist there), owned by root, process runs as
uid 10001 (write fails).

### How I fixed it

`--set` resolves against `__file__`; added `--out-dir`, Job passes `/tmp`. "Works on my
laptop" tests the laptop's working directory and user; a container has neither.

---

## 23. `network is unreachable` — same endpoint as #15, different cause

### Issue

`kubectl` failed against the same `35.230.96.96:443` with a different verb.

### How I found it

```bash
curl -s -4 ifconfig.me; echo      # blank
```

No internet at all. Three errors seen on this one endpoint, three layers:

| error | meaning |
|---|---|
| `i/o timeout` | packets dropped — allowlist (#15) |
| `network is unreachable` | OS has no route — local connectivity |
| `connection refused` | something answered and said no — port-forward down |

### How I fixed it

Reconnected. The same IP in every message made three problems look like one; the verb after
`dial tcp` is the diagnosis.

---

## 24. Two `argo-rollouts` controller pods

### Issue

Fresh install, two pods Running. Two Argo Rollouts *installations* in one cluster would fight
over the same `Rollout`; two pods looked like that.

### How I found it

```bash
kubectl -n argo-rollouts get lease
# argo-rollouts-controller-lock   argo-rollouts-595cb67bb8-sdjhx_...
```

One holder. Chart default `controller.replicas: 2` for HA; leader election means one
reconciles, the other stands by.

Finding that default took three attempts: `grep -A3 "^controller:"` (block is 100+ lines),
then `awk '/^controller:/,/^[a-z]/'` (start line matches both patterns, range closes
immediately). Plain `grep -n replicas` found it at line 105 at once.

### How I fixed it

Left it. Stop guessing how far away a value sits — search the whole file, or render the chart.

---

## 25. Post-promotion gate aborted a rollout because Nova was perfect

### Issue

Revision 2: golden set passed, promoted, then `error-rate assessed Failed due to failed (2) >
failureLimit (1)`. Rolled back.

### How I found it

The AnalysisRun holds every measurement:

```bash
kubectl -n nova get analysisrun nova-6ddcbb9548-2-post -o yaml
```

```yaml
- name: error-rate
  measurements:
  - {phase: Failed, value: '[]'}       # empty
  - {phase: Failed, value: '[]'}
- name: latency-p95
  measurements:
  - {phase: Successful, value: '[2.55]'}   # same Prometheus, real data
```

All 20 synthetic requests succeeded. A labelled counter does not exist until its first
increment, so `nova_requests_total{status="error"}` was never created, `{status!="ok"}`
matched nothing, `sum()` of nothing is empty, and `empty / anything = empty`.

### How I fixed it

```promql
(sum(rate(nova_requests_total{status!~"ok|pending_approval"}[2m])) or vector(0))
  / sum(rate(nova_requests_total[2m]))
```

`or vector(0)` floors the numerator. `len(result) > 0` in the successCondition stays: if the
load Job dies, the denominator is empty and the gate still fails correctly. `!~` not `!=` —
`!=` compares one literal string, and `{status!="ok|pending_approval"}` would match everything.

---

## 26. Faithfulness: `['AED 782.99', 'AED 668.71', 'AED 114.28']`

### Issue

Revision 3 pre-promotion, then revision 5 again. The numbers were all in the tool result.

### How I found it

The flagged strings share one thing that is not in the tool output — the unit:

```python
# spending_by_category returned
{"total_spent": 782.99, "by_category": [{"category": "utilities", "total": 668.71}, ...]}
# no currency field
```

The agent wrote "AED" from context. Five of nine tools had the same gap; two account tools
already returned currency, so the API was inconsistent rather than designed. And the agent
states the unit only *sometimes* — revision 4 passed the identical suite — so this was
**gate flakiness**, not a formatting nit.

### How I fixed it

The tool, not the judge — loosening the prompt would teach the metric to tolerate the class of
claim it exists to catch. Joined `accounts` for `currency` in four tools; one-row lookup in
`spending_by_category` (it is a `GROUP BY`); alongside `amount` in `initiate_transfer`.
`get_loans` exposed a schema gap — `loans` has no currency and no `account_id` — so it infers
from the customer's accounts and returns **null when they disagree**.

---

## 27. Six CI edits reported success — zero applied

### Issue

A python heredoc with six `str.replace` calls on the workflow. The next line ran the YAML
validator, which printed `parses OK`. Nothing had changed.

### How I found it

```
          } >> "$GITHUB_STEP_SUMMARY\"\"\"\",
SyntaxError: unterminated string literal
```

Replacement text ended in `"`; the heredoc's `"""` closed against it. The script died before
its first replace. The validator ran regardless, against the unchanged file.

### How I fixed it

One `Edit` per change — each shows a diff, each can be rejected, none can apply zero of six
silently. A check that runs regardless of whether the prior step succeeded is not a check.

---

## 28. `/approve`: `list indices must be integers or slices, not str`

### Issue

First HITL demo. Two failures in sequence. First, retrying `/chat` on a paused session:

```
400 - messages.2: `tool_use` ids were found without `tool_result` blocks immediately after
```

Then on a fresh session, `/approve` returned the `TypeError`.

### How I found it

The first was a corrupted session — the paused checkpoint held `[Human, AI(tool_use)]` with no
`ToolMessage`; a second `/chat` appended a `HumanMessage` after the dangling `tool_use`.
Unrecoverable; every retry showed the **same** `toolu_` ID, the tell that it was old state.

The second's HTTP body carried only the message. The location was in the pod, keyed by the
returned `trace_id`:

```bash
kubectl -n nova logs -l app=nova --tail=300 | grep -A40 "3bda1051"
```

```
File ".../langchain/agents/middleware/human_in_the_loop.py", line 450, in after_model
    decisions = interrupt(hitl_request)["decisions"]
```

That line is the contract: `interrupt()` returns what `Command(resume=...)` supplied, and the
middleware indexes it with `"decisions"`. A bare list had been sent — inferred from the type
names, flagged as unverified before deploy, and deployed anyway.

### How I fixed it

```python
Command(resume={"decisions": [decision]})
```

A traceback is read bottom-up; the line number is an address, the printed source is the
information. And a known-unverified assumption on a paid path is a decision to pay for finding
out — `inspect.signature` would have cost thirty seconds. The `/chat` guard against paused
sessions is still open.

---

## Appendix: the commands that did the diagnosing

Ordered by how often they earned their place.

```bash
# 1. CONTEXT FIRST — most confusing results were wrong-account/wrong-project errors
gcloud config list
gcloud projects list
kubectl config current-context

# 2. Find a traceback under readiness-probe noise. The single most useful log command
#    here: a 10s probe writes ~6 lines/min, so --tail=200 covers only ~30 minutes.
kubectl logs -n nova deploy/nova --tail=400 | grep -v "GET /healthz" | tail -60

# 3. `kubectl logs deploy/x` picks ONE pod. Loop by name when unsure which served it.
for p in $(kubectl get pods -n nova -l app=nova -o name); do
  echo "--- $p"; kubectl logs -n nova $p --tail=500 | grep -v "GET /healthz" | tail -30
done

# 4. Pod AGE vs when you deployed — an older pod means pre-fix logs, and an apply
#    that produced no new pod
kubectl get pods -n nova -l app=nova -o wide

# 5. Which build is actually live (the manifest holds a placeholder, the cluster the tag)
kubectl get deploy nova -n nova -o jsonpath='{.spec.template.spec.containers[0].image}'

# 6. Resolve versions instead of recalling them
uv pip install --dry-run "<pkg-a>" "<pkg-b>"

# 7. Startup state in one line each
kubectl logs -n nova deploy/nova | grep -iE "loaded|session memory|tracing"

# 8. The two data invariants, after every re-seed
kubectl exec -n nova postgres-0 -- psql -U nova -d nova -c "
  SELECT count(*) FROM accounts a JOIN (
    SELECT account_id, SUM(CASE WHEN direction='credit' THEN amount ELSE -amount END) AS net
    FROM transactions GROUP BY account_id) t USING (account_id) WHERE a.balance <> t.net;"

kubectl exec -n nova postgres-0 -- psql -U nova -d nova -c "
  SELECT account_type, count(*) FILTER (WHERE balance < -overdraft_limit) AS impossible
  FROM accounts GROUP BY account_type;"
```

**When a request fails with a useless error body, ask in this order:** did it reach the pod at
all (is there a `POST` line?), is this the pod I just deployed (does AGE match the rollout?),
and only then read the traceback.
