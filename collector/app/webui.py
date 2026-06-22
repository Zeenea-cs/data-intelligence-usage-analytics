"""Lightweight web UI for the collector.

Features:
* a "Run collection now" button to trigger a cycle on demand (shares the
  collection lock with the scheduler, so manual and cron runs never overlap);
* a table of recent collection runs;
* a per-service log viewer that tails each service's log file on demand.

Mounted by main.py alongside the AsyncIOScheduler in the same event loop.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logs import available_services, tail_log
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
 h2{font-size:1.1rem;margin-top:2rem}
 #logbox{background:#0d1117;color:#d1d5da;padding:.8rem;border-radius:6px;
   font-family:ui-monospace,monospace;font-size:.8rem;white-space:pre-wrap;
   overflow:auto;max-height:420px;margin-top:.6rem}
 select,input{font-size:.9rem;padding:.3rem}
</style></head><body>
<h1>Actian Data Intelligence Companion</h1>
<p>Data Catalog instance: <code>__INSTANCE_URL__</code></p>
<p>Trigger a data collection now, bypassing the cron schedule.</p>
<button id="run" onclick="trigger()">Run collection now</button>
<span id="msg"></span>
<table id="runs"><thead><tr>
 <th>Started</th><th>Finished</th><th>Status</th>
 <th>Users</th><th>Events</th><th>Items</th><th>Error</th>
</tr></thead><tbody></tbody></table>

<h2>Service logs</h2>
<label>Service <select id="svc"></select></label>
<label>Lines <input id="lines" type="number" value="200" min="1" max="2000" style="width:6rem"></label>
<button onclick="loadLogs()">Load logs</button>
<label style="margin-left:1rem"><input id="auto" type="checkbox"> auto-refresh</label>
<div id="logbox">Select a service and click "Load logs".</div>

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
async function loadServices(){
 const svc=document.getElementById('svc');
 const names=await (await fetch('api/logs/services')).json();
 svc.innerHTML = names.length
   ? names.map(n=>`<option value="${n}">${n}</option>`).join('')
   : '<option value="">(no logs available)</option>';
}
async function loadLogs(){
 const svc=document.getElementById('svc').value;
 const lines=document.getElementById('lines').value||200;
 const box=document.getElementById('logbox');
 if(!svc){box.textContent='No service selected.';return;}
 const r=await fetch(`api/logs/${encodeURIComponent(svc)}?lines=${lines}`);
 box.textContent = r.ok ? (await r.text() || '(empty)') : `Error ${r.status}`;
 box.scrollTop = box.scrollHeight;
}
loadServices(); refresh(); setInterval(refresh, 5000);
// Optional auto-refresh of the currently selected log.
setInterval(()=>{ if(document.getElementById('auto').checked) loadLogs(); }, 5000);
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

    # Render the configured instance URL into the static page once at build time.
    page = _PAGE.replace("__INSTANCE_URL__", html.escape(settings.actian_instance_url))

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return page

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

    @app.get("/api/logs/services")
    async def log_services() -> list[str]:
        """List services whose log file currently exists in the shared log dir."""
        return available_services(settings.log_dir)

    @app.get("/api/logs/{service}", response_class=PlainTextResponse)
    async def service_log(service: str, lines: int = 200) -> PlainTextResponse:
        """Return the tail of one service's log (404 if unknown/unavailable)."""
        content = tail_log(settings.log_dir, service, lines)
        if content is None:
            return PlainTextResponse("log not available", status_code=404)
        return PlainTextResponse(content)

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
