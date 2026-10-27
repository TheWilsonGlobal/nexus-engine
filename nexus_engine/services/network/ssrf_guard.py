"""SSRF Guard: blocks loopback, private RFC 1918, link-local, and cloud metadata targets."""
from __future__ import annotations

import fnmatch
import ipaddress
import logging
import asyncio
import socket
from urllib.parse import urlparse
from typing import Optional, Sequence

log = logging.getLogger(__name__)

# Cloud metadata addresses to explicitly reject
DENIED_METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.internal",
    "instance-data",
}


# Uniform, non-diagnostic message handed to callers (the agent's fetch tool, API responses). The guard's own
# exceptions carry the specific reason for logs and tests, but telling an untrusted caller whether a host was
# blocked, private, or merely unresolvable would turn the fetch tool into an internal-network oracle.
GENERIC_BLOCKED_MESSAGE = "URL blocked by outbound network policy"

# Transition ranges that embed an IPv4 address (or relay to one) yet report is_global=True on Python 3.10.
_EMBEDDED_V4_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64 well-known prefix
    ipaddress.ip_network("64:ff9b:1::/48"),  # NAT64 local-use prefix (RFC 8215)
    ipaddress.ip_network("2002::/16"),  # 6to4
    ipaddress.ip_network("2001::/32"),  # Teredo
)


class SSRFValidationError(Exception):
    """Raised when an outbound URL violates SSRF safety policy."""
    pass


class SSRFGuard:
    """Validates URLs against private network ranges, loopbacks, and domain allowlists."""

    def __init__(
        self,
        allowed_domains: Optional[Sequence[str]] = None,
        allow_private_ips: bool = False,
        enabled: bool = True,
    ) -> None:
        self.allowed_domains = list(allowed_domains) if allowed_domains else ["*"]
        self.allow_private_ips = allow_private_ips
        self.enabled = enabled

    def is_domain_allowed(self, hostname: str) -> bool:
        """Check if hostname matches allowed domain patterns."""
        if "*" in self.allowed_domains:
            return True
        h = hostname.lower()
        for pattern in self.allowed_domains:
            p = pattern.lower().strip()
            if fnmatch.fnmatch(h, p):
                return True
        return False

    def validate_url(self, url: str) -> str:
        """Inspect and validate URL. Returns normalized URL if safe, raises SSRFValidationError if unsafe."""
        return self.validate_and_pin(url)[0]

    def validate_and_pin(self, url: str) -> tuple[str, Optional[str]]:
        """Validate like validate_url, and also return the vetted IP to connect to.

        The caller must connect to that IP rather than re-resolving the hostname: a second lookup
        can return a different (internal) address (DNS rebinding). None means no pinning applies
        (allow_private_ips is on, so nothing was vetted).

        Blocking variant (resolves with socket.getaddrinfo); async code should use ``avalidate_and_pin``.
        """
        hostname, port = self._precheck(url)
        if hostname is None:
            return url, None
        try:
            addr_info = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError, OSError) as e:
            raise SSRFValidationError(f"Could not resolve host '{hostname}': {e}") from e
        return url, self._vet(hostname, addr_info)

    async def avalidate_and_pin(self, url: str) -> tuple[str, Optional[str]]:
        """Same contract as ``validate_and_pin`` but resolves via the event loop, never blocking it."""
        hostname, port = self._precheck(url)
        if hostname is None:
            return url, None
        loop = asyncio.get_running_loop()
        try:
            addr_info = await loop.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError, OSError) as e:
            raise SSRFValidationError(f"Could not resolve host '{hostname}': {e}") from e
        return url, self._vet(hostname, addr_info)

    def _precheck(self, url: str) -> tuple[Optional[str], int]:
        """Scheme / host / allowlist checks. Returns (hostname, port), or (None, 0) when IP vetting is off."""
        if not self.enabled:
            raise SSRFValidationError("Network access is disabled by configuration (NETWORK_ENABLED=false)")

        try:
            parsed = urlparse(url)
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except Exception as e:  # noqa: BLE001 - malformed URL / port
            raise SSRFValidationError(f"Invalid URL structure: {e}") from e

        if parsed.scheme.lower() not in ("http", "https"):
            raise SSRFValidationError(f"Invalid URL scheme '{parsed.scheme}'; only http and https are permitted")

        hostname = parsed.hostname
        if not hostname:
            raise SSRFValidationError("URL contains no valid host")

        if hostname.lower() in DENIED_METADATA_HOSTS:
            raise SSRFValidationError(f"Access to cloud metadata host '{hostname}' is strictly denied")

        if not self.is_domain_allowed(hostname):
            raise SSRFValidationError(f"Domain '{hostname}' is not in allowed egress domains: {self.allowed_domains}")

        # allow_private_ips (testing / local endpoints): skip IP vetting entirely
        if self.allow_private_ips:
            return None, 0
        return hostname, port

    @staticmethod
    def _rejected_reason(ip: "ipaddress._BaseAddress") -> Optional[str]:
        """How to describe why ``ip`` must not be contacted, or None when it is a public unicast address."""
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped is not None:
                return "an IPv4-mapped IPv6 address"
            if any(ip in net for net in _EMBEDDED_V4_NETWORKS):
                return "a NAT64/6to4/Teredo transition address"
        if ip.is_loopback:
            return "a loopback address"
        if ip.is_private:
            return "in a private network range"
        if ip.is_link_local:
            return "a link-local address"
        if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return "multicast/reserved/unspecified"
        if str(ip) == "169.254.169.254":
            return "the cloud metadata address"
        if not ip.is_global:  # catch-all, e.g. 100.64.0.0/10 carrier-grade NAT
            return "not publicly routable"
        return None

    def _vet(self, hostname: str, addr_info: list) -> str:
        """Check every resolved address and return the first one (to pin)."""
        vetted: Optional[str] = None
        for _family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str.split("%")[0])
            except ValueError:
                raise SSRFValidationError(f"Destination address {ip_str!r} could not be parsed (SSRF blocked)") from None
            reason = self._rejected_reason(ip)
            if reason is not None:
                raise SSRFValidationError(f"Destination IP {ip_str} is {reason} (SSRF blocked)")
            vetted = vetted or ip_str

        if vetted is None:
            raise SSRFValidationError(f"Host '{hostname}' resolved to no usable address")
        return vetted
