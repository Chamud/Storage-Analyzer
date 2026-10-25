"""Fallback scanner: a plain os.scandir walk. No admin, no WizTree, but minutes instead of seconds.

Produces the same Entry stream as the WizTree parser, parents before children (pre-order), with recursive
sizes and counts. Allocated size is approximated by rounding each file up to the cluster size (4 KiB).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from datetime import datetime

from . import Entry

CLUSTER = 4096


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def walk_drive(letter: str, min_file_bytes: int, on_progress=None) -> Iterator[Entry]:
    root = f"{letter}:\\"
    # Pre-order output requires folder totals, which we only know after the subtree. So we walk twice
    # cheaply: build the tree in memory (folders only + big files), then emit it.
    class Node:
        __slots__ = ("path", "size", "alloc", "mtime", "files", "folders", "kids", "bigfiles", "attrs")

        def __init__(self, path: str):
            self.path = path
            self.size = self.alloc = self.files = self.folders = 0
            self.mtime = 0.0
            self.kids: list[Node] = []
            self.bigfiles: list[Entry] = []
            self.attrs = 16

    seen = 0

    def build(path: str) -> Node:
        nonlocal seen
        node = Node(path)
        try:
            with os.scandir(path) as it:
                for de in it:
                    try:
                        st = de.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    seen += 1
                    if on_progress and seen % 50000 == 0:
                        on_progress(seen, path)
                    if stat.S_ISLNK(st.st_mode) or (st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                        continue  # don't follow junctions/symlinks (would double count or loop)
                    if de.is_dir(follow_symlinks=False):
                        child = build(de.path)
                        node.kids.append(child)
                        node.size += child.size
                        node.alloc += child.alloc
                        node.files += child.files
                        node.folders += child.folders + 1
                        node.mtime = max(node.mtime, child.mtime)
                    else:
                        alloc = (st.st_size + CLUSTER - 1) // CLUSTER * CLUSTER
                        node.size += st.st_size
                        node.alloc += alloc
                        node.files += 1
                        node.mtime = max(node.mtime, st.st_mtime)
                        if st.st_size >= min_file_bytes:
                            node.bigfiles.append(Entry(de.path, False, st.st_size, alloc, _iso(st.st_mtime),
                                                       st.st_file_attributes, 0, 0))
        except (PermissionError, FileNotFoundError, OSError):
            pass
        return node

    tree = build(root)

    def emit(n: Node) -> Iterator[Entry]:
        yield Entry(n.path, True, n.size, n.alloc, _iso(n.mtime) if n.mtime else None, n.attrs, n.files, n.folders)
        for k in n.kids:
            yield from emit(k)
        yield from n.bigfiles

    return emit(tree)
