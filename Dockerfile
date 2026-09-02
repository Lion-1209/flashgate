# Glama registry build/check image: the MCP server must start and answer
# MCP introspection. It does NOT need hardware for that — tools only touch
# serial/ST-Link when actually called.
FROM python:3.12-slim
RUN pip install --no-cache-dir "flashgate[mcp]"
CMD ["flashgate-mcp"]
