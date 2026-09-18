from __future__ import annotations

import csv
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from .config import RequestConfig
from .fetcher import Fetcher
from .nllb import get_nllb_translator

ACL_DOMAIN = "aclanthology.org"
ACM_DOMAIN = "dl.acm.org"
ACL_PAPER_PATH_RE = re.compile(
    r"/(?:\d{4}\.[A-Za-z0-9-]+\.\d+|[A-Za-z]\d{2}-\d+)/?"
)
ACM_PAPER_PATH_RE = re.compile(
    r"/doi/(?!proceedings(?:/|$)|abs(?:/|$)|pdf(?:/|$)|epdf(?:/|$))"
    r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)/?",
    re.IGNORECASE,
)
ACM_PROCEEDINGS_PATH_RE = re.compile(
    r"/doi/proceedings/(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)/?",
    re.IGNORECASE,
)
ACM_HEADING_RE = re.compile(r"heading\d+")
DEFAULT_TRANSLATE_URL = "https://api.mymemory.translated.net/get"
BING_TRANSLATOR_PAGE = "https://cn.bing.com/translator"
TRANSLATE_CHUNK_SIZE = 450
_BING_AUTH_LOCK = threading.Lock()
_BING_AUTH: tuple[str, str, str, str, float] | None = None


@dataclass
class Paper:
    title: str
    title_zh: str
    pdf_url: str
    abstract_en: str
    abstract_zh: str
    url: str
    status: str = "success"
    parser_mode: str = "专用解析"
    favorite: bool = False
    translation_failed: bool = False
    translation_error: str = ""
    title_translation_failed: bool = False
    title_translation_error: str = ""
    authors: list[str] = field(default_factory=list)
    collected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["title_en"] = data["title"]
        data["author"] = ", ".join(self.authors)
        return data


def canonical_paper_url(url: str) -> str:
    """Return the stable HTTPS detail URL used as a paper identity."""
    parsed = urlparse(url)
    if parsed.hostname == ACL_DOMAIN and ACL_PAPER_PATH_RE.fullmatch(parsed.path):
        return f"https://{ACL_DOMAIN}{parsed.path.rstrip('/')}/"
    match = ACM_PAPER_PATH_RE.fullmatch(parsed.path)
    if parsed.hostname == ACM_DOMAIN and match:
        return f"https://{ACM_DOMAIN}/doi/{match.group(1)}"
    raise ValueError("不是有效的 ACL Anthology 或 ACM Digital Library 论文 URL")


def paper_id_from_url(url: str) -> str:
    canonical = canonical_paper_url(url)
    parsed = urlparse(canonical)
    if parsed.hostname == ACM_DOMAIN:
        return parsed.path.removeprefix("/doi/").replace("/", "_")
    return parsed.path.strip("/")


def _paper_belongs_to_volume(paper_id: str, page_url: str) -> bool:
    parts = [part for part in urlparse(page_url).path.split("/") if part]
    if len(parts) < 2 or parts[-2] != "volumes":
        return True
    volume_id = parts[-1]
    if re.fullmatch(r"\d{4}\.[A-Za-z0-9-]+", volume_id):
        return paper_id.rsplit(".", 1)[0] == volume_id
    return paper_id.startswith(volume_id)


def _is_front_matter(paper_id: str, page_url: str) -> bool:
    if paper_id.endswith(".0"):
        return True
    parts = [part for part in urlparse(page_url).path.split("/") if part]
    return (
        len(parts) >= 2
        and parts[-2] == "volumes"
        and re.fullmatch(r"[A-Za-z]\d{2}-\d+", parts[-1]) is not None
        and paper_id == f"{parts[-1]}000"
    )


def validate_volume_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("请输入有效的 ACL 或 ACM 论文页面 URL")
    if is_paper_url(url):
        return canonical_paper_url(url)
    if parsed.hostname == ACL_DOMAIN:
        if not parsed.path.startswith("/volumes/"):
            raise ValueError("论文模式只接受 ACL 文集卷/论文页或 ACM 分组/论文页")
        path = parsed.path.rstrip("/") + "/"
        return f"https://{ACL_DOMAIN}{path}"
    if parsed.hostname == ACM_DOMAIN and ACM_PROCEEDINGS_PATH_RE.fullmatch(parsed.path):
        values = parse_qs(parsed.query).get("tocHeading", [])
        heading = parsed.fragment or (values[0] if values else "")
        if not ACM_HEADING_RE.fullmatch(heading):
            raise ValueError(
                "ACM proceedings URL 必须指定 SESSION 分组，例如 #heading2"
            )
        path = parsed.path.rstrip("/")
        return f"https://{ACM_DOMAIN}{path}?{urlencode({'tocHeading': heading})}"
    raise ValueError("论文模式只接受 ACL 文集卷/论文页或 ACM 分组/论文页")


def is_paper_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.hostname == ACL_DOMAIN
        and ACL_PAPER_PATH_RE.fullmatch(parsed.path) is not None
    ) or (
        parsed.hostname == ACM_DOMAIN
        and ACM_PAPER_PATH_RE.fullmatch(parsed.path) is not None
    )


def discover_papers(html: str, page_url: str) -> list[str]:
    """Return canonical detail URLs from an ACL volume or ACM session page."""
    soup = BeautifulSoup(html, "lxml")
    parsed_page = urlparse(page_url)
    if parsed_page.hostname == ACM_DOMAIN:
        values = parse_qs(parsed_page.query).get("tocHeading", [])
        heading_id = values[0] if values else parsed_page.fragment
        heading = soup.find(id=heading_id) if heading_id else None
        papers: dict[str, None] = {}
        if not isinstance(heading, Tag):
            return []
        # ACM renders every issue item as a sibling of the SESSION heading.
        # Stop at the next heading instead of treating only the first sibling as
        # the complete panel; the latter silently reduced large sessions to one.
        for node in heading.find_all_next():
            if node is heading:
                continue
            node_id = str(node.get("id", ""))
            if ACM_HEADING_RE.fullmatch(node_id):
                break
            if node.name != "a" or not node.get("href"):
                continue
            title_parent = node.find_parent(class_="issue-item__title")
            if title_parent is None:
                continue
            try:
                papers.setdefault(
                    canonical_paper_url(urljoin(page_url, str(node.get("href", "")))),
                    None,
                )
            except ValueError:
                continue
        return list(papers)
    papers: dict[str, None] = {}
    for link in soup.select("a[href]"):
        href = str(link.get("href", "")).strip()
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.hostname != ACL_DOMAIN:
            continue
        if ACL_PAPER_PATH_RE.fullmatch(parsed.path):
            canonical = canonical_paper_url(absolute)
            paper_id = paper_id_from_url(canonical)
            if _paper_belongs_to_volume(paper_id, page_url) and not _is_front_matter(
                paper_id, page_url
            ):
                papers.setdefault(canonical, None)
    return list(papers)


def _text(node: object) -> str:
    if not node:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def _valid_translation(translated: object) -> str:
    if not isinstance(translated, str) or not translated.strip():
        raise ValueError("翻译接口未返回有效译文")
    return translated.strip()


def _translate_with_custom_endpoint(text: str, endpoint: str) -> str:
    response = httpx.post(
        endpoint,
        json={"q": text, "source": "en", "target": "zh", "format": "text"},
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("自定义翻译接口返回格式不是对象")
    return _valid_translation(payload.get("translatedText"))


def _bing_auth() -> tuple[str, str, str, str]:
    global _BING_AUTH
    with _BING_AUTH_LOCK:
        if _BING_AUTH and _BING_AUTH[4] > time.monotonic():
            return _BING_AUTH[:4]
        response = httpx.get(BING_TRANSLATOR_PAGE, follow_redirects=True, timeout=20)
        response.raise_for_status()
        html = response.text
        helper = re.search(
            r'params_AbusePreventionHelper\s*=\s*\[(\d+),"([^"]+)",(\d+)\]',
            html,
        )
        ig = re.search(r'IG:"([^"]+)"', html)
        iid = re.search(r'data-iid="([^"]+)"', html)
        if not helper or not ig or not iid:
            raise ValueError("主翻译服务初始化参数缺失")
        key, token, duration_ms = helper.groups()
        _BING_AUTH = (
            key,
            token,
            ig.group(1),
            iid.group(1),
            time.monotonic() + min(int(duration_ms) / 1000, 3300),
        )
        return _BING_AUTH[:4]


def _translate_with_bing(text: str) -> str:
    key, token, ig, iid = _bing_auth()
    response = httpx.post(
        "https://cn.bing.com/ttranslatev3",
        params={"isVertical": "1", "IG": ig, "IID": f"{iid}.1"},
        data={
            "fromLang": "en",
            "to": "zh-Hans",
            "text": text,
            "token": token,
            "key": key,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    try:
        translated = payload[0]["translations"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("主翻译服务返回格式异常") from exc
    return _valid_translation(translated)


def _translate_with_mymemory(text: str) -> str:
    response = httpx.get(
        DEFAULT_TRANSLATE_URL,
        params={"q": text, "langpair": "en|zh-CN"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("备用翻译接口返回格式不是对象")
    response_status = payload.get("responseStatus", 200)
    if str(response_status) != "200":
        detail = payload.get("responseDetails") or f"状态码 {response_status}"
        raise ValueError(f"备用翻译接口拒绝请求：{detail}")
    response_data = payload.get("responseData")
    if not isinstance(response_data, dict):
        raise ValueError("备用翻译接口缺少 responseData")
    return _valid_translation(response_data.get("translatedText"))


def _allow_online_translation() -> bool:
    value = os.environ.get("ACL_ALLOW_ONLINE_TRANSLATION", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _translate_online(text: str) -> str:
    """Use explicitly enabled online providers after local NLLB fails."""
    endpoint = os.environ.get("ACL_TRANSLATE_URL", "").strip()
    providers: list[tuple[str, Callable[[str], str]]] = []
    if endpoint:
        providers.append(
            ("自定义翻译服务", lambda value: _translate_with_custom_endpoint(value, endpoint))
        )
    if _allow_online_translation():
        providers.extend(
            [
                ("主翻译服务", _translate_with_bing),
                ("备用翻译服务", _translate_with_mymemory),
            ]
        )
    if not providers:
        raise RuntimeError("在线翻译未启用")
    errors: list[str] = []
    for name, provider in providers:
        try:
            return provider(text)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    raise RuntimeError("；".join(errors))


def translate_text(text: str) -> str:
    """Translate a title or short text locally, with opt-in online fallback."""
    if not isinstance(text, str) or not text:
        raise ValueError("翻译文本为空")
    try:
        return get_nllb_translator().translate_many([text])[0]
    except Exception as local_error:
        if not os.environ.get("ACL_TRANSLATE_URL", "").strip() and not _allow_online_translation():
            raise RuntimeError(
                f"本地 NLLB: {type(local_error).__name__}: {local_error}"
            ) from local_error
        try:
            return _translate_online(text)
        except Exception as online_error:
            raise RuntimeError(
                f"本地 NLLB: {type(local_error).__name__}: {local_error}；"
                f"在线备用: {type(online_error).__name__}: {online_error}"
            ) from online_error


def translate_abstract(text: str) -> str:
    if not text:
        raise ValueError("英文摘要为空")
    chunks: list[str] = []
    offset = 0
    while offset < len(text):
        end = min(offset + TRANSLATE_CHUNK_SIZE, len(text))
        if end < len(text):
            boundary = text.rfind(" ", offset + TRANSLATE_CHUNK_SIZE // 2, end)
            if boundary >= 0:
                end = boundary + 1
        chunks.append(text[offset:end])
        offset = end
    try:
        translated = get_nllb_translator().translate_many(chunks)
    except Exception as local_error:
        if not os.environ.get("ACL_TRANSLATE_URL", "").strip() and not _allow_online_translation():
            raise RuntimeError(
                f"本地 NLLB: {type(local_error).__name__}: {local_error}"
            ) from local_error
        try:
            translated = [_translate_online(chunk) for chunk in chunks]
        except Exception as online_error:
            raise RuntimeError(
                f"本地 NLLB: {type(local_error).__name__}: {local_error}；"
                f"在线备用: {type(online_error).__name__}: {online_error}"
            ) from online_error
    if len(translated) != len(chunks) or any(
        not isinstance(value, str) or not value.strip() for value in translated
    ):
        raise ValueError("翻译模块返回的译文数量或内容异常")
    return "".join(translated)


def _clean_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s+([.,;:!?])", r"\1", value)


def _title_text(node: object) -> str:
    if not node:
        return ""
    return _clean_text(node.get_text("", strip=False))


def _translated_paper(
    title: str,
    abstract: str,
    pdf_url: str,
    canonical_url: str,
    authors: list[str],
    parser_mode: str,
    log: Callable[[str], None] | None,
) -> Paper:
    title_translation_failed = False
    title_translation_error = ""
    if log:
        log(f"准备翻译标题，文本长度：{len(title)}")
    try:
        title_zh = translate_text(title)
    except Exception as exc:
        title_translation_failed = True
        title_translation_error = f"{type(exc).__name__}: {exc}"
        title_zh = title
        if log:
            log(f"标题翻译调用异常：{title_translation_error}，保留英文标题")
    translation_failed = False
    translation_error = ""
    if not abstract:
        abstract = "无摘要"
        abstract_zh = "无摘要"
    else:
        if log:
            log(f"准备翻译摘要，文本长度：{len(abstract)}")
        try:
            abstract_zh = translate_abstract(abstract)
        except Exception as exc:
            translation_failed = True
            translation_error = f"{type(exc).__name__}: {exc}"
            abstract_zh = abstract
            if log:
                log(
                    f"翻译调用异常：{translation_error}，"
                    "执行降级，中文摘要复用英文原文"
                )
    return Paper(
        title=title,
        title_zh=title_zh,
        pdf_url=pdf_url,
        abstract_en=abstract,
        abstract_zh=abstract_zh,
        url=canonical_url,
        parser_mode=parser_mode,
        translation_failed=translation_failed,
        translation_error=translation_error,
        title_translation_failed=title_translation_failed,
        title_translation_error=title_translation_error,
        authors=authors,
    )


def _extract_acm_paper(
    soup: BeautifulSoup,
    page_url: str,
    log: Callable[[str], None] | None,
) -> Paper:
    canonical_url = canonical_paper_url(page_url)
    expected_doi = urlparse(canonical_url).path.removeprefix("/doi/")
    doi_node = soup.select_one('meta[name="publication_doi"]')
    actual_doi = _clean_text(str(doi_node.get("content", ""))) if doi_node else ""
    if actual_doi and actual_doi.casefold() != expected_doi.casefold():
        raise ValueError(
            f"ACM 论文页面 DOI 不一致：期望 {expected_doi}，实际 {actual_doi}"
        )
    title = _title_text(soup.select_one('h1[property="name"], main h1'))
    abstract = _text(
        soup.select_one(
            'section#abstract [role="paragraph"], '
            'section[property="abstract"] [role="paragraph"]'
        )
    )
    authors: list[str] = []
    for author in soup.select(
        '.contributors [property="author"][role="listitem"], '
        '[role="list"] > [property="author"][role="listitem"]'
    ):
        given = _text(author.find(attrs={"property": "givenName"}))
        family = _text(author.find(attrs={"property": "familyName"}))
        name = " ".join(part for part in (given, family) if part)
        if name and name not in authors:
            authors.append(name)
    pdf = soup.select_one('a[href*="/doi/pdf/"], a[href*="/doi/epdf/"]')
    pdf_url = urljoin(canonical_url, str(pdf.get("href"))) if pdf else ""
    if not title:
        raise ValueError("无法解析 ACM 论文标题")
    if not doi_node and not soup.select_one('section#abstract, .contributors'):
        raise ValueError("页面缺少 ACM Digital Library 论文元数据")
    return _translated_paper(
        title,
        abstract,
        pdf_url,
        canonical_url,
        authors,
        "ACM 专用解析",
        log,
    )


def _extract_abstract(soup: BeautifulSoup) -> str:
    """Extract text after an Abstract label from ACL's abstract card."""
    heading_names = ["strong", "h2", "h3", "h4", "h5", "h6"]
    blocks = soup.select(".acl-abstract, div.card-body.acl-abstract, #abstract, .abstract")
    for block in blocks:
        for label in block.find_all(heading_names):
            if _clean_text(label.get_text(" ", strip=True)).casefold() != "abstract":
                continue
            parts = [str(node) if isinstance(node, str) else node.get_text(" ", strip=True) for node in label.next_siblings]
            abstract = _clean_text(" ".join(parts))
            if abstract:
                return abstract
    for heading in soup.find_all(heading_names):
        if _clean_text(heading.get_text(" ", strip=True)).casefold() != "abstract":
            continue
        parent = heading.parent
        if parent:
            parts = [str(node) if isinstance(node, str) else node.get_text(" ", strip=True) for node in heading.next_siblings]
            abstract = _clean_text(" ".join(parts))
            if abstract:
                return abstract
    return ""


def extract_paper(
    html: str,
    page_url: str,
    log: Callable[[str], None] | None = None,
) -> Paper:
    soup = BeautifulSoup(html, "lxml")
    if urlparse(page_url).hostname == ACM_DOMAIN:
        return _extract_acm_paper(soup, page_url, log)
    canonical_url = canonical_paper_url(page_url)
    identity = soup.select_one('meta[property="og:url"]')
    if identity and identity.get("content"):
        actual_url = canonical_paper_url(str(identity.get("content")))
        if actual_url != canonical_url:
            raise ValueError(
                f"论文页面标识不一致：期望 {paper_id_from_url(canonical_url)}，"
                f"实际 {paper_id_from_url(actual_url)}"
            )
    citation_title = soup.select_one('meta[name="citation_title"]')
    title = _clean_text(str(citation_title.get("content", ""))) if citation_title else ""
    if not title:
        title = _title_text(soup.select_one("#title, h1#title, h2#title"))
    abstract = _extract_abstract(soup)
    citation_pdf = soup.select_one('meta[name="citation_pdf_url"]')
    pdf = soup.select_one('a[href$=".pdf"], a[href*=".pdf?"]')
    authors = [
        _clean_text(str(node.get("content", "")))
        for node in soup.select('meta[name="citation_author"]')
        if _clean_text(str(node.get("content", "")))
    ]
    parser_mode = "专用解析"
    if not title:
        title = _title_text(soup.select_one("h1, h2, title"))
        parser_mode = "通用兜底解析"
    if not abstract:
        for heading in soup.find_all(["h2", "h3", "h4", "h5", "h6", "strong"]):
            if _text(heading).casefold() == "abstract":
                abstract = _text(heading.find_next("p"))
                parser_mode = "通用兜底解析"
                break
    if not title:
        raise ValueError("无法解析论文标题")
    if not identity and not citation_title and not soup.select_one("#title"):
        raise ValueError("页面缺少 ACL Anthology 论文元数据")
    pdf_url = ""
    if citation_pdf and citation_pdf.get("content"):
        pdf_url = urljoin(canonical_url, str(citation_pdf.get("content")))
    elif pdf:
        pdf_url = urljoin(canonical_url, str(pdf.get("href")))
    expected_pdf_url = canonical_url.rstrip("/") + ".pdf"
    if pdf_url and pdf_url.split("?", 1)[0] != expected_pdf_url:
        raise ValueError("论文 PDF 链接与 Anthology ID 不一致")
    return _translated_paper(
        title,
        abstract,
        pdf_url,
        canonical_url,
        authors,
        parser_mode,
        log,
    )


def make_fetcher(output_dir: Path) -> Fetcher:
    return Fetcher(RequestConfig(), [ACL_DOMAIN, ACM_DOMAIN], output_dir / "cache")


def write_csv(destination: Path, papers: list[dict[str, object]]) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".papers.", text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8-sig", newline="") as stream:
            fields = [
                "title",
                "title_zh",
                "authors",
                "pdf_url",
                "abstract_en",
                "abstract_zh",
                "url",
                "status",
                "error",
                "parser_mode",
                "translation_failed",
                "translation_error",
                "title_translation_failed",
                "title_translation_error",
                "collected_at",
            ]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for paper in papers:
                row = {field: paper.get(field, "") for field in fields}
                authors = row.get("authors")
                if isinstance(authors, list):
                    row["authors"] = "; ".join(str(author) for author in authors)
                writer.writerow(row)
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return destination
