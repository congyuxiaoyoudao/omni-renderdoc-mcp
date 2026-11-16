using System;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;

namespace OmniRenderDoc
{
    // Current opaque scene color for editable refraction/transmission shaders.
    [Serializable]
    public sealed class NativeCameraColorPass : CustomPass
    {
        public string textureProperty = "_NativeOpaqueColorTexture";
        RTHandle snapshot;

        protected override void Setup(ScriptableRenderContext renderContext, CommandBuffer cmd)
        {
            snapshot = RTHandles.Alloc(Vector2.one, slices: TextureXR.slices,
                dimension: TextureXR.dimension, colorFormat: GraphicsFormat.R16G16B16A16_SFloat,
                filterMode: FilterMode.Bilinear, name: "Native current opaque color");
        }

        protected override void Execute(CustomPassContext ctx)
        {
            if (string.IsNullOrEmpty(textureProperty)) return;
            HDUtils.BlitCameraTexture(ctx.cmd, ctx.cameraColorBuffer, snapshot);
            ctx.cmd.SetGlobalTexture(textureProperty, snapshot);
            ctx.cmd.SetGlobalVector(textureProperty + "Scale", snapshot.rtHandleProperties.rtHandleScale);
            CoreUtils.SetRenderTarget(ctx.cmd, ctx.cameraColorBuffer, ctx.cameraDepthBuffer);
        }

        protected override void Cleanup()
        {
            snapshot?.Release(); snapshot = null;
        }
    }
}
