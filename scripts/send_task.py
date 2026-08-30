"""Send a task to a local A2A agent.

Uses a2a-sdk 1.1.x ClientFactory / create_client so the request stays on
protocol 1.0 (protobuf types, SendMessage / SendStreamingMessage). The
factory sets A2A-Version; do not hand-build JSON-RPC with v0.3 fields
(message/send, kind, messageId) or the server treats the call as 0.3.

If the agent pauses in input-required, prints the pending assessment,
prompts on stdin, and resumes on the same task ID.
"""

from __future__ import annotations

import argparse
import asyncio

import httpx
from google.protobuf.json_format import MessageToJson

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_artifact_text, get_message_text, new_text_message
from a2a.types import Artifact, Role, SendMessageRequest, TaskState

# MCP lookup plus cBioPortal can exceed httpx's default 5s timeout.
TIMEOUT = httpx.Timeout(120.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a task to a local A2A agent.")
    parser.add_argument(
        "url",
        nargs="?",
        default="http://127.0.0.1:8001",
        help="Agent base URL (default: http://127.0.0.1:8001)",
    )
    parser.add_argument(
        "message",
        nargs="?",
        default="MSLN in PAAD",
        help='User message to send (default: "MSLN in PAAD")',
    )
    return parser.parse_args()


def _print_artifact(artifact: Artifact) -> None:
    name = artifact.name or artifact.artifact_id or "(unnamed)"
    print(f"--- {name} ---")
    text = get_artifact_text(artifact)
    if text:
        print(text)
    else:
        print(MessageToJson(artifact, ensuring_ascii=False))


class _Turn:
    def __init__(self) -> None:
        self.task_id: str | None = None
        self.context_id: str | None = None
        self.artifacts: dict[str, Artifact] = {}
        self.failures: list[str] = []
        self.input_required = False
        self.input_required_text: str | None = None
        self.rejected = False
        self.completed = False
        self.canceled = False


def _apply_status(turn: _Turn, state: int, message_text: str | None) -> None:
    if state == TaskState.TASK_STATE_INPUT_REQUIRED:
        turn.input_required = True
        if message_text:
            turn.input_required_text = message_text
    elif state == TaskState.TASK_STATE_FAILED:
        turn.failures.append(message_text or "task failed")
    elif state == TaskState.TASK_STATE_REJECTED:
        turn.rejected = True
        if message_text:
            turn.failures.append(message_text)
    elif state == TaskState.TASK_STATE_COMPLETED:
        turn.completed = True
    elif state == TaskState.TASK_STATE_CANCELED:
        turn.canceled = True


async def _send_turn(
    client,
    message: str,
    *,
    task_id: str | None = None,
    context_id: str | None = None,
) -> _Turn:
    request = SendMessageRequest(
        message=new_text_message(
            message,
            role=Role.ROLE_USER,
            task_id=task_id,
            context_id=context_id,
        )
    )
    turn = _Turn()
    turn.task_id = task_id
    turn.context_id = context_id
    async for chunk in client.send_message(request):
        if chunk.HasField("artifact_update"):
            artifact = chunk.artifact_update.artifact
            turn.artifacts[artifact.artifact_id or artifact.name] = artifact
            if chunk.artifact_update.task_id:
                turn.task_id = chunk.artifact_update.task_id
            if chunk.artifact_update.context_id:
                turn.context_id = chunk.artifact_update.context_id
        elif chunk.HasField("task"):
            task = chunk.task
            turn.task_id = task.id or turn.task_id
            turn.context_id = task.context_id or turn.context_id
            for artifact in task.artifacts:
                turn.artifacts[artifact.artifact_id or artifact.name] = artifact
            status_text = (
                get_message_text(task.status.message)
                if task.status.HasField("message")
                else None
            )
            _apply_status(turn, task.status.state, status_text)
        elif chunk.HasField("status_update"):
            event = chunk.status_update
            turn.task_id = event.task_id or turn.task_id
            turn.context_id = event.context_id or turn.context_id
            status_text = (
                get_message_text(event.status.message)
                if event.status.HasField("message")
                else None
            )
            _apply_status(turn, event.status.state, status_text)
    return turn


def _print_pending(turn: _Turn) -> None:
    print("Input required:")
    if turn.input_required_text:
        print(turn.input_required_text)
    elif turn.artifacts:
        print("Pending assessment:")
        for artifact in turn.artifacts.values():
            _print_artifact(artifact)
    else:
        print("(no assessment text on the input-required event)")


def _print_outcome(turn: _Turn) -> None:
    if turn.failures and not turn.rejected:
        print("Task failed:")
        for msg in turn.failures:
            print(msg)
        return
    if turn.rejected:
        print("Task rejected:")
        for msg in turn.failures:
            print(msg)
        return
    if turn.canceled:
        print("Task canceled.")
        return
    print("Artifacts:")
    if not turn.artifacts:
        print("(none)")
        return
    for artifact in turn.artifacts.values():
        _print_artifact(artifact)


async def main(url: str, message: str) -> None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as httpx_client:
        resolver = A2ACardResolver(httpx_client, url)
        card = await resolver.get_agent_card()
        print(f"Agent: {card.name} v{card.version}")
        for iface in card.supported_interfaces:
            print(
                f"  {iface.protocol_binding} {iface.protocol_version} @ {iface.url}"
            )

        client = await create_client(
            card,
            client_config=ClientConfig(httpx_client=httpx_client),
        )
        task_id: str | None = None
        context_id: str | None = None
        outgoing = message
        while True:
            print(f"Sending to {url}: {outgoing}")
            turn = await _send_turn(
                client, outgoing, task_id=task_id, context_id=context_id
            )
            if turn.input_required:
                _print_pending(turn)
                if not turn.task_id:
                    print("Cannot resume: agent did not return a task ID.")
                    return
                try:
                    outgoing = input("Verdict (accept/reject/revise): ").strip()
                except EOFError:
                    print("No stdin verdict; leaving task in input-required.")
                    return
                if not outgoing:
                    print("Empty verdict; leaving task in input-required.")
                    return
                task_id = turn.task_id
                context_id = turn.context_id
                continue
            _print_outcome(turn)
            return


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.url, args.message))
