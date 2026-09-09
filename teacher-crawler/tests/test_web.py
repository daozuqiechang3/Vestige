import threading
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from crawler.acl import Paper, extract_paper
from crawler.web import (
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
    assert "ACL文集或论文页面 URL" in index.text
    assert "if(mode()==='acl')renderResults(currentTask)" in index.text
    assert "acl-table" in index.text
    assert "中文标题 / English Title" in index.text
    assert "标题自动翻译失败，已保留英文" in index.text
    assert "⚠自动翻译失败，请手动编辑" in index.text
    assert "aclPaperEdits:" in index.text
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
    assert task.status == "paused"
    assert any("已达到采集上限1，任务暂停" in message for message in task.logs)
    assert urls[2] not in task.paper_states
    snapshot = manager.snapshot(task.id)
    assert snapshot["csv_url"] == "/api/tasks/limit-test/papers.csv"
    assert manager.csv_path(task.id).is_file()


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
