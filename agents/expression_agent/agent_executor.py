"""A2A executor that looks up TCGA tumor expression via the MCP stdio server."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
GDC_SERVER = REPO_ROOT / "servers" / "gdc_server.py"
MCP_TOOL = "get_expression"
SUPPORTED_COHORTS = ("PAAD", "LUAD", "BRCA")
_SKIP_TOKENS = {
    "IN",
    "FOR",
    "OF",
    "THE",
    "A",
    "AN",
    "TCGA",
    "EXPRESSION",
    "LOOKUP",
    "LOOK",
    "UP",
    "GENE",
    "COHORT",
    "TUMOR",
    "TUMOUR",
    "MRNA",
}


def parse_expression_query(text: str) -> tuple[str, str]:
    """Extract (gene, cohort) from free text or a JSON object."""
    raw = text.strip()
    if not raw:
        raise ValueError(
            "Provide a HUGO gene symbol, optionally with a cohort "
            f"({', '.join(SUPPORTED_COHORTS)}). Example: MSLN PAAD"
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict) and payload.get("gene"):
        gene = str(payload["gene"]).strip().upper()
        cohort = str(payload.get("cohort", "PAAD")).strip().upper() or "PAAD"
        return gene, cohort

    tokens = [tok.strip(".,;:()[]{}\"'") for tok in raw.replace(",", " ").split()]
    tokens = [tok for tok in tokens if tok]
    cohort = "PAAD"
    gene: str | None = None
    for tok in tokens:
        upper = tok.upper()
        if upper in SUPPORTED_COHORTS:
            cohort = upper
        elif upper in _SKIP_TOKENS:
            continue
        elif gene is None and upper.isalnum():
            gene = upper

    if not gene:
        raise ValueError(
            "Could not find a gene symbol in the request. "
            f"Try 'MSLN' or 'MSLN PAAD'. Cohorts: {', '.join(SUPPORTED_COHORTS)}"
        )
    return gene, cohort


def _tool_result_text(result: object) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    output = "\n".join(parts) if parts else str(result)
    is_error = bool(
        getattr(result, "isError", False) or getattr(result, "is_error", False)
    )
    if is_error:
        raise RuntimeError(output)
    return output


async def call_get_expression(gene: str, cohort: str) -> str:
    """Call `get_expression` on the GDC MCP server over stdio."""
    if not GDC_SERVER.is_file():
        raise FileNotFoundError(f"MCP server not found: {GDC_SERVER}")

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(GDC_SERVER)],
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                MCP_TOOL,
                {"gene": gene, "cohort": cohort},
            )
            return _tool_result_text(result)


class ExpressionAgentExecutor(AgentExecutor):
    """Runs TCGA expression lookup as an A2A task and returns an artifact."""

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        if context.current_task:
            task = context.current_task
        elif context.message:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        else:
            raise ValueError("Request is missing a user message.")

        updater = TaskUpdater(
            event_queue=event_queue, task_id=task.id, context_id=task.context_id
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Looking up TCGA tumor expression..."),
        )

        try:
            query = get_message_text(context.message) if context.message else ""
            gene, cohort = parse_expression_query(query)
            logger.info("MCP get_expression gene=%s cohort=%s", gene, cohort)
            result = await call_get_expression(gene, cohort)
        except Exception as exc:
            logger.exception("Expression lookup failed")
            await updater.failed(message=new_text_message(str(exc)))
            return

        await updater.add_artifact(
            parts=[new_text_part(text=result, media_type="text/plain")],
            name="tcga_expression",
            last_chunk=True,
        )
        await updater.complete(
            message=new_text_message("TCGA expression lookup complete.")
        )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("Cancel requires an active task.")
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id,
            context_id=context.context_id,
        )
        await updater.cancel()
