from __future__ import annotations

import csv
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .models import TeacherResearch

CSV_FIELDS = [
    "name",
    "title",
    "research_dir",
    "email",
    "lab",
    "bio",
    "homepage_url",
    "recruit_text",
    "has_recruit_info",
    "note",
]
SORT_FIELDS = {"name", "title", "research_dir", "recruit_score", "contact_status"}
CONTACT_STATUSES = {"未联系", "待联系", "已发邮件", "已回复", "暂不考虑"}


class TeacherRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS teachers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    research_dir TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    lab TEXT NOT NULL DEFAULT '',
                    bio TEXT NOT NULL DEFAULT '',
                    homepage_url TEXT NOT NULL UNIQUE,
                    recruit_text TEXT NOT NULL DEFAULT '',
                    has_recruit_info INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    recruit_types TEXT NOT NULL DEFAULT '',
                    recruit_score INTEGER NOT NULL DEFAULT 0,
                    contact_status TEXT NOT NULL DEFAULT '未联系',
                    favorite INTEGER NOT NULL DEFAULT 0,
                    html_path TEXT NOT NULL DEFAULT '',
                    html_hash TEXT NOT NULL DEFAULT '',
                    parsed_at TEXT NOT NULL
                )
                """
            )

    def upsert(self, record: TeacherResearch) -> None:
        values = record.model_dump(mode="json")
        values["recruit_types"] = "|".join(record.recruit_types)
        values["has_recruit_info"] = int(record.has_recruit_info)
        values["favorite"] = int(record.favorite)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO teachers (
                    name, title, research_dir, email, lab, bio, homepage_url,
                    recruit_text, has_recruit_info, note, recruit_types,
                    recruit_score, contact_status, favorite, html_path, html_hash, parsed_at
                ) VALUES (
                    :name, :title, :research_dir, :email, :lab, :bio, :homepage_url,
                    :recruit_text, :has_recruit_info, :note, :recruit_types,
                    :recruit_score, :contact_status, :favorite, :html_path, :html_hash, :parsed_at
                )
                ON CONFLICT(homepage_url) DO UPDATE SET
                    name=excluded.name, title=excluded.title,
                    research_dir=excluded.research_dir, email=excluded.email,
                    lab=excluded.lab, bio=excluded.bio,
                    recruit_text=excluded.recruit_text,
                    has_recruit_info=excluded.has_recruit_info,
                    recruit_types=excluded.recruit_types,
                    recruit_score=excluded.recruit_score,
                    html_path=excluded.html_path, html_hash=excluded.html_hash,
                    parsed_at=excluded.parsed_at
                """,
                values,
            )

    def list(
        self,
        query: str = "",
        recruit_type: str = "",
        has_recruit_info: bool | None = None,
        has_email: bool | None = None,
        favorite: bool | None = None,
        contact_status: str = "",
        sort: str = "recruit_score",
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if query:
            clauses.append("(name LIKE ? OR research_dir LIKE ? OR lab LIKE ? OR recruit_text LIKE ?)")
            values.extend([f"%{query}%"] * 4)
        if recruit_type:
            clauses.append("('|' || recruit_types || '|') LIKE ?")
            values.append(f"%|{recruit_type}|%")
        if has_recruit_info is not None:
            clauses.append("has_recruit_info = ?")
            values.append(int(has_recruit_info))
        if has_email is not None:
            clauses.append("email != ''" if has_email else "email = ''")
        if favorite is not None:
            clauses.append("favorite = ?")
            values.append(int(favorite))
        if contact_status:
            clauses.append("contact_status = ?")
            values.append(contact_status)
        sort_field = sort if sort in SORT_FIELDS else "recruit_score"
        sort_order = "ASC" if order.lower() == "asc" else "DESC"
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM teachers {where} ORDER BY {sort_field} {sort_order}, name ASC",
                values,
            ).fetchall()
        return [self._serialize(row) for row in rows]

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["has_recruit_info"] = bool(result["has_recruit_info"])
        result["favorite"] = bool(result["favorite"])
        result["recruit_types"] = [value for value in result["recruit_types"].split("|") if value]
        return result

    def update_manual(
        self, teacher_id: int, note: str, contact_status: str, favorite: bool
    ) -> dict[str, Any]:
        if contact_status not in CONTACT_STATUSES:
            raise ValueError("无效的联系状态")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE teachers SET note=?, contact_status=?, favorite=? WHERE id=?",
                (note[:2000], contact_status, int(favorite), teacher_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(teacher_id)
            row = connection.execute("SELECT * FROM teachers WHERE id=?", (teacher_id,)).fetchone()
        return self._serialize(row)

    def export_csv(self, destination: Path, rows: list[dict[str, Any]] | None = None) -> Path:
        records = rows if rows is not None else self.list(sort="name", order="asc")
        handle, temp_name = tempfile.mkstemp(dir=destination.parent, prefix=".teachers.", text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
                writer.writeheader()
                for record in records:
                    writer.writerow({field: record.get(field, "") for field in CSV_FIELDS})
            os.replace(temp_name, destination)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return destination
