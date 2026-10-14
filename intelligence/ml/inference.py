"""Explicit unsupported boundary until measured models are trained and registered."""


class ModelUnavailableError(RuntimeError):
    pass


def predict_capacity(_: dict[str, float]) -> float:
    raise ModelUnavailableError(
        "capacity model is not trained; use the documented fallback controller"
    )
