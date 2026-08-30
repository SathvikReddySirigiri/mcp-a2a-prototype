"""Serve the radioligand target ranking A2A agent on port 8003."""

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
    from .agent_executor import RankingAgentExecutor
else:
    from agent_executor import RankingAgentExecutor

HOST = "127.0.0.1"
PORT = 8003
AGENT_URL = f"http://{HOST}:{PORT}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def build_agent_card() -> AgentCard:
    skill = AgentSkill(
        id="score_target",
        name="Radioligand therapy target suitability",
        description=(
            "Score a gene as a radioligand-therapy target by gathering tumor "
            "expression, healthy-tissue background, and subcellular localization "
            "from the TCGA Expression Agent and the Normal Tissue and Localization "
            "Agent. Returns a criterion-based qualitative assessment (tumor "
            "abundance, normal-tissue background, surface accessibility), each "
            "with a supporting value and source - not a single computed score. "
            "Pauses in input-required for a human verdict (accept, reject, or "
            "revise) before completing. "
            "RSEM values from get_expression are not comparable to GTEx TPM "
            "values and must not be divided against them. GTEx samples are "
            "healthy post-mortem donors, not adjacent-normal tissue from the "
            "same patients as any tumor cohort. HPA locations are "
            "antibody-derived or predicted with confidence varying by gene; "
            "membrane annotation does not establish that the extracellular "
            "domain is accessible to a circulating antibody or radioligand "
            "in vivo."
        ),
        tags=["ranking", "radioligand", "target", "a2a"],
        examples=["MSLN", "MSLN PAAD", "CEACAM5 in LUAD"],
        input_modes=["text/plain"],
        output_modes=["text/plain"],
    )
    return AgentCard(
        name="Radioligand Target Ranking Agent",
        description=(
            "A2A orchestrator that calls the expression agent (port 8001) and "
            "the annotation agent (port 8002) and returns an independent "
            "criterion-based radioligand-therapy suitability reading."
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
        agent_executor=RankingAgentExecutor(),
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
