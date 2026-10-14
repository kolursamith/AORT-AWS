"""Fail-closed author/path admission check. Execute this copy from the PR base branch."""

import argparse
import json
from pathlib import PurePosixPath

OWNERS = {
    "kolursamith": ("intelligence/", "results/"),
    "girinandanv9845-ship-it": ("backend/", "dashboard/", "infra/"),
}


def normalize(path: str) -> str:
    normalized = str(PurePosixPath(path))
    if normalized.startswith("../") or normalized.startswith("/"):
        raise ValueError(f"unsafe changed path: {path!r}")
    return normalized


def changed_paths(records: list[dict]) -> set[str]:
    paths: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or "filename" not in record or "status" not in record:
            raise ValueError("malformed GitHub changed-file record")
        paths.add(normalize(record["filename"]))
        if record["status"] == "renamed":
            previous = record.get("previous_filename")
            if not previous:
                raise ValueError("renamed file is missing previous_filename")
            paths.add(normalize(previous))
    return paths


def violations(author: str, paths: set[str]) -> list[str]:
    strict = {owner: prefixes for owner, prefixes in OWNERS.items()}
    protected = [
        (owner, path)
        for owner, prefixes in strict.items()
        for path in paths
        if path.startswith(prefixes)
    ]
    if author not in strict:
        return [
            f"unrecognized author {author!r} cannot change strict path {path}"
            for _, path in protected
        ]
    return [
        f"{author} cannot change {owner}-owned path {path}"
        for owner, path in protected
        if owner != author
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--author", required=True)
    parser.add_argument("--files", required=True)
    args = parser.parse_args()
    records = json.loads(open(args.files, encoding="utf-8").read())
    failures = violations(args.author, changed_paths(records))
    if failures:
        raise SystemExit("\n".join(f"ERROR: {failure}" for failure in failures))
    print("ownership admission passed")


if __name__ == "__main__":
    main()
