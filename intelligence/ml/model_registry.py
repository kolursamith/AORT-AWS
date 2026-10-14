"""Filesystem model registry policy; loading untrusted pickle artifacts is prohibited."""

from pathlib import Path

ALLOWED_SUFFIXES = {".json", ".onnx"}


def validate_model_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.suffix not in ALLOWED_SUFFIXES:
        raise ValueError("only JSON or ONNX model artifacts are allowed")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved
