# Cloud Capital — Project Ledger

This document is the single source of truth for what was built, why each decision was made, and what remains open. It consolidates the PRD and all six challenge answers.

---

## Product thesis

**Your savings are real, they come from your usage, and here is exactly why we keep what we keep.**

The application has three goals:

1. **At a glance** — show the signed, customer-kept savings number, its denominator (covered compute spend), and unused-commitment waste. Thirty seconds, one screen.
2. **Verify** — let a customer take that number apart by their own dimensions and follow it to the small set of usage lines behind it.
3. **Trust the split** — show customer share, risk reserve, and profit; frame reserve as the cost of the guarantee we carry, not a hidden deduction.

---

## The six challenges

### 1. Stand up the surface ✅

**What the customer gets:** A landing screen that states what we propose and what the customer keeps in under thirty seconds. The headline is the customer's guaranteed share, not flattering gross savings. It states the denominator (covered compute spend) and calls out unused commitment.

**How it works:** `page.tsx` requests the proposal and economics from the backend. The backend composes the data store, an in-process economics client, and the isolated economics domain mounted at `/econ`. The surface can move the economics behind a separate HTTP service later without touching its own code.

**Key decisions:**

- We headline *net* savings, not gross, because gross excludes real committed cost in under-utilized hours and overstates the customer's outcome.
- Waste is shown even though it makes the product number smaller. Hiding it would be a dark pattern.
- The economics math lives in its own bounded context so surface changes can never accidentally alter the financial logic.
- The rate denominator is covered on-demand compute spend, never the whole bill.

---

### 2. Explore the spend ✅

**What the customer gets:** An explorer that answers three increasingly detailed questions — what costs the most, what changed, and what is behind this number — by slicing 22.4 million hourly billing rows through a fast, responsive UI.

**How it works:** The raw data never leaves the backend. DuckDB reads the parquet file. Per-dimension monthly summaries (one per grouping dimension) are built lazily on first use and reused for every subsequent request. A drill returns only the top 30 raw rows behind a selected bar. An outer join is used for period comparison so new and retired categories are preserved — an inner join would silently drop anything that appeared in only one of the two months.

Savings metrics (covered spend, commitment cost, gross savings) are computed by the economics service with its proprietary allocation rules, then returned as a compact per-dimension per-month rollup. The dashboard never receives the full ~1 million per-line impact rows.

**Key decisions:**

- Comparison is optional. A current-period breakdown is the simplest first question; comparison is valuable only when the user is asking why something moved.
- We do not precompute every possible dimension combination. Multi-filter questions beyond the cached dice hit a bounded DuckDB query instead.
- The service calculates savings, not the frontend, because it owns the allocation rules. Duplicating that logic on the frontend risks a different financial answer.

---

### 3. The honest split ✅

**What the customer gets:** A split page that stages the deal in the right order — customer share first, then the net pool, then reserve and profit — with a plain explanation of why each part exists.

**How it works:**

- Net pool = gross savings − unused commitment
- Customer savings = the guaranteed part of that pool
- Profit = a visible, fixed 10% of net savings
- Reserve = cost-of-risk percentage of net savings; it pays for the downside we carry when actual usage does not sustain the commitment
- Protection = the fraction of historical periods where discounted spend supported the commitment level; evidence, not a forecast

The commitment slider changes a draft level only. Hitting "Evaluate this level" triggers one call to `/econ/economics`. Every changed level is labelled as a historical scenario, not a quote or a prediction.

**Dark patterns declined:** profit is not hidden, reserve is not relabelled as a customer benefit, waste is not removed, and historical sensitivity is not sold as a forecast. Those choices would improve conversion at the cost of the trust the product claims to earn.

---

### 4. Ask and explain ✅

**What the customer gets:** A right-side drawer — "Ask about this view" — on every screen. It takes a free-text question, uses the current screen as context, and streams a text explanation with the exact SQL it ran.

**How it works:**

```
Question + screen context (dimension, period, metric, selected row)
  → DSPy creates a strict plan of one or two allowed queries
  → backend compiles SQL and runs it against DuckDB
  → drawer streams: query spec → data → headline → prose → meta
  → "Show SQL used" exposes the compiled query for transparency
```

The DSPy signature selects only from an approved vocabulary: grouping dimension, metric, comparison flag, selection filter, and a row cap of 12. It cannot invent dates, inject filter values, write arbitrary SQL, or plan a third query. The model never receives raw billing rows.

**Why DSPy and LiteLLM:** DSPy owns the typed planning program. LiteLLM/OpenRouter is the provider and cost adapter underneath it. This creates a production path to evaluation sets and DSPy optimization. There is intentionally no optimizer yet because there is no trusted evaluation set.

---

### 5. The cost budget ✅

**Target:** under $0.01 per novel question and under 2 seconds to first structured answer at 10–100× raw usage volume.

**Measured live path (one observation, not a p95 claim):**

| Stage | Tokens | Cost | Latency |
|---|---:|---:|---:|
| DSPy query plan | 478 | $0.000348 | 2.158s |
| Streamed prose | 805 | $0.000343 | 0.920s |
| **Total** | **1,283** | **$0.000691** | **3.119s** |

This is 18.7× below the cost budget. The 3.1s total is not acceptable as a completed-answer p95; streaming lets the first structured answer (chart/headline) appear before prose finishes.

**Why cost does not scale with data:** The planner receives screen context; the final model receives at most two 12-row query results. LLM tokens and cost are fixed regardless of how large the customer bill grows.

**What does scale with data:** The first dice build and the allocation run over the raw source parquet. They are cached per dimension/proposal. At 10–100× data, the right answer is partitioned parquet and persisted dice refresh jobs, not more raw data in the prompt.

**Open:** first-event latency (chart visible before prose completes) is not yet instrumented. Needed before a 2-second p95 claim can be made.

---

### 6. The misleading view ✅

**The problem:** June 2026 contains data only through June 24. A comparison chart that treats it as a complete month would show spend and savings apparently falling — technically correct at every plotted point, but financially misleading.

**The fix:** `Store.periods()` derives completeness from the actual final timestamp in the data. The explorer defaults to the two latest *complete* months. Partial months are labelled in the period selector. A warning banner appears if the user explicitly selects one.

**What honesty costs:** The most recent data is slightly less convenient to compare. That is preferable to a clean, impressive, false period-over-period story.

**Other traps of the same kind:** gross instead of net savings, hidden waste, rates without denominators, dropped null buckets. The landing and explorer address each of these by showing waste, naming the denominator, and using an outer join for comparison.

---

## Architecture summary

| Layer | Technology | Rationale |
|---|---|---|
| Data | DuckDB over parquet | In-process, column-oriented, fast GROUP BY; no server to manage |
| Per-dim summaries | Lazy dice tables in DuckDB | One parquet scan per dimension; sub-second lookups for all subsequent slice-and-dice drills |
| Economics | Isolated bounded context at `/econ` | Settled math is kept separate so surface changes cannot alter it |
| Savings rollup | Service-side aggregation | ~1M impact rows stay inside the service; dashboard receives KB not GB |
| LLM | DSPy + LiteLLM/OpenRouter | Typed query planning, inspectable SQL, provider-agnostic cost adapter |
| Frontend | Next.js on Amplify | Server-side proxy injects the shared secret header; client never holds the secret |
| Backend | FastAPI + Fargate (single task) | Modular monolith; economics bounded context is in-process today, behind HTTP later |
| Auth | ALB header rule (`x-demo-auth`) | No public route reaches the API without the secret; 403 is the default response |

---

## Understanding ledger

### Verified personally

- The settled economics: hourly highest-discount-first allocation; `net = gross − waste`; customer/reserve/profit split; cost-of-risk inputs. Can reconstruct from scratch.
- Data shape: 22,363,148 rows, 1,004,965 eligible compute rows, 64 services, 42 accounts, 12 periods; June 2026 is partial.
- The live LLM route: exercised with the OpenRouter key and measured per-stage token/cost/latency.

### Delegated and reviewed

FastAPI/Next.js wiring, SSE plumbing, CSS, routine DuckDB implementation, and all AWS CDK Infrastructure-as-Code. Ownership of screen thesis, data boundary, query vocabulary, aggregation logic, LLM budget, and the AWS deployment architecture (ECS/ALB and Amplify Next.js Web Compute). Generated wiring and infrastructure were reviewed against those constraints.

### Open items

- Prose grounding is constrained by prompt and bounded data, but numeric post-checking against the structured payload is not yet enforced.
- The 10–100× cost/latency projection and EC2 sizing are not measured. They are estimates.
- First-event latency (chart visible before prose completes) is not instrumented; needed before a 2-second p95 claim.
