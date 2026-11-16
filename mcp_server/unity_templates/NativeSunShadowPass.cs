using System;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;

namespace OmniRenderDoc
{
    [Serializable]
    public sealed class NativeSunShadowPass : CustomPass
    {
        [Serializable] public sealed class Caster
        {
            public Renderer renderer;
            public int materialIndex, submeshIndex;
            public string passName = "NativeSunShadowCaster";
            public bool useMaterialCoverage = true;
            public Material overrideMaterial;
            public NativeSunCasterLink geometryLink;
            public float depthBias, slopeBias;
        }
        [Serializable] public sealed class Region
        {
            public Matrix4x4 sourceWorldToUVZ = Matrix4x4.identity;
            public RectInt viewport;
            public RectInt scissor;
            public float depthQuantization = 65535;
            public float depthBias;
            public Caster[] casters = Array.Empty<Caster>();
        }

        public bool renderSceneView = true, renderGame = true;
        public Texture2D cloudTexture;
        public Matrix4x4 worldToSource = Matrix4x4.identity;
        public Vector4 cloudOrigin, cloudDistancesScale, cloudWindStrength;
        public Vector4 cloudHeightProjection, cloudSecondOrigin, cloudSecondScaleWind;
        public float cloudTime;
        public Vector4 sourceForward;
        public Vector4 cascadeFade, shadowMode, shadowBlend;
        public Matrix4x4[] cascadeMatrices = new Matrix4x4[5];
        public Vector4[] cascadeSpheres = new Vector4[4], cascadeAtlas = new Vector4[4];
        public Vector2Int cascadeDimensions = new Vector2Int(4096,4096);
        public Region[] cascadeRegions = Array.Empty<Region>();
        public Matrix4x4 tileBaseMatrix = Matrix4x4.identity, tileGridMatrix = Matrix4x4.identity;
        public Vector4 tileBiasScale, tileGrid;
        public Vector4[] tileOffsets = new Vector4[128], tileTranslations = new Vector4[128];
        public Vector2Int tileDimensions = new Vector2Int(4096,4096);
        public Region[] tileRegions = Array.Empty<Region>();
        // Explicit validation controls. Authoring cloud texture is safe independently;
        // atlas completeness/coverage must be proved before enabling the source atlas.
        public bool enableSourceCascades, enableSourceTiles;
        // Converts the captured API's signed raster bias to Unity's logical bias.
        // Validate against an atlas fixture when changing rendering backends.
        public float rasterBiasSign = -1;
        public float constantBiasScale = 1;
        public float clearDepth = 1;
        public bool invertCasterCulling;
        [NonSerialized] RenderTexture cascadeDepth, tileDepth;
        [NonSerialized] int renderSerial;
        static int nextRenderSerial;

        bool Supports(Camera camera) => camera != null &&
            (camera.cameraType == CameraType.SceneView ? renderSceneView : camera.cameraType == CameraType.Game && renderGame);

        protected override void Setup(ScriptableRenderContext context, CommandBuffer cmd)
        { RenderPipelineManager.beginCameraRendering += BeginCamera; }
        void BeginCamera(ScriptableRenderContext context, Camera camera)
        {
            var cmd=CommandBufferPool.Get("Reset current-camera source sun binding");
            cmd.SetGlobalFloat("_NativeSunValid",0);
            context.ExecuteCommandBuffer(cmd);CommandBufferPool.Release(cmd);
        }

        static RenderTexture Allocate(RenderTexture current,Vector2Int size,string label)
        {
            if (size.x<=0 || size.y<=0) throw new InvalidOperationException("Sun atlas dimensions must be positive.");
            if (current != null && current.width==size.x && current.height==size.y) return current;
            if (current != null) CoreUtils.Destroy(current);
            var descriptor=new RenderTextureDescriptor(size.x,size.y) {
                graphicsFormat=GraphicsFormat.None, depthStencilFormat=GraphicsFormat.D16_UNorm,
                msaaSamples=1,volumeDepth=1,dimension=TextureDimension.Tex2D,
                useMipMap=false,autoGenerateMips=false,sRGB=false };
            var texture=new RenderTexture(descriptor) { name=label,hideFlags=HideFlags.HideAndDontSave,
                filterMode=FilterMode.Point,wrapMode=TextureWrapMode.Clamp };
            texture.Create();return texture;
        }
        static Matrix4x4 WorldToClip(Matrix4x4 uvz)
        {
            var clip=uvz;
            clip.SetRow(0,2*uvz.GetRow(0)-uvz.GetRow(3));
            clip.SetRow(1,SystemInfo.graphicsUVStartsAtTop ? uvz.GetRow(3)-2*uvz.GetRow(1) : 2*uvz.GetRow(1)-uvz.GetRow(3));
            if (!SystemInfo.usesReversedZBuffer) clip.SetRow(2,uvz.GetRow(3)-uvz.GetRow(2));
            return clip;
        }
        void DrawAtlas(CustomPassContext ctx,RenderTexture texture,Region[] regions)
        {
            ctx.cmd.SetRenderTarget(texture);
            ctx.cmd.SetViewport(new Rect(0,0,texture.width,texture.height));
            ctx.cmd.DisableScissorRect();
            ctx.cmd.ClearRenderTarget(true,false,Color.clear,clearDepth);
            ctx.cmd.SetInvertCulling(invertCasterCulling);
            foreach (var region in regions)
            {
                if (region.viewport.width<=0 || region.viewport.height<=0 || region.viewport.x<0 || region.viewport.y<0 ||
                    region.viewport.xMax>texture.width || region.viewport.yMax>texture.height)
                    throw new InvalidOperationException("Sun shadow region is outside its atlas.");
                var matrix=region.sourceWorldToUVZ*worldToSource;
                ctx.cmd.SetGlobalMatrix("_NativeSunCasterWorldToClip",WorldToClip(matrix));
                ctx.cmd.SetGlobalMatrix("_NativeSunCasterWorldToUVZ",matrix);
                ctx.cmd.SetGlobalFloat("_NativeSunCasterDepthQuantization",region.depthQuantization);
                ctx.cmd.SetGlobalFloat("_NativeSunCasterDepthBias",region.depthBias);
                // CommandBuffer viewports/scissors address native atlas pixels.
                // Clip-space Y conversion is already handled by WorldToClip.
                var r=region.viewport;
                ctx.cmd.SetViewport(new Rect(r.x,r.y,r.width,r.height));
                var s=region.scissor;
                if(s.width>0 && s.height>0)
                    ctx.cmd.EnableScissorRect(new Rect(s.x,s.y,s.width,s.height));
                else ctx.cmd.DisableScissorRect();
                foreach (var caster in region.casters)
                {
                    var renderer=caster?.renderer;
                    if (renderer==null || !renderer.enabled || renderer.forceRenderingOff || !renderer.gameObject.activeInHierarchy) continue;
                    var materials=renderer.sharedMaterials;
                    if (caster.materialIndex<0 || caster.materialIndex>=materials.Length) continue;
                    var material=caster.overrideMaterial!=null?caster.overrideMaterial:materials[caster.materialIndex];
                    if (material==null) continue;
                    int pass=material.FindPass(caster.passName);
                    if (pass<0) throw new InvalidOperationException("Sun caster requires explicit material pass: "+material.name+"/"+caster.passName);
                    ctx.cmd.SetGlobalFloat("_NativeSunCasterUseCoverage",caster.useMaterialCoverage?1:0);
                    ctx.cmd.SetGlobalDepthBias(caster.depthBias*rasterBiasSign*constantBiasScale,caster.slopeBias*rasterBiasSign);
                    if(caster.geometryLink!=null)
                    {
                        var mesh=caster.geometryLink.GetCurrentMesh(renderSerial);
                        ctx.cmd.DrawMesh(mesh,caster.geometryLink.source.transform.localToWorldMatrix,material,caster.submeshIndex,pass);
                    }
                    else ctx.cmd.DrawRenderer(renderer,material,caster.submeshIndex,pass);
                }
            }
            ctx.cmd.SetGlobalDepthBias(0,0);
            ctx.cmd.DisableScissorRect();
            ctx.cmd.SetInvertCulling(false);
        }
        static Vector4 Size(Vector2Int size) => new Vector4(1f/size.x,1f/size.y,size.x,size.y);
        protected override void Execute(CustomPassContext ctx)
        {
            if (!Supports(ctx.hdCamera.camera) || cloudTexture==null) return;
            if (Mathf.Abs(worldToSource.determinant)<1e-12f) throw new InvalidOperationException("Source world transform is singular.");
            renderSerial=++nextRenderSerial;
            bool cascades=enableSourceCascades && cascadeRegions.Length>0;
            bool tiles=enableSourceTiles && tileRegions.Length>0;
            if (cascades) { cascadeDepth=Allocate(cascadeDepth,cascadeDimensions,"Native live sun cascades");DrawAtlas(ctx,cascadeDepth,cascadeRegions); }
            if (tiles) { tileDepth=Allocate(tileDepth,tileDimensions,"Native live sun tiles");DrawAtlas(ctx,tileDepth,tileRegions); }
            ctx.cmd.SetGlobalMatrix("_NativeSunWorldToSource",worldToSource);
            ctx.cmd.SetGlobalMatrix("_NativeSunNormalToSource",worldToSource.inverse.transpose);
            ctx.cmd.SetGlobalTexture("_NativeSunCloudTexture",cloudTexture);
            ctx.cmd.SetGlobalVector("_NativeSunCloudOrigin",cloudOrigin);
            ctx.cmd.SetGlobalVector("_NativeSunCloudDistancesScale",cloudDistancesScale);
            ctx.cmd.SetGlobalVector("_NativeSunCloudWindStrength",cloudWindStrength);
            ctx.cmd.SetGlobalVector("_NativeSunCloudHeightProjection",cloudHeightProjection);
            ctx.cmd.SetGlobalVector("_NativeSunCloudSecondOrigin",cloudSecondOrigin);
            ctx.cmd.SetGlobalVector("_NativeSunCloudSecondScaleWind",cloudSecondScaleWind);
            ctx.cmd.SetGlobalFloat("_NativeSunCloudTime",cloudTime);
            ctx.cmd.SetGlobalVector("_NativeSunForward",sourceForward);
            ctx.cmd.SetGlobalVector("_NativeSunCascadeFade",cascadeFade);
            ctx.cmd.SetGlobalVector("_NativeSunShadowMode",shadowMode);
            ctx.cmd.SetGlobalVector("_NativeSunShadowBlend",shadowBlend);
            ctx.cmd.SetGlobalMatrixArray("_NativeSunCascadeMatrices",cascadeMatrices);
            ctx.cmd.SetGlobalVectorArray("_NativeSunCascadeSpheres",cascadeSpheres);
            ctx.cmd.SetGlobalVectorArray("_NativeSunCascadeAtlas",cascadeAtlas);
            ctx.cmd.SetGlobalVector("_NativeSunCascadeSize",Size(cascadeDimensions));
            ctx.cmd.SetGlobalMatrix("_NativeSunTileBaseMatrix",tileBaseMatrix);
            ctx.cmd.SetGlobalMatrix("_NativeSunTileGridMatrix",tileGridMatrix);
            ctx.cmd.SetGlobalVector("_NativeSunTileBiasScale",tileBiasScale);
            ctx.cmd.SetGlobalVector("_NativeSunTileGrid",tileGrid);
            ctx.cmd.SetGlobalVector("_NativeSunTileSize",Size(tileDimensions));
            ctx.cmd.SetGlobalVectorArray("_NativeSunTileOffsets",tileOffsets);
            ctx.cmd.SetGlobalVectorArray("_NativeSunTileTranslations",tileTranslations);
            if(cascades) ctx.cmd.SetGlobalTexture("_NativeSunCascadeDepth",cascadeDepth,RenderTextureSubElement.Depth);
            else ctx.cmd.SetGlobalTexture("_NativeSunCascadeDepth",Texture2D.blackTexture);
            if(tiles) ctx.cmd.SetGlobalTexture("_NativeSunTileDepth",tileDepth,RenderTextureSubElement.Depth);
            else ctx.cmd.SetGlobalTexture("_NativeSunTileDepth",Texture2D.blackTexture);
            ctx.cmd.SetGlobalFloat("_NativeSunDepthIsReversed",SystemInfo.usesReversedZBuffer?1:0);
            ctx.cmd.SetGlobalFloat("_NativeSunCascadeValid",cascades?1:0);
            ctx.cmd.SetGlobalFloat("_NativeSunTileValid",tiles?1:0);
            ctx.cmd.SetGlobalFloat("_NativeSunValid",1);
            CoreUtils.SetRenderTarget(ctx.cmd,ctx.cameraColorBuffer,ctx.cameraDepthBuffer);
        }
        public RenderTexture GetCascadeTexture() => cascadeDepth;
        public RenderTexture GetTileTexture() => tileDepth;
        protected override void Cleanup()
        {
            RenderPipelineManager.beginCameraRendering -= BeginCamera;
            Shader.SetGlobalFloat("_NativeSunValid",0);
            if(cascadeDepth!=null) CoreUtils.Destroy(cascadeDepth);
            if(tileDepth!=null) CoreUtils.Destroy(tileDepth);
            cascadeDepth=tileDepth=null;
        }
    }
}
