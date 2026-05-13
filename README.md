# notion-site-fetch

Fetch any public Notion page and print it as Markdown to stdout. No API
token. No login. No headless browser. Just plain HTTPS calls to the
endpoints Notion's own web client uses.

If the page opens for anyone in an anonymous browser tab, this tool can
read it.

## Install / run

The package is published on PyPI. The fastest way to run it is with
[`uvx`](https://docs.astral.sh/uv/guides/tools/), which fetches and
executes the tool on demand:

```sh
uvx notion-site-fetch https://example.notion.site/ > page.md
```

You can also install it permanently:

```sh
uv tool install notion-site-fetch
notion-site-fetch https://example.notion.site/ > page.md
# or with pipx:
pipx install notion-site-fetch
```

Requires Python 3.13+.

## Usage

```sh
notion-site-fetch <url>
```

Markdown is written to **stdout**. Use shell redirection to save it:

```sh
notion-site-fetch https://example.notion.site/             > page.md
notion-site-fetch https://example.notion.site/Some-Subpage >> notes.md
```

Errors go to stderr; the exit code is non-zero on failure.

### Accepted URL forms

| URL                                             | What it fetches                       |
| ----------------------------------------------- | ------------------------------------- |
| `https://<sub>.notion.site/`                    | Site's public home page               |
| `https://<sub>.notion.site/<slug-or-id>`        | A specific page on the public site    |
| `https://www.notion.so/<...>-<32-char-page-id>` | A notion.so page URL                  |

Any URL whose path ends in a 32-character Notion page id is accepted
even without a `.notion.site` host.

## Behavior

- **Sub-pages stay as links.** Only the page you ask for is fetched. To
  expand a linked sub-page, run the tool again with its URL.
- **Toggles/dropdowns are expanded inline.** Nothing is hidden behind a
  fold; nested toggles are followed recursively.
- **Private pages fail clearly.** If the page isn't publicly readable,
  the tool exits with an error rather than silently returning nothing.
- **Pagination is handled** for long pages.

### What gets rendered

Headings (H1–H4), paragraphs, bold/italic/strikethrough/code inline
formatting, links, bulleted/numbered/to-do/toggle lists with nesting,
quotes, callouts (as blockquotes), dividers, code blocks (with
language), images, bookmarks, video/file/PDF/audio embeds (as links),
and inline equations.

Things that are deliberately skipped or simplified: table-of-contents
and breadcrumb blocks (Markdown renderers generate their own),
column-layout wrappers (children are flattened into normal flow), and
collection-view databases (only the title is emitted — Notion does not
expose row-level data to anonymous viewers).

## How it works

Notion's public sites are served as an empty React shell and hydrated
by client-side calls to three internal endpoints:

1. `POST /api/v3/getPublicPageData` with `{"spaceDomain": "<sub>"}`
   resolves a `*.notion.site` subdomain to a `spaceId`.
2. `POST /api/v3/getPublicSpaceData` with that `spaceId` returns the
   `publicHomePage` block id — the root page of the site.
3. `POST /api/v3/loadCachedPageChunkV2` walks the page block by block,
   paginated with a cursor. Toggle children that the chunk loader skips
   are pulled in afterwards via `syncRecordValues`.

All endpoints are unauthenticated and return JSON. The tool issues
these requests against the site's own subdomain (`*.notion.site`) where
possible to avoid Notion's cross-cell routing errors on `www.notion.so`.

## License

MIT — see [`LICENSE`](./LICENSE).
