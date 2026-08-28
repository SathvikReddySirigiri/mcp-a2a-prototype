# MCP + A2A Prototype

Learning project exploring how the Model Context Protocol (MCP) and
Agent2Agent (A2A) protocol compose in a multi-agent pipeline.

Domain: target discovery over public TCGA expression data.

## Status

- [x] Part 1 — MCP server for TCGA expression lookup (cBioPortal)
- [ ] Part 2 — GTEx normal baseline, HPA localization
- [ ] Part 3 — Split into A2A agents
- [ ] Part 4 — Human-in-the-loop via input-required

## Setup

    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    python servers/gdc_server.py --selftest MSLN

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