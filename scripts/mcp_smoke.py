"""MCP smoke test: spawn flashgate-mcp over stdio and call real tools.

Usage: python scripts/mcp_smoke.py [board.yaml]
"""

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BOARD = sys.argv[1] if len(sys.argv) > 1 else str(
    Path(__file__).resolve().parent.parent / "boards" / "apollo-h743.yaml"
)


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "flashgate.mcp_server", "--board", BOARD],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("tools:", names)

            for tool, args in [
                ("board_info", {}),
                ("doctor", {}),
                ("console_send", {"line": "ping", "wait_s": 1.5}),
            ]:
                result = await session.call_tool(tool, args)
                is_err = getattr(result, "is_error", getattr(result, "isError", False))
                text = result.content[0].text if result.content else "(empty)"
                print(f"\n=== {tool} (error={is_err}) ===")
                print(text[:800])

            return 0


sys.exit(asyncio.run(main()))
