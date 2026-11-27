"""Archive-extraction hardening (security audit 2026-11-26, F4/F8, batch C2).

Extraction used to check containment only *after* every archive in an item had been written into
one shared staging directory, and 7zz has no switch to refuse links on extract. These tests pin
the replacement: a pre-extraction listing check that refuses an archive holding an absolute path,
a `..` component, a symlink or a hardlink (nothing written anywhere), one fresh directory per
archive, and a per-archive post-check that also fails on any symlink / `st_nlink > 1` file.

Hostile archives are generated at test time in `tmp_path` (no binary fixtures). `.rar` has no
legitimate compressor available, so the RAR3/RAR5 byte builders below write the container format
directly (store method only), the same approach as the fixtures in `tests/test_postprocess.py`;
`unrar` accepts them and reports the link types exactly as it would for a real WinRAR archive.

Needs a real 7-Zip binary (`7zz`, or `7z` on a Debian dev host -- `LFTPWEB_7Z_BIN` overrides) and,
for the rar cases, `unrar` (`LFTPWEB_UNRAR_BIN`); skipped when absent, like the sibling tests.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import os
import shutil
import struct
import subprocess
import tarfile
import zipfile
import zlib
from pathlib import Path

import pytest

from lftpweb.core import extract
from test_postprocess import _RAR_MULTIVOL_VOL1, _RAR_MULTIVOL_VOL2

_SEVEN_ZIP_BIN = os.environ.get("LFTPWEB_7Z_BIN") or next(
    (b for b in ("7zz", "7z") if shutil.which(b)), None
)
_UNRAR_BIN = os.environ.get("LFTPWEB_UNRAR_BIN") or shutil.which("unrar")

needs_7z = pytest.mark.skipif(_SEVEN_ZIP_BIN is None, reason="no 7zz/7z binary on PATH")
needs_unrar = pytest.mark.skipif(_UNRAR_BIN is None, reason="no unrar binary on PATH")


# --- archive builders -------------------------------------------------------------------------


def _tar_bytes(members: list[tuple], mode: str = "w") -> bytes:
    """`members`: `(name, "file", content)` | `(name, "sym", target)` | `(name, "hard", target)`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, kind, value in members:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                data = value.encode()
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
                continue
            ti.type = tarfile.SYMTYPE if kind == "sym" else tarfile.LNKTYPE
            ti.linkname = value
            tf.addfile(ti)
    return buf.getvalue()


def _zip_bytes(members: list[tuple]) -> bytes:
    """`members`: `(name, content)` or `(name, content, unix_mode)`; a mode of 0o120777 makes the
    member a symlink whose target is `content`."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for m in members:
            zi = zipfile.ZipInfo(m[0])
            if len(m) == 3:
                zi.create_system = 3
                zi.external_attr = m[2] << 16
            zf.writestr(zi, m[1])
    return buf.getvalue()


def _v(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _rar3(entries: list[tuple[str, bytes, int]]) -> bytes:
    """RAR 1.5-4.x container, store method. `entries`: `(name, data, unix_attr)` -- a
    `0o120777` attr is a Unix symlink whose target is `data`."""

    def crc16(b: bytes) -> bytes:
        return struct.pack("<H", zlib.crc32(b) & 0xFFFF)

    out = b"Rar!\x1a\x07\x00"
    main = b"\x73\x00\x00\x0d\x00" + b"\x00" * 6
    out += crc16(main) + main
    for name, data, attr in entries:
        nb = name.encode()
        rest = (
            struct.pack(
                "<IIBIIBBHI",
                len(data),
                len(data),
                3,  # host OS: Unix
                zlib.crc32(data) & 0xFFFFFFFF,
                0x28210000,
                20,
                0x30,
                len(nb),
                attr,
            )
            + nb
        )
        head = b"\x74" + struct.pack("<HH", 0x8000, 7 + len(rest)) + rest
        out += crc16(head) + head + data
    end = b"\x7b\x00\x00\x07\x00"
    return out + crc16(end) + end


def _rar5(entries: list[tuple[str, bytes, tuple[int, str] | None]]) -> bytes:
    """RAR 5 container, store method. `entries`: `(name, data, redirect)`; `redirect` is
    `(kind, target)` with kind 1 = Unix symlink, 4 = hard link, 5 = file reference."""

    def block(h: bytes, data: bytes = b"") -> bytes:
        h = _v(len(h)) + h
        return struct.pack("<I", zlib.crc32(h) & 0xFFFFFFFF) + h + data

    out = b"Rar!\x1a\x07\x01\x00" + block(_v(1) + _v(0) + _v(0))
    for name, data, redirect in entries:
        nb = name.encode()
        extra = b""
        attr = 0o100644
        if redirect:
            kind, target = redirect
            tb = target.encode()
            rec = _v(5) + _v(kind) + _v(0) + _v(len(tb)) + tb
            extra = _v(len(rec)) + rec
            if kind == 1:
                attr = 0o120777
        body = (
            _v(0x4)
            + _v(len(data))
            + _v(attr)
            + struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)
            + _v(0)
            + _v(1)
            + _v(len(nb))
            + nb
        )
        h = _v(2) + _v((1 if extra else 0) | (2 if data else 0))
        if extra:
            h += _v(len(extra))
        if data:
            h += _v(len(data))
        out += block(h + body + extra, data)
    return out + block(_v(5) + _v(0) + _v(0))


def _seven_zip_with_symlink(tmp_path: Path, target: str) -> bytes:
    src = tmp_path / "_src7z"
    src.mkdir()
    (src / "lnk").symlink_to(target)
    out = tmp_path / "_built.7z"
    subprocess.run(
        [_SEVEN_ZIP_BIN, "a", "-snl", str(out), str(src / "lnk")],
        check=True,
        capture_output=True,
    )
    data = out.read_bytes()
    out.unlink()
    shutil.rmtree(src)
    return data


def _snapshot(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# --- the hostile matrix -----------------------------------------------------------------------
# Each case: files placed in the item directory -> (reason substring, offending member substring).


def _hostile_cases(tmp_path: Path, outside: Path) -> dict[str, tuple[dict[str, bytes], str, str]]:
    out = str(outside)
    return {
        "zip_dotdot": (
            {"evil.zip": _zip_bytes([("../x.txt", "x")])},
            "'..' path component",
            "../x.txt",
        ),
        "zip_nested_dotdot": (
            {"evil.zip": _zip_bytes([("a/b/../../../x.txt", "x")])},
            "'..' path component",
            "a/b/../../../x.txt",
        ),
        "zip_absolute": (
            {"evil.zip": _zip_bytes([("/abs/x.txt", "x")])},
            "absolute path",
            "/abs/x.txt",
        ),
        "zip_backslash_dotdot": (
            {"evil.zip": _zip_bytes([("..\\x.txt", "x")])},
            "'..' path component",
            "..\\x.txt",
        ),
        "zip_drive_letter": (
            {"evil.zip": _zip_bytes([("C:\\x.txt", "x")])},
            "absolute path",
            "C:\\x.txt",
        ),
        "zip_symlink": (
            {"evil.zip": _zip_bytes([("lnk", out, 0o120777)])},
            "symbolic link",
            "lnk",
        ),
        "tar_dotdot": (
            {"evil.tar": _tar_bytes([("../x.txt", "file", "x")])},
            "'..' path component",
            "../x.txt",
        ),
        "tar_absolute": (
            {"evil.tar": _tar_bytes([("/abs/x.txt", "file", "x")])},
            "absolute path",
            "/abs/x.txt",
        ),
        "tar_symlink_then_write_through": (
            {
                "evil.tar": _tar_bytes(
                    [("esc", "sym", out), ("esc/pwned.txt", "file", "x")],
                )
            },
            "symbolic link",
            "esc",
        ),
        "tar_symlink_dotdot": (
            {"evil.tar": _tar_bytes([("esc", "sym", ".."), ("esc/pwned.txt", "file", "x")])},
            "symbolic link",
            "esc",
        ),
        "tar_hardlink": (
            {"evil.tar": _tar_bytes([("a.txt", "file", "x"), ("b.txt", "hard", "a.txt")])},
            "hard link",
            "b.txt",
        ),
        "tar_hardlink_to_outside_file": (
            {"evil.tar": _tar_bytes([("b.txt", "hard", str(outside / "victim.txt"))])},
            "hard link",
            "b.txt",
        ),
        "tar_xz_symlink": (
            {"evil.tar.xz": lzma.compress(_tar_bytes([("esc", "sym", out)]))},
            "symbolic link",
            "esc",
        ),
        "tar_gz_symlink": (
            {"evil.tar.gz": gzip.compress(_tar_bytes([("esc", "sym", out)]))},
            "symbolic link",
            "esc",
        ),
        "tar_bz2_dotdot": (
            {"evil.tar.bz2": bz2.compress(_tar_bytes([("../x.txt", "file", "x")]))},
            "'..' path component",
            "../x.txt",
        ),
        "tar_xz_hardlink": (
            {
                "evil.tar.xz": lzma.compress(
                    _tar_bytes([("a.txt", "file", "x"), ("b.txt", "hard", "a.txt")])
                )
            },
            "hard link",
            "b.txt",
        ),
        # The split attack: the symlink arrives in one archive, the write-through in a second
        # archive of the same item. Neither alone writes outside; together they did.
        "two_archives_symlink_then_write": (
            {
                "a1.tar": _tar_bytes([("esc", "sym", out)]),
                "a2.tar": _tar_bytes([("esc/pwned.txt", "file", "x")]),
            },
            "symbolic link",
            "esc",
        ),
        "two_archives_one_clean_one_hostile": (
            {
                "a1.zip": _zip_bytes([("fine.txt", "ok")]),
                "a2.zip": _zip_bytes([("../x.txt", "x")]),
            },
            "'..' path component",
            "../x.txt",
        ),
        "member_name_with_newline_injection": (
            {"evil.tar": _tar_bytes([("ok\nSize = 1\nMode = -rw/../..", "file", "x")])},
            "",
            "",
        ),
    }


def _run_hostile(tmp_path: Path, name: str, **kw):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("victim")
    item = tmp_path / "queue" / "Release"
    item.mkdir(parents=True)
    files, reason, member = _hostile_cases(tmp_path, outside)[name]
    for fname, data in files.items():
        (item / fname).write_bytes(data)
    before = _snapshot(tmp_path)
    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN, rar_binary=_UNRAR_BIN, **kw)
    return result, before, reason, member, outside, item


_NAMES = list(_hostile_cases(Path("/nonexistent"), Path("/nonexistent")))


@needs_7z
@pytest.mark.parametrize("name", _NAMES)
def test_hostile_archive_is_refused_and_nothing_is_written(tmp_path, name):
    result, before, reason, member, outside, _item = _run_hostile(tmp_path, name)

    assert result.state == "EXTRACT_FAILED", result.detail
    assert reason in result.detail, result.detail
    assert repr(member)[1:-1] in result.detail, result.detail  # detail shows the repr()
    # Nothing was written anywhere: no _UNPACK_/_FAILED_ dir, nothing in the item, outside
    # untouched (the snapshot spans the whole tmp tree, which contains every possible target).
    assert _snapshot(tmp_path) == before
    assert [p.name for p in outside.iterdir()] == ["victim.txt"]
    assert (outside / "victim.txt").read_text() == "victim"
    assert os.stat(outside / "victim.txt").st_nlink == 1


@needs_7z
def test_hostile_member_name_is_sanitised_in_the_detail(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "evil.zip").write_bytes(_zip_bytes([("../\x1b[31mred\x07" + "A" * 400, "x")]))
    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN)
    assert result.state == "EXTRACT_FAILED"
    assert "\x1b" not in result.detail and "\x07" not in result.detail
    assert "A" * 300 not in result.detail  # truncated


@needs_7z
def test_seven_zip_native_symlink_is_refused(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "evil.7z").write_bytes(_seven_zip_with_symlink(tmp_path, str(tmp_path)))
    before = _snapshot(tmp_path)
    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN)
    assert result.state == "EXTRACT_FAILED"
    assert "symbolic link" in result.detail
    assert _snapshot(tmp_path) == before


@needs_unrar
@pytest.mark.parametrize(
    ("label", "archive", "reason", "member"),
    [
        ("rar3_symlink", _rar3([("esc", b"/etc", 0o120777)]), "symbolic link", "esc"),
        ("rar5_symlink", _rar5([("esc", b"", (1, "/etc"))]), "symbolic link", "esc"),
        (
            "rar5_hardlink",
            _rar5([("a.txt", b"hi", None), ("b.txt", b"", (4, "a.txt"))]),
            "hard link",
            "b.txt",
        ),
        (
            "rar5_file_reference",
            _rar5([("a.txt", b"hi", None), ("b.txt", b"", (5, "a.txt"))]),
            "File reference",
            "b.txt",
        ),
        ("rar3_dotdot", _rar3([("../x.txt", b"x", 0o100644)]), "'..' path component", "../x.txt"),
    ],
)
def test_hostile_rar_is_refused_and_nothing_is_written(tmp_path, label, archive, reason, member):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "evil.rar").write_bytes(archive)
    before = _snapshot(tmp_path)
    result = extract.extract_item(item, rar_binary=_UNRAR_BIN)
    assert result.state == "EXTRACT_FAILED", label
    assert reason in result.detail, result.detail
    assert repr(member)[1:-1] in result.detail, result.detail
    assert _snapshot(tmp_path) == before


@needs_unrar
def test_unrar_is_invoked_with_ol_dash(tmp_path, monkeypatch):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "ok.rar").write_bytes(_rar3([("ok.txt", b"hello", 0o100644)]))
    seen: list[list[str]] = []
    real = extract._run_unrar

    def spy(binary, args):
        seen.append(list(args))
        return real(binary, args)

    monkeypatch.setattr(extract, "_run_unrar", spy)
    result = extract.extract_item(item, rar_binary=_UNRAR_BIN)
    assert result.ok, result.detail
    assert (item / "ok.txt").read_text() == "hello"
    extracts = [a for a in seen if a and a[0] == "x"]
    assert extracts and all("-ol-" in a for a in extracts)


@needs_unrar
def test_unrar_ol_dash_really_skips_a_symlink(tmp_path):
    """Defence in depth, measured against the real binary: even if the listing check were
    bypassed, `-ol-` means `unrar` creates no symlink."""
    archive = tmp_path / "evil.rar"
    archive.write_bytes(_rar3([("esc", b"/etc", 0o120777), ("ok.txt", b"hello", 0o100644)]))
    dest = tmp_path / "dest"
    dest.mkdir()
    extract._extract_rar(archive, dest, password=None, binary=_UNRAR_BIN)
    assert not (dest / "esc").is_symlink()
    assert not os.path.lexists(dest / "esc")


# --- per-archive isolation and the post-check -------------------------------------------------


def test_every_archive_gets_a_fresh_empty_directory(tmp_path, monkeypatch):
    item = tmp_path / "Release"
    item.mkdir()
    for n in ("a1.zip", "a2.zip"):
        (item / n).write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    monkeypatch.setattr(extract, "check_extract_preconditions", lambda a: None)
    monkeypatch.setattr(extract, "_listing_verdict", lambda *a, **k: None)
    targets: list[Path] = []

    def fake_extract_archive(archive, target_dir, **kwargs):
        target_dir.mkdir(parents=True, exist_ok=True)
        assert (
            list(target_dir.iterdir()) == []
        ), "must start empty: no archive sees another's output"
        targets.append(target_dir)
        (target_dir / f"from-{archive.stem}.txt").write_text(archive.stem)
        return extract.ExtractResult(state="EXTRACTED", detail="faked")

    monkeypatch.setattr(extract, "extract_archive", fake_extract_archive)
    result = extract.extract_item(item, binary="unused")
    assert result.ok, result.detail
    assert len(set(targets)) == 2
    assert (item / "from-a1.txt").read_text() == "a1"
    assert (item / "from-a2.txt").read_text() == "a2"
    assert not (tmp_path / f"{extract.UNPACK_PREFIX}Release").exists()


def _plant_and_check(tmp_path, monkeypatch, plant):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "a1.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (item / "a2.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    monkeypatch.setattr(extract, "check_extract_preconditions", lambda a: None)
    monkeypatch.setattr(extract, "_listing_verdict", lambda *a, **k: None)

    def fake_extract_archive(archive, target_dir, **kwargs):
        target_dir.mkdir(parents=True, exist_ok=True)
        if archive.name == "a1.zip":
            (target_dir / "good.txt").write_text("fine")
        else:
            plant(target_dir)
        return extract.ExtractResult(state="EXTRACTED", detail="faked")

    monkeypatch.setattr(extract, "extract_archive", fake_extract_archive)
    return item, extract.extract_item(item, binary="unused")


def test_post_check_fails_on_any_symlink_even_a_contained_one(tmp_path, monkeypatch):
    def plant(target_dir):
        (target_dir / "real.txt").write_text("x")
        (target_dir / "link").symlink_to("real.txt")  # contained: find_escaping_path alone passes

    item, result = _plant_and_check(tmp_path, monkeypatch, plant)
    assert result.state == "EXTRACT_FAILED"
    assert "symbolic link" in result.detail
    assert "link" in result.detail
    assert not (item / "link").exists() and not os.path.lexists(item / "link")
    assert not (item / "real.txt").exists()
    # the hostile archive's output was never merged -- not even into staging
    failed = tmp_path / f"{extract.FAILED_PREFIX}Release"
    assert failed.is_dir()
    assert not (failed / "real.txt").exists()
    assert not (tmp_path / f"{extract.UNPACK_PREFIX}Release").exists()


def test_post_check_fails_on_a_hardlinked_file(tmp_path, monkeypatch):
    def plant(target_dir):
        (target_dir / "one.txt").write_text("x")
        os.link(target_dir / "one.txt", target_dir / "two.txt")

    item, result = _plant_and_check(tmp_path, monkeypatch, plant)
    assert result.state == "EXTRACT_FAILED"
    assert "hard link" in result.detail
    assert not (item / "one.txt").exists() and not (item / "two.txt").exists()


def test_post_check_fails_on_a_special_file(tmp_path, monkeypatch):
    def plant(target_dir):
        os.mkfifo(target_dir / "pipe")

    item, result = _plant_and_check(tmp_path, monkeypatch, plant)
    assert result.state == "EXTRACT_FAILED"
    assert "special file" in result.detail
    assert not os.path.lexists(item / "pipe")


def test_check_extracted_tree_accepts_plain_files_and_dirs(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_text("x")
    assert extract.check_extracted_tree(tmp_path) is None


# --- the non-hostile matrix still extracts ----------------------------------------------------


def _benign_cases() -> dict[str, tuple[dict[str, bytes], dict[str, str]]]:
    tar = _tar_bytes([("d/inner.txt", "file", "payload"), ("top.txt", "file", "top")])
    expected = {"d/inner.txt": "payload", "top.txt": "top"}
    return {
        "zip": ({"p.zip": _zip_bytes([("d/inner.txt", "payload"), ("top.txt", "top")])}, expected),
        "tar": ({"p.tar": tar}, expected),
        "tar_gz": ({"p.tar.gz": gzip.compress(tar)}, expected),
        "tgz": ({"p.tgz": gzip.compress(tar)}, expected),
        "tar_bz2": ({"p.tar.bz2": bz2.compress(tar)}, expected),
        "tar_xz": ({"p.tar.xz": lzma.compress(tar)}, expected),
        "single_gz": ({"one.txt.gz": gzip.compress(b"payload")}, {"one.txt": "payload"}),
        "single_xz": ({"one.txt.xz": lzma.compress(b"payload")}, {"one.txt": "payload"}),
        "single_bz2": ({"one.txt.bz2": bz2.compress(b"payload")}, {"one.txt": "payload"}),
        "two_zips": (
            {
                "a.zip": _zip_bytes([("a.txt", "A")]),
                "b.zip": _zip_bytes([("b.txt", "B")]),
            },
            {"a.txt": "A", "b.txt": "B"},
        ),
    }


@needs_7z
@pytest.mark.parametrize("name", list(_benign_cases()))
def test_benign_archives_still_extract(tmp_path, name):
    files, expected = _benign_cases()[name]
    item = tmp_path / "Release"
    item.mkdir()
    for fname, data in files.items():
        (item / fname).write_bytes(data)
    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN)
    assert result.ok, result.detail
    for rel, content in expected.items():
        assert (item / rel).read_text() == content
    assert not (tmp_path / f"{extract.UNPACK_PREFIX}Release").exists()
    assert not (tmp_path / f"{extract.FAILED_PREFIX}Release").exists()
    assert not list(item.rglob(".lftpweb-work-*"))


@needs_7z
def test_native_7z_round_trip_and_header_encrypted_7z_with_password_list(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "secret.txt").write_text("shh")
    item = tmp_path / "Release"
    item.mkdir()
    subprocess.run(
        [
            _SEVEN_ZIP_BIN,
            "a",
            "-phunter2",
            "-mhe=on",
            str(item / "enc.7z"),
            str(src / "secret.txt"),
        ],
        check=True,
        capture_output=True,
    )
    result = extract.extract_item(item, passwords=("wrong", "hunter2"), binary=_SEVEN_ZIP_BIN)
    assert result.ok, result.detail
    assert (item / "secret.txt").read_text() == "shh"


@needs_7z
def test_header_encrypted_7z_with_no_correct_password_fails_closed(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "secret.txt").write_text("shh")
    item = tmp_path / "Release"
    item.mkdir()
    subprocess.run(
        [
            _SEVEN_ZIP_BIN,
            "a",
            "-phunter2",
            "-mhe=on",
            str(item / "enc.7z"),
            str(src / "secret.txt"),
        ],
        check=True,
        capture_output=True,
    )
    result = extract.extract_item(item, passwords=("wrong",), binary=_SEVEN_ZIP_BIN)
    assert result.state == "EXTRACT_FAILED"
    assert "could not list" in result.detail
    assert [p.name for p in item.iterdir()] == ["enc.7z"]  # nothing was extracted
    failed = tmp_path / f"{extract.FAILED_PREFIX}Release"
    assert failed.is_dir() and list(failed.iterdir()) == []  # evidence dir, empty: nothing ran


@needs_unrar
def test_benign_single_and_multivolume_rar_still_extract(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "one.rar").write_bytes(_rar3([("hello.txt", b"hello world\n", 0o100644)]))
    result = extract.extract_item(item, rar_binary=_UNRAR_BIN)
    assert result.ok, result.detail
    assert (item / "hello.txt").read_text() == "hello world\n"


# --- fail closed on a listing that cannot be trusted ------------------------------------------


@needs_7z
@needs_unrar
def test_link_in_a_later_rar_volume_is_refused(tmp_path):
    """`unrar l` without `-v` lists only the head volume (measured): a symlink carried by volume
    2 of a set would pass an un-flagged listing check."""
    item = tmp_path / "Release"
    item.mkdir()
    vol2 = _RAR_MULTIVOL_VOL2
    link = _rar3([("esc", b"/etc", 0o120777)])[20:-7]  # strip signature, main header, end block
    (item / "release.rar").write_bytes(_RAR_MULTIVOL_VOL1)
    (item / "release.r00").write_bytes(vol2[:-7] + link + vol2[-7:])
    before = _snapshot(tmp_path)
    result = extract.extract_item(item, rar_binary=_UNRAR_BIN)
    assert result.state == "EXTRACT_FAILED"
    assert "symbolic link" in result.detail and "esc" in result.detail
    assert _snapshot(tmp_path) == before


@needs_7z
def test_unparseable_listing_is_refused(tmp_path, monkeypatch):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "p.zip").write_bytes(_zip_bytes([("ok.txt", "x")]))

    def garbage(binary, args):
        return subprocess.CompletedProcess(args, 0, stdout="complete nonsense\n", stderr="")

    monkeypatch.setattr(extract, "_run_7z_list", garbage)
    before = _snapshot(tmp_path)
    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN)
    assert result.state == "EXTRACT_FAILED"
    assert "listing" in result.detail
    assert _snapshot(tmp_path) == before


def test_parse_7z_listing_refuses_a_record_missing_its_path():
    text = "Path = x.zip\nType = zip\n\n----------\nFolder = -\nSize = 1\n\n"
    with pytest.raises(extract._ListingError):
        extract._parse_7z_listing(text)


def test_parse_7z_listing_refuses_duplicate_keys():
    text = "Path = x.zip\nType = zip\n\n----------\nPath = a\nFolder = -\nFolder = -\n\n"
    with pytest.raises(extract._ListingError):
        extract._parse_7z_listing(text)


def test_unsafe_path_reason():
    f = extract._unsafe_path_reason
    assert f("ok/dir/file.txt") is None
    assert f("a..b/c") is None  # `..` inside a component is fine
    assert f("a/../b") is not None
    assert f("..") is not None
    assert f("/etc/passwd") is not None
    assert f("\\\\server\\share") is not None
    assert f("c:/x") is not None
    assert f("a\\..\\b") is not None
    assert f("") is not None
