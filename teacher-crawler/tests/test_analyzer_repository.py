import csv
from pathlib import Path

from crawler.analyzer import analyze_teacher
from crawler.models import Teacher
from crawler.repository import CSV_FIELDS, TeacherRepository


def test_analyzer_extracts_recruitment_signals_and_context(tmp_path: Path) -> None:
    html_path = tmp_path / "teacher.html"
    html_path.write_text(
        """
        <html><body><h1>张老师</h1>
        <h2>个人简介</h2><p>长期从事人工智能系统研究，主持多项科研项目。</p>
        <h2>招生信息</h2>
        <p>长期招收校内外实习生，欢迎投递简历。</p>
        <p>每年计划招收博士生1名和硕士生2名。</p>
        <p>所属智能系统实验室：智能计算团队。</p>
        </body></html>
        """,
        encoding="utf-8",
    )
    teacher = Teacher(
        school="Test University",
        college="Computer Science",
        name="张老师",
        title="副教授",
        category="博士生导师",
        email="zhang@example.edu",
        research_interests=["人工智能", "智能系统"],
        sections={"个人简介": "长期从事人工智能系统研究，主持多项科研项目。"},
        full_text="长期招收校内外实习生，欢迎投递简历。\n每年计划招收博士生1名。",
        profile_url="https://example.edu/zhang",
    )

    result = analyze_teacher(teacher, html_path)

    assert result.title == "副教授 / 博士生导师"
    assert result.research_dir == "人工智能 | 智能系统"
    assert result.has_recruit_info is True
    assert result.recruit_types == ["contact", "graduate", "internship"]
    assert "长期招收校内外实习生，欢迎投递简历。" in result.recruit_text
    assert "计划招收博士生" in result.recruit_text
    assert result.recruit_score > 0
    assert result.bio.startswith("长期从事人工智能")
    assert result.lab == "智能系统实验室 / 智能计算团队"


def test_analyzer_does_not_treat_laboratory_director_as_lab(tmp_path: Path) -> None:
    html_path = tmp_path / "teacher.html"
    html_path.write_text("<p>现任数字媒体北京市重点实验室主任，是项目负责人。</p>", encoding="utf-8")
    teacher = Teacher(
        school="北京航空航天大学",
        college="人工智能学院",
        name="李波",
        title="教授",
        category="博士生导师",
        research_interests=["计算机视觉", "机器学习"],
        full_text="李波，教授，博士生导师，国家级人才。\n现任数字媒体北京市重点实验室主任。",
        profile_url="https://iai.buaa.edu.cn/info/1013/1089.htm",
    )

    result = analyze_teacher(teacher, html_path)

    assert result.research_dir == "计算机视觉 | 机器学习"
    assert result.lab == ""
    assert result.bio == "李波，教授，博士生导师，国家级人才。"


def test_analyzer_recognizes_natural_contact_invitation(tmp_path: Path) -> None:
    html_path = tmp_path / "teacher.html"
    invitation = "欢迎对模式识别和机器学习感兴趣的同学与我联系。"
    html_path.write_text(f"<h4>招生说明</h4><p>{invitation}</p>", encoding="utf-8")
    teacher = Teacher(
        school="北京师范大学",
        college="人工智能学院",
        name="白璐",
        sections={"招生说明": invitation},
        admission_info=invitation,
        full_text=invitation,
        profile_url="https://ai.bnu.edu.cn/xygk/szdw/zgj/bailu.htm",
    )

    result = analyze_teacher(teacher, html_path)

    assert result.has_recruit_info is True
    assert result.recruit_types == ["contact"]
    assert invitation in result.recruit_text


def test_repository_preserves_manual_fields_and_exports_core_csv(tmp_path: Path) -> None:
    html_path = tmp_path / "teacher.html"
    html_path.write_text("<p>欢迎报考，欢迎邮件联系。</p>", encoding="utf-8")
    teacher = Teacher(
        school="Test",
        college="CS",
        name="Alice",
        profile_url="https://example.edu/alice",
    )
    repository = TeacherRepository(tmp_path / "teacher_data.db")
    first = analyze_teacher(teacher, html_path)
    repository.upsert(first)
    row = repository.list()[0]
    repository.update_manual(row["id"], "方向匹配，准备发邮件", "待联系", True)

    changed = first.model_copy(update={"bio": "Updated biography"})
    repository.upsert(changed)
    saved = repository.list()[0]

    assert saved["note"] == "方向匹配，准备发邮件"
    assert saved["contact_status"] == "待联系"
    assert saved["favorite"] is True
    assert saved["bio"] == "Updated biography"

    csv_path = repository.export_csv(tmp_path / "teachers.csv")
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
    assert reader.fieldnames == CSV_FIELDS
    assert rows[0]["name"] == "Alice"
    assert rows[0]["has_recruit_info"] == "True"
    assert rows[0]["note"] == "方向匹配，准备发邮件"
