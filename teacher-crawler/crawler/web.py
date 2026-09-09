from __future__ import annotations

import atexit
import json
import os
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
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .acl import discover_papers, extract_paper, is_paper_url, make_fetcher, validate_volume_url, write_csv
from .config import SchoolConfig, load_school_config
from .discovery import DiscoveredProfile
from .main import retry_failed_profiles, run
from .repository import TeacherRepository
from .storage import Storage, safe_filename_part, teacher_stem

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "configs"
TASKS_DIR = ROOT / "output" / "web-tasks"
STATIC_DIR = Path(__file__).resolve().parent / "static"


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
    abstract_zh: str = ""
    favorite: bool = False


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
    counts: dict[str, int] = field(default_factory=lambda: {"discovered": 0, "processed": 0, "skipped": 0, "duplicates": 0, "failed": 0})
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
                raw["output_dir"] = Path(raw["output_dir"])
                task = TaskRecord(**raw)
                if task.status in {"running", "retrying"}:
                    task.status = "queued"
                self.tasks[task.id] = task
            except (OSError, ValueError, TypeError):
                continue
        for task in list(self.tasks.values()):
            if task.status == "queued":
                self.executor.submit(self._run, task.id)

    def save(self, task: TaskRecord) -> None:
        task.output_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(task)
        payload["output_dir"] = str(task.output_dir)
        temp = task.output_dir / ".task.json.tmp"
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, task.output_dir / "task.json")

    def log(self, task: TaskRecord, message: str) -> None:
        task.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
        del task.logs[:-300]
        self.save(task)

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
        if request.mode == "acl":
            url = validate_volume_url(url)
            school = college = "ACL Anthology"
        elif request.mode == "teacher":
            config = self.teacher_config(url)
            url, school, college = config.start_urls[0], config.school, config.college
        else:
            raise ValueError("未知采集模式")
        task = TaskRecord(uuid.uuid4().hex, url, request.mode, school, college, request.limit)
        task.output_dir = TASKS_DIR / task.id
        with self.lock:
            self.tasks[task.id] = task
            self.log(task, "任务已创建，等待执行")
        self.executor.submit(self._run, task.id)
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
                task.error = f"{type(exc).__name__}: {exc}"
                task.finished_at = datetime.now(timezone.utc).isoformat()
                self.log(task, f"任务终止：{task.error}")

    def _run_acl(self, task_id: str, force: bool) -> None:
        with self.lock:
            task = self.tasks[task_id]
        with make_fetcher(task.output_dir) as fetcher:
            volume = fetcher.fetch(task.url, use_cache=not force)
            urls = [volume.url] if is_paper_url(volume.url) else discover_papers(volume.text, volume.url)
            with self.lock:
                task = self.tasks[task_id]
                if force:
                    task.papers.clear(); task.paper_states.clear(); task.paper_errors.clear()
                    task.counts.update({"processed": 0, "skipped": 0, "failed": 0})
                task.paper_urls = urls
                task.counts["discovered"] = len(urls)
                self.save(task)
            for url in urls:
                with self.lock:
                    task = self.tasks[task_id]
                    if task.limit is not None and task.counts["processed"] >= task.limit:
                        task.status = "paused"
                        task.current_name = None
                        task.finished_at = datetime.now(timezone.utc).isoformat()
                        self.log(task, f"已达到采集上限{task.limit}，任务暂停")
                        return
                    if task.paper_states.get(url) == "success":
                        task.counts["skipped"] += 1
                        self.save(task)
                        continue
                    task.paper_states[url] = "running"
                    task.current_name = url
                    self.save(task)
                try:
                    page = fetcher.fetch(url, use_cache=not force)
                    paper = extract_paper(
                        page.text,
                        page.url,
                        log=lambda message: self._acl_log(task_id, message),
                    )
                    filename = safe_filename_part(paper.title) + ".html"
                    html_dir = task.output_dir / "html"
                    html_dir.mkdir(parents=True, exist_ok=True)
                    (html_dir / filename).write_text(page.text, encoding="utf-8")
                    with self.lock:
                        task = self.tasks[task_id]
                        task.papers = [p for p in task.papers if p.get("url") != url] + [paper.to_dict()]
                        task.paper_states[url] = "success"
                        task.paper_errors.pop(url, None)
                        task.counts["processed"] += 1
                        self.save(task)
                        if task.limit is not None and task.counts["processed"] >= task.limit:
                            task.status = "paused"
                            task.current_name = None
                            task.finished_at = datetime.now(timezone.utc).isoformat()
                            self.log(task, f"已达到采集上限{task.limit}，任务暂停")
                            return
                except Exception as exc:
                    with self.lock:
                        task = self.tasks[task_id]
                        task.paper_states[url] = "fail"
                        task.paper_errors[url] = f"{type(exc).__name__}: {exc}"
                        task.counts["failed"] += 1
                        self.save(task)
        with self.lock:
            self._finish(self.tasks[task_id])

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
        if task.mode == "acl":
            return [{"name": url, "profile_url": url, "error": task.paper_errors.get(url, "")} for url, state in task.paper_states.items() if state == "fail"]
        storage = Storage(task.output_dir)
        return [{"name": storage.state.failed_names.get(url, ""), "profile_url": url, "error": error} for url, error in storage.state.failed.items()]

    def retry(self, task_id: str, url: str | None = None) -> TaskRecord:
        with self.lock:
            task = self.get(task_id)
            targets = [item for item in self.failures(task) if url is None or item["profile_url"] == url]
            if not targets:
                raise ValueError("当前没有失败条目")
            task.status = "retrying"
            self.save(task)
        if task.mode == "acl":
            self.executor.submit(self._retry_acl, task_id, [item["profile_url"] for item in targets])
        else:
            self.executor.submit(self._retry_teacher, task_id, targets)
        return task

    def _retry_acl(self, task_id: str, urls: list[str]) -> None:
        with self.lock:
            task = self.tasks[task_id]
        with make_fetcher(task.output_dir) as fetcher:
            for url in urls:
                try:
                    page = fetcher.fetch(url, use_cache=False)
                    paper = extract_paper(
                        page.text,
                        page.url,
                        log=lambda message: self._acl_log(task_id, message),
                    )
                    with self.lock:
                        task = self.tasks[task_id]
                        task.papers = [p for p in task.papers if p.get("url") != url] + [paper.to_dict()]
                        task.paper_states[url] = "success"; task.paper_errors.pop(url, None)
                        task.counts["failed"] = max(0, task.counts["failed"] - 1); task.counts["processed"] += 1
                        self.save(task)
                except Exception as exc:
                    with self.lock:
                        task.paper_errors[url] = f"{type(exc).__name__}: {exc}"; self.save(task)
        with self.lock: self._finish(self.tasks[task_id])

    def _acl_log(self, task_id: str, message: str) -> None:
        with self.lock:
            self.log(self.tasks[task_id], message)

    def _retry_teacher(self, task_id: str, failures: list[dict[str, str]]) -> None:
        try:
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
            done = task.counts["processed"] + task.counts["failed"] + task.counts["skipped"]
            exportable = task.status in {"paused", "completed"}
            csv_url = None
            if exportable:
                csv_url = (
                    f"/api/tasks/{task.id}/papers.csv"
                    if task.mode == "acl"
                    else f"/api/tasks/{task.id}/teachers.csv"
                )
            data: dict[str, Any] = {"id": task.id, "url": task.url, "mode": task.mode, "school": task.school, "college": task.college, "limit": task.limit, "status": task.status, "counts": dict(task.counts), "progress": 100 if task.status == "completed" else (min(99, round(done / total * 100)) if total else 0), "logs": list(task.logs), "failures": self.failures(task), "error": task.error, "zip_url": f"/api/tasks/{task.id}/download.zip" if task.status == "completed" else None, "csv_url": csv_url}
            if task.mode == "acl": data["papers"] = task.papers
            else: data["teachers"] = [{**item, "download_url": f"/api/tasks/{task.id}/teachers/{index}/download"} for index, item in enumerate(task.teachers)]
            return data

    def csv_path(self, task_id: str) -> Path:
        task = self.get(task_id)
        if task.mode == "acl": return write_csv(task.output_dir / "papers.csv", task.papers)
        return TeacherRepository(task.output_dir / "teacher_data.db").export_csv(task.output_dir / "teachers.csv")

    def zip_path(self, task_id: str) -> Path:
        task = self.get(task_id); destination = task.output_dir / f"source-{task.id[:8]}.zip"; temp = task.output_dir / ".source.zip.tmp"
        with ZipFile(temp, "w", ZIP_DEFLATED) as archive:
            for path in task.output_dir.rglob("*"):
                if path.is_file() and path.name not in {"task.json", temp.name}:
                    archive.write(path, path.relative_to(task.output_dir).as_posix())
        os.replace(temp, destination)
        return destination

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)


MANAGER = TaskManager()
atexit.register(MANAGER.close)
app = FastAPI(title="Teacher and ACL Crawler", version="0.2.0")


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


@app.post("/api/tasks/{task_id}/continue", status_code=status.HTTP_202_ACCEPTED)
def continue_task(task_id: str, request: ContinueRequest) -> dict[str, Any]:
    with MANAGER.lock:
        task = task_or_404(task_id)
        if task.status != "paused":
            raise HTTPException(400, "当前任务不是暂停状态")
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
        task = task_or_404(task_id); task.status = "queued"; task.counts = {"discovered": 0, "processed": 0, "skipped": 0, "duplicates": 0, "failed": 0}; task.papers.clear(); task.paper_states.clear(); task.paper_errors.clear(); MANAGER.save(task)
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


@app.get("/api/tasks/{task_id}/research")
def research(task_id: str, q: str = "", recruit_type: str = "", has_recruit_info: bool | None = None, has_email: bool | None = None, favorite: bool | None = None, contact_status: str = "", sort: str = "recruit_score", order: str = "desc") -> dict[str, Any]:
    task = task_or_404(task_id)
    if task.mode == "acl": return {"count": 0, "teachers": []}
    rows = TeacherRepository(task.output_dir / "teacher_data.db").list(query=q.strip(), recruit_type=recruit_type, has_recruit_info=has_recruit_info, has_email=has_email, favorite=favorite, contact_status=contact_status, sort=sort, order=order)
    return {"count": len(rows), "teachers": rows}


@app.patch("/api/tasks/{task_id}/research/{teacher_id}")
def update_teacher(task_id: str, teacher_id: int, update: TeacherUpdate) -> dict[str, Any]:
    task = task_or_404(task_id)
    try: return TeacherRepository(task.output_dir / "teacher_data.db").update_manual(teacher_id, update.note, update.contact_status, update.favorite)
    except (KeyError, ValueError) as exc: raise HTTPException(400, str(exc)) from exc


@app.patch("/api/tasks/{task_id}/papers/{paper_url:path}")
def update_paper(task_id: str, paper_url: str, update: PaperUpdate) -> dict[str, Any]:
    task = task_or_404(task_id); paper_url = "https://" + paper_url if not paper_url.startswith("http") else paper_url
    with MANAGER.lock:
        for paper in task.papers:
            if paper.get("url") == paper_url:
                paper["abstract_zh"] = update.abstract_zh
                paper["favorite"] = update.favorite
                paper["translation_failed"] = False
                paper["translation_error"] = ""
                MANAGER.save(task)
                return paper
    raise HTTPException(404, "论文记录不存在")


@app.get("/api/tasks/{task_id}/papers/raw")
def paper_raw(task_id: str, url: str) -> FileResponse:
    task = task_or_404(task_id)
    if task.mode != "acl" or url not in task.paper_states: raise HTTPException(404, "原始网页不存在")
    filename = safe_filename_part(next((str(p.get("title", "paper")) for p in task.papers if p.get("url") == url), "paper")) + ".html"
    path = task.output_dir / "html" / filename
    if not path.is_file(): raise HTTPException(404, "原始网页不存在")
    return FileResponse(path, media_type="text/html; charset=utf-8", filename=filename)


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
