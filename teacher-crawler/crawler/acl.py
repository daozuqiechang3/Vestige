from __future__ import annotations

import csv
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import RequestConfig
from .fetcher import Fetcher
from .nllb import get_nllb_translator

ACL_DOMAIN = "aclanthology.org"
ACL_PAPER_PATH_RE = re.compile(r"/\d{4}\.[A-Za-z0-9-]+\.\d+/?")
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

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["title_en"] = data["title"]
        return data


def validate_volume_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != ACL_DOMAIN:
        raise ValueError("请输入有效的 ACL Anthology 文集或论文页面 URL")
    if not parsed.path.startswith("/volumes/") and not is_paper_url(url):
        raise ValueError("ACL 模式只接受文集卷页面或单篇论文页面 URL")
    return urldefrag(url)[0]


def is_paper_url(url: str) -> bool:
    return ACL_PAPER_PATH_RE.fullmatch(urlparse(url).path) is not None


def discover_papers(html: str, page_url: str) -> list[str]:
    """Return canonical paper detail-page URLs from an ACL volume page."""
    soup = BeautifulSoup(html, "lxml")
    papers: dict[str, None] = {}
    for link in soup.select("a[href]"):
        href = str(link.get("href", "")).strip()
        absolute, _ = urldefrag(urljoin(page_url, href))
        parsed = urlparse(absolute)
        if parsed.hostname != ACL_DOMAIN:
            continue
        if ACL_PAPER_PATH_RE.fullmatch(parsed.path) and not parsed.path.rstrip("/").endswith(".0"):
            papers.setdefault(absolute, None)
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
    title = _title_text(soup.select_one("#title, h1#title, h2#title"))
    abstract = _extract_abstract(soup)
    pdf = soup.select_one('a[href$=".pdf"], a[href*=".pdf?"]')
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
            log(f"英文摘要：{abstract}")
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
        pdf_url=urljoin(page_url, str(pdf.get("href"))) if pdf else "",
        abstract_en=abstract,
        abstract_zh=abstract_zh,
        url=page_url,
        parser_mode=parser_mode,
        translation_failed=translation_failed,
        translation_error=translation_error,
        title_translation_failed=title_translation_failed,
        title_translation_error=title_translation_error,
    )


def make_fetcher(output_dir: Path) -> Fetcher:
    return Fetcher(RequestConfig(), [ACL_DOMAIN], output_dir / "cache")


def write_csv(destination: Path, papers: list[dict[str, object]]) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".papers.", text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8-sig", newline="") as stream:
            fields = ["title", "title_zh", "pdf_url", "abstract_en", "abstract_zh", "status"]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for paper in papers:
                writer.writerow({field: paper.get(field, "") for field in fields})
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return destination
