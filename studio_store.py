"""SQLite persistence for the library, API defaults and each series."""
from __future__ import annotations
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from datetime import datetime


@contextmanager
def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA foreign_keys=ON')
        with db:
            yield db
    finally:
        db.close()


def save_document(path, document):
    with connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS series_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS episodes (day INTEGER PRIMARY KEY, title TEXT NOT NULL, data TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('DELETE FROM series_settings')
        db.executemany('INSERT INTO series_settings VALUES (?,?)', [(k, json.dumps(v, ensure_ascii=False)) for k, v in document['settings'].items()])
        db.execute('DELETE FROM episodes')
        db.executemany('INSERT INTO episodes VALUES (?,?,?)', [(e['day'], e.get('title', ''), json.dumps(e, ensure_ascii=False)) for e in document.get('episodes', [])])
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('updated_at', ?)", (datetime.now().isoformat(),))


def load_document(path):
    with connect(path) as db:
        settings = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM series_settings')}
        episodes = [json.loads(row[0]) for row in db.execute('SELECT data FROM episodes ORDER BY day')]
    return {'version': 2, 'settings': settings, 'episodes': episodes}


class Library:
    def __init__(self, path):
        self.path = Path(path)
        with connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS series (id INTEGER PRIMARY KEY, name TEXT NOT NULL, project_path TEXT UNIQUE NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def set(self, key, value):
        with connect(self.path) as db:
            db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def get(self, key, default=None):
        with connect(self.path) as db:
            row = db.execute('SELECT value FROM preferences WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def register(self, path, name):
        with connect(self.path) as db:
            db.execute('INSERT INTO series (name,project_path) VALUES (?,?) ON CONFLICT(project_path) DO UPDATE SET name=excluded.name', (name, str(Path(path).resolve())))

    def series(self):
        with connect(self.path) as db:
            return list(db.execute('SELECT name,project_path FROM series ORDER BY id DESC'))


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]


def protect_secret(text, decrypt=False):
    """Windows DPAPI ties saved API credentials to the current Windows account."""
    if not text:
        return ''
    if os.name != 'nt':
        raise RuntimeError('การจำ API key รองรับ Windows DPAPI เท่านั้น ใช้ environment variable บนระบบอื่น')
    data = base64.b64decode(text) if decrypt else text.encode('utf-8')
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    outgoing = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    # Explicit pointer types also keep LocalFree safe on 64-bit Python.
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = ctypes.string_at(outgoing.pbData, outgoing.cbData)
        return result.decode('utf-8') if decrypt else base64.b64encode(result).decode('ascii')
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))
