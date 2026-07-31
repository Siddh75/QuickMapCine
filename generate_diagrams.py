"""Generates a labelled diagram per curve type, plotting the *actual* curves.py
math (not a hand-drawn approximation) with dashed annotation lines for the
size/shape parameters (radius, height, scale, ...) and a text legend for the
parameters that don't map to a physical distance (turns, freq_x/y, p/q,
height_scale).

Run once to (re)generate diagrams/*.png. Safe to re-run after tweaking a curve
in curves.py or the annotation logic below -- output is fully deterministic.
"""
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from curves import helix, lissajous, torus_knot, trefoil_knot, fly_through

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagrams")
os.makedirs(OUT_DIR, exist_ok=True)

CURVE_COLOR = "#1f6feb"
DASH_COLOR = "#d1242f"
FOCUS_COLOR = "#57606a"


def _new_ax():
    fig = plt.figure(figsize=(6, 5), dpi=150)
    ax = fig.add_subplot(111, projection="3d")
    try:
        ax.set_box_aspect((1, 1, 1))
    except AttributeError:
        pass  # older matplotlib without set_box_aspect
    ax.set_axis_off()
    return fig, ax


def _plot_curve(ax, fn, kwargs, n=400):
    ts = np.linspace(0, 1, n)
    pts = np.array([fn(t, **kwargs) for t in ts])
    ax.plot(pts[:, 0], pts[:, 1], pts[:, 2], color=CURVE_COLOR, linewidth=2.5)
    return pts


def _mark_focus(ax, offset=(0, 0, 0)):
    ax.scatter([0], [0], [0], color=FOCUS_COLOR, s=45, marker="x")
    ax.text(offset[0], offset[1], offset[2], "focus point", color=FOCUS_COLOR, fontsize=9)


def _dashed(ax, p0, p1, label, offset=(0, 0, 0), at=0.85):
    """at: how far along p0->p1 the label sits (0.85 -> near the far end, away
    from a focus point that most of these lines originate at)."""
    ax.plot([p0[0], p1[0]], [p0[1], p1[1]], [p0[2], p1[2]],
             linestyle="--", color=DASH_COLOR, linewidth=1.4)
    pos = (
        p0[0] + (p1[0] - p0[0]) * at + offset[0],
        p0[1] + (p1[1] - p0[1]) * at + offset[1],
        p0[2] + (p1[2] - p0[2]) * at + offset[2],
    )
    ax.text(*pos, label, color=DASH_COLOR, fontsize=9.5, weight="bold")


def _legend(fig, title, lines):
    text = title + "\n" + "\n".join(f"• {l}" for l in lines)
    fig.text(0.03, 0.03, text, fontsize=8.5, va="bottom", family="sans-serif")


def _save(fig, name):
    path = os.path.join(OUT_DIR, f"{name}.png")
    # bbox_inches="tight" alone under-crops 3D text (its screen position is only
    # known at render time, not from the data extent) -- pad_inches gives it
    # slack so labels near the plot edge (e.g. trefoil's height_scale) don't
    # get clipped by the crop.
    fig.savefig(path, transparent=True, bbox_inches="tight", pad_inches=0.35)
    plt.close(fig)
    print("wrote", path)


def make_helix():
    kwargs = dict(radius=100.0, height=100.0, turns=3.0)
    fig, ax = _new_ax()
    pts = _plot_curve(ax, helix, kwargs)
    _mark_focus(ax, offset=(-25, -15, 0))
    z0 = pts[0][2]
    _dashed(ax, (0, 0, z0), (pts[0][0], pts[0][1], z0), "radius")
    x_edge = pts[0][0]
    _dashed(ax, (x_edge, pts[0][1], pts[:, 2].min()), (x_edge, pts[0][1], pts[:, 2].max()),
            "height", offset=(15, 0, 0))
    ax.view_init(elev=16, azim=-55)
    _legend(fig, "Helix", ["radius: horizontal distance from the focus point",
                            "height: total vertical travel, bottom to top",
                            "turns: number of loops around the focus point"])
    _save(fig, "helix")


def make_lissajous():
    kwargs = dict(radius_x=100.0, radius_y=100.0, freq_x=3.0, freq_y=2.0, height=50.0)
    fig, ax = _new_ax()
    _plot_curve(ax, lissajous, kwargs)
    _mark_focus(ax, offset=(-30, -25, -20))
    _dashed(ax, (0, 0, 0), (kwargs["radius_x"], 0, 0), "radius_x", at=1.0, offset=(5, 0, 0))
    _dashed(ax, (0, 0, 0), (0, kwargs["radius_y"], 0), "radius_y", at=1.0, offset=(0, 8, 0))
    _dashed(ax, (0, 0, -kwargs["height"]), (0, 0, kwargs["height"]), "height", at=1.0, offset=(12, 0, 0))
    ax.view_init(elev=22, azim=-48)
    _legend(fig, "Lissajous", ["radius_x / radius_y: max horizontal swing on each axis",
                                "height: max vertical swing",
                                "freq_x / freq_y: how many loops per axis -- their",
                                "  ratio sets the figure's pattern (3:2 shown here)"])
    _save(fig, "lissajous")


def make_torus_knot():
    kwargs = dict(p=2, q=3, radius=100.0, tube_radius=30.0, height_scale=1.0)
    fig, ax = _new_ax()
    _plot_curve(ax, torus_knot, kwargs)
    # faint reference circle at the knot's center radius
    ring_t = np.linspace(0, 1, 200)
    ring = np.array([(kwargs["radius"] * math.cos(2 * math.pi * t),
                       kwargs["radius"] * math.sin(2 * math.pi * t), 0) for t in ring_t])
    ax.plot(ring[:, 0], ring[:, 1], ring[:, 2], linestyle=":", color=FOCUS_COLOR, linewidth=1.0, alpha=0.6)
    _mark_focus(ax, offset=(-30, -20, -15))
    _dashed(ax, (0, 0, 0), (kwargs["radius"], 0, 0), "radius", at=0.5, offset=(0, -12, 0))
    _dashed(ax, (kwargs["radius"], 0, 0), (kwargs["radius"] + kwargs["tube_radius"], 0, 0),
            "tube_radius", at=1.0, offset=(5, 8, 0))
    ax.view_init(elev=28, azim=-50)
    _legend(fig, "Torus Knot", ["radius: distance from focus to the center of the tube",
                                 "tube_radius: how far the path wobbles off that ring",
                                 "p, q: winding counts -- how many times the path",
                                 "  loops each way before it closes (2, 3 shown here)"])
    _save(fig, "torus_knot")


def make_trefoil_knot():
    kwargs = dict(scale=50.0, height_scale=1.0)
    fig, ax = _new_ax()
    pts = _plot_curve(ax, trefoil_knot, kwargs)
    _mark_focus(ax, offset=(-25, -20, -15))
    dists = np.hypot(pts[:, 0], pts[:, 1])
    far_idx = int(np.argmax(dists))
    far = pts[far_idx]
    _dashed(ax, (0, 0, far[2]), (far[0], far[1], far[2]), "scale", at=0.5, offset=(0, -12, 0))
    z_lo, z_hi = pts[:, 2].min(), pts[:, 2].max()
    _dashed(ax, (far[0], far[1], z_lo), (far[0], far[1], z_hi), "height_scale", at=1.0, offset=(8, 0, 0))
    ax.view_init(elev=55, azim=-60)
    _legend(fig, "Trefoil Knot", ["scale: overall size of the three-lobed loop",
                                   "height_scale: how much the path rises/dips",
                                   "  out of the horizontal plane per lobe"])
    _save(fig, "trefoil_knot")


def make_fly_through():
    kwargs = dict(radius=100.0, height=100.0)
    fig, ax = _new_ax()
    pts = _plot_curve(ax, fly_through, kwargs)
    _mark_focus(ax, offset=(-25, 0, -15))  # y=0: see the radius-label note below, same clipping issue
    # fly_through's y is 0 for every t, so mplot3d autoscales the y-axis to a
    # near-zero range -- any label offset with a nonzero y component lands
    # outside that razor-thin view volume and Axes3D silently clips it
    # (invisible, not just mis-positioned). Offset in x/z only here.
    _dashed(ax, (0, pts[0][1], pts[0][2]), (pts[0][0], pts[0][1], pts[0][2]), "radius", at=0.3, offset=(0, 0, 12))
    _dashed(ax, (pts[0][0], pts[0][1], 0), (pts[0][0], pts[0][1], pts[0][2]), "height", at=1.0, offset=(15, 0, 0))
    ax.view_init(elev=14, azim=-60)
    _legend(fig, "Fly Through", ["radius: half the total length of the pass -- the",
                                  "  camera starts and ends this far out sideways",
                                  "height: half the total vertical drop across the pass"])
    _save(fig, "fly_through")


if __name__ == "__main__":
    make_helix()
    make_lissajous()
    make_torus_knot()
    make_trefoil_knot()
    make_fly_through()
