"""資料庫連線工具。"""
import sys
from pathlib import Path

import psycopg2
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config


def get_dsn() -> str:
    return (
        f"host={config.DB_HOST} port={config.DB_PORT} "
        f"dbname={config.DB_NAME} user={config.DB_USER} password={config.DB_PASSWORD}"
    )


def get_connection():
    """回傳 psycopg2 連線，用於批次寫入（execute_values）。"""
    return psycopg2.connect(get_dsn())


def get_engine():
    """回傳 SQLAlchemy engine，用於 geopandas / pandas 讀寫。"""
    url = (
        f"postgresql+psycopg2://{config.DB_USER}:{config.DB_PASSWORD}"
        f"@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}"
    )
    return create_engine(url)
