#!/usr/bin/env python3
"""Read the linked-library table out of a .blend without opening Blender.

WHY THIS EXISTS
---------------
A scene that links its geometry out of another .blend renders **empty** on the
instance, in under a second, and the job is reported `done`. Probe job
`82ebdd064292` came back as a strip of sky over pure black in 0.83 s with
`blank: OK`. Nothing in the broker looked at libraries: `push_scene` sends the
.blend, `push_scene_siblings` sends a name-matched list of directories beside
it, and a `Library` datablock pointing at
`~/f1-round2/render/world/assembly/r2/assembly9.blend` resolves to
nothing on the far side. Blender does not fail on an unresolved library. It
drops the objects and renders the empty world.

`remote.missing_assets` was written to catch exactly this class and cannot: it
greps worker.log for the single string `Image file ... does not exist`, which is
what a missing *image* produces. A missing *library* produces
`Cannot find lib '...'` / `Read blend: <path>` — different text, so it is
invisible to a check whose own docstring names the failure.

WHY PARSE RATHER THAN LOAD
--------------------------
`scenes.sibling_dirs_for` chose name-pattern matching over reading the blend's
path table, on the stated grounds that "reading the blend's path table means
loading a 288 MB file in Blender for every dispatch". That is true of
`bpy.data.libraries`; it is not true of the file format. A .blend is a flat
sequence of length-prefixed blocks, and the library table is a handful of `LI`
blocks. This module seeks past everything else and never decompresses geometry:
a 4.2 GB assembly reads in well under a second.

FORMAT
------
Header, 12 or 17 bytes:

    legacy (file_format_version 0)   BLENDER . v VVV       e.g. BLENDER-v300
                                      ^ '_' 4-byte ptr, '-' 8-byte ptr
                                        ^ 'v' little endian, 'V' big
    v1 (Blender 4.4+, large files)   BLENDER 17 - 01 v VVVV  e.g. BLENDER17-01v0502
                                             ^^ header size, always 17
                                                ^ 8-byte pointers
                                                  ^^ file format version
                                                     ^ always little endian
                                                       ^^^^ blender version

Then blocks, each a fixed-size header followed by `len` bytes of payload:

    ver 0, 4-byte ptr    4s code, i len, I old, i sdna, i count      (20 bytes)
    ver 0, 8-byte ptr    4s code, i len, Q old, i sdna, i count      (24 bytes)
    ver 1                4s code, i sdna, Q old, q len, q count      (28 bytes)

The last block is `ENDB`. Legacy files may write a short 8-byte `ENDB`.

This mirrors Blender's own `scripts/modules/_blendfile_header.py`, which was
read to confirm the v1 field ORDER (`sdna` before `old`, and `len` after it — it
is not the legacy order with wider fields). It is reimplemented here rather than
imported so the broker does not depend on a local Blender install being present
and on a particular version's module path.

The field offset of `Library.filepath` is not hardcoded. It is computed from the
file's own `DNA1` block, so this reads correctly across versions that moved or
renamed the field (it was `name[1024]` before 3.0).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Optional


class BlendReadError(Exception):
    """The file could not be parsed as a .blend. Never raised for a valid file
    that simply has no libraries — that case returns an empty list."""


# A library table is small. This bounds a corrupt or hostile file from being
# read into memory as if it were geometry.
MAX_LI_BLOCK = 1 << 20
MAX_DNA_BLOCK = 64 << 20


@dataclass(frozen=True)
class LibRef:
    """One `Library` datablock.

    `stored` is the path exactly as the file records it, which is usually
    `//`-relative to the linking .blend and sometimes absolute. `path` is that
    resolved against the linking file's directory. `exists` is the local answer
    only — it says nothing about whether the file reached the instance, which is
    the failure this module was written for.
    """
    stored: str
    path: Path
    exists: bool
    linker: Path

    @property
    def relative(self) -> bool:
        return self.stored.startswith("//")


# --- header ---------------------------------------------------------------


@dataclass(frozen=True)
class _Header:
    pointer_size: int
    little_endian: bool
    version: int
    format_version: int

    @property
    def endian(self) -> str:
        return "<" if self.little_endian else ">"

    @property
    def block_struct(self) -> struct.Struct:
        if self.format_version == 1:
            return struct.Struct(self.endian + "4siQqq")
        ptr = "I" if self.pointer_size == 4 else "Q"
        return struct.Struct(self.endian + "4si" + ptr + "ii")

    def block_fields(self, raw: tuple) -> tuple[bytes, int, int, int]:
        """-> (code, sdna_index, length, count), field order normalised."""
        if self.format_version == 1:
            code, sdna, _old, length, count = raw
        else:
            code, length, _old, sdna, count = raw
        return code.partition(b"\0")[0], sdna, length, count


def _read_header(fh: "_Stream") -> _Header:
    """Read the file header. The stream is positioned at byte 0 by `_open` and
    is never rewound — the zstd fallback is a pipe and cannot be."""
    magic = fh.read(7)
    if magic != b"BLENDER":
        raise BlendReadError(f"not a .blend: first bytes {magic!r}")
    byte7 = fh.read(1)
    if byte7 in (b"_", b"-"):
        endian = fh.read(1)
        if endian not in (b"v", b"V"):
            raise BlendReadError(f"invalid endian marker {endian!r}")
        return _Header(
            pointer_size=4 if byte7 == b"_" else 8,
            little_endian=endian == b"v",
            version=int(fh.read(3)),
            format_version=0,
        )
    size = int(byte7 + fh.read(1))
    if size != 17:
        raise BlendReadError(f"unknown file header size {size}")
    if fh.read(1) != b"-":
        raise BlendReadError("v1 header without 8-byte pointers")
    fmt = int(fh.read(2))
    if fmt != 1:
        raise BlendReadError(f"unsupported file format version {fmt}")
    if fh.read(1) != b"v":
        raise BlendReadError("v1 header without little-endian marker")
    return _Header(pointer_size=8, little_endian=True,
                   version=int(fh.read(4)), format_version=1)


class _Stream:
    """Forward-only reader over a .blend, compressed or not.

    Compression matters here: Blender's save dialog has had *Compress* on by
    default since 3.0, and since 3.0 that means zstd. A reader that cannot read
    a compressed file cannot answer for half the corpus. The wrapper exists
    because the zstd fallback is a **pipe** — `zstd -d -c` — which is not
    seekable and returns short reads, and both of those silently corrupt a
    naive block walk: a short read makes a 28-byte header parse as garbage, and
    a failed seek makes the walk read geometry as if it were block headers.

    So: `read` loops until it has the bytes or the file ends, and `skip` seeks
    when it can and reads-and-discards when it cannot.
    """

    __slots__ = ("_fh", "_proc", "_seekable", "pos")

    def __init__(self, fh: BinaryIO, proc=None, seekable: bool = True) -> None:
        self._fh = fh
        self._proc = proc
        self._seekable = seekable
        self.pos = 0

    def read(self, n: int) -> bytes:
        chunks: list[bytes] = []
        got = 0
        while got < n:
            part = self._fh.read(n - got)
            if not part:
                break
            chunks.append(part)
            got += len(part)
        self.pos += got
        return b"".join(chunks)

    def skip(self, n: int) -> None:
        if n <= 0:
            return
        if self._seekable:
            self._fh.seek(n, 1)
            self.pos += n
            return
        remaining = n
        while remaining:
            part = self._fh.read(min(remaining, 1 << 20))
            if not part:
                raise BlendReadError(
                    f"file ended {remaining} bytes into a block body")
            remaining -= len(part)
        self.pos += n

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            if self._proc is not None:
                if self._proc.poll() is None:
                    self._proc.kill()
                self._proc.wait()

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _open(path: Path) -> _Stream:
    """Open a .blend, transparently decompressing gzip and zstd.

    zstd is read through the `zstandard` module when it is installed and through
    the `zstd` binary when it is not. The binary is not an extra dependency:
    `remote.push_scene` already shells out to `zstd` for every single upload, so
    a broker that can send a scene can read one.
    """
    fh = open(path, "rb")
    head = fh.read(4)
    fh.seek(0)
    if head[:2] == b"\x1f\x8b":
        import gzip
        return _Stream(gzip.open(fh, "rb"))   # type: ignore[arg-type]
    if head[:4] == b"\x28\xb5\x2f\xfd":
        fh.close()
        try:
            import zstandard
        except ImportError:
            pass
        else:
            return _Stream(zstandard.open(path, "rb"))   # type: ignore[arg-type]
        import subprocess
        try:
            # `-f` is load-bearing, not tidiness. Without it the zstd CLI
            # refuses any input that is not a regular file and exits 0 having
            # written nothing — so a .blend reached through a SYMLINK decoded to
            # zero bytes and read as "not a .blend". Measured 2026-08-04: 22 of
            # 464 files in the sweep corpus, every one of them a symlink under
            # `f1-round2/work/dr_relief/before_root/`, and every one of them a
            # real 47 MB scene. It surfaced only because an unreadable file is
            # reported as an ERROR here rather than as "no libraries"; had this
            # module guessed "clean" on a read failure, 22 files would have been
            # silently cleared by a sweep that never opened them.
            proc = subprocess.Popen(
                ["zstd", "-d", "-c", "-q", "-f", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise BlendReadError(
                f"{path.name} is zstd-compressed, and neither the `zstandard` "
                f"module nor the `zstd` binary is available to read it: {exc}"
            ) from None
        assert proc.stdout is not None
        return _Stream(proc.stdout, proc=proc, seekable=False)
    return _Stream(fh)


def _blocks(st: _Stream, head: _Header) -> Iterator[tuple[bytes, int, int]]:
    """Walk the block chain, yielding (code, sdna_index, payload_length).

    The payload is left unread. A consumer that wants it calls `st.read(length)`
    and the walk notices — the next block is found from the stream position, not
    from an absolute seek, because the zstd fallback cannot seek backwards.
    """
    bs = head.block_struct
    while True:
        raw = st.read(bs.size)
        if len(raw) != bs.size:
            # Legacy writers emit a short 8-byte ENDB. Anything else short is a
            # truncated file, and saying so is better than silently reporting
            # "no libraries" for a file that was cut off before its table.
            if len(raw) == 8 and raw[:4] == b"ENDB":
                return
            raise BlendReadError(
                f"truncated block header: {len(raw)} of {bs.size} bytes")
        code, sdna, length, _count = head.block_fields(bs.unpack(raw))
        if code == b"ENDB":
            return
        if length < 0:
            raise BlendReadError(f"negative block length {length} for {code!r}")
        start = st.pos
        yield code, sdna, length
        st.skip(length - (st.pos - start))


# --- DNA ------------------------------------------------------------------


def _align4(n: int) -> int:
    return (n + 3) & ~3


def _strings(buf: bytes, pos: int) -> tuple[list[str], int]:
    count = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    out: list[str] = []
    for _ in range(count):
        end = buf.index(b"\0", pos)
        out.append(buf[pos:end].decode("utf-8", "replace"))
        pos = end + 1
    return out, _align4(pos)


def _field_size(name: str, type_len: int, pointer_size: int) -> int:
    if name.startswith("*") or name.startswith("(*"):
        size = pointer_size
    else:
        size = type_len
    rest = name
    while "[" in rest:
        lb = rest.index("[")
        rb = rest.index("]", lb)
        size *= int(rest[lb + 1:rb])
        rest = rest[rb + 1:]
    return size


def _bare(name: str) -> str:
    return name.lstrip("*(").split("[")[0].rstrip(")")


@dataclass(frozen=True)
class _LibraryLayout:
    sdna_index: int
    offset: int
    length: int


def _library_layout(dna: bytes, pointer_size: int) -> Optional[_LibraryLayout]:
    """Locate `Library.filepath` by reading the file's own struct definitions.

    Returns None if the file defines no `Library` struct at all — which is
    itself a strong signal that nothing is linked, since Blender only writes the
    DNA for structs the file uses.
    """
    if dna[:4] != b"SDNA":
        raise BlendReadError("DNA1 block does not start with SDNA")
    pos = 4
    if dna[pos:pos + 4] != b"NAME":
        raise BlendReadError("DNA1 missing NAME")
    names, pos = _strings(dna, pos + 4)
    if dna[pos:pos + 4] != b"TYPE":
        raise BlendReadError("DNA1 missing TYPE")
    types, pos = _strings(dna, pos + 4)
    if dna[pos:pos + 4] != b"TLEN":
        raise BlendReadError("DNA1 missing TLEN")
    pos += 4
    lengths = list(struct.unpack_from("<%dH" % len(types), dna, pos))
    pos = _align4(pos + 2 * len(types))
    if dna[pos:pos + 4] != b"STRC":
        raise BlendReadError("DNA1 missing STRC")
    pos += 4
    n_structs = struct.unpack_from("<I", dna, pos)[0]
    pos += 4

    for index in range(n_structs):
        type_idx, n_fields = struct.unpack_from("<HH", dna, pos)
        pos += 4
        fields = struct.unpack_from("<%dH" % (2 * n_fields), dna, pos)
        pos += 4 * n_fields
        if types[type_idx] != "Library":
            continue
        offset = 0
        # `filepath` since 3.0; `name` in older files. Both hold the path as
        # written, `filepath_abs`/`filename` hold runtime-resolved copies that
        # are not what we want to report.
        for i in range(n_fields):
            ftype, fname = fields[2 * i], fields[2 * i + 1]
            size = _field_size(names[fname], lengths[ftype], pointer_size)
            if _bare(names[fname]) in ("filepath", "name"):
                return _LibraryLayout(index, offset, size)
            offset += size
        return None
    return None


# --- public ---------------------------------------------------------------


def library_paths(blend: Path) -> list[LibRef]:
    """Every `Library` datablock this .blend links, directly.

    Direct only: a linked library may itself link others. Use
    `library_closure` for the transitive set.
    """
    blend = Path(blend)
    with _open(blend) as st:
        head = _read_header(st)
        li_payloads: list[bytes] = []
        li_sdna: list[int] = []
        dna: Optional[bytes] = None
        for code, sdna, length in _blocks(st, head):
            if code == b"LI":
                if length > MAX_LI_BLOCK:
                    raise BlendReadError(
                        f"implausible {length}-byte Library block in {blend.name}")
                li_payloads.append(st.read(length))
                li_sdna.append(sdna)
            elif code == b"DNA1":
                if length > MAX_DNA_BLOCK:
                    raise BlendReadError(
                        f"implausible {length}-byte DNA1 block in {blend.name}")
                dna = st.read(length)
        if not li_payloads:
            return []
        if dna is None:
            raise BlendReadError(
                f"{blend.name} has {len(li_payloads)} Library block(s) but no "
                f"DNA1 block, so their paths cannot be decoded")

    layout = _library_layout(dna, head.pointer_size)
    if layout is None:
        raise BlendReadError(
            f"{blend.name} has Library blocks but its DNA defines no readable "
            f"Library.filepath field")

    out: list[LibRef] = []
    for payload, sdna in zip(li_payloads, li_sdna):
        # The SDNA index on the block must be the Library struct. If it is not,
        # the offset computed above does not describe this payload and reporting
        # a decoded path would be a guess.
        if sdna != layout.sdna_index:
            raise BlendReadError(
                f"{blend.name}: LI block declares DNA struct {sdna}, expected "
                f"{layout.sdna_index} (Library)")
        end = layout.offset + layout.length
        if end > len(payload):
            raise BlendReadError(
                f"{blend.name}: Library block is {len(payload)} bytes, too short "
                f"for filepath at {layout.offset}..{end}")
        raw = payload[layout.offset:end]
        stored = raw.partition(b"\0")[0].decode("utf-8", "replace")
        if not stored:
            continue
        out.append(_ref(stored, blend))
    return out


def _ref(stored: str, linker: Path) -> LibRef:
    if stored.startswith("//"):
        resolved = (linker.parent / stored[2:]).resolve()
    else:
        resolved = Path(stored).resolve()
    return LibRef(stored=stored, path=resolved, exists=resolved.is_file(),
                  linker=linker)


def library_closure(blend: Path, depth: int = 8) -> list[LibRef]:
    """Every library reachable from `blend`, following links through links.

    A library that does not exist locally cannot be descended into, so it is
    reported and the walk stops there — which is the right shape: an unreadable
    library is exactly what the caller needs to hear about.
    """
    blend = Path(blend).resolve()
    seen: set[Path] = {blend}
    out: list[LibRef] = []
    frontier = [(blend, 0)]
    while frontier:
        current, level = frontier.pop()
        try:
            refs = library_paths(current)
        except BlendReadError:
            if current == blend:
                raise
            continue                    # a library we cannot parse is reported
        for ref in refs:                # by its own LibRef below
            out.append(ref)
            if ref.exists and ref.path not in seen and level + 1 < depth:
                seen.add(ref.path)
                frontier.append((ref.path, level + 1))
    return out


def links_libraries(blend: Path) -> bool:
    """Cheap yes/no. Does not decode paths, so it answers even for a file whose
    DNA this module cannot read — which matters for a corpus sweep, where
    "unparseable" must not be silently counted as "clean"."""
    with _open(Path(blend)) as st:
        head = _read_header(st)
        for code, _sdna, _length in _blocks(st, head):
            if code == b"LI":
                return True
    return False


def main() -> int:
    import sys
    rc = 0
    for arg in sys.argv[1:]:
        p = Path(arg)
        try:
            refs = library_closure(p)
        except BlendReadError as exc:
            print(f"{p}: ERROR {exc}")
            rc = 2
            continue
        if not refs:
            print(f"{p}: no libraries")
            continue
        rc = max(rc, 1)
        for r in refs:
            mark = "ok " if r.exists else "MISSING"
            print(f"{p}: {mark} {r.stored} -> {r.path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
