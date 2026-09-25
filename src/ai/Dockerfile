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
RUN uv sync --frozen --no-install-project --no-editable --no-dev --no-default-groups --group ai

# ==============================================================================
# STAGE 2: FINAL RUNTIME (Lambda Web Adapter — true response streaming)
# ==============================================================================
FROM public.ecr.aws/lambda/python:3.12

# AWS Lambda Web Adapter: bridges uvicorn (ASGI) to the Lambda Runtime API and
# implements the InvokeWithResponseStream wire format (metadata + 8 null bytes
# + chunked body) so API Gateway STREAM integrations can stream our SSE output.
# Bundled as an extension, not a layer, so the image is self-contained.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

# Third-party dependencies, installed as flat site-packages under the task root
COPY --from=builder /app/.venv/lib/python3.12/site-packages/ ${LAMBDA_TASK_ROOT}/

# Project code copied as the `src` package so relative imports
# (e.g. src.ai -> src.economics) resolve the same as local development.
COPY src/__init__.py ${LAMBDA_TASK_ROOT}/src/__init__.py
COPY src/ai/ ${LAMBDA_TASK_ROOT}/src/ai/
COPY src/economics/ ${LAMBDA_TASK_ROOT}/src/economics/

# Baked precomputed data lives inside the package
ENV CC_DATA_DIR=${LAMBDA_TASK_ROOT}/src/economics/data

# Web Adapter: serve the FastAPI app with uvicorn.
# AWS_LWA_INVOKE_MODE=response_streaming must be set ONLY when the function is
# invoked through InvokeWithResponseStream (API Gateway STREAM / function URLs);
# it is set in CDK so local SAM/docker runs stay buffered.
ENV PORT=8000 \
    AWS_LWA_INVOKE_MODE=buffered \
    AWS_LWA_READINESS_CHECK_PATH=/health

WORKDIR ${LAMBDA_TASK_ROOT}
# The adapter runs as an extension alongside the process below. Inside Lambda
# it intercepts the Runtime API and bridges requests to/from uvicorn's HTTP
# port; locally (no AWS_LAMBDA_RUNTIME_API) it idles and uvicorn just serves.
# lambda-entrypoint.sh requires a handler arg, so bypass it and exec uvicorn
# directly — the extension mechanism does not depend on the entrypoint.
ENTRYPOINT []
CMD ["python", "-m", "uvicorn", "src.ai.app:app", "--host", "0.0.0.0", "--port", "8000"]
