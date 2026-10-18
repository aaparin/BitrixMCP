from __future__ import annotations

import html
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse

from bitrix_mcp.oauth.provider import CALLBACK_PATH


def _page(title: str, body: str, *, ok: bool = True) -> HTMLResponse:
    color = '#0f766e' if ok else '#b91c1c'
    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 0; background: #f8fafc; color: #0f172a; }}
    main {{ max-width: 40rem; margin: 4rem auto; padding: 2rem; background: white; border-radius: 12px;
            box-shadow: 0 10px 30px rgba(15,23,42,.08); }}
    h1 {{ margin-top: 0; color: {color}; font-size: 1.5rem; }}
    p {{ line-height: 1.5; }}
    .muted {{ color: #64748b; font-size: .95rem; }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(title)}</h1>
    {body}
  </main>
</body>
</html>"""
    return HTMLResponse(content)


def _install_finish_script() -> str:
    """Complete the Bitrix24 UI-app installation from its iframe wizard."""
    return """
<script src="//api.bitrix24.com/api/v1/"></script>
<script>
  (function () {
    if (window.BX24) {
      BX24.init(function () {
        BX24.installFinish();
      });
    }
  }());
</script>
"""


def register_healthz(server: Any) -> None:
    @server.custom_route('/healthz', methods=['GET'])
    async def healthz(_request: Request) -> PlainTextResponse:
        return PlainTextResponse('ok')


def register_oauth_routes(server: Any, *, mcp_url: str) -> None:
    """Pages Bitrix24 opens inside the portal.

    ``GET /oauth/callback`` is the OAuth proxy's redirect_uri and is registered by
    FastMCP itself. Bitrix24 also POSTs the UI-app installation and placement
    frames to the application's handler URL; those requests are answered here.
    Starlette falls through to the proxy's GET route on a method mismatch.
    """
    register_healthz(server)

    async def app_page(request: Request) -> HTMLResponse:
        url = html.escape(mcp_url)
        body = (
            '<p>This application connects Bitrix24 to AI assistants over MCP.</p>'
            f'<p>Add a custom connector in Claude with the URL <code>{url}</code> '
            'and sign in with your Bitrix24 account when prompted.</p>'
            '<p class="muted">Actions run with your own Bitrix24 permissions.</p>'
        )
        if request.method == 'POST':
            body += _install_finish_script()
        return _page('BitrixMCP', body)

    server.custom_route(CALLBACK_PATH, methods=['POST'])(app_page)
    server.custom_route('/bitrix/app', methods=['GET', 'POST'])(app_page)
