# ── Profile ───────────────────────────────────────────────────────────────────
# Override on the CLI: make deploy AWS_PROFILE=production
AWS_PROFILE ?= default

# Export so all child processes (cdk, aws cli) inherit the profile
export AWS_PROFILE

STACK ?= CloudCapitalStack

.PHONY: lint type-check test install backend frontend dev \
	docker-build docker-run synth deploy destroy clean

# ── Quality ───────────────────────────────────────────────────────────────────

lint:
	uv run ruff check --fix
	uv run ruff format

type-check:
	uv run ty check src app.py --verbose

test:
	uv run pytest --ignore=cdk.out

# ── Local development ─────────────────────────────────────────────────────────

install:
	uv sync
	cd src/frontend && npm install
	cd src/frontend && npm approve-scripts unrs-resolver || true

install-backend:
	uv sync

install-frontend:
	cd src/frontend && npm install
	cd src/frontend && npm approve-scripts unrs-resolver || true

backend:
	uv run uvicorn src.api.main:app --reload --port 8001

frontend:
	cd src/frontend && bun run dev

# Runs backend (port 8001) + frontend (port 3000) in parallel.
dev:
	$(MAKE) -j2 backend frontend

# ── Container ─────────────────────────────────────────────────────────────────

docker-build:
	docker build -f src/api/Dockerfile -t cloudcapital/api:latest .

# Quick smoke-test: mounts local parquet over the baked copy.
# Passes CC_OPENROUTER_API_KEY from the host env (export it first).
docker-run:
	docker run --rm -p 8080:8080 \
		-v "$(CURDIR)/src/data.parquet:/app/data:ro" \
		-e CC_OPENROUTER_API_KEY \
		cloudcapital/api:latest

# ── CDK ───────────────────────────────────────────────────────────────────────
# Pass CloudFormation inputs as, for example:
# make deploy CDK_PARAMETERS="--parameters CloudCapitalStack:FrontendRepository=https://github.com/org/repo --parameters CloudCapitalStack:ApiDomainName=api.example.com"
CDK_PARAMETERS ?=

synth:
	uv run cdk synth $(STACK) --profile $(AWS_PROFILE) $(CDK_PARAMETERS)

deploy:
	uv run cdk deploy $(STACK) --require-approval never --profile $(AWS_PROFILE) $(CDK_PARAMETERS)

destroy:
	uv run cdk destroy $(STACK) --force --profile $(AWS_PROFILE) $(CDK_PARAMETERS)

# ── Cleanup ───────────────────────────────────────────────────────────────────
clean:
	rm -rf cdk.out .cache src/.cache src/frontend/.next src/frontend/out
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
