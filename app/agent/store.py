"""DB 连接辅助（与 main.py 共用同一 SQLite 文件）"""
import sqlite3
from contextlib import contextmanager

from .config import DB_PATH


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
