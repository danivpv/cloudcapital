# Cloud Capital : Project Ledger

This document is the single source of truth for what was built, why each decision was made, and what remains open. It consolidates the PRD and all six challenge answers.

---

## Product thesis

The application has three goals:

1. **Show savings immediately** : show the signed, customer-kept savings number, its true denominator, and unused-commitment waste within 30 seconds on a single screen.
2. **Progressive disclosure** : let a customer take that number apart across their own dimensions through an interactive dashboard or a transparent natural-language query interface.
3. **Clarity on debt meeting** : low traffic, zero regressions, professional financial aesthetic, and an extensible architecture ready for team ownership.

---

## The six challenges

### 1. Stand up the surface

#### System shape and architectural decisions

**Monorepo and the AI token economy spectrum:**
We structured the project as a unified monorepo containing AWS CDK infrastructure code, Next.js frontend, Python FastAPI backend, and a centralized `Makefile` defining standard developer workflows (`make dev`, `make test`, `make deploy`).
- *Industry alignment:* Follows the [AWS CDK recommended project structure for Python applications](https://aws.amazon.com/es/blogs/developer/recommended-aws-cdk-project-structure-for-python-applications/) to eliminate cross-repo coordination friction and provide a single source of truth for all entrypoints.
- *The AI token economy spectrum:* In agentic workflows, choosing a technology stack is an explicit tradeoff between agent token consumption and user presentation quality:
  - *Low-code Python frameworks (Streamlit, Gradio):* Agent token spend is low, but the UI is constrained, layouts are rigid, and the surface looks like an internal data science prototype rather than an enterprise financial product.
  - *Heavy enterprise frameworks (Angular):* Enforces rigid multi-file separation (HTML, CSS, TypeScript, module configs per route), resulting in high agent token consumption and slower iteration velocity.
  - *The sweet spot (Next.js + React):* Aligns with the [official React recommendation to build with a production framework](https://react.dev/learn/build-a-react-app-from-scratch). It balances high-end financial visual polish with high agent code-generation ergonomics, delivering native server-side API proxying and edge deployment on AWS Amplify Web Compute.
  - *UI decisions:* We headline *net* savings, not gross, because gross excludes real committed cost in under-utilized hours and overstates the customer's outcome. Waste is shown even though it makes the product number smaller. Hiding it would be a dark pattern. The rate denominator is explicitly covered on-demand compute spend, never the whole bill.

**Frontend security and proxy boundary:**
The Next.js App Router serves as the customer-facing presentation layer and security boundary:
- *Internal protection:* Server-side API route proxying (`/api/backend/*`) injects the shared `x-demo-auth` token server-to-server. The backend ALB rejects any unauthenticated direct traffic with `403 Forbidden`, keeping the core data and AI services hidden from direct internet exposure.
- *Security debt and public hardening:* While the internal backend is protected from direct internet abuse, the public Amplify Next.js proxy itself is currently exposed. In production, this surface requires AWS WAF (Web Application Firewall) on CloudFront/Amplify for bot control and IP rate limiting, alongside Amazon Cognito or OAuth/OIDC JWT verification in Next.js middleware before proxying requests.

**Backend architecture, teamwork, and intentional technical debt:**
For this initial 5-hour milestone, we deployed a modular FastAPI monolith running inside a single AWS ECS Fargate container. It bundles the AI agent, DuckDB analytical engine, API security header validation, and economics service together.
- *Why this shape:* Running a single Fargate container and Application Load Balancer minimized cloud infrastructure spend and eliminated distributed orchestration overhead, enabling end-to-end verification within hours.
- *The technical debt:* The monolith couples CPU- and memory-intensive DuckDB columnar scans with latency-sensitive API proxying and streaming AI agent routes. Under multi-tenant load, heavy analytical queries will degrade chat latency or trigger container OOM restarts.
- *The production target:* Decouple into three independently scalable services:
  1. *API Gateway / Edge Proxy:* Lightweight container handling authentication, rate limiting, and client sessions. The dashboard is 99% reads and aggregates, while writes are restricted to conversational agent session persistence.
  2. *Economics & Analytical Data Service:* High-memory container cluster running DuckDB over partitioned Parquet on S3, isolated from web traffic.
  3. *AI Agent Service:* Async worker container with orchestration decoupled into a resilient workflow engine like Temporal, tool execution standardized via Model Context Protocol (MCP) servers, and dedicated workers for LLM token streaming.
  4. *Cross-functional alignment:* Proactive teamwork with product and platform stakeholders to gather concrete requirements on costs, traffic spikes, latency budgets, and microservice ownership.
  5. *Team ownership and packaging:* Maintain domain boundaries via GitHub CODEOWNERS inside the monorepo for rapid atomic releases. If the quantitative risk engineering team requires repository isolation, advocate packaging their financial math as a versioned private Python library on an internal PyPI index (such as AWS CodeArtifact) rather than operating an uncoordinated distributed microservice.

#### Execution delegated to AI and tooling

**SOLID design and overengineering debt:**
To keep the company's delicate financial logic intact without introducing bugs, we encapsulated the original economics code inside a Bounded Context (`src/api/economics/`) and placed an adapter layer with the Dependency Inversion Principle (`src/api/data/economics.py`) between the data store and the domain. While this preserved domain integrity and satisfied SOLID principles, it created an awkward, layered codebase for a small application. Under team review, we would treat this adapter layer as overengineering technical debt to be streamlined directly with the economics service maintainers.

**Honesty on AI tooling:**
I wanted to test DeepSeek v3/v4 to explore the aesthetic capabilities of modern chinese open-weights models. While the models demonstrated impressive design taste and clean boilerplate generation, extreme inference latency (often exceeding 20 minutes per prompt or hanging indefinitely) threatened the delivery deadline. After about 2 hours I pivoted to frontier GPT-Terra to finish and harden the implementation on schedule.

---

### 2. Explore the spend

#### System shape and architectural decisions

**The data boundary:**
The raw usage firehose (22.4 million rows, 357 MB in `candidate_dataset.parquet`) never leaves the backend. All heavy analytical operations are executed directly inside DuckDB, returning small, pre-aggregated payloads to the frontend.

**Precompute versus on-demand (The slice-and-dice line):**
A core challenge of OLAP over 22.4M rows is avoiding both combinatorial explosion (pre-aggregating every multi-dimension filter combination) and query latency (scanning the full dataset on every click):
- *The precompute line (Lazy Dice):* We build single-dimension monthly summaries (`dice_{dim}`) across the 6 primary dimensions (`service`, `account`, `region`, `usage_kind`, `instance_type`, `commitment_scope`). These six tables cover the vast majority of visual breakdowns, compressing 357 MB of raw data into a 6.5 MB in-memory DuckDB database served in under 1 millisecond.
- *The on-demand line:* Complex multi-filter intersections beyond the standard dice tables execute a bounded DuckDB query pushing down period filters directly to Parquet row groups with a hard limit (`top_n=50`, capped at 1,000 by config), keeping query runtimes sub-second.
- *Startup warm-up strategy:* On application launch, the store connects eagerly to the Parquet file. The background `/warm` endpoint computes the commitment allocation once and materializes all 6 dimension dice tables in a single pass. If a user requests a dimension before `/warm` finishes, DuckDB builds that specific table lazily on first access and caches it permanently.

**Reshaping the economics service:**
In the provided challenge specification, the `/impact` endpoint returned every single eligible usage line across the entire window (1,004,965 rows). Serializing 1 million JSON rows over HTTP crashed browser memory and caused massive network latency.
- *The refactor (`impact_dice`):* We preserved the original financial allocation math 100% untouched in `src/api/economics/domain.py`, but refactored the presentation boundary. DuckDB aggregates the allocation results directly at the source via `GROUP BY dim_value, billing_period`, returning ~700 compact rows per dimension instead of 1M raw rows.

**Horizontal scaling and caching roadmap:**
- *DuckDB on S3 versus Redshift:* Redshift provisioned clusters are excessive and costly ($1,000s/month base fee) for a FinOps dashboard where data updates on an hourly or daily batch schedule. In production, DuckDB streams partitioned Parquet directly from Amazon S3 using projection pushdown (reading only requested columns) and predicate pushdown (skipping historical month row groups).
- *Read-heavy caching (ElastiCache):* Because dashboard traffic is predominantly reads and aggregations, future horizontal scaling across multiple ECS Fargate tasks can place an Amazon ElastiCache (Redis) cluster in front of the data service, caching the 6.5 MB dice tables to scale read throughput without duplicating DuckDB CPU aggregation overhead across instances.

#### Execution delegated to AI and tooling

**SOLID design and adapter overengineering debt:**
Encapsulating the economics domain through `InProcessEconomicsClient` and the `EconomicsGateway` protocol in `src/api/data/economics.py` kept the financial code safe from accidental regressions during the 5-hour sprint. However, this remains intentional overengineering debt to be simplified into a direct service boundary in collaboration with the economics service team.

**Implementation details delegated:**
- *Outer joins for period comparisons:* When comparing two billing periods, we execute an `OUTER JOIN`. A standard `INNER JOIN` would silently drop new services launched in period B or retired services removed after period A, falsifying the period-over-period trend.
- *Bounded drill-downs:* Selecting a bar in the UI returns only the top 30 raw billing rows behind that specific aggregation, allowing the user to verify the underlying data without transferring millions of rows.

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

### 4. Ask and explain

#### System shape and architectural decisions

**The intelligence layer and bounded DSPy query planning:**
The assistant drawer ("Ask about this view") allows plain-English interrogation of the active screen without ever handing raw usage rows to an LLM:
```
Question + screen context (dimension, period, metric, selected row)
  -> DSPy compiles a strictly typed plan of 1 or 2 allowed analytical queries
  -> DuckDB executes compiled SQL over local parquet/dice tables
  -> SSE stream emits: query_spec -> chart_data -> headline -> prose -> meta ledger
```
- *Strict typed vocabulary:* The DSPy signature (`PlanInvestigation`) selects only from allowed dimensions (`service`, `account`, `region`, etc.), metrics (`on_demand_cost`, `amortized_cost`), and enforces a strict row limit (`top_n <= 12`). It cannot invent dates, hallucinate table names, or generate unconstrained SQL.
- *DSPy and the LLMOps roadmap:* DSPy was chosen over raw prompt strings to establish a production path toward mature LLMOps. By defining typed program signatures, prompts become optimizable code integrated with MLflow for experiment tracking, prompt versioning, automated metric evaluation, and safe regression testing.
- *Hard guardrails and token budget envelope:* Enforced in `src/api/intelligence/llm.py` via a strict `Budget` envelope:
  - Max agent loop steps: 4.
  - Per-step token caps: 200 tokens for routing, 600 for query synthesis, 400 for prose generation.
  - Hard query ceiling: $0.01 max cost, 1,500 total tokens.
  - TTLT (worst case): 0.66s + 1500 / 102 = 15.4 seconds. https://openrouter.ai/google/gemini-2.5-flash?endpoint=d9b81424-1623-45ef-aa8c-c8df3357c495#providers
  - TTFT: 0.67s 
  - Live cost measured: $0.000691 per query (18.7x below budget).
- *Trust via consistency and SQL transparency:* The agent queries the exact same DuckDB analytical store as the visual dashboard. The drawer provides a "Show SQL used" toggle displaying the compiled query, ensuring mathematical parity between the charts and the streamed explanation.

#### Execution delegated to AI and debt

**Agent overengineering debt and simplification path:**
The intelligence directory (`src/api/intelligence/`) contains both a legacy rule-based router (`pipeline.py`) and the DSPy agent (`agent.py`). This dual scaffolding is overengineering debt resulting from deepseek ignoring my instruction to use dspy and instead using LiteLLM for rapid prototyping. Later I insisted but never deleted legacy implementation. In production, this can be drastically simplified by standardizing purely on DSPy or native provider tool-calling, eliminating redundant abstraction wrappers.

**Missing conversational session memory:**
The current implementation is stateless (each question is evaluated against the immediate screen context). A production rollout requires persistent session memory implemented with Amazon DynamoDB or PostgreSQL to support multi-turn conversational follow-ups.

**gemini 2.5 flash:** It worked but I should really update to latest gen fast model and retest

---

### 5. The cost budget

**Target:** Under $0.01 per novel question and under 2 seconds to first structured answer at 10 to 100x raw usage volume.

**Measured live path (one observation, not a p95 claim):**

| Stage | Tokens | Cost | Latency |
|---|---:|---:|---:|
| DSPy query plan | 478 | $0.000348 | 2.158s |
| Streamed prose | 805 | $0.000343 | 0.920s |
| **Total** | **1,283** | **$0.000691** | **3.119s** |

- *Why cost does not scale with data:* The planner receives screen context; the synthesis model receives at most two 12-row query results. LLM tokens and cost are O(1) regardless of customer bill size.
- *What does scale with data:* First-time dice builds over raw Parquet. At 10 to 100x data, the solution is partitioned Parquet on S3 and scheduled refresh jobs, never feeding raw data into LLM context.


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

