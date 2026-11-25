"""交接包的文件布局与原子写入辅助函数。"""

from __future__ import annotations

import errno
import os
import uuid

READ_SIZE = 1024 * 1024


class StorageError(Exception):
    """磁盘层面的失败（空间不足、写入出错等）。"""


def package_dir(data_dir: str, delivery_id: int) -> str:
    return os.path.join(data_dir, "packages", str(int(delivery_id)))


def parts_dir(data_dir: str, delivery_id: int) -> str:
    return os.path.join(package_dir(data_dir, delivery_id), "parts")


def recovery_dir(data_dir: str, delivery_id: int) -> str:
    return os.path.join(package_dir(data_dir, delivery_id), "recovery")


def part_path(data_dir: str, delivery_id: int, index: int) -> str:
    return os.path.join(parts_dir(data_dir, delivery_id), f"{int(index):06d}.part")


def ensure_package_dirs(data_dir: str, delivery_id: int) -> None:
    try:
        for path in (
            package_dir(data_dir, delivery_id),
            parts_dir(data_dir, delivery_id),
            recovery_dir(data_dir, delivery_id),
        ):
            os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise _translate_oserror(exc) from exc


def fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _translate_oserror(exc: OSError) -> StorageError:
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return StorageError("磁盘空间不足，写入未完成")
    if exc.errno == errno.EACCES:
        return StorageError("存储目录不可写")
    return StorageError(f"写入失败：{exc.strerror or exc}")


def ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise _translate_oserror(exc) from exc


def temp_name(prefix: str = "op-") -> str:
    return f"{prefix}{uuid.uuid4().hex}.tmp"


def remove_file(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def replace_file(source: str, dest: str) -> None:
    """原子替换目标文件；失败时目标保持原样。"""
    try:
        os.replace(source, dest)
    except OSError as exc:
        raise _translate_oserror(exc) from exc
