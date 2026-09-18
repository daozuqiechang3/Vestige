import threading
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
import httpx
from fastapi.testclient import TestClient

from crawler.acl import Paper, extract_paper
from crawler.web import (
    BrowserDiscoveryRequest,
    BrowserPaperSnapshot,
    BrowserPapersRequest,
    CrawlRequest,
    MANAGER,
    ContinueRequest,
    PaperUpdate,
    TaskManager,
    TaskRecord,
    app,
    continue_task,
)


def test_web_index_and_health() -> None:
    client = TestClient(app)

    index = client.get("/")
    health = client.get("/api/health")

    assert index.status_code == 200
    assert "教师信息采集" in index.text
    assert "失败教师" in index.text
    assert "全部重试失败项" in index.text
    assert "ACL / ACM 论文合集、分组或详情页 URL" in index.text
    assert "论文采集（ACL / ACM）" in index.text
    assert "if(mode()==='acl')renderResults(currentTask)" in index.text
    assert "acl-table" in index.text
    assert "中文标题 / English Title" in index.text
    assert "标题自动翻译失败，已保留英文" in index.text
    assert "自动翻译失败，请手动编辑" in index.text
    assert 'id="pending"' in index.text
    assert 'id="warnings"' in index.text
    assert 'id="pagination"' in index.text
    assert "/papers?${params}" in index.text
    assert "pageParams.get('task')" in index.text
    assert "currentTask.url===body.url" in index.text
    assert 'pattern="[1-9][0-9]*"' in index.text
    assert "replace(/\\D/g,'')" in index.text
    assert "t.status==='paused'?'继续采集':'开始采集'" in index.text
    assert "新采集上限必须大于当前成功数" in index.text
    assert "$('#url').value=t.url" in index.text
    assert index.headers["cache-control"] == "no-store"
    assert health.json() == {"status": "ok"}


def test_web_rejects_unknown_domains_and_invalid_limit() -> None:
    client = TestClient(app)

    unknown = client.post(
        "/api/tasks", json={"url": "https://outside.example.org/teachers", "limit": 2}
    )
    invalid_limit = client.post(
        "/api/tasks", json={"url": "https://cs.bit.edu.cn/szdw/jsml/index.htm", "limit": 0}
    )
    fractional_limit = client.post(
        "/api/tasks", json={"url": "https://cs.bit.edu.cn/szdw/jsml/index.htm", "limit": 1.5}
    )
    boolean_limit = client.post(
        "/api/tasks", json={"url": "https://cs.bit.edu.cn/szdw/jsml/index.htm", "limit": True}
    )

    assert unknown.status_code == 400
    assert "没有找到" in unknown.json()["detail"]
    assert invalid_limit.status_code == 422
    assert fractional_limit.status_code == 422
    assert boolean_limit.status_code == 422


def test_web_matches_school_config_without_starting_task() -> None:
    name, config = MANAGER._match_config("https://cs.bit.edu.cn/szdw/jsml/index.htm")

    assert name == "bit-cs"
    assert config.school == "北京理工大学"
    assert config.allowed_domains == ["cs.bit.edu.cn"]
    assert config.start_urls == ["https://cs.bit.edu.cn/szdw/jsml/index.htm"]


def test_web_creates_acm_session_task_with_normalized_url(
    tmp_path: Path, monkeypatch
) -> None:
    import crawler.web as web_module

    submissions: list[tuple[object, ...]] = []

    class FakeExecutor:
        def submit(self, *args: object) -> None:
            submissions.append(args)

    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    manager.executor = FakeExecutor()
    manager.tasks = {}
    monkeypatch.setattr(web_module, "TASKS_DIR", tmp_path)

    task = manager.create(
        web_module.CrawlRequest(
            url=(
                "https://dl.acm.org/doi/proceedings/10.5555/3776572#heading2"
            ),
            mode="acl",
            limit=1,
        )
    )

    assert task.url == (
        "https://dl.acm.org/doi/proceedings/10.5555/3776572?tocHeading=heading2"
    )
    assert task.school == "ACM Digital Library"
    assert task.output_dir.parent == tmp_path
    assert len(submissions) == 1


def test_web_reports_acm_403_as_site_verification_requirement(tmp_path: Path) -> None:
    request = httpx.Request(
        "GET",
        "https://dl.acm.org/doi/proceedings/10.5555/3776572?tocHeading=heading2",
    )
    response = httpx.Response(403, request=request)
    forbidden = httpx.HTTPStatusError(
        "forbidden",
        request=request,
        response=response,
    )
    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    task = TaskRecord(
        "acm-403-test",
        str(request.url),
        "acl",
        output_dir=tmp_path / "task",
    )
    manager.tasks = {task.id: task}
    manager._run_acl = lambda *_args: (_ for _ in ()).throw(forbidden)

    manager._run(task.id)

    assert task.status == "failed"
    assert "ACM Digital Library 拒绝后台直接访问（HTTP 403）" in (task.error or "")


def test_web_matches_buaa_ai_config_without_starting_task() -> None:
    name, config = MANAGER._match_config(
        "https://iai.buaa.edu.cn/szdw/ayjsdsjs.htm"
    )

    assert name == "buaa-ai"
    assert config.school == "北京航空航天大学"
    assert config.college == "人工智能学院"
    assert config.allowed_domains == ["iai.buaa.edu.cn"]


def test_web_matches_bnu_ai_config_without_starting_task() -> None:
    name, config = MANAGER._match_config(
        "https://ai.bnu.edu.cn/zszl/yjszs/pyds/sssds/a3ebbdaf77b6446e8e017a5fdbc32b58.htm"
    )

    assert name == "bnu-ai"
    assert config.school == "北京师范大学"
    assert config.college == "人工智能学院"
    assert config.allowed_domains == ["ai.bnu.edu.cn"]


def test_paper_update_does_not_reject_long_manual_abstract() -> None:
    abstract = "长摘要" * 50000

    assert PaperUpdate(abstract_zh=abstract).abstract_zh == abstract


def test_acl_limit_counts_successes_and_stops_immediately(
    tmp_path: Path, monkeypatch
) -> None:
    import crawler.web as web_module

    urls = [
        "https://aclanthology.org/2026.acl-long.1/",
        "https://aclanthology.org/2026.acl-long.2/",
        "https://aclanthology.org/2026.acl-long.3/",
    ]
    fetched: list[str] = []

    class FakeAclFetcher:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch(self, url: str, use_cache: bool = True):
            fetched.append(url)
            return SimpleNamespace(url=url, text=f"page:{url}")

    def extract(_html: str, url: str, log=None) -> Paper:
        if url == urls[0]:
            raise RuntimeError("invalid paper")
        return Paper("English title", "中文标题", "", "English abstract", "中文摘要", url)

    monkeypatch.setattr(web_module, "make_fetcher", lambda _output_dir: FakeAclFetcher())
    monkeypatch.setattr(web_module, "discover_papers", lambda _html, _url: urls)
    monkeypatch.setattr(web_module, "extract_paper", extract)

    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    task = TaskRecord(
        "limit-test",
        "https://aclanthology.org/volumes/2026.acl-long/",
        "acl",
        limit=1,
        output_dir=tmp_path / "task",
    )
    manager.tasks = {task.id: task}

    manager._run_acl(task.id, force=False)

    assert fetched == [task.url, urls[0], urls[1]]
    assert task.counts["processed"] == 1
    assert task.counts["failed"] == 1
    assert task.counts["pending"] == 1
    assert task.counts["skipped"] == 0
    assert task.status == "paused"
    assert any("已达到成功论文上限 1，任务暂停" in message for message in task.logs)
    assert task.paper_states[urls[2]] == "pending"
    snapshot = manager.snapshot(task.id)
    assert snapshot["csv_url"] == "/api/tasks/limit-test/papers.csv"
    assert snapshot["progress"] == 100
    assert snapshot["coverage_progress"] == pytest.approx(66.7)
    assert "papers" not in snapshot
    destination = manager.csv_path(task.id)
    assert destination.is_file()
    exported = destination.read_text(encoding="utf-8-sig")
    assert "RuntimeError: invalid paper" in exported
    assert ",pending," in exported

    pending = manager.paper_page(task.id, paper_status="pending")
    failures = manager.paper_page(task.id, paper_status="fail")
    assert pending["count"] == 1
    assert failures["count"] == 1
    assert failures["papers"][0]["error"] == "RuntimeError: invalid paper"


def test_paused_task_accepts_a_larger_limit(tmp_path: Path, monkeypatch) -> None:
    import crawler.web as web_module

    submissions: list[tuple[object, ...]] = []

    class FakeExecutor:
        def submit(self, *args: object) -> None:
            submissions.append(args)

    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    manager.executor = FakeExecutor()
    task = TaskRecord(
        "continue-test",
        "https://aclanthology.org/volumes/2026.acl-long/",
        "acl",
        limit=10,
        output_dir=tmp_path / "continue-task",
        status="paused",
    )
    task.counts["processed"] = 10
    manager.tasks = {task.id: task}
    monkeypatch.setattr(web_module, "MANAGER", manager)

    snapshot = continue_task(task.id, ContinueRequest(limit=50))

    assert task.limit == 50
    assert task.status == "queued"
    assert snapshot["limit"] == 50
    assert len(submissions) == 1


def test_acl_resume_uses_pending_state_without_accumulating_skips(
    tmp_path: Path, monkeypatch
) -> None:
    import crawler.web as web_module

    urls = [
        "https://aclanthology.org/2026.acl-long.1/",
        "https://aclanthology.org/2026.acl-long.2/",
        "https://aclanthology.org/2026.acl-long.3/",
    ]
    fetched: list[str] = []

    class FakeAclFetcher:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch(self, url: str, use_cache: bool = True):
            fetched.append(url)
            return SimpleNamespace(url=url, text=f"page:{url}")

    monkeypatch.setattr(web_module, "make_fetcher", lambda _output_dir: FakeAclFetcher())
    monkeypatch.setattr(
        web_module,
        "extract_paper",
        lambda _html, url, log=None: Paper("Title", "标题", "", "A", "中", url),
    )
    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    task = TaskRecord(
        "resume-test",
        "https://aclanthology.org/volumes/2026.acl-long/",
        "acl",
        limit=2,
        output_dir=tmp_path / "task",
        paper_urls=urls,
        paper_states={urls[0]: "success", urls[1]: "pending", urls[2]: "pending"},
        papers=[Paper("Existing", "已有", "", "A", "中", urls[0]).to_dict()],
    )
    task.counts["skipped"] = 60
    manager.tasks = {task.id: task}

    manager._run_acl(task.id, force=False)

    assert fetched == [urls[1]]
    assert task.counts["processed"] == 2
    assert task.counts["skipped"] == 0
    assert task.counts["pending"] == 1
    assert task.status == "paused"


def test_acl_failed_count_is_current_unique_failures(tmp_path: Path) -> None:
    urls = [
        "https://aclanthology.org/2026.acl-long.1/",
        "https://aclanthology.org/2026.acl-long.2/",
    ]
    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    task = TaskRecord(
        "counts-test",
        "https://aclanthology.org/volumes/2026.acl-long/",
        "acl",
        output_dir=tmp_path / "task",
        paper_urls=urls,
        paper_states={urls[0]: "fail", urls[1]: "success"},
        paper_errors={urls[0]: "old error"},
        papers=[Paper("Success", "成功", "", "A", "中", urls[1]).to_dict()],
    )
    task.counts.update({"processed": 99, "failed": 8, "skipped": 7})

    manager._refresh_acl_counts(task)

    assert task.counts["processed"] == 1
    assert task.counts["failed"] == 1
    assert task.counts["skipped"] == 0
    assert task.counts["pending"] == 0


def test_acl_retry_cannot_exceed_limit(tmp_path: Path) -> None:
    urls = [
        "https://aclanthology.org/2026.acl-long.1/",
        "https://aclanthology.org/2026.acl-long.2/",
    ]
    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    task = TaskRecord(
        "retry-limit-test",
        "https://aclanthology.org/volumes/2026.acl-long/",
        "acl",
        limit=1,
        status="paused",
        output_dir=tmp_path / "task",
        paper_urls=urls,
        paper_states={urls[0]: "success", urls[1]: "fail"},
        paper_errors={urls[1]: "failed"},
        papers=[Paper("Success", "成功", "", "A", "中", urls[0]).to_dict()],
    )
    manager.tasks = {task.id: task}
    manager._refresh_acl_counts(task)

    with pytest.raises(ValueError, match="超过成功论文上限"):
        manager.retry(task.id)


def test_acl_retry_keeps_pending_collection_paused(
    tmp_path: Path, monkeypatch
) -> None:
    import crawler.web as web_module

    urls = [
        "https://aclanthology.org/2026.acl-long.1/",
        "https://aclanthology.org/2026.acl-long.2/",
        "https://aclanthology.org/2026.acl-long.3/",
    ]

    class FakeAclFetcher:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch(self, url: str, use_cache: bool = True):
            return SimpleNamespace(url=url, text=f"page:{url}")

    monkeypatch.setattr(web_module, "make_fetcher", lambda _output_dir: FakeAclFetcher())
    monkeypatch.setattr(
        web_module,
        "extract_paper",
        lambda _html, url, log=None: Paper("Retried", "重试", "", "A", "中", url),
    )
    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    task = TaskRecord(
        "retry-state-test",
        "https://aclanthology.org/volumes/2026.acl-long/",
        "acl",
        limit=3,
        status="paused",
        output_dir=tmp_path / "task",
        paper_urls=urls,
        paper_states={urls[0]: "success", urls[1]: "fail", urls[2]: "pending"},
        paper_errors={urls[1]: "failed"},
        papers=[Paper("Success", "成功", "", "A", "中", urls[0]).to_dict()],
    )
    manager.tasks = {task.id: task}
    manager._refresh_acl_counts(task)

    manager._retry_acl(task.id, [urls[1]])

    assert task.counts["processed"] == 2
    assert task.counts["failed"] == 0
    assert task.counts["pending"] == 1
    assert task.status == "paused"
    assert manager._paper_raw_path(task, urls[1]).is_file()


def test_acl_task_load_relocates_output_and_reconciles_counts(
    tmp_path: Path, monkeypatch
) -> None:
    import crawler.web as web_module

    task_dir = tmp_path / "moved-task"
    task_dir.mkdir()
    url = "https://aclanthology.org/2026.acl-long.1/"
    paper = Paper("Existing", "已有", "", "A", "中", url).to_dict()
    task = TaskRecord(
        "moved-task",
        "https://aclanthology.org/volumes/2026.acl-long/",
        "acl",
        limit=1,
        status="paused",
        output_dir=Path("C:/old/location/moved-task"),
        paper_urls=[url.rstrip("/")],
        paper_states={url.rstrip("/"): "success"},
        papers=[paper],
    )
    task.counts.update({"processed": 9, "failed": 8, "skipped": 60})
    task.logs = [
        "[12:00:00] 英文摘要：This legacy entry is intentionally large.",
        "[12:00:01] 准备翻译摘要，文本长度：48",
        "[12:00:02] 采集成功：2026.acl-long.1",
    ]
    payload = asdict(task)
    payload["output_dir"] = "C:/old/location/moved-task"
    (task_dir / "task.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_module, "TASKS_DIR", tmp_path)

    manager = TaskManager()
    try:
        loaded = manager.get(task.id)
        assert loaded.output_dir == task_dir
        assert loaded.paper_urls == [url]
        assert loaded.counts["processed"] == 1
        assert loaded.counts["failed"] == 0
        assert loaded.counts["skipped"] == 0
        assert loaded.logs == [
            "[12:00:01] 准备翻译摘要，文本长度：48",
            "[12:00:02] 采集成功：2026.acl-long.1",
        ]
        saved = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        assert saved["output_dir"] == "."
        assert saved["papers"] == []
        assert manager._paper_record_path(loaded, url).is_file()
    finally:
        manager.close()


def test_acl_zip_contains_only_current_papers(tmp_path: Path) -> None:
    active_url = "https://aclanthology.org/2026.acl-long.1/"
    orphan_url = "https://aclanthology.org/2026.acl-long.999/"
    paper = Paper("Current Paper", "当前论文", "", "Abstract", "摘要", active_url)
    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    task = TaskRecord(
        "zip-test",
        "https://aclanthology.org/volumes/2026.acl-long/",
        "acl",
        status="paused",
        output_dir=tmp_path / "task",
        paper_urls=[active_url],
        paper_states={active_url: "success"},
        papers=[paper.to_dict()],
    )
    manager.tasks = {task.id: task}
    manager._refresh_acl_counts(task)
    manager._write_paper(task, task.papers[0])
    active_raw = manager._paper_raw_path(task, active_url)
    active_raw.parent.mkdir(parents=True, exist_ok=True)
    active_raw.write_text("current", encoding="utf-8")
    orphan_raw = manager._paper_raw_path(task, orphan_url)
    orphan_raw.write_text("orphan", encoding="utf-8")
    manager._paper_record_path(task, orphan_url).write_text("{}", encoding="utf-8")

    destination = manager.zip_path(task.id)

    with ZipFile(destination) as archive:
        names = set(archive.namelist())
    assert names == {
        "papers.csv",
        "papers/2026.acl-long.1.json",
        "html/2026.acl-long.1.html",
    }


def test_browser_snapshot_disables_destructive_force_reset(tmp_path: Path) -> None:
    url = "https://dl.acm.org/doi/10.65109/HQQZ1937"
    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    task = TaskRecord(
        "snapshot-test",
        "https://dl.acm.org/doi/proceedings/10.5555/3776572?tocHeading=heading2",
        "acl",
        status="completed",
        output_dir=tmp_path / "task",
        paper_urls=[url],
        paper_states={url: "success"},
        papers=[
            {
                **Paper("Paper", "论文", "", "A", "中", url).to_dict(),
                "parser_mode": "ACM 专用解析（浏览器快照）",
            }
        ],
    )
    manager.tasks = {task.id: task}
    manager._refresh_acl_counts(task)

    assert manager.snapshot(task.id)["force_reset_allowed"] is False


def test_acm_browser_discovery_replaces_incomplete_list_and_pauses_at_limit(
    tmp_path: Path,
) -> None:
    first = "https://dl.acm.org/doi/10.65109/HQQZ1937"
    second = "https://dl.acm.org/doi/10.65109/CZPZ7833"
    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    task = TaskRecord(
        "acm-browser-discovery",
        "https://dl.acm.org/doi/proceedings/10.5555/3776572?tocHeading=heading2",
        "acl",
        limit=1,
        status="completed",
        output_dir=tmp_path / "task",
        paper_urls=[first],
        paper_states={first: "success"},
        papers=[
            {
                **Paper("Paper", "论文", "", "A", "中", first).to_dict(),
                "parser_mode": "ACM 专用解析（浏览器快照）",
            }
        ],
    )
    manager.tasks = {task.id: task}

    manager.import_acm_discovery(
        task.id,
        BrowserDiscoveryRequest(
            conference="AAMAS",
            session="Research Paper Track",
            paper_urls=[first, second, second + "?duplicate=true"],
        ),
    )

    assert task.school == "AAMAS"
    assert task.college == "Research Paper Track"
    assert task.paper_urls == [first, second]
    assert task.paper_states == {first: "success", second: "pending"}
    assert task.counts["discovered"] == 2
    assert task.counts["processed"] == 1
    assert task.counts["pending"] == 1
    assert task.status == "paused"
    assert any("浏览器已确认 AAMAS / Research Paper Track" in log for log in task.logs)


def test_acm_browser_papers_import_full_metadata_and_preserve_existing_translation(
    tmp_path: Path,
) -> None:
    first = "https://dl.acm.org/doi/10.65109/HQQZ1937"
    second = "https://dl.acm.org/doi/10.65109/CZPZ7833"
    existing = {
        **Paper("First", "第一篇", "", "First abstract", "第一篇摘要", first).to_dict(),
        "parser_mode": "ACM 专用解析（浏览器快照）",
    }
    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    task = TaskRecord(
        "acm-browser-papers",
        "https://dl.acm.org/doi/proceedings/10.5555/3776572?tocHeading=heading2",
        "acl",
        limit=2,
        status="paused",
        output_dir=tmp_path / "task",
        paper_urls=[first, second],
        paper_states={first: "success", second: "pending"},
        papers=[existing],
    )
    manager.tasks = {task.id: task}

    manager.import_acm_papers(
        task.id,
        BrowserPapersRequest(
            papers=[
                BrowserPaperSnapshot(
                    url=first,
                    title="First updated",
                    authors=["One Author"],
                    abstract_en="First updated abstract",
                    pdf_url="https://dl.acm.org/doi/pdf/10.65109/HQQZ1937",
                ),
                BrowserPaperSnapshot(
                    url=second,
                    title="Second",
                    authors=["Two Author"],
                    abstract_en="Second abstract",
                    pdf_url="https://dl.acm.org/doi/pdf/10.65109/CZPZ7833",
                ),
            ],
            complete=True,
        ),
    )

    assert task.status == "completed"
    assert task.counts["processed"] == 2
    assert task.counts["pending"] == 0
    by_url = {paper["url"]: paper for paper in task.papers}
    assert by_url[first]["title"] == "First updated"
    assert by_url[first]["title_zh"] == "第一篇"
    assert by_url[first]["abstract_zh"] == "第一篇摘要"
    assert by_url[second]["authors"] == ["Two Author"]
    assert by_url[second]["translation_failed"] is True
    assert by_url[second]["title_translation_failed"] is True
    assert manager._paper_record_path(task, second).is_file()


def test_acm_create_reuses_existing_session_result(tmp_path: Path) -> None:
    manager = TaskManager.__new__(TaskManager)
    manager.lock = threading.RLock()
    task = TaskRecord(
        "existing-acm",
        "https://dl.acm.org/doi/proceedings/10.5555/3776572?tocHeading=heading2",
        "acl",
        school="AAMAS",
        college="Research Paper Track",
        limit=338,
        status="completed",
        output_dir=tmp_path / "existing",
        paper_urls=["https://dl.acm.org/doi/10.65109/HQQZ1937"],
        paper_states={"https://dl.acm.org/doi/10.65109/HQQZ1937": "success"},
        papers=[Paper("Paper", "论文", "", "A", "中", "https://dl.acm.org/doi/10.65109/HQQZ1937").to_dict()],
    )
    manager.tasks = {task.id: task}
    manager._refresh_acl_counts(task)

    created = manager.create(
        CrawlRequest(
            url="https://dl.acm.org/doi/proceedings/10.5555/3776572#heading2",
            mode="acl",
        )
    )

    assert created is task
    assert created.id == "existing-acm"
    assert any("复用已有采集结果" in log for log in created.logs)
