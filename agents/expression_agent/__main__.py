"""Serve the TCGA expression A2A agent on port 8001."""

from __future__ import annotations

import logging

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from starlette.applications import Starlette

if __package__:
    from .agent_executor import ExpressionAgentExecutor
else:
    from agent_executor import ExpressionAgentExecutor

HOST = "127.0.0.1"
PORT = 8001
AGENT_URL = f"http://{HOST}:{PORT}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def build_agent_card() -> AgentCard:
    skill = AgentSkill(
        id="get_expression",
        name="TCGA tumor expression lookup",
        description=(
            "Look up mRNA expression for a gene across a TCGA tumor cohort. "
            "Returns summary statistics (n, mean, median, quartiles, range) of "
            "RSEM-normalized expression. Supported cohorts: PAAD, LUAD, BRCA. "
            "Tumor samples only - provides no normal-tissue baseline, so "
            "results cannot establish whether a gene is overexpressed relative "
            "to healthy tissue. RSEM values are not comparable to TPM values "
            "from other sources and must not be divided against them."
        ),
        tags=["tcga", "expression", "tumor", "cbioportal"],
        examples=["MSLN", "MSLN PAAD", "CEACAM5 in LUAD"],
        input_modes=["text/plain"],
        output_modes=["text/plain"],
    )
    return AgentCard(
        name="TCGA Expression Agent",
        description=(
            "A2A wrapper around the tcga-expression MCP server. Looks up "
            "tumor mRNA expression statistics from TCGA via cBioPortal."
        ),
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=AGENT_URL,
                protocol_version="1.0",
            )
        ],
        skills=[skill],
    )


def build_app() -> Starlette:
    agent_card = build_agent_card()
    request_handler = DefaultRequestHandler(
        agent_executor=ExpressionAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )
    routes = []
    routes.extend(create_agent_card_routes(agent_card))
    routes.extend(create_jsonrpc_routes(request_handler, "/"))
    return Starlette(routes=routes)


def main() -> None:
    logger.info(
        "Agent card: %s%s", AGENT_URL, AGENT_CARD_WELL_KNOWN_PATH
    )
    uvicorn.run(build_app(), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
