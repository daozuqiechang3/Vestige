from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from crawler.config import RequestConfig
from crawler.fetcher import Fetcher, ForbiddenDomainError


def test_fetcher_retries_retryable_status_and_reads_html_cache(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["User-Agent"] == "TeacherResearchCrawler/1.0 contact@example.com"
        if calls == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            content=b"<html><body>cached</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    url = "https://example.edu/teacher/alice"
    fetcher = Fetcher(
        RequestConfig(delay_seconds=0, retries=3),
        ["example.edu"],
        tmp_path,
        transport=httpx.MockTransport(handler),
    )
    first = fetcher.fetch(url)
    second = fetcher.fetch(url)
    refreshed = fetcher.fetch(url, use_cache=False)
    fetcher.close()

    cache_key = sha256(url.encode("utf-8")).hexdigest()
    assert first.from_cache is False
    assert second.from_cache is True
    assert refreshed.from_cache is False
    assert second.text == "<html><body>cached</body></html>"
    assert calls == 3
    assert (tmp_path / f"{cache_key}.html").read_bytes() == first.content
    assert (tmp_path / f"{cache_key}.json").exists()


def test_fetcher_rejects_forbidden_domains_before_request(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request)

    fetcher = Fetcher(
        RequestConfig(delay_seconds=0),
        ["example.edu"],
        tmp_path,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ForbiddenDomainError):
        fetcher.fetch("https://outside.example.org/teacher")
    fetcher.close()

    assert calls == 0


def test_fetcher_does_not_retry_non_retryable_http_status(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, request=request)

    fetcher = Fetcher(
        RequestConfig(delay_seconds=0, retries=3),
        ["example.edu"],
        tmp_path,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(httpx.HTTPStatusError):
        fetcher.fetch("https://example.edu/missing")
    fetcher.close()

    assert calls == 1


def test_fetcher_applies_delay_between_network_requests(
    tmp_path: Path,
) -> None:
    monotonic_values = iter([10.0, 10.0, 10.25, 11.0])
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    fetcher = Fetcher(
        RequestConfig(delay_seconds=1),
        ["example.edu"],
        tmp_path,
        transport=httpx.MockTransport(handler),
        clock=lambda: next(monotonic_values),
        sleeper=sleeps.append,
    )
    fetcher.fetch("https://example.edu/one")
    fetcher.fetch("https://example.edu/two")
    fetcher.close()

    assert sleeps == [0.75]
