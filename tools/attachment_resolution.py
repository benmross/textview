#!/usr/bin/env python3
"""Deterministic attachment fallback resolution.

An exact source path is authoritative. Basename-only recovery is allowed only
when the source byte count selects one candidate, or every remaining candidate
has identical content. This prevents common names such as ``tmp.gif`` from
silently attaching the wrong file.
"""
from __future__ import annotations

import hashlib
import os


PLACEHOLDER_SIZE = 1024 * 1024


def file_sha256(path: str, cache: dict[str, str] | None = None) -> str:
    if cache is not None and path in cache:
        return cache[path]
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    if cache is not None:
        cache[path] = value
    return value


def choose_fallback(
    candidates: list[str] | tuple[str, ...],
    expected_size: int | None,
    *,
    allow_size_mismatch: bool = False,
    hash_cache: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    """Return a safe basename fallback and a diagnostic reason."""
    paths = []
    seen = set()
    for path in sorted(candidates):
        canonical = os.path.realpath(path)
        if canonical in seen:
            continue
        seen.add(canonical)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > 0:
            paths.append((path, size))
    if not paths:
        return None, "missing"

    expected = int(expected_size or 0)
    # BlueBubbles sometimes records exactly 1 MiB as a placeholder rather than
    # a real byte count. Treat it like an unknown size.
    if expected and expected != PLACEHOLDER_SIZE:
        matches = [(path, size) for path, size in paths if size == expected]
        if matches:
            paths = matches
        elif not allow_size_mismatch:
            return None, "size-mismatch"

    if len(paths) == 1:
        return paths[0][0], "unique"

    by_hash: dict[str, list[str]] = {}
    for path, _size in paths:
        try:
            digest = file_sha256(path, hash_cache)
        except OSError:
            continue
        by_hash.setdefault(digest, []).append(path)
    if len(by_hash) == 1:
        return sorted(next(iter(by_hash.values())))[0], "identical-copies"
    if not by_hash:
        return None, "missing"
    return None, "ambiguous"
