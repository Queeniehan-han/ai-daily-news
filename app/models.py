from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Run(Base):
    __tablename__ = "runs"

    id = Column(String(32), primary_key=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    status = Column(String(32), default="pending", nullable=False, index=True)
    phase = Column(String(64), default="created", nullable=False)
    provider = Column(String(80), default="", nullable=False)
    model = Column(String(120), default="", nullable=False)
    categories_json = Column(Text, default="[]", nullable=False)
    raw_count = Column(Integer, default=0, nullable=False)
    report_count = Column(Integer, default=0, nullable=False)
    structured_count = Column(Integer, default=0, nullable=False)
    coverage_json = Column(Text, default="{}", nullable=False)
    error_text = Column(Text, default="", nullable=False)

    logs = relationship("RunLog", cascade="all, delete-orphan")
    raw_items = relationship("RawNewsItemRecord", cascade="all, delete-orphan")
    reports = relationship("SourceReportRecord", cascade="all, delete-orphan")
    structured_items = relationship("StructuredNewsRecord", cascade="all, delete-orphan")


class DailyCrawlClaim(Base):
    """Database-backed idempotency record for the 11:00 CST automatic crawl."""

    __tablename__ = "daily_crawl_claims"

    scheduled_date = Column(String(10), primary_key=True)
    run_id = Column(String(32), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class RunLog(Base):
    __tablename__ = "run_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(32), ForeignKey("runs.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    message = Column(Text, nullable=False)


class RawNewsItemRecord(Base):
    __tablename__ = "raw_news_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(32), ForeignKey("runs.id"), nullable=False, index=True)
    title = Column(Text, nullable=False)
    url = Column(Text, nullable=False)
    source = Column(String(240), default="", nullable=False)
    category = Column(String(120), default="", nullable=False)
    content = Column(Text, default="", nullable=False)
    published_at = Column(String(80), default="", nullable=False)
    scrape_strategy = Column(String(120), default="", nullable=False)
    payload_json = Column(Text, default="{}", nullable=False)


class SourceReportRecord(Base):
    __tablename__ = "source_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(32), ForeignKey("runs.id"), nullable=False, index=True)
    name = Column(String(240), nullable=False)
    category = Column(String(120), default="", nullable=False)
    source_type = Column(String(80), default="", nullable=False)
    count = Column(Integer, default=0, nullable=False)
    strategy = Column(String(160), default="", nullable=False)
    status = Column(String(80), default="", nullable=False)
    error = Column(Text, default="", nullable=False)
    issue_type = Column(String(80), default="", nullable=False)
    completeness = Column(String(80), default="", nullable=False)
    latest_seen_at = Column(String(80), default="", nullable=False)
    oldest_seen_at = Column(String(80), default="", nullable=False)
    payload_json = Column(Text, default="{}", nullable=False)


class StructuredNewsRecord(Base):
    __tablename__ = "structured_news"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(32), ForeignKey("runs.id"), nullable=False, index=True)
    event = Column(Text, nullable=False)
    detail = Column(Text, default="", nullable=False)
    impact = Column(Text, default="", nullable=False)
    news_type = Column(String(80), default="", nullable=False)
    company = Column(String(160), default="", nullable=False)
    source = Column(String(240), default="", nullable=False)
    url = Column(Text, default="", nullable=False)
    published_at = Column(String(80), default="", nullable=False)
    category = Column(String(120), default="", nullable=False)
    payload_json = Column(Text, default="{}", nullable=False)


class CustomSource(Base):
    __tablename__ = "custom_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    source_key = Column(String(320), default="", nullable=False, index=True)
    is_builtin = Column(Boolean, default=False, nullable=False)
    name = Column(String(240), nullable=False, unique=True)
    url = Column(Text, nullable=False)
    rss_url = Column(Text, default="", nullable=False)
    api_key = Column(Text, default="", nullable=False)
    category = Column(String(120), default="自定义信源", nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)

    def to_source_dict(self) -> dict:
        source = {
            "name": self.name,
            "url": self.url,
            "type": "web",
            "category": self.category or "自定义信源",
        }
        if self.rss_url:
            source["rss_url"] = self.rss_url
        return source
