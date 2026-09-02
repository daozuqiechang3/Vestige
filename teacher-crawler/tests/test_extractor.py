from crawler.config import SchoolConfig
from crawler.extractor import extract_teacher


def test_extract_teacher_fields_and_absolute_photo() -> None:
    config = SchoolConfig.model_validate(
        {
            "school": "Test University",
            "college": "School of Engineering",
            "base_url": "https://faculty.example.edu",
            "directory_urls": ["/people"],
            "selectors": {
                "name": ["h1"],
                "category": [".category"],
                "title": [".position"],
                "email": ["a[href^='mailto:']"],
                "phone": [".phone"],
                "department": [".department"],
                "research": [".research li"],
                "admission_info": [".admission"],
                "sections": {"education": [".education"]},
                "body": ["main"],
                "photo": [".portrait"],
            },
        }
    )
    html = """
    <main>
      <h1> Alice Zhang </h1><p class="category">Doctoral supervisor</p>
      <p class="position"> Associate Professor </p>
      <a href="mailto:alice@example.edu?subject=Hello">Contact</a>
      <p class="phone">010-12345678</p><p class="department">Computer Science</p>
      <ul class="research"><li>Machine Learning</li><li>Robotics</li></ul>
      <p class="admission">Accepting graduate students.</p>
      <section class="education">PhD, Example University</section>
      <img class="portrait" src="../images/alice.jpg">
      <p>Biography text.</p>
    </main>
    """

    record = extract_teacher(
        html,
        "https://faculty.example.edu/faculty/alice.html",
        config,
    )

    assert record.name == "Alice Zhang"
    assert record.school == "Test University"
    assert record.college == "School of Engineering"
    assert record.category == "Doctoral supervisor"
    assert record.title == "Associate Professor"
    assert record.email == "alice@example.edu"
    assert record.phone == "010-12345678"
    assert record.department == "Computer Science"
    assert record.research_interests == ["Machine Learning", "Robotics"]
    assert record.admission_info == "Accepting graduate students."
    assert record.sections == {"education": "PhD, Example University"}
    assert record.photo_url == "https://faculty.example.edu/images/alice.jpg"
    assert record.profile_url == "https://faculty.example.edu/faculty/alice.html"
    assert "Biography text." in record.full_text


def test_extract_profile_config_and_category_from_url() -> None:
    config = SchoolConfig.model_validate(
        {
            "school": "北京理工大学",
            "college": "计算机学院",
            "start_urls": ["https://cs.bit.edu.cn/szdw/jsml/index.htm"],
            "profile": {
                "container": ".sub_034",
                "name": ".left .title",
                "category_from_url": {"bssds": "博士生导师", "sssds": "硕士生导师"},
                "department": ".left .vicetitle",
                "summary": ".left .summary",
                "content": ".right.article",
                "photo": ".left .img_box img",
            },
        }
    )
    html = """
    <div class="sub_034">
      <div class="left">
        <h1 class="title">张老师</h1>
        <p class="vicetitle">计算机科学与技术系</p>
        <p class="summary">职称：副教授 联系电话：010-68910000 E-mail：zhang@bit.edu.cn</p>
        <div class="img_box"><img src="../../images/zhang.jpg"></div>
      </div>
      <div class="right article">
        <h2>个人信息</h2><p>个人简介</p>
        <h2>研究方向</h2><p>人工智能、机器学习</p>
        <h3>招生信息</h3><p>每年计划招收2名硕士生。</p>
      </div>
    </div>
    """

    teacher = extract_teacher(
        html,
        "https://cs.bit.edu.cn/szdw/jsml/bssds/zhang.htm",
        config,
    )

    assert teacher.name == "张老师"
    assert teacher.category == "博士生导师"
    assert teacher.title == "副教授"
    assert teacher.email == "zhang@bit.edu.cn"
    assert teacher.phone == "010-68910000"
    assert teacher.department == "计算机科学与技术系"
    assert teacher.sections["summary"].startswith("职称：副教授")
    assert teacher.sections["个人信息"] == "个人简介"
    assert teacher.sections["研究方向"] == "人工智能、机器学习"
    assert teacher.sections["招生信息"] == "每年计划招收2名硕士生。"
    assert teacher.research_interests == ["人工智能", "机器学习"]
    assert teacher.admission_info == "每年计划招收2名硕士生。"
    assert "个人信息\n个人简介" in teacher.full_text
    assert teacher.photo_url == "https://cs.bit.edu.cn/szdw/images/zhang.jpg"


def test_extract_labeled_buaa_profile_fields() -> None:
    config = SchoolConfig.model_validate(
        {
            "school": "北京航空航天大学",
            "college": "人工智能学院",
            "start_urls": ["https://iai.buaa.edu.cn/szdw/ayjsdsjs.htm"],
            "profile": {
                "container": ".listpeople",
                "name": ".titbox p:first-child",
                "summary": ".titbox",
                "content": "#vsb_content_2",
                "photo": ".titbox img",
            },
        }
    )
    html = """
    <div class="listpeople">
      <div class="titbox">
        <img src="/__local/teacher.jpg">
        <div>
          <p>姓　　名：郑志明</p>
          <p>现有职称：教授</p>
          <p>硕博导师：博士生导师</p>
          <p>人才称号：两院院士</p>
        </div>
      </div>
      <div id="vsb_content_2">
        <p>李波，教授，博士生导师，国家级人才。</p>
        <p><strong>★科研概况</strong></p>
        <p>1.研究方向：计算机视觉，机器学习，知识推理，嵌入式智能系统，人工智能应用。</p>
        <p>2.科研成果：发表论文140余篇。</p>
        <p><strong>★教育教学</strong></p>
        <p>1.教育背景：获博士学位。</p>
        <p><strong>★联系方式</strong></p>
        <p>邮箱：boli@buaa.edu.cn</p>
      </div>
    </div>
    """

    teacher = extract_teacher(
        html,
        "https://iai.buaa.edu.cn/info/1013/1088.htm",
        config,
    )

    assert teacher.name == "郑志明"
    assert teacher.title == "教授"
    assert teacher.category == "博士生导师"
    assert teacher.research_interests == [
        "计算机视觉",
        "机器学习",
        "知识推理",
        "嵌入式智能系统",
        "人工智能应用",
    ]
    assert teacher.sections["科研概况"].startswith("1.研究方向：计算机视觉")
    assert teacher.sections["教育教学"] == "1.教育背景：获博士学位。"
    assert teacher.sections["联系方式"] == "邮箱：boli@buaa.edu.cn"
    assert teacher.email == "boli@buaa.edu.cn"
    assert teacher.photo_url == "https://iai.buaa.edu.cn/__local/teacher.jpg"


def test_extract_bnu_profile_with_h4_sections() -> None:
    config = SchoolConfig.model_validate(
        {
            "school": "北京师范大学",
            "college": "人工智能学院",
            "start_urls": [
                "https://ai.bnu.edu.cn/zszl/yjszs/pyds/sssds/list.htm"
            ],
            "profile": {
                "container": ".section-right",
                "name": ".subPageTit h3",
                "summary": ".tj-intro",
                "content": ".tj-intro",
                "photo": ".tj-intro img",
            },
        }
    )
    html = """
    <div class="section-right">
      <div class="tj-intro">
        <div class="subPageTit"><h3>白　 璐</h3></div>
        <p><img src="../../../images/bailu.jpeg"></p>
        <h4>基本信息</h4>
        <ul>
          <li>职称：教授（硕士生导师，博士生导师）</li>
          <li>研究方向：结构模式识别、图机器学习、量子随机游走、金融人工智能、智能教育</li>
          <li>电子邮箱：bailu@bnu.edu.cn</li>
        </ul>
        <h4>个人简介</h4><p>白璐，教授，博士生导师。</p>
        <h4>招生说明</h4><p>欢迎对机器学习感兴趣的同学与我联系。</p>
      </div>
    </div>
    """

    teacher = extract_teacher(
        html,
        "https://ai.bnu.edu.cn/xygk/szdw/zgj/bailu.htm",
        config,
    )

    assert teacher.name == "白璐"
    assert teacher.title == "教授"
    assert teacher.category == "硕士生导师、博士生导师"
    assert teacher.email == "bailu@bnu.edu.cn"
    assert teacher.research_interests == [
        "结构模式识别",
        "图机器学习",
        "量子随机游走",
        "金融人工智能",
        "智能教育",
    ]
    assert teacher.sections["个人简介"] == "白璐，教授，博士生导师。"
    assert teacher.admission_info == "欢迎对机器学习感兴趣的同学与我联系。"
    assert teacher.photo_url == "https://ai.bnu.edu.cn/images/bailu.jpeg"


def test_trafilatura_is_used_when_content_selector_fails(monkeypatch: object) -> None:
    config = SchoolConfig.model_validate(
        {
            "school": "Test University",
            "college": "Computer Science",
            "start_urls": ["https://example.edu/people"],
            "profile": {
                "container": "main",
                "name": "h1",
                "content": ".missing-content",
            },
        }
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "crawler.extractor.trafilatura.extract", lambda *_args, **_kwargs: "Fallback body"
    )

    teacher = extract_teacher(
        "<main><h1>Alice Zhang</h1><p>Page shell</p></main>",
        "https://example.edu/people/alice",
        config,
    )

    assert teacher.full_text == "Fallback body"
