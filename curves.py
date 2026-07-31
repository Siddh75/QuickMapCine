"""Parametric camera paths. Pure math, no QGIS import, so it's testable standalone.

Each curve function takes t in [0, 1] and returns an (dx, dy, dz) offset from
a focus point, in scene units.
"""
import math


def helix(t, radius=100.0, height=100.0, turns=3.0):
    angle = 2 * math.pi * turns * t
    return (radius * math.cos(angle), radius * math.sin(angle), height * (t - 0.5))


def lissajous(t, radius_x=100.0, radius_y=100.0, freq_x=3.0, freq_y=2.0, height=50.0):
    angle = 2 * math.pi * t
    phase = math.pi / 2
    return (
        radius_x * math.sin(freq_x * angle + phase),
        radius_y * math.sin(freq_y * angle),
        height * math.sin(angle),
    )


def torus_knot(t, p=2, q=3, radius=100.0, tube_radius=30.0, height_scale=1.0):
    angle = 2 * math.pi * t
    r = radius + tube_radius * math.cos(q * angle)
    return (
        r * math.cos(p * angle),
        r * math.sin(p * angle),
        tube_radius * math.sin(q * angle) * height_scale,
    )


def trefoil_knot(t, scale=50.0, height_scale=1.0):
    angle = 2 * math.pi * t
    return (
        scale * (math.sin(angle) + 2 * math.sin(2 * angle)),
        scale * (math.cos(angle) - 2 * math.cos(2 * angle)),
        scale * height_scale * -math.sin(3 * angle),
    )


def fly_through(t, radius=100.0, height=100.0):
    """Straight line from one side of the focus point to the other, passing near
    it -- a swoop-past rather than an orbit. Still looks at the focus point
    throughout (same setLookingAtPoint() mechanism as every other curve), not a
    forward-facing flight path.
    """
    return (radius * (1 - 2 * t), 0.0, height * (0.5 - t))


# name -> (function, [(kwarg, default, role), ...]) drives the dock widget's generic
# spinbox form without hardcoding a UI per curve. role is "radius" (horizontal size),
# "height" (vertical size), or None (turns/frequencies/ratios -- not a scene-unit size,
# left alone when auto-fitting to a point cloud's extent).
CURVES = {
    "Helix": (helix, [("radius", 100.0, "radius"), ("height", 100.0, "height"), ("turns", 3.0, None)]),
    "Lissajous": (
        lissajous,
        [
            ("radius_x", 100.0, "radius"),
            ("radius_y", 100.0, "radius"),
            ("freq_x", 3.0, None),
            ("freq_y", 2.0, None),
            ("height", 50.0, "height"),
        ],
    ),
    "Torus Knot": (
        torus_knot,
        [
            ("p", 2.0, None),
            ("q", 3.0, None),
            ("radius", 100.0, "radius"),
            ("tube_radius", 30.0, None),
            ("height_scale", 1.0, None),
        ],
    ),
    "Trefoil Knot": (trefoil_knot, [("scale", 50.0, "radius"), ("height_scale", 1.0, None)]),
    "Fly Through": (fly_through, [("radius", 100.0, "radius"), ("height", 100.0, "height")]),
}


def _check(condition, message):
    """assert-alike that isn't actually `assert` -- assert statements are
    stripped under `python -O`, which would silently disable this self-test
    rather than fail loudly (also flagged by the QGIS plugin repo's Bandit
    scan, B101, for the same reason)."""
    if not condition:
        raise AssertionError(message)


def demo():
    for name, (fn, params) in CURVES.items():
        kwargs = {p[0]: p[1] for p in params}
        pts = [fn(t / 20, **kwargs) for t in range(21)]
        for p in pts:
            _check(len(p) == 3, f"{name}: point isn't a 3-tuple: {p}")
            _check(all(math.isfinite(v) for v in p), f"{name}: non-finite value in {p}")
        if name == "Helix":
            # not closed: z ramps monotonically instead of repeating
            _check(pts[-1][2] > pts[0][2], f"{name}: z should ramp up, start={pts[0]} end={pts[-1]}")
        elif name == "Fly Through":
            # not closed: a straight line, start and end are opposite ends
            _check(pts[0][0] > 0 > pts[-1][0], f"{name}: expected opposite ends, start={pts[0]} end={pts[-1]}")
        else:
            # closed loop: start and end offsets should coincide
            start, end = pts[0], pts[-1]
            _check(all(abs(a - b) < 1e-6 for a, b in zip(start, end)), f"{name}: not closed, start={start} end={end}")
    print("curves ok")


if __name__ == "__main__":
    demo()
