from __future__ import annotations

"""OpenReview collection and note parsing.

OpenReview's conference pages are client-rendered and the public API may ask
for a browser challenge.  This module keeps the API path deterministic (the
selected venue and invitation are always sent together) and exposes small
HTML/snapshot helpers for a verified browser fallback.
"""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from .acl import Paper
from .config import RequestConfig

OPENREVIEW_DOMAIN = "openreview.net"
OPENREVIEW_API = "https://api2.openreview.net"
OPENREVIEW_GROUP_PATH = "/group"
OPENREVIEW_FORUM_PATH = "/forum"
OPENREVIEW_TAB_RE = re.compile(r"^tab-([A-Za-z0-9_-]+)$")
OPENREVIEW_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
MAX_API_PAGE_SIZE = 1000

# These are the labels currently used by the ICML webfield.  Unknown tabs are
# still supported: their slug is converted to a venue label and exact venue
# matching prevents notes from another tab leaking into the task.
KNOWN_TABS: dict[str, tuple[str, str]] = {
    "accept-spotlight": ("spotlight", "Accept (spotlight)"),
    "accept-oral": ("oral", "Accept (oral)"),
    "accept-regular": ("regular", "Accept (regular)"),
    "reject": ("rejected submission", "Reject"),
}


class OpenReviewError(RuntimeError):
    """Base error for a failed OpenReview request or parse."""


class OpenReviewChallengeError(OpenReviewError):
    """The API requires a browser verification challenge."""


class OpenReviewNoPapersError(OpenReviewError):
    """A valid collection returned no matching papers."""


@dataclass(frozen=True)
class OpenReviewCollection:
    url: str
    group_id: str
    tab_slug: str
    venue: str
    decision: str
    invitation: str


def is_openreview_forum_url(url: str) -> bool:
    """Return whether *url* is a canonical OpenReview forum URL."""
    try:
        _forum_id_from_url(url)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class OpenReviewPaperRef:
    url: str
    forum_id: str
    title: str = ""
    authors: tuple[str, ...] = ()


def _clean(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _value(value: object, default: object = "") -> object:
    """Unwrap OpenReview v1/v2 content values without assuming one version."""
    if isinstance(value, dict) and "value" in value:
        return value.get("value", default)
    return value if value is not None else default


def _content(note: object) -> dict[str, object]:
    if not isinstance(note, dict):
        return {}
    content = note.get("content")
    if not isinstance(content, dict):
        nested = note.get("note")
        content = nested.get("content") if isinstance(nested, dict) else None
    return content if isinstance(content, dict) else {}


def _first_content(content: dict[str, object], *names: str) -> object:
    normalized = {str(key).casefold().replace("_", " "): value for key, value in content.items()}
    for name in names:
        candidate = normalized.get(name.casefold().replace("_", " "))
        if candidate is not None:
            return _value(candidate)
    return ""


def _string_list(value: object) -> list[str]:
    value = _value(value, [])
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;；、\n]+", value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            item_value = _value(item)
            if isinstance(item_value, dict):
                item_value = item_value.get("name") or item_value.get("id") or ""
            cleaned = _clean(item_value)
            if cleaned:
                result.append(cleaned)
        return result
    return []


VISIBLE_FIELD_NAMES = {
    "abstract",
    "tl;dr",
    "tldr",
    "lay summary",
    "primary area",
    "keywords",
    "originally submitted pdf",
    "submission number",
    "venue",
    "decision",
    "comment",
}


def _visible_field_key(node: Tag) -> str:
    return _clean(node.get_text(" ", strip=True)).rstrip(":：").casefold()


def _contains_visible_field(node: Tag) -> bool:
    if node.name in {"strong", "dt"} and _visible_field_key(node) in VISIBLE_FIELD_NAMES:
        return True
    return any(
        _visible_field_key(label) in VISIBLE_FIELD_NAMES
        for label in node.select("strong, dt, .note-content-field")
    )


def _field_value_after(label: Tag) -> str:
    """Read a visible OpenReview value following its label.

    The legacy UI nests the value beside ``.note-content-field`` while the
    current React UI renders plain ``strong`` labels followed by a text node or
    paragraph.  Some browser snapshots wrap the label and value in adjacent
    blocks, so fall back to the label parent's following siblings when needed.
    """

    def collect(anchor: Tag) -> str:
        parts: list[str] = []
        for sibling in anchor.next_siblings:
            if isinstance(sibling, NavigableString):
                value = _clean(sibling)
            elif isinstance(sibling, Tag):
                if _contains_visible_field(sibling):
                    break
                value = _clean(sibling.get_text(" ", strip=True))
            else:
                value = ""
            if value:
                parts.append(value)
        return _clean(" ".join(parts))

    value = collect(label)
    if value or not isinstance(label.parent, Tag):
        return value
    return collect(label.parent)


def _visible_fields(soup: BeautifulSoup | Tag) -> dict[str, str]:
    fields: dict[str, str] = {}
    for label in soup.select("strong, dt, .note-content-field"):
        key = _visible_field_key(label)
        if key not in VISIBLE_FIELD_NAMES or key in fields:
            continue
        value = _field_value_after(label)
        if value:
            fields[key] = value
    return fields


def _date_value(value: object) -> str:
    value = _value(value)
    if isinstance(value, (int, float)):
        # OpenReview timestamps are milliseconds since epoch.  Keep a stable
        # ISO representation in exports instead of locale-dependent strings.
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return str(value)
    return _clean(value)


def _forum_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    forum_id = parse_qs(parsed.query).get("id", [""])[0].strip()
    if parsed.hostname not in {OPENREVIEW_DOMAIN, f"www.{OPENREVIEW_DOMAIN}"}:
        raise ValueError("不是有效的 OpenReview URL")
    if parsed.path.rstrip("/") != OPENREVIEW_FORUM_PATH or not OPENREVIEW_ID_RE.fullmatch(
        forum_id
    ):
        raise ValueError("不是有效的 OpenReview forum URL")
    return forum_id


def _tab_from_url(parsed: Any) -> str:
    fragment = str(parsed.fragment or "").strip()
    match = OPENREVIEW_TAB_RE.fullmatch(fragment)
    if match:
        return match.group(1).lower()
    query_tab = parse_qs(parsed.query).get("tab", [""])[0].strip().lower()
    if query_tab.startswith("tab-"):
        query_tab = query_tab[4:]
    return query_tab


def collection_from_url(url: str) -> OpenReviewCollection:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        OPENREVIEW_DOMAIN,
        f"www.{OPENREVIEW_DOMAIN}",
    }:
        raise ValueError("请输入有效的 OpenReview 分组 URL")
    if parsed.path.rstrip("/") != OPENREVIEW_GROUP_PATH:
        raise ValueError("OpenReview 采集只接受 group 分组 URL")
    group_id = parse_qs(parsed.query).get("id", [""])[0].strip()
    if not group_id or not group_id.startswith("ICML.cc/"):
        raise ValueError("OpenReview 分组 URL 缺少有效的 group id")
    tab_slug = _tab_from_url(parsed)
    if not tab_slug:
        raise ValueError("OpenReview 分组 URL 必须指定 tab，例如 #tab-accept-spotlight")
    group_parts = group_id.split("/")
    conference = (
        f"{group_parts[0].split('.')[0]} {group_parts[1]}"
        if len(group_parts) >= 2
        else group_id
    )
    venue_suffix, decision = KNOWN_TABS.get(
        tab_slug,
        (tab_slug.replace("accept-", "").replace("-", " "), tab_slug.replace("-", " ")),
    )
    venue = f"{conference} {venue_suffix}".strip()
    canonical = (
        f"https://{OPENREVIEW_DOMAIN}{OPENREVIEW_GROUP_PATH}?"
        f"{urlencode({'id': group_id})}#tab-{tab_slug}"
    )
    return OpenReviewCollection(
        url=canonical,
        group_id=group_id,
        tab_slug=tab_slug,
        venue=venue,
        decision=decision,
        invitation=f"{group_id}/-/Submission",
    )


def validate_openreview_url(url: str) -> str:
    return collection_from_url(url).url


def canonical_openreview_paper_url(url: str) -> str:
    forum_id = _forum_id_from_url(url)
    return f"https://{OPENREVIEW_DOMAIN}{OPENREVIEW_FORUM_PATH}?id={forum_id}"


def _paper_ref(note: object) -> OpenReviewPaperRef | None:
    if not isinstance(note, dict):
        return None
    note_id = _clean(note.get("forum") or note.get("id"))
    if not OPENREVIEW_ID_RE.fullmatch(note_id):
        return None
    content = _content(note)
    title = _clean(_first_content(content, "title"))
    authors = tuple(_string_list(_first_content(content, "authors")))
    return OpenReviewPaperRef(
        url=f"https://{OPENREVIEW_DOMAIN}{OPENREVIEW_FORUM_PATH}?id={note_id}",
        forum_id=note_id,
        title=title,
        authors=authors,
    )


def _extract_note_list(payload: object) -> tuple[list[dict[str, object]], int | None]:
    if not isinstance(payload, dict):
        return [], None
    raw = payload.get("notes")
    notes = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    count = payload.get("count")
    try:
        parsed_count = int(count) if count is not None else None
    except (TypeError, ValueError):
        parsed_count = None
    return notes, parsed_count


def parse_note(
    note: dict[str, object],
    *,
    collection: OpenReviewCollection | None = None,
    parser_mode: str = "OpenReview API",
    decision: str = "",
    decision_comment: str = "",
) -> Paper:
    content = _content(note)
    forum_id = _clean(note.get("forum") or note.get("id"))
    if not OPENREVIEW_ID_RE.fullmatch(forum_id):
        raise ValueError("OpenReview note 缺少有效 forum id")
    title = _clean(_first_content(content, "title"))
    if not title:
        raise ValueError("OpenReview 论文缺少标题")
    abstract = _clean(_first_content(content, "abstract"))
    authors = _string_list(_first_content(content, "authors"))
    author_ids = _string_list(_first_content(content, "authorids", "author ids"))
    author_profiles: list[dict[str, str]] = []
    for index, name in enumerate(authors):
        profile_id = author_ids[index] if index < len(author_ids) else ""
        profile: dict[str, str] = {"name": name}
        if profile_id:
            profile["id"] = profile_id
            profile["url"] = f"https://{OPENREVIEW_DOMAIN}/profile?id={profile_id}"
        author_profiles.append(profile)
    pdf_value = _clean(_first_content(content, "pdf", "paper pdf"))
    pdf_url = urljoin(f"https://{OPENREVIEW_DOMAIN}/", pdf_value) if pdf_value else ""
    original_pdf_value = _clean(
        _first_content(
            content,
            "originally submitted pdf",
            "originally_submitted_pdf",
            "original submission",
        )
    )
    original_pdf_url = (
        urljoin(f"https://{OPENREVIEW_DOMAIN}/", original_pdf_value)
        if original_pdf_value
        else ""
    )
    venue = _clean(_first_content(content, "venue")) or (collection.venue if collection else "")
    decision = _clean(_first_content(content, "decision")) or _clean(decision)
    if not decision and collection:
        decision = collection.decision
    keywords = _string_list(_first_content(content, "keywords", "keyword"))
    primary_area = _clean(_first_content(content, "primary area", "primary_area", "area"))
    tldr = _clean(_first_content(content, "tl;dr", "tldr", "tl dr"))
    lay_summary = _clean(_first_content(content, "lay summary", "lay_summary"))
    submission_number = _clean(
        _first_content(content, "submission number", "submission_number", "number")
        or note.get("number")
    )
    published_at = _date_value(note.get("pdate") or note.get("cdate"))
    modified_at = _date_value(note.get("mdate") or note.get("tmdate"))
    has_abstract = bool(abstract)
    # OpenReview publishes English metadata.  Keep it available immediately,
    # but mark the Chinese fields as pending instead of presenting English as
    # a completed translation in the web UI.
    title_translation_error = "OpenReview 英文标题尚未翻译"
    translation_error = "OpenReview 英文摘要尚未翻译" if has_abstract else ""
    return Paper(
        title=title,
        title_zh=title,
        pdf_url=pdf_url,
        abstract_en=abstract or "无摘要",
        abstract_zh=abstract or "无摘要",
        url=f"https://{OPENREVIEW_DOMAIN}{OPENREVIEW_FORUM_PATH}?id={forum_id}",
        parser_mode=parser_mode,
        translation_failed=has_abstract,
        translation_error=translation_error,
        title_translation_failed=True,
        title_translation_error=title_translation_error,
        authors=authors,
        author_profiles=author_profiles,
        original_pdf_url=original_pdf_url,
        source="OpenReview",
        venue=venue,
        decision=decision,
        decision_comment=_clean(decision_comment),
        tldr=tldr,
        lay_summary=lay_summary,
        primary_area=primary_area,
        keywords=keywords,
        submission_number=submission_number,
        published_at=published_at,
        modified_at=modified_at,
    )


def discover_from_html(html: str, page_url: str) -> list[OpenReviewPaperRef]:
    """Extract only the selected tab's forum links from a browser snapshot.

    OpenReview keeps other tabs in the DOM, so a global ``a[href*=forum]``
    query is intentionally not used here.
    """
    collection = collection_from_url(page_url)
    soup = BeautifulSoup(html, "lxml")
    panel = soup.find(id=collection.tab_slug)
    if panel is None:
        # ``tab-*`` is often the tab button itself when ARIA links it to a
        # separate panel.  Only accept it as the content panel when it
        # actually contains paper links; otherwise continue with the ARIA
        # relationship checks below.
        tab_candidate = soup.find(id=f"tab-{collection.tab_slug}")
        if tab_candidate and tab_candidate.select_one('a[href*="/forum?id="]'):
            panel = tab_candidate
    if panel is None:
        # OpenReview has used both explicit panel ids and ARIA-linked tab
        # panels.  Prefer an attribute match before the text fallback below;
        # the latter can fail when a compact card view omits the venue label.
        slug_tokens = {collection.tab_slug, f"tab-{collection.tab_slug}"}
        for candidate in soup.select(
            '[role="tabpanel"], [data-testid*="tabpanel"], [data-testid*="tab-panel"]'
        ):
            attributes = {
                str(candidate.get("id", "")),
                str(candidate.get("aria-labelledby", "")),
                str(candidate.get("data-testid", "")),
            }
            if any(
                token and any(token.casefold() in value.casefold() for value in attributes)
                for token in slug_tokens
            ):
                panel = candidate
                break
    if panel is None:
        # Some browser exports use an ARIA tabpanel without an id.  Select the
        # panel whose text contains the exact venue label, but fail closed when
        # it cannot be identified instead of mixing tabs.
        for candidate in soup.select('[role="tabpanel"]'):
            if collection.venue.casefold() in candidate.get_text(" ", strip=True).casefold():
                panel = candidate
                break
    if panel is None:
        raise OpenReviewError(
            f"找不到 OpenReview tab {collection.tab_slug}；请导入已完成验证的页面快照"
        )
    refs: dict[str, OpenReviewPaperRef] = {}
    for link in panel.select('a[href*="/forum?id="]'):
        href = urljoin(page_url, str(link.get("href", "")))
        try:
            canonical = canonical_openreview_paper_url(href)
            forum_id = _forum_id_from_url(canonical)
        except ValueError:
            continue
        title = _clean(link.get_text(" ", strip=True))
        refs.setdefault(forum_id, OpenReviewPaperRef(canonical, forum_id, title))
    if not refs:
        raise OpenReviewNoPapersError("OpenReview 选定 tab 没有发现论文")
    return list(refs.values())


def parse_forum_html(
    html: str,
    page_url: str,
    *,
    collection: OpenReviewCollection | None = None,
) -> Paper:
    """Parse a saved forum page when a browser/API snapshot is available."""
    forum_id = _forum_id_from_url(page_url)
    soup = BeautifulSoup(html, "lxml")
    # Next.js may carry the complete submission note in __NEXT_DATA__.  Keep
    # parsing the visible reply tree as well: the official decision is a
    # separate note and is not part of the submission content.
    next_note: dict[str, object] | None = None
    for script in soup.select("script#__NEXT_DATA__"):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue

        def walk(value: object) -> dict[str, object] | None:
            if isinstance(value, dict):
                value_id = _clean(value.get("id"))
                value_forum = _clean(value.get("forum"))
                value_content = _content(value)
                if (
                    value_id == forum_id
                    or (
                        value_forum == forum_id
                        and bool(_clean(_first_content(value_content, "title")))
                    )
                ) and bool(value_content):
                    return value
                for child in value.values():
                    found = walk(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = walk(child)
                    if found:
                        return found
            return None

        note = walk(payload)
        if note:
            next_note = note
            break

    if next_note:
        decision, decision_comment = _decision_from_html(soup)
        return parse_note(
            next_note,
            collection=collection,
            parser_mode="OpenReview 浏览器快照",
            decision=decision,
            decision_comment=decision_comment,
        )

    def selector_text(*selectors: str) -> str:
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                value = _clean(node.get_text(" ", strip=True))
                if value:
                    return value
        return ""

    title = selector_text(
        ".citation_title",
        "h2.citation_title",
        "[data-testid='paper-title']",
        "main h2",
        "h1",
    )
    if not title:
        meta = soup.select_one('meta[name="citation_title"]')
        title = _clean(meta.get("content")) if meta else ""
    abstract = selector_text(
        "[data-testid='abstract']",
        ".note-content-field.abstract + .note-content-value",
        ".abstract",
        "#abstract",
    )
    if not abstract:
        for heading in soup.find_all(["h2", "h3", "h4", "strong"]):
            if _clean(heading.get_text(" ", strip=True)).casefold() == "abstract":
                abstract = _clean(heading.parent.get_text(" ", strip=True)) if heading.parent else ""
                abstract = re.sub(r"^abstract\s*", "", abstract, flags=re.IGNORECASE)
                break
    fields = _visible_fields(soup)
    if not abstract:
        abstract = fields.get("abstract", "")
    author_nodes = soup.select(
        ".forum-authors h3 a, .forum-authors a, [data-testid='author-name'], "
        "[data-testid='authors'] a, .submission-authors a, "
        "main h3 a[href*='/profile?id=']"
    )
    if not author_nodes:
        # Metadata links in replies include reviewers and program chairs.  A
        # broad page-wide profile selector silently turned those people into
        # paper authors, so use citation metadata only as a conservative
        # fallback.
        author_nodes = soup.select("meta[name='citation_author']")
    authors = [
        _clean(node.get("content", ""))
        if node.name == "meta"
        else _clean(node.get_text(" ", strip=True))
        for node in author_nodes
        if (
            _clean(node.get("content", ""))
            if node.name == "meta"
            else _clean(node.get_text(" ", strip=True))
        )
    ]
    author_profiles = []
    for node in soup.select(
        ".forum-authors a[href*='/profile?id='], "
        ".submission-authors a[href*='/profile?id='], "
        "[data-testid='authors'] a[href*='/profile?id='], "
        "main h3 a[href*='/profile?id=']"
    ):
        name = _clean(node.get_text(" ", strip=True))
        href = urljoin(f"https://{OPENREVIEW_DOMAIN}/", str(node.get("href", "")))
        if name and not any(item.get("name") == name for item in author_profiles):
            author_profiles.append({"name": name, "url": href})
    pdf = soup.select_one(
        ".citation_pdf_url[href], a[href*='name=pdf'], "
        "a[href*='/pdf?id='], a[href*='/attachment?id=']"
    )
    pdf_url = urljoin(f"https://{OPENREVIEW_DOMAIN}/", str(pdf.get("href"))) if pdf else ""
    original_pdf = soup.select_one("a[href*='name=originally_submitted_PDF']")
    original_pdf_url = (
        urljoin(f"https://{OPENREVIEW_DOMAIN}/", str(original_pdf.get("href")))
        if original_pdf
        else ""
    )
    published_at = ""
    modified_at = ""
    for node in soup.select(".forum-meta .date.item, .forum-meta .item"):
        value = _clean(node.get_text(" ", strip=True))
        lowered = value.casefold()
        if lowered.startswith("published:"):
            published_at = value.split(":", 1)[1].strip()
        elif lowered.startswith("last modified:"):
            modified_at = value.split(":", 1)[1].strip()
    if not published_at or not modified_at:
        page_text = _clean(soup.get_text(" ", strip=True))
        date_match = re.search(
            r"Published:\s*(.+?),\s*Last Modified:\s*(.+?)(?=\s*ICML\b|\s+Readers:|$)",
            page_text,
            flags=re.IGNORECASE,
        )
        if date_match:
            published_at = published_at or date_match.group(1).strip()
            modified_at = modified_at or date_match.group(2).strip()
    decision = fields.get("decision", collection.decision if collection else "")
    decision_reply, decision_comment = _decision_from_html(soup)
    if decision_reply:
        decision = decision_reply
    submission_number = fields.get("submission number", "")
    number_match = re.search(r"\d+", submission_number)
    if number_match:
        submission_number = number_match.group(0)
    if not title:
        raise ValueError("无法解析 OpenReview 论文标题")
    return Paper(
        title=title,
        title_zh=title,
        pdf_url=pdf_url,
        abstract_en=abstract or "无摘要",
        abstract_zh=abstract or "无摘要",
        url=f"https://{OPENREVIEW_DOMAIN}{OPENREVIEW_FORUM_PATH}?id={forum_id}",
        parser_mode="OpenReview 浏览器快照",
        authors=list(dict.fromkeys(authors)),
        author_profiles=author_profiles,
        original_pdf_url=original_pdf_url,
        source="OpenReview",
        venue=fields.get("venue", collection.venue if collection else ""),
        decision=decision,
        decision_comment=decision_comment,
        tldr=fields.get("tl;dr", fields.get("tldr", "")),
        lay_summary=fields.get("lay summary", ""),
        primary_area=fields.get("primary area", ""),
        keywords=_string_list(fields.get("keywords", "")),
        submission_number=submission_number,
        published_at=published_at,
        modified_at=modified_at,
    )


def _decision_from_html(soup: BeautifulSoup) -> tuple[str, str]:
    """Extract only the official ``/-/Decision`` reply from a forum page."""
    candidates: list[tuple[str, str, str]] = []
    for note_node in soup.select(".note[data-id], .note"):
        heading = _clean(
            " ".join(
                node.get_text(" ", strip=True)
                for node in note_node.select(
                    ".heading h4, .subheading .invitation, h4, h3"
                )
            )
        ).casefold()
        invitation_text = " ".join(
            node.get_text(" ", strip=True)
            for node in note_node.select(
                ".invitation, [data-invitation], [data-testid*='invitation']"
            )
        ).casefold()
        invitation_links = " ".join(
            str(node.get("href", ""))
            for node in note_node.select("a[href*='/Decision'], a[href*='/-/Decision']")
        ).casefold()
        if "paper decision" not in heading and not (
            ("decision" in invitation_text or "/decision" in invitation_links)
            and "review" not in invitation_text
        ):
            continue
        decision_fields = _visible_fields(note_node)
        decision = decision_fields.get("decision", "")
        comment = decision_fields.get("comment", "")
        if decision:
            candidates.append((decision, comment, note_node.get("data-id", "")))
    if not candidates:
        return "", ""
    # The page normally renders newest first; retaining the first candidate
    # avoids accidentally selecting an older revision when no timestamp is
    # exposed in the static markup.
    return candidates[0][0], candidates[0][1]


class OpenReviewClient:
    def __init__(
        self,
        config: RequestConfig | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or RequestConfig(
            delay_seconds=0.25,
            timeout_seconds=30,
            retries=3,
            user_agent="TeacherResearchCrawler/1.0 (OpenReview public metadata)",
        )
        self._sleeper = sleeper
        self._last_request = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": self.config.user_agent, "Accept": "application/json"},
            follow_redirects=True,
            timeout=self.config.timeout_seconds,
            transport=transport,
            verify=self.config.verify_ssl,
        )
        self._notes: dict[str, dict[str, object]] = {}

    def __enter__(self) -> "OpenReviewClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request
        remaining = self.config.delay_seconds - elapsed
        if remaining > 0:
            self._sleeper(remaining)
        self._last_request = time.monotonic()

    def _get_json(self, path: str, params: dict[str, object]) -> dict[str, object]:
        url = f"{OPENREVIEW_API}{path}"
        last_error: BaseException | None = None
        for attempt in range(max(1, self.config.retries)):
            try:
                self._wait()
                response = self._client.get(
                    url,
                    params={key: value for key, value in params.items() if value is not None},
                )
                if response.status_code == 403:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {}
                    name = payload.get("name") if isinstance(payload, dict) else ""
                    challenge_name = _clean(name) or "HTTP 403"
                    # These endpoints contain public conference metadata.  In
                    # practice OpenReview and its edge layer return several
                    # different JSON names, and sometimes an HTML challenge,
                    # for the same browser-verification requirement.  Treat
                    # every 403 as a batch-level pause so a changed error body
                    # cannot turn one site challenge into hundreds of paper
                    # failures.
                    raise OpenReviewChallengeError(
                        f"OpenReview 要求浏览器验证（{challenge_name}）；"
                        "请在浏览器中完成验证后导入页面快照，或稍后重试"
                    )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise OpenReviewError("OpenReview API 返回格式不是对象")
                return payload
            except OpenReviewChallengeError:
                raise
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as exc:
                last_error = exc
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code not in {429, 500, 502, 503, 504} and not isinstance(
                    exc, httpx.TransportError
                ):
                    break
                if attempt + 1 < max(1, self.config.retries):
                    self._sleeper(min(8.0, 0.5 * (2**attempt)))
        raise OpenReviewError(f"OpenReview API 请求失败：{type(last_error).__name__}: {last_error}")

    def discover(self, collection: OpenReviewCollection) -> list[OpenReviewPaperRef]:
        refs: dict[str, OpenReviewPaperRef] = {}
        offset = 0
        expected: int | None = None
        seen_pages: set[tuple[str, ...]] = set()
        while True:
            payload = self._get_json(
                "/notes",
                {
                    "content.venue": collection.venue,
                    "invitation": collection.invitation,
                    "domain": collection.group_id,
                    "details": "replyCount,presentation,writable",
                    "limit": MAX_API_PAGE_SIZE,
                    "offset": offset,
                    "count": "true",
                    "sort": "number:asc",
                },
            )
            notes, count = _extract_note_list(payload)
            if count is not None:
                expected = count
            if not notes:
                break
            page_key = tuple(
                _clean(note.get("forum") or note.get("id")) for note in notes
            )
            if page_key in seen_pages:
                raise OpenReviewError("OpenReview 分页未前进，已停止防止重复采集")
            seen_pages.add(page_key)
            before = len(refs)
            for note in notes:
                ref = _paper_ref(note)
                if ref:
                    refs.setdefault(ref.forum_id, ref)
                    self._notes[ref.forum_id] = note
            offset += len(notes)
            if expected is not None:
                if offset >= expected:
                    break
                if len(refs) == before:
                    raise OpenReviewError(
                        f"OpenReview 分页未发现新论文（已发现 {len(refs)}/{expected}）"
                    )
            elif len(notes) < MAX_API_PAGE_SIZE:
                break
            if offset > 1_000_000:
                raise OpenReviewError("OpenReview 分页数量异常，已停止防止无限请求")
        if expected is not None and len(refs) != expected:
            raise OpenReviewError(
                f"OpenReview 发现数量不完整：{len(refs)}/{expected}；"
                "请重试或使用浏览器快照导入"
            )
        if not refs:
            raise OpenReviewNoPapersError(
                f"OpenReview 未找到 venue={collection.venue!r} 的论文"
            )
        return list(refs.values())

    def fetch_paper(
        self,
        url: str,
        *,
        collection: OpenReviewCollection | None = None,
    ) -> Paper:
        forum_id = _forum_id_from_url(url)
        note = self._notes.get(forum_id)
        if note is None:
            payload = self._get_json("/notes", {"id": forum_id, "limit": 1})
            notes, _ = _extract_note_list(payload)
            if not notes:
                raise OpenReviewError(f"OpenReview 论文不存在：{forum_id}")
            note = notes[0]
            self._notes[forum_id] = note
        decision = ""
        decision_comment = ""
        try:
            replies_payload = self._get_json(
                "/notes",
                {
                    "forum": forum_id,
                    "trash": "true",
                    "details": "writable,signatures,invitation,presentation,tags",
                    "domain": collection.group_id if collection else None,
                    "limit": MAX_API_PAGE_SIZE,
                },
            )
            replies, _ = _extract_note_list(replies_payload)
            decision_candidates: list[tuple[int, dict[str, object], str, str]] = []
            for reply in replies:
                invitations = reply.get("invitations") or [reply.get("invitation")]
                invitation_values = _string_list(invitations)
                reply_content = _content(reply)
                candidate_decision = _clean(_first_content(reply_content, "decision"))
                is_decision = any(
                    value.rstrip("/").endswith("/-/Decision")
                    or value.rstrip("/").endswith("/Decision")
                    for value in invitation_values
                )
                if not is_decision:
                    continue
                timestamp = reply.get("mdate") or reply.get("cdate") or 0
                try:
                    timestamp_value = int(timestamp)
                except (TypeError, ValueError):
                    timestamp_value = 0
                decision_candidates.append(
                    (
                        timestamp_value,
                        reply,
                        candidate_decision,
                        _clean(_first_content(reply_content, "comment")),
                    )
                )
            if decision_candidates:
                _, _, decision, decision_comment = max(
                    decision_candidates, key=lambda item: item[0]
                )
        except OpenReviewChallengeError:
            raise
        except OpenReviewError:
            # The submission itself is still a valid success when replies are
            # temporarily unavailable; the selected collection carries the
            # authoritative decision label.
            pass
        return parse_note(
            note,
            collection=collection,
            decision=decision,
            decision_comment=decision_comment,
        )


def make_openreview_client(output_dir: Path | None = None) -> OpenReviewClient:
    # output_dir is accepted for symmetry with make_fetcher and future cache
    # support; API responses are persisted by TaskManager after parsing.
    del output_dir
    return OpenReviewClient()
