"""清单解析与校验片（XOR 补零）逻辑。

校验片定义：所有数据片分别以 0 补齐到最长片的长度，再逐字节异或。
因此最长片自身即「基准片」，恢复缺片时无需额外上传基准。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHARD_NAME_RE = re.compile(r"^(?P<stem>.+)\.(?P<index>\d+)$")

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_SHARDS = 10_000


class ManifestError(ValueError):
    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


@dataclass(frozen=True)
class ShardSpec:
    index: int
    size: int
    sha256: str

    def as_dict(self) -> dict:
        return {"index": self.index, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class Manifest:
    label: str
    shards: tuple[ShardSpec, ...]
    by_index: dict
    total_bytes: int
    max_size: int
    longest_indices: tuple[int, ...]

    @property
    def indices(self) -> list[int]:
        return [s.index for s in self.shards]

    @property
    def contiguous(self) -> bool:
        return self.indices == list(range(self.indices[0], self.indices[0] + len(self.shards)))


def _as_int(value, field: str, details: list[str]) -> int:
    if isinstance(value, bool):
        raise ManifestError(f"{field} 必须是整数", details)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ManifestError(f"{field} 必须是整数", details)


def _parse_entry(entry, position: int, details: list[str]) -> ShardSpec:
    where = f"第 {position} 项"
    if not isinstance(entry, dict):
        raise ManifestError(f"{where} 不是对象", details)

    index = None
    for key in ("index", "shard_index", "片号", "序号", "no"):
        if key in entry:
            index = _as_int(entry[key], f"{where} 的片号", details)
            break
    if index is None:
        for key in ("name", "filename", "文件名"):
            if key in entry and isinstance(entry[key], str):
                match = SHARD_NAME_RE.match(entry[key].strip())
                if match:
                    index = _as_int(match.group("index"), f"{where} 的片号", details)
                    break
    if index is None:
        raise ManifestError(f"{where} 缺少片号", details)

    size = None
    for key in ("size", "bytes", "byte_size", "字节数", "长度"):
        if key in entry:
            size = _as_int(entry[key], f"{where}（片 {index}）的字节数", details)
            break
    if size is None:
        raise ManifestError(f"片 {index} 缺少字节数", details)
    if size <= 0:
        raise ManifestError(f"片 {index} 的字节数必须为正数", details)

    sha = None
    for key in ("sha256", "hash", "sha", "SHA-256", "摘要"):
        if key in entry:
            value = entry[key]
            if not isinstance(value, str):
                raise ManifestError(f"片 {index} 的 SHA-256 不是字符串", details)
            sha = value.strip().lower()
            break
    if sha is None:
        raise ManifestError(f"片 {index} 缺少 SHA-256", details)
    if not SHA256_RE.match(sha):
        raise ManifestError(f"片 {index} 的 SHA-256 不是 64 位十六进制", details)

    return ShardSpec(index=index, size=size, sha256=sha)


def parse_manifest(data: bytes, filename: str | None = None) -> tuple[Manifest, str]:
    """解析清单字节，返回 (Manifest, label)。不信任任何输入。"""
    if not data:
        raise ManifestError("清单为空")
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError("清单过于庞大")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ManifestError("清单不是 UTF-8 文本") from exc

    details: list[str] = []
    label_hint = None
    try:
        import json

        doc = json.loads(text)
    except ValueError as exc:
        raise ManifestError(f"清单不是合法 JSON：{exc}") from exc

    if isinstance(doc, list):
        entries = doc
        label_hint = filename
    elif isinstance(doc, dict):
        entries = None
        for key in ("shards", "items", "entries", "files", "片列表", "清单"):
            if key in doc:
                entries = doc[key]
                break
        if entries is None:
            raise ManifestError("清单缺少片列表（shards）")
        for key in ("label", "name", "package", "包名", "交接单号"):
            if isinstance(doc.get(key), str) and doc[key].strip():
                label_hint = doc[key].strip()
                break
        if label_hint is None:
            label_hint = filename
    else:
        raise ManifestError("清单顶层必须是对象或数组")

    if not isinstance(entries, list):
        raise ManifestError("片列表必须是数组")
    if not entries:
        raise ManifestError("片列表为空")
    if len(entries) > MAX_SHARDS:
        raise ManifestError(f"片数超过上限 {MAX_SHARDS}")

    shards: list[ShardSpec] = []
    for position, entry in enumerate(entries, start=1):
        try:
            shards.append(_parse_entry(entry, position, details))
        except ManifestError as exc:
            details.append(exc.message)
            if len(details) >= 20:
                break

    if details:
        raise ManifestError(f"清单有 {len(details)} 处问题", details)

    seen: dict[int, ShardSpec] = {}
    for shard in shards:
        if shard.index in seen:
            details.append(f"片号 {shard.index} 重复")
        elif shard.index < 0:
            details.append(f"片号 {shard.index} 不能为负")
        else:
            seen[shard.index] = shard
    if details:
        raise ManifestError("清单片号有误", details)

    ordered = tuple(sorted(seen.values(), key=lambda s: s.index))
    total = sum(s.size for s in ordered)
    max_size = max(s.size for s in ordered)
    longest = tuple(s.index for s in ordered if s.size == max_size)
    label = (label_hint or "未命名交接包").strip()[:200]
    manifest = Manifest(
        label=label,
        shards=ordered,
        by_index=seen,
        total_bytes=total,
        max_size=max_size,
        longest_indices=longest,
    )
    return manifest, label


def manifest_digest(manifest: Manifest) -> str:
    """清单规范化摘要：只看片号、字节数与哈希，同一原片重传必得同一摘要。"""
    digest = hashlib.sha256()
    digest.update(b"flow-section-handover/v1\n")
    for shard in manifest.shards:
        digest.update(f"{shard.index}\t{shard.size}\t{shard.sha256}\n".encode("utf-8"))
    return digest.hexdigest()
