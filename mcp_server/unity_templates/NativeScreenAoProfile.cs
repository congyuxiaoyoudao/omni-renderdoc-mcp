using System;
using UnityEngine;
using UnityEngine.Experimental.Rendering;

namespace OmniRenderDoc
{
    // A verified source algorithm supplies this data. There are deliberately no
    // capture/event/resource IDs and no built-in substitute SSAO algorithm.
    [CreateAssetMenu(menuName = "Omni RenderDoc/Native screen AO profile")]
    public sealed class NativeScreenAoProfile : ScriptableObject
    {
        public enum Input { Resource, CameraDepth, CameraNormal, PreviousHistory, SourceMotion, SourceNormal, SourceDepth }

        [Serializable]
        public sealed class Resource
        {
            public string name;
            public GraphicsFormat format = GraphicsFormat.R32_SFloat;
            [Min(1)] public int resolutionDivisor = 1;
            [Min(1)] public int mipCount = 1;
            public FilterMode filterMode = FilterMode.Point;
            // An explicit per-camera/per-frame initialization, never history.
            // Every mip is cleared before the first compute stage.
            public bool clearBeforeUse;
            // A distinct texture receives the completed current result after dispatch.
            public bool retainHistory;
            public Color clearValue = Color.clear;
        }

        [Serializable]
        public sealed class TextureBinding
        {
            public string property;
            public Input input = Input.Resource;
            public string resource;
            // -1 binds the texture normally; >= 0 selects a writable mip.
            public int writeMip = -1;
            // Read bindings default to the whole mip chain. Narrow this only
            // when the source kernel's accessed mip range has been verified.
            [Min(0)] public int readMipStart;
            [Min(0)] public int readMipCount;
        }

        [Serializable]
        public sealed class VectorBinding
        {
            public string property;
            public Vector4 value;
        }

        [Serializable]
        public sealed class VectorArrayBinding
        {
            public string property;
            public Vector4[] values = Array.Empty<Vector4>();
        }

        [Serializable]
        public sealed class Stage
        {
            // Overrides the profile default; keeps recovered kernels with
            // colliding source symbols in separate compute shader assets.
            public ComputeShader program;
            public string kernel;
            public string dispatchResource;
            [Min(0)] public int dispatchMip;
            // Zero means the kernel thread-group size. Source mip producers
            // can cover more pixels than threads, so an explicit extent is valid.
            public Vector2Int pixelsPerGroup;
            public TextureBinding[] textures = Array.Empty<TextureBinding>();
            public VectorBinding[] vectors = Array.Empty<VectorBinding>();
            public VectorArrayBinding[] vectorArrays = Array.Empty<VectorArrayBinding>();
        }

        public ComputeShader program;
        [TextArea] public string provenance;
        // Keep false until both fixed-input source comparison and current-camera
        // depth/normal/matrix adaptation have passed. History is produced from
        // current editable geometry; captured history images are never installed.
        public bool validatedForLiveInputs;
        public bool supportsOrthographic;
        public bool temporalHistoryEnabled = true;
        public Matrix4x4 sourceNormalToWorld = Matrix4x4.identity;
        public Resource[] resources = Array.Empty<Resource>();
        public Stage[] stages = Array.Empty<Stage>();
        public string outputResource;
    }
}
