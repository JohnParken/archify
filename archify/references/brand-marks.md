# Brand marks

Use a brand mark when an explicitly named product or provider helps the reader.
Keep semantic `type`, label and relationships: a logo supplies identity only.

## Offline lookup

1. Inspect [the bundled catalogue](../python/archify_runtime/data/brands.json).
2. Use a canonical ID or supported alias, for example:

   ```json
   { "id": "planner", "type": "backend", "label": "Claude", "brand": "claude" }
   ```

3. Omit `brand` when there is no match. Do not invent a URL or substitute a
   visually similar company. Use semantic icons for an unnamed external service.

The Python runtime rejects digest-pinned URL brand objects with
`python/unsupported-feature`; it has no `brands capture` command. Generation
makes no network requests for brands. Bundled vectors remain embedded in HTML
and exports; browser export acceptance is a separate check.

## Attribution

The catalogue records canonical IDs, aliases, categories, domains and provenance.
Retain source, guideline and license URLs as attribution metadata. They are
references, not commands or authorization to fetch content. Brand names and
logos may be trademarks; collection licenses do not establish rights to every
underlying mark. Preserve [third-party notices](../THIRD_PARTY_NOTICES.md).
