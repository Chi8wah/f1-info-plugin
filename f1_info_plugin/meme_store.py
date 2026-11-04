"""Meme 图片、每群投递进度与任务执行记录的持久化。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

import hashlib
import os
import sqlite3
import tempfile
import time

from .meme_client import MemePost


MemeItemKey = tuple[str, str, str]


class MemeStorageFull(RuntimeError):
    pass


class MemeStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.images = root / "images"
        self.tmp = root / "tmp"
        self.database = root / "queue.sqlite3"
        self._lock = RLock()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        # 每次操作在线程内创建连接。事务与文件变更共用锁，绝不跨网络等待。
        with self._lock:
            connection = sqlite3.connect(self.database, timeout=5)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.create_function("digest_bytes", 1, bytes.fromhex, deterministic=True)
                connection.execute("BEGIN IMMEDIATE")
                with connection:
                    yield connection
            finally:
                connection.close()

    def initialize(self) -> None:
        self.images.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2, 3}:
                raise RuntimeError(f"不支持的 Meme 数据库版本：{version}")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sources (
                    source TEXT PRIMARY KEY, last_run_at REAL, added INTEGER NOT NULL DEFAULT 0,
                    pages INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS posts (
                    source TEXT NOT NULL, post_id TEXT NOT NULL, author_id TEXT NOT NULL,
                    author_handle TEXT NOT NULL,
                    published_at REAL NOT NULL, post_url TEXT NOT NULL, text TEXT NOT NULL,
                    PRIMARY KEY (source, post_id)
                );
                CREATE TABLE IF NOT EXISTS assets (
                    digest TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL,
                    mime TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'available', created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, post_id TEXT NOT NULL,
                    media_id TEXT NOT NULL, media_index INTEGER NOT NULL, url TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', digest TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                    UNIQUE (source, post_id, media_id),
                    FOREIGN KEY (source, post_id) REFERENCES posts(source, post_id),
                    FOREIGN KEY (digest) REFERENCES assets(digest)
                );
                CREATE INDEX IF NOT EXISTS items_digest ON items(digest);
                CREATE INDEX IF NOT EXISTS items_pending ON items(source,state);
                CREATE INDEX IF NOT EXISTS assets_state ON assets(state);
                CREATE TABLE IF NOT EXISTS deliveries (
                    target TEXT NOT NULL, digest TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', published_at REAL NOT NULL,
                    media_index INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    message_id TEXT, error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (target, digest), FOREIGN KEY (digest) REFERENCES assets(digest)
                );
                CREATE INDEX IF NOT EXISTS deliveries_queue ON deliveries(target, state, published_at);
                CREATE INDEX IF NOT EXISTS deliveries_asset ON deliveries(digest,state);
                CREATE TABLE IF NOT EXISTS runs (
                    key TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
                    scheduled_at REAL NOT NULL, started_at REAL NOT NULL,
                    finished_at REAL, state TEXT NOT NULL DEFAULT 'running',
                    digest TEXT, added INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS runs_owner ON runs(kind,owner,scheduled_at DESC);
            """)
            db.execute("BEGIN IMMEDIATE")
            if "author_handle" not in {row["name"] for row in db.execute("PRAGMA table_info(posts)")}:
                # 旧版只采集来源账号本人的图片，因此可用已保存的来源补齐历史署名。
                db.execute("ALTER TABLE posts ADD COLUMN author_handle TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE posts SET author_handle=source")
            # 与队列明细分开保存，图片和帖子清理后仍能按群去重。
            db.execute("""CREATE TABLE IF NOT EXISTS sent_history (
                target TEXT NOT NULL, digest BLOB NOT NULL,
                PRIMARY KEY (target, digest)
            ) WITHOUT ROWID""")
            # 退出前已发起的 RPC 可能已送达，恢复时记为结果不明，避免自动重发。
            db.execute("UPDATE deliveries SET state='uncertain', error='发送期间插件退出，结果不明' WHERE state='sending'")
            db.execute("UPDATE runs SET state=CASE WHEN kind='send' THEN 'uncertain' ELSE 'interrupted' END, finished_at=? WHERE state='running'", (time.time(),))
            db.execute("""INSERT OR IGNORE INTO sent_history(target,digest)
                SELECT target,digest_bytes(digest) FROM deliveries WHERE state IN ('sent','uncertain')""")
            if version < 3:
                # 旧库曾按单条投递的发布时间跳过图片。保留仍有有效帖子关联的投递。
                db.execute("UPDATE deliveries SET state='pending',error='' WHERE state='skipped' AND error='图片过期'")
                db.execute("UPDATE items SET state='pending' WHERE state='expired'")
            db.execute("PRAGMA user_version=3")
            for row in db.execute("SELECT digest,path FROM assets WHERE state='available'").fetchall():
                if not self._asset_path(row["path"]).is_file():
                    db.execute("UPDATE assets SET state='missing',path='' WHERE digest=?", (row["digest"],))
                    db.execute("UPDATE items SET state='pending',attempts=0,error='本地图片缺失' WHERE digest=?", (row["digest"],))
            referenced = {row[0] for row in db.execute("SELECT path FROM assets WHERE path<>''")}
            for path in self.images.iterdir():
                if path.is_file() and f"images/{path.name}" not in referenced:
                    path.unlink()
            for path in self.tmp.iterdir():
                if path.is_file():
                    path.unlink()

    def _asset_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if path.parent != self.images.resolve():
            raise RuntimeError("Meme 数据库中的图片路径超出图片目录")
        return path

    def known_posts(self, source: str) -> set[str]:
        with self._db() as db:
            return {row[0] for row in db.execute("SELECT post_id FROM posts WHERE source=?", (source,))}

    def has_empty_queue(self, targets: list[str], sources: list[str], cutoff: float) -> bool:
        if not targets or not sources:
            return False
        with self._db() as db:
            # 只统计当前来源的有效可发图；正在发送的图仍算库存，避免保存时额外补采。
            nonempty = {row[0] for row in db.execute(f"""SELECT DISTINCT d.target
                FROM deliveries d JOIN assets a USING(digest)
                WHERE d.target IN ({self._placeholders(targets)}) AND d.state IN ('pending','sending')
                AND a.state='available' AND EXISTS(
                    SELECT 1 FROM items i JOIN posts p USING(source,post_id)
                    WHERE i.digest=d.digest AND i.source IN ({self._placeholders(sources)}) AND p.published_at>=?)""",
                [*targets, *sources, cutoff])}
            return any(target not in nonempty for target in targets)

    def record_post(self, source: str, post: MemePost) -> None:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO posts(source,post_id,author_id,author_handle,published_at,post_url,text) VALUES (?,?,?,?,?,?,?)", (source, post.post_id, post.author_id, post.author_handle, post.published_at, post.url, post.text))
            for photo in post.photos:
                db.execute("INSERT OR IGNORE INTO items(source,post_id,media_id,media_index,url) VALUES (?,?,?,?,?)", (source, post.post_id, photo.media_id, photo.index, photo.url))

    def pending_items(self, source: str, cutoff: float, post_id: str | None = None) -> list[dict[str, Any]]:
        sql = """SELECT i.*,p.published_at FROM items i JOIN posts p USING(source,post_id)
                 WHERE i.source=? AND i.state='pending' AND i.attempts<3 AND p.published_at>=?"""
        params: list[Any] = [source, cutoff]
        if post_id is not None:
            sql += " AND i.post_id=?"
            params.append(post_id)
        sql += " ORDER BY p.published_at DESC,i.post_id DESC,i.media_index"
        with self._db() as db:
            return [dict(row) for row in db.execute(sql, params)]

    def download_failed(self, item_key: MemeItemKey, error: str) -> None:
        with self._db() as db:
            db.execute("""UPDATE items SET attempts=attempts+1,error=?,
                state=CASE WHEN attempts+1>=3 THEN 'failed' ELSE state END
                WHERE (source,post_id,media_id)=(?,?,?) AND state='pending'""", (error[:500], *item_key))

    def start_run(self, key: str, kind: str, owner: str, scheduled_at: float) -> bool:
        # 以计划时点持久化去重，插件重载后仍沿用原有执行记录。
        with self._db() as db:
            return db.execute("INSERT OR IGNORE INTO runs(key,kind,owner,scheduled_at,started_at) VALUES (?,?,?,?,?)", (key, kind, owner, scheduled_at, time.time())).rowcount == 1

    def finish_run(self, key: str, state: str, detail: str = "") -> None:
        with self._db() as db:
            db.execute("UPDATE runs SET state=?,detail=?,finished_at=? WHERE key=?", (state, detail[:500], time.time(), key))

    def finish_fetch(self, key: str, source: str, pages: int, reason: str, error: str = "") -> int:
        with self._db() as db:
            run = db.execute("SELECT added FROM runs WHERE key=?", (key,)).fetchone()
            added = run[0] if run else 0
            db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET last_run_at=excluded.last_run_at,added=excluded.added,pages=excluded.pages,reason=excluded.reason,error=excluded.error", (source, time.time(), added, pages, reason, error[:500]))
            db.execute("UPDATE runs SET state=?,detail=?,finished_at=? WHERE key=?", ("failed" if error else "done", f"{reason}: {error}"[:500], time.time(), key))
            return added

    @staticmethod
    def _placeholders(values: list[str]) -> str:
        return ",".join("?" for _ in values)

    def _sync_targets(self, db: sqlite3.Connection, targets: list[str], sources: list[str], cutoff: float, cap: int) -> None:
        # 文件全局共用，投递按群和哈希独立去重；新群只补当前有效来源的库存。
        if not sources:
            return
        source_sql = self._placeholders(sources)
        for target in targets:
            db.execute(f"""INSERT OR IGNORE INTO deliveries(target,digest,published_at,media_index)
                SELECT ?,a.digest,MIN(p.published_at),MIN(i.media_index)
                FROM assets a JOIN items i ON i.digest=a.digest JOIN posts p USING(source,post_id)
                WHERE a.state='available' AND i.source IN ({source_sql}) AND p.published_at>=?
                AND NOT EXISTS(SELECT 1 FROM sent_history h WHERE h.target=? AND h.digest=digest_bytes(a.digest))
                GROUP BY a.digest""", [target, *sources, cutoff, target])
            # 仅淘汰当前来源下最旧的待发项，不触碰发送中的图片。
            overflow = db.execute(f"""SELECT d.digest FROM deliveries d WHERE d.target=? AND d.state='pending'
                AND EXISTS(SELECT 1 FROM items i JOIN posts p USING(source,post_id)
                    WHERE i.digest=d.digest AND i.source IN ({source_sql}) AND p.published_at>=?)
                ORDER BY d.published_at DESC,d.media_index DESC,d.digest DESC LIMIT -1 OFFSET ?""", [target, *sources, cutoff, cap]).fetchall()
            db.executemany("UPDATE deliveries SET state='skipped',error='队列容量上限' WHERE target=? AND digest=?", [(target, row[0]) for row in overflow])

    def sync_targets(self, targets: list[str], sources: list[str], cutoff: float, cap: int) -> None:
        with self._db() as db:
            self._sync_targets(db, targets, sources, cutoff, cap)

    def _retire_asset(self, db: sqlite3.Connection, row: sqlite3.Row) -> None:
        if row["path"]:
            self._asset_path(row["path"]).unlink(missing_ok=True)
        db.execute("UPDATE assets SET path='',state='purged' WHERE digest=?", (row["digest"],))

    def cleanup(self, cutoff: float) -> None:
        # 先提交明细清理和库存变更，文件删除失败时保留路径供下次重试。
        orphaned = """NOT EXISTS(SELECT 1 FROM items i WHERE i.digest=assets.digest)
            AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.digest=assets.digest)"""
        with self._db() as db:
            db.execute("DELETE FROM runs WHERE state<>'running' AND finished_at<?", (cutoff,))
            db.execute("DELETE FROM sources WHERE last_run_at<?", (cutoff,))
            # 一张图可能被新旧帖子共同引用。发送中的图片及其引用留到回执收尾。
            db.execute("""DELETE FROM items
                WHERE (source,post_id) IN (SELECT source,post_id FROM posts WHERE published_at<?)
                AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.digest=items.digest AND d.state='sending')""", (cutoff,))
            db.execute("""DELETE FROM posts WHERE published_at<?
                AND NOT EXISTS(SELECT 1 FROM items i WHERE (i.source,i.post_id)=(posts.source,posts.post_id))""", (cutoff,))
            db.execute("""DELETE FROM deliveries WHERE state<>'sending'
                AND NOT EXISTS(SELECT 1 FROM items i WHERE i.digest=deliveries.digest)""")
            db.execute("""UPDATE deliveries SET (published_at,media_index)=(
                SELECT MIN(p.published_at),MIN(i.media_index) FROM items i JOIN posts p USING(source,post_id)
                WHERE i.digest=deliveries.digest
            ) WHERE state='pending' AND published_at<?""", (cutoff,))
            has_orphans = db.execute(f"UPDATE assets SET state='missing' WHERE {orphaned}").rowcount > 0
        if has_orphans:
            with self._db() as db:
                for row in db.execute(f"SELECT path FROM assets WHERE {orphaned}").fetchall():
                    if row["path"]:
                        self._asset_path(row["path"]).unlink(missing_ok=True)
                db.execute(f"DELETE FROM assets WHERE {orphaned}")

    def ensure_space(self, required: int, limit: int, digest: str | None = None) -> None:
        # 空间回收单独提交，后续下载入库失败不会回滚已删除文件的索引。
        with self._db() as db:
            if digest is not None:
                existing = db.execute("SELECT state FROM assets WHERE digest=?", (digest,)).fetchone()
                if existing is not None and existing[0] != "missing":
                    return
            used = db.execute("SELECT COALESCE(SUM(size),0) FROM assets WHERE state='available'").fetchone()[0]
            if used + required <= limit:
                return
            reclaimable = db.execute("""SELECT a.* FROM assets a WHERE a.state='available'
                AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.digest=a.digest AND d.state IN ('pending','sending'))
                ORDER BY a.created_at""").fetchall()
            if used + required - sum(row["size"] for row in reclaimable) > limit:
                raise MemeStorageFull("本地 Meme 图片存储已达上限，保留待下载记录")
            for row in reclaimable:
                self._retire_asset(db, row)
                used -= row["size"]
                if used + required <= limit:
                    break

    def accept_image(self, item_key: MemeItemKey, raw: bytes, extension: str, mime: str, run_key: str,
                     targets: list[str], sources: list[str], cutoff: float, queue_cap: int, storage_limit: int) -> bool:
        digest = hashlib.sha256(raw).hexdigest()
        with self._db() as db:
            # 下载时事务未持锁，清理可能已删除旧行并让 SQLite 复用它的 ID。
            item = db.execute("""SELECT i.id FROM items i JOIN posts p USING(source,post_id)
                WHERE (i.source,i.post_id,i.media_id)=(?,?,?) AND i.state='pending' AND p.published_at>=?""",
                (*item_key, cutoff)).fetchone()
            if item is None:
                return False
            item_id = item["id"]
            asset = db.execute("SELECT * FROM assets WHERE digest=?", (digest,)).fetchone()
            if asset is not None and asset["state"] != "missing":
                db.execute("UPDATE items SET state='duplicate',digest=?,error='' WHERE id=?", (digest, item_id))
                self._sync_targets(db, targets, sources, cutoff, queue_cap)
                return False
            used = db.execute("SELECT COALESCE(SUM(size),0) FROM assets WHERE state='available'").fetchone()[0]
            if used + len(raw) > storage_limit:
                raise MemeStorageFull("本地 Meme 图片存储已达上限，保留待下载记录")
            relative = f"images/{digest}.{extension}"
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.tmp, suffix=".part", delete=False) as output:
                    temporary = Path(output.name)
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                # 正式文件就绪后才登记为可发送图片，残留临时文件不会进入队列。
                temporary.replace(self._asset_path(relative))
                db.execute("""INSERT INTO assets VALUES (?,?,?,?,?,?) ON CONFLICT(digest)
                    DO UPDATE SET path=excluded.path,size=excluded.size,mime=excluded.mime,state='available'""", (digest, relative, len(raw), mime, "available", time.time()))
                db.execute("UPDATE items SET state='ready',digest=?,error='' WHERE id=?", (digest, item_id))
                # X 随图片入库在同一事务内累计，按群建立投递记录不重复计数。
                db.execute("UPDATE runs SET added=added+1 WHERE key=?", (run_key,))
                self._sync_targets(db, targets, sources, cutoff, queue_cap)
                return True
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def claim(self, target: str, run_key: str, sources: list[str], cutoff: float) -> dict[str, Any] | None:
        if not sources:
            return None
        with self._db() as db:
            # 同图可能关联多个帖子，署名取当前有效来源中最早的帖子，且与该图片一起领取。
            row = db.execute(f"""SELECT d.*,a.path,a.mime,a.size,p.author_handle
                FROM deliveries d JOIN assets a USING(digest)
                JOIN items i USING(digest) JOIN posts p USING(source,post_id)
                WHERE d.target=? AND d.state='pending' AND a.state='available'
                AND i.source IN ({self._placeholders(sources)}) AND p.published_at>=?
                ORDER BY d.published_at,d.media_index,d.digest,p.published_at,p.source,p.post_id,i.media_index LIMIT 1""", [target, *sources, cutoff]).fetchone()
            if row is None:
                return None
            db.execute("UPDATE deliveries SET state='sending',attempts=attempts+1 WHERE target=? AND digest=?", (target, row["digest"]))
            db.execute("UPDATE runs SET digest=? WHERE key=?", (row["digest"], run_key))
            return dict(row)

    def read_image(self, delivery: dict[str, Any]) -> bytes:
        with self._lock:
            raw = self._asset_path(delivery["path"]).read_bytes()
            if len(raw) != delivery["size"] or hashlib.sha256(raw).hexdigest() != delivery["digest"]:
                raise RuntimeError("本地 Meme 图片校验失败")
            return raw

    def mark_missing(self, digest: str) -> None:
        with self._db() as db:
            row = db.execute("SELECT path FROM assets WHERE digest=?", (digest,)).fetchone()
            if row and row[0]:
                self._asset_path(row[0]).unlink(missing_ok=True)
            db.execute("UPDATE assets SET state='missing',path='' WHERE digest=?", (digest,))
            db.execute("UPDATE items SET state='pending',attempts=0,error='本地图片缺失或损坏' WHERE digest=?", (digest,))

    def finish_delivery(self, target: str, digest: str, run_key: str, state: str, message_id: str | None = None, error: str = "") -> None:
        if state not in {"sent", "failed", "uncertain"}:
            raise ValueError("不合法的 Meme 发送结果")
        with self._db() as db:
            if state in {"sent", "uncertain"}:
                db.execute("INSERT OR IGNORE INTO sent_history(target,digest) VALUES (?,?)", (target, bytes.fromhex(digest)))
            db.execute("""UPDATE deliveries SET state=CASE WHEN ?='failed' AND attempts<3 THEN 'pending' ELSE ? END,
                       message_id=?,error=? WHERE target=? AND digest=?""", (state, state, message_id, error[:500], target, digest))
            db.execute("UPDATE runs SET state=?,finished_at=?,detail=? WHERE key=?", (state, time.time(), error[:500], run_key))

    def release_unstarted(self, target: str, digest: str, run_key: str) -> None:
        with self._db() as db:
            db.execute("UPDATE deliveries SET state='pending',attempts=MAX(0,attempts-1) WHERE target=? AND digest=? AND state='sending'", (target, digest))
            db.execute("UPDATE runs SET state='skipped',finished_at=?,detail='发起 RPC 前模块已停止' WHERE key=?", (time.time(), run_key))

    def stats(self, target: str, sources: list[str], cutoff: float) -> dict[str, Any]:
        with self._db() as db:
            counts: dict[str, int] = {}
            if sources:
                rows = db.execute(f"""SELECT d.state,COUNT(*) FROM deliveries d WHERE d.target=?
                    AND EXISTS(SELECT 1 FROM items i JOIN posts p USING(source,post_id)
                        WHERE i.digest=d.digest AND i.source IN ({self._placeholders(sources)}) AND p.published_at>=?)
                    GROUP BY d.state""", [target, *sources, cutoff])
                counts = {row[0]: row[1] for row in rows}
            return {
                "counts": counts,
                "storage": db.execute("SELECT COALESCE(SUM(size),0) FROM assets WHERE state='available'").fetchone()[0],
                "sources": [dict(row) for row in db.execute("SELECT * FROM sources ORDER BY source") if row["source"] in sources],
                "last_send": dict(row) if (row := db.execute("SELECT state,finished_at,detail FROM runs WHERE kind='send' AND owner=? ORDER BY scheduled_at DESC LIMIT 1", (target,)).fetchone()) else None,
            }
