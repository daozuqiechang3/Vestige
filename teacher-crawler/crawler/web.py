from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlparse
from zipfile import ZIP_DEFLATED, ZipFile

import uvicorn
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .acl import (
    Paper,
    canonical_paper_url,
    discover_papers,
    extract_paper,
    is_paper_url,
    make_fetcher,
    paper_id_from_url,
    validate_volume_url,
    write_csv,
)
from .config import SchoolConfig, load_school_config
from .discovery import DiscoveredProfile
from .openreview import (
    OpenReviewChallengeError,
    OpenReviewError,
    collection_from_url,
    discover_from_html,
    is_openreview_forum_url,
    make_openreview_client,
    parse_forum_html,
)
from .repository import TeacherRepository
from .storage import Storage, safe_filename_part, teacher_stem

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "configs"
TASKS_DIR = Path(os.environ.get("CRAWLER_TASKS_DIR", ROOT / "output" / "web-tasks"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
PAPER_MODES = {"acl", "openreview"}


class CrawlRequest(BaseModel):
    url: str
    mode: str = "teacher"
    limit: int | None = Field(default=None, ge=1, le=5000, strict=True)


class RetryRequest(BaseModel):
    profile_url: str


class ContinueRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=5000, strict=True)


class TeacherUpdate(BaseModel):
    note: str = Field(default="", max_length=2000)
    contact_status: str = "未联系"
    favorite: bool = False


class PaperUpdate(BaseModel):
    abstract_zh: str | None = None
    favorite: bool | None = None


class BrowserDiscoveryRequest(BaseModel):
    conference: str = Field(min_length=1, max_length=200)
    session: str = Field(min_length=1, max_length=200)
    paper_urls: list[str] = Field(default_factory=list, max_length=5000)
    # A browser can submit the rendered group page directly.  ``html_pages``
    # supports the 22 OpenReview pagination pages without requiring a second
    # parser in the UI.
    html: str = Field(default="", max_length=3_000_000)
    page_url: str = Field(default="", max_length=2000)
    html_pages: list[str] = Field(default_factory=list, max_length=30)
    limit: int | None = Field(default=None, ge=1, le=5000, strict=True)


class BrowserPaperSnapshot(BaseModel):
    url: str
    title: str = Field(default="", max_length=10000)
    html: str = Field(default="", max_length=3_000_000)
    authors: list[str] = Field(default_factory=list, max_length=500)
    abstract_en: str = ""
    pdf_url: str = ""
    original_pdf_url: str = ""
    author_profiles: list[dict[str, str]] = Field(default_factory=list, max_length=500)
    venue: str = ""
    decision: str = ""
    decision_comment: str = ""
    tldr: str = ""
    lay_summary: str = ""
    primary_area: str = ""
    keywords: list[str] = Field(default_factory=list, max_length=500)
    submission_number: str = ""
    published_at: str = ""
    modified_at: str = ""


class BrowserPapersRequest(BaseModel):
    papers: list[BrowserPaperSnapshot] = Field(min_length=1, max_length=50)
    complete: bool = False


@dataclass
class TaskRecord:
    id: str
    url: str
    mode: str
    school: str = ""
    college: str = ""
    limit: int | None = None
    output_dir: Path = field(default_factory=lambda: TASKS_DIR)
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str | None = None
    finished_at: str | None = None
    current_name: str | None = None
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "discovered": 0,
            "processed": 0,
            "failed": 0,
            "skipped": 0,
            "pending": 0,
            "running": 0,
            "warnings": 0,
            "duplicates": 0,
        }
    )
    logs: list[str] = field(default_factory=list)
    teachers: list[dict[str, Any]] = field(default_factory=list)
    papers: list[dict[str, Any]] = field(default_factory=list)
    paper_urls: list[str] = field(default_factory=list)
    paper_states: dict[str, str] = field(default_factory=dict)
    paper_errors: dict[str, str] = field(default_factory=dict)
    error: str | None = None


class TaskManager:
    def __init__(self) -> None:
        self.tasks: dict[str, TaskRecord] = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="crawler-web")
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        for path in TASKS_DIR.glob("*/task.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                embedded_papers = raw.get("papers", [])
                raw["output_dir"] = path.parent
                task = TaskRecord(**raw)
                if task.status in {"running", "retrying"}:
                    task.status = "queued"
                if task.mode in PAPER_MODES:
                    task.papers = self._load_papers(task, embedded_papers)
                    self._normalize_acl_task(task)
                    for paper in task.papers:
                        self._write_paper(task, paper)
                    self.save(task)
                self.tasks[task.id] = task
            except (OSError, ValueError, TypeError):
                continue
        for task in list(self.tasks.values()):
            if task.status == "queued":
                self.executor.submit(self._run, task.id)

    def save(self, task: TaskRecord) -> None:
        task.output_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(task)
        payload["output_dir"] = "."
        if task.mode in PAPER_MODES:
            payload["papers"] = []
        temp = task.output_dir / ".task.json.tmp"
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, task.output_dir / "task.json")

    @staticmethod
    def _append_log(task: TaskRecord, message: str) -> None:
        task.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
        del task.logs[:-300]

    def log(self, task: TaskRecord, message: str) -> None:
        self._append_log(task, message)
        self.save(task)

    @staticmethod
    def _atomic_write_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(value)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @staticmethod
    def _paper_record_path(task: TaskRecord, url: str) -> Path:
        return task.output_dir / "papers" / f"{paper_id_from_url(url)}.json"

    @staticmethod
    def _paper_raw_path(task: TaskRecord, url: str) -> Path:
        return task.output_dir / "html" / f"{paper_id_from_url(url)}.html"

    def _resolve_paper_raw_path(self, task: TaskRecord, url: str) -> Path:
        current = self._paper_raw_path(task, url)
        if current.is_file():
            return current
        paper = next(
            (item for item in task.papers if item.get("url") == url),
            None,
        )
        if paper:
            legacy = (
                task.output_dir
                / "html"
                / f"{safe_filename_part(str(paper.get('title', 'paper')))}.html"
            )
            if legacy.is_file():
                return legacy
        return current

    def _write_paper(self, task: TaskRecord, paper: dict[str, Any]) -> None:
        url = canonical_paper_url(str(paper.get("url", "")))
        paper["url"] = url
        self._atomic_write_text(
            self._paper_record_path(task, url),
            json.dumps(paper, ensure_ascii=False, indent=2),
        )

    def _load_papers(
        self,
        task: TaskRecord,
        embedded: object,
    ) -> list[dict[str, Any]]:
        by_url: dict[str, dict[str, Any]] = {}
        if isinstance(embedded, list):
            candidates = embedded
        else:
            candidates = []
        paper_dir = task.output_dir / "papers"
        if paper_dir.is_dir():
            for path in paper_dir.glob("*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        candidates.append(value)
                except (OSError, ValueError):
                    continue
        for paper in candidates:
            if not isinstance(paper, dict):
                continue
            try:
                url = canonical_paper_url(str(paper.get("url", "")))
            except ValueError:
                continue
            paper["url"] = url
            by_url[url] = paper
        ordered = [by_url.pop(url) for url in task.paper_urls if url in by_url]
        ordered.extend(by_url.values())
        return ordered

    @staticmethod
    def _refresh_acl_counts(task: TaskRecord) -> None:
        states = [task.paper_states.get(url, "pending") for url in task.paper_urls]
        task.counts = {
            "discovered": len(task.paper_urls),
            "processed": states.count("success"),
            "failed": states.count("fail"),
            "skipped": states.count("skipped"),
            "pending": states.count("pending"),
            "running": states.count("running"),
            "warnings": sum(
                1
                for paper in task.papers
                if paper.get("translation_failed")
                or paper.get("title_translation_failed")
            ),
            "duplicates": 0,
        }

    def _normalize_acl_task(self, task: TaskRecord) -> None:
        task.logs = [
            entry
            for entry in task.logs[-300:]
            if not entry.split("] ", 1)[-1].startswith("英文摘要：")
        ]
        urls: list[str] = []
        for value in [*task.paper_urls, *(paper.get("url", "") for paper in task.papers)]:
            try:
                url = canonical_paper_url(str(value))
            except ValueError:
                continue
            if url not in urls:
                urls.append(url)
        states: dict[str, str] = {}
        errors: dict[str, str] = {}
        for value, state in task.paper_states.items():
            try:
                url = canonical_paper_url(value)
            except ValueError:
                continue
            normalized_state = "pending" if state == "running" else state
            states[url] = (
                normalized_state
                if normalized_state in {"pending", "success", "fail", "skipped"}
                else "pending"
            )
            if url in task.paper_errors:
                errors[url] = task.paper_errors[url]
            elif value in task.paper_errors:
                errors[url] = task.paper_errors[value]
        paper_urls = {str(paper.get("url")) for paper in task.papers}
        for url in urls:
            states.setdefault(url, "pending")
            if states[url] == "success" and url not in paper_urls:
                states[url] = "pending"
        for url in paper_urls:
            states[url] = "success"
        task.paper_urls = urls
        task.paper_states = states
        task.paper_errors = {
            url: error for url, error in errors.items() if states.get(url) == "fail"
        }
        self._refresh_acl_counts(task)

    def get(self, task_id: str) -> TaskRecord:
        try:
            return self.tasks[task_id]
        except KeyError as exc:
            raise KeyError(task_id) from exc

    def _match_config(self, url: str) -> tuple[str, SchoolConfig]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("请输入有效的公开 HTTP(S) 教师列表页 URL")
        for path in sorted(CONFIG_DIR.glob("*.yaml")):
            config = load_school_config(path)
            if parsed.hostname.lower() in config.allowed_domains:
                normalized, _ = urldefrag(url)
                config.start_urls = [normalized]
                config.directory_urls = [normalized]
                return path.stem, config
        raise ValueError(f"没有找到允许访问域名 {parsed.hostname} 的学校配置")

    def teacher_config(self, url: str) -> SchoolConfig:
        return self._match_config(url)[1]

    def create(self, request: CrawlRequest) -> TaskRecord:
        url = request.url.strip()
        mode = request.mode
        if mode == "acl" and urlparse(url).hostname in {
            "openreview.net",
            "www.openreview.net",
        }:
            mode = "openreview"
        if mode == "acl":
            url = validate_volume_url(url)
            source = {
                "dl.acm.org": "ACM Digital Library",
                "proceedings.iclr.cc": "ICLR Proceedings",
            }.get(urlparse(url).hostname or "", "ACL Anthology")
            school = college = source
        elif mode == "openreview":
            if is_openreview_forum_url(url):
                url = canonical_paper_url(url)
                school, college = "OpenReview", "OpenReview 论文"
            else:
                collection = collection_from_url(url)
                url = collection.url
                group_parts = collection.group_id.split("/")
                conference = (
                    f"{group_parts[0].split('.')[0]} {group_parts[1]}"
                    if len(group_parts) >= 2
                    else "OpenReview"
                )
                school, college = conference, collection.venue
        elif mode == "teacher":
            config = self.teacher_config(url)
            url, school, college = config.start_urls[0], config.school, config.college
        else:
            raise ValueError("未知采集模式")
        if (
            mode in PAPER_MODES
            and urlparse(url).hostname in {"dl.acm.org", "openreview.net"}
        ):
            with self.lock:
                reusable = next(
                    (
                        item
                        for item in self.tasks.values()
                        if item.mode == mode
                        and item.url == url
                        and (
                            item.counts.get("processed", 0) > 0
                            or item.status == "awaiting_browser"
                        )
                        and item.status
                        in {
                            "completed",
                            "completed_with_errors",
                            "paused",
                            "awaiting_browser",
                        }
                    ),
                    None,
                )
                if reusable is not None:
                    self._append_log(
                        reusable,
                        (
                            "检测到相同 ACM SESSION，复用已有采集结果；"
                            if mode == "acl"
                            else "检测到相同 OpenReview 分组，复用已有采集结果；"
                        )
                        + "如需补采请在原任务中继续",
                    )
                    self.save(reusable)
                    return reusable
        task = TaskRecord(uuid.uuid4().hex, url, mode, school, college, request.limit)
        task.output_dir = TASKS_DIR / task.id
        with self.lock:
            self.tasks[task.id] = task
            self.log(task, "任务已创建，等待执行")
        self.executor.submit(self._run, task.id)
        return task

    def import_acm_discovery(
        self,
        task_id: str,
        request: BrowserDiscoveryRequest,
    ) -> TaskRecord:
        """Apply complete paper discovery captured in a verified browser."""
        with self.lock:
            task = self.get(task_id)
            if task.mode not in PAPER_MODES:
                raise ValueError("当前任务不是论文任务")
            if task.mode == "acl" and urlparse(task.url).hostname != "dl.acm.org":
                raise ValueError("当前任务不是 ACM 或 OpenReview 浏览器导入任务")
            expected_domain = (
                "openreview.net" if task.mode == "openreview" else "dl.acm.org"
            )
            if task.status in {"queued", "running", "retrying"}:
                raise ValueError("任务正在执行，不能同时导入浏览器发现结果")

            raw_values = list(request.paper_urls)
            if request.html or request.html_pages:
                if task.mode != "openreview":
                    raise ValueError("HTML 发现导入目前只支持 OpenReview")
                page_url = request.page_url.strip() or task.url
                html_values = ([request.html] if request.html else []) + list(
                    request.html_pages
                )
                for html in html_values:
                    refs = discover_from_html(html, page_url)
                    raw_values.extend(ref.url for ref in refs)

            urls: list[str] = []
            for value in raw_values:
                url = canonical_paper_url(value)
                if urlparse(url).hostname != expected_domain:
                    raise ValueError("浏览器发现结果包含其他来源的论文 URL")
                if url not in urls:
                    urls.append(url)
            if not urls:
                raise ValueError("浏览器发现结果没有有效论文；请提供论文 URL 或页面 HTML")

            existing_successes = {
                str(paper.get("url"))
                for paper in task.papers
                if paper.get("url")
            }
            # OpenReview's rendered group is paginated (ICML spotlight has
            # more than twenty pages).  A browser may therefore send one
            # page at a time.  Keep already discovered URLs and append new
            # ones instead of silently shrinking the task to the latest page.
            # ACM's SESSION importer historically receives one complete
            # section and intentionally keeps its replacement semantics.
            if task.mode == "openreview":
                merged_urls = list(task.paper_urls)
                for url in urls:
                    if url not in merged_urls:
                        merged_urls.append(url)
                urls = merged_urls

            missing = existing_successes.difference(urls)
            if missing:
                raise ValueError("浏览器发现结果缺少任务中已有的成功论文")

            old_states = dict(task.paper_states)
            old_errors = dict(task.paper_errors)
            task.paper_urls = urls
            task.paper_states = {}
            task.paper_errors = {}
            for url in urls:
                if url in existing_successes:
                    task.paper_states[url] = "success"
                else:
                    state = old_states.get(url, "pending")
                    task.paper_states[url] = (
                        state if state in {"pending", "fail", "skipped"} else "pending"
                    )
                    if task.paper_states[url] == "fail" and url in old_errors:
                        task.paper_errors[url] = old_errors[url]

            task.school = request.conference.strip()
            task.college = request.session.strip()
            if "limit" in request.model_fields_set:
                task.limit = request.limit
            task.error = None
            self._refresh_acl_counts(task)
            self._append_log(
                task,
                f"浏览器已确认 {task.school} / {task.college}："
                f"发现 {len(urls)} 篇论文",
            )
            self._settle_acl_task(task)
            return task

    def import_acm_papers(
        self,
        task_id: str,
        request: BrowserPapersRequest,
    ) -> TaskRecord:
        """Merge paper metadata read from detail pages in a verified browser."""
        with self.lock:
            task = self.get(task_id)
            if task.mode not in PAPER_MODES:
                raise ValueError("当前任务不是论文任务")
            if task.mode == "acl" and urlparse(task.url).hostname != "dl.acm.org":
                raise ValueError("当前任务不是 ACM 或 OpenReview 浏览器导入任务")
            expected_domain = (
                "openreview.net" if task.mode == "openreview" else "dl.acm.org"
            )
            if task.status in {"queued", "running", "retrying"}:
                raise ValueError("任务正在执行，不能同时导入浏览器论文快照")
            known_urls = set(task.paper_urls)
            by_url = {str(paper.get("url")): paper for paper in task.papers}

            normalized: list[tuple[str, BrowserPaperSnapshot]] = []
            for snapshot in request.papers:
                url = canonical_paper_url(snapshot.url)
                if url not in known_urls:
                    raise ValueError(f"论文不属于当前 SESSION：{url}")
                if snapshot.html:
                    collection = None
                    if task.mode == "openreview" and not is_openreview_forum_url(task.url):
                        collection = collection_from_url(task.url)
                    parsed = parse_forum_html(
                        snapshot.html,
                        url,
                        collection=collection,
                    )
                    snapshot = BrowserPaperSnapshot(
                        url=url,
                        title=parsed.title,
                        authors=parsed.authors,
                        abstract_en=parsed.abstract_en,
                        pdf_url=parsed.pdf_url,
                        original_pdf_url=parsed.original_pdf_url,
                        author_profiles=parsed.author_profiles,
                        venue=parsed.venue,
                        decision=parsed.decision,
                        decision_comment=parsed.decision_comment,
                        tldr=parsed.tldr,
                        lay_summary=parsed.lay_summary,
                        primary_area=parsed.primary_area,
                        keywords=parsed.keywords,
                        submission_number=parsed.submission_number,
                        published_at=parsed.published_at,
                        modified_at=parsed.modified_at,
                    )
                if not snapshot.title.strip():
                    raise ValueError(f"浏览器论文快照缺少标题：{url}")
                normalized.append((url, snapshot))
            prospective = set(by_url).union(url for url, _ in normalized)
            if task.limit is not None and len(prospective) > task.limit:
                raise ValueError("浏览器论文快照会超过成功论文上限")
            if request.complete and prospective != known_urls:
                raise ValueError("仍有待采集论文，不能标记浏览器采集完成")

            # Build and validate the complete batch before changing task state.
            # This prevents a malformed later snapshot from leaving an earlier
            # snapshot marked successful after the request has failed.
            prepared_by_url = dict(by_url)
            prepared_states = dict(task.paper_states)
            prepared_errors = dict(task.paper_errors)
            raw_html: dict[str, str] = {}
            for url, snapshot in normalized:
                existing = by_url.get(url, {})
                def existing_text(field: str, value: str) -> str:
                    return value.strip() or str(existing.get(field) or "")

                abstract = existing_text("abstract_en", snapshot.abstract_en) or "无摘要"
                pdf_url = snapshot.pdf_url.strip()
                if pdf_url and urlparse(pdf_url).hostname not in {
                    expected_domain,
                    f"www.{expected_domain}",
                }:
                    raise ValueError(f"论文 PDF 与任务来源不一致：{url}")
                original_pdf_url = snapshot.original_pdf_url.strip()
                if original_pdf_url and urlparse(original_pdf_url).hostname not in {
                    expected_domain,
                    f"www.{expected_domain}",
                }:
                    raise ValueError(f"论文原始 PDF 与任务来源不一致：{url}")
                untranslated = not existing
                is_openreview = task.mode == "openreview"
                authors = [name.strip() for name in snapshot.authors if name.strip()]
                if not authors and isinstance(existing.get("authors"), list):
                    authors = [str(name) for name in existing["authors"] if str(name).strip()]
                author_profiles = [
                    {str(key): str(value) for key, value in item.items()}
                    for item in snapshot.author_profiles
                ]
                if not author_profiles and isinstance(existing.get("author_profiles"), list):
                    author_profiles = [
                        {str(key): str(value) for key, value in item.items()}
                        for item in existing["author_profiles"]
                        if isinstance(item, dict)
                    ]
                keywords = [value.strip() for value in snapshot.keywords if value.strip()]
                if not keywords and isinstance(existing.get("keywords"), list):
                    keywords = [str(value) for value in existing["keywords"] if str(value).strip()]
                paper = Paper(
                    title=snapshot.title.strip(),
                    title_zh=str(existing.get("title_zh") or snapshot.title.strip()),
                    pdf_url=pdf_url or str(existing.get("pdf_url") or ""),
                    abstract_en=abstract,
                    abstract_zh=str(existing.get("abstract_zh") or abstract),
                    url=url,
                    parser_mode=(
                        "OpenReview 浏览器快照"
                        if is_openreview
                        else "ACM 专用解析（浏览器快照）"
                    ),
                    favorite=bool(existing.get("favorite", False)),
                    translation_failed=(
                        untranslated and abstract != "无摘要"
                        if untranslated
                        else bool(existing.get("translation_failed", False))
                    ),
                    translation_error=(
                        "浏览器快照仅采集英文原文，尚未执行中文翻译"
                        if untranslated and abstract != "无摘要"
                        else str(existing.get("translation_error", ""))
                    ),
                    title_translation_failed=(
                        True
                        if untranslated
                        else bool(existing.get("title_translation_failed", False))
                    ),
                    title_translation_error=(
                        "浏览器快照仅采集英文标题，尚未执行中文翻译"
                        if untranslated
                        else str(existing.get("title_translation_error", ""))
                    ),
                    authors=authors,
                    author_profiles=author_profiles,
                    original_pdf_url=original_pdf_url
                    or str(existing.get("original_pdf_url") or ""),
                    source="OpenReview" if is_openreview else "ACM Digital Library",
                    venue=existing_text("venue", snapshot.venue),
                    decision=existing_text("decision", snapshot.decision),
                    decision_comment=existing_text("decision_comment", snapshot.decision_comment),
                    tldr=existing_text("tldr", snapshot.tldr),
                    lay_summary=existing_text("lay_summary", snapshot.lay_summary),
                    primary_area=existing_text("primary_area", snapshot.primary_area),
                    keywords=keywords,
                    submission_number=existing_text("submission_number", snapshot.submission_number),
                    published_at=existing_text("published_at", snapshot.published_at),
                    modified_at=existing_text("modified_at", snapshot.modified_at),
                ).to_dict()
                if existing.get("collected_at"):
                    paper["collected_at"] = existing["collected_at"]
                prepared_by_url[url] = paper
                prepared_states[url] = "success"
                prepared_errors.pop(url, None)
                original_snapshot = next(
                    item for item in request.papers if canonical_paper_url(item.url) == url
                )
                if original_snapshot.html:
                    raw_html[url] = original_snapshot.html

            # Persist records only after every item has passed validation, then
            # publish the in-memory state in one step.
            for url, paper in prepared_by_url.items():
                if url in {item_url for item_url, _ in normalized}:
                    self._write_paper(task, paper)
                    if url in raw_html:
                        self._atomic_write_text(self._paper_raw_path(task, url), raw_html[url])
            task.paper_states = prepared_states
            task.paper_errors = prepared_errors
            by_url = prepared_by_url

            task.papers = [by_url[url] for url in task.paper_urls if url in by_url]
            task.error = None
            self._refresh_acl_counts(task)
            self._append_log(
                task,
                f"浏览器快照已采集 {task.counts['processed']}/{task.counts['discovered']} 篇",
            )
            if request.complete:
                self._settle_acl_task(task)
            else:
                task.status = "paused"
                task.current_name = None
                self.save(task)
            return task

    def _teacher_event(self, task_id: str, event: str, data: dict[str, object]) -> None:
        with self.lock:
            task = self.tasks[task_id]
            if isinstance(data.get("counts"), dict):
                task.counts.update({key: int(value) for key, value in data["counts"].items()})
            if event == "paused":
                task.status = "paused"
                task.current_name = None
                task.finished_at = datetime.now(timezone.utc).isoformat()
                self.log(task, f"已达到采集上限{task.limit}，任务暂停")
            elif event == "profile_started":
                task.current_name = str(data.get("name") or data.get("url") or "")
            if event in {"profile_completed", "duplicate", "profile_failed"}:
                self.log(task, f"{event}: {data.get('name') or data.get('url')}")

    def _run(self, task_id: str, force: bool = False) -> None:
        with self.lock:
            task = self.tasks[task_id]
            task.status = "running"
            task.started_at = datetime.now(timezone.utc).isoformat()
            task.error = None
            self.log(task, "开始采集")
        try:
            if task.mode == "acl":
                self._run_acl(task_id, force)
                return
            if task.mode == "openreview":
                self._run_openreview(task_id, force)
                return
            from .main import run

            config = self.teacher_config(task.url)
            config.output_dir = task.output_dir
            counts = run(config, resume=not force, limit=task.limit, progress_callback=lambda e, d: self._teacher_event(task_id, e, d))
            with self.lock:
                task = self.tasks[task_id]
                task.counts = counts
                task.teachers = self._load_teachers(task.output_dir)
                if task.status == "paused":
                    task.current_name = None
                    task.finished_at = datetime.now(timezone.utc).isoformat()
                    self.save(task)
                else:
                    self._finish(task)
        except Exception as exc:
            with self.lock:
                task = self.tasks[task_id]
                task.status = "failed"
                response = getattr(exc, "response", None)
                if (
                    urlparse(task.url).hostname == "dl.acm.org"
                    and getattr(response, "status_code", None) == 403
                ):
                    task.error = (
                        "ACM Digital Library 拒绝后台直接访问（HTTP 403）；"
                        "该站点需要浏览器验证，不能按普通静态页面采集"
                    )
                else:
                    task.error = f"{type(exc).__name__}: {exc}"
                task.finished_at = datetime.now(timezone.utc).isoformat()
                self.log(task, f"任务终止：{task.error}")

    def _run_acl(self, task_id: str, force: bool) -> None:
        with self.lock:
            task = self.tasks[task_id]
            if force:
                self._clear_acl_outputs(task)
                task.papers.clear()
                task.paper_urls.clear()
                task.paper_states.clear()
                task.paper_errors.clear()
                self._refresh_acl_counts(task)
                self.save(task)
        with make_fetcher(task.output_dir) as fetcher:
            if not task.paper_urls:
                volume = fetcher.fetch(task.url, use_cache=not force)
                urls = (
                    [canonical_paper_url(volume.url)]
                    if is_paper_url(volume.url)
                    else discover_papers(volume.text, volume.url)
                )
                if not urls:
                    raise ValueError("合集页面没有发现可采集的 ACL、ACM 或 ICLR 论文")
                with self.lock:
                    task = self.tasks[task_id]
                    task.paper_urls = urls
                    task.paper_states = {url: "pending" for url in urls}
                    self._refresh_acl_counts(task)
                    self._append_log(task, f"发现 {len(urls)} 篇论文")
                    self.save(task)
            for url in list(task.paper_urls):
                with self.lock:
                    task = self.tasks[task_id]
                    self._refresh_acl_counts(task)
                    if self._acl_limit_reached(task):
                        self._settle_acl_task(task)
                        return
                    if task.paper_states.get(url, "pending") != "pending":
                        continue
                    task.paper_states[url] = "running"
                    task.current_name = url
                    self._refresh_acl_counts(task)
                    self.save(task)
                try:
                    paper = self._collect_acl_paper(
                        task_id,
                        url,
                        fetcher,
                        use_cache=not force,
                    )
                    with self.lock:
                        task = self.tasks[task_id]
                        task.papers = [p for p in task.papers if p.get("url") != url] + [paper.to_dict()]
                        task.paper_states[url] = "success"
                        task.paper_errors.pop(url, None)
                        self._write_paper(task, task.papers[-1])
                        self._refresh_acl_counts(task)
                        self._append_log(task, f"采集成功：{paper_id_from_url(url)}")
                        self.save(task)
                        if self._acl_limit_reached(task):
                            self._settle_acl_task(task)
                            return
                except Exception as exc:
                    with self.lock:
                        task = self.tasks[task_id]
                        task.paper_states[url] = "fail"
                        task.paper_errors[url] = f"{type(exc).__name__}: {exc}"
                        self._refresh_acl_counts(task)
                        self._append_log(
                            task,
                            f"采集失败：{paper_id_from_url(url)} - {task.paper_errors[url]}",
                        )
                        self.save(task)
        with self.lock:
            self._settle_acl_task(self.tasks[task_id])

    def _run_openreview(self, task_id: str, force: bool) -> None:
        with self.lock:
            task = self.tasks[task_id]
            if force:
                self._clear_acl_outputs(task)
                task.papers.clear()
                task.paper_urls.clear()
                task.paper_states.clear()
                task.paper_errors.clear()
                self._refresh_acl_counts(task)
                self.save(task)
            collection = (
                None
                if is_openreview_forum_url(task.url)
                else collection_from_url(task.url)
            )

        try:
            with make_openreview_client(task.output_dir) as client:
                if is_openreview_forum_url(task.url):
                    if not task.paper_urls:
                        with self.lock:
                            task = self.tasks[task_id]
                            task.paper_urls = [canonical_paper_url(task.url)]
                            task.paper_states = {task.paper_urls[0]: "pending"}
                            self._refresh_acl_counts(task)
                            self._append_log(task, "单篇 OpenReview 论文已加入采集队列")
                            self.save(task)
                elif not task.paper_urls:
                    refs = client.discover(collection)
                    urls = [canonical_paper_url(ref.url) for ref in refs]
                    if not urls:
                        raise ValueError("OpenReview 分组没有发现可采集的论文")
                    with self.lock:
                        task = self.tasks[task_id]
                        task.paper_urls = list(dict.fromkeys(urls))
                        task.paper_states = {
                            url: "pending" for url in task.paper_urls
                        }
                        self._refresh_acl_counts(task)
                        self._append_log(task, f"发现 {len(task.paper_urls)} 篇论文")
                        self.save(task)

                for url in list(self.tasks[task_id].paper_urls):
                    with self.lock:
                        task = self.tasks[task_id]
                        self._refresh_acl_counts(task)
                        if self._acl_limit_reached(task):
                            self._settle_acl_task(task)
                            return
                        if task.paper_states.get(url, "pending") != "pending":
                            continue
                        task.paper_states[url] = "running"
                        task.current_name = url
                        self._refresh_acl_counts(task)
                        self.save(task)
                    try:
                        paper = self._fetch_openreview_paper(
                            client, url, collection=collection
                        )
                        with self.lock:
                            task = self.tasks[task_id]
                            task.papers = [
                                item for item in task.papers if item.get("url") != url
                            ] + [paper.to_dict()]
                            task.paper_states[url] = "success"
                            task.paper_errors.pop(url, None)
                            self._write_paper(task, task.papers[-1])
                            self._refresh_acl_counts(task)
                            self._append_log(
                                task, f"采集成功：{paper_id_from_url(url)}"
                            )
                            self.save(task)
                            if self._acl_limit_reached(task):
                                self._settle_acl_task(task)
                                return
                    except OpenReviewChallengeError:
                        raise
                    except Exception as exc:
                        with self.lock:
                            task = self.tasks[task_id]
                            task.paper_states[url] = "fail"
                            task.paper_errors[url] = f"{type(exc).__name__}: {exc}"
                            self._refresh_acl_counts(task)
                            self._append_log(
                                task,
                                f"采集失败：{paper_id_from_url(url)} - "
                                f"{task.paper_errors[url]}",
                            )
                            self.save(task)
        except OpenReviewChallengeError as exc:
            self._await_openreview_browser(task_id, exc)
            return
        with self.lock:
            self._settle_acl_task(self.tasks[task_id])

    def _fetch_openreview_paper(
        self,
        client: Any,
        url: str,
        *,
        collection: Any,
    ) -> Paper:
        paper = client.fetch_paper(url, collection=collection)
        if canonical_paper_url(paper.url) != url:
            raise ValueError("OpenReview 返回的 forum id 与任务不一致")
        return paper

    def _await_openreview_browser(
        self, task_id: str, exc: OpenReviewChallengeError
    ) -> None:
        with self.lock:
            task = self.tasks[task_id]
            # A challenge is an infrastructure-level pause, not a paper
            # failure.  Return the in-flight item to pending so the counters
            # retain the invariant discovered = success + failed + pending.
            for url, state in list(task.paper_states.items()):
                if state == "running":
                    task.paper_states[url] = "pending"
            task.status = "awaiting_browser"
            task.current_name = None
            task.error = str(exc)
            task.finished_at = datetime.now(timezone.utc).isoformat()
            self._refresh_acl_counts(task)
            self._append_log(
                task,
                "OpenReview 需要浏览器验证；请导入分组/论文页面快照后继续",
            )
            self.save(task)

    @staticmethod
    def _acl_limit_reached(task: TaskRecord) -> bool:
        return task.limit is not None and task.counts["processed"] >= task.limit

    def _settle_acl_task(self, task: TaskRecord, retry_only: bool = False) -> None:
        self._refresh_acl_counts(task)
        has_work = bool(task.counts["pending"] or task.counts["running"])
        has_failures = bool(task.counts["failed"])
        if self._acl_limit_reached(task) and (has_work or has_failures):
            task.status = "paused"
            message = f"已达到成功论文上限 {task.limit}，任务暂停"
        elif retry_only and has_work:
            task.status = "paused"
            message = "失败项重试结束，任务保持暂停"
        elif has_work:
            task.status = "paused"
            message = "仍有待采集论文，任务暂停"
        elif has_failures:
            task.status = "completed_with_errors"
            message = f"采集结束，仍有 {task.counts['failed']} 篇失败"
        else:
            task.status = "completed"
            message = "采集完成"
        task.current_name = None
        task.finished_at = datetime.now(timezone.utc).isoformat()
        self._append_log(task, message)
        self.save(task)

    def _collect_acl_paper(
        self,
        task_id: str,
        url: str,
        fetcher: object,
        use_cache: bool,
    ) -> Any:
        page = fetcher.fetch(url, use_cache=use_cache)
        final_url = canonical_paper_url(page.url)
        if final_url != url:
            raise ValueError(
                f"论文重定向后的标识不一致：{paper_id_from_url(final_url)}"
            )
        paper = extract_paper(
            page.text,
            url,
            log=lambda message: self._acl_log(task_id, message),
        )
        with self.lock:
            task = self.tasks[task_id]
            self._atomic_write_text(self._paper_raw_path(task, url), page.text)
        return paper

    @staticmethod
    def _clear_acl_outputs(task: TaskRecord) -> None:
        root = task.output_dir.resolve()
        for name in ("cache", "html", "papers"):
            target = (root / name).resolve()
            target.relative_to(root)
            if target.is_dir():
                shutil.rmtree(target)
        for path in [root / "papers.csv", *root.glob("source-*.zip")]:
            path.resolve().relative_to(root)
            path.unlink(missing_ok=True)

    def _finish(self, task: TaskRecord) -> None:
        task.status = "completed"
        task.current_name = None
        task.finished_at = datetime.now(timezone.utc).isoformat()
        self.save(task)

    @staticmethod
    def _load_teachers(output_dir: Path) -> list[dict[str, Any]]:
        storage = Storage(output_dir)
        repository = TeacherRepository(output_dir / "teacher_data.db")
        records = {teacher.profile_url: teacher for teacher in storage.load_records()}
        result = []
        for row in repository.list(sort="name", order="asc"):
            teacher = records.get(row["homepage_url"])
            if teacher:
                result.append({"id": int(row["id"]), "name": teacher.name, "profile_url": teacher.profile_url, "document_name": f"{teacher_stem(teacher)}.docx"})
        return result

    def failures(self, task: TaskRecord) -> list[dict[str, str]]:
        if task.mode in PAPER_MODES:
            return [{"name": url, "profile_url": url, "error": task.paper_errors.get(url, "")} for url, state in task.paper_states.items() if state == "fail"]
        storage = Storage(task.output_dir)
        return [{"name": storage.state.failed_names.get(url, ""), "profile_url": url, "error": error} for url, error in storage.state.failed.items()]

    def retry(self, task_id: str, url: str | None = None) -> TaskRecord:
        with self.lock:
            task = self.get(task_id)
            if task.status in {"queued", "running", "retrying"}:
                raise ValueError("任务正在执行，不能同时重试")
            targets = [item for item in self.failures(task) if url is None or item["profile_url"] == url]
            if not targets:
                raise ValueError("当前没有失败条目")
            if task.mode in PAPER_MODES and task.limit is not None:
                available = task.limit - task.counts["processed"]
                if available < len(targets):
                    raise ValueError(
                        f"重试成功可能超过成功论文上限；请先将上限至少提高到 "
                        f"{task.counts['processed'] + len(targets)}"
                    )
            task.status = "retrying"
            task.error = None
            self.save(task)
        if task.mode == "acl":
            self.executor.submit(self._retry_acl, task_id, [item["profile_url"] for item in targets])
        elif task.mode == "openreview":
            self.executor.submit(
                self._retry_openreview,
                task_id,
                [item["profile_url"] for item in targets],
            )
        else:
            self.executor.submit(self._retry_teacher, task_id, targets)
        return task

    def _retry_acl(self, task_id: str, urls: list[str]) -> None:
        try:
            with self.lock:
                task = self.tasks[task_id]
            with make_fetcher(task.output_dir) as fetcher:
                for url in urls:
                    with self.lock:
                        task = self.tasks[task_id]
                        task.paper_states[url] = "running"
                        task.current_name = url
                        self._refresh_acl_counts(task)
                        self.save(task)
                    try:
                        paper = self._collect_acl_paper(
                            task_id,
                            url,
                            fetcher,
                            use_cache=False,
                        )
                        with self.lock:
                            task = self.tasks[task_id]
                            task.papers = [
                                p for p in task.papers if p.get("url") != url
                            ] + [paper.to_dict()]
                            task.paper_states[url] = "success"
                            task.paper_errors.pop(url, None)
                            self._write_paper(task, task.papers[-1])
                            self._refresh_acl_counts(task)
                            self._append_log(
                                task,
                                f"重试成功：{paper_id_from_url(url)}",
                            )
                            self.save(task)
                    except Exception as exc:
                        with self.lock:
                            task = self.tasks[task_id]
                            task.paper_states[url] = "fail"
                            task.paper_errors[url] = f"{type(exc).__name__}: {exc}"
                            self._refresh_acl_counts(task)
                            self._append_log(
                                task,
                                f"重试失败：{paper_id_from_url(url)} - "
                                f"{task.paper_errors[url]}",
                            )
                            self.save(task)
            with self.lock:
                self._settle_acl_task(self.tasks[task_id], retry_only=True)
        except Exception as exc:
            with self.lock:
                task = self.tasks[task_id]
                for url in urls:
                    if task.paper_states.get(url) == "running":
                        task.paper_states[url] = "fail"
                        task.paper_errors[url] = f"{type(exc).__name__}: {exc}"
                task.error = f"{type(exc).__name__}: {exc}"
                self._refresh_acl_counts(task)
                self._settle_acl_task(task, retry_only=True)

    def _acl_log(self, task_id: str, message: str) -> None:
        with self.lock:
            self._append_log(self.tasks[task_id], message)

    def _retry_openreview(self, task_id: str, urls: list[str]) -> None:
        try:
            with self.lock:
                task = self.tasks[task_id]
                collection = (
                    None
                    if is_openreview_forum_url(task.url)
                    else collection_from_url(task.url)
                )
            with make_openreview_client(task.output_dir) as client:
                for url in urls:
                    with self.lock:
                        task = self.tasks[task_id]
                        task.paper_states[url] = "running"
                        task.current_name = url
                        self._refresh_acl_counts(task)
                        self.save(task)
                    try:
                        paper = self._fetch_openreview_paper(
                            client, url, collection=collection
                        )
                        with self.lock:
                            task = self.tasks[task_id]
                            task.papers = [
                                item
                                for item in task.papers
                                if item.get("url") != url
                            ] + [paper.to_dict()]
                            task.paper_states[url] = "success"
                            task.paper_errors.pop(url, None)
                            self._write_paper(task, task.papers[-1])
                            self._refresh_acl_counts(task)
                            self._append_log(
                                task, f"重试成功：{paper_id_from_url(url)}"
                            )
                            self.save(task)
                    except OpenReviewChallengeError:
                        raise
                    except Exception as exc:
                        with self.lock:
                            task = self.tasks[task_id]
                            task.paper_states[url] = "fail"
                            task.paper_errors[url] = f"{type(exc).__name__}: {exc}"
                            self._refresh_acl_counts(task)
                            self._append_log(
                                task,
                                f"重试失败：{paper_id_from_url(url)} - "
                                f"{task.paper_errors[url]}",
                            )
                            self.save(task)
            with self.lock:
                self._settle_acl_task(self.tasks[task_id], retry_only=True)
        except OpenReviewChallengeError as exc:
            self._await_openreview_browser(task_id, exc)
        except Exception as exc:
            with self.lock:
                task = self.tasks[task_id]
                for url in urls:
                    if task.paper_states.get(url) == "running":
                        task.paper_states[url] = "fail"
                        task.paper_errors[url] = f"{type(exc).__name__}: {exc}"
                task.error = f"{type(exc).__name__}: {exc}"
                self._refresh_acl_counts(task)
                self._settle_acl_task(task, retry_only=True)

    def _retry_teacher(self, task_id: str, failures: list[dict[str, str]]) -> None:
        try:
            from .main import retry_failed_profiles

            with self.lock:
                task = self.tasks[task_id]; config = self.teacher_config(task.url); config.output_dir = task.output_dir
            result = retry_failed_profiles(config, [DiscoveredProfile(name=item["name"], url=item["profile_url"]) for item in failures])
            with self.lock:
                task = self.tasks[task_id]; task.counts["processed"] += result["processed"]; task.counts["failed"] = len(Storage(task.output_dir).state.failed); task.teachers = self._load_teachers(task.output_dir); self._finish(task)
        except Exception as exc:
            with self.lock:
                task = self.tasks[task_id]; task.status = "completed"; task.error = str(exc); self.save(task)

    def snapshot(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            task = self.get(task_id)
            total = task.counts["discovered"]
            done = (
                task.counts["processed"]
                + task.counts["failed"]
                + task.counts["skipped"]
            )
            exportable = task.status in {
                "paused",
                "awaiting_browser",
                "completed",
                "completed_with_errors",
            }
            csv_url = None
            if exportable:
                csv_url = (
                    f"/api/tasks/{task.id}/papers.csv"
                    if task.mode in PAPER_MODES
                    else f"/api/tasks/{task.id}/teachers.csv"
                )
            if task.limit is not None:
                target = min(task.limit, total) if total else task.limit
                progress = min(100, round(task.counts["processed"] / target * 100))
            else:
                progress = min(100, round(done / total * 100)) if total else 0
            coverage = min(100, round(done / total * 100, 1)) if total else 0
            data: dict[str, Any] = {
                "id": task.id,
                "url": task.url,
                "mode": task.mode,
                "school": task.school,
                "college": task.college,
                "limit": task.limit,
                "status": task.status,
                "counts": dict(task.counts),
                "progress": progress,
                "coverage_progress": coverage,
                "logs": list(task.logs),
                "failures": self.failures(task)[:200],
                "failure_count": task.counts["failed"],
                "force_reset_allowed": not any(
                    "浏览器快照" in str(paper.get("parser_mode", ""))
                    for paper in task.papers
                ),
                "error": task.error,
                "zip_url": (
                    f"/api/tasks/{task.id}/download.zip" if exportable else None
                ),
                "csv_url": csv_url,
            }
            if task.mode not in PAPER_MODES:
                data["teachers"] = [
                    {
                        **item,
                        "download_url": (
                            f"/api/tasks/{task.id}/teachers/{index}/download"
                        ),
                    }
                    for index, item in enumerate(task.teachers)
                ]
            return data

    def paper_page(
        self,
        task_id: str,
        query: str = "",
        paper_status: str = "",
        favorite: bool = False,
        sort: str = "default",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        with self.lock:
            task = self.get(task_id)
            if task.mode not in PAPER_MODES:
                raise ValueError("当前任务不是论文任务")
            by_url = {str(paper.get("url")): paper for paper in task.papers}
            rows: list[dict[str, Any]] = []
            for url in task.paper_urls:
                state = task.paper_states.get(url, "pending")
                if state == "success" and url in by_url:
                    row = dict(by_url[url])
                    row["status"] = "success"
                else:
                    row = {
                        "url": url,
                        "title": "",
                        "title_en": "",
                        "title_zh": "",
                        "authors": [],
                        "pdf_url": "",
                        "abstract_en": "",
                        "abstract_zh": "",
                        "favorite": False,
                        "parser_mode": "",
                        "status": state,
                        "error": task.paper_errors.get(url, ""),
                    }
                authors = row.get("authors", [])
                if not isinstance(authors, list):
                    authors = [str(authors)] if authors else []
                row["authors"] = authors
                row["raw_available"] = (
                    state == "success"
                    and self._resolve_paper_raw_path(task, url).is_file()
                )
                rows.append(row)
            if paper_status:
                rows = [row for row in rows if row.get("status") == paper_status]
            if favorite:
                rows = [row for row in rows if row.get("favorite")]
            normalized_query = query.strip().casefold()
            if normalized_query:
                rows = [
                    row
                    for row in rows
                    if normalized_query
                    in " ".join(
                        [
                            str(row.get("title", "")),
                            str(row.get("title_zh", "")),
                            " ".join(str(author) for author in row.get("authors", [])),
                            str(row.get("abstract_en", "")),
                            str(row.get("abstract_zh", "")),
                            str(row.get("venue", "")),
                            str(row.get("decision", "")),
                            str(row.get("decision_comment", "")),
                            str(row.get("tldr", "")),
                            str(row.get("lay_summary", "")),
                            str(row.get("primary_area", "")),
                            " ".join(str(value) for value in row.get("keywords", [])),
                            str(row.get("submission_number", "")),
                            str(row.get("url", "")),
                            str(row.get("error", "")),
                        ]
                    ).casefold()
                ]
            if sort == "title":
                rows.sort(key=lambda row: str(row.get("title", "")).casefold())
            elif sort == "collected_at":
                rows.sort(
                    key=lambda row: str(row.get("collected_at", "")),
                    reverse=True,
                )
            count = len(rows)
            return {
                "count": count,
                "offset": offset,
                "limit": limit,
                "papers": rows[offset : offset + limit],
            }

    def csv_path(self, task_id: str) -> Path:
        task = self.get(task_id)
        if task.mode in PAPER_MODES:
            rows = self.paper_page(task_id, limit=max(1, len(task.paper_urls)))["papers"]
            return write_csv(task.output_dir / "papers.csv", rows)
        return TeacherRepository(task.output_dir / "teacher_data.db").export_csv(task.output_dir / "teachers.csv")

    def zip_path(self, task_id: str) -> Path:
        task = self.get(task_id)
        if task.mode in PAPER_MODES:
            self.csv_path(task_id)
        destination = task.output_dir / f"source-{task.id[:8]}.zip"
        temp = task.output_dir / ".source.zip.tmp"
        with ZipFile(temp, "w", ZIP_DEFLATED) as archive:
            if task.mode in PAPER_MODES:
                csv_path = task.output_dir / "papers.csv"
                archive.write(csv_path, "papers.csv")
                for paper in task.papers:
                    url = str(paper.get("url", ""))
                    record_path = self._paper_record_path(task, url)
                    if record_path.is_file():
                        archive.write(record_path, f"papers/{record_path.name}")
                    raw_path = self._resolve_paper_raw_path(task, url)
                    if raw_path.is_file():
                        archive.write(
                            raw_path,
                            f"html/{paper_id_from_url(url)}.html",
                        )
            else:
                for path in task.output_dir.rglob("*"):
                    if (
                        path.is_file()
                        and path.name not in {"task.json", temp.name}
                        and path.suffix.lower() != ".zip"
                    ):
                        archive.write(
                            path,
                            path.relative_to(task.output_dir).as_posix(),
                        )
        os.replace(temp, destination)
        return destination

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)


MANAGER = TaskManager()
atexit.register(MANAGER.close)
app = FastAPI(title="Teacher and Paper Crawler", version="0.3.0")


def task_or_404(task_id: str) -> TaskRecord:
    try: return MANAGER.get(task_id)
    except KeyError as exc: raise HTTPException(404, "任务不存在") from exc


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/tasks", status_code=status.HTTP_202_ACCEPTED)
def create_task(request: CrawlRequest) -> dict[str, Any]:
    try: return MANAGER.snapshot(MANAGER.create(request).id)
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc


@app.get("/api/tasks/{task_id}")
def task_status(task_id: str) -> dict[str, Any]: task_or_404(task_id); return MANAGER.snapshot(task_id)


@app.post("/api/tasks/{task_id}/browser-discovery")
def import_browser_discovery(
    task_id: str,
    request: BrowserDiscoveryRequest,
) -> dict[str, Any]:
    task_or_404(task_id)
    try:
        MANAGER.import_acm_discovery(task_id, request)
        return MANAGER.snapshot(task_id)
    except (ValueError, OpenReviewError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/tasks/{task_id}/browser-papers")
def import_browser_papers(
    task_id: str,
    request: BrowserPapersRequest,
) -> dict[str, Any]:
    task_or_404(task_id)
    try:
        MANAGER.import_acm_papers(task_id, request)
        return MANAGER.snapshot(task_id)
    except (ValueError, OpenReviewError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/tasks/{task_id}/continue", status_code=status.HTTP_202_ACCEPTED)
def continue_task(task_id: str, request: ContinueRequest) -> dict[str, Any]:
    with MANAGER.lock:
        task = task_or_404(task_id)
        if task.status not in {"paused", "awaiting_browser"}:
            raise HTTPException(400, "当前任务不是可继续状态")
        new_limit = request.limit if request.limit is not None else task.limit
        if new_limit is not None and new_limit <= task.counts["processed"]:
            raise HTTPException(
                400,
                f"新采集上限必须大于当前成功数 {task.counts['processed']}",
            )
        task.limit = new_limit
        task.status = "queued"
        task.error = None
        MANAGER.save(task)
    MANAGER.executor.submit(MANAGER._run, task_id)
    return MANAGER.snapshot(task_id)


@app.post("/api/tasks/{task_id}/reset", status_code=status.HTTP_202_ACCEPTED)
def reset_task(task_id: str) -> dict[str, Any]:
    with MANAGER.lock:
        task = task_or_404(task_id)
        if task.status in {"queued", "running", "retrying"}:
            raise HTTPException(409, "任务正在执行，不能同时重置")
        if any(
            "浏览器快照" in str(paper.get("parser_mode", ""))
            for paper in task.papers
        ):
            raise HTTPException(
                409,
                "该任务来自浏览器快照，强制重抓会因站点验证丢失现有结果",
            )
        task.status = "queued"
        task.error = None
        task.current_name = None
        task.papers.clear()
        task.paper_urls.clear()
        task.paper_states.clear()
        task.paper_errors.clear()
        if task.mode in PAPER_MODES:
            MANAGER._refresh_acl_counts(task)
        else:
            task.counts = {
                "discovered": 0,
                "processed": 0,
                "skipped": 0,
                "duplicates": 0,
                "failed": 0,
            }
        MANAGER.save(task)
    MANAGER.executor.submit(MANAGER._run, task_id, True)
    return MANAGER.snapshot(task_id)


@app.post("/api/tasks/{task_id}/failures/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_one(task_id: str, request: RetryRequest) -> dict[str, Any]:
    task_or_404(task_id)
    try: MANAGER.retry(task_id, request.profile_url); return MANAGER.snapshot(task_id)
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc


@app.post("/api/tasks/{task_id}/failures/retry-all", status_code=status.HTTP_202_ACCEPTED)
def retry_all(task_id: str) -> dict[str, Any]:
    task_or_404(task_id)
    try: MANAGER.retry(task_id); return MANAGER.snapshot(task_id)
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc


@app.get("/api/tasks/{task_id}/papers")
def papers_page(
    task_id: str,
    q: str = "",
    paper_status: str = "",
    favorite: bool = False,
    sort: str = "default",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    task_or_404(task_id)
    try:
        return MANAGER.paper_page(
            task_id,
            query=q,
            paper_status=paper_status,
            favorite=favorite,
            sort=sort,
            offset=offset,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/tasks/{task_id}/research")
def research(task_id: str, q: str = "", recruit_type: str = "", has_recruit_info: bool | None = None, has_email: bool | None = None, favorite: bool | None = None, contact_status: str = "", sort: str = "recruit_score", order: str = "desc") -> dict[str, Any]:
    task = task_or_404(task_id)
    if task.mode in PAPER_MODES: return {"count": 0, "teachers": []}
    rows = TeacherRepository(task.output_dir / "teacher_data.db").list(query=q.strip(), recruit_type=recruit_type, has_recruit_info=has_recruit_info, has_email=has_email, favorite=favorite, contact_status=contact_status, sort=sort, order=order)
    return {"count": len(rows), "teachers": rows}


@app.patch("/api/tasks/{task_id}/research/{teacher_id}")
def update_teacher(task_id: str, teacher_id: int, update: TeacherUpdate) -> dict[str, Any]:
    task = task_or_404(task_id)
    try: return TeacherRepository(task.output_dir / "teacher_data.db").update_manual(teacher_id, update.note, update.contact_status, update.favorite)
    except (KeyError, ValueError) as exc: raise HTTPException(400, str(exc)) from exc


@app.patch("/api/tasks/{task_id}/papers/{paper_url:path}")
def update_paper(task_id: str, paper_url: str, update: PaperUpdate) -> dict[str, Any]:
    task = task_or_404(task_id)
    paper_url = "https://" + paper_url if not paper_url.startswith("http") else paper_url
    try:
        paper_url = canonical_paper_url(paper_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with MANAGER.lock:
        for paper in task.papers:
            if paper.get("url") == paper_url:
                if update.abstract_zh is not None:
                    paper["abstract_zh"] = update.abstract_zh
                    paper["translation_failed"] = False
                    paper["translation_error"] = ""
                if update.favorite is not None:
                    paper["favorite"] = update.favorite
                MANAGER._write_paper(task, paper)
                if task.mode in PAPER_MODES:
                    MANAGER._refresh_acl_counts(task)
                MANAGER.save(task)
                return paper
    raise HTTPException(404, "论文记录不存在")


@app.get("/api/tasks/{task_id}/papers/raw")
def paper_raw(task_id: str, url: str) -> FileResponse:
    task = task_or_404(task_id)
    try:
        url = canonical_paper_url(url)
    except ValueError as exc:
        raise HTTPException(404, "原始网页不存在") from exc
    if task.mode not in PAPER_MODES or task.paper_states.get(url) != "success":
        raise HTTPException(404, "原始网页不存在")
    path = MANAGER._resolve_paper_raw_path(task, url)
    if not path.is_file():
        raise HTTPException(404, "原始网页不存在")
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        filename=path.name,
    )


@app.get("/api/tasks/{task_id}/teachers.csv")
def teachers_csv(task_id: str) -> FileResponse: task_or_404(task_id); return FileResponse(MANAGER.csv_path(task_id), media_type="text/csv; charset=utf-8", filename="teachers.csv")


@app.get("/api/tasks/{task_id}/papers.csv")
def papers_csv(task_id: str) -> FileResponse: task_or_404(task_id); return FileResponse(MANAGER.csv_path(task_id), media_type="text/csv; charset=utf-8", filename="papers.csv")


@app.get("/api/tasks/{task_id}/teachers/{teacher_index}/download")
def teacher_download(task_id: str, teacher_index: int) -> FileResponse:
    task = task_or_404(task_id)
    try: filename = task.teachers[teacher_index]["document_name"]
    except (IndexError, KeyError): raise HTTPException(404, "文件不存在")
    path = (task.output_dir / "documents" / filename).resolve()
    if not path.is_file(): raise HTTPException(404, "文件不存在")
    return FileResponse(path, filename=path.name)


@app.get("/api/tasks/{task_id}/download.zip")
def download_all(task_id: str) -> FileResponse: task_or_404(task_id); path = MANAGER.zip_path(task_id); return FileResponse(path, media_type="application/zip", filename=path.name)


def main() -> None: uvicorn.run("crawler.web:app", host="127.0.0.1", port=8002, reload=False)


if __name__ == "__main__": main()
