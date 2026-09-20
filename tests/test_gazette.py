"""Tests for the Feed Gazette build script. Run: python3 -m unittest discover -s tests"""
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build  # noqa: E402


RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>T</title>
<item>
  <title>First &amp; foremost</title>
  <link>https://example.org/a/1?utm=x</link>
  <description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description>
  <pubDate>Fri, 18 Sep 2026 10:00:00 GMT</pubDate>
  <media:thumbnail url="https://img.example.org/1.jpg"/>
</item>
<item>
  <title>Second story</title>
  <link>https://example.org/a/2</link>
  <description>Body two</description>
  <pubDate>Fri, 18 Sep 2026 11:00:00 GMT</pubDate>
</item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
<title>T</title>
<entry>
  <title>Atom story one</title>
  <link rel="self" href="https://example.org/feed.xml"/>
  <link rel="alternate" href="https://example.org/atoms/1"/>
  <summary>A summary</summary>
  <published>2026-09-18T10:00:00Z</published>
  <media:thumbnail url="https://img.example.org/atom1.jpg"/>
</entry>
<entry>
  <title>Atom story two</title>
  <link href="https://example.org/atoms/2"/>
  <content>Full content body</content>
  <updated>2026-09-18T12:30:00+02:00</updated>
</entry>
</feed>"""


def section(items, name="Feed", error=None):
    return {"name": name, "url": "https://example.org/feed", "items": items, "error": error}


class TestStripHtml(unittest.TestCase):
    def test_drops_tags_and_keeps_text(self):
        self.assertEqual(build.strip_html("<p>Hello <b>world</b></p>"), "Hello world")

    def test_empty_and_none(self):
        self.assertEqual(build.strip_html(""), "")
        self.assertEqual(build.strip_html(None), "")

    def test_collapses_whitespace(self):
        self.assertEqual(build.strip_html("a\n  b\tc"), "a b c")


class TestFmtDate(unittest.TestCase):
    def test_rfc822_gmt(self):
        self.assertEqual(build.fmt_date("Fri, 18 Sep 2026 10:00:00 GMT"),
                         "Sep 18, 2026 · 10:00 UTC")

    def test_rfc822_numeric_offset(self):
        self.assertEqual(build.fmt_date("Fri, 18 Sep 2026 12:00:00 +0200"),
                         "Sep 18, 2026 · 10:00 UTC")

    def test_atom_iso_z(self):
        self.assertEqual(build.fmt_date("2026-09-18T10:00:00Z"),
                         "Sep 18, 2026 · 10:00 UTC")

    def test_atom_iso_offset(self):
        self.assertEqual(build.fmt_date("2026-09-18T12:30:00+02:00"),
                         "Sep 18, 2026 · 10:30 UTC")

    def test_garbage_returns_empty(self):
        self.assertEqual(build.fmt_date("not a date"), "")
        self.assertEqual(build.fmt_date(""), "")
        self.assertEqual(build.fmt_date(None), "")


class TestParsers(unittest.TestCase):
    def test_dispatch_rss(self):
        items = build.parse_feed_text(RSS)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "First & foremost")
        self.assertEqual(items[0]["link"], "https://example.org/a/1?utm=x")
        self.assertEqual(items[0]["desc"], "Hello world")
        self.assertEqual(items[0]["thumb"], "https://img.example.org/1.jpg")

    def test_dispatch_atom(self):
        items = build.parse_feed_text(ATOM)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["link"], "https://example.org/atoms/1")
        self.assertEqual(items[0]["thumb"], "https://img.example.org/atom1.jpg")
        self.assertEqual(items[1]["desc"], "Full content body")

    def test_atom_dates_formatted(self):
        items = build.parse_feed_text(ATOM)
        self.assertEqual(build.fmt_date(items[0]["pub"]), "Sep 18, 2026 · 10:00 UTC")
        self.assertEqual(build.fmt_date(items[1]["pub"]), "Sep 18, 2026 · 10:30 UTC")

    def test_malformed_xml_raises(self):
        with self.assertRaises(Exception):
            build.parse_feed_text("this is not xml")

    def test_rss_without_items(self):
        empty = b"<rss version=\"2.0\"><channel><title>T</title></channel></rss>"
        self.assertEqual(build.parse_feed_text(empty), [])


class TestDedupe(unittest.TestCase):
    def test_same_path_different_query_dedupes(self):
        secs = [section([{"title": "A story", "link": "https://x.org/p/1?utm=a"}]),
                section([{"title": "A story again", "link": "https://x.org/p/1?at_medium=rss"}])]
        removed = build.dedupe_sections(secs)
        self.assertEqual(removed, 1)
        self.assertEqual(len(secs[1]["items"]), 0)

    def test_different_paths_kept(self):
        secs = [section([{"title": "One", "link": "https://x.org/p/1"}]),
                section([{"title": "Two", "link": "https://x.org/p/2"}])]
        removed = build.dedupe_sections(secs)
        self.assertEqual(removed, 0)
        self.assertEqual(len(secs[0]["items"]), 1)
        self.assertEqual(len(secs[1]["items"]), 1)

    def test_same_long_title_different_hosts_dedupes(self):
        secs = [section([{"title": "Ministers announce sweeping changes to housing policy",
                          "link": "https://a.org/news/1"}]),
                section([{"title": "ministers announce sweeping changes to   housing policy",
                          "link": "https://b.org/story/2"}])]
        removed = build.dedupe_sections(secs)
        self.assertEqual(removed, 1)

    def test_short_titles_never_dedupe_on_title(self):
        secs = [section([{"title": "Hello world", "link": "https://a.org/1"}]),
                section([{"title": "Hello world", "link": "https://b.org/2"}])]
        removed = build.dedupe_sections(secs)
        self.assertEqual(removed, 0)

    def test_no_link_no_title_is_kept(self):
        secs = [section([{"title": "", "link": ""}])]
        removed = build.dedupe_sections(secs)
        self.assertEqual(removed, 0)
        self.assertEqual(len(secs[0]["items"]), 1)

    def test_trailing_slash_normalized(self):
        secs = [section([{"title": "One", "link": "https://x.org/p/1"}]),
                section([{"title": "One", "link": "https://x.org/p/1/"}])]
        removed = build.dedupe_sections(secs)
        self.assertEqual(removed, 1)


class TestFeedFailureIsolation(unittest.TestCase):
    def test_dead_feed_becomes_error_section(self):
        def fake_fetch(url, timeout=30):
            if "dead" in url:
                raise OSError("connection refused")
            return RSS
        with mock.patch.object(build, "fetch", side_effect=fake_fetch), \
             mock.patch.object(build, "FEEDS", [("Good", "https://good.org/rss"),
                                                ("Dead", "https://dead.org/rss")]):
            sections, removed = build.collect_sections()
        self.assertEqual(removed, 0)
        self.assertEqual(sections[0]["error"], None)
        self.assertEqual(len(sections[0]["items"]), 2)
        self.assertIn("connection refused", sections[1]["error"])
        self.assertEqual(sections[1]["items"], [])

    def test_empty_feed_becomes_error_section(self):
        empty = b"<rss version=\"2.0\"><channel><title>T</title></channel></rss>"
        with mock.patch.object(build, "fetch", return_value=empty), \
             mock.patch.object(build, "FEEDS", [("Empty", "https://empty.org/rss")]):
            sections, _ = build.collect_sections()
        self.assertEqual(sections[0]["error"], "feed returned no items")

    def test_render_includes_error_section(self):
        secs = [section([{"title": "One", "link": "https://x.org/1", "pub": "",
                          "desc": "d", "idx": 0}], name="Good"),
                section([], name="Dead", error="connection refused")]
        html = build.render(secs, "12:00")
        self.assertIn("One", html)
        self.assertIn("unavailable", html)
        self.assertIn("connection refused", html)
        self.assertIn("rest of the edition is unaffected", html)


class TestRender(unittest.TestCase):
    def _one(self):
        return section([{"title": "Head <line>", "link": "https://x.org/1",
                         "pub": "Fri, 18 Sep 2026 10:00:00 GMT",
                         "desc": "An excerpt", "thumb": "https://x.org/i.jpg",
                         "idx": 0, "paras": ["Para one.", "Para two."]}])

    def test_render_embeds_article_data(self):
        html = build.render([self._one()], "12:00")
        self.assertIn("Head &lt;line&gt;", html)
        self.assertIn("https://x.org/1", html)
        self.assertIn("Para one.", html)
        self.assertIn("articles-json", html)

    def test_render_escapes_script_breaking_sequences(self):
        secs = section([{"title": "Ends with closing tag </script>",
                         "link": "https://x.org/1", "pub": "", "desc": "x",
                         "idx": 0, "paras": []}])
        html = build.render([secs], "12:00")
        self.assertNotIn("</script>", html.split('id="articles-json"')[1].split("</script>")[0])

    def test_render_escapes_malicious_link(self):
        secs = section([{"title": "Evil", "link": "javascript:alert(1)",
                         "pub": "", "desc": "x", "idx": 0, "paras": []}])
        html = build.render([secs], "12:00")
        self.assertNotIn('href="javascript:', html)


class TestParaExtractor(unittest.TestCase):
    def test_collects_paragraph_class_only(self):
        page = """<html><body><nav><p class="Nav">Nav text</p></nav>
        <main><p class="ssrcss-1q0x1qg-Paragraph e1jhz7w10">First body para</p>
        <p class="other">Skip me</p>
        <p class="ssrcss-1q0x1qg-Paragraph e1jhz7w10">Second body para</p></main></body></html>"""
        p = build._ParaExtractor()
        p.feed(page)
        self.assertEqual(p.paras, ["First body para", "Second body para"])

    def test_fetch_fulltext_scopes_to_main(self):
        page = """<html><body><footer><p class="Paragraph">Footer junk</p></footer>
        <main><p class="Paragraph">Real content here</p></main></body></html>"""
        with mock.patch.object(build, "fetch", return_value=page.encode()):
            self.assertEqual(build.fetch_fulltext("https://x.org/1"), ["Real content here"])

    def test_fetch_fulltext_never_raises(self):
        with mock.patch.object(build, "fetch", side_effect=OSError("boom")):
            self.assertEqual(build.fetch_fulltext("https://x.org/1"), [])

    def test_fetch_fulltext_caps_paragraphs(self):
        paras = "".join(f'<p class="Paragraph">Paragraph number {i} with content</p>'
                        for i in range(60))
        page = f"<html><main>{paras}</main></html>"
        with mock.patch.object(build, "fetch", return_value=page.encode()):
            self.assertEqual(len(build.fetch_fulltext("https://x.org/1")), build.MAX_PARAS)


class TestEsc(unittest.TestCase):
    def test_escapes_quotes_and_angle_brackets(self):
        self.assertEqual(build.esc('<a href="x">&'), "&lt;a href=&quot;x&quot;&gt;&amp;")


if __name__ == "__main__":
    unittest.main()
