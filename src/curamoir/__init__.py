"""cúramóir."""

#: The release version, kept equal to pyproject.toml's and
#: .claude-plugin/plugin.json's by a test. Stated here because the standalone
#: `curamoir-lint` runs from a bare checkout with nothing installed, so there
#: is no package metadata for `importlib.metadata` to read.
__version__ = "0.1.0"
