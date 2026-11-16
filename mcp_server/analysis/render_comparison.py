"""Compare explicitly decoded render outputs without image transformations.

Files contain tightly packed, little-endian components. Dimensions, encoding,
row reversal and tolerances are caller decisions. No automatic exposure,
registration, resizing, color conversion or decoding of packed GPU formats is
performed. Dependency localization is an observation under the supplied
graph, never proof of a causal error or of correct behavior after scene edits.
"""

import hashlib
import json
import math
import struct
import uuid
from pathlib import Path

MAX_LAYERS = 128
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_BATCH_BYTES = 1024 * 1024 * 1024
MAX_COMPONENTS = 32 * 1024 * 1024
MAX_DIMENSION = 16384
ENCODINGS = {
    "float32le": ("f", 4, None),
    "float16le": ("e", 2, None),
    "unorm8": ("B", 1, 255),
    "unorm16": ("H", 2, 65535),
    "snorm8": ("b", 1, 127),
    "snorm16": ("h", 2, 32767),
}


def _integer(value, name, minimum=1, maximum=MAX_DIMENSION):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be an integer in [{minimum}, {maximum}]"
        )
    return value


def _descriptor(value):
    if not isinstance(value, dict):
        raise ValueError("Each output needs a file descriptor object")
    path = value.get("path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError("Output paths must be absolute file paths")
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f"Output file does not exist: {path}")
    width = _integer(value.get("width"), "width")
    height = _integer(value.get("height"), "height")
    channels = _integer(value.get("channels"), "channels", maximum=4)
    encoding = value.get("encoding")
    if not isinstance(encoding, str) or encoding not in ENCODINGS:
        raise ValueError(
            "Unsupported encoding; decode native packed/compressed formats "
            "explicitly first. Supported: " + ", ".join(ENCODINGS)
        )
    if type(value.get("flip_y", False)) is not bool:
        raise ValueError("flip_y must be an explicit Boolean")
    components = width * height * channels
    expected = components * ENCODINGS[encoding][1]
    if components > MAX_COMPONENTS or expected > MAX_FILE_BYTES:
        raise ValueError("Output exceeds the bounded comparison size")
    if path.stat().st_size != expected:
        raise ValueError(
            f"File size does not match tightly packed dimensions: {path}"
        )
    digest = value.get("sha256")
    if digest is not None and (
        not isinstance(digest, str) or len(digest) != 64
        or any(c not in "0123456789abcdefABCDEF" for c in digest)
    ):
        raise ValueError("sha256 must be a 64-character hexadecimal digest")
    return {
        "path": str(path), "width": width, "height": height,
        "channels": channels, "encoding": encoding,
        "flip_y": value.get("flip_y", False), "byte_length": expected,
        "expected_sha256": digest.lower() if digest else None,
        "color_space": value.get("color_space"),
    }


def _tolerance(value):
    if not isinstance(value, dict):
        raise ValueError("tolerance must be an object")
    result = {}
    for key, alias in (("absolute", "abs"), ("relative", "rel")):
        if key in value and alias in value and value[key] != value[alias]:
            raise ValueError(f"Conflicting tolerance {key}/{alias}")
        number = value.get(key, value.get(alias, 0))
        if type(number) not in (int, float) or not math.isfinite(number):
            raise ValueError("Tolerances must be finite nonnegative numbers")
        if number < 0:
            raise ValueError("Tolerances must be finite nonnegative numbers")
        result[key] = float(number)
    return result


def _prepare(layers):
    if not isinstance(layers, list) or not 1 <= len(layers) <= MAX_LAYERS:
        raise ValueError(f"Provide between 1 and {MAX_LAYERS} layers")
    result, names, total = [], set(), 0
    for layer in layers:
        if not isinstance(layer, dict):
            raise ValueError("Each layer must be an object")
        name = layer.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 256:
            raise ValueError("Every layer needs a nonempty name <= 256 chars")
        if name in names:
            raise ValueError(f"Duplicate layer name: {name}")
        names.add(name)
        sources = {
            side: _descriptor(layer.get(side))
            for side in ("reference", "rendered")
        }
        before = ("reference_before" in layer, "rendered_before" in layer)
        if any(before):
            if not all(before):
                raise ValueError("Contributions require both before outputs")
            for side in ("reference_before", "rendered_before"):
                sources[side] = _descriptor(layer[side])
        shape = tuple(sources["reference"][k]
                      for k in ("width", "height", "channels"))
        spaces = set()
        for source in sources.values():
            if tuple(source[k] for k in
                     ("width", "height", "channels")) != shape:
                raise ValueError("Dimensions/channels differ; no auto-resize")
            if source["color_space"] is not None:
                if not isinstance(source["color_space"], str):
                    raise ValueError("color_space must be a string")
                spaces.add(source["color_space"])
            total += source["byte_length"]
        if len(spaces) > 1:
            raise ValueError("Color spaces differ; no implicit conversion")
        width, height, _ = shape
        roi = layer.get("roi", [0, 0, width, height])
        if not isinstance(roi, list) or len(roi) != 4:
            raise ValueError("roi must be [x, y, width, height]")
        x, y = (_integer(roi[i], "roi origin", minimum=0) for i in (0, 1))
        w, h = (_integer(roi[i], "roi size") for i in (2, 3))
        if x + w > width or y + h > height:
            raise ValueError("roi must fit within the declared dimensions")
        dependencies = layer.get("depends_on", [])
        if (not isinstance(dependencies, list)
                or len(dependencies) > MAX_LAYERS
                or any(not isinstance(n, str) or not n or len(n) > 256
                       for n in dependencies)
                or len(set(dependencies)) != len(dependencies)):
            raise ValueError("depends_on must contain unique layer names")
        if type(layer.get("identical_inputs", False)) is not bool:
            raise ValueError("identical_inputs must be an explicit Boolean")
        result.append({
            "name": name, "sources": sources, "roi": roi,
            "tolerance": _tolerance(layer.get("tolerance", {})),
            "depends_on": dependencies,
            "identical_inputs": layer.get("identical_inputs", False),
        })
    if total > MAX_BATCH_BYTES:
        raise ValueError("Batch exceeds the bounded total input size")
    return result


class _Surface:
    def __init__(self, descriptor):
        self.info = dict(descriptor)
        # Hash the immutable bytes used below, not a separate file read.
        with open(descriptor["path"], "rb") as stream:
            self.data = stream.read(descriptor["byte_length"] + 1)
        if len(self.data) != descriptor["byte_length"]:
            raise ValueError("Output changed size during comparison")
        digest = hashlib.sha256(self.data).hexdigest()
        expected = self.info.pop("expected_sha256")
        if expected is not None and expected != digest:
            raise ValueError(f"Output SHA-256 changed: {descriptor['path']}")
        self.info["sha256"] = digest
        code, self.stride, self.divisor = ENCODINGS[descriptor["encoding"]]
        self.unpacker = struct.Struct(
            "<" + str(descriptor["width"] * descriptor["channels"]) + code
        )

    def row(self, y):
        if self.info["flip_y"]:
            y = self.info["height"] - 1 - y
        row = self.unpacker.unpack_from(self.data, y * self.unpacker.size)
        if self.divisor is None:
            return row
        if self.info["encoding"].startswith("snorm"):
            return tuple(max(-1.0, value / self.divisor) for value in row)
        return tuple(value / self.divisor for value in row)


def _compare(layer):
    sources = {key: _Surface(value)
               for key, value in layer["sources"].items()}
    x0, y0, width, height = layer["roi"]
    channels = sources["reference"].info["channels"]
    absolute, relative = (layer["tolerance"][k]
                          for k in ("absolute", "relative"))
    finite = nonfinite = over = changed = 0
    maximum = total = squares = 0.0
    bounds = worst = first_nonfinite = None
    contribution = "reference_before" in sources
    for y in range(y0, y0 + height):
        rows = {key: source.row(y) for key, source in sources.items()}
        for x in range(x0, x0 + width):
            pixel_changed = False
            for channel in range(channels):
                index = x * channels + channel
                values = [rows[key][index] for key in sources]
                valid = all(math.isfinite(value) for value in values)
                left, right = values[:2]
                if contribution and valid:
                    left -= rows["reference_before"][index]
                    right -= rows["rendered_before"][index]
                if not valid:
                    nonfinite += 1
                    pixel_changed = True
                    if first_nonfinite is None:
                        first_nonfinite = [x, y, channel]
                    continue
                error = abs(left - right)
                finite += 1
                total += error
                squares += error * error
                if worst is None or error > maximum:
                    maximum = error
                    worst = {
                        "position": [x, y, channel], "reference": left,
                        "rendered": right, "absolute_error": error,
                    }
                if error > absolute + relative * abs(left):
                    over += 1
                    pixel_changed = True
            if pixel_changed:
                changed += 1
                if bounds is None:
                    bounds = [x, y, x, y]
                else:
                    bounds = [min(bounds[0], x), min(bounds[1], y),
                              max(bounds[2], x), max(bounds[3], y)]
    if bounds is not None:
        bounds = [bounds[0], bounds[1], bounds[2] - bounds[0] + 1,
                  bounds[3] - bounds[1] + 1]
    return {
        "name": layer["name"],
        "status": "different" if over or nonfinite else "within_tolerance",
        "mode": "contribution" if contribution else "output",
        "sources": {key: value.info for key, value in sources.items()},
        "depends_on": layer["depends_on"],
        "identical_inputs": layer["identical_inputs"],
        "tolerance": layer["tolerance"],
        "metrics": {
            "roi": layer["roi"], "compared_pixels": width * height,
            "compared_components": width * height * channels,
            "finite_components": finite, "nonfinite_components": nonfinite,
            "components_over_tolerance": over, "changed_pixels": changed,
            "changed_pixel_fraction": changed / (width * height),
            "max_absolute_error": maximum if finite else None,
            "mean_absolute_error": total / finite if finite else None,
            "rmse": math.sqrt(squares / finite) if finite else None,
            "difference_bounds": bounds, "worst_component": worst,
            "first_nonfinite": first_nonfinite,
        },
    }


def _localize(layers):
    by_name = {layer["name"]: layer for layer in layers}
    visited, active, cycles = set(), [], set()

    def visit(name):
        if name not in by_name or name in visited:
            return
        if name in active:
            cycles.update(active[active.index(name):])
            return
        active.append(name)
        for parent in by_name[name]["depends_on"]:
            visit(parent)
        active.pop()
        visited.add(name)

    for name in by_name:
        visit(name)
    affected = set(cycles)
    while True:
        added = {name for name, layer in by_name.items()
                 if set(layer["depends_on"]) & affected} - affected
        if not added:
            break
        affected.update(added)
    frontier, downstream, unknown = [], [], []
    for name, layer in by_name.items():
        parents = layer["depends_on"]
        missing = [parent for parent in parents if parent not in by_name]
        if name in affected or missing:
            unknown.append({
                "name": name, "missing_dependencies": missing,
                "depends_on_cycle": name in affected,
            })
            continue
        if layer["status"] != "different":
            continue
        bad = [parent for parent in parents
               if by_name[parent]["status"] == "different"]
        if bad:
            downstream.append({"name": name, "differing_inputs": bad})
        else:
            partial = []
            for parent in parents:
                observed = by_name[parent]
                source = observed["sources"]["reference"]
                extent = [0, 0, source["width"], source["height"]]
                if (observed["mode"] != "output"
                        or observed["metrics"]["roi"] != extent):
                    partial.append(parent)
            if partial:
                unknown.append({
                    "name": name,
                    "incomplete_dependency_comparisons": partial,
                    "reason": "A matching ROI or effect contribution does "
                              "not establish a matching complete input.",
                })
                continue
            frontier.append({
                "name": name,
                "classification": (
                    "matching_input_boundary" if parents
                    else "declared_identical_inputs"
                    if layer["identical_inputs"]
                    else "input_or_root_boundary_unproven"
                ),
                "difference_bounds": layer["metrics"]["difference_bounds"],
            })
    return {
        "status": "unknown" if unknown else "observed",
        "frontier": frontier, "downstream": downstream, "unknown": unknown,
        "cycles": sorted(cycles), "causal_proof": False,
        "scope": "Observed outputs under caller-supplied dependencies; "
                 "matching ROI outputs do not prove complete inputs match.",
    }


def compare_render_outputs(layers, output_directory=None):
    """Compare 1..128 raw-output pairs and optionally save a unique report.

    Each layer needs name, reference/rendered descriptors, optional tolerance
    {absolute, relative}, roi [x,y,w,h], depends_on names and identical_inputs.
    Descriptors need absolute path, width, height, channels and encoding from
    ENCODINGS; optional flip_y and expected sha256 are explicit choices.
    Paired reference_before/rendered_before descriptors compare contributions.
    Nonfinite components inside the ROI always fail, including matching NaNs.
    Default tolerances are zero. Integer normalized formats use little endian.
    """
    prepared = _prepare(layers)
    directory = None
    if output_directory is not None:
        if not isinstance(output_directory, str):
            raise ValueError("output_directory must be an absolute path")
        directory = Path(output_directory)
        if not directory.is_absolute():
            raise ValueError("output_directory must be an absolute path")
        directory = directory.resolve()
        if directory.exists() and not directory.is_dir():
            raise ValueError("output_directory is not a directory")
    compared = [_compare(layer) for layer in prepared]
    report = {
        "schema": "omni-render-comparison/v1",
        "status": "different" if any(
            layer["status"] == "different" for layer in compared
        ) else "within_tolerance",
        "layers": compared, "localization": _localize(compared),
        "limitations": [
            "Only the declared ROI, encoding, orientation and input files "
            "are compared; no automatic registration or color conversion.",
            "Source hashes identify consumed bytes, not proof that Unity or "
            "RenderDoc produced them from the intended current configuration.",
            "Numeric agreement does not certify whole-scene fidelity or "
            "correct behavior under edits or different views.",
        ],
    }
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("render-comparison-" + uuid.uuid4().hex + ".json")
        report["report_path"] = str(path)
        # Exclusive creation preserves prior reports and all input files.
        with path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
    return report
