from __future__ import annotations

import shutil
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
TARGET_DIRS = [
    BASE_DIR / "runs",
    BASE_DIR / "uploads",
    BASE_DIR / "tmp",
    BASE_DIR / "latest_export",
    BASE_DIR / "__pycache__",
]


def clear_directory_contents(path: Path) -> tuple[int, int]:
    deleted_files = 0
    deleted_dirs = 0
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return deleted_files, deleted_dirs
    for child in path.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child)
                deleted_dirs += 1
            else:
                child.unlink(missing_ok=True)
                deleted_files += 1
        except Exception as exc:
            print(f"Skipped {child}: {exc}")
    path.mkdir(parents=True, exist_ok=True)
    return deleted_files, deleted_dirs


def main() -> None:
    print("Clearing bot history and saved run data...")
    total_files = 0
    total_dirs = 0
    for target in TARGET_DIRS:
        files, dirs = clear_directory_contents(target)
        total_files += files
        total_dirs += dirs
        print(f"- {target.name}: removed {files} files and {dirs} folders")
    print("")
    print(f"Done. Removed {total_files} files and {total_dirs} folders.")
    print("Code files were not touched.")


if __name__ == "__main__":
    main()
