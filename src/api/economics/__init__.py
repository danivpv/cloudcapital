"""The economics service, migrated into this repo and kept isolated.

Package layout (SOLID on purpose):

- ``domain.py`` — pure math, no HTTP. The settled allocation logic, ported
  verbatim from the provided service so the numbers cannot drift.
- ``app.py`` — the only HTTP adapter: a FastAPI ``APIRouter`` that mounts under
  the ``/econ`` prefix, holds the one shared DuckDB connection + its lock, and
  caches results per proposal.

The surface talks to this router over HTTP at ``/econ`` — an intentional
boundary: today it is mounted in-process, tomorrow it can be its own process
or image behind a load balancer by changing one URL.
"""
