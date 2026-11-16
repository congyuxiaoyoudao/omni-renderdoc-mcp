"""Read exported evidence without mistaking assets for rendering equivalence.

Profiles are explicit, hash-bound analysis claims. This module does not infer
effects from anonymous shader names, comments, constants or generated code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

_MAX_JSON = 128 * 1024 * 1024
_MAX_FILE = 512 * 1024 * 1024
_MAX_FILES = 20000
_MAX_ROWS = 100000
_SHA = re.compile(r"[0-9a-fA-F]{64}\Z")
_STATUSES = {"exact", "adapted", "frozen", "missing", "unknown"}


def _json(path):
    if not path.is_file() or path.stat().st_size > _MAX_JSON:
        raise ValueError(f"Missing or oversized JSON evidence: {path}")
    with path.open(encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _rows(value, label):
    if not isinstance(value, list) or len(value) > _MAX_ROWS:
        raise ValueError(f"{label} needs a bounded list")
    if any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{label} entries must be objects")
    return value


def _project_file(project, value):
    if not isinstance(value, str) or not value:
        raise ValueError("Project evidence paths must be nonempty strings")
    path = (project / value).resolve()
    if not path.is_relative_to(project / "Assets"):
        raise ValueError(f"Project evidence must stay inside Assets: {value}")
    return path


def _digest(path):
    before = path.stat()
    if before.st_size > _MAX_FILE:
        raise ValueError(f"Audit evidence exceeds the file limit: {path}")
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError(f"Evidence changed while hashing: {path}")
    return value.hexdigest()


def _identity(value):
    return (
        value.lower()
        if isinstance(value, str) and _SHA.fullmatch(value)
        else None
    )


class _Evidence:
    def __init__(self, project):
        self.project = project
        self.files = {}
        self.inputs = set()

    def record(self, path):
        path = path.resolve()
        self.inputs.add(path)
        if path not in self.files:
            if len(self.files) >= _MAX_FILES:
                raise ValueError("Audit exceeds the explicit file limit")
            self.files[path] = {
                "path": str(path),
                "exists": path.is_file(),
                "sha256": _digest(path) if path.is_file() else None,
            }
        return dict(self.files[path])

    def project_file(self, relative):
        record = self.record(_project_file(self.project, relative))
        record["asset"] = str(relative)
        return record

    def read(self, path):
        record = self.record(path)
        value = _json(path)
        if _digest(path) != record["sha256"]:
            raise ValueError(f"Evidence changed during audit: {path}")
        return value


def _profile(evidence, path):
    if path is None:
        return [], None
    path = Path(path).resolve()
    value = evidence.read(path)
    if value.get("version") != 1:
        raise ValueError("Restoration profile requires version 1")
    modules = _rows(value.get("modules"), "Profile modules")
    seen, result = set(), []
    for source in modules:
        name, state = source.get("id"), source.get("status")
        if not isinstance(name, str) or not name or name in seen:
            raise ValueError("Profile module IDs must be unique strings")
        if state not in _STATUSES:
            raise ValueError(f"Unsupported restoration status: {state}")
        seen.add(name)
        row = {
            key: source[key]
            for key in ("id", "reason", "scope", "event_ids", "resource_ids")
            if key in source
        }
        events = row.get("event_ids", [])
        if not isinstance(events, list) or any(
            type(eid) is not int or eid <= 0 for eid in events
        ):
            raise ValueError(f"Invalid profile event_ids: {name}")
        resources = row.get("resource_ids", [])
        if not isinstance(resources, list) or any(
            not isinstance(rid, str) or not rid for rid in resources
        ):
            raise ValueError(f"Invalid profile resource_ids: {name}")
        checked, issues, stale = [], [], False
        for group in ("implementation_files", "evidence"):
            for item in _rows(source.get(group, []), group):
                relative = item.get("path")
                if group == "implementation_files":
                    target = _project_file(evidence.project, relative)
                else:
                    if not isinstance(relative, str) or not relative:
                        raise ValueError("Evidence requires an explicit path")
                    target = Path(relative)
                    if not target.is_absolute():
                        target = path.parent / target
                    target = target.resolve()
                observed = evidence.record(target)
                expected = _identity(item.get("sha256"))
                match = expected is not None and observed["sha256"] == expected
                checked.append(
                    {
                        **observed,
                        "kind": group,
                        "expected_sha256": expected,
                        "matches": match,
                    }
                )
                if expected is None:
                    issues.append(f"Missing SHA-256: {relative}")
                elif not match:
                    stale = True
                    issues.append(f"Missing or changed evidence: {relative}")
        if (
            not isinstance(source.get("reason"), str)
            or not source["reason"].strip()
        ):
            issues.append("A classification needs an explicit reason")
        if not checked:
            issues.append("No hash-bound evidence supplied")
        if state in {"exact", "adapted"} and not any(
            item["kind"] == "implementation_files" for item in checked
        ):
            issues.append(
                "A running implementation claim needs implementation files"
            )
        if state == "exact" and not any(
            item["kind"] == "evidence" for item in checked
        ):
            issues.append("An exact claim needs explicit validation evidence")
        row.update(
            declared_status=state,
            status="stale" if stale else "unknown" if issues else state,
            evidence_status="stale"
            if stale
            else "unverified"
            if issues
            else "current",
            files=checked,
            issues=issues,
            visual_equivalence_verified=False,
            classification_authority="explicit-profile-claim",
        )
        result.append(row)
    return result, evidence.record(path)


def _bindings(draw):
    result = {}
    for key in ("constant_buffers", "raw_buffers", "textures"):
        result[key] = []
        for item in _rows(draw.get(key, []), key):
            result[key].append(
                {
                    field: item[field]
                    for field in (
                        "name",
                        "resource_id",
                        "file",
                        "byte_count",
                        "format",
                        "first_mip",
                        "first_slice",
                        "dynamic",
                        "status",
                        "requires_dynamic_binding",
                        "byte_offset",
                        "descriptor_offset",
                    )
                    if field in item
                }
            )
    return result


def _replay_observations(data):
    graph = data.get("render_graph_provenance", {})
    if not isinstance(graph, dict):
        raise ValueError("render_graph_provenance must be an object")
    frozen = graph.get("frozen_auxiliary_inputs", [])
    dedup = {}
    for item in _rows(frozen, "Frozen inputs"):
        key = (
            item.get("event_id"),
            str(item.get("resource_id")),
            item.get("name"),
        )
        dedup[key] = dict(item)
    return {
        "scope": "recovered-fragment-replay-only",
        "frozen_auxiliary_inputs": list(dedup.values()),
        "frozen_input_record_count": len(frozen),
        "default_clear_resources": graph.get("default_clear_resources", []),
        "coordinate_adapter": graph.get("coordinate_adapter"),
        "warnings": data.get("warnings", []),
        "note": "Replay snapshots do not classify the native editable scene.",
    }


def _metadata_observations(value, path="", depth=0):
    """Keep declared limitations; never scan HLSL for guessed effects."""
    if depth > 12:
        return []
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            location = f"{path}.{key}" if path else key
            if key in {
                "warnings",
                "limitations",
                "limits",
                "issues",
                "unsupported",
                "runtime_bindings",
                "render_state_notes",
            }:
                found.append({"path": location, "value": item})
            elif key == "status" and item in {"partial", "unavailable"}:
                found.append({"path": location, "value": item})
            elif isinstance(item, (dict, list)):
                found.extend(_metadata_observations(item, location, depth + 1))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(
                _metadata_observations(item, f"{path}[{index}]", depth + 1)
            )
    return found


def _graph(evidence, path, events, modules):
    if path is None:
        return None
    graph = evidence.read(Path(path).resolve())
    if graph.get("schema") != "omni-renderdoc-resource-dependencies/v1":
        raise ValueError("Expected a resource-dependencies/v1 graph")
    nodes = _rows(graph.get("nodes", []), "Graph nodes")
    graph_events = {row.get("event_id") for row in nodes}
    if any(type(eid) is not int or eid <= 0 for eid in graph_events):
        raise ValueError("Graph nodes need positive event_id")
    for module in modules:
        module["graph_event_matches"] = sorted(
            set(module.get("event_ids", [])) & graph_events
        )
    return {
        "file": evidence.record(Path(path).resolve()),
        "schema": graph["schema"],
        "scope": graph.get("scope"),
        "exact": graph.get("exact", False),
        "truncated": graph.get("truncated"),
        "limits": graph.get("limits"),
        "warnings": graph.get("warnings", []),
        "unresolved": graph.get("unresolved", []),
        "external_inputs": graph.get("external_inputs", []),
        "node_count": len(nodes),
        "edge_count": len(graph.get("edges", [])),
        "matched_export_event_ids": sorted(graph_events & events),
        "not_in_export_event_ids": sorted(graph_events - events),
        "capture_identity_verified": False,
        "note": "Event-number joins are candidates only; capture identity and "
        "resource subranges require independent verification.",
    }


def audit_unity_restoration(
    project_path: str,
    dependency_graph_path: str | None = None,
    profile_path: str | None = None,
    output_directory: str | None = None,
) -> dict:
    """Audit declared evidence, scope and current hashes without running Unity.

    A version-1 profile has modules with id, status, reason, event_ids,
    resource_ids,
    implementation_files and evidence. File records contain path and sha256.
    Implementation paths are restricted to the project's Assets directory.
    Evidence paths may be absolute or relative to the profile. All classes are
    explicit analyst claims, including exact; none certify GPU equivalence.
    """
    project = Path(project_path).resolve()
    if not (project / "Assets").is_dir():
        raise ValueError("Expected an existing Unity project with Assets")
    evidence = _Evidence(project)
    native_path = _project_file(project, "Assets/NativeCapture/manifest.json")
    replay_path = _project_file(project, "Assets/RecoveredShaders/draws.json")
    native = evidence.read(native_path) if native_path.is_file() else {}
    replay = evidence.read(replay_path) if replay_path.is_file() else {}
    if not native and not replay:
        raise ValueError("No native or recovered export manifests were found")
    materials = {}
    if native.get("material_library"):
        library = evidence.read(
            _project_file(project, native["material_library"])
        )
        for item in _rows(library.get("entries", []), "Material entries"):
            key = item.get("key")
            if not isinstance(key, str) or not key or key in materials:
                raise ValueError(
                    "Material library keys must be unique strings"
                )
            if not isinstance(item.get("spec"), dict):
                raise ValueError("Material spec must be an object")
            materials[key] = item["spec"]
    modules, profile = _profile(evidence, profile_path)
    draws, raw_shaders = [], {}
    source_draws = {}
    for item in _rows(replay.get("draws", []), "Replay draws"):
        key = (item.get("event_id"), item.get("instance", 0))
        source_draws[key] = item
    references = _rows(native.get("meshes", []), "Native meshes")
    if not references:
        references = [
            dict(event_id=key[0], instance=key[1]) for key in source_draws
        ]
    events = set()
    for item in references:
        event, instance = item.get("event_id"), item.get("instance", 0)
        if (
            (event is not None and (type(event) is not int or event <= 0))
            or type(instance) is not int
            or instance < 0
        ):
            raise ValueError(
                "Draw references need positive event and nonnegative instance"
            )
        if event is not None:
            events.add(event)
        source = source_draws.get((event, instance), {})
        keys = [item["material_key"]] if item.get("material_key") else []
        keys += item.get("additional_material_keys", [])
        slots = []
        for key in keys:
            spec = materials.get(key)
            if spec is None:
                slots.append({"key": key, "status": "missing-material-spec"})
                continue
            shader = spec.get("explicit_shader") or {}
            slot = {
                "key": key,
                "declared_fidelity": spec.get("fidelity"),
                "provenance": spec.get("provenance"),
                "renderer_enabled": spec.get("renderer_enabled", True),
                "shader_asset": evidence.project_file(shader["asset"])
                if shader.get("asset")
                else None,
                "source_shader_sha256": _identity(
                    spec.get("source_shader_sha256")
                ),
            }
            slots.append(slot)
        source_hash = _identity(source.get("source_sha256"))
        hashes = sorted(
            {
                value
                for value in [source_hash]
                + [slot.get("source_shader_sha256") for slot in slots]
                if value
            }
        )
        draw = {
            "event_id": event,
            "instance": instance,
            "mesh_file": item.get("file"),
            "source_identity_status": "declared-event"
            if event is not None
            else "event-not-recorded",
            "status": "unknown",
            "classification_reason": "Static files do not establish "
            "complete draw fidelity",
            "material_slots": slots,
            "declared_source_shader_hashes": hashes,
            "replay_shader_asset": evidence.project_file(
                source["shader_asset"]
            )
            if source.get("shader_asset")
            else None,
            "replay_bindings": _bindings(source),
            "profile_modules": [
                module["id"]
                for module in modules
                if event in module.get("event_ids", [])
            ],
        }
        draws.append(draw)
        for digest in hashes:
            group = raw_shaders.setdefault(
                digest,
                {
                    "sha256": digest,
                    "identity_kind": "manifest-declared-original-shader",
                    "raw_bytes_verified": False,
                    "draws": [],
                    "generated_assets": set(),
                    "note": "Reuse candidate only; bindings and material "
                    "semantics remain per draw",
                },
            )
            group["draws"].append({"event_id": event, "instance": instance})
            if digest == source_hash and draw["replay_shader_asset"]:
                group["generated_assets"].add(
                    draw["replay_shader_asset"]["path"]
                )
            for slot in slots:
                if slot.get("source_shader_sha256") == digest and slot.get(
                    "shader_asset"
                ):
                    group["generated_assets"].add(slot["shader_asset"]["path"])
    for group in raw_shaders.values():
        group["generated_assets"] = sorted(group["generated_assets"])
    graph = _graph(evidence, dependency_graph_path, events, modules)
    scene = (
        evidence.project_file(native["scene_path"])
        if native.get("scene_path")
        else None
    )
    result = {
        "schema": "omni-unity-restoration-audit/v1",
        "project": str(project),
        "audit_kind": "static-export-evidence",
        "visual_equivalence_verified": False,
        "scene_file": scene,
        "profile": profile,
        "modules": modules,
        "summary": {
            "draw_instances": len(draws),
            "event_count": len(events),
            "source_shader_groups": len(raw_shaders),
            "draws_without_source_identity": sum(
                not d["declared_source_shader_hashes"] for d in draws
            ),
            "draws_without_event_identity": sum(
                d["event_id"] is None for d in draws
            ),
            "module_status_counts": dict(
                Counter(m["status"] for m in modules)
            ),
            "draw_fidelity_unverified": len(draws),
        },
        "draws": draws,
        "shader_inventory": list(raw_shaders.values()),
        "replay_observations": _replay_observations(replay),
        "native_observations": _metadata_observations(native),
        "source_export_observations": _metadata_observations(replay),
        "dependency_graph": graph,
        "input_files": list(evidence.files.values()),
        "limits": [
            "Manifest references do not prove the saved or loaded scene's "
            "actual runtime bindings.",
            "No Unity execution, shader compilation, dynamic response or "
            "image comparison was performed.",
            "Profile classifications are analyst claims; matching hashes "
            "establish freshness only.",
            "Original shader identities are declared export hashes, "
            "not reverified original bytecode.",
            "Unprofiled effects remain unknown; shader existence and "
            "comments do not prove fidelity.",
        ],
    }
    if output_directory is not None:
        directory = Path(output_directory).resolve()
        if directory.is_relative_to(project):
            raise ValueError("Write the audit outside the Unity project")
        destination = directory / "restoration-audit.json"
        if destination.is_symlink():
            raise ValueError(
                "Audit destination must not redirect to another file"
            )
        if destination.resolve() in evidence.inputs or (
            destination.exists()
            and any(
                source.is_file() and destination.samefile(source)
                for source in evidence.inputs
            )
        ):
            raise ValueError("Audit output would overwrite input evidence")
        directory.mkdir(parents=True, exist_ok=True)
        result["report_path"] = str(destination)
        destination.write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return result
