---
name: archify
description: "Create polished, validated architecture, workflow, sequence, data-flow, and lifecycle/state diagrams as explorable standalone HTML with inline SVG, dark/light themes, optional trace motion, and PNG/JPEG/WebP/SVG/WebM export. Accept plain-language requirements or pasted Mermaid flowchart, sequenceDiagram, and stateDiagram input; inspect repository evidence when the diagram must reflect real code. Use when the user asks to visualize system architecture, infrastructure, cloud/security/network topology, technical workflows, API call sequences, request lifecycles, data pipelines, ETL/ELT, data lineage, state machines, or to convert/beautify Mermaid. Also use for everyday subjects with steps, parts, relationships, or states: a leave or travel plan, an application or approval process, a back-and-forth such as renting, where money or documents go, or where an application or order stands. Not for numeric charts or dashboards."
license: MIT
metadata:
  version: "3.0"
  author: tt-a1i
  based_on: Cocoon-AI/architecture-diagram-generator (MIT, v1.0)
---

# Archify

Create a standalone interactive HTML diagram from typed JSON. Diagram layout and SVG generation run in Python; the Viewer runs in the recipient's browser. Static output is the default; enable motion only when requested.

## Runtime and output

Use Python 3.9+ with `python/requirements.txt` installed in the selected interpreter. The default generation path needs no Node.js or browser, including in LangChain DeepAgent containers. An import failure is an environment failure, not a diagram defect; install declared dependencies into the intended environment before retrying.

Run commands from your working directory and replace `bin/archify.py` below with the package's absolute path. Give each new diagram its own `.archify/<type>-<slug>-<YYYYMMDD-HHMMSS>/` folder (local time, chosen once); keep `candidate.json`, `<slug>.html`, and its `.python-delivery.json` receipt together. Set `meta.output` to the relative HTML path and reuse the folder for repairs. Output paths must have no symbolic-link components. Inputs, artifacts, and receipts must be regular single-link files.

## Generation

1. Choose a diagram type using the router below. For a frozen candidate, go directly to `finalize` without editing it.
2. For a fresh candidate, read [Authoring defaults](references/authoring-defaults.md) and the mode's example in a bounded batch; recover truncated sections before writing. Read the mode and common schemas before using constrained fields. For a real codebase, inspect the relevant source using [Repository authoring](references/repository-authoring.md). The current Python CLI rejects `meta.repository` and node `sources` and does not support `--repo-root`; retain inspected repository evidence separately from the runnable candidate and disclose this limitation.
3. Write the complete candidate. Examples teach field shape, not facts. Preserve requested topology, responsibilities, labels, direction, conditions, and source evidence; use fresh wording and IDs. For Architecture, choose connected placement using Authoring defaults before assigning coordinates. Start with automatic routes and use explicit routes only for necessary branches, returns, supplied geometry, or measured repair. Use `meta.quality_profile: "showcase"` unless the user requests dense `standard`.
4. Run the complete generation command directly:

   ```bash
   python3 bin/archify.py finalize <type> <candidate.json> <output.html> --quality showcase --browser off --json
   ```

   `off` is also the CLI default. This validates the candidate, renders SVG, strictly checks the final HTML and embedded SVG, publishes HTML plus a Python delivery receipt, and rereads the input, artifact, and receipt to verify integrity. A passing result proves only the reported static checks and byte integrity. It never implies browser or visual acceptance.
5. On non-zero exit, use `stage` and `diagnostics[]` to distinguish authoring, integrity, publication, and browser failures. Repair the named connected neighborhood while preserving meaning and rerun `finalize`. After two focused repairs fail, inspect measured geometry or the relevant contract; after one evidence-based retry, report the remaining gap. Never delete unknown receipts or locks to make a retry pass.

`render` and `deliver` use the same static publication and integrity path. `validate` checks the candidate and rendered SVG without writing an artifact. To verify an existing delivery without regenerating it:

```bash
python3 bin/archify.py check <type> <candidate.json> <output.html> --quality showcase --browser off --json
```

`check` requires the matching `.python-delivery.json` receipt. Read [Delivery contract](references/delivery-contract.md) for receipt ownership, repeated delivery, failure recovery, and optional browser checks.

## Browser policy

- `--browser off`: skip browser work explicitly; use for ordinary generation in browser-free containers.
- `--browser auto`: inspect when optional Playwright and its Chromium binary are available. Missing dependencies yield `skipped`; actual startup, runtime, or measurement failures yield `failed` and a non-zero exit.
- `--browser required`: browser inspection must pass. Missing dependencies also make the command exit non-zero, while retaining an otherwise valid generated static artifact and its receipt.

Use `required` only for an explicitly requested browser acceptance task or a browser-capable CI gate. Do not install browser dependencies during ordinary generation. Browser results bind to the exact HTML bytes inspected, and report their measured scope. They do not prove export behavior or every Viewer interaction. Keep static and browser status separate when browser acceptance fails.

## Type router

| Type | Use for | Schema | Example |
|---|---|---|---|
| `architecture` | Components, services, cloud/security boundaries, infrastructure; what something everyday is made of | `schemas/architecture.schema.json` | System descriptions, services, libraries, and CLI repos: `examples/web-app.architecture.json`; deployment repos: `examples/production-deployment.architecture.json` |
| `workflow` | Processes, approval gates, tool calls, runbooks, CI/CD; plans and step-by-step life processes | `schemas/workflow.schema.json` | `examples/agent-tool-call.workflow.json` |
| `sequence` | API call chains, request lifecycles, async traces, returns; back-and-forth between people | `schemas/sequence.schema.json` | `examples/cache-miss-request.sequence.json` |
| `dataflow` | Pipelines, ETL/ELT, lineage, governance, consumers; where money or documents go | `schemas/dataflow.schema.json` | `examples/product-analytics.dataflow.json` |
| `lifecycle` | State/status transitions, retries, waiting and terminal states; where an application or order stands | `schemas/lifecycle.schema.json` | `examples/deployment-release.lifecycle.json` |

When ambiguous, check the examples in `examples/` for structural references. Scenario proof examples are structural references, not facts to copy.

For an everyday subject, keep the same five modes and semantic types, then name them for the reader: use everyday `icon` values and `meta.legend` labels as in [Node icons](references/authoring-contract.md#node-icons). Ask for missing personal facts instead of inventing dates, amounts, or rules.

## Mermaid input

Read Mermaid for topology and meaning, then author fresh Archify JSON; do not mechanically render Mermaid styling.

- `flowchart` / `graph` → `workflow`, or `architecture` for a component map.
- `sequenceDiagram` → `sequence`; participants become semantic participants and arrows become messages.
- `stateDiagram` → `lifecycle`; states and transitions retain meaning, not Mermaid style.

## Viewer and visual review

`meta.animation: "trace"` is opt-in. Read `references/viewer-runtime.md` when the user requests motion, Share Cards, deep links, presentation, search/focus, or other Viewer features. Viewer exports and interactions need their own browser verification; Python generation alone does not verify them.

Perceptual review is optional. If a user requests it, inspect the generated artifact in an available browser or image-capable workflow and record the actual scope. No screenshot or perceptual review is performed by this CLI. Report `visualReview: "not-requested"` unless a separate review actually occurred; disclose a requested review that could not run.

## Handoff

Return the absolute HTML path, diagram type, `.python-delivery.json` path, static-check and artifact-integrity statuses, specification/artifact hashes, browser policy/status/reason, and truthful visual-review status. Summarize rather than copying the entire receipt. Do not claim a legacy “9/9 showcase” gate, browser acceptance, or production provenance transaction that this runtime does not implement. No update check, live installation change, or automatic opening is performed.
