"""OpenRouter transport and credentials; no automatic retries or data writes."""

import json
import os
import ssl
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from picton_graph.config import ROOT

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def api_key():
    value = os.environ.get("OPENROUTER_API_KEY")
    if not value and (ROOT / ".env").is_file():
        for line in (ROOT / ".env").read_text().splitlines():
            key, separator, candidate = line.partition("=")
            if separator and key.strip() == "OPENROUTER_API_KEY":
                value = candidate.strip().strip("\"'")
    if not value or any(c.isspace() for c in value):
        raise ValueError("Set OPENROUTER_API_KEY in the process environment or project .env.")
    return value


def call_openrouter(request, api_key):
    if not api_key or any(c.isspace() for c in api_key):
        raise ValueError("Set OPENROUTER_API_KEY in the process environment.")
    context = ssl.create_default_context()
    # Same verified-CA fallback as the existing SEC client on Python.org macOS builds.
    if (
        sys.platform == "darwin"
        and not ssl.get_default_verify_paths().cafile
        and not os.getenv("SSL_CERT_FILE")
    ):
        context.load_verify_locations("/etc/ssl/cert.pem")
    req = Request(
        ENDPOINT,
        data=json.dumps(request).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=120, context=context) as response:
            return json.load(response)
    except HTTPError as exc:
        # Do not log provider error bodies, which can echo request data or credentials.
        raise RuntimeError(f"OpenRouter HTTP {exc.code}; no automatic retry was made.") from None
    except OSError:
        raise RuntimeError("OpenRouter connection failed; no automatic retry was made.") from None
