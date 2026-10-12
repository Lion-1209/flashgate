# flashgate MCP server — registry build/introspection image.
#
# The registry (Glama) clones the repo, builds this Dockerfile in a
# sandbox, runs the image with `docker run -i`, and speaks MCP over
# stdio: initialize + tools/list must work WITHOUT any hardware or
# board profile (board_info reports honestly without one; hardware
# tools name their prerequisites when called).
#
# The install is pinned to the committed uv.lock (--frozen), so the
# image reproduces exactly what `uv sync` gives a local developer —
# mcp included (core dependency since 0.10.0).
FROM python:3.12-slim

# stdio JSON-RPC must not sit in a block buffer: registry clients read
# `docker run -i` stdout through a pipe (non-TTY), and an unflushed
# initialize response reads as a hang.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir uv \
    && uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH"

CMD ["flashgate-mcp"]
