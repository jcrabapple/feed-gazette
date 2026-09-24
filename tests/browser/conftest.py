"""Playwright fixtures: render a test edition from build.render() and serve it
locally. Feeds and article pages are fixture files on the same origin, so no
test depends on the network or a public CORS relay."""
import functools
import http.server
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import build  # noqa: E402

LONG_PARA = ("The body of the fixture article is a plain paragraph that is long "
             "enough to pass the forty character extraction threshold easily.")

FIXTURE_FEED_A = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Fixture A</title>
<item><title>Fixture story alpha about mars</title>
<link>__BASE__/article-alpha.html</link>
<description>Alpha summary text from the fixture feed</description>
<pubDate>Fri, 18 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>Fixture story beta about venus</title>
<link>__BASE__/article-alpha.html</link>
<description>Beta summary text from the fixture feed</description>
<pubDate>Fri, 18 Sep 2026 11:00:00 GMT</pubDate></item>
__MORE_ITEMS__
</channel></rss>"""


def _more_feed_items(n=8):
    out = []
    for i in range(n):
        out.append(f"""<item><title>Fixture extra story {i} about regional news</title>
<link>__BASE__/article-extra-{i}.html</link>
<description>Extra summary {i} from the fixture feed</description>
<pubDate>Fri, 18 Sep 2026 1{i % 10}:00:00 GMT</pubDate></item>""")
    return "\n".join(out)

ARTICLE_ALPHA = """<html><body><main>
<p>This video can not be played due to technical problems, sorry.</p>
<p>Image caption, a fixture image of a red rocket on a pad.</p>
<p>The body of the fixture article is a plain paragraph that is long enough to
pass the forty character extraction threshold easily, numbered one.</p>
<p>A second body paragraph continues the fixture story with enough text to clear
the minimum length bar without any trouble at all, numbered two.</p>
<p>A third body paragraph rounds out the fixture article so extraction reaches
the three paragraph full-story confidence level, numbered three.</p>
</main></body></html>"""

ARTICLE_ALPHA_JINA = """Title: Fixture story alpha about mars

URL Source: /article-alpha.html

Markdown Content:

The jina-rendered body paragraph of the fixture article is plain prose long
enough to pass the forty character extraction threshold easily, jina one.

A second jina paragraph continues the fixture story with enough text to clear
the minimum length bar without any trouble at all, numbered jina two.

A third jina paragraph rounds out the fixture article so extraction reaches the
three paragraph full-story confidence level, numbered jina three.
"""


def _static_articles():
    items = []
    special = [
        ("Alpha story about mars rover", "https://example.org/a1",
         "Alpha summary", [LONG_PARA], ["Tech", "Science"]),
        ("Beta story about venus probe", "https://example.org/b1",
         "Beta summary", [LONG_PARA, LONG_PARA, LONG_PARA, LONG_PARA], []),
    ]
    for title, link, desc, paras, also in special:
        items.append({"title": title, "link": link, "desc": desc,
                      "pub": "Fri, 18 Sep 2026 10:00:00 GMT", "thumb": None,
                      "idx": len(items), "paras": paras, "also_in": also})
    # enough filler stories to make the page scrollable in tests
    for i in range(2, 10):
        items.append({"title": f"Fixture story number {i} about local affairs",
                      "link": f"https://example.org/x{i}",
                      "desc": f"Summary {i}", "pub": "Fri, 18 Sep 2026 10:00:00 GMT",
                      "thumb": None, "idx": i, "paras": [LONG_PARA] * 3,
                      "also_in": []})
    return items


@pytest.fixture(scope="session")
def base_url(tmp_path_factory):
    d = tmp_path_factory.mktemp("gazette")
    sections = [{"name": "Fixture World", "url": "https://example.org/rss",
                 "items": _static_articles(), "error": None}]
    (d / "index.html").write_text(build.render(sections, "12:00"), encoding="utf-8")
    (d / "sw.js").write_text(build.SW_JS.replace("__VERSION__", "test"), encoding="utf-8")
    build.write_pwa_assets(d)
    (d / "article-alpha.html").write_text(ARTICLE_ALPHA, encoding="utf-8")
    (d / "article-alpha.jina").write_text(ARTICLE_ALPHA_JINA, encoding="utf-8")

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(d)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    (d / "fixture-a.xml").write_text(
        FIXTURE_FEED_A.replace("__MORE_ITEMS__", _more_feed_items()).replace("__BASE__", base),
        encoding="utf-8")
    yield base
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _clean_storage(context):
    yield
    # pytest-playwright gives each test a fresh context, so localStorage and
    # the service-worker cache partition are isolated per test already.
