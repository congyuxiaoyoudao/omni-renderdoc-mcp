using System;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;

namespace OmniRenderDoc
{
    [Serializable, VolumeComponentMenu("Post-processing/Omni RenderDoc/Native material transform")]
    public sealed class NativeMaterialPostProcess : CustomPostProcessVolumeComponent, IPostProcessComponent
    {
        [Serializable]
        public sealed class TexturePropertyParameter : VolumeParameter<string>
        {
            public TexturePropertyParameter(string value, bool overrideState = false) : base(value, overrideState) { }
        }
        [Serializable]
        public sealed class SourceBloomParameter : VolumeParameter<NativeSourceBloomProfile>
        {
            public SourceBloomParameter(NativeSourceBloomProfile value, bool overrideState = false) : base(value, overrideState) { }
            public override void Interp(NativeSourceBloomProfile from, NativeSourceBloomProfile to, float t) => value = t > 0 ? to : from;
        }
        public MaterialParameter material = new MaterialParameter(null);
        public BoolParameter enabledEffect = new BoolParameter(true);
        public TexturePropertyParameter sourceTextureProperty = new TexturePropertyParameter("_InputTexture");
        public SourceBloomParameter sourceBloom = new SourceBloomParameter(null);
        Material instance;
        Material sourceMaterial;
        NativeOriginalBloomRenderer bloomRenderer;
        ComputeShader bloomCopy, bloomPrefilter, bloomDownsample, bloomUpsample;

        public bool IsActive() => enabledEffect.value && material.value != null;
        public override bool visibleInSceneView => true;
        public override CustomPostProcessInjectionPoint injectionPoint => CustomPostProcessInjectionPoint.AfterPostProcess;

        public override void Render(CommandBuffer cmd, HDCamera camera, RTHandle source, RTHandle destination)
        {
            if (material.value == null) return;
            if (instance == null || sourceMaterial != material.value || instance.shader != material.value.shader)
            {
                CoreUtils.Destroy(instance);
                sourceMaterial = material.value;
                instance = new Material(sourceMaterial) { hideFlags = HideFlags.HideAndDontSave };
            }
            else instance.CopyPropertiesFromMaterial(sourceMaterial);
            // Older serialized materials may contain zero for a newly added
            // shader property. Keep existing positive authoring values intact.
            if (instance.HasProperty("_NativeInputScale"))
            {
                float scale = instance.GetFloat("_NativeInputScale");
                if (!(scale > 0) || !float.IsFinite(scale)) instance.SetFloat("_NativeInputScale", 1);
            }
            // Input is the current camera image. No captured-frame image or
            // fixed render dimensions participate in this pass.
            instance.SetTexture(sourceTextureProperty.value, source);
            instance.SetVector("_NativeViewport", new Vector4(camera.actualWidth, camera.actualHeight,
                1f / Mathf.Max(1, camera.actualWidth), 1f / Mathf.Max(1, camera.actualHeight)));
            instance.SetVector("_NativeSourceScale", source.rtHandleProperties.rtHandleScale);
            var bloomProfile = sourceBloom.value;
            if (bloomProfile != null)
            {
                RenderTexture bloom = null;
                if (bloomProfile.enabledEffect)
                {
                    bloomProfile.Validate();
                    if (bloomRenderer == null || bloomCopy != bloomProfile.copy || bloomPrefilter != bloomProfile.prefilter ||
                        bloomDownsample != bloomProfile.downsample || bloomUpsample != bloomProfile.upsample)
                    {
                        ReleaseBloom();
                        bloomCopy = bloomProfile.copy; bloomPrefilter = bloomProfile.prefilter;
                        bloomDownsample = bloomProfile.downsample; bloomUpsample = bloomProfile.upsample;
                        bloomRenderer = new NativeOriginalBloomRenderer(bloomCopy, bloomPrefilter, bloomDownsample, bloomUpsample);
                    }
                    // The source is the current resolved color. Bloom runs before
                    // the material's CA, sharpening, inverse exposure and LUT.
                    bloom = bloomRenderer.Render(cmd, camera.camera, source, camera.actualWidth, camera.actualHeight,
                        NativeOriginalBloomRenderer.BuildRoundedLevels(camera.actualWidth, camera.actualHeight, bloomProfile.pyramidLevels),
                        bloomProfile.threshold, bloomProfile.inversePreExposure, bloomProfile.scatter, bloomProfile.inputScale);
                }
                else ReleaseBloom();
                bloomProfile.ApplyToMaterial(instance, bloom);
            }
            else
            {
                ReleaseBloom();
                if (instance.HasProperty("_EnableSourceBloom")) instance.SetFloat("_EnableSourceBloom", 0);
            }
            HDUtils.DrawFullScreen(cmd, instance, destination, shaderPassId: 0);
        }

        void ReleaseBloom()
        {
            bloomRenderer?.Dispose();
            bloomRenderer = null;
            bloomCopy = bloomPrefilter = bloomDownsample = bloomUpsample = null;
        }

        public override void Cleanup()
        {
            ReleaseBloom();
            CoreUtils.Destroy(instance);
            instance = null;
            sourceMaterial = null;
        }
    }
}
