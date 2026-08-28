"""Entrypoint for the separately deployed Temporal research-agent worker."""

from __future__ import annotations

import asyncio
import os

from src.workspace.agent_runtime import (
    MODEL_TASK_QUEUE,
    TASK_QUEUE,
    AgentRunWorkflow,
    collect_agent_sources_activity,
    fail_agent_run_activity,
    finalize_agent_run_activity,
    generate_agent_output_activity,
    persist_agent_result_activity,
    prepare_agent_run_activity,
)


async def main() -> None:
    from temporalio.client import Client
    from temporalio.worker import Worker

    address = os.getenv("TEMPORAL_ADDRESS", "").strip()
    if not address:
        raise RuntimeError("TEMPORAL_ADDRESS is required for the agent worker.")
    client = await Client.connect(address, namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[AgentRunWorkflow],
        activities=[prepare_agent_run_activity, collect_agent_sources_activity, persist_agent_result_activity, finalize_agent_run_activity, fail_agent_run_activity],
        max_concurrent_activities=4,
    ), Worker(
        client,
        task_queue=MODEL_TASK_QUEUE,
        activities=[generate_agent_output_activity],
        max_concurrent_activities=2,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
