"""Verify source references against pinned objects in a local Git repository."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote, unquote_to_bytes, urlsplit

from .common import DiagnosticError, diagnostic

FULL_SHA_RE = re.compile(r"^[a-fA-F0-9]{40}$")
CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_SOURCE_BYTES = 16 * 1024 * 1024

EVIDENCE_NODE_COLLECTIONS = {
    "architecture": "components",
    "workflow": "nodes",
    "sequence": "participants",
    "dataflow": "nodes",
    "lifecycle": "states",
}


def _js_truthy(value):
    return not (value is None or value is False or value == "" or value == 0)


def parse_remote(value, authored=False):
    """Parse a repository remote into its canonical identity and web address.

    This is deliberately local parsing only. It does not resolve a host, read
    SSH configuration, or contact the remote. The rules mirror
    ``repository-location.mjs`` so custom local-only remotes retain their
    transport and path identity.
    """
    if not isinstance(value, str):
        return None
    raw = value if authored else value.strip()
    if not raw or re.search(r"[\s\\\x00-\x1f\x7f?#]", raw):
        return None

    scp_match = re.match(r"^git@([^/:]+):(.+)$", raw)
    scp = scp_match is not None
    scp_absolute = bool(scp and scp_match.group(2).startswith("/"))
    if scp:
        host_text, raw_path = scp_match.groups()
        expanded = f"ssh://git@{host_text}/{raw_path[1:] if scp_absolute else raw_path}"
    else:
        expanded = raw
        uri_match = re.match(r"^(https?|ssh)://([^/]+)/(.+)$", expanded, re.I)
        if not uri_match:
            return None
        raw_path = uri_match.group(3)

    if not scp:
        # Keep the pre-URL path spelling: URL parsers normalize dot segments.
        raw_path = raw_path[:-1] if raw_path.endswith("/") else raw_path
        encoded_segments = raw_path.split("/")
        try:
            segments = []
            for segment in encoded_segments:
                if re.search(r"%(?![0-9a-fA-F]{2})", segment):
                    return None
                segments.append(unquote_to_bytes(segment).decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError):
            return None
    else:
        raw_path = raw_path[1:] if scp_absolute else raw_path
        raw_path = raw_path[:-1] if raw_path.endswith("/") else raw_path
        segments = raw_path.split("/")

    if any(not part or part in (".", "..") or re.search(r"[/\\\s\x00-\x1f\x7f?#]", part)
           for part in segments):
        return None

    try:
        parsed = urlsplit(expanded)
        protocol_name = parsed.scheme.lower()
        protocol = protocol_name + ":"
        # Accessing .port validates malformed and out-of-range ports.
        explicit_port = parsed.port
        username, password = parsed.username, parsed.password
        hostname = parsed.hostname
        if not hostname or "%" in hostname:
            return None
        hostname = hostname.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None

    if protocol_name not in ("http", "https", "ssh"):
        return None
    if protocol_name == "ssh" and (username != "git" or password):
        return None
    if authored and protocol_name != "ssh" and (username or password):
        return None

    provider = "github" if hostname == "github.com" else "gitee" if hostname == "gitee.com" else None
    if provider:
        suffix = re.compile(r"\.git$", re.I if provider == "github" else 0)
        segments[-1] = suffix.sub("", segments[-1])
    if not segments[-1] or segments[-1] in (".", ".."):
        return None

    repository_path = "/".join(segments)
    port = (explicit_port if explicit_port is not None else
            {"ssh": 22, "https": 443, "http": 80}[protocol_name])
    endpoint = ("standard" if provider and
                ((protocol_name == "https" and port == 443) or
                 (protocol_name == "ssh" and port == 22))
                else f"{protocol}{port}")
    path_kind = "repository" if provider else "relative" if scp and not scp_absolute else "absolute"
    identity_path = repository_path.lower() if provider == "github" else repository_path
    identity = json.dumps([hostname, endpoint, path_kind, identity_path],
                          ensure_ascii=False, separators=(",", ":"))

    if scp:
        canonical_url = f"git@{hostname}:{'/' if scp_absolute else ''}{repository_path}"
    else:
        host = f"[{hostname}]" if ":" in hostname else hostname
        # WHATWG URL serialization drops the default port for HTTP(S). SSH is
        # non-special and retains an explicitly written default port.
        if explicit_port is not None and not (
                protocol_name in ("http", "https") and
                explicit_port == {"http": 80, "https": 443}[protocol_name]):
            host += f":{explicit_port}"
        encoded_path = "/".join(quote(part, safe="-_.!~*'()") for part in segments)
        auth = "git@" if protocol_name == "ssh" else ""
        canonical_url = f"{protocol}//{auth}{host}/{encoded_path}"

    return {
        "identity": identity,
        "url": canonical_url,
        "provider": provider,
        "protocol": protocol,
        "path": repository_path,
        "endpoint": endpoint,
    }


def has_repository_evidence(diagram, diagram_type):
    collection = EVIDENCE_NODE_COLLECTIONS.get(diagram_type)
    if not collection:
        return False
    nodes = diagram.get(collection, []) if isinstance(diagram, dict) else []
    meta = diagram.get("meta") if isinstance(diagram, dict) else None
    repository = meta.get("repository") if isinstance(meta, dict) else None
    has_repository = _js_truthy(repository)
    return has_repository or (isinstance(nodes, list) and any(
        isinstance(node, dict) and isinstance(node.get("sources"), list) and node["sources"]
        for node in nodes))


def _fail(code, message, *, subject=None, evidence=None, fixes=None):
    details = {"surface": "repository-evidence"}
    details.update(subject or {})
    raise DiagnosticError(message, [diagnostic(code, message, details, evidence, fixes)])


def _source_path(value, where):
    source_path = str(value or "")
    if (not source_path or source_path.startswith("/") or "\\" in source_path
            or CONTROL_CHARACTER_RE.search(source_path)):
        _fail("repository-evidence/path-invalid", f"{where} must be a repo-relative POSIX path.",
              subject={"path": where}, evidence={"authoredPath": source_path},
              fixes=["use a repository-relative path with forward slashes"])
    parts = source_path.split("/")
    if any(not part or part in (".", "..") for part in parts) or parts[0] == ".git":
        _fail("repository-evidence/path-escape",
              f"{where} must stay inside the repository and may not address .git.",
              subject={"path": where}, evidence={"authoredPath": source_path},
              fixes=["remove empty, dot, parent, or .git path segments"])
    try:
        source_path.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("repository-evidence/path-invalid", f"{where} must be a repo-relative POSIX path.",
              subject={"path": where}, evidence={"authoredPath": source_path},
              fixes=["use a repository-relative path with forward slashes"])
    return source_path


def _run_git(repo_root, args):
    executable = os.environ.get("ARCHIFY_GIT") or "git"
    try:
        return subprocess.run(
            [executable, "--no-replace-objects", "-C", os.fspath(repo_root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        _fail("repository-evidence/git-unavailable", f"Could not run Git: {error}",
              evidence={"reason": str(error)},
              fixes=["install Git and ensure it is available on PATH"])


def _git_value(repo_root, args, failure):
    result = _run_git(repo_root, args)
    if result.returncode != 0:
        _fail("repository-evidence/git-command", failure,
              evidence={"gitArgs": args, "exitCode": result.returncode},
              fixes=["use the intended local Git repository and verify its origin and revision"])
    return result.stdout.decode("utf-8", "replace").strip()


def _redact_remote(value):
    redacted = re.sub(r"^((?:https?|ssh)://)[^/]*@", r"\1REDACTED@", str(value or ""), flags=re.I)
    return re.sub(r"[?#].*$", "?REDACTED", redacted, flags=re.S)


def _tree_blob(repo_root, revision, source_path):
    pathspec = ":(literal)" + source_path
    result = _run_git(repo_root, ["ls-tree", "-z", "--full-tree", revision, "--", pathspec])
    if result.returncode != 0:
        return None
    path_bytes = source_path.encode("utf-8")
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        try:
            header, entry_path = entry.split(b"\t", 1)
            mode, kind, object_id = header.decode("ascii").split(" ")
        except (ValueError, UnicodeDecodeError):
            continue
        if entry_path != path_bytes:
            continue
        # A symlink is represented by a blob too; only ordinary tracked files
        # can support repository evidence. Gitlinks are not blob objects.
        if kind != "blob" or mode not in ("100644", "100755"):
            return None
        return object_id
    return None


def _line_count(content):
    if not content:
        return 0
    lines = re.split(rb"\r\n|\n|\r", content)
    return len(lines) - (1 if re.search(rb"(?:\r\n|\n|\r)$", content) else 0)


def _source_href(location, revision, source):
    encoded_path = "/".join(quote(part, safe="-_.!~*'()") for part in source["path"].split("/"))
    end_line = source.get("endLine")
    # GitHub spells range fragments #L1-L3; Gitee uses #L1-3.
    suffix = (f"-{'L' if location['provider'] == 'github' else ''}{end_line}"
              if end_line and end_line != source.get("line") else "")
    fragment = f"#L{source['line']}{suffix}" if source.get("line") else ""
    return f"{location['url']}/blob/{revision}/{encoded_path}{fragment}"


def verify_repository(diagram, diagram_type, repo_root=None):
    """Return verified repository evidence or raise a diagnostic error.

    The pinned commit and its tree are the only source of truth. The checked
    out worktree is used only to identify the local repository and its origin.
    """
    collection = EVIDENCE_NODE_COLLECTIONS.get(diagram_type)
    if not collection:
        return None
    diagram = diagram if isinstance(diagram, dict) else {}
    authored_nodes = diagram.get(collection)
    if not isinstance(authored_nodes, list):
        authored_nodes = []
    meta = diagram.get("meta") if isinstance(diagram.get("meta"), dict) else {}
    if not has_repository_evidence(diagram, diagram_type):
        return None
    repository = meta.get("repository")
    if not _js_truthy(repository):
        _fail("repository-evidence/repository-required", "Repository evidence requires /meta/repository.",
              subject={"path": "/meta/repository", "diagramType": diagram_type, "collection": collection},
              fixes=[f"add the pinned repository metadata or remove /{collection} sources"])
    revision_value = repository.get("revision") if isinstance(repository, dict) else None
    if not isinstance(revision_value, str) or not FULL_SHA_RE.fullmatch(revision_value):
        _fail("repository-evidence/revision-invalid",
              "/meta/repository/revision must be a full 40-character commit SHA.",
              subject={"path": "/meta/repository/revision"}, evidence={"revision": revision_value},
              fixes=["pin one full 40-character commit SHA"])

    repository_url = repository.get("url")
    location = parse_remote(repository_url, authored=True)
    if not location:
        filesystem_path = bool(re.match(
            r"^(?:[\\/]|~|\.{1,2}(?:[\\/]|$)|[A-Za-z]:[\\/])",
            "" if repository_url is None else str(repository_url)))
        _fail("repository-evidence/url-invalid",
              "/meta/repository/url must be a credential-free HTTP(S) or Git SSH repository address without query, fragment, or dot segments.",
              subject={"path": "/meta/repository/url"},
              evidence=({"authoredValueLooksLike":
                         "local filesystem path; the expected value is the remote origin address"}
                        if filesystem_path else {}),
              fixes=["run `git remote get-url origin` inside --repo-root and declare that credential-free address",
                     "use link_mode: local-only for internal repositories"])

    raw_link_mode = repository.get("link_mode")
    link_mode = "web" if raw_link_mode is None else raw_link_mode
    if link_mode not in ("web", "local-only"):
        _fail("repository-evidence/link-mode-invalid", "Repository link_mode must be web or local-only.")
    if "provider" in repository and (
            repository["provider"] not in ("github", "gitee") or
            repository["provider"] != location["provider"]):
        _fail("repository-evidence/provider-invalid",
              "Repository provider must match its supported public host (github.com or gitee.com).",
              subject={"path": "/meta/repository/provider"},
              fixes=["use the matching provider or omit provider and select link_mode: local-only"])
    if link_mode == "web" and (
            not location["provider"] or location["protocol"] != "https:" or
            location["endpoint"] != "standard" or
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", location["path"])):
        _fail("repository-evidence/links-unsupported",
              "Web source links require a canonical GitHub or Gitee HTTPS owner/repository URL.",
              subject={"path": "/meta/repository/url"},
              fixes=["use a canonical GitHub or Gitee URL, or select link_mode: local-only to retain local verification without web links"])
    if not repo_root:
        _fail("repository-evidence/root-required",
              "This diagram declares source evidence. Pass --repo-root <repository> so Archify can verify it before rendering.",
              subject={"path": "/meta/repository"},
              fixes=["pass --repo-root with the matching local Git checkout"])

    requested_root = os.path.abspath(os.fspath(repo_root))
    try:
        real_root = str(Path(requested_root).resolve(strict=True))
    except OSError as error:
        _fail("repository-evidence/root-unreadable",
              f'Could not resolve evidence repository root "{requested_root}": {error}.',
              subject={"repoRoot": requested_root}, evidence={"reason": str(error)},
              fixes=["pass one readable local repository directory"])

    git_root = _git_value(real_root, ["rev-parse", "--show-toplevel"],
                          f'Evidence root "{real_root}" is not a Git repository.')
    try:
        same_root = (os.stat(real_root).st_dev, os.stat(real_root).st_ino) == (
            os.stat(git_root).st_dev, os.stat(git_root).st_ino)
    except OSError as error:
        _fail("repository-evidence/root-identity-indeterminate",
              "Could not determine whether the evidence root is the Git top-level directory.",
              subject={"repoRoot": real_root},
              evidence={"gitTopLevel": git_root,
                        "relation": {"code": "root-resolution-failed", "operation": "same-entry",
                                     "systemCode": error.errno}},
              fixes=["pass the readable Git top-level directory using its canonical filesystem path"])
    if not same_root:
        _fail("repository-evidence/root-not-top-level",
              f"Evidence root must be the Git top-level directory: {git_root}",
              subject={"repoRoot": real_root}, evidence={"gitTopLevel": git_root},
              fixes=[f"pass --repo-root {git_root}"])

    origin = _git_value(real_root, ["remote", "get-url", "origin"],
                         "Evidence repository must have an origin remote.")
    parsed_origin = parse_remote(origin)
    if not parsed_origin or parsed_origin["identity"] != location["identity"]:
        safe_origin = _redact_remote(origin)
        _fail("repository-evidence/origin-mismatch",
              f"Evidence repository origin {json.dumps(safe_origin, ensure_ascii=False)} does not match {json.dumps(repository_url, ensure_ascii=False)}.",
              subject={"repoRoot": real_root},
              evidence={"localOrigin": safe_origin, "authoredRepository": repository_url},
              fixes=["use the matching local checkout or correct the authored repository URL"])

    revision = revision_value.lower()
    commit = _run_git(real_root, ["cat-file", "-e", f"{revision}^{{commit}}"])
    if commit.returncode != 0:
        _fail("repository-evidence/revision-unavailable",
              f"Evidence revision {revision} is not available in the local repository.",
              subject={"repoRoot": real_root}, evidence={"revision": revision},
              fixes=["fetch the pinned commit or pin an available full commit SHA"])

    verified_nodes = {}
    reference_count = 0
    for node_index, node in enumerate(authored_nodes):
        if not isinstance(node, dict) or not isinstance(node.get("sources"), list) or not node["sources"]:
            continue
        node_id = node.get("id")
        node_subject = {"diagramType": diagram_type, "collection": collection, "nodeId": node_id}
        if collection == "components":
            node_subject["componentId"] = node_id
        verified_sources = []
        for source_index, authored in enumerate(node["sources"]):
            authored = authored if isinstance(authored, dict) else {}
            at = f"/{collection}/{node_index}/sources/{source_index}"
            where = f"{at}/path"
            source_path = _source_path(authored.get("path"), where)
            line = authored.get("line")
            end_line = authored.get("end_line")
            source = {"path": source_path}
            if line:
                source["line"] = line
            if end_line:
                source["endLine"] = end_line
            if authored.get("label"):
                source["label"] = authored["label"]
            if end_line and not line:
                _fail("repository-evidence/line-required", f"{at}/end_line requires line.",
                      subject={"path": f"{at}/end_line", **node_subject},
                      fixes=["add line or remove end_line"])
            if end_line and end_line < line:
                _fail("repository-evidence/line-range-invalid",
                      f"{at}/end_line must be greater than or equal to line.",
                      subject={"path": at, **node_subject}, evidence={"line": line, "endLine": end_line},
                      fixes=["use an end_line greater than or equal to line"])

            object_id = _tree_blob(real_root, revision, source_path)
            if object_id is None:
                _fail("repository-evidence/file-missing",
                      f"{where} does not identify a file at revision {revision}.",
                      subject={"path": where, **node_subject},
                      evidence={"sourcePath": source_path, "revision": revision},
                      fixes=["use a file path that exists at the pinned revision"])
            if line:
                size_result = _run_git(real_root, ["cat-file", "-s", object_id])
                if size_result.returncode != 0:
                    _fail("repository-evidence/file-unreadable",
                          f"{where} could not be read at revision {revision}.",
                          subject={"path": where, **node_subject},
                          evidence={"sourcePath": source_path, "revision": revision},
                          fixes=["verify the pinned blob is readable in the local checkout"])
                try:
                    source_size = int(size_result.stdout.strip())
                except ValueError:
                    source_size = MAX_SOURCE_BYTES + 1
                if source_size > MAX_SOURCE_BYTES:
                    _fail("repository-evidence/git-unavailable",
                          "Could not run Git: source blob exceeds the 16 MiB capture limit.",
                          evidence={"reason": "source blob exceeds the 16 MiB capture limit"},
                          fixes=["install Git and ensure it is available on PATH"])
                content_result = _run_git(real_root, ["cat-file", "blob", object_id])
                if content_result.returncode != 0 or len(content_result.stdout) > MAX_SOURCE_BYTES:
                    _fail("repository-evidence/file-unreadable",
                          f"{where} could not be read at revision {revision}.",
                          subject={"path": where, **node_subject},
                          evidence={"sourcePath": source_path, "revision": revision},
                          fixes=["verify the pinned blob is readable in the local checkout"])
                count = _line_count(content_result.stdout)
                requested_line = end_line or line
                if requested_line > count:
                    _fail("repository-evidence/line-out-of-range",
                          f"{at} requests line {requested_line}, but {source_path} has {count} lines at revision {revision}.",
                          subject={"path": at, **node_subject},
                          evidence={"sourcePath": source_path, "requestedLine": requested_line,
                                    "lineCount": count, "revision": revision},
                          fixes=["use a line range that exists at the pinned revision"])

            if link_mode == "web":
                source["href"] = _source_href(location, revision, source)
            verified_sources.append(source)
            reference_count += 1
        verified_nodes[node_id] = verified_sources

    if reference_count == 0:
        _fail("repository-evidence/source-required",
              f"/meta/repository requires at least one /{collection} source reference.",
              subject={"path": "/meta/repository", "diagramType": diagram_type,
                       "collection": collection},
              fixes=[f"add at least one verified /{collection} source or remove repository metadata"])

    verified_repository = {
        "url": location["url"],
        "revision": revision,
        "shortRevision": revision[:7],
        "label": location["path"] if location["provider"] == "github" else
                 re.sub(r"^(?:https?://|ssh://git@|git@)", "", location["url"]),
    }
    if link_mode == "web":
        verified_repository["href"] = f"{location['url']}/tree/{revision}"
    else:
        verified_repository["linkMode"] = link_mode
    return {
        "schemaVersion": 1,
        "verified": True,
        "repository": verified_repository,
        "referenceCount": reference_count,
        "nodes": verified_nodes,
    }
