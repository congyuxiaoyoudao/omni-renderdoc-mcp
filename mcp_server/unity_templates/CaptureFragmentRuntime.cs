using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;
using UnityEngine.Experimental.Rendering;

namespace OmniRenderDoc
{
    [ExecuteAlways]
    public sealed class CaptureFragmentRuntime : MonoBehaviour
    {
        [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
        struct UInt4 { public uint x, y, z, w; }
        [Serializable] public sealed class RenderTargetSpec
        {
            public string id, format;
            public int width, height;
            public bool depth;
            public Color clearColor = Color.clear;
            public float clearDepth = 0;
            public uint clearStencil = 0;
        }
        [Serializable] public sealed class DynamicBinding { public string name, resource; }
        [Serializable] public sealed class ColorClear { public string resource; public Color color; }
        [Serializable] public sealed class DepthClear
        {
            public string resource;
            public bool clearDepth, clearStencil;
            public float depth;
            public uint stencil;
        }
        [Serializable] public sealed class BufferBinding
        {
            public string name;
            public TextAsset data;
            public int byteCount;
            public bool writable;
            public int uavRegister = -1;
            [NonSerialized] public ComputeBuffer gpuBuffer;
        }
        [Serializable] public sealed class FragmentDraw
        {
            public string name;
            public int eventId, instance, varyingStride;
            public Mesh mesh;
            public Material material;
            public TextAsset varyings, clipPositions;
            public float clipYSign = 1;
            public bool frontFaceFlip;
            public Vector4 outputScale = Vector4.one;
            public List<BufferBinding> constants = new List<BufferBinding>();
            public List<BufferBinding> rawBuffers = new List<BufferBinding>();
            public List<string> cameraColorProperties = new List<string>();
            public List<DynamicBinding> resourceProperties = new List<DynamicBinding>();
            public string[] colorTargets;
            public string depthTarget;
            public int viewportWidth, viewportHeight;
            public List<ColorClear> clearColorTargets = new List<ColorClear>();
            public DepthClear clearDepthTarget;
            [NonSerialized] public MaterialPropertyBlock properties;
            [NonSerialized] public List<ComputeBuffer> ownedBuffers;
        }

        public int sourceWidth, sourceHeight;
        public bool clearColor = true;
        public Color clearColorLinear = Color.black;
        public bool isolatedDepth = true;
        public List<FragmentDraw> draws = new List<FragmentDraw>();
        public List<RenderTargetSpec> renderTargets = new List<RenderTargetSpec>();
        public string finalTarget;
        public bool finalIsSrgb;
        public bool finalFlipY;
        public Material finalCopyMaterial;
        [NonSerialized] public string lastRenderError;
        [NonSerialized] bool ready;

        void OnEnable() { ready = false; }
        void OnDisable() { Release(); }
        void OnDestroy() { Release(); }
        public void Release()
        {
            foreach (var draw in draws)
            {
                if (draw.ownedBuffers != null)
                    foreach (var buffer in draw.ownedBuffers) buffer?.Release();
                draw.ownedBuffers = null;
                draw.properties = null;
            }
            ready = false;
        }

        static ComputeBuffer CreateBuffer(TextAsset file, int stride, ComputeBufferType type, int declaredBytes = 0)
        {
            if (file == null) throw new InvalidOperationException("A captured buffer asset is missing.");
            var bytes = file.bytes;
            int byteCount = declaredBytes == 0 ? bytes.Length : declaredBytes;
            if (byteCount <= 0 || bytes.Length < byteCount)
                throw new InvalidOperationException("Captured buffer is incomplete: " + file.name);
            int padded = ((byteCount + stride - 1) / stride) * stride;
            var values = new uint[padded / 4];
            Buffer.BlockCopy(bytes, 0, values, 0, byteCount);
            var buffer = new ComputeBuffer(padded / stride, stride, type);
            if (stride == 16)
            {
                var rows = new UInt4[padded / 16];
                for (int i = 0; i < rows.Length; i++)
                    rows[i] = new UInt4 { x = values[i * 4], y = values[i * 4 + 1], z = values[i * 4 + 2], w = values[i * 4 + 3] };
                buffer.SetData(rows);
            }
            else buffer.SetData(values);
            return buffer;
        }

        public void Prepare()
        {
            if (sourceWidth <= 0 || sourceHeight <= 0)
                throw new InvalidOperationException("Captured source width and height must be positive.");
            if (ready) return;
            Release();
            try
            {
                foreach (var draw in draws)
                {
                    if (draw.mesh == null || draw.material == null)
                        throw new InvalidOperationException("Missing recovered mesh/material: " + draw.name);
                    draw.properties = new MaterialPropertyBlock();
                    draw.ownedBuffers = new List<ComputeBuffer>();
                    var clip = CreateBuffer(draw.clipPositions, 16, ComputeBufferType.Structured);
                    draw.ownedBuffers.Add(clip);
                    draw.properties.SetBuffer("_CapturedClipPositions", clip);
                    if (draw.varyings != null && draw.varyingStride > 0)
                    {
                        var varying = CreateBuffer(draw.varyings, 16, ComputeBufferType.Structured);
                        draw.ownedBuffers.Add(varying);
                        draw.properties.SetBuffer("_CapturedVaryings", varying);
                    }
                    draw.properties.SetInteger("_CapturedVaryingStride", draw.varyingStride);
                    draw.properties.SetFloat("_CapturedClipYSign", draw.clipYSign);
                    draw.properties.SetFloat("_CapturedFrontFaceFlip", draw.frontFaceFlip ? 1 : 0);
                    draw.properties.SetVector("_CapturedOutputScale", draw.outputScale);
                    foreach (var binding in draw.constants)
                    {
                        var buffer = CreateBuffer(binding.data, 16, ComputeBufferType.Constant, binding.byteCount);
                        draw.ownedBuffers.Add(buffer);
                        int bytes = ((binding.byteCount > 0 ? binding.byteCount : binding.data.bytes.Length) + 15) / 16 * 16;
                        draw.properties.SetConstantBuffer(binding.name, buffer, 0, bytes);
                    }
                    foreach (var binding in draw.rawBuffers)
                    {
                        var buffer = CreateBuffer(binding.data, 4, ComputeBufferType.Raw, binding.byteCount);
                        draw.ownedBuffers.Add(buffer);
                        binding.gpuBuffer = buffer;
                        draw.properties.SetBuffer(binding.name, buffer);
                    }
                }
                ready = true;
            }
            catch { Release(); throw; }
        }
    }

    [Serializable]
    public sealed class RecoveredFragmentPass : CustomPass
    {
        public CaptureFragmentRuntime source;
        RTHandle capturedDepth;
        RTHandle colorSnapshot;
        readonly Dictionary<string, RenderTexture> targets = new Dictionary<string, RenderTexture>();
        int depthWidth, depthHeight;

        // Unity reverses the supplied clear depth on reversed-Z backends. The
        // manifest stores the captured native value, so undo that API conversion.
        static float UnityClearDepth(float nativeDepth) => SystemInfo.usesReversedZBuffer ? 1f - nativeDepth : nativeDepth;

        protected override void Execute(CustomPassContext ctx)
        {
            try { ExecuteCaptured(ctx); }
            catch (Exception error)
            {
                if (source != null) source.lastRenderError = error.ToString();
                throw;
            }
        }

        void ExecuteCaptured(CustomPassContext ctx)
        {
            if (source == null || !source.isActiveAndEnabled) return;
            source.Prepare();
            int width = ctx.hdCamera.actualWidth, height = ctx.hdCamera.actualHeight;
            if (source.isolatedDepth)
            {
                if (capturedDepth == null || width != depthWidth || height != depthHeight)
                {
                    capturedDepth?.Release();
                    capturedDepth = RTHandles.Alloc(width, height, format: GraphicsFormat.D32_SFloat_S8_UInt,
                        name: "Recovered capture depth");
                    depthWidth = width; depthHeight = height;
                }
                CoreUtils.SetRenderTarget(ctx.cmd, ctx.cameraColorBuffer, capturedDepth);
                ctx.cmd.ClearRenderTarget(true, source.clearColor, source.clearColorLinear,
                    UnityClearDepth(0));
            }
            else
            {
                CoreUtils.SetRenderTarget(ctx.cmd, ctx.cameraColorBuffer, ctx.cameraDepthBuffer);
                if (source.clearColor) ctx.cmd.ClearRenderTarget(false, true, source.clearColorLinear);
            }
            var scale = new Vector4((float)source.sourceWidth / width,
                (float)source.sourceHeight / height, 0, 0);
            foreach (var spec in source.renderTargets)
            {
                if (!targets.TryGetValue(spec.id, out var target))
                {
                    var descriptor = new RenderTextureDescriptor(spec.width, spec.height)
                    {
                        graphicsFormat = spec.depth ? GraphicsFormat.None : (GraphicsFormat)Enum.Parse(typeof(GraphicsFormat), spec.format, true),
                        depthStencilFormat = spec.depth ? (GraphicsFormat)Enum.Parse(typeof(GraphicsFormat), spec.format, true) : GraphicsFormat.None,
                        msaaSamples = 1,
                    };
                    target = new RenderTexture(descriptor) { name = "Captured resource " + spec.id, filterMode = FilterMode.Bilinear, wrapMode = TextureWrapMode.Clamp };
                    if (!target.Create()) throw new InvalidOperationException("Failed to create native target " + spec.id + " (" + spec.format + ").");
                    targets[spec.id] = target;
                }
                ctx.cmd.SetRenderTarget(target);
                ctx.cmd.ClearRenderTarget(spec.depth, !spec.depth, spec.clearColor, UnityClearDepth(spec.clearDepth), spec.clearStencil);
            }
            foreach (var draw in source.draws)
            {
                foreach (var clear in draw.clearColorTargets)
                {
                    if (!targets.TryGetValue(clear.resource, out var target)) throw new InvalidOperationException("Missing color clear resource " + clear.resource);
                    ctx.cmd.SetRenderTarget(target);
                    ctx.cmd.ClearRenderTarget(RTClearFlags.Color, clear.color, 0, 0);
                }
                if (draw.clearDepthTarget != null && !string.IsNullOrEmpty(draw.clearDepthTarget.resource))
                {
                    var clear = draw.clearDepthTarget;
                    if (!targets.TryGetValue(clear.resource, out var target)) throw new InvalidOperationException("Missing depth clear resource " + clear.resource);
                    ctx.cmd.SetRenderTarget(target);
                    RTClearFlags flags = (clear.clearDepth ? RTClearFlags.Depth : RTClearFlags.None) | (clear.clearStencil ? RTClearFlags.Stencil : RTClearFlags.None);
                    ctx.cmd.ClearRenderTarget(flags, Color.clear, UnityClearDepth(clear.depth), clear.stencil);
                }
                if (draw.cameraColorProperties.Count > 0)
                {
                    if (colorSnapshot == null || colorSnapshot.rt.width != width || colorSnapshot.rt.height != height)
                    {
                        colorSnapshot?.Release();
                        colorSnapshot = RTHandles.Alloc(width, height, colorFormat: ctx.cameraColorBuffer.rt.graphicsFormat,
                            name: "Recovered live color input");
                    }
                    HDUtils.BlitCameraTexture(ctx.cmd, ctx.cameraColorBuffer, colorSnapshot);
                    CoreUtils.SetRenderTarget(ctx.cmd, ctx.cameraColorBuffer, source.isolatedDepth ? capturedDepth : ctx.cameraDepthBuffer);
                    foreach (string property in draw.cameraColorProperties)
                        draw.properties.SetTexture(property, colorSnapshot.rt);
                }
                foreach (var property in draw.resourceProperties)
                {
                    if (!targets.TryGetValue(property.resource, out var texture)) throw new InvalidOperationException("Missing dynamic resource " + property.resource);
                    if (texture.depthStencilFormat != GraphicsFormat.None)
                        draw.properties.SetTexture(property.name, texture, RenderTextureSubElement.Depth);
                    else draw.properties.SetTexture(property.name, texture);
                }
                if (draw.colorTargets != null && draw.colorTargets.Length > 0)
                {
                    var colors = new RenderTargetIdentifier[draw.colorTargets.Length];
                    for (int i = 0; i < colors.Length; i++) colors[i] = targets[draw.colorTargets[i]];
                    RenderTargetIdentifier depth = string.IsNullOrEmpty(draw.depthTarget) ? BuiltinRenderTextureType.None : new RenderTargetIdentifier(targets[draw.depthTarget]);
                    ctx.cmd.SetRenderTarget(colors, depth);
                    var first = targets[draw.colorTargets[0]];
                    ctx.cmd.SetViewport(new Rect(0, 0, draw.viewportWidth > 0 ? draw.viewportWidth : first.width, draw.viewportHeight > 0 ? draw.viewportHeight : first.height));
                    draw.properties.SetVector("_CapturedScreenScale", new Vector4(1, 1, 0, 0));
                }
                else if (!string.IsNullOrEmpty(draw.depthTarget))
                {
                    var depth = targets[draw.depthTarget];
                    ctx.cmd.SetRenderTarget(BuiltinRenderTextureType.None, depth);
                    ctx.cmd.SetViewport(new Rect(0, 0, draw.viewportWidth > 0 ? draw.viewportWidth : depth.width, draw.viewportHeight > 0 ? draw.viewportHeight : depth.height));
                    draw.properties.SetVector("_CapturedScreenScale", new Vector4(1, 1, 0, 0));
                }
                else
                {
                    CoreUtils.SetRenderTarget(ctx.cmd, ctx.cameraColorBuffer, source.isolatedDepth ? capturedDepth : ctx.cameraDepthBuffer);
                    ctx.cmd.SetViewport(new Rect(0, 0, width, height));
                    draw.properties.SetVector("_CapturedScreenScale", scale);
                }
                bool hasWrites = false;
                foreach (var binding in draw.rawBuffers)
                {
                    if (!binding.writable) continue;
                    int colors = draw.colorTargets != null && draw.colorTargets.Length > 0 ? draw.colorTargets.Length : (string.IsNullOrEmpty(draw.depthTarget) ? 1 : 0);
                    if (binding.uavRegister < colors || binding.uavRegister >= 8)
                        throw new InvalidOperationException("Pixel UAV register overlaps attachments or exceeds D3D11 slots: " + binding.name);
                    ctx.cmd.SetRandomWriteTarget(binding.uavRegister, binding.gpuBuffer, true);
                    hasWrites = true;
                }
                ctx.cmd.DrawMesh(draw.mesh, Matrix4x4.identity, draw.material, 0, 0, draw.properties);
                if (hasWrites) ctx.cmd.ClearRandomWriteTargets();
            }
            if (!string.IsNullOrEmpty(source.finalTarget))
            {
                if (!targets.TryGetValue(source.finalTarget, out var final)) throw new InvalidOperationException("Missing final target " + source.finalTarget);
                if (source.finalCopyMaterial == null) throw new InvalidOperationException("Missing final color transport material.");
                source.finalCopyMaterial.SetFloat("_DecodeSRGB", source.finalIsSrgb ? 1 : 0);
                source.finalCopyMaterial.SetFloat("_FlipY", source.finalFlipY ? 1 : 0);
                ctx.cmd.Blit(final, ctx.cameraColorBuffer, source.finalCopyMaterial);
            }
        }
        protected override void Cleanup()
        {
            capturedDepth?.Release(); capturedDepth = null;
            colorSnapshot?.Release(); colorSnapshot = null;
            foreach (var target in targets.Values) { target.Release(); UnityEngine.Object.DestroyImmediate(target); }
            targets.Clear();
            source?.Release();
        }
    }
}
