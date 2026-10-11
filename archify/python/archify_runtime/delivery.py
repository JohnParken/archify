"""Recoverable, byte-bound publication of an artifact and its receipts.

A journal is flushed before any public replacement. Unknown owners and changed
claimants are preserved. The pair is recoverable, not crash-atomic.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .common import DiagnosticError, diagnostic
from .integrity import CONTRACT, read_regular, receipt_path


def fail(code, message, path):
    raise DiagnosticError(message, [diagnostic(code, message, {"path": str(path)},
        supported_fixes=["inspect the owned transaction and run archify recover <output.html>; preserve foreign files"])])


def digest(content):
    return {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def sidecar(output, suffix):
    native = receipt_path(Path(output))
    return native.with_name(native.name.removesuffix(".python-delivery.json") + suffix)


def flush_file(path, content, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    if mode != 0o600:
        os.chmod(path, mode)


def flush_directory(path):
    # Directory fsync is not exposed consistently on Windows.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def safe_path(path):
    for part in [path, *path.parents]:
        if part.is_symlink():
            fail("delivery/unsafe-path", "Transaction paths must not contain symlinks.", path)


def current(path):
    if not path.exists() and not path.is_symlink():
        return None
    return digest(read_regular(path))


def alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    # On Windows os.kill(pid, 0) can terminate a process on some Python versions.
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return ctypes.windll.kernel32.GetLastError() != 87
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _load(output, require_dead):
    output = Path(os.path.abspath(output))
    if output.is_symlink():fail("delivery/unsafe-path", "Recovery must not follow an output symlink.", output)
    output = output.resolve()
    safe_path(output)
    journal = sidecar(output, ".delivery-pending.json")
    raw = read_regular(journal)
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError):
        fail("delivery/journal-invalid", "Transaction journal is not valid JSON.", journal)
    identifier = record.get("receiptId", "") if isinstance(record, dict) else ""
    if (not isinstance(record, dict) or record.get("transactionVersion") != 1
            or record.get("runtime") != "python" or record.get("output") != str(output)
            or not isinstance(identifier, str) or not identifier
            or any(c not in "0123456789abcdef-" for c in identifier)):
        fail("delivery/journal-invalid", "Journal does not own this output.", journal)
    directory = output.parent / (".archify-txn-" + identifier)
    if require_dead and alive(record.get("pid")):
        fail("delivery/concurrent-attempt", "The transaction owner may still be running.", journal)
    completed = record.get("phase") in ("committed", "rolled-back")
    if record.get("directory") != str(directory) or directory.is_symlink() or (not directory.is_dir() and not completed):
        fail("delivery/journal-invalid", "Recovery directory is missing or unsafe.", directory)
    info = directory.stat() if directory.exists() else None
    if info and [info.st_dev, info.st_ino] != record.get("directoryIdentity"):
        fail("delivery/ownership-lost", "Recovery directory ownership changed.", directory)
    entries = record.get("entries")
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict) or entries[0].get("path") != str(output):
        fail("delivery/journal-invalid", "Journal target list is invalid.", journal)
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if len(paths) != len(entries) or len(set(paths)) != len(paths):
        fail("delivery/journal-invalid", "Journal targets are not unique.", journal)
    expected_locks = {str(Path(path).parent / ".archify-delivery-lock.json") for path in paths}
    locks = record.get("locks", [])
    if not isinstance(locks, list) or any(not isinstance(item, dict) for item in locks) or {item.get("path") for item in locks} != expected_locks:
        fail("delivery/journal-invalid", "Journal lock list is invalid.", journal)
    for item in locks:
        lock = Path(item["path"])
        safe_path(lock)
        if completed and not lock.exists() and not lock.is_symlink():
            continue
        owner = json.loads(read_regular(lock))
        info = lock.stat()
        if (owner.get("receiptId") != identifier or [info.st_dev, info.st_ino] != item.get("identity")
                or owner.get("pid") != record.get("pid")):
            fail("delivery/ownership-lost", "Transaction lock has a different owner.", lock)
        if require_dead and alive(owner.get("pid")):
            fail("delivery/concurrent-attempt", "The transaction owner may still be running.", lock)
    for index, entry in enumerate(entries):
        target = Path(entry["path"])
        safe_path(target)
        if str(target.absolute()) != entry["path"] or (index and target.suffix.lower() != ".json"):
            fail("delivery/journal-invalid", "Recovery target spelling is invalid.", target)
        if not completed and entry.get("old") is not None and digest(read_regular(directory / ("old-" + str(index)))) != entry["old"]:
            fail("delivery/backup-mismatch", "Recovery backup was changed.", directory)
    if read_regular(journal) != raw:
        fail("delivery/ownership-lost", "Journal changed during inspection.", journal)
    return journal, record, directory, raw


def _rollback(output, require_dead=False):
    journal, record, directory, raw = _load(output, require_dead)
    # Check all names before restoring any: never overwrite an unrelated claimant.
    for entry in record["entries"]:
        found = current(Path(entry["path"]))
        if found not in (entry.get("old"), entry.get("new")):
            fail("delivery/recovery-conflict", "A transaction target was changed by another writer.", entry["path"])
    for index, entry in reversed(list(enumerate(record["entries"]))):
        target = Path(entry["path"])
        if current(target) == entry.get("old"):
            continue
        if entry.get("old") is None:
            target.unlink()
        else:
            restored = directory / ("restore-" + str(index))
            if restored.exists() or restored.is_symlink():
                if current(restored) != entry["old"]:
                    fail("delivery/recovery-conflict", "Restore candidate does not match the owned backup.", restored)
            else:
                flush_file(restored, read_regular(directory / ("old-" + str(index))), entry.get("mode", 0o600))
            if current(target) != entry.get("new"):
                fail("delivery/recovery-conflict", "Target changed during recovery.", target)
            os.replace(restored, target)
        flush_directory(target.parent)
    _mark_phase(journal, record, directory, raw, "rolled-back")
    return _complete(output, require_dead=False)


def _mark_phase(journal, record, directory, raw, phase):
    if read_regular(journal) != raw:
        fail("delivery/ownership-lost", "Journal changed before completion.", journal)
    record["phase"] = phase
    candidate = directory / ("journal-" + phase)
    encoded = (json.dumps(record, indent=2) + "\n").encode()
    if candidate.exists() or candidate.is_symlink():
        if read_regular(candidate) != encoded:
            fail("delivery/recovery-conflict", "Completion candidate has unknown bytes.", candidate)
    else:
        flush_file(candidate, encoded)
    if read_regular(journal) != raw:
        fail("delivery/ownership-lost", "Journal changed during completion.", journal)
    os.replace(candidate, journal)
    flush_directory(journal.parent)


def _complete(output, require_dead):
    journal, record, directory, raw = _load(output, require_dead)
    phase = record.get("phase")
    if phase not in ("committed", "rolled-back"):
        fail("delivery/journal-invalid", "Transaction has no completion marker.", journal)
    for entry in record["entries"]:
        if current(Path(entry["path"])) != entry.get("new" if phase == "committed" else "old"):
            fail("delivery/recovery-conflict", "Completed transaction bytes were changed.", entry["path"])
    for item in record["locks"]:
        lock = Path(item["path"])
        if not lock.exists() and not lock.is_symlink():
            continue
        owner = json.loads(read_regular(lock))
        info = lock.lstat()
        if owner.get("receiptId") != record["receiptId"] or [info.st_dev, info.st_ino] != item["identity"]:
            fail("delivery/ownership-lost", "Lock changed during completion.", lock)
        lock.unlink()
        flush_directory(lock.parent)
    if directory.exists():
        info = directory.lstat()
        if directory.is_symlink() or [info.st_dev, info.st_ino] != record["directoryIdentity"]:
            fail("delivery/ownership-lost", "Recovery directory changed.", directory)
        shutil.rmtree(directory)
    if read_regular(journal) != raw:
        fail("delivery/ownership-lost", "Journal changed during completion.", journal)
    journal.unlink()
    flush_directory(journal.parent)
    return {"schemaVersion": 1, "ok": True, "command": "recover", "output": record["output"],
            "receiptId": record["receiptId"], "status": phase}


def recover(output):
    _, record, _, _ = _load(output, require_dead=True)
    return _complete(output, require_dead=True) if record.get("phase") else _rollback(output, require_dead=True)


def publish_transaction(target, html, expected, receipt_id, receipt, verifier):
    from .output import snapshot
    target = Path(target)
    native = receipt_path(target)
    compatibility = sidecar(target, ".delivery.json")
    extra = receipt.get("requestedReceipt")
    targets = [target, native, compatibility]
    if extra and Path(extra) not in targets:
        targets.append(Path(extra))
    for path in targets:
        if path.name == ".archify-delivery-lock.json" or path.name.endswith(".delivery-pending.json"):
            fail("output/receipt-conflict", "Receipt must not name a transaction lock or journal.", path)
        safe_path(path)
        if path != target and path.suffix.lower() != ".json":
            fail("output/receipt-extension", "Receipt outputs must end in .json.", path)
        from .output import canonical_path
        for field in ("input", "baseInput"):
            source = receipt.get(field)
            if source and (canonical_path(path) == canonical_path(source)
                    or path.exists() and Path(source).exists() and os.path.samefile(path, source)):
                fail("output/input-alias", "A receipt must not replace either input.", path)
        path.parent.mkdir(parents=True, exist_ok=True)
    journal = sidecar(target, ".delivery-pending.json")
    if journal.exists() or journal.is_symlink():
        fail("delivery/recovery-required", "An unfinished transaction requires explicit recovery.", journal)
    owned = []
    directory = None
    directory_identity = None
    journal_written = False
    committed = False
    try:
        for parent in sorted({path.parent for path in targets}, key=str):
            lock = parent / ".archify-delivery-lock.json"
            try:
                flush_file(lock, json.dumps({"schemaVersion": 1, "receiptId": receipt_id, "pid": os.getpid()}).encode())
            except FileExistsError:
                fail("delivery/concurrent-attempt", "An existing delivery lock requires inspection or recovery.", lock)
            info = lock.stat()
            owned.append({"path": str(lock), "identity": [info.st_dev, info.st_ino]})
        if snapshot(target) != expected:
            fail("output/target-changed", "Output changed while preparing the candidate.", target)
        for path in targets[1:]:
            if current(path) is not None:
                try:
                    prior = json.loads(read_regular(path))
                except (ValueError, UnicodeError):
                    fail("integrity/receipt-conflict", "Unknown receipt was preserved.", path)
                def input_owner(field):
                    old, new = prior.get(field), receipt.get(field)
                    return old == new if old is None or new is None else (
                        isinstance(old, str) and isinstance(new, str)
                        and canonical_path(old) == canonical_path(new))
                if (not isinstance(prior, dict) or prior.get("deliveryContract") != CONTRACT
                        or prior.get("output") != str(target) or not input_owner("input")
                        or not input_owner("baseInput")
                        or prior.get("type") != receipt.get("type") or expected is None
                        or prior.get("artifact") != {"sha256": expected[6].hex(), "bytes": expected[2]}):
                    fail("integrity/receipt-conflict", "Receipt does not own the previous output.", path)
        receipt["artifactIntegrity"] = {"status": "passed", "specification": receipt["specification"], "artifact": digest(html)}
        receipt["publication"] = {"status": "committed", "transactionVersion": 1, "recoverable": True}
        receipt.setdefault("provenance", "byte-bound")
        encoded = (json.dumps(receipt, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode()
        contents = [html] + [encoded] * (len(targets) - 1)
        candidate_directory = target.parent / (".archify-txn-" + receipt_id)
        candidate_directory.mkdir(mode=0o700)
        directory = candidate_directory
        info = directory.stat()
        directory_identity = [info.st_dev, info.st_ino]
        entries = []
        for index, (path, content) in enumerate(zip(targets, contents)):
            old = current(path)
            mode = stat.S_IMODE(path.stat().st_mode) if old else 0o600
            if old is not None:
                previous = read_regular(path)
                if digest(previous) != old:
                    fail("output/target-changed", "Target changed during snapshot.", path)
                flush_file(directory / ("old-" + str(index)), previous, mode)
            flush_file(directory / ("new-" + str(index)), content, mode)
            entries.append({"path": str(path), "old": old, "new": digest(content), "mode": mode})
        record = {"transactionVersion": 1, "runtime": "python", "receiptId": receipt_id,
                  "pid": os.getpid(), "output": str(target), "directory": str(directory),
                  "directoryIdentity": [info.st_dev, info.st_ino], "entries": entries, "locks": owned}
        flush_directory(directory)
        flush_file(journal, (json.dumps(record, indent=2) + "\n").encode())
        journal_written = True
        flush_directory(target.parent)
        for index, entry in enumerate(entries):
            path = Path(entry["path"])
            if current(path) != entry["old"]:
                fail("output/target-changed", "Target changed before publication.", path)
            os.replace(directory / ("new-" + str(index)), path)
            flush_directory(path.parent)
            if index == 0 and verifier:
                verifier()
            if current(path) != entry["new"]:
                fail("integrity/artifact-mismatch", "Published bytes differ from the staged candidate.", path)
        if verifier:
            receipt["artifactIntegrity"] = verifier()
        # Persist the completion decision before releasing any ownership.
        raw_record = (json.dumps(record, indent=2) + "\n").encode()
        _mark_phase(journal, record, directory, raw_record, "committed")
        committed = True
        _complete(target, require_dead=False)
        owned = []
        directory = None
        journal_written = False
    except BaseException:
        if journal_written and not committed:
            try:
                _rollback(target)
                owned = []
                directory = None
                journal_written = False
            except (DiagnosticError, OSError, ValueError):
                # Keep every recovery byte and owned lock when a claimant conflicts.
                owned = []
                directory = None
        raise
    finally:
        if not journal_written:
            for item in reversed(owned):
                lock = Path(item["path"])
                try:
                    owner = json.loads(read_regular(lock))
                    info = lock.lstat()
                    if [info.st_dev, info.st_ino] == item["identity"] and owner.get("receiptId") == receipt_id:
                        lock.unlink()
                    else:
                        fail("delivery/ownership-lost", "Lock ownership changed; it was preserved.", lock)
                except FileNotFoundError:
                    pass
            if directory is not None and directory.exists():
                info = directory.lstat()
                if directory.is_symlink() or [info.st_dev, info.st_ino] != directory_identity:
                    fail("delivery/ownership-lost", "Staging directory owner changed.", directory)
                shutil.rmtree(directory)
