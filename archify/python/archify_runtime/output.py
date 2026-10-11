"""Native output resolution and snapshots for recoverable publication."""

import hashlib
import os
from pathlib import Path
import stat

from .common import DiagnosticError, diagnostic


def fail(code, message, output):
    raise DiagnosticError(message, [diagnostic(code, message, {"output": str(output)},
        {"reason": message}, ["choose a separate regular .html output without existing delivery sidecars or locks"])])


def canonical_path(value):
    """Resolve directory aliases without following a final file symlink."""
    path = Path(os.path.abspath(value))
    return path.parent.resolve() / path.name


def resolve_output(requested, authored, input_path, default_output="sequence.html"):
    raw = requested if requested is not None else authored
    if requested is not None:
        from .validation import validate_authored_output
        portable = str(requested)
        if portable.endswith("/") or os.name == "nt" and portable.endswith("\\"):
            fail("output/cli-path-syntax", "Output must name a file, not a trailing directory separator.", requested)
        if os.name != 'nt' and '\\' in portable:
            fail('output/cli-path-syntax', 'Backslashes are not native output separators on this platform.', requested)
        if os.name == 'nt' and Path(requested).drive and not Path(requested).root:
            fail('output/cli-path-syntax', 'Drive-relative outputs are ambiguous.', requested)
        # Existing native directories belong to the host, not to meta.output.
        # Validate the filename and only directories this command would create.
        native = Path(os.path.abspath(requested))
        components = [native.name]
        parent = native.parent
        while not parent.exists() and parent != parent.parent:
            components.insert(0, parent.name)
            parent = parent.parent
        try:
            validate_authored_output('/'.join(components))
        except DiagnosticError as error:
            message = "CLI output must name a file with portable new path components."
            raise DiagnosticError(message, [diagnostic("output/cli-path-syntax", message,
                {"output": str(requested)}, entry["evidence"],
                ["choose a portable .html filename and new directory names"])
                for entry in error.diagnostics]) from error
    target = Path(os.path.abspath(raw or default_output))
    try:
        same = target.resolve() == input_path.resolve() or target.exists() and os.path.samefile(target, input_path)
    except OSError:
        same = False
    if same:
        fail("output/input-alias", "Output must not replace the input JSON file.", target)
    if target.suffix.lower() != ".html":
        fail("output/cli-extension" if requested is not None else "output/meta-extension", "Output must be an .html file.", target)
    if requested is None:
        try:
            target.resolve().relative_to(Path.cwd().resolve())
        except ValueError:
            fail("output/meta-outside-cwd", "Authored output must stay beneath the current working directory.", target)
    if target.is_symlink():
        fail("output/target-not-regular-file", "Output must not replace a symlink.", target)
    # Resolve directory aliases (including macOS /tmp) to the physical parent.
    return canonical_path(target)


def snapshot(target):
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        fail("output/target-not-regular-file", "Output must name a regular file with one hard link.", target)
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
            metadata.st_ctime_ns, metadata.st_mode, hashlib.sha256(target.read_bytes()).digest())


def publish(target, html, expected, command, receipt_id, receipt=None, verifier=None):
    """Publish a recoverable artifact/receipt transaction under owned locks."""
    from .delivery import publish_transaction
    if receipt is None:
        fail("delivery/receipt-required", "Publication requires a byte-bound delivery receipt.", target)
    return publish_transaction(target, html, expected, receipt_id, receipt, verifier)
