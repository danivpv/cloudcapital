# ==============================================================================
# STAGE 1: THE BUILDER
# ==============================================================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_PROGRESS=1
ENV UV_LINK_MODE=copy

WORKDIR /app

# Dependency layer: only invalidated when pyproject.toml / uv.lock change.
# No RUN --mount cache mounts here on purpose: AWS SAM CLI builds images via
# the legacy docker builder (no BuildKit), which rejects mount syntax.
# Classic layer caching plus the slim .dockerignore give the same rebuild speed.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-editable --no-dev --no-default-groups --group economics

# ==============================================================================
# STAGE 2: FINAL RUNTIME
# ==============================================================================
FROM public.ecr.aws/lambda/python:3.12

# Third-party dependencies, installed as flat site-packages under the task root
COPY --from=builder /app/.venv/lib/python3.12/site-packages/ ${LAMBDA_TASK_ROOT}/

# Project code copied as the `src` package so relative imports
# (e.g. src.ai -> src.economics) resolve the same as local development.
COPY src/__init__.py ${LAMBDA_TASK_ROOT}/src/__init__.py
COPY src/economics/ ${LAMBDA_TASK_ROOT}/src/economics/

# Baked precomputed data lives inside the package
ENV CC_DATA_DIR=${LAMBDA_TASK_ROOT}/src/economics/data

CMD ["src.economics.handler.handler"]
