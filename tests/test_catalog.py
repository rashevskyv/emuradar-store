"""catalog.py: README parsing, dedupe, slugs, manual.json merge. Offline, uses the saved README."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
import catalog  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "switchports_readme.md")


def readme_text(body):
    return "# SwitchPorts\n" + body


class RealReadme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, encoding="utf-8") as f:
            cls.cat = catalog.build_catalog(f.read())

    def test_entry_count_and_sections(self):
        entries = self.cat["entries"]
        self.assertEqual(len(entries), 115)
        self.assertEqual(self.cat["rows_without_github_link"], 1)   # Commander Keen, gbatemp only
        self.assertEqual(self.cat["duplicate_rows"], 1)             # silent-hill-decomp-nx listed twice
        sections = {e["section"] for e in entries}
        self.assertIn("Emulators", sections)
        self.assertNotIn("Nintendo Entertainment System", sections)  # inside an HTML comment

    def test_names_are_unique_slugs(self):
        names = [e["name"] for e in self.cat["entries"]]
        self.assertEqual(len(names), len(set(names)))
        for n in names:
            self.assertRegex(n, r"^[a-z0-9_-]+$")

    def test_title_from_title_column_and_derived_when_empty(self):
        by_repo = {e["repo"].lower(): e for e in self.cat["entries"]}
        self.assertEqual(by_repo["nagaa95/dolphin-nx"]["title"], "Dolphin")
        self.assertEqual(by_repo["glitcheddeveloper/ff4_nx"]["title"], "ff4 nx")  # empty title cell

    def test_headerless_table_and_tree_links(self):
        by_repo = {e["repo"].lower(): e for e in self.cat["entries"]}
        self.assertIn("souldbminerr/0ad-switch", by_repo)           # PC table has no leading pipe
        self.assertEqual(by_repo["chanseyisthebest/btd5_nx"]["title"], "Bloons TD 5 Mobile")  # /tree/main

    def test_lowercase_owner_dedupes_with_canonical_case(self):
        self.assertEqual(sum(1 for e in self.cat["entries"] if e["repo"].lower() == "chanseyisthebest/deusexgo_nx"), 1)


class Parsing(unittest.TestCase):
    def test_row_without_github_link_is_counted_not_listed(self):
        text = readme_text("### PC\n| Game | Version | Link |\n| --- | --- | --- |\n| Foo | 1 | [x](https://example.com) |\n")
        rows, no_link = catalog.parse_readme(text)
        self.assertEqual((rows, no_link), ([], 1))

    def test_url_variants(self):
        text = readme_text("### PC\n| Game | Version | Link |\n| --- | --- | --- |\n"
                           "| A | 1 | [o/a](https://github.com/o/a/) |\n"
                           "| B | 1 | [o/b](https://github.com/o/b/tree/main) |\n"
                           "| C | 1 | [o/c.git](https://github.com/o/c.git) |\n")
        repos = [r["repo"] for r in catalog.parse_readme(text)[0]]
        self.assertEqual(repos, ["o/a", "o/b", "o/c"])

    def test_section_only_from_h3_and_table_scope(self):
        text = readme_text("### Android\n| Game | Version | Link |\n| --- | --- | --- |\n| X | 2 | [o/x](https://github.com/o/x) |\n"
                           "\n## Other\n| Game | Version | Link |\n| --- | --- | --- |\n| Y | 3 | [o/y](https://github.com/o/y) |\n")
        rows = catalog.parse_readme(text)[0]
        self.assertEqual([(r["title"], r["section"], r["version_hint"]) for r in rows],
                         [("X", "Android", "2"), ("Y", "Android", "3")])


class Names(unittest.TestCase):
    def test_slug_rules(self):
        self.assertEqual(catalog.slugify("Dr. Mario_Mania"), "dr-mario_mania")
        self.assertEqual(catalog.slugify("--a..b--"), "a-b")

    def test_collision_gets_owner_prefix_on_both_sides(self):
        names = catalog.assign_names(["NaGaa95/dolphin-nx", "other/Dolphin-NX"])
        self.assertEqual(names, ["nagaa95-dolphin-nx", "other-dolphin-nx"])

    def test_no_collision_keeps_repo_slug(self):
        self.assertEqual(catalog.assign_names(["a/Foo_Bar"]), ["foo_bar"])


class Dedupe(unittest.TestCase):
    def test_first_occurrence_wins_case_insensitively(self):
        text = readme_text("### PC\n| Game | Version | Link |\n| --- | --- | --- |\n"
                           "| First | 1 | [a](https://github.com/Owner/Thing) |\n"
                           "| Second | 2 | [b](https://github.com/owner/thing) |\n")
        cat = catalog.build_catalog(text)
        self.assertEqual(len(cat["entries"]), 1)
        self.assertEqual(cat["entries"][0]["title"], "First")
        self.assertEqual(cat["duplicate_rows"], 1)


class Manual(unittest.TestCase):
    def setUp(self):
        self.text = readme_text("### PC\n| Game | Version | Link |\n| --- | --- | --- |\n"
                                "| Old | 1 | [o/thing](https://github.com/o/thing) |\n")

    def test_manual_overrides_fields_and_adds_rows(self):
        manual = [
            {"repo": "O/Thing", "title": "Thing Fixed", "description": "Hand written"},
            {"repo": "p/extra", "title": "Extra", "section": "Manual"},
        ]
        cat = catalog.build_catalog(self.text, manual)
        by_repo = {e["repo"]: e for e in cat["entries"]}
        self.assertEqual(by_repo["O/Thing"]["title"], "Thing Fixed")
        self.assertEqual(by_repo["O/Thing"]["description"], "Hand written")
        self.assertEqual(by_repo["O/Thing"]["version_hint"], "1")         # kept from README
        self.assertEqual(by_repo["p/extra"]["section"], "Manual")
        self.assertEqual(len(cat["entries"]), 2)

    def test_manual_cannot_set_name(self):
        cat = catalog.build_catalog(self.text, [{"repo": "o/thing", "name": "hacked"}])
        self.assertEqual(cat["entries"][0]["name"], "thing")

    def test_bad_manual_entry_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "manual.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([{"title": "no repo"}], f)
            with self.assertRaises(ValueError):
                catalog.load_manual(path)

    def test_output_is_sorted_and_stable(self):
        a = catalog.build_catalog(self.text, [{"repo": "z/last"}, {"repo": "a/first"}])
        b = catalog.build_catalog(self.text, [{"repo": "a/first"}, {"repo": "z/last"}])
        self.assertEqual(json.dumps(a), json.dumps(b))
        self.assertEqual([e["name"] for e in a["entries"]], sorted(e["name"] for e in a["entries"]))


if __name__ == "__main__":
    unittest.main()
