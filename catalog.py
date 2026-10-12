#!/usr/bin/env python3
"""Build catalog.json: one entry per GitHub repo listed in the SwitchPorts README.

    python catalog.py                  fetch the README, merge manual.json
    python catalog.py --readme FILE    use a saved copy (offline)

Output is sorted and indented so diffs stay readable. Standard library only.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
README_URL = "https://raw.githubusercontent.com/ChanseyIsTheBest/SwitchPorts/main/README.md"
USER_AGENT = "emuradar-store-catalog"

COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
SECTION_RE = re.compile(r"^###\s+(.+?)\s*$")
GITHUB_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
CELL_SEP_RE = re.compile(r"^:?-{3,}:?$")
FIRST_KEYS = ("name", "title", "repo", "section", "version_hint")


# ---- README parsing --------------------------------------------------------------

def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def is_separator(cells):
    return bool(cells) and all(CELL_SEP_RE.match(c) for c in cells)


def column(header, names, default):
    if header:
        for i, h in enumerate(header):
            if h in names:
                return i
    return default


def parse_readme(text):
    """Return (rows, rows_without_link). Each row: title, repo, section, version_hint."""
    lines = COMMENT_RE.sub("", text).splitlines()
    rows = []
    no_link = 0
    section = None
    header = None
    for i, raw in enumerate(lines):
        line = raw.strip()
        m = SECTION_RE.match(line)
        if m:
            section, header = m.group(1), None
            continue
        if "|" not in line:
            header = None
            continue
        cells = split_row(line)
        if is_separator(cells):
            continue
        nxt = split_row(lines[i + 1]) if i + 1 < len(lines) and "|" in lines[i + 1] else []
        if is_separator(nxt):
            header = [c.lower() for c in cells]
            continue
        if not any(cells):
            continue
        title_i = column(header, ("title", "game"), 0)
        version_i = column(header, ("version",), 1)
        g = GITHUB_RE.search(line)
        if not g:
            no_link += 1
            continue
        repo_name = re.sub(r"\.git$", "", g.group(2))
        rows.append({
            "title": cells[title_i] if title_i < len(cells) else "",
            "repo": f"{g.group(1)}/{repo_name}",
            "section": section or "",
            "version_hint": cells[version_i] if version_i < len(cells) else "",
        })
    return rows, no_link


# ---- names -----------------------------------------------------------------------

def slugify(text):
    return re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-")


def assign_names(repos):
    """Repo name slug; on a collision every clashing repo gets its owner prefixed."""
    base = [slugify(r.split("/")[1]) or slugify(r.replace("/", "-")) for r in repos]
    counts = Counter(base)
    names = []
    for repo, b in zip(repos, base):
        names.append(slugify(repo.replace("/", "-")) if counts[b] > 1 else b)
    seen = set()
    out = []
    for n in names:  # last resort, only if two different repos still give one slug
        cand, k = n, 2
        while cand in seen:
            cand, k = f"{n}-{k}", k + 1
        seen.add(cand)
        out.append(cand)
    return out


def derived_title(repo):
    return re.sub(r"[-_]+", " ", repo.split("/")[1]).strip()


# ---- catalog ---------------------------------------------------------------------

def load_manual(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    for item in items:
        if not isinstance(item.get("repo"), str) or item["repo"].count("/") != 1:
            raise ValueError(f"manual.json: entry needs repo 'owner/name': {item!r}")
    return items


def build_catalog(readme_text, manual=()):
    rows, no_link = parse_readme(readme_text)
    by_repo = {}
    duplicates = 0
    for row in rows:
        key = row["repo"].lower()
        if key in by_repo:
            duplicates += 1
            continue
        by_repo[key] = row
    for item in manual:  # manual entries win over parsed ones, field by field
        key = item["repo"].lower()
        extra = {k: v for k, v in item.items() if k != "name" and v not in (None, "")}
        if key in by_repo:
            by_repo[key].update(extra)
        else:
            by_repo[key] = {"title": "", "repo": item["repo"], "section": "", "version_hint": "", **extra}

    items = sorted(by_repo.values(), key=lambda e: e["repo"].lower())
    for item, name in zip(items, assign_names([e["repo"] for e in items])):
        item["name"] = name
        item["title"] = item.get("title") or derived_title(item["repo"])
    entries = []
    for item in sorted(items, key=lambda e: e["name"]):
        ordered = {k: item.get(k, "") for k in FIRST_KEYS}
        for k in sorted(item):
            if k not in ordered:
                ordered[k] = item[k]
        entries.append(ordered)
    return {
        "source": README_URL,
        "rows_without_github_link": no_link,
        "duplicate_rows": duplicates,
        "entries": entries,
    }


def fetch_readme(source):
    if os.path.exists(source):
        with open(source, encoding="utf-8") as f:
            return f.read()
    req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--readme", default=README_URL, help="URL or local file of the README")
    ap.add_argument("--manual", default=os.path.join(ROOT, "manual.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "catalog.json"))
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    catalog = build_catalog(fetch_readme(args.readme), load_manual(args.manual))
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(catalog['entries'])} entries, {catalog['rows_without_github_link']} rows without a GitHub link, "
          f"{catalog['duplicate_rows']} duplicate rows -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
