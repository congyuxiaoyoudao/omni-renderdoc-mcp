using System;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;

namespace OmniRenderDoc
{
    // Explicit material dependency. All scene identifiers, projection parameters,
    // caster membership and sampling conventions are supplied by a scene profile.
    // This pass draws current Renderer meshes; no captured geometry is retained.
    [Serializable]
    public sealed class NativeActorShadowPass : CustomPass
    {
        [Serializable]
        public sealed class Caster
        {
            public Renderer renderer;
            public int materialIndex;
            public int submeshIndex;
            public string passName = "NativeActorShadowCaster";
            public bool useMaterialCoverage = true;
        }

        [Serializable]
        public sealed class Receiver
        {
            public Renderer renderer;
            public int materialIndex;
            public float visibilityQuantization;
        }

        [Serializable]
        public sealed class Actor
        {
            public string label;
            public Transform anchor;
            public Matrix4x4 referenceAnchorLocalToWorld = Matrix4x4.identity;
            // Normalized texture coordinates and reversed normalized depth.
            public Matrix4x4 referenceWorldToShadowUVZ = Matrix4x4.identity;
            // Linear coordinate basis in which forward and receiver biases were authored.
            public Matrix4x4 referenceWorldToBiasSpace = Matrix4x4.identity;
            public Vector3 forward;
            public Vector2 receiverBias;
            public Vector2 filterRadius;
            public float depthQuantization;
            public float depthBias;
            public int borderPixels;
            public int width;
            public int height;
            public Caster[] casters = Array.Empty<Caster>();
            public Receiver[] receivers = Array.Empty<Receiver>();
        }

        public Actor[] actors = Array.Empty<Actor>();
        public bool renderSceneView = true;
        public bool renderGame = true;
        [NonSerialized] RenderTexture[] textures;
        [NonSerialized] MaterialPropertyBlock properties;
        [NonSerialized] CustomPassVolume owningVolume;

        static readonly int TextureId = Shader.PropertyToID("_NativeActorShadowDepth");
        static readonly int ValidId = Shader.PropertyToID("_NativeActorShadowValid");
        static readonly int MatrixId = Shader.PropertyToID("_NativeActorWorldToShadowUVZ");
        static readonly int NormalId = Shader.PropertyToID("_NativeActorShadowNormalToBiasSpace");
        static readonly int BiasToWorldId = Shader.PropertyToID("_NativeActorShadowBiasToWorld");
        static readonly int ForwardId = Shader.PropertyToID("_NativeActorShadowForward");
        static readonly int BiasId = Shader.PropertyToID("_NativeActorShadowBias");
        static readonly int RadiusId = Shader.PropertyToID("_NativeActorShadowFilterRadius");
        static readonly int CasterMatrixId = Shader.PropertyToID("_NativeActorShadowCasterWorldToClip");
        static readonly int CasterDepthId = Shader.PropertyToID("_NativeActorShadowCasterWorldToUVZ");
        static readonly int QuantizationId = Shader.PropertyToID("_NativeActorShadowCasterDepthQuantization");
        static readonly int DepthBiasId = Shader.PropertyToID("_NativeActorShadowCasterDepthBias");
        static readonly int CoverageId = Shader.PropertyToID("_NativeActorShadowCasterUseCoverage");
        static readonly int VisibilityQuantizationId = Shader.PropertyToID("_NativeActorShadowVisibilityQuantization");

        protected override void Setup(ScriptableRenderContext renderContext, CommandBuffer cmd)
        {
            properties = new MaterialPropertyBlock();
            EnsureTextures();
            foreach (var volume in UnityEngine.Object.FindObjectsByType<CustomPassVolume>(FindObjectsInactive.Include, FindObjectsSortMode.None))
                if (volume.customPasses.Contains(this)) { owningVolume = volume; break; }
            RenderPipelineManager.beginCameraRendering += BeginCamera;
        }

        bool SupportsCamera(Camera camera) => camera != null &&
            (camera.cameraType == CameraType.SceneView ? renderSceneView :
             camera.cameraType == CameraType.Game && renderGame);

        void BeginCamera(ScriptableRenderContext context, Camera camera)
        {
            // HDRP snapshots Renderer property blocks during culling/renderer-list
            // preparation. Bind here, before culling; changing them in Execute is
            // too late for the receiver draws of the current camera.
            if (!enabled || !SupportsCamera(camera) ||
                (owningVolume != null && !owningVolume.isActiveAndEnabled))
            {
                ResetReceiverBindings();
                return;
            }
            EnsureTextures();
            BindReceiverProperties();
        }

        void BindReceiverProperties()
        {
            if (properties == null) properties = new MaterialPropertyBlock();
            for (int index = 0; index < actors.Length; index++)
            {
                var actor = actors[index];
                var deltaInverse = actor.anchor == null ? Matrix4x4.identity :
                    actor.referenceAnchorLocalToWorld * actor.anchor.worldToLocalMatrix;
                var worldToShadow = actor.referenceWorldToShadowUVZ * deltaInverse;
                var worldToBias = actor.referenceWorldToBiasSpace * deltaInverse;
                var biasToWorld = worldToBias.inverse;
                var normalToBias = biasToWorld.transpose;
                var texture = textures[index];
                foreach (var receiver in actor.receivers ?? Array.Empty<Receiver>())
                {
                    if (receiver?.renderer == null || receiver.materialIndex < 0 ||
                        receiver.materialIndex >= receiver.renderer.sharedMaterials.Length) continue;
                    receiver.renderer.GetPropertyBlock(properties, receiver.materialIndex);
                    properties.SetTexture(TextureId, texture);
                    properties.SetFloat(ValidId, 1);
                    properties.SetFloat(VisibilityQuantizationId, receiver.visibilityQuantization);
                    properties.SetMatrix(MatrixId, worldToShadow);
                    properties.SetMatrix(NormalId, normalToBias);
                    properties.SetMatrix(BiasToWorldId, biasToWorld);
                    properties.SetVector(ForwardId, actor.forward.normalized);
                    properties.SetVector(BiasId, new Vector4(actor.receiverBias.x, actor.receiverBias.y, 0, 0));
                    properties.SetVector(RadiusId, new Vector4(actor.filterRadius.x, actor.filterRadius.y, 0, 0));
                    receiver.renderer.SetPropertyBlock(properties, receiver.materialIndex);
                }
            }
        }

        public RenderTexture GetShadowTexture(int actorIndex) =>
            textures != null && actorIndex >= 0 && actorIndex < textures.Length ? textures[actorIndex] : null;

        static bool Finite(float value) => !float.IsNaN(value) && !float.IsInfinity(value);
        static bool ValidMatrix(Matrix4x4 matrix)
        {
            for (int i = 0; i < 16; i++) if (!Finite(matrix[i])) return false;
            return Mathf.Abs(matrix.determinant) > 1e-12f;
        }

        void EnsureTextures()
        {
            if (actors == null) actors = Array.Empty<Actor>();
            if (textures == null || textures.Length != actors.Length)
            {
                ReleaseTextures();
                textures = new RenderTexture[actors.Length];
            }
            for (int i = 0; i < actors.Length; i++)
            {
                var actor = actors[i];
                if (actor == null || actor.width <= 0 || actor.height <= 0 ||
                    !ValidMatrix(actor.referenceWorldToShadowUVZ) ||
                    !ValidMatrix(actor.referenceAnchorLocalToWorld) ||
                    !ValidMatrix(actor.referenceWorldToBiasSpace) ||
                    !Finite(actor.forward.sqrMagnitude) || actor.forward.sqrMagnitude < 1e-10f ||
                    !Finite(actor.receiverBias.sqrMagnitude) || !Finite(actor.filterRadius.sqrMagnitude) ||
                    actor.filterRadius.x < 0 || actor.filterRadius.y < 0 ||
                    !Finite(actor.depthQuantization) || actor.depthQuantization < 0 || !Finite(actor.depthBias) ||
                    actor.borderPixels < 0 || actor.borderPixels*2 >= Math.Min(actor.width, actor.height))
                    throw new InvalidOperationException("Native actor shadow requires explicit positive dimensions, finite invertible matrices and a nonzero forward vector.");
                var texture = textures[i];
                if (texture != null && texture.width == actor.width && texture.height == actor.height) continue;
                if (texture != null) CoreUtils.Destroy(texture);
                var descriptor = new RenderTextureDescriptor(actor.width, actor.height)
                {
                    graphicsFormat = GraphicsFormat.R32_SFloat,
                    depthStencilFormat = GraphicsFormat.D32_SFloat,
                    msaaSamples = 1,
                    volumeDepth = 1,
                    dimension = TextureDimension.Tex2D,
                    useMipMap = false,
                    autoGenerateMips = false,
                    sRGB = false
                };
                texture = new RenderTexture(descriptor)
                {
                    name = "Native actor shadow: " + actor.label,
                    hideFlags = HideFlags.HideAndDontSave,
                    filterMode = FilterMode.Point,
                    wrapMode = TextureWrapMode.Clamp
                };
                texture.Create();
                textures[i] = texture;
            }
        }

        public static Matrix4x4 WorldToClip(Matrix4x4 uvz)
        {
            // Texture.Load/Gather uses top-left texture coordinates on D3D.
            // The supplied normalized depth remains reversed on every backend.
            var clip = uvz;
            clip.SetRow(0, 2 * uvz.GetRow(0) - uvz.GetRow(3));
            clip.SetRow(1, SystemInfo.graphicsUVStartsAtTop
                ? uvz.GetRow(3) - 2 * uvz.GetRow(1)
                : 2 * uvz.GetRow(1) - uvz.GetRow(3));
            if (!SystemInfo.usesReversedZBuffer) clip.SetRow(2, uvz.GetRow(3) - uvz.GetRow(2));
            return clip;
        }

        protected override void Execute(CustomPassContext ctx)
        {
            var camera = ctx.hdCamera.camera;
            if (!SupportsCamera(camera)) return;
            EnsureTextures();
            if (properties == null) properties = new MaterialPropertyBlock();
            for (int index = 0; index < actors.Length; index++)
            {
                var actor = actors[index];
                var deltaInverse = actor.anchor == null ? Matrix4x4.identity :
                    actor.referenceAnchorLocalToWorld * actor.anchor.worldToLocalMatrix;
                var worldToShadow = actor.referenceWorldToShadowUVZ * deltaInverse;
                var texture = textures[index];
                ctx.cmd.SetRenderTarget(texture);
                ctx.cmd.SetViewport(new Rect(0, 0, actor.width, actor.height));
                ctx.cmd.DisableScissorRect();
                // Unity logically clears depth to far=1, reversing for the GPU.
                // Our color channel always stores the reversed source depth.
                ctx.cmd.ClearRenderTarget(true, true, Color.clear, 1f);
                ctx.cmd.SetGlobalMatrix(CasterMatrixId, WorldToClip(worldToShadow));
                ctx.cmd.SetGlobalMatrix(CasterDepthId, worldToShadow);
                ctx.cmd.SetGlobalFloat(QuantizationId, actor.depthQuantization);
                ctx.cmd.SetGlobalFloat(DepthBiasId, actor.depthBias);
                if (actor.borderPixels > 0)
                    ctx.cmd.EnableScissorRect(new Rect(actor.borderPixels, actor.borderPixels,
                        actor.width-actor.borderPixels*2, actor.height-actor.borderPixels*2));
                foreach (var caster in actor.casters ?? Array.Empty<Caster>())
                {
                    var renderer = caster?.renderer;
                    if (renderer == null || !renderer.enabled || renderer.forceRenderingOff ||
                        !renderer.gameObject.activeInHierarchy) continue;
                    var materials = renderer.sharedMaterials;
                    if (caster.materialIndex < 0 || caster.materialIndex >= materials.Length) continue;
                    var material = materials[caster.materialIndex];
                    if (material == null) continue;
                    int pass = material.FindPass(caster.passName);
                    if (pass < 0) throw new InvalidOperationException("Actor shadow caster material lacks explicit pass: " + material.name + "/" + caster.passName);
                    var meshFilter = renderer.GetComponent<MeshFilter>();
                    var skinned = renderer as SkinnedMeshRenderer;
                    var mesh = meshFilter != null ? meshFilter.sharedMesh : skinned != null ? skinned.sharedMesh : null;
                    if (mesh == null || caster.submeshIndex < 0 || caster.submeshIndex >= mesh.subMeshCount) continue;
                    ctx.cmd.SetGlobalFloat(CoverageId, caster.useMaterialCoverage ? 1 : 0);
                    ctx.cmd.DrawRenderer(renderer, material, caster.submeshIndex, pass);
                }
                ctx.cmd.DisableScissorRect();

            }
            CoreUtils.SetRenderTarget(ctx.cmd, ctx.cameraColorBuffer, ctx.cameraDepthBuffer);
        }

        void ReleaseTextures()
        {
            if (textures == null) return;
            foreach (var texture in textures) if (texture != null) CoreUtils.Destroy(texture);
            textures = null;
        }

        public void ResetReceiverBindings()
        {
            if (properties == null) properties = new MaterialPropertyBlock();
            foreach (var actor in actors ?? Array.Empty<Actor>())
                foreach (var receiver in actor?.receivers ?? Array.Empty<Receiver>())
                {
                    if (receiver?.renderer == null || receiver.materialIndex < 0 ||
                        receiver.materialIndex >= receiver.renderer.sharedMaterials.Length) continue;
                    receiver.renderer.GetPropertyBlock(properties, receiver.materialIndex);
                    properties.SetFloat(ValidId, 0);
                    receiver.renderer.SetPropertyBlock(properties, receiver.materialIndex);
                }
        }

        protected override void Cleanup()
        {
            RenderPipelineManager.beginCameraRendering -= BeginCamera;
            ResetReceiverBindings();
            ReleaseTextures();
        }
    }
}
