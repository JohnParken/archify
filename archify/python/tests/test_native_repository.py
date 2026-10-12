"""Repository evidence checks against isolated, local Git object databases."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from archify_runtime.common import DiagnosticError
from archify_runtime.repository import parse_remote, verify_repository


NODE_COLLECTIONS = {
    "architecture": ("components", "users"),
    "workflow": ("nodes", "users"),
    "sequence": ("participants", "users"),
    "dataflow": ("nodes", "users"),
    "lifecycle": ("states", "users"),
}
GIT = os.environ.get("ARCHIFY_GIT") or shutil.which("git") or "git"
SCRATCH = Path(tempfile.gettempdir()).resolve() / "archify-migration-evidence"


def git(root, *args):
    result = subprocess.run([GIT, "-C", str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout.decode("utf-8", "replace").strip()


class NativeRepositoryTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which(GIT):
            self.skipTest(f"git executable not found: {GIT}")
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="repository-test-", dir=str(SCRATCH))
        self.scratch = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_repository(self, origin="git@github.com:example/sourcerepo.git"):
        root = self.scratch / "repository"
        (root / "src").mkdir(parents=True)
        (root / "src" / "source #1.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
        git(root, "init", "-q")
        git(root, "config", "core.symlinks", "true")
        git(root, "config", "user.name", "Archify Tests")
        git(root, "config", "user.email", "archify@example.test")
        git(root, "remote", "add", "origin", origin)
        git(root, "add", ".")
        git(root, "commit", "-qm", "pinned source")
        return root, git(root, "rev-parse", "HEAD")

    def diagram(self, diagram_type, revision, *, path="src/source #1.py", source=None,
                url=None, **repository_fields):
        collection, node_id = NODE_COLLECTIONS[diagram_type]
        source = source if source is not None else {
            "path": path, "line": 1, "end_line": 3, "label": "Source <entry>"
        }
        repository = {"url": url or "https://github.com/Example/SourceRepo", "revision": revision,
                      **repository_fields}
        return {"meta": {"repository": repository},
                collection: [{"id": node_id, "sources": [source]}]}

    def failure_code(self, diagram_value, diagram_type, root, expected):
        with self.assertRaises(DiagnosticError) as caught:
            verify_repository(diagram_value, diagram_type, root)
        self.assertEqual(caught.exception.diagnostics[0]["code"], expected)
        return caught.exception

    def test_verifies_sources_and_builds_provider_links_for_all_modes(self):
        root, revision = self.make_repository()
        for diagram_type in NODE_COLLECTIONS:
            with self.subTest(diagram_type=diagram_type):
                evidence = verify_repository(self.diagram(diagram_type, revision), diagram_type, root)

                self.assertTrue(evidence["verified"])
                self.assertEqual(evidence["schemaVersion"], 1)
                self.assertEqual(evidence["repository"], {
                    "url": "https://github.com/Example/SourceRepo",
                    "revision": revision,
                    "shortRevision": revision[:7],
                    "label": "Example/SourceRepo",
                    "href": f"https://github.com/Example/SourceRepo/tree/{revision}",
                })
                self.assertEqual(evidence["referenceCount"], 1)
                self.assertEqual(evidence["nodes"]["users"], [{
                    "path": "src/source #1.py", "line": 1, "endLine": 3,
                    "label": "Source <entry>",
                    "href": f"https://github.com/Example/SourceRepo/blob/{revision}/src/source%20%231.py#L1-L3",
                }])

    def test_remote_identity_matches_forge_rules_and_custom_transports(self):
        github_authored = parse_remote("https://github.com/Team/Repo.GIT", authored=True)
        github_origin = parse_remote("git@github.com:team/repo.git")
        self.assertEqual(github_authored["identity"], github_origin["identity"])
        self.assertEqual(github_authored["provider"], "github")

        gitee_authored = parse_remote("https://gitee.com/Team/Repo.git", authored=True)
        self.assertNotEqual(gitee_authored["identity"],
                            parse_remote("https://gitee.com/team/Repo")["identity"])
        self.assertEqual(parse_remote("https://gitee.com/Team/Repo.GIT", authored=True)["path"],
                         "Team/Repo.GIT")

        custom_uri = parse_remote("ssh://git@git.internal/Team/repo%41", authored=True)
        custom_scp = parse_remote("git@git.internal:/Team/repoA")
        self.assertEqual(custom_uri["identity"], custom_scp["identity"])
        self.assertIsNone(custom_uri["provider"])
        self.assertEqual(custom_uri["path"], "Team/repoA")
        self.assertNotEqual(parse_remote("git@git.internal:Team/repoA")["identity"],
                            custom_uri["identity"])
        port_zero = parse_remote("https://git.internal:0/Team/Repo", authored=True)
        self.assertEqual(port_zero["endpoint"], "https:0")

        self.assertIsNone(parse_remote("https://token:secret@github.com/Team/Repo", authored=True))
        self.assertIsNone(parse_remote("ssh://user@git.internal/Team/Repo", authored=True))
        self.assertIsNone(parse_remote("https://github.com/Team/../Repo", authored=True))
        self.assertIsNone(parse_remote("https://github.com/Team/%2e%2e/Repo", authored=True))

    def test_gitee_links_and_provider_web_restrictions(self):
        root, revision = self.make_repository(origin="git@gitee.com:Example/SourceRepo.git")
        value = self.diagram("architecture", revision, url="https://gitee.com/Example/SourceRepo",
                             provider="gitee")

        evidence = verify_repository(value, "architecture", root)

        self.assertEqual(evidence["repository"]["href"],
                         f"https://gitee.com/Example/SourceRepo/tree/{revision}")
        self.assertEqual(evidence["nodes"]["users"][0]["href"],
                         f"https://gitee.com/Example/SourceRepo/blob/{revision}/src/source%20%231.py#L1-3")

        value["meta"]["repository"]["provider"] = "github"
        self.failure_code(value, "architecture", root, "repository-evidence/provider-invalid")
        value["meta"]["repository"] = {
            "url": "https://git.internal/Team/SourceRepo", "revision": revision,
        }
        self.failure_code(value, "architecture", root, "repository-evidence/links-unsupported")

    def test_local_only_custom_ssh_identity_verifies_without_links(self):
        root, revision = self.make_repository(origin="git@git.internal:/Team/repoA")
        value = self.diagram("architecture", revision,
                             url="ssh://git@git.internal/Team/repo%41", link_mode="local-only")

        evidence = verify_repository(value, "architecture", root)

        self.assertEqual(evidence["repository"], {
            "url": "ssh://git@git.internal/Team/repoA", "revision": revision,
            "shortRevision": revision[:7], "label": "git.internal/Team/repoA",
            "linkMode": "local-only",
        })
        self.assertNotIn("href", evidence["repository"])
        self.assertNotIn("href", evidence["nodes"]["users"][0])

    def test_local_only_custom_https_identity_verifies_without_links(self):
        origin = "https://git.example.internal:3000/Platform/Services/repo"
        root, revision = self.make_repository(origin=origin)
        value = self.diagram("architecture", revision, url=origin, link_mode="local-only")

        evidence = verify_repository(value, "architecture", root)

        self.assertEqual(evidence["repository"]["url"], origin)
        self.assertEqual(evidence["repository"]["linkMode"], "local-only")
        self.assertNotIn("href", evidence["repository"])

    def test_uses_pinned_blob_when_worktree_is_dirty(self):
        root, revision = self.make_repository()
        (root / "src" / "source #1.py").write_text("worktree only\n", encoding="utf-8")

        evidence = verify_repository(self.diagram("architecture", revision), "architecture", root)

        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["nodes"]["users"][0]["endLine"], 3)

    def test_disables_replacement_refs_when_reading_pinned_blob(self):
        root, original = self.make_repository()
        source = root / "src" / "source #1.py"
        source.write_text("\n".join(f"replacement {number}" for number in range(1, 9)) + "\n",
                          encoding="utf-8")
        git(root, "add", "src/source #1.py")
        git(root, "commit", "-qm", "replacement object")
        replacement = git(root, "rev-parse", "HEAD")
        git(root, "replace", original, replacement)

        value = self.diagram("architecture", original,
                             source={"path": "src/source #1.py", "line": 8})
        error = self.failure_code(value, "architecture", root,
                                  "repository-evidence/line-out-of-range")

        self.assertEqual(error.diagnostics[0]["evidence"]["lineCount"], 3)

    def test_rejects_missing_revision_foreign_root_and_bad_origin(self):
        root, revision = self.make_repository()
        error = self.failure_code(self.diagram("architecture", "f" * 40), "architecture", root,
                                  "repository-evidence/revision-unavailable")
        self.assertEqual(error.diagnostics[0]["evidence"]["revision"], "f" * 40)

        error = self.failure_code(self.diagram("architecture", revision), "architecture", root / "src",
                                  "repository-evidence/root-not-top-level")
        self.assertEqual(error.diagnostics[0]["evidence"]["gitTopLevel"], str(root))

        git(root, "remote", "set-url", "origin",
            "https://x-access-token:SECRET_VALUE@github.com/example/other-repo")
        error = self.failure_code(self.diagram("architecture", revision), "architecture", root,
                                  "repository-evidence/origin-mismatch")
        self.assertNotIn("SECRET_VALUE", str(error))
        self.assertEqual(error.diagnostics[0]["evidence"]["localOrigin"],
                         "https://REDACTED@github.com/example/other-repo")

    def test_requires_root_and_rejects_paths_and_invalid_line_ranges(self):
        root, revision = self.make_repository()
        value = self.diagram("architecture", revision)
        self.failure_code(value, "architecture", None, "repository-evidence/root-required")

        for source_path, code in (("../outside", "repository-evidence/path-escape"),
                                  (".git/config", "repository-evidence/path-escape"),
                                  ("/absolute/path", "repository-evidence/path-invalid"),
                                  ("src\\source.py", "repository-evidence/path-invalid")):
            with self.subTest(source_path=source_path):
                value = self.diagram("architecture", revision, path=source_path)
                self.failure_code(value, "architecture", root, code)

        value = self.diagram("architecture", revision,
                             source={"path": "src/source #1.py", "end_line": 2})
        self.failure_code(value, "architecture", root, "repository-evidence/line-required")
        value = self.diagram("architecture", revision,
                             source={"path": "src/source #1.py", "line": 3, "end_line": 2})
        self.failure_code(value, "architecture", root, "repository-evidence/line-range-invalid")
        value = self.diagram("architecture", revision,
                             source={"path": "src/source #1.py", "line": 4})
        error = self.failure_code(value, "architecture", root,
                                  "repository-evidence/line-out-of-range")
        self.assertEqual(error.diagnostics[0]["evidence"]["lineCount"], 3)

    def test_rejects_symlink_tree_entries_and_oversized_line_sources(self):
        root, _ = self.make_repository()
        (root / "src" / "link.py").symlink_to("source #1.py")
        git(root, "add", "src/link.py")
        git(root, "commit", "-qm", "tracked symlink")
        revision = git(root, "rev-parse", "HEAD")
        value = self.diagram("architecture", revision, source={"path": "src/link.py"})
        self.failure_code(value, "architecture", root, "repository-evidence/file-missing")

        (root / "large.txt").write_bytes(b"x" * (16 * 1024 * 1024 + 1))
        git(root, "add", "large.txt")
        git(root, "commit", "-qm", "large source")
        revision = git(root, "rev-parse", "HEAD")
        value = self.diagram("architecture", revision,
                             source={"path": "large.txt", "line": 1})
        error = self.failure_code(value, "architecture", root,
                                  "repository-evidence/git-unavailable")
        self.assertIn("16 MiB", str(error))

    def test_repository_and_source_presence_are_reported_consistently(self):
        root, revision = self.make_repository()
        self.assertIsNone(verify_repository({"meta": {}, "components": [{"id": "users"}]},
                                            "architecture", root))

        source_only = {"meta": {}, "components": [{"id": "users",
                                                     "sources": [{"path": "src/source #1.py"}]}]}
        self.failure_code(source_only, "architecture", root,
                          "repository-evidence/repository-required")

        metadata_only = {
            "meta": {"repository": {"url": "https://github.com/example/sourcerepo",
                                     "revision": revision}},
            "components": [{"id": "users"}],
        }
        self.failure_code(metadata_only, "architecture", root,
                          "repository-evidence/source-required")


if __name__ == "__main__":
    unittest.main()
