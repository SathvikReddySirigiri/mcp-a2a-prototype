# MCP + A2A Prototype

Learning project exploring how the Model Context Protocol (MCP) and
Agent2Agent (A2A) protocol compose in a multi-agent pipeline.

Domain: target discovery over public TCGA expression data.

## Status

- [x] Part 1 — MCP server for TCGA expression lookup (cBioPortal)
- [x] Part 2 — GTEx normal baseline, HPA localization
- [ ] Part 3 — Split into A2A agents
  - [x] Expression agent (wraps `servers/gdc_server.py`)
- [ ] Part 4 — Human-in-the-loop via input-required

## Setup

    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    python servers/gdc_server.py --selftest MSLN

## A2A expression agent

Wraps the TCGA expression MCP server as an A2A agent (a2a-sdk 1.1.x).
The executor talks to `servers/gdc_server.py` as an MCP client over stdio
and returns the tool result as a task artifact.

Start (from the repo root, with the venv active):

    python -m agents.expression_agent

The agent listens on `http://127.0.0.1:8001`. Verify the agent card at the
standard well-known path:

    curl http://127.0.0.1:8001/.well-known/agent-card.json

PowerShell:

    Invoke-RestMethod http://127.0.0.1:8001/.well-known/agent-card.json

You should see name `TCGA Expression Agent`, a JSON-RPC interface on port
8001, and one skill (`get_expression`) for TCGA tumor expression lookup.

## MCP client config

Add to `~/.cursor/mcp.json`, using absolute paths:

    {
      "mcpServers": {
        "tcga-expression": {
          "command": "<repo>/.venv/Scripts/python.exe",
          "args": ["<repo>/servers/gdc_server.py"]
        }
      }
    }

## Data

Public TCGA data via the cBioPortal API. No institutional or patient data.
