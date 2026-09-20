#!/usr/bin/env python3
"""Fetch two BBC RSS feeds and render a newspaper-format static site with reader-mode popups."""
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

FEEDS = [
    ("World & Canada", "https://bbc-feeds.danq.dev/us_and_canada-no-sports.xml"),
    ("Technology", "https://bbc-feeds.danq.dev/technology-no-sports.xml"),
]
# Optional override: a feeds.json next to this script replaces the defaults.
#   [{"name": "Section name", "url": "https://example.com/feed.xml"}, ...]
FEEDS_FILE = Path(__file__).parent / "feeds.json"
if FEEDS_FILE.exists():
    FEEDS = [(f["name"], f["url"]) for f in json.loads(FEEDS_FILE.read_text(encoding="utf-8"))]

TITLE = "The Feed Gazette"
TAGLINE = "All the news that fits, we syndicate."
OUT = Path(__file__).parent / "site"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
MAX_PARAS = 40

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def strip_html(s):
    if not s:
        return ""
    out, depth = [], 0
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())

def parse_feed(url):
    raw = fetch(url)
    root = ET.fromstring(raw)
    ns = {
        "media": "http://search.yahoo.com/mrss/",
        "dc": "http://purl.org/dc/elements/1.1/",
    }
    items = []
    for item in root.iter("item"):
        title = strip_html(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        desc = strip_html(item.findtext("description") or "")
        pub = item.findtext("pubDate") or ""
        thumb = None
        media = item.find("media:thumbnail", ns)
        if media is not None and media.get("url"):
            thumb = media.get("url")
        if not thumb:
            enc = item.find("media:content", ns)
            if enc is not None and enc.get("url"):
                thumb = enc.get("url")
        items.append({"title": title, "link": link, "desc": desc,
                      "pub": pub, "thumb": thumb})
    return items


class _ParaExtractor(HTMLParser):
    """Collect text of <p> tags whose class contains 'Paragraph'."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras = []
        self._in_p = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "p" and "Paragraph" in dict(attrs).get("class", ""):
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "p" and self._in_p:
            text = " ".join("".join(self._buf).split())
            if text:
                self.paras.append(text)
            self._in_p = False

    def handle_data(self, data):
        if self._in_p:
            self._buf.append(data)


def fetch_fulltext(link):
    """Return full article paragraphs, or [] on any failure."""
    try:
        raw = fetch(link).decode("utf-8", "replace")
    except Exception:
        return []
    # isolate <main> so nav/footer promos don't leak in
    m = re.search(r"<main\b.*?</main>", raw, re.S)
    scope = m.group(0) if m else raw
    p = _ParaExtractor()
    try:
        p.feed(scope)
    except Exception:
        return []
    return p.paras[:MAX_PARAS]

def fmt_date(pub):
    # RFC 822 like "Fri, 19 Sep 2026 14:22:00 GMT"
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(pub, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")
        except ValueError:
            continue
    return ""

def esc(s):
    return html.escape(s or "", quote=True)

def article_html(a, lead=False, with_img=True):
    d = fmt_date(a["pub"])
    img = ""
    if with_img and a.get("thumb"):
        img = (f'<figure class="art-fig"><img src="{esc(a["thumb"])}" alt="" loading="lazy" '
               f'onerror="this.parentElement.remove()">'
               f'</figure>')
    cls = "article lead" if lead else "article"
    return f'''<article class="{cls}">
{img}
<h2><a href="{esc(a["link"])}" data-idx="{a["idx"]}" class="art-link">{esc(a["title"])}</a></h2>
<p class="dateline">{esc(d)}</p>
<p class="excerpt" data-idx="{a["idx"]}">{esc(a["desc"])}</p>
</article>'''

SCRIPT_FEEDS = r'''
<script>
/* Feed Gazette client-side edition engine: per-visitor feed config in localStorage.
   Active only when custom feeds are saved; otherwise the server-built BBC page stands. */
(function () {
  var DEFAULTS = __DEFAULT_FEEDS__;
  var RELAY = 'https://api.allorigins.win/raw?url=';
  var liveData = {};
  var liveIdx = 0;
  var active = null;
  try {
    var saved = JSON.parse(localStorage.getItem('gazette-feeds') || 'null');
    if (Array.isArray(saved) && saved.length && saved.every(function (f) { return f && f.u; })) active = saved;
  } catch (e) {}

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }
  function stripHtml(s) {
    var d = document.createElement('div');
    d.innerHTML = s || '';
    return (d.textContent || '').replace(/\s+/g, ' ').trim();
  }
  function fmtDate(s) {
    if (!s) return '';
    var d = new Date(s);
    if (isNaN(d)) return '';
    return d.toLocaleString('en-US', { month: 'short', day: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) + ' UTC';
  }
  function txt(el, tag) {
    var e = el.getElementsByTagName(tag)[0];
    return e ? e.textContent.trim() : '';
  }

  function tryFetchText(url) {
    return fetch(url, { redirect: 'follow' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    });
  }
  function fetchFeedText(url) {
    return tryFetchText(url).catch(function () {
      return tryFetchText(RELAY + encodeURIComponent(url));
    });
  }

  function parseFeedXml(text) {
    var doc = new DOMParser().parseFromString(text, 'text/xml');
    if (doc.querySelector('parsererror')) throw new Error('not a valid feed');
    var nodes = doc.querySelectorAll('item, entry');
    var out = [];
    for (var i = 0; i < nodes.length && i < 25; i++) {
      var it = nodes[i];
      var link = '';
      var links = it.getElementsByTagName('link');
      for (var j = 0; j < links.length; j++) {
        var rel = links[j].getAttribute('rel');
        var href = (links[j].getAttribute('href') || links[j].textContent || '').trim();
        if (href && (!rel || rel === 'alternate')) { link = href; break; }
      }
      var thumb = '';
      var m = it.getElementsByTagName('media:thumbnail')[0] || it.getElementsByTagName('media:content')[0];
      if (m && m.getAttribute('url')) thumb = m.getAttribute('url');
      if (!thumb) {
        var en = it.querySelector('enclosure[type^="image"]');
        if (en && en.getAttribute('url')) thumb = en.getAttribute('url');
      }
      var title = txt(it, 'title');
      if (!title) continue;
      out.push({
        t: title,
        u: link,
        d: fmtDate(txt(it, 'pubDate') || txt(it, 'published') || txt(it, 'updated')),
        s: stripHtml(txt(it, 'description') || txt(it, 'summary') || txt(it, 'content')).slice(0, 600),
        thumb: thumb,
        p: [],
        live: true
      });
    }
    return out;
  }

  function extractParas(html) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var root = doc.querySelector('main') || doc.querySelector('article') || doc.body;
    if (!root) return [];
    var out = [];
    var ps = root.querySelectorAll('p');
    for (var i = 0; i < ps.length && out.length < 40; i++) {
      var t = (ps[i].textContent || '').replace(/\s+/g, ' ').trim();
      if (t.length > 40) out.push(t);
    }
    return out;
  }

  function articleCard(a, idx, lead, withImg) {
    var img = '';
    if (withImg && a.thumb) {
      img = '<figure class="art-fig"><img src="' + esc(a.thumb) + '" alt="" loading="lazy" onerror="this.parentElement.remove()"></figure>';
    }
    var cls = lead ? 'article lead' : 'article';
    return '<article class="' + cls + '">' + img +
      '<h2><a href="' + esc(a.u) + '" data-idx="' + esc(idx) + '" class="art-link">' + esc(a.t) + '</a></h2>' +
      '<p class="dateline">' + esc(a.d) + '</p>' +
      '<p class="excerpt" data-idx="' + esc(idx) + '">' + esc(a.s) + '</p></article>';
  }

  function loadEdition() {
    var container = document.getElementById('sections');
    var label = document.getElementById('edition-label');
    var upd = document.getElementById('updated-label');
    if (label) label.textContent = 'Custom edition · ' + active.length + ' feed' + (active.length === 1 ? '' : 's');
    if (upd) upd.textContent = 'Updated just now';
    container.innerHTML = '<p class="loading-note">Setting the type… loading your feeds.</p>';
    Promise.all(active.map(function (f) {
      return fetchFeedText(f.u).then(function (t) { return { f: f, items: parseFeedXml(t) }; })
        .catch(function (err) { return { f: f, error: (err && err.message) || 'failed to load' }; });
    })).then(function (results) {
      var html = '';
      liveData = {}; liveIdx = 0;
      results.forEach(function (r) {
        var head = '<div class="section-head"><span class="section-title">' + esc(r.f.n || 'Feed') + '</span>';
        if (r.error) {
          html += '<section class="section">' + head + '<span class="section-count">error</span></div>' +
            '<p class="loading-note">Could not load this feed (' + esc(r.error) + '). It may block the relay — check the URL or try another source.</p></section>';
          return;
        }
        if (!r.items.length) {
          html += '<section class="section">' + head + '<span class="section-count">0 stories</span></div>' +
            '<p class="loading-note">Feed loaded but returned no items.</p></section>';
          return;
        }
        var cards = r.items.map(function (a, i) {
          var idx = String(liveIdx++);
          liveData[idx] = a;
          return articleCard(a, idx, i === 0, i < 3);
        }).join('');
        html += '<section class="section">' + head + '<span class="section-count">' + r.items.length + ' stories</span></div>' +
          '<div class="columns">' + cards + '</div></section>';
      });
      container.innerHTML = html;
    });
  }

  window.__gazetteResolve = function (idx) { return liveData[idx]; };

  window.__gazetteOpen = function (idx) {
    var a = liveData[idx];
    if (!a) return;
    var savedY = window.scrollY;
    var dlg = document.getElementById('reader');
    var body = document.getElementById('r-body');
    var kicker = document.getElementById('r-kicker');
    var titleEl = document.getElementById('r-title');
    document.getElementById('r-date').textContent = a.d;
    document.getElementById('r-src').href = a.u;
    titleEl.textContent = a.t;
    function draw(paras, note) {
      body.innerHTML = '';
      var ps = paras && paras.length ? paras : [a.s || '(no summary available)'];
      ps.forEach(function (p) {
        var el = document.createElement('p');
        el.textContent = p;
        body.appendChild(el);
      });
      if (note) {
        var n = document.createElement('p');
        n.className = 'loading-note';
        n.textContent = note;
        body.appendChild(n);
      }
      body.scrollTop = 0;
    }
    draw(a.p, a.p.length ? '' : 'Loading full story…');
    if (typeof dlg.showModal === 'function') { dlg.showModal(); window.scrollTo(0, savedY); }
    else dlg.setAttribute('open', '');
    if (!a.p.length && !a.tried) {
      a.tried = true;
      if (!a.u) { draw(null, 'No article link in this feed item — summary only.'); return; }
      fetchFeedText(a.u).then(function (html) {
        var paras = extractParas(html);
        if (paras.length) a.p = paras;
        if (dlg.open && titleEl.textContent === a.t) {
          kicker.textContent = a.p.length ? 'Full story' : 'Summary';
          draw(a.p, a.p.length ? '' : 'Full text unavailable for this site — summary shown. Use the link below for the source.');
        }
      }).catch(function () {
        if (dlg.open && titleEl.textContent === a.t) draw(null, 'Could not load full text — summary shown.');
      });
    }
  };

  /* settings dialog */
  var sdlg = document.getElementById('settings');
  var listEl = document.getElementById('feed-list');
  function renderList() {
    var feeds = active || DEFAULTS;
    listEl.innerHTML = feeds.map(function (f, i) {
      var right = active
        ? '<button class="feed-del" type="button" data-i="' + i + '">Remove</button>'
        : '<span class="feed-tag">default</span>';
      return '<div class="feed-row"><span class="feed-name">' + esc(f.n || 'Feed') + '</span>' +
        '<span class="feed-url">' + esc(f.u) + '</span>' + right + '</div>';
    }).join('');
  }
  renderList();
  document.getElementById('feed-add').addEventListener('click', function () {
    var n = document.getElementById('feed-name').value.trim() || 'Feed';
    var u = document.getElementById('feed-url').value.trim();
    if (!/^https?:\/\//i.test(u)) { alert('Feed URL must start with http:// or https://'); return; }
    var feeds = (active || []).slice();
    feeds.push({ n: n, u: u });
    active = feeds;
    renderList();
    document.getElementById('feed-name').value = '';
    document.getElementById('feed-url').value = '';
  });
  listEl.addEventListener('click', function (e) {
    var b = e.target.closest('.feed-del');
    if (!b) return;
    active.splice(parseInt(b.getAttribute('data-i'), 10), 1);
    renderList();
  });
  document.getElementById('feed-save').addEventListener('click', function () {
    if (active && active.length) localStorage.setItem('gazette-feeds', JSON.stringify(active));
    else localStorage.removeItem('gazette-feeds');
    location.reload();
  });
  document.getElementById('feed-reset').addEventListener('click', function () {
    localStorage.removeItem('gazette-feeds');
    location.reload();
  });
  document.getElementById('settings-btn').addEventListener('click', function () {
    if (typeof sdlg.showModal === 'function') sdlg.showModal();
    else sdlg.setAttribute('open', '');
  });
  document.getElementById('settings-close').addEventListener('click', function () {
    if (sdlg.open && typeof sdlg.close === 'function') sdlg.close();
    else sdlg.removeAttribute('open');
  });
  sdlg.addEventListener('click', function (e) {
    if (e.target === sdlg) { if (typeof sdlg.close === 'function') sdlg.close(); else sdlg.removeAttribute('open'); }
  });

  if (active) loadEdition();
})();
</script>
'''

def render(sections, generated):
    defaults_js = json.dumps([{"n": n, "u": u} for n, u in FEEDS], ensure_ascii=False)
    feeds_script = SCRIPT_FEEDS.replace("__DEFAULT_FEEDS__", defaults_js)
    now = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    body = []
    for name, items in sections:
        cards = []
        for i, a in enumerate(items):
            cards.append(article_html(a, lead=(i == 0), with_img=(i < 3)))
        body.append(f'''<section class="section">
<div class="section-head"><span class="section-title">{esc(name)}</span>
<span class="section-count">{len(items)} stories</span></div>
<div class="columns">{"".join(cards)}</div>
</section>''')

    # article data for the reader popup, keyed by idx
    data = {str(a["idx"]): {"t": a["title"], "d": fmt_date(a["pub"]),
                            "u": a["link"], "p": a.get("paras") or [],
                            "s": a["desc"]}
            for _, items in sections for a in items}
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Feed Gazette</title>
<script>
try {{
  var t = localStorage.getItem('gazette-theme');
  if (t === 'dark' || t === 'eink') document.documentElement.setAttribute('data-theme', t);
  var f = localStorage.getItem('gazette-font');
  if (f === 'serif' || f === 'sans' || f === 'plex') document.documentElement.setAttribute('data-font', f);
}} catch (e) {{}}
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,700;0,900;1,500&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&family=Inter:wght@400;600;700&family=IBM+Plex+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --ink: #1a1714;
  --paper: #f6f1e5;
  --rule: #1a1714;
  --faint: #6b6257;
  --accent: #7a1f1f;
  --colrule: #cfc4ae;
  --imgborder: #cfc4ae;
  --img-filter: sepia(0.15) contrast(0.95);
  --backdrop: rgba(20,16,10,0.6);
  --shadow: 0 12px 60px rgba(0,0,0,0.45);
}}
html[data-theme="dark"] {{
  --ink: #e8e0d0;
  --paper: #191612;
  --rule: #b8ad99;
  --faint: #9a9084;
  --accent: #d98b6a;
  --colrule: #3a342c;
  --imgborder: #3a342c;
  --img-filter: contrast(0.92) brightness(0.92);
  --backdrop: rgba(0,0,0,0.7);
  --shadow: 0 12px 60px rgba(0,0,0,0.8);
}}
html[data-theme="eink"] {{
  --ink: #000000;
  --paper: #ffffff;
  --rule: #000000;
  --faint: #444444;
  --accent: #000000;
  --colrule: #999999;
  --imgborder: #000000;
  --img-filter: grayscale(1) contrast(1.05);
  --backdrop: rgba(255,255,255,0.75);
  --shadow: none;
}}
:root {{
  --font-head: "Playfair Display", Georgia, serif;
  --font-body: "Source Serif 4", Georgia, serif;
}}
html[data-font="serif"] {{
  --font-head: "Source Serif 4", Georgia, serif;
  --font-body: "Source Serif 4", Georgia, serif;
}}
html[data-font="sans"] {{
  --font-head: "Inter", system-ui, sans-serif;
  --font-body: "Inter", system-ui, sans-serif;
}}
html[data-font="plex"] {{
  --font-head: "IBM Plex Sans", system-ui, sans-serif;
  --font-body: "IBM Plex Sans", system-ui, sans-serif;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  background: var(--paper);
  color: var(--ink);
  font-family: var(--font-body);
  line-height: 1.45;
  padding: 2rem 1.5rem 4rem;
}}
.sheet {{ max-width: 1200px; margin: 0 auto; }}
.masthead {{ text-align: center; border-bottom: 4px double var(--rule); padding-bottom: 1rem; }}
.masthead h1 {{
  font-family: var(--font-head);
  font-weight: 900;
  font-size: clamp(2.5rem, 8vw, 5rem);
  letter-spacing: 0.02em;
  line-height: 1.05;
}}
.masthead .tagline {{ font-style: italic; color: var(--faint); margin-top: 0.35rem; }}
.dateline-bar {{
  display: flex; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;
  border-bottom: 1px solid var(--rule);
  padding: 0.4rem 0;
  font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.12em;
  color: var(--faint);
}}
.theme-toggle {{
  background: none; border: 1px solid var(--rule); color: var(--faint);
  font: inherit; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.12em;
  padding: 0.2rem 0.7rem; cursor: pointer;
}}
.theme-toggle:hover {{ color: var(--accent); border-color: var(--accent); }}
/* settings panel */
.feed-row {{ display: flex; align-items: center; gap: 0.6rem; padding: 0.5rem 0; border-bottom: 1px solid var(--colrule); font-size: 0.9rem; }}
.feed-name {{ font-weight: 600; min-width: 8rem; }}
.feed-url {{ color: var(--faint); font-size: 0.78rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }}
.feed-del, .feed-add button, .reader-foot button {{
  background: none; border: 1px solid var(--rule); color: var(--ink);
  font: inherit; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em;
  padding: 0.3rem 0.7rem; cursor: pointer;
}}
.feed-del:hover, .feed-add button:hover, .reader-foot button:hover {{ color: var(--accent); border-color: var(--accent); }}
.feed-tag {{ font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--faint); }}
.feed-add {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 1rem; }}
.feed-add input {{ flex: 1 1 10rem; background: var(--paper); border: 1px solid var(--rule); color: var(--ink); font: inherit; font-size: 0.85rem; padding: 0.4rem 0.6rem; }}
.settings-note {{ margin-top: 1rem; font-size: 0.78rem; color: var(--faint); font-style: italic; }}
.loading-note {{ color: var(--faint); font-style: italic; }}
@media (max-width: 640px) {{
  .feed-name {{ min-width: 0; }}
}}
.section {{ margin-top: 2.5rem; }}
.section-head {{
  display: flex; align-items: baseline; justify-content: space-between;
  border-top: 3px solid var(--rule); border-bottom: 1px solid var(--rule);
  padding: 0.45rem 0; margin-bottom: 1.25rem;
}}
.section-title {{
  font-family: var(--font-head);
  font-weight: 700; font-size: 1.4rem; text-transform: uppercase; letter-spacing: 0.08em;
}}
.section-count {{ font-size: 0.75rem; color: var(--faint); text-transform: uppercase; letter-spacing: 0.12em; }}
.columns {{ columns: 3 280px; column-gap: 2rem; column-rule: 1px solid var(--colrule); }}
.article {{ break-inside: avoid; margin-bottom: 1.75rem; }}
.article h2 {{
  font-family: var(--font-head);
  font-weight: 700; font-size: 1.25rem; line-height: 1.2; margin-bottom: 0.3rem;
}}
.article.lead h2 {{ font-size: 1.7rem; }}
.article.lead .excerpt {{ font-size: 1.02rem; }}
.article h2 a {{ color: inherit; text-decoration: none; cursor: pointer; }}
.article h2 a:hover {{ color: var(--accent); }}
.dateline {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--faint); margin-bottom: 0.4rem; }}
.excerpt {{ font-size: 0.92rem; cursor: pointer; }}
/* reader dialog */
dialog.reader {{
  border: none; padding: 0; max-width: 720px; width: calc(100% - 2rem);
  max-height: min(85vh, 900px); background: var(--paper); color: var(--ink);
  box-shadow: var(--shadow);
  /* explicit modal centering: the global * reset kills the UA's margin:auto,
     and anything but fixed makes the dialog sit in document flow, scrolling the page */
  position: fixed; inset: 0; margin: auto;
}}
html[data-theme="eink"] dialog.reader {{ border: 2px solid var(--ink); }}
dialog.reader::backdrop {{ background: var(--backdrop); }}
.art-fig {{ margin: 0 0 0.5rem; }}
.art-fig img {{ width: 100%; height: auto; display: block; border: 1px solid var(--imgborder); filter: var(--img-filter); }}
.reader-inner {{ display: flex; flex-direction: column; max-height: inherit; }}
.reader-head {{
  border-bottom: 4px double var(--rule); padding: 1.1rem 1.4rem 0.8rem;
  flex: 0 0 auto;
}}
.reader-head .kicker {{
  font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.14em;
  color: var(--accent); margin-bottom: 0.3rem;
}}
.reader-head h2 {{
  font-family: var(--font-head);
  font-weight: 700; font-size: 1.5rem; line-height: 1.2;
}}
.reader-head .dateline {{ margin: 0.4rem 0 0; }}
.reader-close {{
  position: absolute; top: 0.6rem; right: 0.8rem;
  background: none; border: none; font-size: 1.6rem; line-height: 1;
  color: var(--faint); cursor: pointer; font-family: var(--font-head);
}}
.reader-close:hover {{ color: var(--accent); }}
.reader-body {{ overflow-y: auto; padding: 1.2rem 1.4rem 1.4rem; }}
.reader-body p {{ margin-bottom: 0.9rem; font-size: 1.02rem; }}
.reader-body p:first-child {{ font-size: 1.1rem; }}
.reader-foot {{
  flex: 0 0 auto; border-top: 1px solid var(--rule);
  padding: 0.6rem 1.4rem; display: flex; justify-content: space-between; align-items: center;
  font-size: 0.78rem; color: var(--faint); gap: 1rem;
}}
.reader-foot a {{ color: var(--accent); text-decoration: none; text-transform: uppercase; letter-spacing: 0.08em; white-space: nowrap; }}
.reader-foot a:hover {{ text-decoration: underline; }}
footer {{
  margin-top: 3rem; border-top: 4px double var(--rule); padding-top: 0.75rem;
  text-align: center; font-size: 0.75rem; color: var(--faint); font-style: italic;
}}
@media (max-width: 640px) {{
  body {{ padding: 1.25rem 1rem 3rem; }}
  .columns {{ columns: 1; }}
  dialog.reader {{ width: 100%; max-height: none; height: 100dvh; max-width: none; }}
}}
</style>
</head>
<body>
<div class="sheet">
<header class="masthead">
  <h1>{esc(TITLE)}</h1>
  <p class="tagline">{esc(TAGLINE)}</p>
</header>
<div class="dateline-bar">
  <span>{esc(now)}</span>
  <span id="edition-label">Compiled from RSS feeds</span>
  <span id="updated-label">Updated {esc(generated)} UTC</span>
  <button class="theme-toggle" id="theme-toggle" type="button" aria-label="Switch theme">Theme: Paper</button>
  <button class="theme-toggle" id="font-toggle" type="button" aria-label="Switch font">Font: Classic</button>
  <button class="theme-toggle" id="settings-btn" type="button">Settings</button>
</div>
<div id="sections">
{"".join(body)}
</div>
<footer>Sources: linked feeds &middot; Tap a story to read &middot; Rendered {esc(generated)} UTC</footer>
</div>

<dialog class="reader" id="reader" aria-modal="true">
  <div class="reader-inner">
    <div class="reader-head">
      <button class="reader-close" id="reader-close" aria-label="Close">&times;</button>
      <p class="kicker" id="r-kicker"></p>
      <h2 id="r-title"></h2>
      <p class="dateline" id="r-date"></p>
    </div>
    <div class="reader-body" id="r-body"></div>
    <div class="reader-foot">
      <span>Archived snapshot · {esc(generated)} UTC</span>
      <a id="r-src" href="#" target="_blank" rel="noopener">Read at source &rarr;</a>
    </div>
  </div>
</dialog>

<dialog class="reader" id="settings" aria-modal="true">
  <div class="reader-inner">
    <div class="reader-head">
      <button class="reader-close" id="settings-close" aria-label="Close">&times;</button>
      <p class="kicker">Settings</p>
      <h2>Your edition</h2>
      <p class="dateline">Stored in this browser &middot; each visitor gets their own edition</p>
    </div>
    <div class="reader-body">
      <div id="feed-list"></div>
      <div class="feed-add">
        <input id="feed-name" type="text" placeholder="Section name (e.g. Science)" maxlength="60">
        <input id="feed-url" type="url" placeholder="https://example.com/feed.xml">
        <button id="feed-add" type="button">Add feed</button>
      </div>
      <p class="settings-note">Feeds and full articles are fetched in your browser through a public CORS relay, so a few sites may refuse. RSS and Atom both work. Removing every feed restores the default edition.</p>
    </div>
    <div class="reader-foot">
      <button id="feed-reset" type="button">Reset to defaults</button>
      <button id="feed-save" type="button">Save &amp; rebuild edition</button>
    </div>
  </div>
</dialog>

<script id="articles-json" type="application/json">{data_json}</script>
<script>
(function () {{
  var DATA = JSON.parse(document.getElementById('articles-json').textContent);
  var dlg = document.getElementById('reader');

  /* theme cycling: paper -> dark -> eink */
  var THEMES = ['paper', 'dark', 'eink'];
  var LABELS = {{ paper: 'Paper', dark: 'Dark', eink: 'E-ink' }};
  var toggleBtn = document.getElementById('theme-toggle');
  function currentTheme() {{
    var t = document.documentElement.getAttribute('data-theme');
    return THEMES.indexOf(t) >= 0 ? t : 'paper';
  }}
  function updateToggleLabel() {{
    toggleBtn.textContent = 'Theme: ' + LABELS[currentTheme()];
  }}
  toggleBtn.addEventListener('click', function () {{
    var next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
    if (next === 'paper') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', next);
    try {{ localStorage.setItem('gazette-theme', next); }} catch (e) {{}}
    updateToggleLabel();
  }});
  updateToggleLabel();

  /* font cycling: classic -> serif -> sans -> plex */
  var FONTS = ['classic', 'serif', 'sans', 'plex'];
  var FLABELS = {{ classic: 'Classic', serif: 'Serif', sans: 'Sans', plex: 'Plex' }};
  var fontBtn = document.getElementById('font-toggle');
  function currentFont() {{
    var f = document.documentElement.getAttribute('data-font');
    return FONTS.indexOf(f) >= 0 ? f : 'classic';
  }}
  function updateFontLabel() {{
    fontBtn.textContent = 'Font: ' + FLABELS[currentFont()];
  }}
  fontBtn.addEventListener('click', function () {{
    var next = FONTS[(FONTS.indexOf(currentFont()) + 1) % FONTS.length];
    if (next === 'classic') document.documentElement.removeAttribute('data-font');
    else document.documentElement.setAttribute('data-font', next);
    try {{ localStorage.setItem('gazette-font', next); }} catch (e) {{}}
    updateFontLabel();
  }});
  updateFontLabel();

  var els = {{
    kicker: document.getElementById('r-kicker'),
    title: document.getElementById('r-title'),
    date: document.getElementById('r-date'),
    body: document.getElementById('r-body'),
    src: document.getElementById('r-src')
  }};

  var lastScrollY = 0;

  function resolveArticle(idx) {{
    if (typeof window.__gazetteResolve === 'function') {{
      var live = window.__gazetteResolve(idx);
      if (live) return live;
    }}
    return DATA[idx];
  }}

  function open(idx) {{
    var a = resolveArticle(idx);
    if (!a) return;
    if (a.live && typeof window.__gazetteOpen === 'function') {{ window.__gazetteOpen(idx); return; }}
    lastScrollY = window.scrollY;
    els.kicker.textContent = a.p.length ? 'Full story' : 'Summary';
    els.title.textContent = a.t;
    els.date.textContent = a.d;
    els.src.href = a.u;
    els.body.innerHTML = '';
    var paras = a.p.length ? a.p : [a.s];
    paras.forEach(function (p) {{
      var el = document.createElement('p');
      el.textContent = p;
      els.body.appendChild(el);
    }});
    if (typeof dlg.showModal === 'function') {{
      dlg.showModal();
      // some mobile browsers scroll the page when the modal grabs focus
      window.scrollTo(0, lastScrollY);
    }} else {{
      dlg.setAttribute('open', '');
    }}
    els.body.scrollTop = 0;
  }}

  function close() {{
    if (typeof dlg.close === 'function' && dlg.open) dlg.close();
    else dlg.removeAttribute('open');
    window.scrollTo(0, lastScrollY);
  }}

  document.addEventListener('click', function (e) {{
    var link = e.target.closest('.art-link');
    if (link) {{
      e.preventDefault();
      open(link.getAttribute('data-idx'));
      return;
    }}
    var exc = e.target.closest('.excerpt');
    if (exc && !e.target.closest('a')) {{
      e.preventDefault();
      open(exc.getAttribute('data-idx'));
    }}
  }});

  document.getElementById('reader-close').addEventListener('click', close);
  dlg.addEventListener('click', function (e) {{
    if (e.target === dlg) close();  // backdrop tap
  }});
  // Esc closes natively via dialog; nothing extra needed
}})();
</script>
{feeds_script}
</body>
</html>'''

def main():
    sections = []
    idx = 0
    jobs = []
    for name, url in FEEDS:
        items = parse_feed(url)
        print(f"{name}: {len(items)} items")
        for a in items:
            a["idx"] = idx
            idx += 1
            jobs.append(a)
        sections.append((name, items))

    with ThreadPoolExecutor(max_workers=8) as ex:
        paras_list = list(ex.map(lambda a: fetch_fulltext(a["link"]), jobs))
    got = 0
    for a, paras in zip(jobs, paras_list):
        a["paras"] = paras
        if paras:
            got += 1
    print(f"full text: {got}/{len(jobs)} articles")

    generated = datetime.now(timezone.utc).strftime("%H:%M")
    (OUT).mkdir(parents=True, exist_ok=True)
    (OUT / "index.html").write_text(render(sections, generated), encoding="utf-8")
    print(f"wrote {OUT / 'index.html'}")

if __name__ == "__main__":
    main()
