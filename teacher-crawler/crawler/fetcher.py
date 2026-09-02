from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from .config import RequestConfig

LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}


class ForbiddenDomainError(ValueError):
    """Raised when a request or redirect leaves the configured domain whitelist."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    content: bytes
    content_type: str
    encoding: str
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding, errors="replace")


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code in RETRYABLE_STATUS_CODES
    )


class Fetcher:
    def __init__(
        self,
        config: RequestConfig,
        allowed_domains: list[str],
        cache_dir: Path,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.allowed_domains = {domain.strip().lower() for domain in allowed_domains if domain.strip()}
        if not self.allowed_domains:
            raise ValueError("allowed_domains must not be empty")
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._sleeper = sleeper
        self._last_request_at = 0.0
        self._lock = threading.Lock()
        self._client = httpx.Client(
            headers={"User-Agent": config.user_agent},
            follow_redirects=True,
            timeout=config.timeout_seconds,
            event_hooks={"response": [self._validate_redirect]},
            transport=transport,
            verify=config.verify_ssl,
        )

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _ensure_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or hostname not in self.allowed_domains:
            raise ForbiddenDomainError(f"URL is outside allowed_domains: {url}")

    def _validate_redirect(self, response: httpx.Response) -> None:
        self._ensure_allowed(str(response.url))
        location = response.headers.get("location")
        if location:
            self._ensure_allowed(urljoin(str(response.url), location))

    def _wait_for_rate_limit(self) -> None:
        with self._lock:
            elapsed = self._clock() - self._last_request_at
            remaining = self.config.delay_seconds - elapsed
            if remaining > 0:
                self._sleeper(remaining)
            self._last_request_at = self._clock()

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.html", self.cache_dir / f"{key}.json"

    def _read_cache(self, url: str) -> FetchResult | None:
        html_path, metadata_path = self._cache_paths(url)
        if not html_path.exists():
            return None
        metadata: dict[str, str] = {}
        if metadata_path.exists():
            try:
                parsed = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    metadata = {str(key): str(value) for key, value in parsed.items()}
            except (OSError, ValueError):
                LOGGER.warning("Ignoring invalid cache metadata: %s", metadata_path)
        final_url = metadata.get("url", url)
        self._ensure_allowed(final_url)
        try:
            content = html_path.read_bytes()
        except OSError:
            return None
        return FetchResult(
            url=final_url,
            content=content,
            content_type=metadata.get("content_type", "text/html"),
            encoding=metadata.get("encoding", "utf-8"),
            from_cache=True,
        )

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def _write_cache(self, requested_url: str, result: FetchResult) -> None:
        html_path, metadata_path = self._cache_paths(requested_url)
        metadata = json.dumps(
            {
                "url": result.url,
                "content_type": result.content_type,
                "encoding": result.encoding,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        self._atomic_write(html_path, result.content)
        self._atomic_write(metadata_path, metadata)

    def fetch(self, url: str, use_cache: bool = True) -> FetchResult:
        self._ensure_allowed(url)
        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                LOGGER.debug("Cache hit: %s", url)
                return cached

        retrying = Retrying(
            stop=stop_after_attempt(self.config.retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception(_should_retry),
            reraise=True,
            before_sleep=lambda state: LOGGER.warning(
                "Request failed; retrying %s (attempt %s)", url, state.attempt_number
            ),
        )
        for attempt in retrying:
            with attempt:
                self._wait_for_rate_limit()
                response = self._client.get(url)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                result = FetchResult(
                    str(response.url),
                    response.content,
                    content_type,
                    response.encoding or "utf-8",
                )
                if content_type in HTML_CONTENT_TYPES:
                    self._write_cache(url, result)
                return result
        raise RuntimeError("unreachable")
