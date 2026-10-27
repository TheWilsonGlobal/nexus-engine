"""Safe outbound HTTP client with SSRF protection, size caps, and text extraction."""
from __future__ import annotations

import html
import logging
import re
from typing import Any, Optional
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit, urlunsplit
import asyncio
import httpx

from nexus_engine.services.network.ssrf_guard import GENERIC_BLOCKED_MESSAGE, SSRFGuard, SSRFValidationError

log = logging.getLogger(__name__)

DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB
DEFAULT_TIMEOUT_S = 5.0
MAX_REDIRECTS = 5


def _pinned_request(url: str, ip: Optional[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Point the request at the vetted IP while keeping the original Host header and TLS name."""
    if ip is None:
        return url, {}, {}
    parts = urlsplit(url)
    host = parts.hostname or ""
    ip_host = f"[{ip}]" if ":" in ip else ip
    host_header = f"[{host}]" if ":" in host else host
    if parts.port:
        ip_host += f":{parts.port}"
        host_header += f":{parts.port}"
    pinned = urlunsplit((parts.scheme, ip_host, parts.path or "/", parts.query, ""))
    extensions: dict[str, Any] = {"sni_hostname": host} if parts.scheme.lower() == "https" else {}
    return pinned, {"Host": host_header}, extensions


def _clean_html_to_text(raw_html: str, max_chars: int = 4000) -> str:
    """Fast, safe regex-based HTML text extractor without requiring external C libraries."""
    # Strip script and style blocks
    cleaned = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    # Strip remaining tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Decode HTML entities
    cleaned = html.unescape(cleaned)
    # Normalize whitespaces
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s*\n+", "\n\n", cleaned).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + f"\n... [truncated to {max_chars} chars]"
    return cleaned


def _decode_ddg_url(href: str) -> str:
    """DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<percent-encoded target>; return the target."""
    href = html.unescape(href.strip())
    parts = urlsplit(href)
    if (parts.hostname or "").endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg", [""])[0]
        if urlsplit(target).scheme in ("http", "https"):
            return target
    if href.startswith("//"):
        return "https:" + href
    return href


class SafeHTTPClient:
    """Controlled outbound HTTP client enforcing SSRF protection and bounded execution."""

    def __init__(
        self,
        guard: Optional[SSRFGuard] = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.guard = guard or SSRFGuard()
        self.timeout = timeout
        self.max_body_bytes = max_body_bytes
        self._client = client
        self._client_loop: Optional[asyncio.AbstractEventLoop] = None

    async def _get_client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        stale = self._client_loop is not None and self._client_loop is not loop
        if self._client is None or self._client.is_closed or stale:
            # An AsyncClient's pooled connections belong to one event loop; never reuse it on another.
            headers = {"User-Agent": "NexusKernel-Agent/0.3 (+https://github.com/nexus-kernel)"}
            # Redirects are followed by hand in fetch(): httpx would SEND each hop before we could vet it.
            # trust_env=False: ignore HTTP(S)_PROXY / NO_PROXY / netrc, which would route around the pinned-IP check.
            self._client = httpx.AsyncClient(
                headers=headers, timeout=self.timeout, follow_redirects=False, trust_env=False
            )
        self._client_loop = loop
        return self._client

    async def fetch(self, url: str) -> str:
        """Fetch URL content after SSRF validation, enforcing payload limits.

        Every hop (the URL and each redirect target) is validated BEFORE it is requested, and the
        connection goes to the IP the guard vetted, not to a fresh DNS answer.
        """
        client = await self._get_client()
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                safe_url, ip = await self.guard.avalidate_and_pin(current)
            except SSRFValidationError as exc:
                if not self.guard.enabled:
                    raise  # configuration message, not target-specific
                log.warning("Outbound URL rejected: %s", exc)
                raise SSRFValidationError(GENERIC_BLOCKED_MESSAGE) from None
            req_url, headers, extensions = _pinned_request(safe_url, ip)
            async with client.stream("GET", req_url, headers=headers, extensions=extensions) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location", "")
                    if not location:
                        raise ValueError(f"Redirect from {safe_url} without a Location header")
                    current = urljoin(safe_url, location)
                    continue
                return await self._read_body(resp)
        raise SSRFValidationError(f"Too many redirects (more than {MAX_REDIRECTS})")

    async def _read_body(self, resp: httpx.Response) -> str:
        resp.raise_for_status()

        body_chunks = []
        total_bytes = 0
        async for chunk in resp.aiter_bytes():
            total_bytes += len(chunk)
            if total_bytes > self.max_body_bytes:
                raise ValueError(f"Response size exceeded limit of {self.max_body_bytes} bytes")
            body_chunks.append(chunk)

        raw_bytes = b"".join(body_chunks)
        encoding = resp.encoding or "utf-8"
        return raw_bytes.decode(encoding, errors="replace")

    async def fetch_text(self, url: str, max_chars: int = 4000) -> str:
        """Fetch URL and convert HTML/JSON to readable text."""
        raw = await self.fetch(url)
        if "<html" in raw.lower() or "<body" in raw.lower():
            return _clean_html_to_text(raw, max_chars=max_chars)
        if len(raw) > max_chars:
            return raw[:max_chars] + f"\n... [truncated to {max_chars} chars]"
        return raw

    async def web_search(self, query: str, num_results: int = 5) -> list[dict[str, str]]:
        """Execute web search using DuckDuckGo HTML search or return structured results."""
        if not self.guard.enabled:
            raise SSRFValidationError("Web search is disabled because network access is turned off")

        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        # Errors propagate: "the search failed" must not look like "the search found nothing".
        html_text = await self.fetch(url)

        # Parse snippet results
        results: list[dict[str, str]] = []
        # Pattern matching result titles and snippets from DuckDuckGo HTML
        snippets = re.findall(
            r'<a class="result__snippet[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html_text,
            flags=re.DOTALL,
        )
        if not snippets:
            # Fallback regex for standard DDG HTML result links
            links = re.findall(r'<a class="result__url"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html_text)
            for href, text in links[:num_results]:
                results.append({"title": text.strip(), "url": _decode_ddg_url(href), "snippet": text.strip()})
        else:
            for href, text in snippets[:num_results]:
                clean_snippet = _clean_html_to_text(text, max_chars=300)
                results.append({"title": clean_snippet[:60], "url": _decode_ddg_url(href), "snippet": clean_snippet})

        return results
