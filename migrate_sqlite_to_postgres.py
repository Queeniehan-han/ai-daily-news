from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Iterable, Type

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    CustomSource,
    RawNewsItemRecord,
    Run,
    RunLog,
    SourceReportRecord,
    StructuredNewsRecord,
)


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def copy_rows(
    source: Session,
    target: Session,
    model: Type,
    *,
    omit: Iterable[str] = (),
    clear_api_key: bool = False,
) -> int:
    omitted = set(omit)
    columns = [column.name for column in model.__table__.columns
               if column.name not in omitted]
    count = 0
    for row in source.query(model).yield_per(500):
        values = {column: getattr(row, column) for column in columns}
        if clear_api_key and "api_key" in values:
            values["api_key"] = ""
        target.add(model(**values))
        count += 1
        if count % 500 == 0:
            target.flush()
    target.commit()
    return count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="一次性将本地 AI 新闻 SQLite 历史迁移到 Railway PostgreSQL。"
    )
    parser.add_argument(
        "--source",
        default="data/ai_news.sqlite3",
        help="SQLite 文件路径（默认：data/ai_news.sqlite3）",
    )
    parser.add_argument(
        "--include-source-api-keys",
        action="store_true",
        help="同时迁移信息源管理页保存的 AnySearch/Tavily API Key。",
    )
    args = parser.parse_args()

    source_path = Path(args.source).expanduser().resolve()
    if not source_path.is_file():
        raise SystemExit(f"SQLite 文件不存在：{source_path}")

    target_url = normalize_database_url(os.getenv("DATABASE_URL", "").strip())
    if not target_url.startswith("postgresql+psycopg://"):
        raise SystemExit("DATABASE_URL 必须指向 PostgreSQL。")

    source_engine = create_engine(f"sqlite:///{source_path}")
    target_engine = create_engine(target_url, pool_pre_ping=True)
    Base.metadata.create_all(bind=target_engine)
    SourceSession = sessionmaker(bind=source_engine, autoflush=False)
    TargetSession = sessionmaker(bind=target_engine, autoflush=False)

    source = SourceSession()
    target = TargetSession()
    try:
        if target.query(Run).count() or target.query(CustomSource).count():
            raise SystemExit(
                "目标 PostgreSQL 已有任务或信息源数据，已中止以避免重复迁移。"
            )

        results = {
            "runs": copy_rows(source, target, Run),
            "run_logs": copy_rows(source, target, RunLog, omit={"id"}),
            "raw_news_items": copy_rows(
                source, target, RawNewsItemRecord, omit={"id"}),
            "source_reports": copy_rows(
                source, target, SourceReportRecord, omit={"id"}),
            "structured_news": copy_rows(
                source, target, StructuredNewsRecord, omit={"id"}),
            "custom_sources": copy_rows(
                source,
                target,
                CustomSource,
                omit={"id"},
                clear_api_key=not args.include_source_api_keys,
            ),
        }
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()
        source_engine.dispose()
        target_engine.dispose()

    print("迁移完成：")
    for table, count in results.items():
        print(f"  {table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
