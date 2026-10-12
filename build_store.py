#!/usr/bin/env python3
"""Build the Emu Radar Store: one Kefir Hub package per catalog.json entry.

    python build_store.py --repo owner/emuradar-store          build, upload to release 'store'
    python build_store.py --no-upload --out out/               build zips locally only

Files written to --out (default: repo root):
    repo.json          package list in hbstore field names; committed; read by Kefir Hub
    skipped.json       entries with no usable Switch .nro, with the reason
    zips/<name>.zip    one mini zip per package; NOT committed, uploaded as assets of release 'store'

A mini zip holds switch/<name>/<file>.nro, manifest.install and info.json, nothing else.
Standard library only. Set GITHUB_TOKEN to lift the anonymous API limit.
"""
import argparse
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import tarfile
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"
USER_AGENT = "emuradar-store-builder"
CATEGORY = "port"
STORE_TAG = "store"
STORE_TITLE = "Emu Radar Store packages"
STORE_NOTES = "Rolling release with the package zips listed in repo.json. Written by the store workflow only."
ARCHIVE_EXTS = (".7z", ".rar", ".tar", ".tgz", ".txz", ".zst", ".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst")
ARCHIVE_LIMIT = 300 * 1024 * 1024         # largest .7z/.tar we download whole to take one .nro out
FULL_DOWNLOAD_LIMIT = 80 * 1024 * 1024   # largest zip we download whole when Range is not supported
RANGE_BLOCK = 256 * 1024                 # read-ahead for the remote zip reader
MAX_WAIT = 120                           # longest we sleep for the API limit before stopping
ZIP_TIME = (1980, 1, 1, 0, 0, 0)         # fixed zip timestamps keep md5 reproducible
UPLOAD_BATCH = 20


class Skip(Exception):
    """This entry has no usable Switch build. The reason goes to skipped.json."""


class RateLimited(Exception):
    """The GitHub API limit is hit and will not reset soon. Stop the run gracefully."""


class RangeUnsupported(Exception):
    """The server ignored the Range header."""


# ---- pure helpers (tested in tests/test_build_store.py) ----------------------------

def manifest_text(paths):
    """hbstore manifest: one 'U: path' line per file, LF endings, no leading slash."""
    return "".join(f"U: {p}\n" for p in paths)


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def name_score(stem, repo, name):
    """2 = same as repo or entry name, 1 = one contains the other, 0 = unrelated."""
    s = _norm(stem)
    if not s:
        return 0
    targets = [t for t in (_norm(repo.split("/")[1]), _norm(name)) if t]
    if s in targets:
        return 2
    if any(t in s or s in t for t in targets):
        return 1
    return 0


UNPACK_EXTS = (".7z", ".tar", ".tgz", ".txz", ".tar.gz", ".tar.xz", ".tar.bz2")
_ARCHIVE = {"url": None, "data": b""}   # the archive last downloaded, so listing and reading fetch it once


def _archive_bytes(asset):
    if _ARCHIVE["url"] != asset["browser_download_url"]:
        if asset.get("size", 0) > ARCHIVE_LIMIT:
            raise Skip(f"{asset['name']}: archive over {ARCHIVE_LIMIT // 1024 // 1024} MB")
        _ARCHIVE["url"], _ARCHIVE["data"] = asset["browser_download_url"], download(asset["browser_download_url"])
    return _ARCHIVE["data"]


def archive_nros(asset):
    """[(member, size)] of the .nro files in a .7z or .tar* asset."""
    data = _archive_bytes(asset)
    if asset["name"].lower().endswith(".7z"):
        import py7zr
        with py7zr.SevenZipFile(io.BytesIO(data)) as z:
            return [(i.filename, i.uncompressed) for i in z.list() if not i.is_directory and i.filename.lower().endswith(".nro")]
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        return [(m.name, m.size) for m in t.getmembers() if m.isfile() and m.name.lower().endswith(".nro")]


def archive_read(asset, member):
    data = _archive_bytes(asset)
    if asset["name"].lower().endswith(".7z"):
        import py7zr
        with py7zr.SevenZipFile(io.BytesIO(data)) as z:
            return z.read([member])[member].read()
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        return t.extractfile(member).read()


def pick_source(assets, repo, name, open_zip):
    """Choose the .nro to ship. Returns (asset, member, size); member is None for a bare .nro.

    Bare .nro assets win. Otherwise every .zip asset is listed (open_zip gives a ZipFile)
    and its .nro members are candidates. Raises Skip with the reason when there are none.
    """
    cands = [(a, None, a.get("size", 0)) for a in assets if a["name"].lower().endswith(".nro")]
    errors = []
    zips = [a for a in assets if a["name"].lower().endswith(".zip")]
    if not cands:
        for a in zips:
            try:
                with open_zip(a) as zf:
                    for info in zf.infolist():
                        member = info.filename
                        if member.endswith("/") or member.startswith("__MACOSX/"):
                            continue
                        if member.lower().endswith(".nro"):
                            cands.append((a, member, info.file_size))
            except Skip as e:
                errors.append(str(e))
            except zipfile.BadZipFile:
                errors.append(f"{a['name']}: not a valid zip")
    if not cands:
        for a in assets:
            if a["name"].lower().endswith(UNPACK_EXTS):
                try:
                    cands += [(a, m, size) for m, size in archive_nros(a)]
                except Skip as e:
                    errors.append(str(e))
                except Exception as e:  # a broken archive must not stop the run
                    errors.append(f"{a['name']}: cannot read ({type(e).__name__})")
    if not cands:
        if zips or errors:
            raise Skip("; ".join(errors) or "no .nro inside the archive assets")
        if any(a["name"].lower().endswith(ARCHIVE_EXTS) for a in assets):
            raise Skip("only .rar/.zst archives: not supported")
        raise Skip("no .nro or .zip asset")
    cands.sort(key=lambda c: (-name_score(_stem(c), repo, name), -c[2], c[0]["name"], c[1] or ""))
    return cands[0]


def _stem(cand):
    base = os.path.basename(cand[1] or cand[0]["name"])
    return base[:-4] if base.lower().endswith(".nro") else base


def build_zip(name, title, tag, nro_name, nro_bytes):
    """The mini zip: switch/<name>/<nro>, manifest.install and info.json."""
    path = f"switch/{name}/{nro_name}"
    info = json.dumps({"version": tag, "name": name, "title": title}, ensure_ascii=False) + "\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for member, data in ((path, nro_bytes),
                             ("manifest.install", manifest_text([path]).encode("utf-8")),
                             ("info.json", info.encode("utf-8"))):
            zi = zipfile.ZipInfo(member, date_time=ZIP_TIME)
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
    return buf.getvalue(), path


def repo_entry(e, release, zip_len, nro_size, md5, binary, download):
    """One package object of repo.json, in the field names hbstore uses."""
    published = (release.get("published_at") or "")[:10]
    updated = datetime.strptime(published, "%Y-%m-%d").strftime("%d/%m/%Y") if published else ""
    owner = e["repo"].split("/")[0]
    description = e.get("description") or (f"Switch port: {e['section']}" if e.get("section") else "Switch port")
    entry = {
        "category": CATEGORY,
        "name": e["name"],
        "title": e.get("title") or e["name"],
        "author": e.get("author") or owner,
        "version": release.get("tag_name", ""),
        "updated": updated,
        "description": description,
        "details": e.get("details") or description,
        "changelog": (release.get("body") or "")[:4000],
        "url": e.get("url") or f"https://github.com/{e['repo']}",
        "license": e.get("license", ""),
        "binary": binary,
        "filesize": max(1, zip_len // 1024),
        "extracted": max(1, nro_size // 1024),
        "md5": md5,
        "download": download,
        "screens": 0,
        "app_dls": 0,
    }
    if e.get("icon"):
        entry["icon"] = e["icon"]
    return entry


def apply_description(entry, e):
    """The repo's own GitHub description replaces the generic "Switch port: <platform>" text."""
    info = api_get(f"/repos/{e['repo']}", allow404=True) or {}
    text = " ".join((info.get("description") or "").split())
    if not text:
        return
    platform = "\n\nOriginal platform: " + e["section"] if e.get("section") else ""
    entry["description"] = text
    entry["details"] = (text + platform + "\n\n" + entry.get("url", "")).strip()


def download_url(repo, name):
    return f"https://github.com/{repo}/releases/download/{STORE_TAG}/{name}.zip"


# ---- HTTP -------------------------------------------------------------------------

_RATE = {"remaining": None, "reset": None}


def _open(url, headers=None, api=False):
    h = {"User-Agent": USER_AGENT}
    if api:
        h["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            h["Authorization"] = f"Bearer {token}"
    h.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120)


def _retry(fn, tries=3):
    """Retry network errors and 5xx answers. Other HTTP errors are raised at once."""
    for attempt in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == tries - 1:
                raise
        except OSError:
            if attempt == tries - 1:
                raise
        time.sleep(2 * (attempt + 1))


def _wait_or_stop(seconds):
    seconds = max(1, int(seconds))
    if seconds > MAX_WAIT:
        raise RateLimited(f"GitHub API limit, resets in {seconds} s")
    print(f"  API limit: sleeping {seconds} s", file=sys.stderr)
    time.sleep(seconds + 1)
    _RATE["remaining"] = None


def _seconds_until_reset(headers):
    reset = headers.get("X-RateLimit-Reset") or ""
    if reset.isdigit():
        return int(reset) - time.time()
    return 60


def api_get(path, allow404=False):
    """GET one JSON document from the GitHub API. Returns None on 404 when allow404."""
    url = API + path
    for _ in range(3):
        if _RATE["remaining"] is not None and _RATE["remaining"] <= 5:
            _wait_or_stop(_RATE["reset"] or 60)
        try:
            with _open(url, api=True) as r:
                _RATE["remaining"] = _int_or_none(r.headers.get("X-RateLimit-Remaining"))
                _RATE["reset"] = _seconds_until_reset(r.headers)
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404 and allow404:
                return None
            if e.code == 429 or (e.code == 403 and (e.headers.get("X-RateLimit-Remaining") == "0" or e.headers.get("Retry-After"))):
                wait = e.headers.get("Retry-After")
                _wait_or_stop(int(wait) if wait and wait.isdigit() else _seconds_until_reset(e.headers))
                continue
            if e.code >= 500:
                time.sleep(3)
                continue
            raise
        except OSError:
            time.sleep(3)
    raise RateLimited(f"GitHub API kept failing: {url}")


def _int_or_none(text):
    return int(text) if text and text.isdigit() else None


def download(url):
    def go():
        with _open(url) as r:
            return r.read()
    return _retry(go)


class RangeFile(io.RawIOBase):
    """Read-only seekable file over HTTP Range requests, so zipfile reads only what it needs."""

    def __init__(self, url):
        self.url = url
        self._pos = 0
        self._buf = b""
        self._buf_start = 0
        with _open(url, {"Range": "bytes=0-0"}) as r:
            if r.status != 206:
                raise RangeUnsupported(url)
            m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range", ""))
        if not m:
            raise RangeUnsupported(url)
        self._size = int(m.group(1))

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        base = {0: 0, 1: self._pos, 2: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def read(self, size=-1):
        if size is None or size < 0:
            size = self._size - self._pos
        size = min(size, self._size - self._pos)
        if size <= 0:
            return b""
        end = self._pos + size
        if not (self._buf_start <= self._pos and end <= self._buf_start + len(self._buf)):
            first = self._pos
            last = min(self._size, first + max(size, RANGE_BLOCK)) - 1
            self._buf = self._get(first, last)
            self._buf_start = first
        off = self._pos - self._buf_start
        data = self._buf[off:off + size]
        self._pos += len(data)
        return data

    def _get(self, first, last):
        def go():
            with _open(self.url, {"Range": f"bytes={first}-{last}"}) as r:
                if r.status != 206:
                    raise RangeUnsupported(self.url)
                return r.read()
        data = _retry(go)
        if len(data) != last - first + 1:
            raise OSError(f"short range read from {self.url}")
        return data


def open_remote_zip(asset):
    """ZipFile over a release asset. Falls back to a full download below 80 MB only."""
    url = asset["browser_download_url"]
    try:
        return zipfile.ZipFile(RangeFile(url))
    except RangeUnsupported:
        if asset.get("size", 0) > FULL_DOWNLOAD_LIMIT:
            raise Skip(f"{asset['name']}: no HTTP Range support and over 80 MB")
        return zipfile.ZipFile(io.BytesIO(download(url)))


def fetch_nro(asset, member, open_zip):
    if member is None:
        return download(asset["browser_download_url"])
    if asset["name"].lower().endswith(UNPACK_EXTS):
        return archive_read(asset, member)
    with open_zip(asset) as zf:
        return zf.read(member)


# ---- GitHub releases --------------------------------------------------------------

def latest_release(repo):
    """Latest stable release, else the newest non-draft release (pre-releases included)."""
    rel = api_get(f"/repos/{repo}/releases/latest", allow404=True)
    if rel is None:
        rels = api_get(f"/repos/{repo}/releases?per_page=10", allow404=True) or []
        rel = next((r for r in rels if not r.get("draft")), None)
    return rel


def store_asset_names(repo):
    rel = api_get(f"/repos/{repo}/releases/tags/{STORE_TAG}", allow404=True)
    return {a["name"] for a in rel.get("assets", [])} if rel else set()


# ---- files and upload -------------------------------------------------------------

def load_previous(path):
    try:
        with open(path, encoding="utf-8") as f:
            return {e["name"]: e for e in json.load(f).get("packages", [])}
    except (OSError, ValueError):
        return {}


def _gh(args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def ensure_store_release(repo):
    if _gh(["release", "view", STORE_TAG, "--repo", repo]).returncode == 0:
        return
    r = _gh(["release", "create", STORE_TAG, "--repo", repo, "--title", STORE_TITLE,
             "--notes", STORE_NOTES, "--latest=false"])
    if r.returncode:
        raise RuntimeError(f"cannot create release {STORE_TAG}: {r.stderr.strip()}")


def upload(repo, paths):
    """Upload zips to release 'store'. Returns the set of package names that failed."""
    failed = set()
    for i in range(0, len(paths), UPLOAD_BATCH):
        batch = paths[i:i + UPLOAD_BATCH]
        if _gh(["release", "upload", STORE_TAG, *batch, "--clobber", "--repo", repo]).returncode == 0:
            continue
        for p in batch:  # one bad file must not fail the batch
            r = _gh(["release", "upload", STORE_TAG, p, "--clobber", "--repo", repo])
            if r.returncode:
                failed.add(os.path.splitext(os.path.basename(p))[0])
                print(f"  upload failed: {os.path.basename(p)}: {r.stderr.strip()}", file=sys.stderr)
    return failed


# ---- icons ------------------------------------------------------------------------

def nro_icon(nro):
    """The JPEG icon stored after the NRO (ASET block), or None."""
    if len(nro) < 0x20 or nro[0x10:0x14] != b"NRO0":
        return None
    a = struct.unpack_from("<I", nro, 0x18)[0]
    if nro[a:a + 4] != b"ASET":
        return None
    off, size = struct.unpack_from("<QQ", nro, a + 8)
    icon = nro[a + off:a + off + size]
    return icon if size and icon[:2] == bytes([0xFF, 0xD8]) else None


def write_icon(out_dir, name, nro):
    """packages/<name>/icon.png, the path Kefir Hub requests. True when a file was written."""
    jpg = nro_icon(nro)
    if not jpg:
        return False
    path = os.path.join(out_dir, "packages", name, "icon.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        from PIL import Image
        Image.open(io.BytesIO(jpg)).convert("RGBA").save(path, "PNG", optimize=True)
    except ImportError:  # Hub decodes by content, not by extension
        with open(path, "wb") as f:
            f.write(jpg)
    return True


def ensure_icon(out_dir, old):
    """An already built package without an icon file: read it from its own store zip."""
    name = old["name"]
    if os.path.exists(os.path.join(out_dir, "packages", name, "icon.png")) or not old.get("download"):
        return
    try:
        with zipfile.ZipFile(io.BytesIO(download(old["download"]))) as z:
            for m in z.namelist():
                if m.lower().endswith(".nro"):
                    write_icon(out_dir, name, z.read(m))
                    return
    except Exception as ex:  # icons are cosmetic
        print(f"  icon for {name}: {ex}", file=sys.stderr)


# ---- run --------------------------------------------------------------------------

def process_entry(e, old, store_repo, zips_dir, store_assets):
    """Return (entry, built). built is False when the previous entry is still good.

    store_assets is the set of asset names on release 'store', or None when not uploading.
    """
    rel = latest_release(e["repo"])
    if rel is None:
        raise Skip("no published release")
    tag = rel.get("tag_name") or ""
    zip_path = os.path.join(zips_dir, f"{e['name']}.zip")
    have_zip = (f"{e['name']}.zip" in store_assets) if store_assets is not None else os.path.exists(zip_path)
    if old and old.get("version") == tag and old.get("download") and have_zip:
        ensure_icon(os.path.dirname(zips_dir), old)
        if old.get("description", "").startswith("Switch port"):
            old = dict(old)
            apply_description(old, e)
        return old, False

    asset, member, nro_size = pick_source(rel.get("assets") or [], e["repo"], e["name"], open_remote_zip)
    nro_name = os.path.basename(member or asset["name"])
    nro_bytes = fetch_nro(asset, member, open_remote_zip)
    write_icon(os.path.dirname(zips_dir), e["name"], nro_bytes)
    zip_bytes, path = build_zip(e["name"], e.get("title", ""), tag, nro_name, nro_bytes)
    os.makedirs(zips_dir, exist_ok=True)
    with open(zip_path, "wb") as f:
        f.write(zip_bytes)
    md5 = hashlib.md5(zip_bytes).hexdigest()
    entry = repo_entry(e, rel, len(zip_bytes), len(nro_bytes), md5, "/" + path,
                       download_url(store_repo, e["name"]))
    apply_description(entry, e)
    print(f"  {e['name']}: {tag} <- {asset['name']}" + (f" [{member}]" if member else ""))
    return entry, True


def run(args):
    with open(args.catalog, encoding="utf-8") as f:
        entries = json.load(f)["entries"]
    if args.limit:
        entries = entries[:args.limit]
    out = os.path.abspath(args.out)
    zips_dir = os.path.join(out, "zips")
    previous = load_previous(os.path.join(out, "repo.json"))
    store_assets = None if args.no_upload else store_asset_names(args.repo)

    result, new_names, skipped = {}, set(), []
    rate_stop = None
    total = len(entries)
    for i, e in enumerate(entries, 1):
        name, old = e["name"], previous.get(e["name"])
        if rate_stop:
            if old:
                result[name] = old
            else:
                skipped.append({"name": name, "repo": e["repo"], "reason": f"not checked: {rate_stop}"})
            continue
        print(f"[{i}/{total}] {name} ({e['repo']})")
        try:
            entry, built = process_entry(e, old, args.repo, zips_dir, store_assets)
            result[name] = entry
            if built:
                new_names.add(name)
        except RateLimited as ex:
            rate_stop = str(ex)
            print(f"  stopping: {ex}", file=sys.stderr)
            if old:
                result[name] = old
            else:
                skipped.append({"name": name, "repo": e["repo"], "reason": f"not checked: {rate_stop}"})
        except Skip as ex:
            print(f"  skipped: {ex}")
            skipped.append({"name": name, "repo": e["repo"], "reason": str(ex)})
        except Exception as ex:  # one broken package must not fail the run
            reason = f"error: {type(ex).__name__}: {ex}"
            print(f"  {reason}", file=sys.stderr)
            if old:
                result[name] = old
                reason += " (previous version kept)"
            skipped.append({"name": name, "repo": e["repo"], "reason": reason})

    if not args.no_upload and new_names:
        ensure_store_release(args.repo)
        paths = [os.path.join(zips_dir, f"{n}.zip") for n in sorted(new_names)]
        for n in sorted(upload(args.repo, paths)):
            new_names.discard(n)
            old = previous.get(n)
            if old:
                result[n] = old
            else:
                result.pop(n, None)
                skipped.append({"name": n, "repo": next(e["repo"] for e in entries if e["name"] == n),
                                "reason": "upload to release store failed"})

    packages = [result[n] for n in sorted(result)]
    skipped.sort(key=lambda s: s["name"])
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "repo.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"packages": packages}, ensure_ascii=False, indent=1) + "\n")
    with open(os.path.join(out, "skipped.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(skipped, ensure_ascii=False, indent=1) + "\n")

    report(packages, skipped, new_names, zips_dir, out)
    return 0


def report(packages, skipped, built_names, zips_dir, out):
    sizes = []
    for p in packages:
        path = os.path.join(zips_dir, f"{p['name']}.zip")
        if os.path.exists(path):
            sizes.append((os.path.getsize(path), p["name"]))
    total = sum(s for s, _ in sizes)
    print()
    print(f"catalog entries processed: {len(packages) + len(skipped)}")
    print(f"packages in repo.json: {len(packages)} (built this run: {len(built_names)}, reused: {len(packages) - len(built_names)})")
    print(f"skipped: {len(skipped)}")
    for reason, n in Counter(re.sub(r"\(previous version kept\)", "", s["reason"]).strip()[:90] for s in skipped).most_common(8):
        print(f"  {n:4d}  {reason}")
    print(f"zips total: {total / 1024 / 1024:.1f} MiB in {zips_dir}")
    for size, name in sorted(sizes, reverse=True)[:5]:
        print(f"  {size / 1024 / 1024:8.2f} MiB  {name}")
    print(f"written: {os.path.join(out, 'repo.json')}, {os.path.join(out, 'skipped.json')}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--catalog", default=os.path.join(ROOT, "catalog.json"))
    ap.add_argument("--out", default=ROOT, help="folder for repo.json, skipped.json and zips/")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/emuradar-store")
    ap.add_argument("--no-upload", action="store_true", help="write zips locally only")
    ap.add_argument("--limit", type=int, default=0, help="process only the first N catalog entries")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if not args.repo:
        ap.error("--repo owner/emuradar-store is required (or set GITHUB_REPOSITORY)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
