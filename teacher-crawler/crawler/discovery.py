from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from .config import SchoolConfig
from .fetcher import Fetcher


@dataclass(frozen=True)
class DiscoveredProfile:
    name: str
    url: str


def _matches(url: str, includes: list[str], excludes: list[str]) -> bool:
    if includes and not any(re.search(pattern, url, re.IGNORECASE) for pattern in includes):
        return False
    return not any(re.search(pattern, url, re.IGNORECASE) for pattern in excludes)


def discover_profiles(config: SchoolConfig, fetcher: Fetcher) -> list[DiscoveredProfile]:
    discovered: dict[str, DiscoveredProfile] = {}
    allowed = set(config.allowed_domains)
    for directory_url in config.start_urls:
        result = fetcher.fetch(urljoin(config.base_url + "/", directory_url))
        soup = BeautifulSoup(result.text, "lxml")
        for selector in config.discovery.link_selectors:
            for link in soup.select(selector):
                href = link.get("href")
                if not isinstance(href, str) or not href.strip():
                    continue
                absolute, _ = urldefrag(urljoin(result.url, href.strip()))
                parsed = urlparse(absolute)
                if parsed.scheme not in {"http", "https"}:
                    continue
                if (parsed.hostname or "").lower() not in allowed:
                    continue
                if not _matches(absolute, config.discovery.include_patterns, config.discovery.exclude_patterns):
                    continue
                name = " ".join(link.get_text(" ", strip=True).split())
                if not name:
                    title = link.get("title")
                    name = " ".join(title.split()) if isinstance(title, str) else ""
                discovered.setdefault(absolute, DiscoveredProfile(name=name, url=absolute))
    return list(discovered.values())
