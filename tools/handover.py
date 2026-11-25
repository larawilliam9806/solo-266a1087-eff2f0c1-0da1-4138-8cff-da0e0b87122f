#!/usr/bin/env python3
"""交接辅助工具：生成清单、生成/核验校验片、本地核验拼接结果。

全部按固定大小分块处理，内存开销不随片大小增长。

用法：
  python3 tools/handover.py manifest  <片目录> --out manifest.json [--pattern '*.part']
  python3 tools/handover.py checkshard <manifest.json> <片目录> --out checkshard.bin [--pattern ...]
  python3 tools/handover.py verify    <manifest.json> <片目录> [--pattern ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import codec  # noqa: E402
from app.manifest import SHARD_NAME_RE, ManifestError, parse_manifest  # noqa: E402

READ = 1024 * 1024


def iter_files(directory: str, pattern: str):
    regex = re.compile(pattern)
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or not regex.match(name):
            continue
        match = SHARD_NAME_RE.match(name)
        if not match:
            print(f"跳过无法识别片号的文件：{name}", file=sys.stderr)
            continue
        yield int(match.group("index")), path


def sha_and_size(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(READ)
            if not block:
                break
            digest.update(block)
            total += len(block)
    return digest.hexdigest(), total


def cmd_manifest(args) -> int:
    shards = []
    for index, path in iter_files(args.directory, args.pattern):
        digest, size = sha_and_size(path)
        shards.append({"index": index, "size": size, "sha256": digest})
    if not shards:
        print("没有找到符合命名规则的片文件", file=sys.stderr)
        return 1
    shards.sort(key=lambda s: s["index"])
    doc = {"label": args.label or os.path.basename(os.path.abspath(args.directory)), "shards": shards}
    payload = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload)
        print(f"清单已写入 {args.out}（{len(shards)} 片，共 {sum(s['size'] for s in shards)} 字节）")
    else:
        sys.stdout.write(payload)
    return 0


def _load_manifest(path: str):
    with open(path, "rb") as handle:
        data = handle.read()
    manifest, _label = parse_manifest(data, os.path.basename(path))
    return manifest


def _sources(manifest, directory: str, pattern: str, exclude: set[int] = frozenset()):
    sources = []
    for index, path in iter_files(directory, pattern):
        if index in exclude:
            continue
        if index not in manifest.by_index:
            print(f"目录中有清单外的片 {index}：{path}", file=sys.stderr)
            continue
        sources.append((path, manifest.by_index[index].size))
    return sources


def cmd_checkshard(args) -> int:
    manifest = _load_manifest(args.manifest)
    sources = _sources(manifest, args.directory, args.pattern)
    found = {int(SHARD_NAME_RE.match(os.path.basename(p)).group("index")) for p, _ in sources}
    missing = [i for i in manifest.by_index if i not in found]
    if missing:
        print(f"缺少片：{missing}", file=sys.stderr)
        return 1
    total, digest = codec.build_checkshard(sources, manifest.max_size, args.out)
    print(f"校验片已写入 {args.out}：{total} 字节，SHA-256 {digest}")
    return 0


def cmd_verify(args) -> int:
    manifest = _load_manifest(args.manifest)
    sources = dict(
        (index, (path, manifest.by_index[index].size))
        for index, path in iter_files(args.directory, args.pattern)
        if index in manifest.by_index
    )
    missing = [i for i in manifest.by_index if i not in sources]
    if missing:
        print(f"缺少片：{missing}", file=sys.stderr)
        return 1
    bad = 0
    for index, spec in manifest.by_index.items():
        path, _size = sources[index]
        digest, size = sha_and_size(path)
        if size != spec.size or digest != spec.sha256:
            bad += 1
            print(f"片 {index} 不符：清单 {spec.size}/{spec.sha256}，实际 {size}/{digest}")
    if bad:
        print(f"{bad} 片未通过核验", file=sys.stderr)
        return 1
    print(f"全部 {len(sources)} 片通过核验")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="流速断面交接辅助工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_manifest = sub.add_parser("manifest", help="按片目录生成清单")
    p_manifest.add_argument("directory")
    p_manifest.add_argument("--out")
    p_manifest.add_argument("--label")
    p_manifest.add_argument("--pattern", default=r"^.+\.\d+$")
    p_manifest.set_defaults(func=cmd_manifest)

    p_check = sub.add_parser("checkshard", help="由原片生成校验片")
    p_check.add_argument("manifest")
    p_check.add_argument("directory")
    p_check.add_argument("--out", required=True)
    p_check.add_argument("--pattern", default=r"^.+\.\d+$")
    p_check.set_defaults(func=cmd_checkshard)

    p_verify = sub.add_parser("verify", help="按清单逐片核验")
    p_verify.add_argument("manifest")
    p_verify.add_argument("directory")
    p_verify.add_argument("--pattern", default=r"^.+\.\d+$")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ManifestError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
