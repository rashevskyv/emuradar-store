"""build_store.py: asset picking, mini zip layout, repo.json fields, HTTP Range reader. Offline."""
import http.server
import io
import json
import os
import sys
import threading
import unittest
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
import build_store as bs  # noqa: E402


def make_zip(members):
    """members: {name: bytes}. Returns zip bytes (STORED so Range tests see real sizes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def asset(name, size=None, url=None):
    return {"name": name, "size": size or 1, "browser_download_url": url or f"https://example.invalid/{name}"}


def fake_open(blobs):
    """open_zip stand-in: asset name -> ZipFile over in-memory bytes."""
    return lambda a: zipfile.ZipFile(io.BytesIO(blobs[a["name"]]))


class Picking(unittest.TestCase):
    def test_bare_nro_asset_wins_over_zip(self):
        assets = [asset("port-switch.zip"), asset("port.nro", 500)]
        a, member, size = bs.pick_source(assets, "o/port", "port", fake_open({}))
        self.assertEqual((a["name"], member, size), ("port.nro", None, 500))

    def test_nro_inside_zip(self):
        blobs = {"port.zip": make_zip({"readme.txt": b"hi", "bin/port.nro": b"N" * 40})}
        a, member, size = bs.pick_source([asset("port.zip")], "o/port", "port", fake_open(blobs))
        self.assertEqual((a["name"], member, size), ("port.zip", "bin/port.nro", 40))

    def test_several_nro_best_name_match_then_largest(self):
        blobs = {"all.zip": make_zip({"debug-tool.nro": b"x" * 10, "__MACOSX/port.nro": b"y" * 99,
                                      "dolphin-nx.nro": b"z" * 5, "big.nro": b"w" * 50})}
        a, member, _ = bs.pick_source([asset("all.zip")], "o/dolphin-nx", "dolphin", fake_open(blobs))
        self.assertEqual(member, "dolphin-nx.nro")
        blobs = {"all.zip": make_zip({"aaa.nro": b"x" * 10, "zzz.nro": b"y" * 50})}
        a, member, _ = bs.pick_source([asset("all.zip")], "o/other", "other", fake_open(blobs))
        self.assertEqual(member, "zzz.nro")

    def test_unsupported_archives_are_skipped_with_reason(self):
        with self.assertRaises(bs.Skip) as cm:
            bs.pick_source([asset("port.rar")], "o/p", "p", fake_open({}))
        self.assertIn("not supported", str(cm.exception))

    def test_tar_and_7z_archives_give_their_nro(self):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            info = tarfile.TarInfo("dir/game.nro")
            info.size = 3
            t.addfile(info, io.BytesIO(b"abc"))
        a = asset("game.tar.gz")
        a["browser_download_url"] = "http://x/game.tar.gz"
        bs._ARCHIVE.update(url=a["browser_download_url"], data=buf.getvalue())
        got = bs.pick_source([a], "o/p", "game", fake_open({}))
        self.assertEqual(got[1], "dir/game.nro")
        self.assertEqual(bs.fetch_nro(a, got[1], fake_open({})), b"abc")

    def test_zip_without_nro_reason(self):
        blobs = {"src.zip": make_zip({"main.cpp": b"int main(){}"})}
        with self.assertRaises(bs.Skip) as cm:
            bs.pick_source([asset("src.zip")], "o/p", "p", fake_open(blobs))
        self.assertIn("no .nro", str(cm.exception))

    def test_no_assets_reason(self):
        with self.assertRaises(bs.Skip):
            bs.pick_source([asset("notes.txt")], "o/p", "p", fake_open({}))

    def test_broken_zip_does_not_stop_other_zips(self):
        blobs = {"bad.zip": b"not a zip", "good.zip": make_zip({"p.nro": b"n"})}
        a, member, _ = bs.pick_source([asset("bad.zip"), asset("good.zip")], "o/p", "p", fake_open(blobs))
        self.assertEqual((a["name"], member), ("good.zip", "p.nro"))


class MiniZip(unittest.TestCase):
    def test_layout_manifest_and_info(self):
        data, path = bs.build_zip("dolphin-nx", "Dolphin", "v1.2", "dolphin.nro", b"NRO0")
        self.assertEqual(path, "switch/dolphin-nx/dolphin.nro")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertEqual(sorted(z.namelist()), ["info.json", "manifest.install", "switch/dolphin-nx/dolphin.nro"])
            self.assertEqual(z.read("switch/dolphin-nx/dolphin.nro"), b"NRO0")
            self.assertEqual(z.read("manifest.install"), b"U: switch/dolphin-nx/dolphin.nro\n")
            self.assertEqual(json.loads(z.read("info.json")),
                             {"version": "v1.2", "name": "dolphin-nx", "title": "Dolphin"})

    def test_manifest_lines_match_installer_rules(self):
        for line in bs.manifest_text(["switch/a/a.nro"]).splitlines():
            self.assertGreater(len(line), 3)
            self.assertEqual(line[1:3], ": ")
            self.assertNotEqual(line[3], "/")

    def test_zip_bytes_are_reproducible(self):
        a, _ = bs.build_zip("x", "X", "1", "x.nro", b"abc")
        b, _ = bs.build_zip("x", "X", "1", "x.nro", b"abc")
        self.assertEqual(a, b)


class RepoEntry(unittest.TestCase):
    def test_fields(self):
        e = {"name": "dolphin-nx", "title": "Dolphin", "repo": "NaGaa95/dolphin-nx", "section": "Emulators"}
        rel = {"tag_name": "v2.0", "published_at": "2026-10-07T10:00:00Z", "body": "notes"}
        entry = bs.repo_entry(e, rel, 2048 * 3, 1024 * 10, "d41d8cd9", "/switch/dolphin-nx/dolphin.nro",
                              "https://github.com/rashevskyv/emuradar-store/releases/download/store/dolphin-nx.zip")
        self.assertEqual(entry["category"], "port")
        self.assertEqual(entry["updated"], "07/10/2026")
        self.assertEqual((entry["filesize"], entry["extracted"]), (6, 10))
        self.assertEqual(entry["author"], "NaGaa95")
        self.assertEqual(entry["url"], "https://github.com/NaGaa95/dolphin-nx")
        self.assertEqual(entry["description"], "Switch port: Emulators")
        self.assertEqual(entry["binary"], "/switch/dolphin-nx/dolphin.nro")
        self.assertEqual(entry["download"],
                         "https://github.com/rashevskyv/emuradar-store/releases/download/store/dolphin-nx.zip")
        self.assertEqual(entry["version"], "v2.0")
        for key in ("name", "title", "md5", "details", "changelog", "license", "screens", "app_dls"):
            self.assertIn(key, entry)

    def test_manual_description_wins(self):
        e = {"name": "a", "repo": "o/a", "section": "PC", "description": "Mine"}
        entry = bs.repo_entry(e, {}, 1, 1, "x", "/switch/a/a.nro", "u")
        self.assertEqual(entry["description"], "Mine")
        self.assertEqual(entry["updated"], "")


class RangeReader(unittest.TestCase):
    """A local HTTP server with Range support; counts the bytes it sends."""

    @classmethod
    def setUpClass(cls):
        cls.payload = make_zip({"filler.bin": os.urandom(3 * 1024 * 1024), "switch/p/p.nro": b"NRO" * 1000})
        cls.served = 0
        cls.range_ok = True
        outer = cls

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                data = outer.payload
                rng = self.headers.get("Range")
                if rng and outer.range_ok:
                    first, last = rng.split("=", 1)[1].split("-")
                    first = int(first)
                    last = min(int(last), len(data) - 1)
                    chunk = data[first:last + 1]
                    code = 206
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {first}-{last}/{len(data)}")
                else:
                    chunk = data
                    code = 200
                    self.send_response(200)
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                if code == 206:  # only ranged reads count; full bodies may be left unread
                    outer.served += len(chunk)
                self.wfile.write(chunk)

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/asset.zip"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        RangeReader.served = 0
        RangeReader.range_ok = True

    def test_reads_only_the_member_not_the_whole_zip(self):
        with zipfile.ZipFile(bs.RangeFile(self.url)) as zf:
            self.assertEqual(zf.read("switch/p/p.nro"), b"NRO" * 1000)
        self.assertLess(RangeReader.served, len(self.payload) // 10)

    def test_seek_tell_and_partial_reads(self):
        f = bs.RangeFile(self.url)
        f.seek(-22, 2)
        self.assertEqual(len(f.read(22)), 22)
        f.seek(5)
        self.assertEqual(f.tell(), 5)
        self.assertEqual(f.read(10), self.payload[5:15])
        self.assertEqual(f.read(-1)[:4], self.payload[15:19])

    def test_server_without_range_raises_unsupported(self):
        RangeReader.range_ok = False
        with self.assertRaises(bs.RangeUnsupported):
            bs.RangeFile(self.url)

    def test_open_remote_zip_falls_back_for_small_assets(self):
        RangeReader.range_ok = False
        small = asset("asset.zip", size=1024, url=self.url)
        with bs.open_remote_zip(small) as zf:
            self.assertIn("switch/p/p.nro", zf.namelist())

    def test_open_remote_zip_refuses_big_full_download(self):
        RangeReader.range_ok = False
        big = asset("asset.zip", size=bs.FULL_DOWNLOAD_LIMIT + 1, url=self.url)
        with self.assertRaises(bs.Skip):
            bs.open_remote_zip(big)


if __name__ == "__main__":
    unittest.main()


def test_nro_icon_reads_aset_block():
    import struct
    import build_store as b
    jpg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"icon"
    body = bytearray(0x20)
    body[0x10:0x14] = b"NRO0"
    struct.pack_into("<I", body, 0x18, len(body))
    aset = b"ASET" + bytes(4) + struct.pack("<QQ", 0x38, len(jpg)) + bytes(0x20) + jpg
    assert b.nro_icon(bytes(body) + aset) == jpg
    assert b.nro_icon(bytes(body)) is None


def test_7z_archive_gives_its_nro():
    import io
    import pytest
    py7zr = pytest.importorskip("py7zr")
    import build_store as b
    buf = io.BytesIO()
    with py7zr.SevenZipFile(buf, "w") as z:
        z.writestr(b"nro-bytes", "game/game.nro")
    a = {"name": "game.7z", "size": 1, "browser_download_url": "http://x/game.7z"}
    b._ARCHIVE.update(url=a["browser_download_url"], data=buf.getvalue())
    got = b.pick_source([a], "o/g", "game", None)
    assert got[1] == "game/game.nro"
    assert b.fetch_nro(a, got[1], None) == b"nro-bytes"
