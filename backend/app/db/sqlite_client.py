import sqlite3
from app.utils.path_helper import get_runtime_path

def get_connection():
    return sqlite3.connect(get_runtime_path("vidinsight.db"))
