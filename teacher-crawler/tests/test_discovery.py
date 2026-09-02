from crawler.config import SchoolConfig
from crawler.discovery import discover_profiles
from crawler.fetcher import FetchResult


class FakeFetcher:
    def fetch(self, url: str) -> FetchResult:
        html = """
        <div class="faculty-list">
          <a href="/faculty/alice.html#bio">Alice</a>
          <a href="https://faculty.example.edu/faculty/alice.html">Alice duplicate</a>
          <a href="/news/update.html">News</a>
          <a href="https://outside.example.org/faculty/bob.html">Outside</a>
        </div>
        """
        return FetchResult(url, html.encode(), "text/html", "utf-8")


def test_discovery_filters_domain_pattern_and_duplicates() -> None:
    config = SchoolConfig.model_validate(
        {
            "school": "Test",
            "college": "Engineering",
            "base_url": "https://faculty.example.edu",
            "directory_urls": ["/people"],
            "discovery": {
                "link_selectors": [".faculty-list a[href]"],
                "include_patterns": [r"/faculty/[^/]+\.html$"],
            },
        }
    )

    profiles = discover_profiles(config, FakeFetcher())  # type: ignore[arg-type]

    assert [(profile.name, profile.url) for profile in profiles] == [
        (
            "Alice",
            "https://faculty.example.edu/faculty/alice.html",
        )
    ]
