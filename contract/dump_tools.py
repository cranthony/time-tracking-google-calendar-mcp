"""Write the server's MCP tool list to contract/tools.json, exactly as
`tools/list` sends it: each tool's name, description, input schema and
output schema -- in the order they're sent, each schema's keys too.

That file is the contract the Rust port has to match -- what an MCP client
(Claude, the app) sees of this server -- and the Rust server sends it as
it is. tests/test_contract.py fails if the Python server ever drifts from
it. Rerun this after deliberately changing a tool's signature or
docstring:

    python contract/dump_tools.py

The list is taken from a running server's own answer (over stdio), not
from its models: the transport orders keys its own way, and that order is
part of what clients receive.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS_PATH = Path(__file__).resolve().parent / "tools.json"


def tool_list() -> list[dict]:
    """The tools a freshly started server lists, as it sends them."""
    env = {**os.environ, "MCP_TRANSPORT": "stdio", "CTT_HEALTH_WRITER": "off", "PYTHONIOENCODING": "utf-8"}
    env.setdefault("GOOGLE_CALENDAR_ID", "contract-dump")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "contract", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    try:
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        for line in process.stdout:
            message = json.loads(line)
            if message.get("id") == 2:
                return message["result"]["tools"]
    finally:
        process.kill()
        process.wait()
    raise RuntimeError("the server never answered tools/list")


def render(tools: list[dict]) -> str:
    return json.dumps(tools, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    TOOLS_PATH.write_text(render(tool_list()), encoding="utf-8", newline="\n")
    print(f"wrote {TOOLS_PATH}")
