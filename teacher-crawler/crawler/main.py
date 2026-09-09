from __future__ import annotations

import argparse
import logging
import mimetypes
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .config import SchoolConfig, load_school_config
from .analyzer import analyze_teacher
from .discovery import DiscoveredProfile, discover_profiles
from .document import create_teacher_document
from .extractor import extract_teacher
from .fetcher import Fetcher
from .storage import Storage, teacher_content_hash
from .repository import TeacherRepository

LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[str, dict[str, object]], None]


def _emit(
    callback: ProgressCallback | None, event: str, **data: object
) -> None:
    if callback:
        callback(event, data)


def _photo_extension(url: str, content_type: str) -> str:
    extension = mimetypes.guess_extension(content_type) if content_type else None
    if extension:
        return ".jpg" if extension == ".jpe" else extension
    suffix = Path(urlparse(url).path).suffix
    return suffix if suffix else ".jpg"


def _export_outputs(storage: Storage) -> None:
    repository = TeacherRepository(storage.root / "teacher_data.db")
    for record in storage.load_records():
        repository.upsert(analyze_teacher(record, storage.html_path(record)))
    rows = repository.list(sort="name", order="asc")
    successful_urls = {row["homepage_url"] for row in rows}
    for profile_url, error in sorted(storage.state.failed.items()):
        if profile_url in successful_urls:
            continue
        rows.append(
            {
                "name": storage.state.failed_names.get(profile_url, ""),
                "homepage_url": profile_url,
                "has_recruit_info": False,
            }
        )
    repository.export_csv(storage.root / "teachers.csv", rows=rows)
    storage.write_failures()


def _process_profiles(
    config: SchoolConfig,
    profiles: list[DiscoveredProfile],
    storage: Storage,
    progress_callback: ProgressCallback | None = None,
    force_refresh: bool = False,
    fetcher: Fetcher | None = None,
    success_limit: int | None = None,
) -> tuple[dict[str, int], bool]:
    counts = {"processed": 0, "duplicates": 0, "failed": 0}
    reached_limit = False
    owns_fetcher = fetcher is None
    if fetcher is None:
        fetcher = Fetcher(config.request, config.allowed_domains, storage.cache_dir)
    try:
        for profile in profiles:
            _emit(
                progress_callback,
                "profile_started",
                name=profile.name,
                url=profile.url,
                counts=dict(counts),
            )
            try:
                page = fetcher.fetch(profile.url, use_cache=not force_refresh)
                record = extract_teacher(page.text, page.url, config)
                content_hash = teacher_content_hash(record)
                existing = storage.state.content_hashes.get(content_hash)
                if existing and existing != profile.url:
                    storage.state.duplicates[profile.url] = existing
                    storage.state.failed.pop(profile.url, None)
                    storage.state.failed_names.pop(profile.url, None)
                    storage.save_state()
                    counts["duplicates"] += 1
                    _emit(
                        progress_callback,
                        "duplicate",
                        name=profile.name,
                        url=profile.url,
                        counts=dict(counts),
                    )
                    continue

                photo_path = None
                if record.photo_url:
                    try:
                        photo = fetcher.fetch(record.photo_url)
                        if photo.content_type.startswith("image/"):
                            photo_path = storage.save_photo(
                                record,
                                photo.content,
                                _photo_extension(photo.url, photo.content_type),
                            )
                    except Exception as exc:  # A photo failure should not discard the profile.
                        LOGGER.warning("Could not download photo for %s: %s", profile.url, exc)

                record_path = storage.save_record(record)
                storage.save_html(record, page.text)
                create_teacher_document(record, storage.documents_dir, photo_path)
                storage.state.completed[profile.url] = record_path.relative_to(storage.root).as_posix()
                storage.state.content_hashes[content_hash] = profile.url
                storage.state.failed.pop(profile.url, None)
                storage.state.failed_names.pop(profile.url, None)
                storage.save_state()
                counts["processed"] += 1
                _emit(
                    progress_callback,
                    "profile_completed",
                    name=record.name,
                    url=record.profile_url,
                    counts=dict(counts),
                )
                if success_limit is not None and counts["processed"] >= success_limit:
                    reached_limit = True
                    break
            except Exception as exc:
                LOGGER.exception("Failed to process %s", profile.url)
                storage.state.failed[profile.url] = f"{type(exc).__name__}: {exc}"
                storage.state.failed_names[profile.url] = profile.name
                storage.save_state()
                counts["failed"] += 1
                _emit(
                    progress_callback,
                    "profile_failed",
                    name=profile.name,
                    url=profile.url,
                    error=f"{type(exc).__name__}: {exc}",
                    counts=dict(counts),
                )
    finally:
        if owns_fetcher:
            fetcher.close()
    _export_outputs(storage)
    return counts, reached_limit


def retry_failed_profiles(
    config: SchoolConfig,
    profiles: list[DiscoveredProfile],
    progress_callback: ProgressCallback | None = None,
) -> dict[str, int]:
    """Retry known failed profiles without rediscovering or expanding crawl scope."""
    if config.request.use_browser:
        raise NotImplementedError("dynamic browser fetching is not supported in the static-page MVP")
    storage = Storage(config.output_dir)
    failed_urls = set(storage.state.failed)
    requested_urls = {profile.url for profile in profiles}
    if not requested_urls or not requested_urls.issubset(failed_urls):
        raise ValueError("retry targets must be current failed profiles")
    counts, _ = _process_profiles(
        config,
        profiles,
        storage,
        progress_callback=progress_callback,
        force_refresh=True,
    )
    return counts


def run(
    config: SchoolConfig,
    resume: bool = True,
    limit: int | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, int]:
    if config.request.use_browser:
        raise NotImplementedError("dynamic browser fetching is not supported in the static-page MVP")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    storage = Storage(config.output_dir, reset_state=not resume)
    counts = {"discovered": 0, "processed": 0, "skipped": 0, "duplicates": 0, "failed": 0}
    existing_successes = len(storage.state.completed) if resume else 0
    counts["processed"] = existing_successes

    with Fetcher(config.request, config.allowed_domains, storage.cache_dir) as fetcher:
        profiles = discover_profiles(config, fetcher)
        counts["discovered"] = len(profiles)
        LOGGER.info("Discovered %s profile URLs", len(profiles))
        _emit(progress_callback, "discovered", count=len(profiles), counts=dict(counts))

        pending: list[DiscoveredProfile] = []
        for profile in profiles:
            if resume and profile.url in storage.state.completed:
                counts["skipped"] += 1
                continue
            if resume and profile.url in storage.state.duplicates:
                counts["skipped"] += 1
                continue
            pending.append(profile)

        if limit is not None and existing_successes >= limit:
            processed = {"processed": 0, "duplicates": 0, "failed": 0}
            reached_limit = True
        else:
            processed, reached_limit = _process_profiles(
                config,
                pending,
                storage,
                progress_callback=progress_callback,
                fetcher=fetcher,
                success_limit=(limit - existing_successes) if limit is not None else None,
            )
    counts["processed"] += processed["processed"]
    counts["duplicates"] = processed["duplicates"]
    counts["failed"] = processed["failed"]
    if reached_limit:
        _emit(progress_callback, "paused", counts=dict(counts))
    else:
        _emit(progress_callback, "completed", counts=dict(counts))
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crawl static faculty profile pages")
    parser.add_argument("--config", required=True, help="Path to a school YAML configuration")
    parser.add_argument("--no-resume", action="store_true", help="Ignore and replace prior crawl state")
    parser.add_argument(
        "--limit",
        type=_positive_int,
        help="Process at most this many unfinished teacher profiles",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_school_config(args.config)
    counts = run(config, resume=not args.no_resume, limit=args.limit)
    LOGGER.info(
        "Done: discovered=%s processed=%s skipped=%s duplicates=%s failed=%s",
        counts["discovered"],
        counts["processed"],
        counts["skipped"],
        counts["duplicates"],
        counts["failed"],
    )
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
