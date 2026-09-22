__all__ = ["app"]

# Kept equal to project.version in pyproject.toml (flet build stamps the bundle from
# that one); tests/test_version.py fails when the two drift apart.
__version__ = "1.0.0"
