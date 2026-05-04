"""PO Monitor — FastAPI entry point.

Serves React frontend + JSON API. Honors OBO user auth via
X-Forwarded-Access-Token header on every data-touching route.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from server import db
from server.config import WAREHOUSE_OVERRIDE, get_workspace_host, preflight_sp_warehouse_access
from server.routes import actions as action_routes
from server.routes import alerts as alert_routes
from server.routes import card_cache as card_cache_routes
from server.routes import catalog as catalog_routes
from server.routes import config as config_routes
from server.routes import dashboards as dashboard_routes
from server.routes import feedback as feedback_routes
from server.routes import po as po_routes
from server.routes import schedules as schedule_routes
from server.scheduler import schedules_tick_loop
from server.alerts_engine import alerts_tick_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bootstrap UC persistence (idempotent). If this fails we don't crash the
    # app — we surface it via /api/health so the UI can show an amber banner.
    try:
        db.bootstrap()
    except Exception as e:
        print(f"[lifespan] bootstrap error (non-fatal): {e}")

    # Preflight: verify the app SP can use the default warehouse. Surfaces
    # missing CAN_USE drift at startup as a single warning rather than as
    # runtime 500s from every scheduler/alerts tick.
    ok, msg = preflight_sp_warehouse_access()
    print(f"[lifespan] warehouse-preflight: {'OK' if ok else 'WARN'} — {msg}")
    app.state.preflight_ok = ok
    app.state.preflight_msg = msg

    # Start the schedules tick loop. Fire-and-forget task; cancelled on shutdown.
    # If bootstrap failed the loop will still run but its DB calls will error
    # out one-by-one — logged but non-fatal.
    trigger_task: asyncio.Task | None = None
    alerts_task: asyncio.Task | None = None
    try:
        trigger_task = asyncio.create_task(schedules_tick_loop(), name="schedules_tick_loop")
        app.state.trigger_task = trigger_task
    except Exception as e:
        print(f"[lifespan] failed to start schedules loop: {e}")

    try:
        alerts_task = asyncio.create_task(alerts_tick_loop(), name="alerts_tick_loop")
        app.state.alerts_task = alerts_task
    except Exception as e:
        print(f"[lifespan] failed to start alerts loop: {e}")

    try:
        yield
    finally:
        for t in (trigger_task, alerts_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


app = FastAPI(title="PO Monitor", version="0.2.0", lifespan=lifespan)


_APP_DEBUG = os.environ.get("PO_MONITOR_DEBUG", "").lower() in ("1", "true", "yes")


@app.exception_handler(Exception)
async def _debug_exception_handler(request: Request, exc: Exception):
    """Always log the traceback server-side; only echo it to clients in debug mode.

    Set PO_MONITOR_DEBUG=1 to surface tracebacks in JSON responses for local
    troubleshooting. In prod, clients get a generic error and the operator
    reads the server log for the full trace.
    """
    import traceback as _tb
    tb = _tb.format_exc()
    print(f"[unhandled] {request.url.path}: {tb}")
    body: dict = {"error": type(exc).__name__, "detail": "internal error"}
    if _APP_DEBUG:
        body["detail"] = str(exc)
        body["traceback"] = tb.splitlines()[-12:]
    return JSONResponse(body, status_code=500)


@app.middleware("http")
async def warehouse_header_middleware(request: Request, call_next):
    """Pipe the X-Warehouse-Id header into a contextvar so every SQL query this
    request makes runs on the user's currently-selected warehouse."""
    wh = request.headers.get("x-warehouse-id")
    token = WAREHOUSE_OVERRIDE.set(wh) if wh else None
    try:
        return await call_next(request)
    finally:
        if token is not None:
            WAREHOUSE_OVERRIDE.reset(token)

app.include_router(catalog_routes.router)
app.include_router(po_routes.router)
app.include_router(action_routes.router)
app.include_router(alert_routes.router)
app.include_router(config_routes.router)
app.include_router(feedback_routes.router)
app.include_router(dashboard_routes.router)
app.include_router(schedule_routes.router)
app.include_router(card_cache_routes.router)


@app.get("/api/health")
def health():
    state = db.bootstrap_state()
    preflight_ok = getattr(app.state, "preflight_ok", None)
    preflight_msg = getattr(app.state, "preflight_msg", None)
    return {
        "ok": True,
        "bootstrap": state,
        "warehouse_preflight": {
            "ok": preflight_ok,
            "message": preflight_msg,
        },
    }


@app.get("/sign-out", response_class=HTMLResponse)
def sign_out():
    """Real sign-out that kills both the account AND workspace sessions.

    The workspace SSO cookie (on the workspace domain, not accounts.cloud) is
    what the Apps OAuth flow checks. So we need to kill both:

      1. Hidden iframe loads accounts.cloud.databricks.com/logout  (kills account session)
      2. Top-level redirect to {workspace}/account/signout?next_url=<app>  (kills workspace
         session, then redirects back to the app — OAuth kicks in fresh)

    Ordering matters: we fire the iframe first (background), then after a brief
    delay navigate top-level to the workspace signout with return URL.
    """
    host = get_workspace_host().rstrip("/")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Signing out…</title>
  <style>
    body {{ font: 14px/1.4 system-ui, -apple-system, sans-serif;
           background: #0e0e12; color: #e5e7eb; margin: 0;
           display: flex; align-items: center; justify-content: center;
           height: 100vh; }}
    .box {{ text-align: center; }}
    .spinner {{ width: 40px; height: 40px; margin: 0 auto 16px;
               border: 3px solid rgba(255,255,255,0.15);
               border-top-color: #60a5fa; border-radius: 50%;
               animation: spin 0.8s linear infinite; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  </style>
</head>
<body>
  <div class="box">
    <div class="spinner"></div>
    <div>Signing out of Databricks…</div>
  </div>
  <!-- Two hidden iframes run concurrently to clear both the account-level
       and workspace-level Databricks session cookies. /account/signout
       returns 303 to a login page but the Set-Cookie clear-fires regardless. -->
  <iframe src="https://accounts.cloud.databricks.com/logout" style="display:none"></iframe>
  <iframe src="{host}/account/signout" style="display:none"></iframe>
  <script>
    // Give the iframes ~1.5s to hit their endpoints and clear cookies,
    // then redirect the main window to the Apps proxy /logout which
    // clears the app-side session cookie and bounces to OAuth.
    setTimeout(function() {{
      window.location.replace('/logout');
    }}, 1500);
  </script>
</body>
</html>"""


# ----- Static frontend -----
FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"error": "Not found"}, status_code=404)
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"error": "frontend not built — run `npm run build` in frontend/"}, status_code=503)
else:
    @app.get("/")
    async def root():
        return {
            "status": "running",
            "note": "Frontend not built. Run `cd frontend && npm install && npm run build` to serve the UI.",
            "api_docs": "/docs",
        }
