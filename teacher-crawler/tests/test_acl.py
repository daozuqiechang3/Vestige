from pathlib import Path

import httpx
import pytest

from crawler.acl import (
    TRANSLATE_CHUNK_SIZE,
    discover_papers,
    extract_paper,
    is_paper_url,
    translate_abstract,
    translate_text,
    validate_volume_url,
    write_csv,
)


def test_acl_extracts_abstract_after_label_from_acl_card(monkeypatch) -> None:
    monkeypatch.setattr("crawler.acl.translate_text", lambda text: f"翻译:{text}")
    monkeypatch.setattr("crawler.acl.translate_abstract", lambda text: f"翻译:{text}")
    html = """
    <html>
      <h1 id="title">OctoTools</h1>
      <div class="card-body acl-abstract">
        <strong>Abstract</strong>
        <p>Solving complex reasoning tasks may involve
        visual understanding, domain knowledge retrieval, and numerical calculation.</p>
      </div>
    </html>
    """

    paper = extract_paper(html, "https://aclanthology.org/2026.acl-long.1/")

    assert paper.abstract_en == (
        "Solving complex reasoning tasks may involve visual understanding, "
        "domain knowledge retrieval, and numerical calculation."
    )
    assert paper.abstract_en != "无摘要"
    assert paper.abstract_zh.startswith("翻译:")
    assert paper.title_zh == "翻译:OctoTools"
    assert not paper.translation_failed


def test_acl_extracts_abstract_from_current_h5_card_markup(monkeypatch) -> None:
    monkeypatch.setattr("crawler.acl.translate_text", lambda text: f"翻译:{text}")
    monkeypatch.setattr("crawler.acl.translate_abstract", lambda text: f"翻译:{text}")
    html = """
    <html>
      <h2 id="title"><a href="/2026.acl-long.1.pdf"><span>O</span>cto<span>T</span>ools</a></h2>
      <div class="card-body acl-abstract">
        <h5 class="card-title">Abstract</h5>
        <span>Solving complex reasoning tasks may involve visual understanding.</span>
      </div>
    </html>
    """

    paper = extract_paper(html, "https://aclanthology.org/2026.acl-long.1/")

    assert paper.title == "OctoTools"
    assert paper.abstract_en == (
        "Solving complex reasoning tasks may involve visual understanding."
    )
    assert paper.pdf_url == "https://aclanthology.org/2026.acl-long.1.pdf"


def test_acl_accepts_volume_and_individual_paper_urls() -> None:
    volume_url = "https://aclanthology.org/volumes/2026.acl-long/"
    paper_url = "https://aclanthology.org/2026.acl-long.1/"

    assert validate_volume_url(volume_url) == volume_url
    assert validate_volume_url(paper_url) == paper_url
    assert validate_volume_url(paper_url.rstrip("/")) == paper_url.rstrip("/")
    assert is_paper_url(paper_url)
    assert not is_paper_url(volume_url)


def test_acl_volume_discovery_excludes_front_matter_entry() -> None:
    html = """
    <a href="/2026.acl-long.0/">Volume front matter</a>
    <a href="/2026.acl-long.1/">First paper</a>
    <a href="/2026.acl-long.2/">Second paper</a>
    """

    assert discover_papers(
        html, "https://aclanthology.org/volumes/2026.acl-long/"
    ) == [
        "https://aclanthology.org/2026.acl-long.1/",
        "https://aclanthology.org/2026.acl-long.2/",
    ]


def test_acl_rejects_unrelated_anthology_pages() -> None:
    with pytest.raises(ValueError, match="文集卷页面或单篇论文页面"):
        validate_volume_url("https://aclanthology.org/people/pan-lu/")


def test_acl_uses_no_abstract_only_when_missing(monkeypatch) -> None:
    monkeypatch.setattr("crawler.acl.translate_text", lambda text: f"翻译:{text}")

    paper = extract_paper(
        '<h1 id="title">Paper</h1>',
        "https://aclanthology.org/2026.acl-long.2/",
    )

    assert paper.abstract_en == "无摘要"
    assert paper.abstract_zh == "无摘要"


def test_acl_passes_complete_abstract_to_translation_without_extra_changes(
    monkeypatch,
) -> None:
    captured: list[str] = []
    abstract = "First sentence with <markers> & punctuation! Second sentence."
    monkeypatch.setattr(
        "crawler.acl.translate_abstract",
        lambda value: captured.append(value) or "完整译文",
    )
    monkeypatch.setattr("crawler.acl.translate_text", lambda value: f"标题译文:{value}")

    paper = extract_paper(
        f'<h2 id="title">Paper</h2><div class="acl-abstract">'
        f"<h5>Abstract</h5><span>{abstract}</span></div>",
        "https://aclanthology.org/2026.acl-long.1/",
    )

    assert captured == [paper.abstract_en]
    assert paper.abstract_zh == "完整译文"


def test_acl_translation_failure_reuses_english_and_logs_reason(monkeypatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr("crawler.acl.translate_text", lambda _text: "论文标题")
    monkeypatch.setattr(
        "crawler.acl.translate_abstract",
        lambda _text: (_ for _ in ()).throw(httpx.ReadTimeout("timed out")),
    )

    paper = extract_paper(
        '<h2 id="title">Paper</h2><div class="acl-abstract">'
        "<h5>Abstract</h5><span>Complete English abstract.</span></div>",
        "https://aclanthology.org/2026.acl-long.1/",
        log=logs.append,
    )

    assert paper.abstract_zh == paper.abstract_en
    assert paper.translation_failed
    assert "ReadTimeout: timed out" in paper.translation_error
    assert logs[0] == "准备翻译标题，文本长度：5"
    assert logs[1] == "准备翻译摘要，文本长度：26"
    assert logs[2] == "英文摘要：Complete English abstract."
    assert logs[3].endswith("执行降级，中文摘要复用英文原文")


def test_title_translation_failure_does_not_block_abstract(monkeypatch) -> None:
    def translate(value: str) -> str:
        if value == "Paper title":
            raise httpx.ReadTimeout("title timed out")
        return f"摘要译文:{value}"

    monkeypatch.setattr("crawler.acl.translate_text", translate)
    monkeypatch.setattr("crawler.acl.translate_abstract", translate)
    paper = extract_paper(
        '<h2 id="title">Paper title</h2><div class="acl-abstract">'
        "<h5>Abstract</h5><span>English abstract.</span></div>",
        "https://aclanthology.org/2026.acl-long.1/",
    )

    assert paper.title_zh == paper.title
    assert paper.title_translation_failed
    assert paper.abstract_zh == "摘要译文:English abstract."
    assert not paper.translation_failed


def test_long_abstract_translation_sends_every_character(monkeypatch) -> None:
    source = "A sentence with punctuation. " * 80
    calls: list[list[str]] = []

    class Translator:
        def translate_many(self, values: list[str]) -> list[str]:
            calls.append(values)
            return ["译文" for _ in values]

    monkeypatch.setattr("crawler.acl.get_nllb_translator", lambda: Translator())

    translated = translate_abstract(source)

    assert len(calls) == 1
    assert "".join(calls[0]) == source
    assert all(0 < len(chunk) <= TRANSLATE_CHUNK_SIZE for chunk in calls[0])
    assert translated == "译文" * len(calls[0])


def test_translate_text_uses_local_nllb(monkeypatch) -> None:
    monkeypatch.delenv("ACL_TRANSLATE_URL", raising=False)
    monkeypatch.delenv("ACL_ALLOW_ONLINE_TRANSLATION", raising=False)

    class Translator:
        def translate_many(self, values: list[str]) -> list[str]:
            return [f"本地:{value}" for value in values]

    monkeypatch.setattr("crawler.acl.get_nllb_translator", lambda: Translator())

    assert translate_text("English title") == "本地:English title"


def test_translate_text_does_not_use_online_fallback_by_default(monkeypatch) -> None:
    monkeypatch.delenv("ACL_TRANSLATE_URL", raising=False)
    monkeypatch.delenv("ACL_ALLOW_ONLINE_TRANSLATION", raising=False)
    monkeypatch.setattr(
        "crawler.acl.get_nllb_translator",
        lambda: type(
            "BrokenTranslator",
            (),
            {"translate_many": lambda self, values: (_ for _ in ()).throw(RuntimeError("local failed"))},
        )(),
    )
    online_called = False

    def online(_text: str) -> str:
        nonlocal online_called
        online_called = True
        return "不应调用"

    monkeypatch.setattr("crawler.acl._translate_with_bing", online)

    with pytest.raises(RuntimeError, match="本地 NLLB.*local failed"):
        translate_text("English abstract")
    assert not online_called


def test_translate_text_uses_backup_when_primary_fails(monkeypatch) -> None:
    monkeypatch.delenv("ACL_TRANSLATE_URL", raising=False)
    monkeypatch.setenv("ACL_ALLOW_ONLINE_TRANSLATION", "1")
    monkeypatch.setattr(
        "crawler.acl.get_nllb_translator",
        lambda: type(
            "BrokenTranslator",
            (),
            {"translate_many": lambda self, values: (_ for _ in ()).throw(RuntimeError("local failed"))},
        )(),
    )
    monkeypatch.setattr(
        "crawler.acl._translate_with_bing",
        lambda _text: (_ for _ in ()).throw(httpx.ReadTimeout("primary timeout")),
    )
    monkeypatch.setattr(
        "crawler.acl._translate_with_mymemory", lambda _text: "备用中文译文"
    )

    assert translate_text("English abstract") == "备用中文译文"


def test_csv_keeps_complete_fallback_and_manual_abstracts(tmp_path: Path) -> None:
    english = "Long English abstract. " * 1000
    manual_zh = "人工修改的中文摘要。" * 1000
    papers = [
        {
            "title": "Fallback",
            "title_zh": "",
            "pdf_url": "fallback.pdf",
            "abstract_en": english,
            "abstract_zh": english,
            "status": "success",
        },
        {
            "title": "Manual",
            "title_zh": "",
            "pdf_url": "manual.pdf",
            "abstract_en": english,
            "abstract_zh": manual_zh,
            "status": "success",
        },
    ]

    destination = write_csv(tmp_path / "papers.csv", papers)
    exported = destination.read_text(encoding="utf-8-sig")

    assert english in exported
    assert manual_zh in exported
