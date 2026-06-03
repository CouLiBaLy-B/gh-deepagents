"""Tiny single-purpose reverse proxy used in the HF all-in-one Space.

HF only exposes ONE port (7860). We need both:
  - the Streamlit dashboard (for the user)
  - the webhook endpoint (for GitHub)

This router multiplexes both behind /7860. Path-based:
    /webhook, /healthz, /metrics, /jobs, /dlq, /installations, /audit, /me
        → http://localhost:8080  (webhook server)
    everything else
        → http://localhost:8501  (streamlit)

We use plain stdlib + httpx (no heavy framework) so the container starts fast.
WebSocket upgrades for Streamlit are forwarded so the live tail keeps working.
"""
from __future__ import annotations

import logging
import os

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket
import asyncio
import websockets

WEBHOOK_BASE = os.getenv("DEEPAGENT_WEBHOOK_BASE", "http://127.0.0.1:8080")
STREAMLIT_BASE = os.getenv("DEEPAGENT_STREAMLIT_BASE", "http://127.0.0.1:8501")
PORT = int(os.getenv("PORT", "7860"))

API_PREFIXES = ("/webhook", "/healthz", "/metrics", "/jobs", "/dlq",
                "/installations", "/audit", "/me")

log = logging.getLogger("router")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _is_api(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in API_PREFIXES)


_client = httpx.AsyncClient(timeout=None)


async def proxy_http(request: Request) -> Response:
    """Forward an HTTP request to either the webhook or the dashboard."""
    target = WEBHOOK_BASE if _is_api(request.url.path) else STREAMLIT_BASE
    url = httpx.URL(f"{target}{request.url.path}").copy_with(query=request.url.query.encode())
    # Drop hop-by-hop headers and the Host (proxied service rewrites it).
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "connection", "content-length")}

    if _is_streaming_path(request.url.path):
        async def _stream():
            async with _client.stream(request.method, str(url), headers=headers,
                                       content=request.stream()) as r:
                async for chunk in r.aiter_raw():
                    yield chunk
        # Initial request to get headers, then re-stream — easier path:
        async with _client.stream(request.method, str(url), headers=headers,
                                  content=await request.body()) as r:
            return StreamingResponse(
                r.aiter_raw(),
                status_code=r.status_code,
                headers={k: v for k, v in r.headers.items()
                         if k.lower() not in ("content-encoding", "transfer-encoding",
                                              "content-length")},
                media_type=r.headers.get("content-type"),
            )

    body = await request.body()
    r = await _client.request(request.method, str(url), headers=headers, content=body)
    out_headers = {k: v for k, v in r.headers.items()
                   if k.lower() not in ("content-encoding", "transfer-encoding")}
    return Response(content=r.content, status_code=r.status_code, headers=out_headers)


def _is_streaming_path(path: str) -> bool:
    return path.endswith("/stream") or "/stream/" in path


async def proxy_ws(websocket: WebSocket) -> None:
    """Forward Streamlit's websocket (server-side reactivity)."""
    await websocket.accept()
    target = STREAMLIT_BASE.replace("http://", "ws://", 1) + websocket.url.path
    try:
        async with websockets.connect(target) as ws_target:
            async def from_client():
                try:
                    while True:
                        msg = await websocket.receive()
                        if "text" in msg and msg["text"] is not None:
                            await ws_target.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await ws_target.send(msg["bytes"])
                        elif msg.get("type") == "websocket.disconnect":
                            return
                except Exception:
                    return

            async def from_target():
                try:
                    async for msg in ws_target:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception:
                    return

            await asyncio.gather(from_client(), from_target())
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


app = Starlette(routes=[
    WebSocketRoute("/_stcore/stream", proxy_ws),
    Route("/{path:path}", proxy_http,
          methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
])


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
