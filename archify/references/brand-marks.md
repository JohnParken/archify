# Brand marks

Use a brand mark only when a real product, provider, model family, channel, or
service identity helps the reader. Semantic `type` still explains what the node
does; `brand` explains whose product it is.

## Agent decision path

1. Look up the built-in catalogue in `archify_runtime/data/brands.json` when the request names a recognizable brand:
   Use the canonical ID (e.g. `"claude"`, `"github"`, `"python"`, `"aws"`, `"docker"`):

   ```json
   {
     "id": "planner",
     "type": "backend",
     "label": "Claude",
     "brand": "claude"
   }
   ```

2. Note: Remote URL capture is explicitly unsupported in the offline pure Python runtime (`python/unsupported-feature`). Use built-in brand IDs or standard semantic icons.

   ```json
   {
     "id": "partner",
     "type": "external",
     "label": "Partner portal",
     "brand": {
       "url": "https://partner.example.com",
       "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
     }
   }
   ```

4. If there is no match and no user-provided URL, omit `brand`. Do not invent a
   URL or silently assign a visually similar company.

Known-brand URLs resolve to the bundled vector instead of using the network.
For discovered icon `href` attributes, capture decodes the basic named references
`amp`, `quot`, `apos`, `lt`, `gt` (and their defined uppercase aliases), plus
decimal and hexadecimal numeric references, once before URL resolution. Thus
`/icon.png?v=1&amp;size=32` requests `/icon.png?v=1&size=32`. URL percent escapes
remain intact; nested escapes are not decoded recursively. This bounded decoder
does not add a general HTML parser or support every named HTML entity.
HTML reads stop at an explicit head ending outside comments, raw-text elements
and quoted attributes, including when those tokens span network chunks. The
256 KiB head limit and capture deadline still apply; a larger body after the
head is not read for icon discovery.
Unknown URL capture accepts only bounded raster image formats, blocks
credentials, nonstandard public ports, and private or link-local destinations,
uses bounded concurrency and one total deadline, and returns the captured
content digest. Later render and validate operations require that exact digest;
blocked, unavailable, changed, oversized, or unsafe content fails closed instead
of silently changing the artifact.

Page, icon and redirect requests send `Accept-Encoding: identity`. Capture does
not decompress response bodies: a successful response declaring another content
coding is closed and rejected explicitly. This keeps the existing byte limits
and pinned digest tied to the unencoded representation. A later usable icon may
still succeed; otherwise an encoding error is retained instead of being hidden
by an unrelated favicon 404.

The final artifact never fetches a brand asset when opened. Preset vectors and
digest-verified captured site icons remain embedded in SVG, PNG, WebP, JPEG,
Share Card, and WebM exports.

Inspect `archify/python/archify_runtime/data/brands.json` to view all canonical IDs, aliases,
categories, domains, and provenance. Current categories cover AI, cloud,
engineering, data, collaboration, business systems, channels, languages, and
frameworks.
