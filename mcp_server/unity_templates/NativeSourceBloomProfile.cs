using System;
using UnityEngine;

namespace OmniRenderDoc
{
    // Explicit verified source parameters. Neither captured images nor event IDs
    // are part of the runtime contract.
    [CreateAssetMenu(menuName = "Omni RenderDoc/Native source Bloom profile")]
    public sealed class NativeSourceBloomProfile : ScriptableObject
    {
        public bool enabledEffect = true;
        public ComputeShader copy, prefilter, downsample, upsample;
        [Range(1, 16)] public int pyramidLevels = 1;
        public Vector4 threshold;
        [Min(0)] public float inversePreExposure = 1;
        [Min(0)] public float inputScale = 1;
        public Vector4 scatter;
        public Vector4 compositeParameters;
        public Vector3 tint = Vector3.one;
        [TextArea] public string provenance;

        public void Validate()
        {
            if (!copy || !prefilter || !downsample || !upsample)
                throw new InvalidOperationException("Source Bloom profile is missing a compute program.");
            if (pyramidLevels < 1 || pyramidLevels > 16 || !Positive(inversePreExposure) || !Positive(inputScale) ||
                !Finite(threshold) || !Finite(scatter) || !Finite(compositeParameters) || !Finite(tint))
                throw new InvalidOperationException("Source Bloom profile contains invalid dimensions or non-finite parameters.");
            if (!copy.HasKernel("Copy2D") || !copy.HasKernel("CopyArray") ||
                !prefilter.HasKernel("main") || !downsample.HasKernel("main") || !upsample.HasKernel("main"))
                throw new InvalidOperationException("Source Bloom profile is missing a required kernel.");
        }

        public static bool SupportsMaterial(Material material) => material != null &&
            material.HasProperty("_EnableSourceBloom") && material.HasProperty("_SourceBloomTexture") &&
            material.HasProperty("_SourceBloomThreshold") && material.HasProperty("_SourceBloomParameters") &&
            material.HasProperty("_SourceBloomTint");

        // Call after CopyPropertiesFromMaterial every frame. Profile settings
        // must not be lost when the source material has its serialized defaults.
        public void ApplyToMaterial(Material material, Texture bloom)
        {
            if (!SupportsMaterial(material))
                throw new InvalidOperationException("The post-process shader does not implement the source Bloom composite contract.");
            material.SetFloat("_EnableSourceBloom", enabledEffect && bloom != null ? 1 : 0);
            material.SetTexture("_SourceBloomTexture", bloom != null ? bloom : Texture2D.blackTexture);
            material.SetVector("_SourceBloomThreshold", threshold);
            material.SetVector("_SourceBloomParameters", compositeParameters);
            material.SetVector("_SourceBloomTint", new Vector4(tint.x, tint.y, tint.z, 1));
            if (material.HasProperty("_SourceInverseExposure")) material.SetFloat("_SourceInverseExposure", inversePreExposure);
            if (material.HasProperty("_NativeInputScale")) material.SetFloat("_NativeInputScale", inputScale);
        }

        static bool Positive(float value) => value > 0 && float.IsFinite(value);
        static bool Finite(Vector4 value) => float.IsFinite(value.x) && float.IsFinite(value.y) && float.IsFinite(value.z) && float.IsFinite(value.w);
    }
}
