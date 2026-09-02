from __future__ import annotations

import re
from urllib.parse import urljoin

import trafilatura
from bs4 import BeautifulSoup, NavigableString, Tag

from .config import SchoolConfig
from .models import Teacher

EMAIL_PATTERN = r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"
EMAIL_RE = re.compile(EMAIL_PATTERN, re.IGNORECASE)
PHONE_RE = re.compile(
    r"(?:联系电话|办公电话|电话|手机)\s*[:：]\s*([+\d][\d\s()（）-]{5,}\d)",
    re.IGNORECASE,
)
FIELD_LABELS = (
    r"姓名|职称|职务|硕博导师|导师类别|人才称号|研究方向|研究领域|科研方向|"
    r"联系电话|办公电话|电话|手机|邮箱|电子邮箱|E-?mail|通信地址|办公地点"
)
RESEARCH_TITLES = ("研究方向", "研究领域", "科研方向", "research interests", "research areas")
ADMISSION_KEYWORDS = ("招生", "欢迎报考", "计划招收")


def _clean(text: str) -> str:
    return " ".join(text.split())


SearchRoot = BeautifulSoup | Tag


def _first_text(soup: SearchRoot, selectors: list[str]) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = _clean(node.get_text(" ", strip=True))
            if text:
                return text
    return None


def _all_text(soup: SearchRoot, selectors: list[str]) -> list[str]:
    for selector in selectors:
        values = [_clean(node.get_text(" ", strip=True)) for node in soup.select(selector)]
        values = [value for value in values if value]
        if values:
            if len(values) == 1:
                parts = re.split(r"[;；|、]", values[0])
                return [part.strip() for part in parts if part.strip()]
            return list(dict.fromkeys(values))
    return []


def _extract_email(soup: SearchRoot, selectors: list[str]) -> str | None:
    for selector in selectors:
        for node in soup.select(selector):
            if isinstance(node, Tag):
                href = node.get("href")
                if isinstance(href, str) and href.lower().startswith("mailto:"):
                    candidate = href[7:].split("?", 1)[0].strip()
                    if EMAIL_RE.fullmatch(candidate):
                        return candidate
            match = EMAIL_RE.search(node.get_text(" ", strip=True))
            if match:
                return match.group(0)
    match = EMAIL_RE.search(soup.get_text(" ", strip=True))
    return match.group(0) if match else None


def _extract_content(
    html: str, soup: SearchRoot, selectors: list[str]
) -> tuple[str, Tag | None]:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = node.get_text("\n", strip=True)
            if text:
                cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())
                return cleaned, node
    fallback = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
    return fallback.strip(), None


def _extract_photo(soup: SearchRoot, selectors: list[str], page_url: str) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        source = node.get("src") or node.get("data-src")
        if isinstance(source, str) and source.strip():
            return urljoin(page_url, source.strip())
    return None


def _extract_sections(soup: SearchRoot, selectors: dict[str, list[str]]) -> dict[str, str]:
    sections: dict[str, str] = {}
    for label, candidates in selectors.items():
        value = _first_text(soup, candidates)
        if value:
            sections[label] = value
    return sections


def _split_heading_sections(content: Tag | None, ignored_titles: set[str] | None = None) -> dict[str, str]:
    if content is None:
        return {}
    ignored = ignored_titles or set()
    buffers: dict[str, list[str]] = {}
    current_title: str | None = None
    for node in content.descendants:
        if isinstance(node, Tag) and node.name in {"h1", "h2", "h3", "h4"}:
            title = _clean(node.get_text(" ", strip=True))
            current_title = title if title and title not in ignored else None
            if current_title:
                buffers.setdefault(current_title, [])
            continue
        if not isinstance(node, NavigableString) or not current_title:
            continue
        if node.find_parent(["h1", "h2", "h3", "h4"]):
            continue
        text = _clean(str(node))
        if text and (not buffers[current_title] or buffers[current_title][-1] != text):
            buffers[current_title].append(text)
    return {title: "\n".join(parts) for title, parts in buffers.items() if parts}


def _split_emphasized_sections(content: Tag | None) -> dict[str, str]:
    """Split pages that use bold star-prefixed paragraphs instead of heading tags."""
    if content is None:
        return {}
    sections: dict[str, str] = {}
    for marker in content.select("strong, b"):
        title = _clean(marker.get_text(" ", strip=True)).lstrip("★◆■● ")
        if not title:
            continue
        block = marker.find_parent(["p", "li"])
        if block is None or not _clean(block.get_text(" ", strip=True)).startswith(("★", "◆", "■", "●")):
            continue
        parts: list[str] = []
        for sibling in block.next_siblings:
            if not isinstance(sibling, Tag):
                continue
            next_marker = sibling.select_one("strong, b")
            next_text = _clean(sibling.get_text(" ", strip=True))
            if next_marker and next_text.startswith(("★", "◆", "■", "●")):
                break
            if next_text:
                parts.append(next_text)
        if parts:
            sections[title] = "\n".join(parts)
    return sections


def _extract_labeled_value(text: str, labels: str) -> str | None:
    pattern = re.compile(
        rf"(?:{labels})\s*[:：]\s*(.*?)(?=\s*(?:{FIELD_LABELS})\s*[:：]|$)",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None
    value = _clean(match.group(1))
    return value or None


def _extract_phone(root: SearchRoot, selectors: list[str], basic_text: str) -> str | None:
    structured = _first_text(root, selectors)
    if structured:
        match = re.search(r"[+\d][\d\s()（）-]{5,}\d", structured)
        return _clean(match.group(0)) if match else structured
    match = PHONE_RE.search(basic_text) or PHONE_RE.search(root.get_text(" ", strip=True))
    return _clean(match.group(1)) if match else None


def _research_from_sections(sections: dict[str, str]) -> list[str]:
    for title, content in sections.items():
        normalized = title.casefold()
        if any(keyword.casefold() in normalized for keyword in RESEARCH_TITLES):
            values = re.split(r"[\n;；|、]+", content)
            return list(dict.fromkeys(value.strip() for value in values if value.strip()))
    return []


def _research_from_labeled_text(text: str) -> list[str]:
    match = re.search(
        r"(?:^|\n)\s*(?:\d+\s*[.、．]\s*)?(?:研究方向|研究领域|科研方向)\s*[:：]\s*([^。\n]+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return []
    return list(
        dict.fromkeys(
            value.strip()
            for value in re.split(r"[,，;；|、]+", match.group(1))
            if value.strip()
        )
    )


def _admission_from_text(sections: dict[str, str], full_text: str) -> str | None:
    section_values = [
        content
        for title, content in sections.items()
        if any(keyword in title for keyword in ADMISSION_KEYWORDS)
    ]
    if section_values:
        return "\n".join(section_values)
    sentences = re.split(r"(?<=[。！？!?])\s*|\n+", full_text)
    matches = [
        sentence.strip()
        for sentence in sentences
        if sentence.strip() and any(keyword in sentence for keyword in ADMISSION_KEYWORDS)
    ]
    return "\n".join(matches) or None


def extract_teacher(html: str, page_url: str, config: SchoolConfig) -> Teacher:
    soup = BeautifulSoup(html, "lxml")
    profile = config.profile
    root: SearchRoot = soup
    if profile:
        root = soup.select_one(profile.container) or soup
        name_selectors = [profile.name]
    else:
        name_selectors = config.selectors.name
    name = _first_text(root, name_selectors)
    if not name:
        raise ValueError("could not extract a teacher name")
    name = re.sub(r"^\s*姓\s*名\s*[:：]\s*", "", name).strip()
    name = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", name)

    category = _first_text(root, config.selectors.category)
    department = _first_text(root, config.selectors.department)
    sections = _extract_sections(root, config.selectors.sections)
    full_text, content_node = _extract_content(html, root, config.selectors.body)
    photo_selectors = config.selectors.photo
    title = _first_text(root, config.selectors.title)
    basic_text = root.get_text(" ", strip=True)
    if profile:
        category = next(
            (label for url_part, label in profile.category_from_url.items() if url_part in page_url),
            category,
        )
        department = _first_text(root, [profile.department]) if profile.department else department
        if profile.summary:
            summary = _first_text(root, [profile.summary])
            if summary:
                sections = {"summary": summary, **sections}
                basic_text = summary
        full_text, content_node = _extract_content(html, root, [profile.content])
        photo_selectors = [profile.photo] if profile.photo else []
        title = _extract_labeled_value(basic_text, r"职称|职务")
        category = category or _extract_labeled_value(basic_text, r"硕博导师|导师类别")
        if title and not category:
            supervisor_labels = [
                label for label in ("硕士生导师", "博士生导师") if label in title
            ]
            if supervisor_labels:
                category = "、".join(supervisor_labels)
                title = re.sub(
                    r"[（(][^）)]*(?:硕士生导师|博士生导师)[^）)]*[）)]",
                    "",
                    title,
                ).strip(" 、,，")

    sections.update(_split_heading_sections(content_node, {name}))
    sections.update(_split_emphasized_sections(content_node))
    research_interests = _all_text(root, config.selectors.research)
    if not research_interests:
        research_interests = _research_from_sections(sections)
    if not research_interests:
        research_interests = _research_from_labeled_text(full_text)
    admission_info = _first_text(root, config.selectors.admission_info)
    if not admission_info:
        admission_info = _admission_from_text(sections, full_text)

    return Teacher(
        school=config.school,
        college=config.college,
        name=name,
        category=category,
        title=title,
        email=_extract_email(root, config.selectors.email),
        phone=_extract_phone(root, config.selectors.phone, basic_text),
        department=department,
        research_interests=research_interests,
        admission_info=admission_info,
        sections=sections,
        full_text=full_text,
        photo_url=_extract_photo(root, photo_selectors, page_url),
        profile_url=page_url,
    )
