# Delivery contract

This contract describes the current Python CLI, including browser-free LangChain DeepAgent containers. Static validation and byte integrity are mandatory for generated HTML; browser inspection is a separate policy-controlled stage. A static success is not browser or perceptual acceptance.

## Validate and deliver

Python 3.9+ and `python/requirements.txt` are required. The default path needs no JavaScript engine, Node.js, browser binary, or display server.

```bash
python3 bin/archify.py finalize <type> <candidate.json> <output.html> --quality showcase --browser off --json
```

`finalize`, `deliver`, and `render` use the same publication path:

1. Read one candidate snapshot and validate Schema, references, layout and mode constraints.
2. Render a complete inline SVG; run the native final SVG checks, including showcase geometry and projected-text checks when the profile is showcase.
3. Build the final HTML. Strictly inspect its actual embedded SVG, ensure it matches the validated rendering, check HTML structure and remaining template slots, and reject duplicate SVG IDs or unresolved SVG references.
4. Apply the selected browser policy to an immutable snapshot of those HTML bytes.
5. Recheck candidate bytes, acquire the directory delivery lock and publish the same HTML bytes.
6. Reread candidate and published HTML to verify exact byte identity. Persist the Python delivery receipt, reread its bytes, and reverify candidate and HTML before returning.

`validate` runs candidate and final SVG validation without publishing HTML; it rejects `--browser auto|required`. It does not run browser inspection.

Exit 0 means the requested command's policy passed. Static or integrity failures exit 1; invalid arguments exit 2. Browser failures also exit 1. If static generation succeeds but `required` cannot run, or a browser finds a defect, the static artifact may still be delivered with `ok: false` and separate static/integrity status. Read the receipt instead of treating a file's existence as success.

Checks are reported by actual name and outcome. The current native static checks are not the legacy nine-check production contract; do not infer a “9/9 showcase” result from a successful command. The renderer's projected-text estimate is not measured DOM/font behavior.

## Receipts and integrity

Every HTML publication writes `<output-stem>.python-delivery.json`. Long stems receive a bounded, hashed receipt name; use the returned `deliveryReceipt` path. The contract identifier is `python-static-v1`. Receipts include:

- Candidate and output absolute paths, diagram type, command and unique receipt ID.
- `specification` and `artifact` SHA-256 hashes and byte counts.
- `staticChecks` with the embedded SVG identity and performed checks.
- `artifactIntegrity.status` from actual post-publication reads.
- `browserEvidence` with policy, status, reason, diagnostics, measurements and artifact identity.
- `visualReview: "not-requested"`; the CLI performs no perceptual review.

To verify an existing artifact without regenerating it:

```bash
python3 bin/archify.py check <type> <candidate.json> <output.html> --quality showcase --browser off --json
```

`check` requires the Python receipt, verifies its contract/type/paths and input/HTML hashes, checks the final HTML/SVG again, and rereads the files and receipt before accepting them. Missing, malformed, mismatched or unsafe receipts fail closed. A requested profile must match the SVG. `check --browser required` independently adds browser acceptance of the existing bytes; it reports that result to stdout without rewriting the delivery receipt.

These hashes establish local consistency, not signed authenticity or repository-source correctness. An actor able to rewrite both artifact and receipt can forge their consistency. The CLI rejects `meta.repository` and node `sources` and does not support `--repo-root`; retain verified source citations outside the runnable candidate.

## Publication and repeated delivery

Inputs, artifacts and receipts must be regular single-link files. Output paths must have no symlink components. All HTML publishers share `.archify-delivery-lock.json` in the output directory, so run writes to that directory serially. `check` refuses an existing lock or legacy pending journal.

An existing Python receipt can be replaced only when it identifies the same candidate path, diagram type and output path and its artifact identity matches the previous HTML. An unknown receipt, symlink, hard link, concurrent change or foreign owner is a conflict; preserve it and use another output path. Existing legacy `.delivery.json` or `.delivery-pending.json` sidecars are not migrated and block publication.

HTML and receipt are staged, flushed and individually replaced, then reread under the directory mutex. This is not the legacy production provenance journal transaction, a crash-atomic HTML/receipt pair, or a distributed lock. An interruption can leave new HTML with an old or absent receipt; `check` rejects that mismatch. A reported integrity failure may leave an artifact on disk and must not be presented as success. Use a separate output for recovery when ownership cannot be established. Do not remove unknown locks or receipt files automatically.

## Browser policy

| Policy | Missing Playwright or Chromium | Startup/runtime/measurement failure | Complete passing inspection |
| --- | --- | --- | --- |
| `off` (default) | No browser discovery; `skipped`, reason `disabled-by-policy` | Not applicable | Not claimed |
| `auto` | `skipped`; static success can exit 0 | `failed`, exit 1 | `passed`, exit 0 |
| `required` | `skipped`; command exits 1 | `failed`, exit 1 | `passed`, exit 0 |

Browser dependencies are optional and never installed automatically. To prepare a dedicated browser-capable Linux environment, install `playwright` in that interpreter and install its matching Chromium binary and OS dependencies during image construction. Default DeepAgent images need only the declared static dependencies.

## Automated browser evidence

Use `finalize --browser required` for new artifacts or `check --browser required` for existing deliveries. The optional Python Playwright helper launches headless Chromium and opens a temporary immutable copy of the exact inspected HTML. The receipt binds its hash and byte count. It waits for fonts and frames, captures uncaught page errors, and measures horizontal containment and semantic projected text at 1440×900, 1600×1000, 1920×1080 and 2048×1320 in light theme, plus dark-theme endpoint viewports. It exercises the actual theme toggle. Page-level vertical scrolling is allowed.

The helper's scope is desktop containment, semantic text, theme switching and page errors. It does not prove the legacy full Viewer gate, all interactions, export functionality, READ/Still state acceptance, or aesthetics. No screenshots are created. A missing dependency is `skipped`; an attempted browser with startup/runtime failures is `failed`, never silently downgraded to skipped.

For external browser CI, preserve the delivered HTML and receipt unchanged and run against those bytes; verify the artifact identity when binding the returned evidence. Running generation again produces a different evidence target.

## Failed finalize and candidate repair

Use `stage`, `diagnostics[].code`, subjects, evidence and supported fixes. Repair the candidate only for authoring/geometry failures. Missing dependencies, receipt conflicts and publication failures are environmental or integrity conditions, not reasons to change diagram meaning. Reuse the original folder for an owned repair; preserve unknown files and previous evidence.

After a candidate edit, rerun complete `finalize`; its new receipt supersedes evidence only for the new bytes. A non-zero exit is never a successful requested gate. After two focused authoring repairs, inspect measured geometry or the relevant contract; after one evidence-based retry, report the concrete gap.

## Output path contracts

Set `meta.output` to a portable POSIX-relative `.html` path beneath the working directory. Explicit CLI outputs may be absolute but must end in `.html` and meet the filesystem restrictions above. Neither output nor receipt may replace the candidate. Keep one output folder per request and resolve native aliases such as macOS `/tmp` to their physical path when necessary.

## Sequence width review

Preserve frozen candidate geometry. For a fresh sequence, use the mode schema and authoring defaults to choose column fitting. This runtime reports static diagnostics; it does not emit the legacy `layoutReviewRecommendation` or accept `--out-dir`.

## Optional capture evidence

The current Python CLI has no `visual-check` command. Screenshots and perceptual inspection need a separate available browser workflow, bound to the delivered HTML hash. Capture success alone is not perceptual approval.

## Optional opening

The CLI does not implement `--open` or live preview. Open the delivered HTML separately when the user requests a preview. Opening is not validation evidence.

## Handoff receipt

Report diagram type, absolute HTML and delivery-receipt paths, specification/artifact identities, actual static checks, integrity status, browser policy/status/reason and truthful separate visual-review status. Never replace a skipped browser status with a static or manual visual pass.
