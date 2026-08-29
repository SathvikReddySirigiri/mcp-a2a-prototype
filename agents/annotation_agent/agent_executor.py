"""A2A executor that routes GTEx and HPA lookups to the matching MCP server."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from google.protobuf.json_format import MessageToDict

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
GTEX_SERVER = REPO_ROOT / "servers" / "gtex_server.py"
HPA_SERVER = REPO_ROOT / "servers" / "hpa_server.py"

SKILL_NORMAL = "get_normal_expression"
SKILL_LOCALIZATION = "get_localization"
DEFAULT_TISSUE = "Pancreas"

_SKILL_ALIASES = {
    SKILL_NORMAL: SKILL_NORMAL,
    "GTEX": SKILL_NORMAL,
    "NORMAL": SKILL_NORMAL,
    "NORMAL_EXPRESSION": SKILL_NORMAL,
    SKILL_LOCALIZATION: SKILL_LOCALIZATION,
    "LOCALIZATION": SKILL_LOCALIZATION,
    "LOCALISATION": SKILL_LOCALIZATION,
    "HPA": SKILL_LOCALIZATION,
}

_LOCALIZATION_HINTS = {
    "LOCALIZATION",
    "LOCALISATION",
    "LOCALIZE",
    "LOCALISE",
    "LOCALIZED",
    "LOCALISED",
    "SUBCELLULAR",
    "HPA",
    "ATLAS",
    SKILL_LOCALIZATION.upper(),
}

_NORMAL_HINTS = {
    "GTEX",
    "NORMAL",
    "HEALTHY",
    "TPM",
    "DONOR",
    "DONORS",
    SKILL_NORMAL.upper(),
}

_SKIP_TOKENS = {
    "IN",
    "FOR",
    "OF",
    "THE",
    "A",
    "AN",
    "AND",
    "FROM",
    "GENE",
    "LOOKUP",
    "LOOK",
    "UP",
    "GET",
    "EXPRESSION",
    "TISSUE",
    "PROTEIN",
    "HUMAN",
    "MRNA",
    "MEDIAN",
} | _LOCALIZATION_HINTS | _NORMAL_HINTS


def _normalize_skill(value: str | None) -> str | None:
    if not value:
        return None
    return _SKILL_ALIASES.get(value.strip().upper().replace("-", "_").replace(" ", "_"))


def _first_str(data: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _struct_dict(message: object | None) -> dict:
    if message is None:
        return {}
    try:
        data = MessageToDict(message)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _skill_from_mapping(data: dict) -> str | None:
    return _normalize_skill(
        _first_str(data, ("skill", "skillId", "skill_id", "tool"))
    )


def parse_annotation_query(
    text: str, context: RequestContext | None = None
) -> tuple[str, str, str]:
    """Extract (skill, gene, tissue) from metadata, JSON, or free text."""
    raw = text.strip()
    metadata_skill = None
    if context is not None:
        metadata_skill = _skill_from_mapping(context.metadata)
        if metadata_skill is None and context.message is not None:
            metadata_skill = _skill_from_mapping(
                _struct_dict(context.message.metadata)
            )

    payload = None
    if raw:
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded

    if payload is not None:
        skill = (
            metadata_skill
            or _skill_from_mapping(payload)
            or (SKILL_NORMAL if payload.get("tissue") else None)
        )
        gene = _first_str(payload, ("gene",))
        tissue = _first_str(payload, ("tissue",)) or DEFAULT_TISSUE
        if not skill:
            raise ValueError(
                "Specify a skill: get_normal_expression or get_localization. "
                "Example: {\"skill\": \"get_localization\", \"gene\": \"MSLN\"}"
            )
        if not gene:
            raise ValueError(
                "Provide a HUGO gene symbol. "
                'Example: {"skill": "get_normal_expression", "gene": "MSLN", '
                '"tissue": "Pancreas"}'
            )
        return skill, gene.upper(), tissue

    tokens = [tok.strip(".,;:()[]{}\"'") for tok in raw.replace(",", " ").split()]
    tokens = [tok for tok in tokens if tok]
    upper_tokens = [tok.upper() for tok in tokens]

    skill = metadata_skill
    if skill is None:
        loc = any(tok in _LOCALIZATION_HINTS for tok in upper_tokens)
        normal = any(tok in _NORMAL_HINTS for tok in upper_tokens)
        if loc and not normal:
            skill = SKILL_LOCALIZATION
        elif normal and not loc:
            skill = SKILL_NORMAL

    gene: str | None = None
    leftover: list[str] = []
    for tok in tokens:
        upper = tok.upper()
        if _normalize_skill(tok) or upper in _SKIP_TOKENS:
            continue
        if gene is None and upper.isalnum():
            gene = upper
        else:
            leftover.append(tok)

    if skill is None:
        if leftover:
            skill = SKILL_NORMAL
        else:
            raise ValueError(
                "Specify a skill: get_normal_expression (optionally with a "
                "GTEx tissue) or get_localization. "
                "Example: 'MSLN Pancreas' or 'MSLN localization'"
            )

    if not gene:
        raise ValueError(
            "Could not find a gene symbol in the request. "
            "Try 'MSLN Pancreas' or 'MSLN localization'."
        )

    tissue = " ".join(leftover) if leftover else DEFAULT_TISSUE
    return skill, gene, tissue


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


async def call_mcp_tool(server: Path, tool: str, arguments: dict) -> str:
    """Call `tool` on an MCP stdio server and return its text result."""
    if not server.is_file():
        raise FileNotFoundError(f"MCP server not found: {server}")

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(server)],
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            return _tool_result_text(result)


class AnnotationAgentExecutor(AgentExecutor):
    """Routes a request to GTEx or HPA and returns the tool result as an artifact."""

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

        try:
            query = get_message_text(context.message) if context.message else ""
            skill, gene, tissue = parse_annotation_query(query, context)
        except Exception as exc:
            logger.exception("Failed to parse annotation request")
            await updater.failed(message=new_text_message(str(exc)))
            return

        if skill == SKILL_LOCALIZATION:
            working = "Looking up HPA localization..."
            complete = "HPA localization lookup complete."
            artifact_name = "hpa_localization"
            server = HPA_SERVER
            arguments = {"gene": gene}
        else:
            working = "Looking up GTEx normal expression..."
            complete = "GTEx normal expression lookup complete."
            artifact_name = "gtex_normal_expression"
            server = GTEX_SERVER
            arguments = {"gene": gene, "tissue": tissue}

        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message(working),
        )

        try:
            logger.info("MCP %s %s", skill, arguments)
            result = await call_mcp_tool(server, skill, arguments)
        except Exception as exc:
            logger.exception("Annotation lookup failed")
            await updater.failed(message=new_text_message(str(exc)))
            return

        await updater.add_artifact(
            parts=[new_text_part(text=result, media_type="text/plain")],
            name=artifact_name,
            last_chunk=True,
        )
        await updater.complete(message=new_text_message(complete))

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
