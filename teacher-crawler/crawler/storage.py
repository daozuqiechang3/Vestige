from __future__ import annotations

import csv
from hashlib import sha256
import json
import os
import re
import tempfile
from pathlib import Path

from .models import CrawlState, Teacher

INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename_part(value: str) -> str:
    return (INVALID_FILENAME.sub("_", value).strip(" ._") or "unknown")[:80]


def teacher_stem(teacher: Teacher) -> str:
    return "_".join(
        safe_filename_part(value) for value in (teacher.school, teacher.college, teacher.name)
    )


def teacher_content_hash(teacher: Teacher) -> str:
    payload = teacher.model_dump(mode="json", exclude={"collected_at", "profile_url"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


class Storage:
    def __init__(self, output_dir: Path, reset_state: bool = False) -> None:
        self.root = output_dir
        self.records_dir = self.root / "json"
        self.documents_dir = self.root / "documents"
        self.html_dir = self.root / "html"
        self.photos_dir = self.root / "photos"
        self.cache_dir = self.root / "cache"
        self.state_path = self.root / "state.json"
        for directory in (
            self.root,
            self.records_dir,
            self.documents_dir,
            self.html_dir,
            self.photos_dir,
            self.cache_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.state = CrawlState() if reset_state else self._load_state()

    def _load_state(self) -> CrawlState:
        if not self.state_path.exists():
            return CrawlState()
        try:
            return CrawlState.model_validate_json(self.state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"invalid crawl state: {self.state_path}") from exc

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def save_state(self) -> None:
        self._atomic_write(self.state_path, self.state.model_dump_json(indent=2))

    def save_record(self, record: Teacher) -> Path:
        path = self.records_dir / f"{teacher_stem(record)}.json"
        self._atomic_write(path, record.model_dump_json(indent=2))
        return path

    def save_html(self, record: Teacher, html: str) -> Path:
        path = self.html_dir / f"{teacher_stem(record)}.html"
        self._atomic_write(path, html)
        return path

    def html_path(self, record: Teacher) -> Path:
        return self.html_dir / f"{teacher_stem(record)}.html"

    def save_photo(self, record: Teacher, content: bytes, extension: str) -> Path:
        extension = extension.lower() if re.fullmatch(r"\.[a-z0-9]{1,5}", extension.lower()) else ".jpg"
        path = self.photos_dir / f"{teacher_stem(record)}{extension}"
        path.write_bytes(content)
        return path

    def load_records(self) -> list[Teacher]:
        records: dict[str, Teacher] = {}
        for path in self.records_dir.glob("*.json"):
            try:
                record = Teacher.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"invalid teacher JSON: {path}") from exc
            existing = records.get(record.profile_url)
            if existing is None or record.collected_at > existing.collected_at:
                records[record.profile_url] = record
        return sorted(records.values(), key=lambda record: (record.name.casefold(), record.profile_url))

    def write_csv(self, records: list[Teacher]) -> Path:
        path = self.root / "teachers.csv"
        handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".teachers.", text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=[
                        "school",
                        "college",
                        "name",
                        "category",
                        "title",
                        "email",
                        "phone",
                        "department",
                        "research_interests",
                        "admission_info",
                        "profile_url",
                        "collected_at",
                    ],
                )
                writer.writeheader()
                for record in records:
                    writer.writerow(
                        {
                            "school": record.school,
                            "college": record.college,
                            "name": record.name,
                            "category": record.category or "",
                            "title": record.title or "",
                            "email": record.email or "",
                            "phone": record.phone or "",
                            "department": record.department or "",
                            "research_interests": " | ".join(record.research_interests),
                            "admission_info": record.admission_info or "",
                            "profile_url": record.profile_url,
                            "collected_at": record.collected_at.isoformat(),
                        }
                    )
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return path

    def write_failures(self) -> Path:
        path = self.root / "failures.csv"
        handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".failures.", text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["profile_url", "error"])
                writer.writeheader()
                for profile_url, error in sorted(self.state.failed.items()):
                    writer.writerow({"profile_url": profile_url, "error": error})
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return path
