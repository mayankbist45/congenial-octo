"""Entrypoint for the separately deployed Temporal research-agent worker."""

from __future__ import annotations

import asyncio
import os

from src.workspace.agent_runtime import AgentRunWorkflow, TASK_QUEUE, run_agent_activity


async def main() -> None:
    from temporalio.client import Client
    from temporalio.worker import Worker

    address = os.getenv("TEMPORAL_ADDRESS", "").strip()
    if not address:
        raise RuntimeError("TEMPORAL_ADDRESS is required for the agent worker.")
    client = await Client.connect(address, namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=[AgentRunWorkflow], activities=[run_agent_activity])
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
