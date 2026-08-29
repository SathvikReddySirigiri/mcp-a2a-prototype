"""Send a TCGA expression lookup task to the local A2A agent.

Uses a2a-sdk 1.1.x ClientFactory / create_client so the request stays on
protocol 1.0 (protobuf types, SendMessage / SendStreamingMessage). The
factory sets A2A-Version; do not hand-build JSON-RPC with v0.3 fields
(message/send, kind, messageId) or the server treats the call as 0.3.
"""

from __future__ import annotations

import asyncio

import httpx
from google.protobuf.json_format import MessageToJson

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_artifact_text, get_message_text, new_text_message
from a2a.types import Artifact, Role, SendMessageRequest, TaskState

AGENT_URL = "http://127.0.0.1:8001"
QUERY = "MSLN in PAAD"
# MCP lookup plus cBioPortal can exceed httpx's default 5s timeout.
TIMEOUT = httpx.Timeout(120.0)


def _print_artifact(artifact: Artifact) -> None:
    name = artifact.name or artifact.artifact_id or "(unnamed)"
    print(f"--- {name} ---")
    text = get_artifact_text(artifact)
    if text:
        print(text)
    else:
        print(MessageToJson(artifact, ensuring_ascii=False))


async def main() -> None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as httpx_client:
        resolver = A2ACardResolver(httpx_client, AGENT_URL)
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
        request = SendMessageRequest(
            message=new_text_message(QUERY, role=Role.ROLE_USER),
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

        if failures:
            print("Task failed:")
            for msg in failures:
                print(msg)
            return

        print("Artifacts:")
        if not artifacts:
            print("(none)")
            return
        for artifact in artifacts.values():
            _print_artifact(artifact)


if __name__ == "__main__":
    asyncio.run(main())
