from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path

from bs4 import BeautifulSoup

from .models import Teacher, TeacherResearch

SIGNALS: dict[str, tuple[tuple[str, int], ...]] = {
    "internship": (
        ("长期招收实习生", 5),
        ("招收校内外实习生", 5),
        ("招收实习生", 5),
        ("接收实习生", 4),
        ("欢迎实习", 3),
        ("科研实习", 2),
    ),
    "graduate": (
        ("招收博士研究生", 5),
        ("招收硕士研究生", 5),
        ("招收推免生", 5),
        ("欢迎报考", 4),
        ("计划招收", 3),
        ("欢迎加入课题组", 3),
        ("博士生名额", 3),
        ("硕士生名额", 3),
        ("招收博士生", 5),
        ("招收硕士生", 5),
    ),
    "contact": (
        ("欢迎来邮件交流", 4),
        ("欢迎邮件联系", 4),
        ("有意者请发邮件", 4),
        ("欢迎投递简历", 4),
        ("请发送简历", 3),
        ("与我联系", 3),
        ("欢迎联系", 1),
    ),
}
NEGATIVE_SIGNALS = ("今年不招生", "暂无招生名额", "停止招收", "不接收实习生", "名额已满")
LAB_PATTERN = re.compile(
    r"(?:所属|所在)?\s*([^。；;\n:：]{0,40}(?:实验室|研究中心|课题组|研究团队))"
    r"\s*[:：]\s*([^。；;\n]{2,80})"
)


def _clean(value: str) -> str:
    return " ".join(value.split())


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
    return list(dict.fromkeys(_clean(part) for part in parts if _clean(part)))


def _find_section(teacher: Teacher, keywords: tuple[str, ...]) -> str:
    for title, content in teacher.sections.items():
        if any(keyword.casefold() in title.casefold() for keyword in keywords):
            return _clean(content)
    return ""


def analyze_teacher(teacher: Teacher, html_path: Path) -> TeacherResearch:
    html = html_path.read_text(encoding="utf-8", errors="replace") if html_path.exists() else ""
    visible_text = BeautifulSoup(html, "lxml").get_text("\n", strip=True) if html else teacher.full_text
    combined_text = "\n".join(filter(None, [visible_text, teacher.full_text, teacher.admission_info or ""]))

    matched: list[str] = []
    types: set[str] = set()
    score = 0
    for sentence in _sentences(combined_text):
        sentence_types: set[str] = set()
        sentence_score = 0
        for signal_type, entries in SIGNALS.items():
            for keyword, weight in entries:
                if keyword in sentence:
                    sentence_types.add(signal_type)
                    sentence_score = max(sentence_score, weight)
        negative = any(keyword in sentence for keyword in NEGATIVE_SIGNALS)
        if sentence_types or negative:
            matched.append(sentence)
            types.update(sentence_types)
            score += -10 if negative else sentence_score

    title_parts: list[str] = []
    for value in filter(None, [teacher.title, teacher.category]):
        if not any(value in existing or existing in value for existing in title_parts):
            title_parts.append(value)
    research = " | ".join(teacher.research_interests)
    if not research:
        research = _find_section(teacher, ("研究方向", "研究领域", "科研方向"))

    lab = _find_section(teacher, ("实验室", "研究中心", "课题组", "团队"))
    if not lab:
        lab_match = LAB_PATTERN.search(combined_text)
        lab = (
            f"{_clean(re.sub(r'^(?:所属|所在)\s*', '', lab_match.group(1)))} / "
            f"{_clean(lab_match.group(2))}"
            if lab_match
            else ""
        )

    bio = _find_section(teacher, ("个人简介", "个人信息", "简介", "教育经历"))
    if not bio:
        sentences = _sentences(teacher.full_text)
        named = [sentence for sentence in sentences if sentence.startswith(teacher.name)]
        candidates = named or [sentence for sentence in sentences if len(sentence) >= 12]
        bio = candidates[0][:300] if candidates else ""

    return TeacherResearch(
        name=teacher.name,
        title=" / ".join(title_parts),
        research_dir=research,
        email=teacher.email or "",
        lab=lab,
        bio=bio[:500],
        homepage_url=teacher.profile_url,
        recruit_text=" | ".join(matched),
        has_recruit_info=bool(matched),
        recruit_types=sorted(types),
        recruit_score=score,
        html_path=html_path.as_posix(),
        html_hash=sha256(html.encode("utf-8")).hexdigest() if html else "",
    )
