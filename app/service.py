"""交接业务：建包、逐片验收、缺片恢复、拼接下载。"""

from __future__ import annotations

import hashlib
import os
import sqlite3

from flask import current_app

from . import codec, db, storage
from .manifest import Manifest, manifest_digest, parse_manifest


class HandoverError(Exception):
    def __init__(self, message: str, status: int = 400, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details or []


def _data_dir() -> str:
    return current_app.config["DATA_DIR"]


def _chunk() -> int:
    return int(current_app.config["CHUNK_SIZE"])


def _delivery(conn, delivery_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM deliveries WHERE id = ?", (int(delivery_id),)
    ).fetchone()
    if row is None:
        raise HandoverError("交付包不存在", 404)
    return dict(row)


def _shard_rows(conn, delivery_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM shards WHERE delivery_id = ? ORDER BY shard_index",
        (int(delivery_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def _manifest_of(delivery: dict) -> Manifest:
    manifest, _label = parse_manifest(
        delivery["manifest_json"].encode("utf-8"), delivery.get("manifest_name")
    )
    return manifest


def delivery_state(delivery_id: int) -> dict:
    """汇总页面所需的完整状态。"""
    conn = db.get_db()
    delivery = _delivery(conn, delivery_id)
    manifest = _manifest_of(delivery)
    rows = _shard_rows(conn, delivery_id)
    shards = []
    missing = []
    accepted = 0
    accepted_bytes = 0
    for row in rows:
        spec = manifest.by_index[row["shard_index"]]
        done = row["accepted_at"] is not None
        if done:
            accepted += 1
            accepted_bytes += spec.size
        else:
            missing.append(spec.index)
        shards.append(
            {
                "index": spec.index,
                "size": spec.size,
                "sha256": spec.sha256,
                "accepted": done,
                "source": row["source"],
                "accepted_at": row["accepted_at"],
                "stored_size": row["stored_size"],
                "stored_sha256": row["stored_sha256"],
                "is_longest": spec.size == manifest.max_size,
            }
        )
    return {
        "delivery": delivery,
        "manifest": manifest,
        "shards": shards,
        "missing": missing,
        "accepted_count": accepted,
        "accepted_bytes": accepted_bytes,
        "total_count": len(shards),
        "complete": not missing,
        "recovery_allowed": len(missing) == 1,
        "recovery_target": next(
            (s for s in shards if missing and s["index"] == missing[0]), None
        ),
    }


def _canonical_manifest_json(manifest: Manifest, label: str) -> str:
    import json

    return json.dumps(
        {
            "label": label,
            "shards": [s.as_dict() for s in manifest.shards],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def create_delivery(manifest_bytes: bytes, filename: str | None) -> tuple[int, bool]:
    """建包或沿用已有记录（同一原片清单摘要相同）。返回 (delivery_id, 是否新建)。"""
    manifest, label = parse_manifest(manifest_bytes, filename)
    digest = manifest_digest(manifest)
    conn = db.get_db()

    existing = conn.execute(
        "SELECT id FROM deliveries WHERE manifest_hash = ?", (digest,)
    ).fetchone()
    if existing is not None:
        return int(existing["id"]), False

    now = db.utcnow()
    try:
        with db.write_tx(conn):
            cursor = conn.execute(
                """INSERT INTO deliveries
                   (manifest_hash, manifest_name, label, manifest_json,
                    total_shards, total_bytes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    digest,
                    (filename or "")[:255] or None,
                    label,
                    _canonical_manifest_json(manifest, label),
                    len(manifest.shards),
                    manifest.total_bytes,
                    now,
                    now,
                ),
            )
            delivery_id = int(cursor.lastrowid)
            conn.executemany(
                """INSERT INTO shards
                   (delivery_id, shard_index, declared_size, declared_sha256)
                   VALUES (?, ?, ?, ?)""",
                [(delivery_id, s.index, s.size, s.sha256) for s in manifest.shards],
            )
    except sqlite3.IntegrityError:
        # 并发建包：另一方先写入，沿用它的记录。
        row = conn.execute(
            "SELECT id FROM deliveries WHERE manifest_hash = ?", (digest,)
        ).fetchone()
        if row is None:
            raise
        return int(row["id"]), False

    try:
        storage.ensure_package_dirs(_data_dir(), delivery_id)
    except storage.StorageError:
        with db.write_tx(conn):
            conn.execute("DELETE FROM deliveries WHERE id = ?", (delivery_id,))
        raise
    return delivery_id, True


def _staging_path(delivery_id: int, tag: str) -> str:
    directory = os.path.join(_data_dir(), "tmp")
    storage.ensure_dir(directory)
    return os.path.join(directory, storage.temp_name(f"d{int(delivery_id)}-{tag}-"))


def _stream_to_staging(file_storage, staging: str, limit: int | None = None):
    """把上传流写入暂存文件，返回 (sha256, 字节数)。

    limit 为允许的最大字节数：超出即停止读取，避免为超限上传耗尽磁盘。
    """
    digest = hashlib.sha256()
    total = 0
    try:
        with open(staging, "wb") as out:
            while True:
                block = file_storage.stream.read(_chunk())
                if not block:
                    break
                total += len(block)
                if limit is not None and total > limit:
                    break
                out.write(block)
                digest.update(block)
            out.flush()
            os.fsync(out.fileno())
    except OSError as exc:
        raise HandoverError(f"写入暂存文件失败：{exc.strerror or exc}", 507) from exc
    return digest.hexdigest(), total


def _safe_replace(source: str, dest: str) -> None:
    try:
        storage.ensure_dir(os.path.dirname(dest))
        storage.replace_file(source, dest)
        storage.fsync_dir(os.path.dirname(dest))
    except storage.StorageError as exc:
        raise HandoverError(str(exc), 507) from exc


def accept_shard(delivery_id: int, index: int, file_storage) -> dict:
    """验收单片：先核验长度与 SHA-256 再入账；失败不影响已验收片。"""
    conn = db.get_db()
    delivery = _delivery(conn, delivery_id)
    manifest = _manifest_of(delivery)
    spec = manifest.by_index.get(int(index))
    if spec is None:
        raise HandoverError(f"片号 {index} 不在清单内", 404)
    if file_storage is None or not file_storage.filename:
        raise HandoverError("缺少上传文件", 400)

    row = conn.execute(
        "SELECT accepted_at, source FROM shards WHERE delivery_id = ? AND shard_index = ?",
        (int(delivery_id), spec.index),
    ).fetchone()
    if row is not None and row["accepted_at"] is not None:
        return _reuse_result(row, spec.index, "该片此前已验收，沿用已有记录")

    staging = _staging_path(delivery_id, f"shard{spec.index}")
    try:
        actual_sha, total = _stream_to_staging(file_storage, staging, limit=spec.size)
        if total != spec.size:
            raise HandoverError(
                f"片 {spec.index} 字节数不符：清单 {spec.size}，实收 {total}",
                422,
            )
        if actual_sha != spec.sha256:
            raise HandoverError(
                f"片 {spec.index} SHA-256 不符：清单 {spec.sha256}，实收 {actual_sha}",
                422,
            )

        final_path = storage.part_path(_data_dir(), delivery_id, spec.index)
        now = db.utcnow()
        with db.write_tx(conn):
            current = conn.execute(
                """SELECT accepted_at, source FROM shards
                   WHERE delivery_id = ? AND shard_index = ?""",
                (int(delivery_id), spec.index),
            ).fetchone()
            if current is not None and current["accepted_at"] is not None:
                return _reuse_result(
                    current, spec.index, "该片已由并发的另一次提交完成，沿用已有记录"
                )
            _safe_replace(staging, final_path)
            staging = None
            conn.execute(
                """UPDATE shards
                   SET stored_size = ?, stored_sha256 = ?, source = 'upload', accepted_at = ?
                   WHERE delivery_id = ? AND shard_index = ?""",
                (spec.size, actual_sha, now, int(delivery_id), spec.index),
            )
            conn.execute(
                "UPDATE deliveries SET updated_at = ? WHERE id = ?",
                (now, int(delivery_id)),
            )
        return {
            "status": "accepted",
            "index": spec.index,
            "source": "upload",
            "reused": False,
            "message": f"片 {spec.index} 已验收",
        }
    finally:
        if staging is not None:
            storage.remove_file(staging)


def _reuse_result(row, index: int, message: str) -> dict:
    return {
        "status": "already-accepted",
        "index": index,
        "source": row["source"],
        "reused": True,
        "message": message,
    }


def _part_sources(manifest: Manifest, data_dir: str, delivery_id: int, indices):
    return [
        (storage.part_path(data_dir, delivery_id, i), manifest.by_index[i].size)
        for i in indices
    ]


def recover_missing(delivery_id: int, index: int, file_storage) -> dict:
    """用校验片恢复唯一缺片；与实交并发时只完成一次。"""
    conn = db.get_db()
    delivery = _delivery(conn, delivery_id)
    manifest = _manifest_of(delivery)
    spec = manifest.by_index.get(int(index))
    if spec is None:
        raise HandoverError(f"片号 {index} 不在清单内", 404)
    if file_storage is None or not file_storage.filename:
        raise HandoverError("缺少校验片文件", 400)

    rows = _shard_rows(conn, delivery_id)
    accepted_indices = [r["shard_index"] for r in rows if r["accepted_at"] is not None]
    missing = [r["shard_index"] for r in rows if r["accepted_at"] is None]
    if not missing:
        raise HandoverError("清单内所有片均已验收，无需恢复", 409)
    if len(missing) != 1:
        raise HandoverError(
            f"当前缺 {len(missing)} 片（{', '.join(map(str, missing))}），"
            "仅当恰好缺一片时才可用校验片恢复",
            409,
        )
    if missing[0] != spec.index:
        raise HandoverError(f"应恢复缺片 {missing[0]}，而非片 {spec.index}", 409)

    staging = _staging_path(delivery_id, f"checkshard{spec.index}")
    recovered_tmp = None
    try:
        # 1. 校验片落暂存，长度必须等于最长片长度。
        _upload_sha, upload_size = _stream_to_staging(
            file_storage, staging, limit=manifest.max_size
        )
        if upload_size != manifest.max_size:
            raise HandoverError(
                f"校验片长度应为 {manifest.max_size} 字节（最长片长度），实收 {upload_size}",
                422,
            )

        # 2. 校验片 ⊕ 其余已验收片 = 缺片；按清单长度截断并核验 SHA-256。
        data_dir = _data_dir()
        recovered_tmp = _staging_path(delivery_id, f"recovered{spec.index}")
        try:
            rec_size, rec_sha = codec.recover_part(
                staging,
                manifest.max_size,
                _part_sources(manifest, data_dir, delivery_id, accepted_indices),
                spec.size,
                recovered_tmp,
                chunk_size=_chunk(),
            )
        except storage.StorageError as exc:
            raise HandoverError(str(exc), 507) from exc
        if rec_size != spec.size:
            raise HandoverError(
                f"恢复所得片长度 {rec_size} 与清单 {spec.size} 不符", 422
            )
        if rec_sha != spec.sha256:
            raise HandoverError(
                "校验片无法恢复出清单中的原片：恢复结果 SHA-256 不符"
                f"（得到 {rec_sha}，清单 {spec.sha256}），请核对校验片与已验收片",
                422,
            )

        # 3. 写锁内复核：若实交已先完成，则恢复不写入、沿用已有记录。
        final_path = storage.part_path(data_dir, delivery_id, spec.index)
        now = db.utcnow()
        with db.write_tx(conn):
            current = conn.execute(
                """SELECT accepted_at, source FROM shards
                   WHERE delivery_id = ? AND shard_index = ?""",
                (int(delivery_id), spec.index),
            ).fetchone()
            still_missing = conn.execute(
                """SELECT COUNT(*) AS n FROM shards
                   WHERE delivery_id = ? AND accepted_at IS NULL""",
                (int(delivery_id),),
            ).fetchone()["n"]
            if current is not None and current["accepted_at"] is not None:
                return _reuse_result(
                    current,
                    spec.index,
                    "该片已由实交完成，本次恢复未写入，沿用已有记录",
                )
            if still_missing != 1:
                raise HandoverError("缺片状态已变化，未完成恢复，请刷新后重试", 409)
            _safe_replace(recovered_tmp, final_path)
            recovered_tmp = None
            conn.execute(
                """UPDATE shards
                   SET stored_size = ?, stored_sha256 = ?, source = 'recovered',
                       accepted_at = ?
                   WHERE delivery_id = ? AND shard_index = ?""",
                (spec.size, rec_sha, now, int(delivery_id), spec.index),
            )
            conn.execute(
                """UPDATE deliveries
                   SET updated_at = ?, recovered_index = ?, recovered_at = ?
                   WHERE id = ?""",
                (now, spec.index, now, int(delivery_id)),
            )
        return {
            "status": "recovered",
            "index": spec.index,
            "source": "recovered",
            "reused": False,
            "used_recovery": True,
            "message": f"片 {spec.index} 已由校验片恢复并通过哈希核验",
        }
    finally:
        if staging is not None:
            storage.remove_file(staging)
        if recovered_tmp is not None:
            storage.remove_file(recovered_tmp)


def open_download(delivery_id: int):
    """拼接下载：集齐后才开放；按片号升序、逐片按清单字节数流式输出。

    返回 (delivery, manifest, generate)。下载次数只在 generate 完整输出
    后入账一次；客户端提前断开、分片过短/过长/读取失败或 HEAD 请求都不计。
    """
    conn = db.get_db()
    delivery = _delivery(conn, delivery_id)
    manifest = _manifest_of(delivery)
    rows = _shard_rows(conn, delivery_id)
    missing = [r["shard_index"] for r in rows if r["accepted_at"] is None]
    if missing:
        raise HandoverError(
            f"尚缺 {len(missing)} 片（{', '.join(map(str, missing))}），暂不能下载",
            409,
        )

    chunk = _chunk()
    data_dir = _data_dir()
    database = current_app.config["DATABASE"]
    # 流式输出发生在请求上下文结束之后，闭包只携带普通值，不依赖 flask g。
    # manifest.shards 已按片号升序；逐片以清单上的字节数为准。
    parts = [
        (storage.part_path(data_dir, delivery_id, spec.index), spec.size)
        for spec in manifest.shards
    ]

    def generate():
        completed = False
        try:
            for path, size in parts:
                yield from codec.iter_part_exact(path, size, chunk)
            completed = True
        finally:
            # 只有完整输出才入账；提前关闭（GeneratorExit）或读取失败不计。
            if completed:
                record_download(database, delivery_id)

    return delivery, manifest, generate


def record_download(database: str, delivery_id: int) -> None:
    """一次完整输出落账：下载次数 +1，最近下载时间取各次完成时刻的最晚值。

    独立连接的短事务：流式输出期间不持有任何连接或锁，下载暂停不妨碍
    其他交接包的验收写入；同一包的并发下载各按自身结果入账。
    """
    conn = db.connect(database)
    try:
        now = db.utcnow()
        with db.write_tx(conn):
            conn.execute(
                """UPDATE deliveries
                   SET download_count = download_count + 1,
                       last_download_at = MAX(COALESCE(last_download_at, ''), ?)
                   WHERE id = ?""",
                (now, int(delivery_id)),
            )
    finally:
        conn.close()
