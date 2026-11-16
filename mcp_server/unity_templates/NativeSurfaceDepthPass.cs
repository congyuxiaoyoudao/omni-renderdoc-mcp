using System;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;

namespace OmniRenderDoc
{
    // An optional current-camera depth dependency for materials that blend with
    // a selected underlying surface. Renderers remain ordinary editable meshes.
    [Serializable]
    public sealed class NativeSurfaceDepthPass : CustomPass
    {
        public MeshRenderer[] surfaces = Array.Empty<MeshRenderer>();
        public Material depthMaterial;
        public string textureProperty = "_NativeSurfaceDepthTexture";
        RTHandle color, depth;

        protected override void Setup(ScriptableRenderContext renderContext, CommandBuffer cmd)
        {
            color = RTHandles.Alloc(Vector2.one, slices: TextureXR.slices, dimension: TextureXR.dimension,
                colorFormat: GraphicsFormat.R32_SFloat, filterMode: FilterMode.Point,
                name: "Native selected surface depth");
            depth = RTHandles.Alloc(Vector2.one, slices: TextureXR.slices, dimension: TextureXR.dimension,
                depthBufferBits: DepthBits.Depth32, filterMode: FilterMode.Point,
                name: "Native selected surface depth attachment");
        }

        protected override void Execute(CustomPassContext ctx)
        {
            if (depthMaterial == null || string.IsNullOrEmpty(textureProperty)) return;
            CoreUtils.SetRenderTarget(ctx.cmd, color, depth, ClearFlag.None);
            float farDepth = SystemInfo.usesReversedZBuffer ? 0 : 1;
            // ClearRenderTarget's depth is in Unity's logical depth convention;
            // the backend reverses it. The R32 color stores native device depth.
            ctx.cmd.ClearRenderTarget(true, true, new Color(farDepth, farDepth, farDepth, farDepth), 1f);
            foreach (var surface in surfaces)
            {
                if (surface == null || !surface.enabled || !surface.gameObject.activeInHierarchy ||
                    (ctx.hdCamera.camera.cullingMask & (1 << surface.gameObject.layer)) == 0) continue;
                var filter = surface.GetComponent<MeshFilter>();
                if (filter == null || filter.sharedMesh == null) continue;
                for (int submesh = 0; submesh < filter.sharedMesh.subMeshCount; submesh++)
                    ctx.cmd.DrawRenderer(surface, depthMaterial, submesh, 0);
            }
            ctx.cmd.SetGlobalTexture(textureProperty, color);
            CoreUtils.SetRenderTarget(ctx.cmd, ctx.cameraColorBuffer, ctx.cameraDepthBuffer);
        }

        protected override void Cleanup()
        {
            color?.Release(); depth?.Release(); color = depth = null;
        }
    }
}
