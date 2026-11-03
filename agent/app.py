"""The web interface's service: the page, one agent run per request, and each run's events streamed back.

uvicorn runs it on the agent VM as agent.app:app, from /opt/agentic-iac. POST /runs starts
agent/runner.py as its own process; GET /runs/{id}/events relays that process's events to the page
as server-sent events, replaying from the start so a reloaded page catches up. Runs live in memory
only: a restart forgets them, and their changes stay in the forge, where the reader can close them.
FastAPI 0.162.0: https://github.com/fastapi/fastapi/blob/0.162.0/docs/en/docs/tutorial/server-sent-events.md
"""

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.sse import EventSourceResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
app = FastAPI(title="Ask the agent for a change")


class Run:
    """One run's events so far, and whether its process has ended."""

    def __init__(self):
        self.events = []
        self.ended = False
        self.changed = asyncio.Condition()
        self.relay = None  # the task reading the process's output; kept so it isn't collected


runs: dict[str, Run] = {}


class NewRun(BaseModel):
    request: str = Field(min_length=1, max_length=2000)
    # shards/demo is shared, so runs on it queue behind each other; a new shard is the run's own.
    shard: Literal["demo", "new"] = "demo"


@app.post("/runs")
async def start_run(new: NewRun):
    # Run IDs name branches and new shards, so they keep to the shard names init-shard.sh accepts.
    run_id = "run-" + secrets.token_hex(3)
    shard = run_id if new.shard == "new" else "demo"
    # Each run's working data stays under the agent user's home: CrewAI's records and the scratch
    # directory its validate tool uses.
    home = Path.home() / "runs" / run_id
    home.mkdir(parents=True)
    env = {**os.environ, "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache")}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "agent.runner", run_id, shard,
        cwd=HERE.parent, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        limit=2**20,  # one event per line; a proposed file can make a long one
    )
    process.stdin.write(new.request.encode())
    await process.stdin.drain()
    process.stdin.close()
    run = runs[run_id] = Run()
    run.relay = asyncio.create_task(relay(process, run))
    return {"id": run_id, "shard": f"shards/{shard}"}


async def relay(process, run):
    async for line in process.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            continue  # not an event; the runner sends everything else to the service log
        await record(run, event)
    code = await process.wait()
    if not any(event["kind"] == "done" for event in run.events):
        text = f"The run stopped unexpectedly (exit code {code}); the service log says why."
        await record(run, {"kind": "done", "outcome": "failed", "text": text})
    async with run.changed:
        run.ended = True
        run.changed.notify_all()


async def record(run, event):
    async with run.changed:
        run.events.append(event)
        run.changed.notify_all()


def find_run(run_id: str) -> Run:
    if run_id not in runs:
        raise HTTPException(status_code=404, detail="No such run.")
    return runs[run_id]


@app.get("/runs/{run_id}/events", response_class=EventSourceResponse)
async def run_events(run: Run = Depends(find_run)):
    sent = 0
    while True:
        async with run.changed:
            await run.changed.wait_for(lambda: len(run.events) > sent or run.ended)
            fresh, ended = run.events[sent:], run.ended
        for event in fresh:
            yield event
        sent += len(fresh)
        if ended and sent == len(run.events):
            return


# The page, mounted last so the routes above take precedence.
app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")
