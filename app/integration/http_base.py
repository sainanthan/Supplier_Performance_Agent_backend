"""Shared, read-only HTTP layer for API connectors (SAP OData, REST, MES REST) - DI Phase 4.

Security
- READ-ONLY: only GET requests reach the source (plus a POST to the OAuth2 token endpoint, which reads nothing).
- SSRF guard: every request's host is resolved and checked. Always blocked: link-local / cloud metadata (169.254.x, fe80::),
  multicast, unspecified and reserved ranges. Private / loopback addresses (an on-prem SAP, a local MES) only when an ADMIN
  marks the source as "internal network" - loopback additionally only in DEV. Redirects are never followed (a redirect to an
  internal address is a classic SSRF trick); paging links must stay on the same host.
- TLS: https required outside DEV; certificate verification on by default (may only be relaxed in DEV).
- Limits: per-request timeout, response size cap, retries with backoff on 429 / 5xx respecting Retry-After (capped).
- Secrets (password / token / API key / client secret) come from the vault; they are never logged or returned.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urljoin, urlparse

import httpx

from .base import Field, TestResult
from .db_connectors import _b

MAX_BYTES = 50 * 1024 * 1024
TIMEOUT = 30
RETRY_STATUS = {429, 502, 503, 504}


class BlockedTarget(ValueError):
    pass


def check_host(host: str, internal_ok: bool, environment: str) -> None:
    """Raise BlockedTarget unless every address the host resolves to is allowed."""
    if not host:
        raise BlockedTarget("no host in the URL")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise BlockedTarget(f"the host name '{host}' could not be resolved") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or (ip.is_reserved and not ip.is_private):
            raise BlockedTarget(f"{host} resolves to {ip}, a link-local / metadata / reserved address - always blocked")
        if ip.is_loopback and not (internal_ok and environment == "DEV"):
            raise BlockedTarget(f"{host} resolves to the backend itself ({ip}) - only allowed for internal-network sources in DEV")
        if ip.is_private and not internal_ok:
            raise BlockedTarget(f"{host} resolves to the private address {ip}. Tick 'Source is on the internal network' if this is intended "
                                "(an ADMIN decision; production sources also need a second ADMIN's approval)")


def auth_fields() -> list[Field]:
    return [Field("auth_type", "Authentication", "select", True, "basic", options=["basic", "bearer", "api_key", "oauth2_client_credentials", "none"]),
            Field("username", "Username", help="Use a READ-ONLY (display-only) user"),
            Field("password", "Password", "secret"),
            Field("token", "Bearer token", "secret", group="auth"),
            Field("api_key_header", "API-key header name", default="APIKey", group="auth", help="SAP API Business Hub uses 'APIKey'"),
            Field("api_key", "API key", "secret", group="auth"),
            Field("token_url", "OAuth2 token URL", group="auth"),
            Field("client_id", "OAuth2 client id", group="auth"),
            Field("client_secret", "OAuth2 client secret", "secret", group="auth"),
            Field("scope", "OAuth2 scope", group="auth"),
            Field("tls_verify", "Verify server certificate", "boolean", default=True, group="tls", help="Required outside DEV"),
            Field("internal_network", "Source is on the internal network", "boolean", default=False, group="advanced",
                  help="Allows private addresses (on-prem SAP / MES). Link-local / metadata addresses are always blocked."),
            Field("timeout_s", "Request timeout (seconds)", "number", default=TIMEOUT, group="advanced"),
            Field("page_size", "Page size", "number", default=1000, group="advanced")]


def http_policy_errors(url_field: str, config: dict, secrets: dict, environment: str) -> list[str]:
    errs = []
    u = urlparse(config.get(url_field) or "")
    if u.scheme not in ("https", "http") or not u.hostname:
        errs.append("the URL must start with https://")
    elif u.scheme == "http" and environment != "DEV":
        errs.append("https is required for TEST and PROD")
    if environment != "DEV" and not _b(config.get("tls_verify"), True):
        errs.append("certificate verification can only be switched off in DEV")
    a = config.get("auth_type") or "basic"
    need = {"basic": [("username", config), ("password", secrets)], "bearer": [("token", secrets)], "api_key": [("api_key", secrets)],
            "oauth2_client_credentials": [("token_url", config), ("client_id", config), ("client_secret", secrets)]}.get(a, [])
    errs += [f"{n.replace('_', ' ')} is required for {a} authentication" for n, src in need if not src.get(n)]
    if a == "oauth2_client_credentials" and config.get("token_url") and not str(config["token_url"]).startswith("https://") and environment != "DEV":
        errs.append("the OAuth2 token URL must use https")
    return errs


class HttpSource:
    """GET-only client with SSRF guard, auth, retries, size cap. One per connector instance."""

    def __init__(self, base_url: str, config: dict, secrets: dict, environment: str = "DEV"):
        self.base, self.cfg, self.sec, self.env = base_url.rstrip("/") + "/", config, secrets, environment
        self.host = urlparse(self.base).hostname
        self.internal = _b(config.get("internal_network"), False)
        self.verify = _b(config.get("tls_verify"), True)
        self.timeout = float(config.get("timeout_s") or TIMEOUT)
        self._token: tuple[str, float] | None = None
        self.client = httpx.Client(verify=self.verify, timeout=self.timeout, follow_redirects=False,
                                   headers={"User-Agent": "supplier-performance-agent/data-integration", "Accept": "application/json"})

    def close(self):
        self.client.close()

    # ---- auth
    def _auth(self) -> tuple[dict, httpx.Auth | None]:
        a = self.cfg.get("auth_type") or "basic"
        if a == "basic":
            return {}, httpx.BasicAuth(self.cfg.get("username") or "", self.sec.get("password") or "")
        if a == "bearer":
            return {"Authorization": f"Bearer {self.sec.get('token')}"}, None
        if a == "api_key":
            return {self.cfg.get("api_key_header") or "APIKey": self.sec.get("api_key") or ""}, None
        if a == "oauth2_client_credentials":
            if not self._token or self._token[1] < time.time() + 30:
                tu = self.cfg.get("token_url")
                check_host(urlparse(tu).hostname, self.internal, self.env)
                r = self.client.post(tu, data={"grant_type": "client_credentials", "client_id": self.cfg.get("client_id"),
                                               "client_secret": self.sec.get("client_secret"), **({"scope": self.cfg["scope"]} if self.cfg.get("scope") else {})})
                if r.status_code != 200:
                    raise PermissionError(f"OAuth2 token request failed (HTTP {r.status_code}) - check client id / secret / scope")
                j = r.json()
                self._token = (j["access_token"], time.time() + float(j.get("expires_in") or 300))
            return {"Authorization": f"Bearer {self._token[0]}"}, None
        return {}, None

    # ---- GET
    def url(self, path_or_url: str) -> str:
        u = urljoin(self.base, path_or_url)
        if urlparse(u).hostname != self.host:
            raise BlockedTarget(f"refusing to follow a link to another host ({urlparse(u).hostname})")
        return u

    def get(self, path_or_url: str, params: dict | None = None, accept: str | None = None) -> httpx.Response:
        u = self.url(path_or_url)
        check_host(self.host, self.internal, self.env)
        headers, auth = self._auth()
        if accept:
            headers["Accept"] = accept
        last = None
        for attempt in range(4):
            with self.client.stream("GET", u, params=params, headers=headers, auth=auth) as r:
                if r.status_code in RETRY_STATUS and attempt < 3:
                    wait = min(float(r.headers.get("Retry-After") or 0) or 2 ** attempt, 30)
                    last = r.status_code
                    time.sleep(wait)
                    continue
                if 300 <= r.status_code < 400:
                    raise BlockedTarget(f"the source answered with a redirect (HTTP {r.status_code}) - redirects are not followed for security; "
                                        "use the final URL")
                size, chunks = 0, []
                for ch in r.iter_bytes():
                    size += len(ch)
                    if size > MAX_BYTES:
                        raise ValueError(f"response larger than {MAX_BYTES // 1024 // 1024} MB - use a smaller page size or a filter")
                    chunks.append(ch)
                r._content = b"".join(chunks)
                return r
        raise ConnectionError(f"the source kept answering HTTP {last} (busy / unavailable) - try again later")


def http_error(e: Exception) -> TestResult:
    """Plain-language HTTP failures."""
    raw = " ".join(str(e).split())[:300]
    if isinstance(e, BlockedTarget):
        return TestResult(False, "Blocked for security.", raw, {"category": "BLOCKED"})
    if isinstance(e, httpx.ConnectTimeout) or isinstance(e, httpx.ReadTimeout):
        return TestResult(False, "No answer in time.", "Check the URL; a firewall / VPN may block the backend, or raise the timeout.", {"category": "TIMEOUT", "error": raw})
    if isinstance(e, httpx.ConnectError):
        cat = "TLS" if "certificate" in raw.lower() or "ssl" in raw.lower() else "REFUSED"
        hint = ("The TLS certificate was not accepted - check the host name and certificate (only DEV may skip verification)." if cat == "TLS"
                else "Could not connect - check the host and port, and that the service is reachable from the backend.")
        return TestResult(False, "Could not connect.", hint, {"category": cat, "error": raw})
    if isinstance(e, PermissionError):
        return TestResult(False, "Authentication failed.", raw, {"category": "AUTH"})
    return TestResult(False, "The request failed.", raw, {"category": "OTHER", "error": raw})


def status_hint(code: int) -> str:
    return {401: "The credentials were rejected (check user / password / token / API key).",
            403: "Authenticated, but this user may not read this service - ask for display (read) authorisation.",
            404: "Not found - check the service URL / path.", 429: "Too many requests - the source is rate-limiting; try later or lower the page size."}.get(
        code, f"The source answered HTTP {code}.")
