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

`mcp dev` requires two tools outside the Python virtual environment: **Node.js/npx** (to launch the Inspector UI itself) and **uv** (the Inspector uses it to run the server in an isolated, on-the-fly environment). Neither is something pip/venv can provide — a venv only manages Python packages already on your machine, not other language runtimes or tools.

**Installing Node.js (provides `npx`):**
- **macOS:** `brew install node`
- **Windows:** `winget install OpenJS.NodeJS.LTS` (or download the installer from [nodejs.org](https://nodejs.org))
- **Linux:** use your distro's package manager (e.g. `sudo apt install nodejs npm`) or install via [nvm](https://github.com/nvm-sh/nvm)

**Installing uv:**
- **macOS/Linux:** `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Windows:** `winget install astral-sh.uv` (or `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`)

After installing, restart your terminal and verify with:

```bash
npx --version
uv --version
```

If you only need to run the server (not the Inspector), `python server.py` works without Node.js or uv.

`server.py` currently contains a minimal scaffold (an `add` tool and a `greeting` resource) from the [MCP Python SDK quickstart](https://py.sdk.modelcontextprotocol.io/), ready to be extended with Google Calendar-backed tools.
