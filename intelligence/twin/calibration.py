"""Calibration interface reserved for measured twin-versus-system error."""


def absolute_error(predicted: float, observed: float) -> float:
    return abs(predicted - observed)
