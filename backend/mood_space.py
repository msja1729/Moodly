"""Markov chain over a discretised valence x energy mood space.

The unit square [0,1]^2 is split into an N x N grid; each cell is a state.
From a cell the chain may only step to an adjacent cell (Chebyshev radius 1)
whose centre is strictly closer to the target point, so every step is a
small, monotone move toward the target and the target cell is absorbing.

Among the allowed moves:

    P[i -> j]  ∝  exp(-beta * d(c_j, target))      pull toward the target
                * exp(-gamma * d(c_i, c_j)^2)      penalty for big jumps
                * w_skip(direction(i -> j))        learned from skips

w_skip is the posterior mean completion rate of bridging tracks queued in that
direction, Beta(1 + completions, 1 + skips), scaled so an unseen direction
has weight 1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

import numpy as np

Point = tuple[float, float]  # (valence, energy)
Cell = tuple[int, int]  # (valence index, energy index)
Direction = tuple[int, int]  # (dv, de), each in {-1, 0, 1}

DIRECTIONS: tuple[Direction, ...] = tuple(
    (dv, de) for dv in (-1, 0, 1) for de in (-1, 0, 1) if (dv, de) != (0, 0)
)


@dataclass(frozen=True)
class MarkovParams:
    grid_size: int = 10
    beta: float = 6.0
    gamma: float = 20.0

    @classmethod
    def from_config(cls, cfg: Mapping) -> "MarkovParams":
        m = cfg.get("markov", {})
        return cls(
            grid_size=int(m.get("grid_size", cls.grid_size)),
            beta=float(m.get("beta", cls.beta)),
            gamma=float(m.get("gamma", cls.gamma)),
        )


def distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def clamp_point(p: Point) -> Point:
    return (min(max(p[0], 0.0), 1.0), min(max(p[1], 0.0), 1.0))


def point_to_cell(p: Point, n: int) -> Cell:
    v, e = clamp_point(p)
    return (min(int(v * n), n - 1), min(int(e * n), n - 1))


def cell_center(c: Cell, n: int) -> Point:
    return ((c[0] + 0.5) / n, (c[1] + 0.5) / n)


def cell_index(c: Cell, n: int) -> int:
    return c[1] * n + c[0]


def index_cell(i: int, n: int) -> Cell:
    return (i % n, i // n)


def skip_weight(stats: Mapping[Direction, tuple[int, int]] | None, d: Direction) -> float:
    """Beta(1+completions, 1+skips) mean, normalised so no data -> 1.0."""
    if not stats or d not in stats:
        return 1.0
    completions, skips = stats[d]
    return 2.0 * (1 + completions) / (2 + completions + skips)


def build_transition_matrix(
    target: Point,
    params: MarkovParams = MarkovParams(),
    skip_stats: Mapping[Direction, tuple[int, int]] | None = None,
) -> np.ndarray:
    n = params.grid_size
    target = clamp_point(target)
    target_cell = point_to_cell(target, n)
    size = n * n
    P = np.zeros((size, size))

    for i in range(size):
        ci = index_cell(i, n)
        if ci == target_cell:
            P[i, i] = 1.0
            continue
        center_i = cell_center(ci, n)
        d_i = distance(center_i, target)
        for d in DIRECTIONS:
            cj = (ci[0] + d[0], ci[1] + d[1])
            if not (0 <= cj[0] < n and 0 <= cj[1] < n):
                continue
            # The target cell counts as distance 0 because we land on the point itself.
            center_j = target if cj == target_cell else cell_center(cj, n)
            d_j = distance(center_j, target)
            if d_j >= d_i:
                continue
            P[i, cell_index(cj, n)] = (
                math.exp(-params.beta * d_j)
                * math.exp(-params.gamma * distance(center_i, center_j) ** 2)
                * skip_weight(skip_stats, d)
            )
        row_sum = P[i].sum()
        if row_sum == 0:  # cannot happen on a grid, but never leave a dead state
            P[i, i] = 1.0
        else:
            P[i] /= row_sum
    return P


class MoodChain:
    """A transition matrix bound to one target, with sampling helpers."""

    def __init__(
        self,
        target: Point,
        params: MarkovParams = MarkovParams(),
        skip_stats: Mapping[Direction, tuple[int, int]] | None = None,
    ):
        self.target = clamp_point(target)
        self.params = params
        self.n = params.grid_size
        self.target_cell = point_to_cell(self.target, self.n)
        self.P = build_transition_matrix(self.target, params, skip_stats)

    def waypoint_of(self, cell: Cell) -> Point:
        return self.target if cell == self.target_cell else cell_center(cell, self.n)

    def transition_probs(self, current: Point) -> dict[Cell, float]:
        i = cell_index(point_to_cell(current, self.n), self.n)
        row = self.P[i]
        return {index_cell(j, self.n): float(row[j]) for j in np.nonzero(row)[0]}

    def next_cell(self, current: Point, rng: np.random.Generator) -> Cell:
        i = cell_index(point_to_cell(current, self.n), self.n)
        j = int(rng.choice(self.P.shape[0], p=self.P[i]))
        return index_cell(j, self.n)

    def next_waypoint(self, current: Point, rng: np.random.Generator) -> tuple[Point, Direction]:
        """Sample the next waypoint and the grid direction taken to reach it."""
        here = point_to_cell(current, self.n)
        there = self.next_cell(current, rng)
        direction = (there[0] - here[0], there[1] - here[1])
        return self.waypoint_of(there), direction

    def expected_steps_to_target(self) -> np.ndarray:
        """Expected steps to absorption from every cell (fundamental matrix)."""
        size = self.P.shape[0]
        t = cell_index(self.target_cell, self.n)
        transient = [i for i in range(size) if i != t]
        Q = self.P[np.ix_(transient, transient)]
        N = np.linalg.inv(np.eye(len(transient)) - Q)
        steps = np.zeros(size)
        steps[transient] = N.sum(axis=1)
        return steps

    def expected_steps_from(self, current: Point) -> float:
        return float(self.expected_steps_to_target()[cell_index(point_to_cell(current, self.n), self.n)])

    def most_likely_path(self, current: Point, max_steps: int = 50) -> list[Point]:
        """Greedy path through the chain, for display/debugging."""
        cell = point_to_cell(current, self.n)
        path = [clamp_point(current)]
        for _ in range(max_steps):
            if cell == self.target_cell:
                break
            row = self.P[cell_index(cell, self.n)]
            cell = index_cell(int(np.argmax(row)), self.n)
            path.append(self.waypoint_of(cell))
        return path
