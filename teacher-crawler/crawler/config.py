from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class SelectorConfig(BaseModel):
    name: list[str] = Field(default_factory=lambda: ["h1", ".name", ".teacher-name"])
    category: list[str] = Field(default_factory=lambda: [".category", ".teacher-category"])
    title: list[str] = Field(default_factory=lambda: [".title", ".position", ".teacher-title"])
    email: list[str] = Field(default_factory=lambda: ["a[href^='mailto:']", ".email"])
    phone: list[str] = Field(default_factory=lambda: ["a[href^='tel:']", ".phone", ".telephone"])
    department: list[str] = Field(default_factory=lambda: [".department", ".affiliation"])
    research: list[str] = Field(
        default_factory=lambda: [".research-interests", ".research", ".research-direction"]
    )
    admission_info: list[str] = Field(
        default_factory=lambda: [".admission-info", ".student-recruitment"]
    )
    sections: dict[str, list[str]] = Field(default_factory=dict)
    body: list[str] = Field(default_factory=lambda: ["main", "article", ".content", ".teacher-detail"])
    photo: list[str] = Field(default_factory=lambda: [".profile img", ".teacher-photo img", "main img"])


class DiscoveryConfig(BaseModel):
    teacher_link_selector: str | None = None
    url_patterns: list[str] = Field(default_factory=list)
    link_selectors: list[str] = Field(default_factory=lambda: ["a[href]"])
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_discovery(self) -> "DiscoveryConfig":
        if self.teacher_link_selector:
            self.link_selectors = [self.teacher_link_selector]
        if self.url_patterns:
            self.include_patterns = self.url_patterns
        return self


class ProfileConfig(BaseModel):
    container: str
    name: str
    category_from_url: dict[str, str] = Field(default_factory=dict)
    department: str | None = None
    summary: str | None = None
    content: str
    photo: str | None = None


class RequestConfig(BaseModel):
    delay_seconds: float = Field(default=1.0, ge=0)
    timeout_seconds: float = Field(default=30.0, gt=0)
    retries: int = Field(default=3, ge=1, le=10)
    user_agent: str = "TeacherResearchCrawler/1.0 contact@example.com"
    verify_ssl: bool = True
    use_browser: bool = False


class SchoolConfig(BaseModel):
    school: str
    college: str
    start_urls: list[str] = Field(default_factory=list)
    base_url: str | None = None
    directory_urls: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    profile: ProfileConfig | None = None
    selectors: SelectorConfig = Field(default_factory=SelectorConfig)
    request: RequestConfig = Field(default_factory=RequestConfig)
    output_dir: Path = Path("../output")

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        return value.rstrip("/")

    @model_validator(mode="after")
    def normalize_urls_and_domains(self) -> "SchoolConfig":
        if not self.start_urls:
            self.start_urls = list(self.directory_urls)
        if not self.start_urls:
            raise ValueError("start_urls must contain at least one URL")
        if not self.base_url:
            parsed = urlparse(self.start_urls[0])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("base_url is required when start_urls are relative")
            self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        self.directory_urls = list(self.start_urls)
        if not self.allowed_domains:
            host = urlparse(self.base_url).hostname
            self.allowed_domains = [host] if host else []
        self.allowed_domains = [domain.lower() for domain in self.allowed_domains]
        return self


def load_config(path: str | Path) -> dict[str, Any]:
    """Read and validate a school YAML file, returning a plain Python dictionary."""
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a YAML mapping")
    for field in ("start_urls", "allowed_domains"):
        value = raw.get(field)
        if not isinstance(value, list) or not value:
            raise ValueError(f"configuration requires a non-empty '{field}' list")
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError(f"configuration field '{field}' must contain non-empty strings")
    config = SchoolConfig.model_validate(raw)
    if not config.output_dir.is_absolute():
        config.output_dir = (config_path.parent / config.output_dir).resolve()
    return config.model_dump(mode="python")


def load_school_config(path: str | Path) -> SchoolConfig:
    """Return the typed internal representation used by the crawler."""
    return SchoolConfig.model_validate(load_config(path))
