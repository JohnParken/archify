"""Setuptools hooks for packaging canonical Skill runtime resources."""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    """Copy canonical resources into the installed Python package."""

    def run(self):
        super().run()
        if not self.packages or getattr(self, "editable_mode", False):
            return
        repository = Path(__file__).resolve().parent
        package_build_dir = Path(self.build_lib) / "archify_runtime" / "resources"
        sources = [
            *[(repository / "archify" / "schemas" / f"{name}.schema.json", Path("schemas") / f"{name}.schema.json")
              for name in ("common", "sequence", "dataflow", "lifecycle", "architecture", "workflow")],
            (repository / "archify" / "assets" / "template.html", Path("assets") / "template.html"),
            (repository / "archify" / "LICENSE", Path("LICENSE")),
            (repository / "archify" / "THIRD_PARTY_NOTICES.md", Path("THIRD_PARTY_NOTICES.md")),
        ]
        self._archify_resource_outputs = []
        for source, relative in sources:
            target = package_build_dir / relative
            self.mkpath(str(target.parent))
            self.copy_file(str(source), str(target))
            self._archify_resource_outputs.append(str(target))

    def get_outputs(self, include_bytecode=True):
        outputs = super().get_outputs(include_bytecode)
        return outputs + getattr(self, "_archify_resource_outputs", [])


setup(cmdclass={"build_py": BuildPy})
