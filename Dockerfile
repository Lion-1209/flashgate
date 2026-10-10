# Glama registry build/check image: the MCP server must start and answer
# MCP introspection. It does NOT need hardware for that — tools only touch
# serial/ST-Link when actually called.
FROM python:3.12-slim

# serial/ST-Link tools are optional at runtime (only flash/verify touch
# them); declared so the registry build logs show what is available.
RUN pip install --no-cache-dir "flashgate[mcp]==0.10.0"

# The registry's introspection runs `docker run -i <image>` and speaks
# MCP over stdio: no arguments, no board, foreground process.
CMD ["flashgate-mcp"]
