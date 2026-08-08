# Pinned to 3.13 rather than tracking the host's Python. The target VPS runs
# 3.14, where pydantic-core and rapidfuzz may have no prebuilt wheel yet and pip
# would fall back to compiling Rust and C++ from source. Pinning the runtime is
# most of the reason to containerise this at all.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Dependencies in their own layer: editing a source file must not reinstall
# pydantic and rapidfuzz. The stub package satisfies setuptools' package
# discovery before the real source exists.
COPY pyproject.toml ./
RUN mkdir -p carodi \
    && touch carodi/__init__.py \
    && pip install -e ".[llm]" \
    && rm carodi/__init__.py

COPY carodi/ ./carodi/

# No USER directive on purpose: compose passes the host's uid:gid so files
# written into the bind-mounted data/ are owned by you rather than by root.
# /app itself is only ever read.

ENTRYPOINT ["carodi"]
CMD ["run"]
