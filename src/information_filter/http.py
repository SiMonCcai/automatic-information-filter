"""HTTP helpers shared by built-in network adapters."""

from __future__ import annotations

from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), parsed.hostname, port


class SameOriginRedirectHandler(HTTPRedirectHandler):
    """Allow redirects only when scheme, host, and effective port stay unchanged."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _origin(req.full_url) != _origin(redirected.full_url):
            raise HTTPError(
                newurl,
                code,
                "Refusing a cross-origin redirect to protect request credentials",
                headers,
                fp,
            )
        return redirected


def open_request(request: Request, timeout: float):
    """Open an HTTP request without forwarding headers across origins."""
    return build_opener(SameOriginRedirectHandler()).open(request, timeout=timeout)
