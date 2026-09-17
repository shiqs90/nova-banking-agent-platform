"""Nova — banking assistant API.

FastAPI + LangChain agent over three MCP connectors, with Redis-backed session memory.

RUNAWAY PROTECTION. An agent loop with no ceiling will call tools until something
else stops it — usually bill. Four independent limits, because each catches a
different failure and any one of them alone has a gap:

  1. RECURSION_LIMIT   — total graph steps per request. The hard stop on loops.
  2. MAX_TOKENS        — output cap per model call. Stops one runaway generation.
  3. REQUEST_TIMEOUT   — wall-clock per HTTP call. Stops a hung tool from pinning
                         a worker forever.
  4. CLIENT_TIMEOUT    — per Anthropic API call, with retries bounded.

Limit 1 is the one that matters for cost. A tool-calling round is roughly two graph
steps (model decides, tools execute), so RECURSION_LIMIT=15 allows about seven
rounds — generous for a two-tool question, and far short of a loop.

MODELS. Agent and judge are deliberately separate variables even though both start
as Haiku. The judge moves to Sonnet once the agent behaves, and that has to be a
config change rather than a code change — the judge model is part of what an
evaluation run is versioned by, so swapping it must be visible and deliberate.
"""

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, PIIMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.types import Command

# ---------------------------------------------------------------------------
# Configuration — every limit is an env var so it can be tuned without a rebuild
# ---------------------------------------------------------------------------

AGENT_MODEL = os.environ.get("NOVA_AGENT_MODEL", "claude-haiku-4-5")
JUDGE_MODEL = os.environ.get("NOVA_JUDGE_MODEL", "claude-haiku-4-5")  # → sonnet later

#Guardrails for runaway loops and Cost control.
RECURSION_LIMIT = int(os.environ.get("NOVA_RECURSION_LIMIT", "15"))
MAX_TOKENS = int(os.environ.get("NOVA_MAX_TOKENS", "2048"))
REQUEST_TIMEOUT = float(os.environ.get("NOVA_REQUEST_TIMEOUT", "60"))
CLIENT_TIMEOUT = float(os.environ.get("NOVA_CLIENT_TIMEOUT", "30"))
MAX_RETRIES = int(os.environ.get("NOVA_MAX_RETRIES", "2"))

MCP_SERVERS = {
    "accounts": {
        "url": os.environ.get("MCP_ACCOUNTS_URL", "http://mcp-accounts:8080/mcp"),
        "transport": "streamable_http",
    },
    "transactions": {
        "url": os.environ.get("MCP_TRANSACTIONS_URL", "http://mcp-transactions:8080/mcp"),
        "transport": "streamable_http",
    },
    "products": {
        "url": os.environ.get("MCP_PRODUCTS_URL", "http://mcp-products:8080/mcp"),
        "transport": "streamable_http",
    },
}

SYSTEM_PROMPT = """You are Nova, a retail banking assistant.

Answer only from what the tools return. If a tool gives you no data, say so — never
estimate a balance, invent a transaction, or infer a number the tools did not
provide. A wrong number here is worse than no answer.

Always call a tool for questions about balances, transactions, cards, or loans —
even if you answered the same question earlier in this conversation. Account data
changes between turns, so an earlier tool result is not evidence for a later answer.
Use the conversation history to work out what the question means (which account,
which period), never to supply the answer itself.

Today's date is 2026-08-01. Resolve relative dates against it before calling any
tool: "last month" is 2026-07-01 to 2026-07-31. Tools take absolute YYYY-MM-DD
dates and do not interpret relative phrases.

A negative balance on a current account is normal if it is within the overdraft
limit. Say so plainly rather than raising alarm.

Keep answers short. State the number and the fact that supports it."""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nova")

state: dict = {}

# ---------------------------------------------------------------------------
# Prometheus metrics
#
# Langfuse and Prometheus answer different questions and neither replaces the other:
#   Langfuse   "what happened in THIS request"       — traces, for Debugging
#   Prometheus "what's happening across ALL of them" — metrics, for Alerting
#
# Only an aggregate can gate a rollout, which is why E1's Argo Rollouts analysis
# reads Prometheus and not Langfuse.
#
# Cardinality discipline: `tool` is bounded at 9 and `status` at 3. trace_id and
# session_id are deliberately NOT labels — unbounded label values are the standard
# way to take down a Prometheus, and that is exactly what traces are for.
# ---------------------------------------------------------------------------

from prometheus_client import Counter, Gauge, Histogram  # noqa: E402

REQUESTS = Counter("nova_requests_total", "Chat requests", ["status"])
LATENCY = Histogram(
    "nova_request_duration_seconds", "End-to-end chat latency",
    # Buckets chosen from observed behaviour: a single-tool turn lands ~1.9s, a
    # two-tool turn ~2.6s. Default buckets top out too low to show the tail.
    buckets=(0.5, 1, 2, 3, 5, 8, 15, 30, 60),
)
TOOL_CALLS = Counter("nova_tool_calls_total", "Tool invocations", ["tool"])
TOKENS = Counter("nova_tokens_total", "Model tokens", ["direction"])
COST = Counter("nova_cost_usd_total", "Estimated model spend in USD")
TURN_TOOLS = Histogram(
    "nova_tools_per_turn", "Tool calls in one turn",
    buckets=(0, 1, 2, 3, 5, 8),
)

# The degraded-state gauges. War story #5: the Redis checkpointer fell back to
# in-process memory and announced it in a single WARNING — a healthy pod that
# silently loses every conversation on restart. A log line is not a signal; this is.
MEMORY_PERSISTENT = Gauge("nova_memory_persistent", "1 if session memory is Redis-backed, 0 if in-process")
TRACING_ENABLED = Gauge("nova_tracing_enabled", "1 if Langfuse tracing is active")

# USD per million tokens, mirroring eval/run_eval.py.
RATES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5":  (3.00, 15.00),
    "claude-opus-5":    (5.00, 25.00),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Connect to the MCP servers once at startup, not per request.

    Per-request connection would add the full MCP handshake (initialize, session,
    tool discovery) to every customer question — three round trips before any work
    begins.
    """
    client = MultiServerMCPClient(MCP_SERVERS)
    tools = await client.get_tools()
    log.info("loaded %d tools: %s", len(tools), [t.name for t in tools])

    model = ChatAnthropic(
        model=AGENT_MODEL,
        max_tokens=MAX_TOKENS,
        timeout=CLIENT_TIMEOUT,
        max_retries=MAX_RETRIES,
        api_key=os.environ["ANTHROPIC_API_KEY"],
    )

    # Checkpointer = session memory. Redis so a pod restart doesn't drop every
    # in-flight conversation. Falls back to in-memory if Redis is unreachable —
    # degraded (memory dies with the pod) but the service still answers, which is
    # the right tradeoff for a memory store.
    # AsyncRedisSaver, not RedisSaver. The sync saver's async methods (aget_tuple,
    # aput) are unimplemented on the base class, so every request through
    # agent.ainvoke() dies with a bare NotImplementedError — and `str()` on that is
    # the empty string, so the error surfaces with no message at all.
    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver

        redis_url = os.environ.get("REDIS_URL", "redis://redis.nova.svc.cluster.local:6379")
        checkpointer_cm = AsyncRedisSaver.from_conn_string(redis_url)
        checkpointer = await checkpointer_cm.__aenter__()
        await checkpointer.asetup()
        state["checkpointer_cm"] = checkpointer_cm
        state["memory_backend"] = "redis"
        log.info("session memory: redis at %s", redis_url)
    except Exception as exc:
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer = InMemorySaver()
        state["memory_backend"] = "in-memory (DEGRADED)"
        log.warning("redis unavailable (%s: %s) — session memory is in-process only",
                    type(exc).__name__, exc)

    # Langfuse tracing. One callback handler instruments the whole agent — every LLM
    # call, tool call, prompt, completion, and token count, nested under one trace.
    #
    # Optional by design: if the keys are absent the agent runs untraced rather than
    # failing to start. Observability that can take down the thing it observes is a
    # bad trade.
    state["tracing"] = "disabled"
    if os.environ.get("LANGFUSE_PUBLIC_KEY"):
        try:
            from langfuse.langchain import CallbackHandler

            state["langfuse_handler"] = CallbackHandler()
            state["tracing"] = "langfuse"
            log.info("tracing: langfuse at %s",
                     os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"))
        except Exception as exc:
            log.warning("langfuse init failed (%s: %s) — running untraced",
                        type(exc).__name__, exc)

    state["agent"] = create_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
        middleware=[
            # Only the transfer tool pauses.
            HumanInTheLoopMiddleware(interrupt_on={"initiate_transfer": True}),

            # apply_to_tool_results is the flag that matters here: it defaults to
            # False, and the PII we care about arrives FROM get_customer — full_name,
            # email, phone — none of which the agent needs to answer anything. Setting
            # it redacts the ToolMessage in before_model, so the model, the traces, and
            # the checkpointed transcript never carry the address.
            #
            # apply_to_input stays at its default of True, so user messages are scanned
            # too. Harmless for the golden set — every case keys on an account ID like
            # ACC-00004, which an email detector cannot match — but it does mean a user
            # who types an email gets a placeholder passed to any tool expecting one.
            # No current MCP tool takes an email argument; revisit if one does.
            #
            # email only: the built-in types are email/credit_card/ip/mac_address/url.
            # A phone regex is tractable; full_name is not, and a name detector that
            # misses half of them is worse than not claiming one. See README.
            PIIMiddleware("email", strategy="redact", apply_to_tool_results=True),
            PIIMiddleware("phone", strategy="mask", detector=r"\+\d{9,15}", apply_to_tool_results=True)
        ],
    )
    state["tool_names"] = [t.name for t in tools]

    MEMORY_PERSISTENT.set(1 if state.get("memory_backend") == "redis" else 0)
    TRACING_ENABLED.set(1 if state.get("tracing") == "langfuse" else 0)

    yield

    if "checkpointer_cm" in state:
        await state["checkpointer_cm"].__aexit__(None, None, None)


app = FastAPI(title="Nova", lifespan=lifespan)


@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint.

    Hand-mounted rather than using prometheus-fastapi-instrumentator: the metrics
    that matter here are domain ones (tool calls, tokens, cost, memory backend), and
    the default HTTP middleware would add per-path request counters that duplicate
    what the ingress already reports.
    """
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    from fastapi.responses import Response

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


class ChatRequest(BaseModel):
    message: str
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


@app.get("/healthz")
async def healthz():
    # memory_backend is here on purpose. A checkpointer that silently fell back to
    # in-process memory leaves a healthy pod that works in a one-pod demo and loses
    # every conversation on restart or scale-out. A WARNING in the log is not enough
    # signal for that; it belongs somewhere a probe or a dashboard can see it.
    return {"status": "ok", "agent_model": AGENT_MODEL, "judge_model": JUDGE_MODEL,
            "memory_backend": state.get("memory_backend", "unknown"),
            "tracing": state.get("tracing", "unknown"),
            "tools": state.get("tool_names", [])}


@app.post("/chat")
async def chat(req: ChatRequest):
    """One customer turn.

    Returns the tool calls and token usage alongside the answer. The evaluation in
    Phase 4 scores tool_correctness and argument_correctness directly from this
    response, so it has to expose what the agent *did*, not only what it said.
    """
    trace_id = str(uuid.uuid4())
    started = time.monotonic()

    config = {
        "configurable": {"thread_id": req.session_id},   # <- what makes memory work
        "recursion_limit": RECURSION_LIMIT,              # <- the loop ceiling
    }

    # trace_id and session_id go into Langfuse as metadata, so a failing eval row
    # leads straight to the trace that produced it. That link is the whole point of
    # the five-stage debugging path — without it, a bad score tells you something
    # broke but not where.
    if "langfuse_handler" in state:
        config["callbacks"] = [state["langfuse_handler"]]
        config["metadata"] = {
            "langfuse_session_id": req.session_id,
            "langfuse_tags": [AGENT_MODEL],
            "trace_id": trace_id,
        }

    # How many messages the thread already holds. Everything from this index on is
    # what THIS turn produced.
    #
    # Without this, result["messages"] returns the whole conversation from the
    # checkpointer, and reporting all of it means tool_calls accumulates across turns
    # (1, 2, 3, 4 ...) and usage sums every past call. Phase 4 scores tool_correctness
    # and argument_correctness from exactly this field, so the bug would fail every
    # multi-turn eval case against a correctly-behaving agent.
    prior_len = 0
    try:
        snapshot = await state["agent"].aget_state(config)
        if snapshot and snapshot.values:
            prior_len = len(snapshot.values.get("messages", []))
    except Exception:
        pass   # first turn in a thread — no state yet

    try:
        import asyncio

        result = await asyncio.wait_for(
            state["agent"].ainvoke({"messages": [{"role": "user", "content": req.message}]},
                                   config=config),
            timeout=REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        REQUESTS.labels(status="timeout").inc()
        LATENCY.observe(time.monotonic() - started)
        log.warning("trace_id=%s timeout after %.1fs", trace_id, REQUEST_TIMEOUT)
        return {"trace_id": trace_id, "error": "timeout", "session_id": req.session_id}
    except Exception as exc:
        # A recursion-limit breach lands here. Surface it as its own error rather
        # than a generic 500 — "the agent looped" and "the model errored" need
        # different fixes, and Phase 4's metrics have to tell them apart.
        kind = "recursion_limit" if "recursion" in str(exc).lower() else "error"
        # recursion_limit gets its own label: "the agent looped" and "the model
        # errored" need different fixes, and an alert should distinguish them.
        REQUESTS.labels(status=kind).inc()
        LATENCY.observe(time.monotonic() - started)
        log.exception("trace_id=%s %s", trace_id, kind)
        # Include the exception TYPE, not just str(exc). A bare NotImplementedError
        # stringifies to "" — which is how this handler once returned
        # {"error": "error", "detail": ""} and sent us to the pod logs for something
        # the response could have named outright.
        return {"trace_id": trace_id, "error": kind,
                "detail": f"{type(exc).__name__}: {exc}"[:500],
                "session_id": req.session_id}

    # The graph paused instead of finishing. HumanInTheLoopMiddleware raised an
    # interrupt before initiate_transfer executed, so NO MONEY HAS MOVED — the balances
    # are unchanged and can be checked to prove it.
    #
    # State is checkpointed in Redis under thread_id == session_id, so session_id IS
    # the resume token. Nothing extra to store or expire.
    if "__interrupt__" in result:
        REQUESTS.labels(status="pending_approval").inc()
        LATENCY.observe(time.monotonic() - started)
        pending = [i.value for i in result["__interrupt__"]]
        log.info("trace_id=%s PAUSED for approval: %s", trace_id, pending)
        return {
            "trace_id": trace_id,
            "session_id": req.session_id,
            "status": "pending_approval",
            "pending": pending,
            "resume_with": "POST /approve {session_id, decision: approve|reject}",
        }

    messages = result["messages"]

    # Only this turn's messages. `messages` is the full thread; `turn` is the slice
    # the current request produced.
    turn = messages[prior_len:]

    tool_calls, tool_results, tokens_in, tokens_out = [], [], 0, 0
    for m in turn:
        for call in getattr(m, "tool_calls", []) or []:
            tool_calls.append({"tool": call["name"], "args": call["args"]})

        # ToolMessage — what the tool actually RETURNED. Without this the response
        # says which tools ran but not what they said, and the groundedness judge has
        # nothing to check the answer against: it sees only
        #   [{"tool": "check_balance", "args": {...}}]
        # and correctly rules that "the balance is 185,254.95" is unsupported. The
        # metric then measures the harness, not the agent.
        if getattr(m, "type", "") == "tool":
            tool_results.append({
                "tool": getattr(m, "name", "unknown"),
                "result": m.content if isinstance(m.content, str) else str(m.content),
            })

        usage = getattr(m, "usage_metadata", None) or {}
        tokens_in += usage.get("input_tokens", 0)
        tokens_out += usage.get("output_tokens", 0)

    answer = messages[-1].content
    if isinstance(answer, list):   # content blocks rather than a plain string
        answer = " ".join(b.get("text", "") for b in answer if isinstance(b, dict))

    elapsed = time.monotonic() - started
    latency_ms = round(elapsed * 1000)

    REQUESTS.labels(status="ok").inc()
    LATENCY.observe(elapsed)
    TURN_TOOLS.observe(len(tool_calls))
    for c in tool_calls:
        TOOL_CALLS.labels(tool=c["tool"]).inc()
    TOKENS.labels(direction="input").inc(tokens_in)
    TOKENS.labels(direction="output").inc(tokens_out)
    rate_in, rate_out = RATES.get(AGENT_MODEL, (0.0, 0.0))
    COST.inc((tokens_in / 1e6) * rate_in + (tokens_out / 1e6) * rate_out)

    log.info("trace_id=%s turn=%d tools=%s tokens=%d/%d %dms",
             trace_id, len(turn), [t["tool"] for t in tool_calls],
             tokens_in, tokens_out, latency_ms)

    return {
        "trace_id": trace_id,
        "session_id": req.session_id,
        "answer": answer,
        # This turn only — see prior_len above.
        "tool_calls": tool_calls,
        # What the tools returned. This is the evidence the groundedness judge scores
        # the answer against — reference-free evaluation needs the tool output, not
        # just the fact that a tool ran.
        "tool_results": tool_results,
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        # Conversation depth. Input tokens legitimately grow with it (the whole
        # history is resent each turn), so cost analysis needs to know whether a
        # rising bill means an inefficient agent or simply a longer conversation.
        "turn_messages": len(turn),
        "history_messages": len(messages),
        "latency_ms": latency_ms,
        "model": AGENT_MODEL,
    }


class ApprovalRequest(BaseModel):
    session_id: str
    decision: str = "approve"      # approve | reject  (edit/respond exist, unused here)
    reason: str = ""               # surfaced to the agent on reject


@app.post("/approve")
async def approve(req: ApprovalRequest):
    """Resume a conversation paused waiting for approval on a write.

    Nova is request/response with no UI, so approval is a second call rather than a
    button. `session_id` needs no lookup table: LangGraph keyed the paused state on
    thread_id == session_id in Redis, so replaying it is the whole mechanism.

    Rejecting does not error — the tool is skipped and the agent is told why, so it
    can answer the customer instead of failing.
    """
    trace_id = str(uuid.uuid4())
    started = time.monotonic()

    if req.decision not in ("approve", "reject"):
        return {"trace_id": trace_id, "error": "bad_decision",
                "detail": "decision must be 'approve' or 'reject'"}

    config = {
        "configurable": {"thread_id": req.session_id},
        "recursion_limit": RECURSION_LIMIT,
    }
    if "langfuse_handler" in state:
        config["callbacks"] = [state["langfuse_handler"]]
        config["metadata"] = {"langfuse_session_id": req.session_id,
                              "langfuse_tags": [AGENT_MODEL, f"decision:{req.decision}"],
                              "trace_id": trace_id}

    snapshot = await state["agent"].aget_state(config)
    if not snapshot or not snapshot.next:
        # `next` is empty when the graph is not paused. Distinguish "already resumed or
        # never paused" from a genuine failure — replaying an approval must not
        # silently run the transfer a second time.
        return {"trace_id": trace_id, "error": "nothing_pending",
                "detail": f"session {req.session_id} is not awaiting approval",
                "session_id": req.session_id}

    prior_len = len(snapshot.values.get("messages", []))

    decision: dict = {"type": req.decision}
    if req.decision == "reject" and req.reason:
        decision["message"] = req.reason

    try:
        import asyncio
        result = await asyncio.wait_for(
            state["agent"].ainvoke(Command(resume={"decisions": [decision]}), config=config),
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:
        REQUESTS.labels(status="error").inc()
        log.exception("trace_id=%s approve failed", trace_id)
        return {"trace_id": trace_id, "error": "error",
                "detail": f"{type(exc).__name__}: {exc}"[:500],
                "session_id": req.session_id}

    messages = result["messages"]
    turn = messages[prior_len:]

    tool_calls, tool_results = [], []
    for m in turn:
        for call in getattr(m, "tool_calls", []) or []:
            tool_calls.append({"tool": call["name"], "args": call["args"]})
        if getattr(m, "type", "") == "tool":
            tool_results.append({"tool": getattr(m, "name", "unknown"),
                                 "result": m.content if isinstance(m.content, str)
                                 else str(m.content)})

    answer = messages[-1].content
    if isinstance(answer, list):
        answer = " ".join(b.get("text", "") for b in answer if isinstance(b, dict))

    elapsed = time.monotonic() - started
    REQUESTS.labels(status="ok").inc()
    LATENCY.observe(elapsed)
    for c in tool_calls:
        TOOL_CALLS.labels(tool=c["tool"]).inc()

    log.info("trace_id=%s decision=%s tools=%s", trace_id, req.decision,
             [t["tool"] for t in tool_calls])

    return {
        "trace_id": trace_id,
        "session_id": req.session_id,
        "decision": req.decision,
        "answer": answer,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "latency_ms": round(elapsed * 1000),
        "model": AGENT_MODEL,
    }
