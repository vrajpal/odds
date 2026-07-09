import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def fixture_transport(name: str, *, quota_remaining: str = "497") -> httpx.MockTransport:
    """A transport that serves a recorded The Odds API response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=load_fixture(name),
            headers={"x-requests-remaining": quota_remaining, "x-requests-used": "3"},
        )

    return httpx.MockTransport(handler)
