from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


FORBIDDEN_PARTS = {
    ".git",
    ".trellis",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}
FORBIDDEN_SUFFIXES = {".key", ".pem", ".pyc", ".pyo"}


def normalized_members(archive: Path) -> set[PurePosixPath]:
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as handle:
            names = handle.namelist()
        return {PurePosixPath(name) for name in names if not name.endswith("/")}
    with tarfile.open(archive, "r:gz") as handle:
        names = [member.name for member in handle.getmembers() if member.isfile()]
    paths = {PurePosixPath(name) for name in names}
    roots = {path.parts[0] for path in paths if path.parts}
    if len(roots) != 1:
        raise ValueError(f"{archive.name}: source archive must have one root")
    return {PurePosixPath(*path.parts[1:]) for path in paths}


def check_archive(archive: Path) -> None:
    members = normalized_members(archive)
    forbidden = sorted(
        str(path)
        for path in members
        if FORBIDDEN_PARTS.intersection(path.parts)
        or path.suffix.lower() in FORBIDDEN_SUFFIXES
        or path.name in {".env", ".DS_Store"}
    )
    if forbidden:
        raise ValueError(
            f"{archive.name}: forbidden archive members: {', '.join(forbidden)}"
        )
    if archive.suffix == ".whl":
        required = {
            PurePosixPath("remote_runner/__init__.py"),
            PurePosixPath("remote_runner/cli.py"),
            PurePosixPath("remote_runner/web_app.py"),
            PurePosixPath("remote_runner/web_static/index.html"),
        }
    else:
        required = {
            PurePosixPath("LICENSE"),
            PurePosixPath("README.md"),
            PurePosixPath("README.zh-CN.md"),
            PurePosixPath("pyproject.toml"),
            PurePosixPath("src/remote_runner/cli.py"),
            PurePosixPath("src/remote_runner/web_app.py"),
            PurePosixPath("src/remote_runner/web_static/index.html"),
            PurePosixPath("tests/test_cli_contract.py"),
            PurePosixPath("web/DESIGN.md"),
            PurePosixPath("web/package-lock.json"),
            PurePosixPath("web/package.json"),
        }
    missing = sorted(str(path) for path in required - members)
    if missing:
        raise ValueError(f"{archive.name}: missing required members: {', '.join(missing)}")
    web_assets = {
        path.suffix
        for path in members
        if "web_static" in path.parts and "assets" in path.parts
    }
    missing_asset_types = sorted({".css", ".js"} - web_assets)
    if missing_asset_types:
        raise ValueError(
            f"{archive.name}: missing web asset types: {', '.join(missing_asset_types)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate built distribution contents.")
    parser.add_argument("dist", type=Path, nargs="?", default=Path("dist"))
    args = parser.parse_args()
    archives = sorted((*args.dist.glob("*.whl"), *args.dist.glob("*.tar.gz")))
    if len(archives) != 2:
        parser.error(f"expected one wheel and one sdist in {args.dist}, found {len(archives)}")
    for archive in archives:
        check_archive(archive)
        print(f"validated {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
