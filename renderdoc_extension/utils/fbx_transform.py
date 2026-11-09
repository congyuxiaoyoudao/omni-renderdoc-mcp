"""Affine transforms in FBX array order, with translation at indices 12..14."""

import math

IDENTITY = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)


def affine(values):
    if len(values) != 16 or not all(math.isfinite(x) for x in values):
        raise ValueError("Expected a finite 4x4 bind matrix")
    if any(abs(a - b) > 1e-7 for a, b in zip(values[3::4], (0, 0, 0, 1))):
        raise ValueError("Bind matrices must be affine")
    return values


def multiply(left, right):
    return [
        sum(left[4 * row + k] * right[k * 4 + col] for k in range(4))
        for row in range(4)
        for col in range(4)
    ]


def inverse(matrix):
    rows = [
        list(matrix[i : i + 4]) + list(IDENTITY[i : i + 4])
        for i in range(0, 16, 4)
    ]
    for col in range(4):
        pivot = max(range(col, 4), key=lambda i: abs(rows[i][col]))
        if not rows[pivot][col]:
            raise ValueError("Bind matrix is singular")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        divisor = rows[col][col]
        rows[col] = [v / divisor for v in rows[col]]
        for other in range(4):
            if col != other:
                factor = rows[other][col]
                rows[other] = [
                    a - factor * b for a, b in zip(rows[other], rows[col])
                ]
    return [v for row in rows for v in row[4:]]


def decompose(matrix):
    """Return local translation, Euler XYZ degrees and signed scale."""
    basis = [matrix[i : i + 3] for i in (0, 4, 8)]
    scales = [math.sqrt(sum(v * v for v in row)) for row in basis]
    if not all(scales):
        raise ValueError("A zero scale cannot define a bind transform")
    x, y, z = [[v / s for v in row] for row, s in zip(basis, scales)]
    for first, second in ((x, y), (y, z), (z, x)):
        if abs(sum(a * b for a, b in zip(first, second))) > 1e-5:
            raise ValueError("A sheared local bind transform is unsupported")
    cross = [
        y[(i + 1) % 3] * z[(i + 2) % 3] - y[(i + 2) % 3] * z[(i + 1) % 3]
        for i in range(3)
    ]
    if sum(a * b for a, b in zip(x, cross)) < 0:
        scales[0] = -scales[0]
        x = [-v for v in x]
    pitch = math.atan2(-x[2], math.hypot(x[0], x[1]))
    if math.hypot(x[0], x[1]) > 1e-8:
        roll = math.atan2(y[2], z[2])
        yaw = math.atan2(x[1], x[0])
    else:
        roll = math.atan2(-z[1], y[1])
        yaw = 0
    angles = [math.degrees(a) for a in (roll, pitch, yaw)]
    return matrix[12:15], angles, scales
