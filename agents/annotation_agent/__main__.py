"""Serve the normal-tissue and localization A2A agent on port 8002."""

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
    from .agent_executor import AnnotationAgentExecutor
else:
    from agent_executor import AnnotationAgentExecutor

HOST = "127.0.0.1"
PORT = 8002
AGENT_URL = f"http://{HOST}:{PORT}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def build_agent_card() -> AgentCard:
    normal_skill = AgentSkill(
        id="get_normal_expression",
        name="GTEx healthy-tissue expression lookup",
        description=(
            "Look up median expression of a gene in healthy tissue from GTEx. "
            "Returns the median mRNA expression of a HUGO gene symbol in a GTEx "
            "healthy tissue, in TPM. GTEx TPM values are not directly comparable "
            "to cBioPortal RSEM values from get_expression, because they use "
            "different normalization pipelines. The two must not be divided to "
            "produce a fold change. GTEx samples come from healthy post-mortem "
            "donors, not from adjacent-normal tissue in the same patients as any "
            "tumor cohort. Donor demographics, tissue collection, and batch "
            "effects all differ. A GTEx-versus-TCGA comparison is therefore "
            "directional evidence, not a matched tumor-versus-normal analysis."
        ),
        tags=["gtex", "expression", "normal", "healthy"],
        examples=["MSLN", "MSLN Pancreas", "CEACAM5 in Lung"],
        input_modes=["text/plain"],
        output_modes=["text/plain"],
    )
    localization_skill = AgentSkill(
        id="get_localization",
        name="HPA subcellular localization lookup",
        description=(
            "Look up subcellular localization and tissue specificity from HPA. "
            "Answers where a protein is located in the cell, and how "
            "tissue-restricted it is, using Human Protein Atlas annotations. "
            "HPA subcellular locations are antibody-derived or predicted, and "
            "confidence varies by gene - this is not uniform experimental "
            "confirmation. Membrane annotation indicates a protein is "
            "membrane-associated, but does not establish that the extracellular "
            "domain is accessible to a circulating antibody or radioligand "
            "in vivo."
        ),
        tags=["hpa", "localization", "subcellular", "tissue-specificity"],
        examples=["MSLN", "CEACAM5", "ALB"],
        input_modes=["text/plain"],
        output_modes=["text/plain"],
    )
    return AgentCard(
        name="Normal Tissue and Localization Agent",
        description=(
            "A2A wrapper around the gtex-normal and hpa-localization MCP "
            "servers. Looks up healthy-tissue expression from GTEx and "
            "subcellular localization from the Human Protein Atlas."
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
        skills=[normal_skill, localization_skill],
    )


def build_app() -> Starlette:
    agent_card = build_agent_card()
    request_handler = DefaultRequestHandler(
        agent_executor=AnnotationAgentExecutor(),
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
