"""凭证 / 更正 / 入账 / 下期差额的 SQLite 持久化。

所有状态推进都在 ``BEGIN IMMEDIATE`` 事务内完成，配合 revision
乐观锁与差额去重唯一约束，保证：
- 更正与入账并发时结果可串行化；
- 重试不会重复记录差额。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


VOUCHER_SCHEMA = """
CREATE TABLE IF NOT EXISTS vouchers(
  id TEXT PRIMARY KEY,
  run_id TEXT,
  scope TEXT NOT NULL DEFAULT '',
  period TEXT NOT NULL DEFAULT '',
  building_id TEXT NOT NULL DEFAULT '',
  next_period TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'OPEN',
  revision INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  current_q TEXT NOT NULL DEFAULT '{}',
  original_q TEXT NOT NULL DEFAULT '{}',
  posted_q TEXT,
  posted_version INTEGER,
  posted_at TEXT,
  expires_at TEXT,
  post_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, scope, period, building_id)
);
CREATE TABLE IF NOT EXISTS voucher_versions(
  id TEXT PRIMARY KEY,
  voucher_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  q TEXT NOT NULL,
  correction_id TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(voucher_id, seq)
);
CREATE TABLE IF NOT EXISTS corrections(
  id TEXT PRIMARY KEY,
  voucher_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  status TEXT NOT NULL,
  idem_key TEXT,
  reason TEXT NOT NULL DEFAULT '',
  raw TEXT NOT NULL DEFAULT '{}',
  candidate TEXT NOT NULL DEFAULT '{}',
  before_q TEXT NOT NULL DEFAULT '{}',
  after_q TEXT NOT NULL DEFAULT '{}',
  diff TEXT NOT NULL DEFAULT '{}',
  report TEXT NOT NULL DEFAULT '{}',
  effect TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(voucher_id, seq)
);
CREATE TABLE IF NOT EXISTS differences(
  id TEXT PRIMARY KEY,
  voucher_id TEXT NOT NULL,
  correction_id TEXT NOT NULL,
  run_id TEXT,
  period TEXT NOT NULL DEFAULT '',
  next_period TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT '',
  primary_field TEXT NOT NULL DEFAULT 'quantity_wh',
  wh INTEGER NOT NULL DEFAULT 0,
  amounts TEXT NOT NULL DEFAULT '{}',
  dedup_key TEXT NOT NULL UNIQUE,
  idem_key TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idem_responses(
  scope TEXT NOT NULL,
  idem_key TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(scope, idem_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_corrections_idem
  ON corrections(voucher_id, idem_key)
  WHERE idem_key IS NOT NULL AND status IN ('APPLIED','CARRIED');
"""


def init_voucher_tables(db):
    db.executescript(VOUCHER_SCHEMA)


class VoucherStore:
    def __init__(self, path):
        raw = str(path)
        if raw == ":memory:":
            # 每个连接都是独立内存库，而本 Store 按操作开连接，
            # 因此映射到唯一临时文件以保留 schema 与数据。
            import os
            import tempfile
            fd, raw = tempfile.mkstemp(prefix="voucher_", suffix=".sqlite3")
            os.close(fd)
        self.path = Path(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=8)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def immediate(self):
        db = self.connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def init(self):
        with self.connect() as db:
            init_voucher_tables(db)

    # ---------- 凭证读取 ----------

    @staticmethod
    def _voucher(row):
        if row is None:
            return None
        item = dict(row)
        for key in ("current_q", "original_q", "posted_q"):
            if item.get(key):
                item[key.removesuffix("_q") + "_block"] = json.loads(item[key])
        return item

    def get_voucher_row(self, voucher_id, db=None):
        own = db is None
        db = db or self.connect()
        try:
            row = db.execute("SELECT * FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
            return self._voucher(row)
        finally:
            if own:
                db.close()

    def get_voucher(self, voucher_id):
        return self.get_voucher_row(voucher_id)

    def find_voucher(self, run_id=None, period=None, building_id="", scope="", db=None):
        own = db is None
        db = db or self.connect()
        try:
            query = "SELECT * FROM vouchers WHERE 1=1"
            args = []
            if run_id is not None:
                query += " AND run_id IS ?"
                args.append(run_id)
            if period:
                query += " AND period=?"
                args.append(period)
            if building_id is not None:
                query += " AND building_id=?"
                args.append(building_id or "")
            if scope:
                query += " AND scope=?"
                args.append(scope)
            query += " ORDER BY created_at ASC LIMIT 1"
            row = db.execute(query, args).fetchone()
            return self._voucher(row)
        finally:
            if own:
                db.close()

    def list_vouchers(self, run_id=None, period=None):
        with self.connect() as db:
            query = "SELECT * FROM vouchers WHERE 1=1"
            args = []
            if run_id:
                query += " AND run_id=?"
                args.append(run_id)
            if period:
                query += " AND period=?"
                args.append(period)
            query += " ORDER BY created_at DESC"
            return [self._voucher(row) for row in db.execute(query, args).fetchall()]

    def insert_voucher(self, *, voucher_id, run_id, scope, period, next_period,
                       building_id, status, revision, version, current_block,
                       original_block, expires_at=None, idem_key=None):
        ts = now_iso()
        with self.immediate() as db:
            db.execute(
                """INSERT INTO vouchers(id,run_id,scope,period,building_id,next_period,
                     status,revision,version,current_q,original_q,expires_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (voucher_id, run_id, scope, period, building_id or "", next_period,
                 status, revision, version,
                 json.dumps(current_block, ensure_ascii=False),
                 json.dumps(original_block, ensure_ascii=False),
                 expires_at, ts, ts),
            )
            db.execute(
                """INSERT INTO voucher_versions(id,voucher_id,seq,kind,q,created_at)
                   VALUES(?,?,1,'ORIGINAL',?,?)""",
                (new_id("ver_"), voucher_id,
                 json.dumps(original_block, ensure_ascii=False), ts),
            )
        return self.get_voucher(voucher_id)

    # ---------- 版本 ----------

    def list_versions(self, voucher_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM voucher_versions WHERE voucher_id=? ORDER BY seq",
                (voucher_id,)).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                item["block"] = json.loads(item.pop("q"))
                out.append(item)
            return out

    def get_version(self, voucher_id, seq):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM voucher_versions WHERE voucher_id=? AND seq=?",
                (voucher_id, seq)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["block"] = json.loads(item.pop("q"))
            return item

    # ---------- 更正 ----------

    def get_correction(self, correction_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM corrections WHERE id=?", (correction_id,)).fetchone()
            return self._correction(row)

    def list_corrections(self, voucher_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM corrections WHERE voucher_id=? ORDER BY seq",
                (voucher_id,)).fetchall()
            return [self._correction(row) for row in rows]

    @staticmethod
    def _correction(row):
        if row is None:
            return None
        item = dict(row)
        for key in ("raw", "candidate", "before_q", "after_q", "diff", "report"):
            if item.get(key):
                item[key] = json.loads(item[key])
        item["before_block"] = item.pop("before_q", {})
        item["after_block"] = item.pop("after_q", {})
        return item

    def find_correction_by_idem(self, voucher_id, idem_key):
        if not idem_key:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM corrections WHERE voucher_id=? AND idem_key=? ORDER BY seq LIMIT 1",
                (voucher_id, idem_key)).fetchone()
            return self._correction(row)

    # ---------- 差额 ----------

    def list_differences(self, voucher_id=None, next_period=None, run_id=None):
        with self.connect() as db:
            query = "SELECT * FROM differences WHERE 1=1"
            args = []
            if voucher_id:
                query += " AND voucher_id=?"
                args.append(voucher_id)
            if next_period:
                query += " AND next_period=?"
                args.append(next_period)
            if run_id:
                query += " AND run_id=?"
                args.append(run_id)
            query += " ORDER BY created_at"
            out = []
            for row in db.execute(query, args).fetchall():
                item = dict(row)
                item["amounts"] = json.loads(item["amounts"])
                out.append(item)
            return out

    # ---------- 幂等响应 ----------

    def get_idem(self, scope, idem_key):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM idem_responses WHERE scope=? AND idem_key=?",
                (scope, idem_key)).fetchone()
            return dict(row) if row else None

    def save_idem(self, scope, idem_key, status_code, body):
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO idem_responses(scope,idem_key,status_code,body,created_at)
                   VALUES(?,?,?,?,?)""",
                (scope, idem_key, status_code,
                 json.dumps(body, ensure_ascii=False), now_iso()),
            )
