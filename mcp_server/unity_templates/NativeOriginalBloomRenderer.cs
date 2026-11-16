using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;

namespace OmniRenderDoc
{
    /// <summary>
    /// Runs the recovered threshold/firefly, half-packed Gaussian and bicubic
    /// scatter kernels on this camera's current pre-color-transform image.
    /// All capture/profile-dependent parameters are supplied by the caller.
    /// The returned image contains scene-referred Bloom, not final composite color.
    /// </summary>
    public sealed class NativeOriginalBloomRenderer : IDisposable
    {
        const GraphicsFormat BloomFormat = GraphicsFormat.B10G11R11_UFloatPack32;
        const GraphicsFormat InputFormat = GraphicsFormat.R16G16B16A16_SFloat;
        readonly ComputeShader copy, prefilter, downsample, upsample;
        readonly int copy2D, copyArray, prefilterKernel, downsampleKernel, upsampleKernel;
        readonly Dictionary<int, CameraResources> cameras = new Dictionary<int, CameraResources>();
        readonly List<int> expired = new List<int>();
        bool disposed;

        static readonly int Source = Shader.PropertyToID("_Source");
        static readonly int Output = Shader.PropertyToID("_Output");
        static readonly int SourceSize = Shader.PropertyToID("_SourceSize");
        static readonly int OutputSize = Shader.PropertyToID("_OutputSize");
        static readonly int SourceExposure = Shader.PropertyToID("_SourceExposure");
        static readonly int Threshold = Shader.PropertyToID("_Threshold");
        static readonly int InputScale = Shader.PropertyToID("_InputScale");
        static readonly int Low = Shader.PropertyToID("_Low");
        static readonly int High = Shader.PropertyToID("_High");
        static readonly int LowSize = Shader.PropertyToID("_LowSize");
        static readonly int Scatter = Shader.PropertyToID("_Scatter");

        sealed class CameraResources : IDisposable
        {
            public Camera camera;
            public RenderTexture source;
            public RenderTexture[] down, up;

            public bool Matches(int width, int height, IReadOnlyList<Vector2Int> levels)
            {
                if (source == null || source.width != width || source.height != height || down.Length != levels.Count)
                    return false;
                for (int i = 0; i < down.Length; ++i)
                    if (down[i] == null || down[i].width != levels[i].x || down[i].height != levels[i].y)
                        return false;
                return true;
            }

            public void Dispose()
            {
                Release(source);
                if (down != null) foreach (var texture in down) Release(texture);
                if (up != null) foreach (var texture in up) Release(texture);
                source = null;
                down = up = null;
            }
        }

        public NativeOriginalBloomRenderer(ComputeShader copy, ComputeShader prefilter,
            ComputeShader downsample, ComputeShader upsample)
        {
            this.copy = copy != null ? copy : throw new ArgumentNullException(nameof(copy));
            this.prefilter = prefilter != null ? prefilter : throw new ArgumentNullException(nameof(prefilter));
            this.downsample = downsample != null ? downsample : throw new ArgumentNullException(nameof(downsample));
            this.upsample = upsample != null ? upsample : throw new ArgumentNullException(nameof(upsample));
            RequireFormat(InputFormat);
            RequireFormat(BloomFormat);
            copy2D = copy.FindKernel("Copy2D");
            copyArray = copy.FindKernel("CopyArray");
            prefilterKernel = prefilter.FindKernel("main");
            downsampleKernel = downsample.FindKernel("main");
            upsampleKernel = upsample.FindKernel("main");
        }

        static void RequireFormat(GraphicsFormat format)
        {
            // Packed formats may fail Unity's Texture2D/typed-UAV-load support
            // probe while native RenderTexture UAV stores and SRV sampling work.
            // These kernels never load from a UAV. Validate the actual allocation
            // below, and never accept a substituted storage format.
            if (!SystemInfo.supportsComputeShaders)
                throw new NotSupportedException("Source Bloom requires compute shaders for " + format);
        }

        /// <summary>
        /// Builds viewport-relative levels with nearest-even rounding. This matches
        /// the observed source dimensions. An exporter with a different proven
        /// size policy can supply its own level dimensions directly to Render.
        /// </summary>
        public static Vector2Int[] BuildRoundedLevels(int width, int height, int count)
        {
            if (width < 1 || height < 1 || count < 1 || count > 16)
                throw new ArgumentOutOfRangeException(nameof(count));
            var result = new Vector2Int[count];
            float scale = 0.5f;
            for (int i = 0; i < count; ++i, scale *= 0.5f)
                result[i] = new Vector2Int(Mathf.Max(1, Mathf.RoundToInt(width * scale)),
                    Mathf.Max(1, Mathf.RoundToInt(height * scale)));
            return result;
        }

        /// <param name="currentSource">Live current camera color, before CA/sharpen and LUT.</param>
        /// <param name="width">Active RTHandle viewport width, not backing allocation width.</param>
        /// <param name="height">Active RTHandle viewport height, not backing allocation height.</param>
        /// <param name="threshold">Source threshold and knee constants, in source preexposed units.</param>
        /// <param name="inversePreExposure">Applied by source prefilter after threshold.</param>
        /// <param name="scatter">x is intensity; y selects additive (&gt; .5) or interpolation mode.</param>
        /// <param name="inputScale">Converts current input into source preexposed units before threshold.</param>
        public RenderTexture Render(CommandBuffer cmd, Camera camera, RTHandle currentSource,
            int width, int height, IReadOnlyList<Vector2Int> levels, Vector4 threshold,
            float inversePreExposure, Vector4 scatter, float inputScale = 1f)
        {
            if (disposed) throw new ObjectDisposedException(nameof(NativeOriginalBloomRenderer));
            if (cmd == null) throw new ArgumentNullException(nameof(cmd));
            if (camera == null) throw new ArgumentNullException(nameof(camera));
            var input = currentSource != null ? currentSource.rt : null;
            if (input == null) throw new ArgumentException("Source Bloom requires a live RenderTexture-backed RTHandle.");
            if (width < 1 || height < 1 || width > input.width || height > input.height)
                throw new ArgumentException("Active Bloom viewport must fit the source RTHandle.");
            bool array = input.dimension == TextureDimension.Tex2DArray;
            if ((!array && input.dimension != TextureDimension.Tex2D) ||
                (array && input.volumeDepth != 1) || input.antiAliasing != 1)
                throw new NotSupportedException("Source Bloom currently accepts resolved 2D or single-slice 2DArray color.");
            if (levels == null || levels.Count < 1 || levels.Count > 16)
                throw new ArgumentException("Source Bloom requires an explicit pyramid with 1 to 16 levels.");
            int previousWidth = width, previousHeight = height;
            for (int i = 0; i < levels.Count; ++i)
            {
                if (levels[i].x < 1 || levels[i].y < 1 || levels[i].x > previousWidth || levels[i].y > previousHeight)
                    throw new ArgumentException("Bloom level sizes must be positive and non-increasing.");
                previousWidth = levels[i].x;
                previousHeight = levels[i].y;
            }
            if (!FinitePositive(inversePreExposure) || !FinitePositive(inputScale))
                throw new ArgumentOutOfRangeException(nameof(inversePreExposure));

            PruneDestroyedCameras();
            int id = camera.GetInstanceID();
            if (!cameras.TryGetValue(id, out var state) || state.camera != camera || !state.Matches(width, height, levels))
            {
                state?.Dispose();
                cameras.Remove(id);
                state = Allocate(camera, width, height, levels);
                cameras.Add(id, state);
            }

            // HDRP viewports start at (0,0). Integer loads copy only active pixels,
            // so filtering the pyramid cannot leak unused RTHandle backing pixels.
            int copyKernel = array ? copyArray : copy2D;
            cmd.SetComputeVectorParam(copy, OutputSize, Size(state.source));
            cmd.SetComputeFloatParam(copy, InputScale, inputScale);
            cmd.SetComputeTextureParam(copy, copyKernel, Source, input);
            cmd.SetComputeTextureParam(copy, copyKernel, Output, state.source);
            Dispatch(cmd, copy, copyKernel, state.source);

            cmd.SetComputeVectorParam(prefilter, SourceSize, Size(state.source));
            cmd.SetComputeVectorParam(prefilter, OutputSize, Size(state.down[0]));
            cmd.SetComputeVectorParam(prefilter, SourceExposure, new Vector4(inversePreExposure, 0, 0, 0));
            cmd.SetComputeVectorParam(prefilter, Threshold, threshold);
            cmd.SetComputeTextureParam(prefilter, prefilterKernel, Source, state.source);
            cmd.SetComputeTextureParam(prefilter, prefilterKernel, Output, state.down[0]);
            Dispatch(cmd, prefilter, prefilterKernel, state.down[0]);

            for (int i = 1; i < state.down.Length; ++i)
            {
                cmd.SetComputeVectorParam(downsample, OutputSize, Size(state.down[i]));
                cmd.SetComputeTextureParam(downsample, downsampleKernel, Source, state.down[i - 1]);
                cmd.SetComputeTextureParam(downsample, downsampleKernel, Output, state.down[i]);
                Dispatch(cmd, downsample, downsampleKernel, state.down[i]);
            }

            RenderTexture low = state.down[state.down.Length - 1];
            for (int i = state.down.Length - 2; i >= 0; --i)
            {
                cmd.SetComputeVectorParam(upsample, OutputSize, Size(state.up[i]));
                cmd.SetComputeVectorParam(upsample, LowSize, Size(low));
                cmd.SetComputeVectorParam(upsample, Scatter, scatter);
                cmd.SetComputeTextureParam(upsample, upsampleKernel, Low, low);
                cmd.SetComputeTextureParam(upsample, upsampleKernel, High, state.down[i]);
                cmd.SetComputeTextureParam(upsample, upsampleKernel, Output, state.up[i]);
                Dispatch(cmd, upsample, upsampleKernel, state.up[i]);
                low = state.up[i];
            }
            return low;
        }

        static bool FinitePositive(float value) => value > 0 && !float.IsNaN(value) && !float.IsInfinity(value);
        static Vector4 Size(RenderTexture texture) => new Vector4(texture.width, texture.height, 1f / texture.width, 1f / texture.height);

        static void Dispatch(CommandBuffer cmd, ComputeShader shader, int kernel, RenderTexture destination)
        {
            // The kernels guard only their final store, after shared-memory barriers.
            cmd.DispatchCompute(shader, kernel, (destination.width + 7) / 8, (destination.height + 7) / 8, 1);
        }

        static CameraResources Allocate(Camera camera, int width, int height, IReadOnlyList<Vector2Int> levels)
        {
            var result = new CameraResources { camera = camera, down = new RenderTexture[levels.Count], up = new RenderTexture[levels.Count - 1] };
            try
            {
                result.source = Create(width, height, InputFormat, "Source Bloom active camera input");
                for (int i = 0; i < levels.Count; ++i)
                {
                    result.down[i] = Create(levels[i].x, levels[i].y, BloomFormat, "Source Bloom down " + i);
                    if (i < levels.Count - 1)
                        result.up[i] = Create(levels[i].x, levels[i].y, BloomFormat, "Source Bloom up " + i);
                }
                return result;
            }
            catch
            {
                result.Dispose();
                throw;
            }
        }

        static RenderTexture Create(int width, int height, GraphicsFormat format, string name)
        {
            var descriptor = new RenderTextureDescriptor(width, height)
            {
                graphicsFormat = format, depthStencilFormat = GraphicsFormat.None,
                dimension = TextureDimension.Tex2D, volumeDepth = 1, msaaSamples = 1,
                enableRandomWrite = true, useMipMap = false, autoGenerateMips = false,
                useDynamicScale = false, sRGB = false
            };
            var texture = new RenderTexture(descriptor)
            {
                name = name, hideFlags = HideFlags.HideAndDontSave,
                filterMode = FilterMode.Bilinear, wrapMode = TextureWrapMode.Clamp
            };
            if (texture.Create() && texture.IsCreated() && texture.graphicsFormat == format) return texture;
            Release(texture);
            throw new InvalidOperationException("Cannot allocate " + name + " using " + format);
        }

        void PruneDestroyedCameras()
        {
            expired.Clear();
            foreach (var entry in cameras)
                if (entry.Value.camera == null) expired.Add(entry.Key);
            foreach (int id in expired)
            {
                cameras[id].Dispose();
                cameras.Remove(id);
            }
        }

        public void ReleaseCamera(Camera camera)
        {
            if (camera == null) return;
            int id = camera.GetInstanceID();
            if (!cameras.TryGetValue(id, out var state)) return;
            state.Dispose();
            cameras.Remove(id);
        }

        static void Release(RenderTexture texture)
        {
            if (texture == null) return;
            texture.Release();
            CoreUtils.Destroy(texture);
        }

        public void Dispose()
        {
            foreach (var state in cameras.Values) state.Dispose();
            cameras.Clear();
            expired.Clear();
            disposed = true;
        }
    }
}
