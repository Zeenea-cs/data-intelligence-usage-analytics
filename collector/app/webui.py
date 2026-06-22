"""Lightweight web UI to trigger a collection run on demand.

A single page with a "Run collection now" button plus a table of recent runs.
The trigger shares the collection lock with the scheduler, so a manual run and
the cron run never overlap. Mounted by main.py alongside the AsyncIOScheduler in
the same event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import CollectionRun
from app.scheduler import run_collection

logger = logging.getLogger(__name__)

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Actian Companion - Collection</title>
<style>
 body{font-family:system-ui,sans-serif;margin:2rem;max-width:900px}
 h1{font-size:1.4rem}
 button{font-size:1rem;padding:.6rem 1.2rem;border:0;border-radius:6px;
   background:#1f6feb;color:#fff;cursor:pointer}
 button:disabled{background:#888;cursor:not-allowed}
 table{border-collapse:collapse;width:100%;margin-top:1.5rem;font-size:.9rem}
 th,td{border:1px solid #ddd;padding:.4rem .6rem;text-align:left}
 th{background:#f3f3f3}
 .s-success{color:#1a7f37;font-weight:600}
 .s-partial{color:#9a6700;font-weight:600}
 .s-failed{color:#cf222e;font-weight:600}
 #msg{margin-left:1rem;color:#555}
</style></head><body>
<h1>Actian Data Intelligence Companion</h1>
<p>Trigger a data collection now, bypassing the cron schedule.</p>
<button id="run" onclick="trigger()">Run collection now</button>
<span id="msg"></span>
<table id="runs"><thead><tr>
 <th>Started</th><th>Finished</th><th>Status</th>
 <th>Users</th><th>Events</th><th>Items</th><th>Error</th>
</tr></thead><tbody></tbody></table>
<script>
async function refresh(){
 const r = await fetch('api/runs'); const rows = await r.json();
 const tb = document.querySelector('#runs tbody'); tb.innerHTML='';
 for(const x of rows){
  const tr=document.createElement('tr');
  tr.innerHTML=`<td>${x.started_at??''}</td><td>${x.finished_at??''}</td>`+
   `<td class="s-${x.status}">${x.status??''}</td>`+
   `<td>${x.users_collected??''}</td><td>${x.events_collected??''}</td>`+
   `<td>${x.items_collected??''}</td><td>${x.error_message??''}</td>`;
  tb.appendChild(tr);
 }
}
async function trigger(){
 const btn=document.getElementById('run'); const msg=document.getElementById('msg');
 btn.disabled=true; msg.textContent='Starting...';
 const r=await fetch('api/collect',{method:'POST'}); const j=await r.json();
 msg.textContent = r.status===202 ? 'Collection started - refreshing...'
   : (j.detail||'Already running');
 setTimeout(async()=>{await refresh(); btn.disabled=false; msg.textContent='';}, 4000);
}
refresh(); setInterval(refresh, 5000);
</script></body></html>
"""


def create_app(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    lock: asyncio.Lock,
) -> FastAPI:
    """Build the FastAPI app sharing the collection lock with the scheduler."""
    app = FastAPI(title="Actian Companion")
    app.state.tasks = set()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _PAGE

    @app.get("/api/runs")
    async def recent_runs() -> list[dict[str, Any]]:
        async with session_factory() as session:
            result = await session.execute(
                select(CollectionRun).order_by(CollectionRun.id.desc()).limit(20)
            )
            runs = result.scalars().all()
        return [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "status": r.status,
                "users_collected": r.users_collected,
                "events_collected": r.events_collected,
                "items_collected": r.items_collected,
                "error_message": r.error_message,
            }
            for r in runs
        ]

    @app.post("/api/collect")
    async def trigger_collection() -> JSONResponse:
        if lock.locked():
            return JSONResponse(
                status_code=409, content={"detail": "A collection is already running"}
            )
        task = asyncio.create_task(run_collection(settings, session_factory, lock))
        # Hold a reference so the task is not garbage-collected mid-run.
        app.state.tasks.add(task)
        task.add_done_callback(app.state.tasks.discard)
        logger.info("Manual collection triggered via web UI")
        return JSONResponse(status_code=202, content={"detail": "Collection started"})

    return app
