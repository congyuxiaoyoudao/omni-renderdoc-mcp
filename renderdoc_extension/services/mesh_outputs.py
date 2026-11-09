"""Decode reflected vertex, tessellation, geometry and mesh-shader outputs."""

from .mesh_formats import MeshError, enum_name
from .mesh_inputs import Binding, indices, read_attribute
from .mesh_topology import compact, faces


def _signature(rd, pipeline, shader, data_stage):
    stream = (
        max(0, pipeline.GetRasterizedStream())
        if data_stage == rd.MeshDataStage.GSOut
        else 0
    )
    columns = [
        s
        for s in shader.outputSignature
        if s.stream == stream
        and s.systemValue != rd.ShaderBuiltin.OutputIndices
    ]
    columns.sort(key=lambda s: s.systemValue != rd.ShaderBuiltin.Position)
    offsets = {True: 0, False: 0}
    aligned = pipeline.HasAlignedPostVSData(data_stage)
    for sig in columns:
        fmt = rd.ResourceFormat()
        fmt.type = rd.ResourceFormatType.Regular
        fmt.compCount = sig.compCount
        fmt.compByteWidth = max(4, rd.VarTypeByteSize(sig.varType))
        fmt.compType = rd.VarTypeCompType(sig.varType)
        per_face = sig.perPrimitiveRate
        offset = offsets[per_face]
        if aligned and sig.compCount > 1:
            multiple = min(4, 2 ** (sig.compCount - 1))
            alignment = fmt.compByteWidth * multiple
            offset += -offset % alignment
        offsets[per_face] = offset + fmt.compByteWidth * fmt.compCount
        yield sig, fmt, offset


def post_mesh(controller, rd, pipeline, action, instance, stage, view=0):
    if stage == rd.MeshDataStage.MeshOut:
        shader_stage = rd.ShaderStage.Mesh
    elif stage == rd.MeshDataStage.GSOut:
        shader_stage = rd.ShaderStage.Geometry
        if pipeline.GetShaderReflection(shader_stage) is None:
            shader_stage = rd.ShaderStage.Domain
    else:
        shader_stage = rd.ShaderStage.Vertex
    shader = pipeline.GetShaderReflection(shader_stage)
    if shader is None:
        raise MeshError("No reflected geometry-producing shader")
    mesh = controller.GetPostVSData(instance, view, stage)
    if mesh.vertexResourceId == rd.ResourceId.Null() or not mesh.numIndices:
        raise MeshError(mesh.status or "No post-transform vertex buffer")
    if mesh.indexResourceId != rd.ResourceId.Null():
        ib = Binding(
            mesh.indexResourceId,
            mesh.indexByteOffset,
            mesh.indexByteSize,
            mesh.indexByteStride,
        )
        restart = None
        if (
            stage == rd.MeshDataStage.VSOut
            and action.flags & rd.ActionFlags.Indexed
            and pipeline.IsRestartEnabled()
        ):
            restart = pipeline.GetRestartIndex()
        index_stream = indices(
            controller, ib, mesh.numIndices, 0, mesh.baseVertex, restart
        )
    else:
        index_stream = range(mesh.numIndices)
    topology = enum_name(mesh.topology)
    # Use index-stream offsets first so VSOut can be paired with VSIn even
    # after RenderDoc compacts sparse input indices into its replay buffer.
    source_rows = [
        None if v is None else i for i, v in enumerate(index_stream)
    ]
    face_rows = faces(topology, source_rows)
    if not face_rows:
        raise MeshError("No complete primitives in post-transform data")
    signature = list(_signature(rd, pipeline, shader, stage))
    per_face = any(sig.perPrimitiveRate for sig, _, _ in signature)
    keys = [
        (index_stream[row], prim if per_face else None)
        for prim, polygon in enumerate(face_rows)
        for row in polygon
    ]
    points, remap = compact(keys)
    face_indices = [
        [
            remap[index_stream[row], prim if per_face else None]
            for row in polygon
        ]
        for prim, polygon in enumerate(face_rows)
    ]
    first_rows = {}
    for prim, polygon in enumerate(face_rows):
        for row in polygon:
            key = (index_stream[row], prim if per_face else None)
            first_rows.setdefault(key, row)

    attributes = []
    position = None
    for sig, fmt, offset in signature:
        name = sig.varName or sig.semanticIdxName
        if sig.perPrimitiveRate:
            offset += mesh.perPrimitiveOffset
            stride = mesh.perPrimitiveStride
            rows = [prim for _, prim in points]
        else:
            stride = mesh.vertexByteStride
            rows = [index for index, _ in points]
        binding = Binding(
            mesh.vertexResourceId,
            mesh.vertexByteOffset,
            mesh.vertexByteSize,
            stride,
        )
        attr = read_attribute(controller, name, fmt, binding, offset, rows)
        if sig.systemValue == rd.ShaderBuiltin.Position:
            position = attr
        attributes.append(attr)
    if position is None:
        raise MeshError("Shader outputs have no reflected position")
    return (
        position,
        attributes,
        face_indices,
        [first_rows[key] for key in points],
        topology,
    )
