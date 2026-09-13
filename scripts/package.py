"""Build an upload-ready source archive without local credentials, test data, or dependencies."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

root = Path(__file__).resolve().parents[1]
destination = root / "dist" / "pera-panel-0.1.0.zip"
destination.parent.mkdir(exist_ok=True)
include = [root / name for name in ("install.sh", "README.md", "LICENSE", "requirements.txt",
                                    "requirements-dev.txt", "pyproject.toml", ".gitignore", ".gitattributes")]
for folder in ("pera_panel", "deploy", "tests", ".github", "scripts"):
    include.extend(path for path in (root / folder).rglob("*")
                   if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
    for path in sorted(include):
        entry = ZipInfo("pera-panel/" + path.relative_to(root).as_posix())
        entry.create_system = 3
        entry.compress_type = ZIP_DEFLATED
        entry.external_attr = (0o100755 if path.name == "install.sh" else 0o100644) << 16
        archive.writestr(entry, path.read_bytes())
print(destination)
