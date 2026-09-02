from pathlib import Path

import pytest

from crawler.config import load_config, load_school_config


def test_load_config_returns_dict_and_resolves_output(tmp_path: Path) -> None:
    path = tmp_path / "school.yaml"
    path.write_text(
        """
school: Test University
college: School of Engineering
base_url: https://faculty.example.edu/
start_urls: [https://faculty.example.edu/people]
allowed_domains: [faculty.example.edu]
output_dir: results
""".strip(),
        encoding="utf-8",
    )

    config = load_config(path)

    assert isinstance(config, dict)
    assert config["base_url"] == "https://faculty.example.edu"
    assert config["college"] == "School of Engineering"
    assert config["allowed_domains"] == ["faculty.example.edu"]
    assert config["output_dir"] == tmp_path / "results"


@pytest.mark.parametrize("missing_field", ["start_urls", "allowed_domains"])
def test_load_config_rejects_missing_required_lists(tmp_path: Path, missing_field: str) -> None:
    values = {
        "school": "Test University",
        "college": "School of Engineering",
        "start_urls": ["https://faculty.example.edu/people"],
        "allowed_domains": ["faculty.example.edu"],
    }
    values.pop(missing_field)
    path = tmp_path / "invalid.yaml"
    path.write_text("\n".join(f"{key}: {value}" for key, value in values.items()), encoding="utf-8")

    with pytest.raises(ValueError, match=missing_field):
        load_config(path)


def test_load_bit_cs_config() -> None:
    path = Path(__file__).parents[1] / "configs" / "bit-cs.yaml"

    raw = load_config(path)
    config = load_school_config(path)

    assert isinstance(raw, dict)
    assert config.school == "北京理工大学"
    assert config.college == "计算机学院"
    assert config.base_url == "https://cs.bit.edu.cn"
    assert config.start_urls == ["https://cs.bit.edu.cn/szdw/jsml/index.htm"]
    assert config.discovery.link_selectors == [".sub_033a a.item"]
    assert config.discovery.include_patterns == ["/szdw/jsml/bssds/", "/szdw/jsml/sssds/"]
    assert config.profile is not None
    assert config.profile.category_from_url["bssds"] == "博士生导师"
    assert config.request.use_browser is False
    assert config.output_dir == path.parents[1] / "output"
