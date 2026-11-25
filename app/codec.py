"""固定内存的校验片（XOR 补零）计算、缺片恢复与流式拼接。

语义：把每片视为尾部补零到「最长片长度」的字节串，逐字节异或。
计算采用「磁盘累加器 + 分批打开源文件」的折叠方式：
  - 内存中始终只有若干固定大小的块（chunk_size），与片大小无关；
  - 同时打开的文件句柄数有上限，与片数无关地可控；
  - 累加器是临时文件，最终原子替换到目标路径。
"""

from __future__ import annotations

import hashlib
import os
import tempfile

from .storage import READ_SIZE, StorageError, fsync_dir, replace_file

# 同一批次最多同时打开的源文件数。
MAX_OPEN_FILES = 32


class CodecError(StorageError):
    """校验片计算或恢复过程中的数据问题。"""


def _xor_fold(
    sources: list[tuple[str, int]],
    length: int,
    chunk_size: int,
    directory: str,
) -> str:
    """把 sources 尾部补零到 length 后逐字节异或，写入临时累加器文件。

    返回累加器路径（长度恰为 length）。调用方负责替换或删除。
    """
    os.makedirs(directory, exist_ok=True)
    fd, acc_path = tempfile.mkstemp(prefix=".xor-", dir=directory)
    os.close(fd)
    try:
        with open(acc_path, "wb") as acc:
            acc.truncate(length)  # 稀疏文件，内容为零
        for start in range(0, len(sources), MAX_OPEN_FILES):
            batch = sources[start : start + MAX_OPEN_FILES]
            handles = []
            try:
                for path, _size in batch:
                    handles.append(open(path, "rb"))
                with open(acc_path, "r+b") as acc:
                    position = 0
                    while position < length:
                        want = min(chunk_size, length - position)
                        acc.seek(position)
                        acc_block = acc.read(want)
                        if len(acc_block) != want:
                            raise CodecError("累加器长度异常")
                        acc_value = int.from_bytes(acc_block, "little")
                        for handle, (path, size) in zip(handles, batch):
                            remaining = size - position
                            if remaining <= 0:
                                continue  # 该片已到尾部，余下按零补齐
                            take = min(want, remaining)
                            block = handle.read(take)
                            if len(block) != take:
                                raise CodecError(
                                    f"{os.path.basename(path)} 长度不足，"
                                    "与清单声明不符"
                                )
                            acc_value ^= int.from_bytes(block, "little")
                        acc.seek(position)
                        acc.write(acc_value.to_bytes(want, "little"))
                        position += want
            except OSError as exc:
                raise StorageError(f"读写分片失败：{exc.strerror or exc}") from exc
            finally:
                for handle in handles:
                    try:
                        handle.close()
                    except OSError:
                        pass
        return acc_path
    except BaseException:
        try:
            os.unlink(acc_path)
        except OSError:
            pass
        raise


def _digest_file(path: str, length: int, chunk_size: int) -> str:
    digest = hashlib.sha256()
    remaining = length
    with open(path, "rb") as handle:
        while remaining > 0:
            block = handle.read(min(chunk_size, remaining))
            if not block:
                raise CodecError("文件长度不足")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def build_checkshard(
    sources: list[tuple[str, int]],
    length: int,
    dest: str,
    chunk_size: int = READ_SIZE,
) -> tuple[int, str]:
    """按「尾部补零至 length 后逐字节异或」生成校验片，原子落到 dest。

    sources 为 (文件路径, 原始字节数) 列表，通常就是全部原片。
    返回 (字节数, sha256)。
    """
    directory = os.path.dirname(dest) or "."
    acc_path = _xor_fold(sources, length, chunk_size, directory)
    try:
        digest = _digest_file(acc_path, length, chunk_size)
        replace_file(acc_path, dest)
        acc_path = None
        fsync_dir(directory)
        return length, digest
    finally:
        if acc_path is not None:
            try:
                os.unlink(acc_path)
            except OSError:
                pass


def recover_part(
    checkshard_path: str,
    checkshard_length: int,
    sources: list[tuple[str, int]],
    target_length: int,
    dest: str,
    chunk_size: int = READ_SIZE,
) -> tuple[int, str]:
    """校验片 ⊕ 其余片 = 缺片；按 target_length 截断后原子写到 dest。

    checkshard_length 是最长片长度（校验片本身的长度），折叠时用它；
    最终结果截回清单给该片的长度 target_length。
    返回 (字节数, sha256)；调用方必须再与清单哈希核对后才计入交付。
    """
    directory = os.path.dirname(dest) or "."
    if target_length > checkshard_length:
        raise CodecError("缺片长度大于校验片长度，无法恢复")
    fold_sources = [(checkshard_path, checkshard_length)] + sources
    acc_path = _xor_fold(fold_sources, checkshard_length, chunk_size, directory)
    try:
        if target_length != checkshard_length:
            with open(acc_path, "r+b") as handle:
                handle.truncate(target_length)
                handle.flush()
                os.fsync(handle.fileno())
        digest = _digest_file(acc_path, target_length, chunk_size)
        replace_file(acc_path, dest)
        acc_path = None
        fsync_dir(directory)
        return target_length, digest
    finally:
        if acc_path is not None:
            try:
                os.unlink(acc_path)
            except OSError:
                pass


def iter_part(path: str, chunk_size: int = READ_SIZE):
    """按固定块大小产出单片字节，用于按片号升序流式拼接下载。"""
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            yield block


def checkshard_bytes(parts: list[bytes], length: int) -> bytes:
    """测试与工具用：纯内存版本，语义与 build_checkshard 相同。"""
    acc = bytearray(length)
    for part in parts:
        if len(part) > length:
            raise ValueError("part longer than checkshard length")
        for i, value in enumerate(part):
            acc[i] ^= value
    return bytes(acc)
