# time-tracking-google-calendar-mcp
A Model Context Protocol (MCP) for managing a Google calendar with a set of non-overlapping events representing one person's time

## Setup

Create and activate a virtual environment, then install dependencies.

**macOS/Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Running the server

With the virtual environment activated, run the server directly:

```bash
python server.py
```

Or launch it with the MCP Inspector for interactive testing:

```bash
mcp dev server.py
```

`server.py` currently contains a minimal scaffold (an `add` tool and a `greeting` resource) from the [MCP Python SDK quickstart](https://py.sdk.modelcontextprotocol.io/), ready to be extended with Google Calendar-backed tools.
