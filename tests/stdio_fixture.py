"""Test-only process launcher: redirect HTTP to the local Discord simulator."""

import os
from pathlib import Path
import sys
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request):
        base = httpx.URL(os.environ["TEST_DISCORD_URL"])
        request.url = request.url.copy_with(scheme=base.scheme, host=base.host, port=base.port)
        return await super().handle_async_request(request)


RealClient = httpx.AsyncClient


def test_client(**kwargs):
    return RealClient(transport=TestTransport(), **kwargs)


httpx.AsyncClient = test_client

with patch("dotenv.load_dotenv"):
    import server

server.mcp.run(show_banner=False)
