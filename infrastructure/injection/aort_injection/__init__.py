"""Controlled failure injection for the AORT banking stack.

Applies a reversible, allow-listed disturbance to one component, holds it for a
fixed window, restores it, and records exactly what was done and when.

Those records are the ground truth that makes the telemetry interpretable: a
metric moving is only evidence once you know a failure was injected at a known
moment into a known component.

Nothing here destroys data or containers, and every injection is reverted -
including when the run is interrupted.
"""

__version__ = "0.1.0"
