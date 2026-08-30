"""A2A executor that scores a gene via the expression and annotation agents."""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import (
    get_artifact_text,
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    AgentCard,
    AgentSkill,
    Artifact,
    Role,
    SendMessageRequest,
    Task,
    TaskState,
)

logger = logging.getLogger(__name__)

EXPRESSION_URL = "http://127.0.0.1:8001"
ANNOTATION_URL = "http://127.0.0.1:8002"
SKILL_EXPRESSION = "get_expression"
SKILL_NORMAL = "get_normal_expression"
SKILL_LOCALIZATION = "get_localization"
SUPPORTED_COHORTS = ("PAAD", "LUAD", "BRCA")
COHORT_TO_TISSUE = {
    "PAAD": "Pancreas",
    "LUAD": "Lung",
    "BRCA": "Breast_Mammary_Tissue",
}
TIMEOUT = httpx.Timeout(120.0)
_SKIP_TOKENS = {
    "IN",
    "FOR",
    "OF",
    "THE",
    "A",
    "AN",
    "AND",
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
    "SCORE",
    "TARGET",
    "RANK",
    "RANKING",
    "RADIO",
    "RADIOLIGAND",
    "THERAPY",
    "SUITABILITY",
    "ACCEPT",
    "ACCEPTED",
    "REJECT",
    "REJECTED",
    "REVISE",
    "RETRY",
}

REVIEW_PROMPT = (
    "Human review required. Reply with accept, reject, or revise."
)
ASSESSMENT_ARTIFACT = "target_assessment"
_ACCEPT_WORDS = frozenset({"ACCEPT", "ACCEPTED", "APPROVE", "APPROVED", "YES"})
_REJECT_WORDS = frozenset({"REJECT", "REJECTED", "NO", "DENY"})
_REVISE_WORDS = frozenset({"REVISE", "RETRY", "RERUN", "AGAIN"})


def parse_ranking_query(text: str) -> tuple[str, str]:
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
        if cohort not in SUPPORTED_COHORTS:
            raise ValueError(
                f"Unsupported cohort '{cohort}'. Cohorts: {', '.join(SUPPORTED_COHORTS)}"
            )
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


def _skill(card: AgentCard, skill_id: str) -> AgentSkill:
    for skill in card.skills:
        if skill.id == skill_id:
            return skill
    raise ValueError(
        f"Agent '{card.name}' has no skill '{skill_id}'. "
        f"Available: {', '.join(s.id for s in card.skills) or '(none)'}"
    )


def _constraint_note(description: str, needles: tuple[str, ...]) -> str:
    """Keep sentences from a fetched skill description that state constraints."""
    parts = [p.strip() for p in re.split(r"(?<=[.])\s+", description) if p.strip()]
    picked = [
        p.rstrip(".")
        for p in parts
        if any(needle.lower() in p.lower() for needle in needles)
    ]
    if not picked:
        return description.strip()
    return ". ".join(picked) + "."


def _labeled(text: str, key: str) -> str | None:
    prefix = key.lower()
    for line in text.splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        label, _, rest = stripped.partition(":")
        if label.strip().lower() == prefix:
            value = rest.strip()
            return value or None
    return None


def _labeled_float(text: str, key: str) -> float | None:
    raw = _labeled(text, key)
    if not raw:
        return None
    token = raw.split()[0].replace(",", "")
    try:
        return float(token)
    except ValueError:
        return None


def _tumor_label(median: float | None) -> str:
    if median is None:
        return "unknown"
    if median >= 1000:
        return "high"
    if median >= 100:
        return "moderate"
    return "low"


def _normal_label(median_tpm: float | None) -> str:
    if median_tpm is None:
        return "unknown"
    if median_tpm < 1:
        return "low"
    if median_tpm < 10:
        return "moderate"
    return "high"


def _surface_label(locations: str | None, membrane: str | None, secreted: str | None) -> str:
    loc = (locations or "").lower()
    mem = (membrane or "").strip().lower() == "yes"
    sec = (secreted or "").strip().lower() == "yes"
    if mem and "plasma membrane" in loc:
        return "membrane-localized; in vivo ligand access not established"
    if mem:
        return "membrane-associated; extracellular access not established"
    if sec:
        return "secreted; not a demonstrated surface target"
    return "no membrane annotation; weak surface-target evidence"


def format_assessment(
    gene: str,
    cohort: str,
    tissue: str,
    expr_card: AgentCard,
    ann_card: AgentCard,
    tumor_text: str,
    normal_text: str,
    loc_text: str,
) -> str:
    """Build a criterion-based reading. Never combines RSEM with TPM."""
    expr_skill = _skill(expr_card, SKILL_EXPRESSION)
    normal_skill = _skill(ann_card, SKILL_NORMAL)
    loc_skill = _skill(ann_card, SKILL_LOCALIZATION)

    tumor_median = _labeled_float(tumor_text, "median")
    tumor_mean = _labeled_float(tumor_text, "mean")
    tumor_n = _labeled(tumor_text, "samples")
    tumor_units = _labeled(tumor_text, "units") or "RSEM"
    tumor_bits = []
    if tumor_median is not None:
        tumor_bits.append(f"median {tumor_median:g} {tumor_units}")
    if tumor_mean is not None:
        tumor_bits.append(f"mean {tumor_mean:g}")
    if tumor_n:
        tumor_bits.append(f"n={tumor_n}")
    tumor_value = "; ".join(tumor_bits) if tumor_bits else tumor_text.strip()

    normal_median = _labeled_float(normal_text, "median")
    normal_n = _labeled(normal_text, "samples")
    normal_units = _labeled(normal_text, "units") or "TPM"
    normal_bits = []
    if normal_median is not None:
        normal_bits.append(f"median {normal_median:g} {normal_units}")
    if normal_n:
        normal_bits.append(f"n={normal_n}")
    normal_value = "; ".join(normal_bits) if normal_bits else normal_text.strip()

    locations = _labeled(loc_text, "locations")
    membrane = _labeled(loc_text, "membrane")
    secreted = _labeled(loc_text, "secreted")
    tissue_spec = _labeled(loc_text, "tissue")
    loc_bits = []
    if locations:
        loc_bits.append(f"locations {locations}")
    if membrane:
        loc_bits.append(f"membrane {membrane}")
    if secreted:
        loc_bits.append(f"secreted {secreted}")
    if tissue_spec:
        loc_bits.append(f"tissue specificity {tissue_spec}")
    loc_value = "; ".join(loc_bits) if loc_bits else loc_text.strip()

    tumor_note = _constraint_note(
        expr_skill.description,
        (
            "must not be divided",
            "not comparable",
            "Tumor samples only",
            "no normal-tissue",
            "overexpressed",
        ),
    )
    normal_note = _constraint_note(
        normal_skill.description,
        (
            "must not be divided",
            "not directly comparable",
            "post-mortem",
            "adjacent-normal",
            "matched tumor-versus-normal",
        ),
    )
    loc_note = _constraint_note(
        loc_skill.description,
        (
            "antibody-derived",
            "confidence varies",
            "does not establish",
            "accessible",
        ),
    )

    return "\n".join(
        [
            f"{gene} - radioligand therapy target assessment",
            f"Cohort queried: TCGA-{cohort}. Normal tissue queried: GTEx {tissue}.",
            "Criterion-based reading, not a single score. "
            "Tumor RSEM and GTEx TPM are reported separately and are not divided.",
            "",
            f"Tumor abundance: {_tumor_label(tumor_median)}",
            f"  value : {tumor_value}",
            f"  source: {expr_card.name}, skill {SKILL_EXPRESSION}, TCGA-{cohort} tumor samples",
            f"  note  : {tumor_note} Heuristic label uses tumor RSEM magnitude only.",
            "",
            f"Normal-tissue background: {_normal_label(normal_median)}",
            f"  value : {normal_value}",
            f"  source: {ann_card.name}, skill {SKILL_NORMAL}, GTEx {tissue}",
            f"  note  : {normal_note} Heuristic label uses GTEx TPM magnitude only.",
            "",
            f"Surface accessibility: {_surface_label(locations, membrane, secreted)}",
            f"  value : {loc_value}",
            f"  source: {ann_card.name}, skill {SKILL_LOCALIZATION}, Human Protein Atlas",
            f"  note  : {loc_note}",
        ]
    )


async def _collect_artifacts(client, message: str) -> tuple[list[Artifact], list[str]]:
    request = SendMessageRequest(
        message=new_text_message(message, role=Role.ROLE_USER),
    )
    artifacts: dict[str, Artifact] = {}
    failures: list[str] = []
    async for chunk in client.send_message(request):
        if chunk.HasField("artifact_update"):
            artifact = chunk.artifact_update.artifact
            artifacts[artifact.artifact_id or artifact.name] = artifact
        elif chunk.HasField("task"):
            for artifact in chunk.task.artifacts:
                artifacts[artifact.artifact_id or artifact.name] = artifact
        elif chunk.HasField("status_update"):
            status = chunk.status_update.status
            if status.state == TaskState.TASK_STATE_FAILED:
                if status.HasField("message"):
                    failures.append(get_message_text(status.message))
                else:
                    failures.append("task failed")
    return list(artifacts.values()), failures


async def _artifact_text(client, message: str) -> str:
    artifacts, failures = await _collect_artifacts(client, message)
    if failures:
        raise RuntimeError("; ".join(failures))
    texts = [get_artifact_text(a) for a in artifacts if get_artifact_text(a)]
    if not texts:
        raise RuntimeError(f"No artifact text returned for: {message}")
    return "\n".join(texts)


def parse_verdict(text: str) -> tuple[str | None, str]:
    """Return (verdict, remainder) from a follow-up message."""
    tokens = [tok.strip(".,;:()[]{}\"'") for tok in text.replace(",", " ").split()]
    tokens = [tok for tok in tokens if tok]
    if not tokens:
        return None, ""
    first = tokens[0].upper()
    rest = " ".join(tokens[1:]).strip()
    if first in _ACCEPT_WORDS:
        return "accept", rest
    if first in _REJECT_WORDS:
        return "reject", rest
    if first in _REVISE_WORDS:
        return "revise", rest
    return None, text.strip()


def _assessment_from_task(task: Task) -> str:
    for artifact in reversed(list(task.artifacts)):
        if artifact.name == ASSESSMENT_ARTIFACT:
            text = get_artifact_text(artifact)
            if text:
                return text
    if task.status.HasField("message"):
        text = get_message_text(task.status.message)
        marker = f"\n\n{REVIEW_PROMPT}"
        if marker in text:
            return text.split(marker, 1)[0].strip()
        return text.strip()
    return ""


def _original_gene_cohort(task: Task, remainder: str) -> tuple[str, str]:
    if remainder:
        try:
            return parse_ranking_query(remainder)
        except ValueError:
            pass
    for msg in task.history:
        if msg.role != Role.ROLE_USER:
            continue
        text = get_message_text(msg)
        verdict, rest = parse_verdict(text)
        candidate = rest if verdict else text
        if not candidate:
            continue
        try:
            return parse_ranking_query(candidate)
        except ValueError:
            continue
    raise ValueError(
        "Could not recover a gene symbol from the task. "
        "Reply 'revise MSLN PAAD' to re-run."
    )


def _review_message(assessment: str) -> str:
    return f"{assessment}\n\n{REVIEW_PROMPT}"


class RankingAgentExecutor(AgentExecutor):
    """Calls peer A2A agents, then pauses for a human accept/reject/revise verdict."""

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

        # V2 loads the persisted task into current_task before execute().
        # INPUT_REQUIRED means this call is a human follow-up on the same task.
        if (
            context.current_task
            and task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        ):
            await self._handle_review(context, updater, task)
            return

        query = get_message_text(context.message) if context.message else ""
        await self._gather_and_request_review(updater, query)

    async def _handle_review(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        task: Task,
    ) -> None:
        query = get_message_text(context.message) if context.message else ""
        verdict, remainder = parse_verdict(query)
        logger.info("score_target review verdict=%s task=%s", verdict, task.id)

        if verdict == "accept":
            assessment = _assessment_from_task(task)
            if not assessment:
                await updater.failed(
                    message=new_text_message(
                        "No pending assessment to accept."
                    )
                )
                return
            await updater.add_artifact(
                parts=[new_text_part(text=assessment, media_type="text/plain")],
                name=ASSESSMENT_ARTIFACT,
                last_chunk=True,
            )
            await updater.complete(
                message=new_text_message("Target assessment accepted.")
            )
            return

        if verdict == "reject":
            await updater.reject(
                message=new_text_message("Target assessment rejected.")
            )
            return

        if verdict == "revise":
            gene, cohort = _original_gene_cohort(task, remainder)
            await self._gather_and_request_review(
                updater, f"{gene} {cohort}"
            )
            return

        await updater.requires_input(
            message=new_text_message(
                _review_message(
                    _assessment_from_task(task)
                    or "No assessment text is on this task."
                )
                + " Unrecognized verdict."
            )
        )

    async def _gather_and_request_review(
        self, updater: TaskUpdater, query: str
    ) -> None:
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message(
                "Fetching peer agent cards and scoring the target..."
            ),
        )
        try:
            gene, cohort = parse_ranking_query(query)
            tissue = COHORT_TO_TISSUE[cohort]
            logger.info(
                "score_target gene=%s cohort=%s tissue=%s", gene, cohort, tissue
            )
            assessment = await self._run_lookups(gene, cohort, tissue)
        except Exception as exc:
            logger.exception("Target ranking failed")
            await updater.failed(message=new_text_message(str(exc)))
            return

        await updater.add_artifact(
            parts=[new_text_part(text=assessment, media_type="text/plain")],
            name=ASSESSMENT_ARTIFACT,
            last_chunk=True,
        )
        await updater.requires_input(
            message=new_text_message(_review_message(assessment))
        )

    async def _run_lookups(self, gene: str, cohort: str, tissue: str) -> str:
        async with httpx.AsyncClient(timeout=TIMEOUT) as httpx_client:
            config = ClientConfig(httpx_client=httpx_client)
            expr_card = await A2ACardResolver(
                httpx_client, EXPRESSION_URL
            ).get_agent_card()
            ann_card = await A2ACardResolver(
                httpx_client, ANNOTATION_URL
            ).get_agent_card()
            _skill(expr_card, SKILL_EXPRESSION)
            _skill(ann_card, SKILL_NORMAL)
            _skill(ann_card, SKILL_LOCALIZATION)
            logger.info(
                "Fetched cards: %s (%s), %s (%s)",
                expr_card.name,
                ", ".join(s.id for s in expr_card.skills),
                ann_card.name,
                ", ".join(s.id for s in ann_card.skills),
            )

            expr_client = await create_client(expr_card, client_config=config)
            ann_client = await create_client(ann_card, client_config=config)
            tumor_text, normal_text, loc_text = await asyncio.gather(
                _artifact_text(expr_client, f"{gene} {cohort}"),
                _artifact_text(ann_client, f"{SKILL_NORMAL} {gene} {tissue}"),
                _artifact_text(ann_client, f"{SKILL_LOCALIZATION} {gene}"),
            )

        return format_assessment(
            gene,
            cohort,
            tissue,
            expr_card,
            ann_card,
            tumor_text,
            normal_text,
            loc_text,
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
