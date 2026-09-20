# Feed Gazette

[![CI](https://github.com/jcrabapple/feed-gazette/actions/workflows/ci.yml/badge.svg)](https://github.com/jcrabapple/feed-gazette/actions/workflows/ci.yml)

A single-file RSS/Atom-to-newspaper generator. Point it at any feeds and it
renders a broadsheet-style static page: serif masthead, three-column layout, section
rules, and a reader-mode popup for every story.

**Live demo:** https://waxen-thistle-ckbr.here.now/

No framework, no dependencies, no build tooling. One Python script (stdlib only),
one output HTML file you can host anywhere static files are served.

## Features

- **Newspaper layout** — masthead, double rules, three text columns with column
  rules, lead-story treatment per section. Collapses to a single column on mobile.
- **Reader-mode popup** — tapping a story opens the full article text in an overlay.
  Full text is extracted from each article at build time (BBC-style pages) or
  lazily in the browser (visitor-configured feeds). The page behind the popup
  keeps its scroll position.
- **Per-visitor feed settings** — a Settings panel lets any visitor add their own
  RSS/Atom feeds (stored in their browser, not the server), turning the page into
  a personal RSS reader without touching anyone else's edition.
- **Three themes** — Paper (default), Dark, and E-ink (pure black/white, grayscale
  images, no shadows). User choice persists in localStorage and applies pre-paint.
- **Resilient builds** — a dead or malformed feed becomes a visible "unavailable"
  section in the page instead of killing the build, so a cron refresh always ships
  whatever it could fetch.
- **RSS and Atom** — both formats parse at build time and in the browser.
- **Deduplication** — the same story syndicated across overlapping feeds appears
  once (matched by normalized link, or by title when the link differs).
- **Full text at build time** — article bodies are pulled and embedded when the
  page is built, so the reader opens instantly with no network round-trip.
- **Normal link behavior** — cmd-click / ctrl-click / middle-click on a headline
  opens the source in a new tab, exactly as you'd expect; a plain tap opens the
  reader popup.
- **Instant search** — press `/` or use the search box to filter the whole edition
  across headlines, excerpts, and embedded full text.
- **Story clustering** — the same story syndicated across feeds appears once with
  an "Also in: …" badge naming the other sections covering it (in the paper and
  in the reader popup).
- **OPML import/export** — bring your subscriptions from any RSS reader in
  Settings, or export your edition as OPML. `feeds.opml` in the project directory
  works as a build-time feed source too (after `feeds.json`).
- **Shareable editions** — "Copy share link" in Settings encodes the feed list in
  the URL hash; anyone opening that link gets the same edition.
- **Self-updating on GitHub Pages** — a scheduled workflow rebuilds and deploys
  the edition every 6 hours with zero infrastructure (see below).

## Quick start

```bash
python3 build.py
# -> site/index.html — open it in a browser or upload it to any static host
```

Requires Python 3.10+. Nothing else.

## Configuring feeds

By default the page builds from two BBC News feeds. To use your own, create a
`feeds.json` next to `build.py` (see `feeds.example.json`):

```json
[
  {"name": "World", "url": "https://example.org/world.xml"},
  {"name": "Tech", "url": "https://example.org/tech.xml"}
]
```

Run `python3 build.py` again and each entry becomes a newspaper section. To change
the masthead, edit `TITLE` and `TAGLINE` at the top of `build.py`.

## How full-text extraction works

Build-time extraction targets BBC News article markup (paragraphs inside `<main>`).
For other feeds, full text depends on the source site: cleanly structured article
pages extract fine; JS-only pages fall back to the feed summary. The reader popup
labels which you're getting ("Full story" vs "Summary") and always links to the
source.

Visitor-added feeds (via the Settings panel) are fetched in the browser through a
public CORS relay ([allorigins](https://allorigins.win)) with a 15-second timeout,
since most feeds don't send CORS headers. Sites that block the relay show a visible
error in their section.

## Tests and CI

```bash
python -m unittest discover -s tests
```

30 tests cover feed parsing (RSS + Atom), date handling, full-text extraction,
deduplication, feed-failure isolation, and HTML escaping (including
`javascript:` link neutralization). CI runs the suite on Python 3.10 and 3.13 on
every push and PR.

## Refreshing on a schedule

The page is a snapshot of whenever you last ran `build.py`.

### GitHub Pages (zero infra)

Enable Pages once (Settings → Pages → Source: **GitHub Actions**). The included
`.github/workflows/pages.yml` then rebuilds the edition from your committed
`feeds.json`/`feeds.opml` (or the BBC defaults) and deploys it to
`https://<user>.github.io/<repo>/` every 6 hours, on every push to `main`, and on
demand — tests run first, so a broken build never deploys.

### Any other host

Run it on a cron and upload `site/` to your static host. For example, with
[here.now](https://here.now) (any static host with a CLI works the same way):

```bash
python3 build.py && your-publish-command site/
```

## License

[MIT](LICENSE)
