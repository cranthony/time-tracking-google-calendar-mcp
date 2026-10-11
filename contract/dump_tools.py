"""Write the server's MCP tool list to contract/tools.json, exactly as
`tools/list` sends it: each tool's name, description, input schema and
output schema.

That file is the contract the Rust port has to match -- what an MCP client
(Claude, the app) sees of this server -- and tests/test_contract.py fails
if the Python server ever drifts from it. Rerun this after deliberately
changing a tool's signature or docstring:

    python contract/dump_tools.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS_PATH = Path(__file__).resolve().parent / "tools.json"


def tool_list() -> list[dict]:
    """The server's tools, as `tools/list` puts them on the wire, sorted
    by name so the file's order doesn't depend on registration order."""
    sys.path.insert(0, str(ROOT))
    import server

    tools = asyncio.run(server.mcp.list_tools())
    wire = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
    return sorted(wire, key=lambda tool: tool["name"])


def render(tools: list[dict]) -> str:
    return json.dumps(tools, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


if __name__ == "__main__":
    TOOLS_PATH.write_text(render(tool_list()), encoding="utf-8", newline="\n")
    print(f"wrote {TOOLS_PATH}")
