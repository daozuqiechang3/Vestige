import csv
from pathlib import Path

from crawler.config import SchoolConfig
from crawler.discovery import DiscoveredProfile
from crawler.fetcher import FetchResult
from crawler.main import build_parser, retry_failed_profiles, run
from crawler.storage import Storage


HTML = """
<main>
  <h1>Alice Zhang</h1>
  <p class="position">Professor</p>
  <a href="mailto:alice@example.edu">Email</a>
  <div class="research">AI；Robotics</div>
  <p>Faculty biography.</p>
</main>
"""


class FakeFetcher:
    fetch_count = 0

    def __init__(self, _config: object, *_args: object) -> None:
        pass

    def __enter__(self) -> "FakeFetcher":
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def close(self) -> None:
        pass

    def fetch(self, url: str, use_cache: bool = True) -> FetchResult:
        type(self).fetch_count += 1
        return FetchResult(url, HTML.encode(), "text/html", "utf-8")


def test_pipeline_deduplicates_and_resumes(tmp_path: Path, monkeypatch: object) -> None:
    import crawler.main as main_module

    profiles = [
        DiscoveredProfile("Alice", "https://example.edu/alice"),
        DiscoveredProfile("Alice Copy", "https://example.edu/alice-copy"),
    ]
    monkeypatch.setattr(main_module, "Fetcher", FakeFetcher)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        main_module, "discover_profiles", lambda _config, _fetcher: profiles
    )
    config = SchoolConfig.model_validate(
        {
            "school": "Test University",
            "college": "School of Engineering",
            "base_url": "https://example.edu",
            "directory_urls": ["/people"],
            "output_dir": tmp_path / "output",
        }
    )

    first = run(config)
    first_fetch_count = FakeFetcher.fetch_count
    second = run(config)

    assert first["processed"] == 1
    assert first["duplicates"] == 1
    assert len(list((config.output_dir / "json").glob("*.json"))) == 1
    assert len(list((config.output_dir / "html").glob("*.html"))) == 1
    assert len(list((config.output_dir / "documents").glob("*.docx"))) == 1
    assert (config.output_dir / "teachers.csv").exists()
    assert (config.output_dir / "teacher_data.db").exists()
    assert (config.output_dir / "failures.csv").exists()
    assert second["skipped"] == 2
    assert FakeFetcher.fetch_count == first_fetch_count

    FakeFetcher.fetch_count = 0
    limited_config = config.model_copy(update={"output_dir": tmp_path / "limited-output"})
    limited = run(limited_config, limit=1)
    assert limited["discovered"] == 2
    assert limited["processed"] == 1
    assert limited["duplicates"] == 0
    assert FakeFetcher.fetch_count == 1


def test_cli_accepts_positive_limit() -> None:
    args = build_parser().parse_args(["--config", "configs/bit-cs.yaml", "--limit", "2"])
    assert args.limit == 2


def test_limit_counts_only_successful_profiles(tmp_path: Path, monkeypatch: object) -> None:
    import crawler.main as main_module

    profiles = [
        DiscoveredProfile("Broken", "https://example.edu/broken"),
        DiscoveredProfile("Alice", "https://example.edu/alice"),
        DiscoveredProfile("Bob", "https://example.edu/bob"),
    ]

    class FirstFailsFetcher(FakeFetcher):
        fetched_urls: list[str] = []

        def fetch(self, url: str, use_cache: bool = True) -> FetchResult:
            type(self).fetched_urls.append(url)
            if url.endswith("/broken"):
                raise RuntimeError("broken profile")
            return super().fetch(url, use_cache=use_cache)

    monkeypatch.setattr(main_module, "Fetcher", FirstFailsFetcher)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        main_module, "discover_profiles", lambda _config, _fetcher: profiles
    )
    config = SchoolConfig.model_validate(
        {
            "school": "Test University",
            "college": "School of Engineering",
            "base_url": "https://example.edu",
            "directory_urls": ["/people"],
            "output_dir": tmp_path / "success-limit-output",
        }
    )
    events: list[str] = []

    counts = run(config, limit=1, progress_callback=lambda event, _data: events.append(event))

    assert counts["processed"] == 1
    assert counts["failed"] == 1
    assert FirstFailsFetcher.fetched_urls == [
        "https://example.edu/broken",
        "https://example.edu/alice",
    ]
    assert events[-1] == "paused"


def test_failed_profile_is_exported_and_can_be_retried(
    tmp_path: Path, monkeypatch: object
) -> None:
    import crawler.main as main_module

    profile = DiscoveredProfile("Alice", "https://example.edu/alice")

    class FailOnceFetcher(FakeFetcher):
        should_fail = True

        def fetch(self, url: str, use_cache: bool = True) -> FetchResult:
            if type(self).should_fail:
                raise RuntimeError("temporary failure")
            return super().fetch(url, use_cache=use_cache)

    monkeypatch.setattr(main_module, "Fetcher", FailOnceFetcher)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        main_module, "discover_profiles", lambda _config, _fetcher: [profile]
    )
    config = SchoolConfig.model_validate(
        {
            "school": "Test University",
            "college": "School of Engineering",
            "base_url": "https://example.edu",
            "directory_urls": ["/people"],
            "output_dir": tmp_path / "retry-output",
        }
    )

    first = run(config)
    storage = Storage(config.output_dir)
    with (config.output_dir / "teachers.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        failed_rows = list(csv.DictReader(handle))

    assert first["failed"] == 1
    assert storage.state.failed_names[profile.url] == "Alice"
    assert failed_rows == [
        {
            "name": "Alice",
            "title": "",
            "research_dir": "",
            "email": "",
            "lab": "",
            "bio": "",
            "homepage_url": profile.url,
            "recruit_text": "",
            "has_recruit_info": "False",
            "note": "",
        }
    ]

    FailOnceFetcher.should_fail = False
    retried = retry_failed_profiles(config, [profile])
    storage = Storage(config.output_dir)

    assert retried == {"processed": 1, "duplicates": 0, "failed": 0}
    assert profile.url not in storage.state.failed
    assert profile.url not in storage.state.failed_names
    with (config.output_dir / "teachers.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        successful_rows = list(csv.DictReader(handle))
    assert successful_rows[0]["name"] == "Alice Zhang"
    assert successful_rows[0]["title"] == "Professor"
