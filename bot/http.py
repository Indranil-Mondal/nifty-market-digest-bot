"""HTTP layer.

Indian exchange endpoints fall into two very different classes and this module treats them
differently on purpose:

  * Static archive hosts (nsearchives, amfiindia, niftyindices report files) serve plain files
    and need little more than a credible User-Agent.
  * Cookie-gated JSON APIs (www.nseindia.com/api/*, api.bseindia.com) reject a cold request.
    They need a homepage visit first to collect session cookies, and a Referer that matches
    the page a browser would have been on.

Everything retries with backoff and every failure is non-fatal: a source that dies returns
None and the digest prints an em dash for that cell. A missing number is acceptable; a crashed
run that sends nothing is not.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

# A real, current desktop UA. Exchange WAFs reject obvious library defaults, and this is the
# single most common reason an unattended fetch starts failing.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
# Accept-Encoding is deliberately NOT set here. requests advertises exactly the encodings it can
# actually decode (gzip and deflate, plus br/zstd only when those optional libraries are
# present). Hard-coding "gzip, deflate, br" makes servers reply with Brotli that requests then
# cannot decompress, so resp.content is raw compressed bytes -- which surfaces as
# "not well-formed (invalid token): line 1, column 0" from the XML parser and looks exactly like
# a dead feed. Let requests negotiate it.

DEFAULT_TIMEOUT = 25
DEFAULT_RETRIES = 3


class Http:
    """A requests.Session with sane retries and per-host warm-up support."""

    def __init__(self, *, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES):
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self._warmed: set[str] = set()

    def warm(self, homepage: str, *, force: bool = False) -> bool:
        """Visit a site's homepage to collect the session cookies its API requires."""
        if homepage in self._warmed and not force:
            return True
        try:
            resp = self.session.get(
                homepage,
                timeout=self.timeout,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            )
        except requests.RequestException as exc:
            log.warning("warm-up failed for %s: %s", homepage, exc)
            return False
        if resp.ok:
            self._warmed.add(homepage)
            return True
        log.warning("warm-up for %s returned HTTP %s", homepage, resp.status_code)
        return False

    def get(
        self,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        warm: Optional[str] = None,
        expect: str = "text",
        allow_status: tuple[int, ...] = (200,),
        timeout: Optional[int] = None,
    ) -> Optional[Any]:
        """GET with backoff. Returns text, parsed JSON, bytes, or None on failure.

        `warm` names a homepage to visit first if the request comes back unauthorised --
        we try cold first because the warm-up costs a round trip we usually do not need.
        """
        merged = dict(headers or {})
        delay = 1.5
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(url, headers=merged, params=params, timeout=timeout or self.timeout)
            except requests.RequestException as exc:
                log.warning("GET %s failed (%s/%s): %s", url, attempt, self.retries, exc)
            else:
                if resp.status_code in allow_status:
                    return self._decode(resp, expect, url)
                log.warning("GET %s -> HTTP %s (%s/%s)", url, resp.status_code, attempt, self.retries)
                # 401/403 from a cookie-gated API is the signal to warm up and retry.
                if resp.status_code in (401, 403, 419) and warm:
                    self.warm(warm, force=True)
                if resp.status_code == 429:
                    time.sleep(min(delay * 4, 30))
            if attempt < self.retries:
                time.sleep(delay)
                delay *= 2
        return None

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: Optional[dict[str, str]] = None,
        warm: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Optional[Any]:
        merged = {"Content-Type": "application/json; charset=UTF-8", "Accept": "application/json, text/javascript, */*; q=0.01"}
        merged.update(headers or {})
        delay = 1.5
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.post(url, json=payload, headers=merged, timeout=timeout or self.timeout)
            except requests.RequestException as exc:
                # niftyindices.com does NOT return 403 for a disallowed User-Agent -- it hangs
                # until the socket times out. So a timeout here usually means the headers are
                # wrong, not that the exchange is down. Do not "fix" this by shortening the
                # timeout; fix it by sending a browser UA.
                log.warning("POST %s failed (%s/%s): %s", url, attempt, self.retries, exc)
            else:
                if resp.ok:
                    return self._decode(resp, "json", url)
                log.warning("POST %s -> HTTP %s (%s/%s)", url, resp.status_code, attempt, self.retries)
                if resp.status_code in (401, 403, 419) and warm:
                    self.warm(warm, force=True)
            if attempt < self.retries:
                time.sleep(delay)
                delay *= 2
        return None

    def stream_lines(
        self,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[int] = None,
        keep: Optional[str] = None,
        max_bytes: int = 80 * 1024 * 1024,
    ) -> Optional[list[str]]:
        """Download a large text file line by line, keeping only what matters.

        AMFI's NAV history report is ~15MB for a twelve-month window and we need a handful of
        lines from it. `keep` is a prefix filter applied while streaming, so the whole payload
        never lands in memory at once. The first few lines are always retained regardless of the
        filter, because callers need the header row to detect an HTML error page masquerading as
        a 200.
        """
        merged = dict(headers or {})
        delay = 1.5
        for attempt in range(1, self.retries + 1):
            try:
                with self.session.get(
                    url,
                    headers=merged,
                    params=params,
                    timeout=timeout or self.timeout,
                    stream=True,
                ) as resp:
                    if resp.status_code != 200:
                        log.warning("STREAM %s -> HTTP %s (%s/%s)", url, resp.status_code, attempt, self.retries)
                    else:
                        kept: list[str] = []
                        seen = 0
                        for index, raw in enumerate(resp.iter_lines(decode_unicode=False)):
                            if raw is None:
                                continue
                            seen += len(raw) + 1
                            if seen > max_bytes:
                                log.warning("STREAM %s exceeded %s bytes; truncating", url, max_bytes)
                                break
                            line = raw.decode("utf-8", errors="replace").strip()
                            if index < 3 or keep is None or line.startswith(keep):
                                kept.append(line)
                        return kept
            except requests.RequestException as exc:
                log.warning("STREAM %s failed (%s/%s): %s", url, attempt, self.retries, exc)
            if attempt < self.retries:
                time.sleep(delay)
                delay *= 2
        return None

    @staticmethod
    def _decode(resp: requests.Response, expect: str, url: str) -> Optional[Any]:
        if expect == "bytes":
            return resp.content
        if expect == "json":
            try:
                return resp.json()
            except ValueError:
                # Some of these APIs answer 200 with an HTML block page. Treat that as failure
                # rather than letting a TypeError surface three layers up.
                log.warning("GET %s returned 200 but not JSON (first 120 chars: %r)", url, resp.text[:120])
                return None
        return resp.text
