"""Conservative prototype publication: stage first, replace a stable regular file.

This does not implement the production artifact/provenance journal transaction.
Symlink destinations and existing production delivery receipts are refused.
"""

import hashlib
import os
from pathlib import Path
import stat
import tempfile

from .common import DiagnosticError, diagnostic


def fail(code, message, output):
    raise DiagnosticError(message, [diagnostic(code, message, {"output": str(output)},
        {"reason": message}, ["choose a separate regular .html output without existing delivery sidecars or locks"])])


def resolve_output(requested, authored, input_path, default_output="sequence.html"):
    raw = requested if requested is not None else authored
    target = Path(os.path.abspath(raw or default_output))
    try:
        same = target.resolve() == input_path.resolve() or target.exists() and os.path.samefile(target, input_path)
    except OSError:
        same = False
    if same:
        fail("output/input-alias", "Output must not replace the input JSON file.", target)
    if target.suffix.lower() != ".html":
        fail("output/cli-extension" if requested else "output/meta-extension", "Output must be an .html file.", target)
    if requested is None:
        try:
            target.resolve().relative_to(Path.cwd().resolve())
        except ValueError:
            fail("output/meta-outside-cwd", "Authored output must stay beneath the current working directory.", target)
    for parent in [target, *target.parents]:
        if parent.is_symlink():
            fail("python/unsupported-path", "The prototype requires an output path without symlink components.", target)
    return target


def snapshot(target):
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        fail("output/target-not-regular-file", "Output must name a regular file with one hard link.", target)
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
            metadata.st_ctime_ns, metadata.st_mode, hashlib.sha256(target.read_bytes()).digest())


def publish(target, html, expected, command, receipt_id):
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / ".archify-delivery-lock.json"
    candidate = None
    candidate_identity = None
    owned = None
    try:
        if command == "deliver":
            # Share the existing CLI mutex; never remove or recover another owner.
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                fail("delivery/concurrent-attempt", "An existing delivery lock requires inspection or recovery.", target)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                import json
                info = os.fstat(stream.fileno())
                owned = (info.st_dev, info.st_ino)
                stream.write(json.dumps({"schemaVersion": 1, "receiptId": receipt_id, "pid": os.getpid()}))
                stream.flush()
                os.fsync(stream.fileno())
                info = os.fstat(stream.fileno())
                owned = (info.st_dev, info.st_ino)
        elif lock.exists() or lock.is_symlink():
            fail("delivery/concurrent-attempt", "Output directory has an active delivery lock.", target)
        # Prevent a prototype artifact from invalidating a production receipt.
        for suffix in (".delivery.json", ".delivery-pending.json"):
            sidecar = target.with_suffix(suffix)
            if sidecar.exists() or sidecar.is_symlink():
                fail("python/unsupported-feature", "Choose a new output: production delivery sidecars are not migrated by this prototype.", target)
        for parent in [target, *target.parents]:
            if parent.is_symlink():
                fail("output/target-changed", "Output path changed before publication.", target)
        if snapshot(target) != expected:
            fail("output/target-changed", "Output changed while the candidate was being prepared.", target)
        descriptor, candidate = tempfile.mkstemp(prefix=".archify-python-", suffix=".html", dir=target.parent)
        with os.fdopen(descriptor, "wb") as stream:
            info = os.fstat(stream.fileno())
            candidate_identity = (info.st_dev, info.st_ino)
            stream.write(html)
            stream.flush()
            os.fsync(stream.fileno())
            if expected is not None:
                os.fchmod(stream.fileno(), stat.S_IMODE(expected[5]))
        if snapshot(target) != expected:
            fail("output/target-changed", "Output changed before publication.", target)
        staged = Path(candidate).lstat()
        if (staged.st_dev, staged.st_ino) != candidate_identity or staged.st_nlink != 1 or Path(candidate).read_bytes() != html:
            fail("output/target-changed", "Staged candidate changed before publication.", target)
        os.replace(candidate, target)
        candidate = None
    finally:
        if candidate is not None:
            try:
                staged = Path(candidate).lstat()
                if (staged.st_dev, staged.st_ino) == candidate_identity:
                    os.unlink(candidate)
            except FileNotFoundError:
                pass
        if owned is not None:
            info = lock.lstat()
            if (info.st_dev, info.st_ino) == owned:
                lock.unlink()
            else:
                fail("delivery/ownership-lost", "Delivery lock changed; it was preserved for inspection.", target)
