import importlib.util
from pathlib import Path


def load_policy():
    path = Path(".github/scripts/check_ownership.py")
    spec = importlib.util.spec_from_file_location("ownership", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_renames_check_old_and_new_paths() -> None:
    policy = load_policy()
    records = [
        {
            "filename": "backend/new.py",
            "previous_filename": "intelligence/old.py",
            "status": "renamed",
        }
    ]
    paths = policy.changed_paths(records)
    assert policy.violations("kolursamith", paths)
    assert policy.violations("girinandanv9845-ship-it", paths)


def test_unknown_author_fails_closed_for_strict_path() -> None:
    policy = load_policy()
    failures = policy.violations("outsider", {"backend/api.py"})
    assert failures
