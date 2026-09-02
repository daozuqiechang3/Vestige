from fastapi.testclient import TestClient

from crawler.web import MANAGER, app


def test_web_index_and_health() -> None:
    client = TestClient(app)

    index = client.get("/")
    health = client.get("/api/health")

    assert index.status_code == 200
    assert "教师信息采集" in index.text
    assert "失败教师" in index.text
    assert "全部重试失败项" in index.text
    assert health.json() == {"status": "ok"}


def test_web_rejects_unknown_domains_and_invalid_limit() -> None:
    client = TestClient(app)

    unknown = client.post(
        "/api/tasks", json={"url": "https://outside.example.org/teachers", "limit": 2}
    )
    invalid_limit = client.post(
        "/api/tasks", json={"url": "https://cs.bit.edu.cn/szdw/jsml/index.htm", "limit": 0}
    )

    assert unknown.status_code == 400
    assert "没有找到" in unknown.json()["detail"]
    assert invalid_limit.status_code == 422


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
