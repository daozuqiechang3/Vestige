from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Teacher(BaseModel):
    school: str
    college: str
    name: str
    category: str | None = None
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    department: str | None = None
    research_interests: list[str] = Field(default_factory=list)
    admission_info: str | None = None
    sections: dict[str, str] = Field(default_factory=dict)
    full_text: str = ""
    photo_url: str | None = None
    profile_url: str
    collected_at: datetime = Field(default_factory=datetime.now)


class TeacherResearch(BaseModel):
    name: str
    title: str = ""
    research_dir: str = ""
    email: str = ""
    lab: str = ""
    bio: str = ""
    homepage_url: str
    recruit_text: str = ""
    has_recruit_info: bool = False
    note: str = ""
    recruit_types: list[str] = Field(default_factory=list)
    recruit_score: int = 0
    contact_status: str = "未联系"
    favorite: bool = False
    html_path: str = ""
    html_hash: str = ""
    parsed_at: datetime = Field(default_factory=datetime.now)


class CrawlState(BaseModel):
    completed: dict[str, str] = Field(default_factory=dict)
    duplicates: dict[str, str] = Field(default_factory=dict)
    failed: dict[str, str] = Field(default_factory=dict)
    failed_names: dict[str, str] = Field(default_factory=dict)
    content_hashes: dict[str, str] = Field(default_factory=dict)
