from __future__ import annotations

import atexit
import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlparse
from zipfile import ZIP_DEFLATED, ZipFile

import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import SchoolConfig, load_school_config
from .discovery import DiscoveredProfile
from .main import retry_failed_profiles, run
from .repository import TeacherRepository
from .storage import Storage, teacher_stem

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "configs"
TASKS_DIR = PROJECT_ROOT / "output" / "web-tasks"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class CrawlRequest(BaseModel):
    url: str
    limit: int | None = Field(default=2, ge=1, le=500)


class TeacherUpdate(BaseModel):
    note: str = Field(default="", max_length=2000)
    contact_status: str = "未联系"
    favorite: bool = False


class RetryRequest(BaseModel):
    profile_url: str


@dataclass
class TaskRecord:
    id: str
    url: str
    school: str
    college: str
    limit: int | None
    output_dir: Path
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str | None = None
    finished_at: str | None = None
    current_name: str | None = None
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "discovered": 0,
            "processed": 0,
            "skipped": 0,
            "duplicates": 0,
            "failed": 0,
        }
    )
    logs: list[str] = field(default_factory=list)
    teachers: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="crawler-web")
        TASKS_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _append_log(self, task: TaskRecord, message: str) -> None:
        task.logs.append(f"[{self._timestamp()}] {message}")
        if len(task.logs) > 300:
            del task.logs[:-300]

    def _match_config(self, url: str) -> tuple[str, SchoolConfig]:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not hostname or parsed.username:
            raise ValueError("请输入有效的公开 HTTP(S) 教师列表页 URL")
        for path in sorted(CONFIG_DIR.glob("*.yaml")):
            config = load_school_config(path)
            if hostname in config.allowed_domains:
                normalized, _ = urldefrag(url)
                config.start_urls = [normalized]
                config.directory_urls = [normalized]
                return path.stem, config
        raise ValueError(f"没有找到允许访问域名 {hostname} 的学校配置")

    def create_task(self, request: CrawlRequest) -> TaskRecord:
        _, config = self._match_config(request.url.strip())
        task_id = uuid.uuid4().hex
        output_dir = TASKS_DIR / task_id
        config.output_dir = output_dir
        task = TaskRecord(
            id=task_id,
            url=config.start_urls[0],
            school=config.school,
            college=config.college,
            limit=request.limit,
            output_dir=output_dir,
        )
        self._append_log(task, "任务已创建，等待执行")
        with self._lock:
            self._tasks[task_id] = task
        self._executor.submit(self._run_task, task_id, config)
        return task

    def _handle_event(self, task_id: str, event: str, data: dict[str, object]) -> None:
        with self._lock:
            task = self._tasks[task_id]
            counts = data.get("counts")
            if isinstance(counts, dict):
                task.counts.update({key: int(value) for key, value in counts.items()})
            name = str(data.get("name") or "").strip()
            if event == "discovered":
                self._append_log(task, f"发现 {data.get('count', 0)} 个教师主页")
            elif event == "profile_started":
                task.current_name = name or str(data.get("url") or "")
                self._append_log(task, f"开始处理：{task.current_name}")
            elif event == "profile_completed":
                self._append_log(task, f"处理成功：{name}")
            elif event == "duplicate":
                self._append_log(task, f"内容重复，已跳过：{name}")
            elif event == "profile_failed":
                self._append_log(task, f"处理失败：{name} - {data.get('error', '')}")

    def _run_task(self, task_id: str, config: SchoolConfig) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = "running"
            task.started_at = datetime.now(timezone.utc).isoformat()
            self._append_log(task, f"开始采集 {task.school} / {task.college}")
        try:
            counts = run(
                config,
                resume=True,
                limit=task.limit,
                progress_callback=lambda event, data: self._handle_event(task_id, event, data),
            )
            teachers = self._load_teachers(config.output_dir)
            with self._lock:
                task = self._tasks[task_id]
                task.counts = counts
                task.teachers = teachers
                task.current_name = None
                task.status = "completed"
                task.finished_at = datetime.now(timezone.utc).isoformat()
                self._append_log(task, f"任务完成，共生成 {len(teachers)} 份教师文档")
        except Exception as exc:
            with self._lock:
                task = self._tasks[task_id]
                task.status = "failed"
                task.error = f"{type(exc).__name__}: {exc}"
                task.current_name = None
                task.finished_at = datetime.now(timezone.utc).isoformat()
                self._append_log(task, f"任务终止：{task.error}")

    @staticmethod
    def _load_teachers(output_dir: Path) -> list[dict[str, str]]:
        storage = Storage(output_dir)
        repository = TeacherRepository(output_dir / "teacher_data.db")
        records_by_url = {teacher.profile_url: teacher for teacher in storage.load_records()}
        teachers = []
        for research in repository.list(sort="name", order="asc"):
            teacher = records_by_url.get(research["homepage_url"])
            if teacher is None:
                continue
            teachers.append(
                {
                    "id": str(research["id"]),
                    "name": teacher.name,
                    "profile_url": teacher.profile_url,
                    "document_name": f"{teacher_stem(teacher)}.docx",
                }
            )
        return teachers

    @staticmethod
    def _failed_items(task: TaskRecord) -> list[dict[str, str]]:
        storage = Storage(task.output_dir)
        return [
            {
                "name": storage.state.failed_names.get(profile_url, ""),
                "profile_url": profile_url,
                "error": error,
            }
            for profile_url, error in sorted(storage.state.failed.items())
        ]

    def retry(self, task_id: str, profile_url: str | None = None) -> TaskRecord:
        with self._lock:
            task = self.get(task_id)
            if task.status != "completed":
                raise RuntimeError("任务当前不可重试")
            failures = self._failed_items(task)
            if profile_url is not None:
                failures = [item for item in failures if item["profile_url"] == profile_url]
                if not failures:
                    raise KeyError(profile_url)
            if not failures:
                raise ValueError("当前没有失败教师")
            task.status = "retrying"
            task.error = None
            task.finished_at = None
            action = "全部失败项" if profile_url is None else failures[0]["name"] or profile_url
            self._append_log(task, f"开始重试：{action}")
        self._executor.submit(self._run_retry, task_id, failures)
        return task

    def _handle_retry_event(self, task_id: str, event: str, data: dict[str, object]) -> None:
        with self._lock:
            task = self._tasks[task_id]
            name = str(data.get("name") or data.get("url") or "").strip()
            if event == "profile_started":
                task.current_name = name
                self._append_log(task, f"正在重试：{name}")
            elif event == "profile_completed":
                self._append_log(task, f"重试成功：{name}")
            elif event == "duplicate":
                self._append_log(task, f"重试页面与已有教师重复：{name}")
            elif event == "profile_failed":
                self._append_log(task, f"重试仍失败：{name} - {data.get('error', '')}")

    def _run_retry(self, task_id: str, failures: list[dict[str, str]]) -> None:
        try:
            with self._lock:
                task = self._tasks[task_id]
                _, config = self._match_config(task.url)
                config.output_dir = task.output_dir
            profiles = [
                DiscoveredProfile(name=item["name"], url=item["profile_url"])
                for item in failures
            ]
            result = retry_failed_profiles(
                config,
                profiles,
                progress_callback=lambda event, data: self._handle_retry_event(
                    task_id, event, data
                ),
            )
            storage = Storage(config.output_dir)
            teachers = self._load_teachers(config.output_dir)
            with self._lock:
                task = self._tasks[task_id]
                task.counts["processed"] += result["processed"]
                task.counts["duplicates"] += result["duplicates"]
                task.counts["failed"] = len(storage.state.failed)
                task.teachers = teachers
                task.current_name = None
                task.status = "completed"
                task.finished_at = datetime.now(timezone.utc).isoformat()
                self._append_log(
                    task,
                    f"重试完成：成功 {result['processed']}，仍失败 {result['failed']}",
                )
        except Exception as exc:
            with self._lock:
                task = self._tasks[task_id]
                task.status = "completed"
                task.error = f"{type(exc).__name__}: {exc}"
                task.current_name = None
                task.finished_at = datetime.now(timezone.utc).isoformat()
                self._append_log(task, f"重试任务异常：{task.error}")

    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            return task

    def snapshot(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self.get(task_id)
            target = min(task.counts["discovered"], task.limit) if task.limit else task.counts["discovered"]
            done = task.counts["processed"] + task.counts["failed"] + task.counts["duplicates"]
            progress = 0
            if task.status == "completed":
                progress = 100
            elif target:
                progress = min(99, round(done / target * 100))
            failures = self._failed_items(task)
            return {
                "id": task.id,
                "url": task.url,
                "school": task.school,
                "college": task.college,
                "limit": task.limit,
                "status": task.status,
                "created_at": task.created_at,
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "current_name": task.current_name,
                "counts": dict(task.counts),
                "progress": progress,
                "logs": list(task.logs),
                "teachers": [
                    {
                        "id": int(teacher["id"]),
                        "name": teacher["name"],
                        "profile_url": teacher["profile_url"],
                        "download_url": f"/api/tasks/{task.id}/teachers/{index}/download",
                    }
                    for index, teacher in enumerate(task.teachers)
                ],
                "failures": failures,
                "zip_url": f"/api/tasks/{task.id}/download.zip" if task.status == "completed" else None,
                "csv_url": f"/api/tasks/{task.id}/teachers.csv" if task.status == "completed" else None,
                "error": task.error,
            }

    def document_path(self, task_id: str, teacher_index: int) -> Path:
        with self._lock:
            task = self.get(task_id)
            if task.status != "completed":
                raise RuntimeError("task is not completed")
            try:
                filename = task.teachers[teacher_index]["document_name"]
            except IndexError as exc:
                raise KeyError(teacher_index) from exc
            path = (task.output_dir / "documents" / filename).resolve()
            documents_root = (task.output_dir / "documents").resolve()
            if path.parent != documents_root or not path.is_file():
                raise FileNotFoundError(filename)
            return path

    def zip_path(self, task_id: str) -> Path:
        with self._lock:
            task = self.get(task_id)
            if task.status != "completed":
                raise RuntimeError("task is not completed")
            output_dir = task.output_dir
        destination = output_dir / f"teachers-{task_id[:8]}.zip"
        handle, temp_name = tempfile.mkstemp(dir=output_dir, prefix=".teachers-", suffix=".zip")
        os.close(handle)
        try:
            with ZipFile(temp_name, "w", ZIP_DEFLATED) as archive:
                for directory_name in ("documents", "html", "json", "photos"):
                    directory = output_dir / directory_name
                    if directory.exists():
                        for path in sorted(directory.rglob("*")):
                            if path.is_file():
                                archive.write(path, path.relative_to(output_dir).as_posix())
                for filename in ("teachers.csv", "failures.csv"):
                    path = output_dir / filename
                    if path.is_file():
                        archive.write(path, filename)
            os.replace(temp_name, destination)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return destination

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def repository(self, task_id: str) -> TeacherRepository:
        task = self.get(task_id)
        if task.status != "completed":
            raise RuntimeError("task is not completed")
        return TeacherRepository(task.output_dir / "teacher_data.db")

    def csv_path(self, task_id: str) -> Path:
        task = self.get(task_id)
        repository = self.repository(task_id)
        rows = repository.list(sort="name", order="asc")
        successful_urls = {row["homepage_url"] for row in rows}
        for failure in self._failed_items(task):
            if failure["profile_url"] not in successful_urls:
                rows.append(
                    {
                        "name": failure["name"],
                        "homepage_url": failure["profile_url"],
                        "has_recruit_info": False,
                    }
                )
        return repository.export_csv(task.output_dir / "teachers.csv", rows=rows)


MANAGER = TaskManager()
atexit.register(MANAGER.close)
app = FastAPI(title="Teacher Research Crawler", version="0.1.0")


def _task_or_404(task_id: str) -> TaskRecord:
    try:
        return MANAGER.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/tasks", status_code=status.HTTP_202_ACCEPTED)
def create_task(request: CrawlRequest) -> dict[str, Any]:
    try:
        task = MANAGER.create_task(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MANAGER.snapshot(task.id)


@app.get("/api/tasks/{task_id}")
def task_status(task_id: str) -> dict[str, Any]:
    _task_or_404(task_id)
    return MANAGER.snapshot(task_id)


@app.get("/api/tasks/{task_id}/research")
def research_teachers(
    task_id: str,
    q: str = "",
    recruit_type: str = "",
    has_recruit_info: bool | None = None,
    has_email: bool | None = None,
    favorite: bool | None = None,
    contact_status: str = "",
    sort: str = "recruit_score",
    order: str = "desc",
) -> dict[str, Any]:
    _task_or_404(task_id)
    try:
        repository = MANAGER.repository(task_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    rows = repository.list(
        query=q.strip(),
        recruit_type=recruit_type,
        has_recruit_info=has_recruit_info,
        has_email=has_email,
        favorite=favorite,
        contact_status=contact_status,
        sort=sort,
        order=order,
    )
    task = MANAGER.get(task_id)
    download_indexes = {
        int(teacher["id"]): index for index, teacher in enumerate(task.teachers)
    }
    for row in rows:
        index = download_indexes.get(row["id"])
        row["download_url"] = (
            f"/api/tasks/{task_id}/teachers/{index}/download" if index is not None else None
        )
    return {"count": len(rows), "teachers": rows}


@app.post("/api/tasks/{task_id}/failures/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_failure(task_id: str, request: RetryRequest) -> dict[str, Any]:
    _task_or_404(task_id)
    try:
        MANAGER.retry(task_id, request.profile_url)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="失败教师记录不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return MANAGER.snapshot(task_id)


@app.post("/api/tasks/{task_id}/failures/retry-all", status_code=status.HTTP_202_ACCEPTED)
def retry_all_failures(task_id: str) -> dict[str, Any]:
    _task_or_404(task_id)
    try:
        MANAGER.retry(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return MANAGER.snapshot(task_id)


@app.patch("/api/tasks/{task_id}/research/{teacher_id}")
def update_research_teacher(
    task_id: str, teacher_id: int, update: TeacherUpdate
) -> dict[str, Any]:
    _task_or_404(task_id)
    try:
        return MANAGER.repository(task_id).update_manual(
            teacher_id, update.note, update.contact_status, update.favorite
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="教师记录不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/tasks/{task_id}/teachers.csv")
def download_csv(task_id: str) -> FileResponse:
    _task_or_404(task_id)
    try:
        path = MANAGER.csv_path(task_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(path, media_type="text/csv; charset=utf-8", filename="teachers.csv")


@app.get("/api/tasks/{task_id}/teachers/{teacher_index}/download")
def download_teacher(task_id: str, teacher_index: int) -> FileResponse:
    _task_or_404(task_id)
    try:
        path = MANAGER.document_path(task_id, teacher_index)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="教师文档不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=path.name,
    )


@app.get("/api/tasks/{task_id}/download.zip")
def download_all(task_id: str) -> FileResponse:
    _task_or_404(task_id)
    try:
        path = MANAGER.zip_path(task_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(path, media_type="application/zip", filename=path.name)


def main() -> None:
    uvicorn.run("crawler.web:app", host="127.0.0.1", port=8002, reload=False)


if __name__ == "__main__":
    main()
