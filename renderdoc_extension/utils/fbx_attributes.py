"""Lossless geometry properties shared by the ASCII FBX exporters."""

import base64
import json
import math


def _encode(data):
    payload = json.dumps(
        data, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )
    return base64.b64encode(payload.encode("ascii")).decode("ascii")


def attribute_properties(attributes, count):
    """Yield FBX user properties, with source rows chunked by control point."""
    yield "OmniAttributeCount", len(attributes)
    for index, attribute in enumerate(attributes):
        rows = attribute["values"]
        raw = attribute.get("raw")
        if len(rows) != count or (raw is not None and len(raw) != count):
            raise ValueError("Vertex attribute count differs from geometry")
        prefix = "OmniAttr{}".format(index)
        meta = {
            k: v for k, v in attribute.items() if k not in ("values", "raw")
        }
        meta["rows_per_chunk"] = 128
        yield prefix, _encode(meta)
        for start in range(0, count, 128):
            chunk = rows[start : start + 128]
            data = {
                "values": [
                    [v if math.isfinite(v) else str(v) for v in row]
                    for row in chunk
                ]
            }
            if raw is not None:
                data["raw"] = raw[start : start + 128]
            yield "{}_{}".format(prefix, start // 128), _encode(data)
