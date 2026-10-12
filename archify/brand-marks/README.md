# Built-in brand marks

Archify ships a bounded catalogue of 107 commonly used brands for architecture,
workflow, sequence, data-flow, and lifecycle nodes. The mark is optional authored
identity: it never replaces the node's semantic `type`, color, label, or
relationships.

The built-in marks are stored in `python/archify_runtime/data/brands.json`.
Digest-pinned remote URL capture is unsupported in the pure Python runtime. Normal
render and validate commands do not perform network requests.

Most vector paths and brand metadata are generated from Simple Icons 16.28.0.
The OpenAI mark is traced to OpenAI's official brand guidelines. Every generated
entry records its source and, when available upstream, its guidelines and license
metadata in `python/archify_runtime/data/brands.json`.

Brand names and logos may be trademarks of their respective owners. Simple
Icons' CC0 license covers its collection work, not every underlying trademark or
artwork. Contributors must review the recorded source, current brand guidelines,
and intended referential use before adding or updating a mark. Archify does not
imply sponsorship, endorsement, or partnership.

The current native catalogue is `../python/archify_runtime/data/brands.json`;
review its provenance and runtime tests when changing marks. The old generated
JavaScript renderer and npm generation commands are not part of this runtime.
Agent-facing usage is in [Brand marks](../references/brand-marks.md). This
maintenance README and legacy `catalog.json` build input are excluded from the
distributed Skill.
