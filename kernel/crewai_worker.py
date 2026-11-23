"""Standalone CrewAI process. Its only UH! interface is the worker RPC."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from crewai import Agent, BaseLLM, Crew, LLM, Task
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel


class MockLLM(BaseLLM):
    answer: str

    def call(self, messages, **kwargs):
        return f"Final Answer: {self.answer}"

    async def acall(self, messages, **kwargs):
        return self.call(messages, **kwargs)


def _model(mock_answer: str):
    provider = os.getenv("UH_LLM_PROVIDER", "mock")
    if provider == "mock":
        return MockLLM(model="uh-mock", temperature=0, answer=mock_answer)
    if provider != "openai":
        raise ValueError("CrewAI worker supports mock and OpenAI-compatible providers")
    return LLM(
        model="openai/" + os.getenv("UH_LLM_MODEL", "gpt-4o-mini"),
        base_url=os.getenv("UH_LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ["UH_LLM_API_KEY"],
        timeout=45,
        max_retries=0,
    )


def _crew(content: str) -> Crew:
    analyst = Agent(
        role="Analyst", goal="Identify the key considerations in the request",
        backstory="You analyze requests and identify relevant trade-offs.",
        llm=_model(f"协作分析（mock）：{content}"), max_iter=3,
    )
    writer = Agent(
        role="Writer", goal="Synthesize a direct answer from the analysis",
        backstory="You turn an analysis into a concise response in the user's language.",
        llm=_model(f"协作结果（mock）：{content}"), max_iter=3,
    )
    analysis = Task(
        agent=analyst, description="Analyze the user request: {content}",
        expected_output="A concise analysis of the request and its constraints.",
    )
    synthesis = Task(
        agent=writer, context=[analysis], description="Respond to the user request: {content}",
        expected_output="A helpful answer in the language of the user request.",
    )
    return Crew(agents=[analyst, writer], tasks=[analysis, synthesis], memory=False, cache=False, tracing=False)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Validate provider configuration and initialize the SDK before serving /ready.
    _crew("")
    yield


app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
@app.get("/ready")
async def health():
    return {"status": "ok"}


class CollaborationInput(BaseModel):
    task_id: str
    content: str


class WorkerRequest(BaseModel):
    trace_id: str
    payload: CollaborationInput


async def _wait_for_disconnect(request: Request) -> None:
    # FastAPI has already consumed the request body for WorkerRequest.
    while (await request.receive())["type"] != "http.disconnect":
        pass


@app.post("/rpc/collaboration.run")
async def run(body: WorkerRequest, request: Request):
    content = body.payload.content
    try:
        async with asyncio.timeout(115):
            execution = asyncio.create_task(_crew(content).akickoff(inputs={"content": content}))
            disconnected = asyncio.create_task(_wait_for_disconnect(request))
            try:
                done, _ = await asyncio.wait({execution, disconnected}, return_when=asyncio.FIRST_COMPLETED)
                if execution not in done:
                    raise HTTPException(499, "Collaboration caller disconnected")
                return {"answer": execution.result().raw}
            finally:
                execution.cancel()
                disconnected.cancel()
                await asyncio.gather(execution, disconnected, return_exceptions=True)
    except TimeoutError:
        raise HTTPException(504, "Collaboration deadline exceeded") from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Collaboration execution unavailable") from None
