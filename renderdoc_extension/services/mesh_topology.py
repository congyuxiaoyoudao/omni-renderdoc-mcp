"""Convert indexed GPU primitives to FBX faces, honoring restart cuts."""

from itertools import groupby

from .mesh_formats import MeshError


def _segments(indices):
    return (
        list(items)
        for keep, items in groupby(indices, lambda value: value is not None)
        if keep
    )


def _faces(topology, segment):
    n = len(segment)
    list_widths = {"PointList": 1, "LineList": 2, "TriangleList": 3}
    if topology in list_widths:
        width = list_widths[topology]
        for i in range(0, n - width + 1, width):
            yield tuple(segment[i : i + width])
    elif topology in ("LineStrip", "LineLoop", "LineStrip_Adj"):
        if topology == "LineStrip_Adj":
            segment = segment[1:-1]
        yield from zip(segment, segment[1:])
        if topology == "LineLoop" and n > 1:
            yield segment[-1], segment[0]
    elif topology in ("TriangleStrip", "TriangleStrip_Adj"):
        if topology == "TriangleStrip_Adj":
            segment = segment[::2] if n % 2 == 0 else segment[:-1:2]
        for i in range(len(segment) - 2):
            a, b, c = segment[i : i + 3]
            yield (b, a, c) if i % 2 else (a, b, c)
    elif topology == "TriangleFan":
        for i in range(1, n - 1):
            yield segment[0], segment[i], segment[i + 1]
    elif topology in ("LineList_Adj", "TriangleList_Adj"):
        width = 4 if topology == "LineList_Adj" else 6
        for i in range(0, n - width + 1, width):
            group = segment[i : i + width]
            yield tuple(group[1:3] if width == 4 else group[::2])
    else:
        raise MeshError("No FBX primitive mapping for " + topology)


def faces(topology, indices):
    return [
        face
        for segment in _segments(indices)
        for face in _faces(topology, segment)
    ]


def compact(values):
    """Return source IDs in their first-use order and original-to-local map."""
    remap = {}
    ordered = []
    for value in values:
        if value is not None and value not in remap:
            remap[value] = len(ordered)
            ordered.append(value)
    return ordered, remap
