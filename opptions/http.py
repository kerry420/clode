"""Small stdlib HTTP helpers shared by every tracker.

Only the standard library is used so the toolkit runs anywhere Python 3.10+
does, with no install step.
"""

import http.client
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
try:
    TIMEOUT = float(os.environ.get("OPPTIONS_TIMEOUT", "20"))
except ValueError:  # a bad value should not stop the CLI from starting
    TIMEOUT = 20.0


class FetchError(RuntimeError):
    """A source could not be reached. The message names the host."""


def fetch_text(url, headers=None):
    req_headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    req_headers.update(headers or {})
    req = urllib.request.Request(url, headers=req_headers)
    host = urlparse(url).hostname
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        raise FetchError(f"{host} answered HTTP {e.code} for {url}") from e
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise FetchError(
            f"could not reach {host} ({reason}). If this runs in a sandbox, "
            f"add {host} to its allowed network domains."
        ) from e


def fetch_json(url, headers=None):
    text = fetch_text(url, {"Accept": "application/json", **(headers or {})})
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        host = urlparse(url).hostname
        raise FetchError(f"{host} did not return JSON for {url}") from e
