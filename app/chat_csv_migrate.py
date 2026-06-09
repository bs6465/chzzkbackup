from __future__ import annotations

import argparse
import csv
from pathlib import Path

from . import config

STANDARD_CSV_FIELDS = ["type", "timestamp", "offset_seconds", "nickname", "content"]


def convert_chat_csv(path: Path) -> dict[str, int | bool]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    needs_conversion = fieldnames != STANDARD_CSV_FIELDS
    if not needs_conversion:
        return {"converted": False, "rows": len(rows)}

    temp_path = path.with_name(f"{path.name}.convert.tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STANDARD_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)
    return {"converted": True, "rows": len(rows)}


def discover_chat_csv_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() == ".csv" else []
    return sorted(path for path in root.rglob("*.csv") if path.is_file())


def convert_chat_csv_tree(root: Path) -> dict[str, int]:
    result = {"files": 0, "converted": 0, "rows": 0}
    for path in discover_chat_csv_files(root):
        converted = convert_chat_csv(path)
        result["files"] += 1
        result["rows"] += int(converted["rows"])
        if converted["converted"]:
            result["converted"] += 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert existing chat CSV files to the standard columns.")
    parser.add_argument("root", type=Path, nargs="?", default=config.FINAL_ROOT)
    args = parser.parse_args()

    result = convert_chat_csv_tree(args.root)
    print(result)


if __name__ == "__main__":
    main()
