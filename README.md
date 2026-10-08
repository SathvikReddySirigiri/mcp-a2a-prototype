# MCP + A2A Prototype: Radioligand Target Assessment

A learning project that shows how two agent protocols work together in one pipeline:

- **MCP (Model Context Protocol)** connects an agent to a tool or data source.
- **A2A (Agent2Agent protocol)** lets one agent send tasks to another agent.

The example domain is cancer biology. It answers one question:

> **Is this gene a good target for radioligand therapy?**

Radioligand therapy delivers a radioactive drug to tumor cells by binding a protein on their surface. A good target has three properties:

1. **High in the tumor.** The gene is strongly expressed in cancer cells.
2. **Low in healthy tissue.** Normal organs are spared.
3. **On the cell surface.** A drug in the bloodstream can reach it.

Give the project a gene and a cancer type (for example `MSLN PAAD`). It fetches evidence for each property from a public database, writes a short assessment, and waits for a person to accept, reject or revise it.

> This is a protocol-learning prototype, not a validated target-discovery tool. See [Limitations](#limitations).

---

## What it does, step by step

1. You send a request such as `MSLN in PAAD` to the **Ranking Agent**.
2. The Ranking Agent reads the **agent cards** of two other agents to find out what skills they offer.
3. It sends them three tasks **in parallel**:
   - **Expression Agent:** how high is MSLN in TCGA pancreatic tumors?
   - **Annotation Agent:** how high is MSLN in healthy pancreas (GTEx)?
   - **Annotation Agent:** where does the MSLN protein sit in the cell (Human Protein Atlas)?
4. Each agent starts its MCP server, calls the tool, and returns the result as an A2A artifact.
5. The Ranking Agent combines the three results into a **criterion-based assessment**. It labels tumor abundance, normal-tissue background and surface accessibility, and attaches each value, its source and its caveats.
6. The task pauses in the A2A **`input-required`** state. You reply:
   - `accept` completes the task with the assessment.
   - `reject` marks the task rejected.
   - `revise` re-runs the lookups on the same task.

---

## Architecture

```
 You (scripts/send_task.py)
        │  A2A: "MSLN PAAD"
        ▼
 ┌─────────────────────────────┐
 │ Ranking Agent       :8003   │  orchestrator + human review
 └──────┬───────────────┬──────┘
   A2A  │               │  A2A (parallel)
        ▼               ▼
 ┌──────────────┐  ┌──────────────────────────────┐
 │ Expression   │  │ Annotation Agent       :8002 │
 │ Agent  :8001 │  │ (routes by skill)            │
 └──────┬───────┘  └──────┬───────────────┬───────┘
   MCP  │ stdio      MCP  │ stdio    MCP  │ stdio
        ▼                 ▼               ▼
  gdc_server.py     gtex_server.py   hpa_server.py
  cBioPortal/TCGA   GTEx v10         Human Protein Atlas
```

There are three layers:

| Layer | Protocol | Role |
|---|---|---|
| MCP servers (`servers/`) | MCP over stdio | Each wraps one public API as one tool |
| Leaf agents (`agents/expression_agent`, `agents/annotation_agent`) | A2A server + MCP client | Turn an A2A task into an MCP tool call |
| Ranking agent (`agents/ranking_agent`) | A2A server + A2A client | Calls the other agents, builds the assessment, asks a human |

---

## Components

### MCP servers

| File | Tool | Source | Returns |
|---|---|---|---|
| `servers/gdc_server.py` | `get_expression(gene, cohort)` | TCGA via cBioPortal | Tumor mRNA stats: n, mean, median, IQR, range (RSEM) |
| `servers/gtex_server.py` | `get_normal_expression(gene, tissue)` | GTEx v10 | Median healthy-tissue expression (TPM) and sample count |
| `servers/hpa_server.py` | `get_localization(gene)` | Human Protein Atlas | Subcellular location, tissue specificity, membrane / secreted flags |

Supported TCGA cohorts are `PAAD` (pancreatic), `LUAD` (lung adenocarcinoma) and `BRCA` (breast). GTEx accepts any of its tissue IDs, for example `Pancreas`, `Lung` or `Breast_Mammary_Tissue`.

Each tool description states its own limitations. Those caveats are carried through the whole pipeline.

### A2A agents

| Agent | Port | Skills | Wraps |
|---|---|---|---|
| TCGA Expression Agent | 8001 | `get_expression` | `gdc_server.py` |
| Normal Tissue and Localization Agent | 8002 | `get_normal_expression`, `get_localization` | `gtex_server.py`, `hpa_server.py` |
| Radioligand Target Ranking Agent | 8003 | `score_target` | Calls agents on 8001 and 8002 |

Every agent publishes its card at `/.well-known/agent-card.json`. Requests can be free text (`MSLN PAAD`, `CEACAM5 in LUAD`) or JSON (`{"gene": "MSLN", "cohort": "PAAD"}`).

The Ranking Agent maps each cohort to a matching GTEx tissue:

| Cohort | GTEx tissue |
|---|---|
| PAAD | Pancreas |
| LUAD | Lung |
| BRCA | Breast_Mammary_Tissue |

### Client

`scripts/send_task.py` sends a message to any agent, prints the artifacts, and, if the task pauses in `input-required`, asks for a verdict on stdin and resumes the same task.

---

## The assessment

The Ranking Agent produces a **criterion-based reading, not a single score**. Each criterion gets a label, the value behind it, its source and a caveat note.

| Criterion | Based on | Label rule |
|---|---|---|
| Tumor abundance | TCGA median (RSEM) | high ≥ 1000, moderate ≥ 100, else low |
| Normal-tissue background | GTEx median (TPM) | low < 1, moderate < 10, else high |
| Surface accessibility | HPA annotation | plasma-membrane / membrane-associated / secreted / no membrane annotation |

Tumor RSEM and GTEx TPM are reported side by side and **never divided**, because they come from different normalization pipelines. The caveat notes are pulled from the peer agents' skill descriptions, so the limits of each data source travel with the data.

---

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
pip install "a2a-sdk>=1.1,<1.2"
```

Check that an MCP server works on its own:

```bash
python servers/gdc_server.py --selftest MSLN
python servers/gtex_server.py --selftest MSLN Pancreas
python servers/hpa_server.py --selftest MSLN
```

---

## Running the full pipeline

Run all commands from the repo root with the virtual environment active. Start each agent in its own terminal:

```bash
python -m agents.expression_agent     # http://127.0.0.1:8001
python -m agents.annotation_agent     # http://127.0.0.1:8002
python -m agents.ranking_agent        # http://127.0.0.1:8003
```

Start the Ranking Agent last, because it contacts the other two agents.

Confirm an agent is up:

```bash
curl http://127.0.0.1:8003/.well-known/agent-card.json
```

Send a task:

```bash
python scripts/send_task.py http://127.0.0.1:8003 "MSLN PAAD"
```

The script prints the assessment and asks:

```
Verdict (accept/reject/revise):
```

You can also call a single agent directly:

```bash
python scripts/send_task.py http://127.0.0.1:8001 "MSLN PAAD"
python scripts/send_task.py http://127.0.0.1:8002 "MSLN Pancreas"
python scripts/send_task.py http://127.0.0.1:8002 "MSLN localization"
```

---

## Using the MCP servers in an editor

Each MCP server can run on its own in an MCP client such as Cursor. Add to `~/.cursor/mcp.json`, using absolute paths:

```json
{
  "mcpServers": {
    "tcga-expression": {
      "command": "<repo>/.venv/Scripts/python.exe",
      "args": ["<repo>/servers/gdc_server.py"]
    },
    "gtex-normal": {
      "command": "<repo>/.venv/Scripts/python.exe",
      "args": ["<repo>/servers/gtex_server.py"]
    },
    "hpa-localization": {
      "command": "<repo>/.venv/Scripts/python.exe",
      "args": ["<repo>/servers/hpa_server.py"]
    }
  }
}
```

On macOS / Linux the interpreter path is `<repo>/.venv/bin/python`.

---

## Limitations

The prototype is built to show the protocol plumbing. It is not a real target-discovery system:

- **One gene at a time.** You supply the gene. It does not scan or rank many genes.
- **Summary statistics only.** It uses ready-made numbers from public APIs. No raw data, batch correction or differential-expression statistics.
- **Unmatched normals.** GTEx tissue comes from healthy post-mortem donors, not adjacent-normal tissue from the same patients. The comparison is directional only.
- **Different units.** Tumor values are RSEM and normal values are TPM, so no fold change is computed.
- **One surface source.** Localization comes only from HPA, whose annotations are antibody-derived or predicted. Membrane annotation does not prove a drug can reach the protein in the body.
- **Simple labels, no model.** The labels use fixed cutoffs. There is no scoring model, no language model and no learning.
- **No validation.** Results are not checked against a known list of targets.
- **Local only.** Agent addresses are hardcoded to `127.0.0.1`, and task state is kept in memory.

---

## Project status

- [x] Part 1: MCP server for TCGA tumor expression (cBioPortal)
- [x] Part 2: MCP servers for GTEx normal expression and HPA localization
- [x] Part 3: A2A agents
  - [x] Expression agent
  - [x] Annotation agent (GTEx + HPA)
  - [x] Ranking agent that calls the other two
- [ ] Part 4: Human-in-the-loop
  - [x] Pause in `input-required` for accept / reject / revise
  - [ ] Further review steps

---

## Repository layout

```
servers/
  gdc_server.py          MCP: TCGA tumor expression
  gtex_server.py         MCP: GTEx healthy-tissue expression
  hpa_server.py          MCP: HPA subcellular localization
agents/
  expression_agent/      A2A agent on :8001
  annotation_agent/      A2A agent on :8002
  ranking_agent/         A2A orchestrator on :8003
scripts/
  send_task.py           A2A client with interactive review
requirements.txt
```

---

## Data

All data comes from public APIs: TCGA through cBioPortal, GTEx and the Human Protein Atlas. No institutional or patient-identifiable data is used.
