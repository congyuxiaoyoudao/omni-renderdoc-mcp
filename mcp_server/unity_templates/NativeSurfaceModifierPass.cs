using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;

namespace OmniRenderDoc
{
    /// <summary>
    /// Rebuilds a current-geometry classification prepass and applies explicit
    /// surface modifiers before HDRP transparents. Classification shaders own
    /// their original depth/stencil/coverage state; no captured image is used.
    /// Install in a CustomPassVolume at BeforeTransparent.
    /// </summary>
    [Serializable]
    public sealed class NativeSurfaceModifierPass : CustomPass
    {
        public enum PropertyKind { Float, Vector, Texture }
        [Serializable] public sealed class FloatProperty { public string name; public float value; }
        [Serializable] public sealed class TextureProperty { public string name; public Texture value; }
        [Serializable] public sealed class PropertyCopy
        {
            public string source, target;
            public PropertyKind kind;
            // A negative slot uses this draw's source material. Other slots
            // allow an auxiliary pass to track a related live material.
            public int sourceMaterialIndex = -1;
        }
        [Serializable] public sealed class RendererDraw
        {
            public Renderer renderer;
            public int materialIndex;
            public int submeshIndex;
            public string passName;
            public Material overrideMaterial;
            public FloatProperty[] floatProperties = Array.Empty<FloatProperty>();
            public TextureProperty[] textureProperties = Array.Empty<TextureProperty>();
            public PropertyCopy[] copyProperties = Array.Empty<PropertyCopy>();
        }

        public RendererDraw[] classifiers = Array.Empty<RendererDraw>();
        public RendererDraw[] modifiers = Array.Empty<RendererDraw>();
        public Material resolveMaterial;
        public string resolvePassName;
        // Optional private-stencil resolve for subsequent transparent/temporal
        // draws. The source profile supplies its stencil equation and pass.
        public string auxiliaryResolvePassName;
        public GraphicsFormat classificationFormat = GraphicsFormat.R16_SFloat;
        public float clearClassification;
        public uint clearStencil;
        public bool renderSceneView = true;
        public bool renderGame = true;

        sealed class CameraResources
        {
            public Camera camera;
            public int width, height;
            public GraphicsFormat format;
            public TextureDimension dimension;
            public RTHandle classification, mask, auxiliaryMask, depthStencil;
            public void Release()
            {
                classification?.Release(); mask?.Release(); auxiliaryMask?.Release(); depthStencil?.Release();
                classification = mask = auxiliaryMask = depthStencil = null;
            }
        }
        sealed class MaterialCopy
        {
            public Material source, instance;
        }
        sealed class DrawCommand
        {
            public Mesh mesh;
            public Matrix4x4 matrix;
            public Material material;
            public int submesh, pass;
            public MaterialPropertyBlock properties;
        }
        [NonSerialized] Dictionary<int, CameraResources> cameras;
        [NonSerialized] Dictionary<RendererDraw, MaterialCopy> materials;
        [NonSerialized] Dictionary<SkinnedMeshRenderer, Mesh> bakedMeshes;
        [NonSerialized] HashSet<SkinnedMeshRenderer> bakedThisExecution;
        [NonSerialized] HashSet<string> reported;
        [NonSerialized] MaterialCopy resolve;

        static readonly int ClassificationId = Shader.PropertyToID("_NativeSurfaceClassificationTexture");
        static readonly int MaskId = Shader.PropertyToID("_NativeSurfaceReceiverMask");
        static readonly int SizeId = Shader.PropertyToID("_NativeSurfaceMaskSize");
        static readonly int ValidId = Shader.PropertyToID("_NativeSurfaceMaskValid");
        static readonly int CameraDepthId = Shader.PropertyToID("_NativeSurfaceCameraDepthTexture");
        static readonly int CameraDepthSizeId = Shader.PropertyToID("_NativeSurfaceCameraDepthSize");
        static readonly int AuxiliaryId = Shader.PropertyToID("_NativeSurfaceAuxiliaryMask");
        static readonly int AuxiliaryValidId = Shader.PropertyToID("_NativeSurfaceAuxiliaryValid");
        static readonly int AuxiliarySizeId = Shader.PropertyToID("_NativeSurfaceAuxiliarySize");

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.SubsystemRegistration)]
        static void InstallCameraReset()
        {
            RenderPipelineManager.beginCameraRendering -= ResetCamera;
            RenderPipelineManager.beginCameraRendering += ResetCamera;
            Shader.SetGlobalFloat(AuxiliaryValidId, 0);
        }
#if UNITY_EDITOR
        [UnityEditor.InitializeOnLoadMethod]
        static void InstallEditorCameraReset() => InstallCameraReset();
#endif
        static void ResetCamera(ScriptableRenderContext context, Camera camera)
        {
            // Also runs for cameras/frames where this custom pass is disabled.
            var cmd = CommandBufferPool.Get("Reset current surface auxiliary mask");
            cmd.SetGlobalFloat(AuxiliaryValidId, 0);
            context.ExecuteCommandBuffer(cmd);
            CommandBufferPool.Release(cmd);
        }

        public NativeSurfaceModifierPass()
        {
            targetColorBuffer = TargetBuffer.None;
            targetDepthBuffer = TargetBuffer.None;
        }
        protected override void Setup(ScriptableRenderContext context, CommandBuffer cmd)
        {
            InstallCameraReset();
            cameras = new Dictionary<int, CameraResources>();
            materials = new Dictionary<RendererDraw, MaterialCopy>();
            bakedMeshes = new Dictionary<SkinnedMeshRenderer, Mesh>();
            bakedThisExecution = new HashSet<SkinnedMeshRenderer>();
            reported = new HashSet<string>();
            cmd.SetGlobalFloat(ValidId, 0);
        }
        bool Supports(Camera camera) => camera != null &&
            (camera.cameraType == CameraType.SceneView ? renderSceneView : camera.cameraType == CameraType.Game && renderGame);
        void Report(string message)
        {
            if (reported.Add(message)) Debug.LogWarning("Native surface modifier skipped: " + message);
        }
        static bool Reserved(string name) => name == "_NativeSurfaceClassificationTexture" ||
            name == "_NativeSurfaceReceiverMask" || name == "_NativeSurfaceMaskSize" ||
            name == "_NativeSurfaceMaskValid" || name == "_NativeSurfacePrivateDepthTexture" ||
            name == "_NativeSurfaceCameraDepthTexture" || name == "_NativeSurfaceCameraDepthSize" ||
            name == "_NativeSurfaceAuxiliaryMask" || name == "_NativeSurfaceAuxiliaryValid" || name == "_NativeSurfaceAuxiliarySize";
        static void PropertyName(string name)
        {
            if (string.IsNullOrWhiteSpace(name) || Reserved(name))
                throw new InvalidOperationException("Invalid or reserved profile property: " + name);
        }
        static Material Clone(Material source, ref MaterialCopy copy)
        {
            if (copy == null) copy = new MaterialCopy();
            if (copy.instance == null || copy.source != source || copy.instance.shader != source.shader)
            {
                CoreUtils.Destroy(copy.instance);
                copy.source = source;
                copy.instance = new Material(source) { hideFlags = HideFlags.HideAndDontSave };
            }
            else copy.instance.CopyPropertiesFromMaterial(source);
            return copy.instance;
        }
        Mesh CurrentMesh(Renderer renderer)
        {
            if (renderer is SkinnedMeshRenderer skinned)
            {
                if (skinned.sharedMesh == null) return null;
                if (!bakedMeshes.TryGetValue(skinned, out var baked) || baked == null)
                {
                    baked = new Mesh { name = "Current surface classification skin", hideFlags = HideFlags.HideAndDontSave };
                    bakedMeshes[skinned] = baked;
                }
                if (bakedThisExecution.Add(skinned)) skinned.BakeMesh(baked, false);
                return baked;
            }
            if (!(renderer is MeshRenderer))
                throw new InvalidOperationException("Only MeshRenderer and SkinnedMeshRenderer inputs are supported.");
            return renderer.GetComponent<MeshFilter>()?.sharedMesh;
        }
        List<DrawCommand> Prepare(RendererDraw[] entries, Camera camera)
        {
            var commands = new List<DrawCommand>();
            foreach (var entry in entries ?? Array.Empty<RendererDraw>())
            {
                if (entry == null) throw new InvalidOperationException("Null renderer draw entry.");
                var renderer = entry.renderer;
                if (renderer == null || !renderer.enabled || renderer.forceRenderingOff || !renderer.gameObject.activeInHierarchy ||
                    (camera.cullingMask & (1 << renderer.gameObject.layer)) == 0) continue;
                var originals = renderer.sharedMaterials;
                if (entry.materialIndex < 0 || entry.materialIndex >= originals.Length || originals[entry.materialIndex] == null)
                    throw new InvalidOperationException("Invalid source material slot: " + renderer.name);
                Material original = originals[entry.materialIndex];
                var selected = entry.overrideMaterial != null ? entry.overrideMaterial : original;
                if (string.IsNullOrWhiteSpace(entry.passName) || selected.FindPass(entry.passName) < 0)
                    throw new InvalidOperationException("Required surface pass missing: " + selected.name + "/" + entry.passName);
                var mesh = CurrentMesh(renderer);
                if (mesh == null) continue;
                if (entry.submeshIndex < 0 || entry.submeshIndex >= mesh.subMeshCount)
                    throw new InvalidOperationException("Invalid source submesh: " + renderer.name);
                materials.TryGetValue(entry, out var copy);
                var material = Clone(selected, ref copy); materials[entry] = copy;
                var properties = new MaterialPropertyBlock();
                renderer.GetPropertyBlock(properties, entry.materialIndex);
                if (properties.isEmpty) renderer.GetPropertyBlock(properties);
                // Profile constants provide defaults. Explicit live copies are
                // applied last so edits to their source material/MPB win.
                foreach (var property in entry.floatProperties ?? Array.Empty<FloatProperty>())
                {
                    if (property == null || !float.IsFinite(property.value)) throw new InvalidOperationException("Invalid float override.");
                    PropertyName(property.name);
                    material.SetFloat(property.name, property.value); properties.SetFloat(property.name, property.value);
                }
                foreach (var property in entry.textureProperties ?? Array.Empty<TextureProperty>())
                {
                    if (property == null) throw new InvalidOperationException("Null texture override.");
                    PropertyName(property.name);
                    material.SetTexture(property.name, property.value); properties.SetTexture(property.name, property.value);
                }
                var sourceBlocks = new Dictionary<int, MaterialPropertyBlock>();
                foreach (var property in entry.copyProperties ?? Array.Empty<PropertyCopy>())
                {
                    if (property == null) throw new InvalidOperationException("Null property-copy entry.");
                    PropertyName(property.source); PropertyName(property.target);
                    int sourceIndex = property.sourceMaterialIndex < 0 ? entry.materialIndex : property.sourceMaterialIndex;
                    if (property.sourceMaterialIndex < -1 || sourceIndex >= originals.Length || originals[sourceIndex] == null)
                        throw new InvalidOperationException("Invalid property-copy source material slot: " + renderer.name);
                    Material sourceMaterial = originals[sourceIndex];
                    if (!sourceBlocks.TryGetValue(sourceIndex, out var sourceBlock))
                    {
                        sourceBlock = new MaterialPropertyBlock();
                        renderer.GetPropertyBlock(sourceBlock, sourceIndex);
                        if (sourceBlock.isEmpty) renderer.GetPropertyBlock(sourceBlock);
                        sourceBlocks[sourceIndex] = sourceBlock;
                    }
                    int source = Shader.PropertyToID(property.source), target = Shader.PropertyToID(property.target);
                    bool fromBlock = sourceBlock.HasProperty(source);
                    if (!fromBlock && !sourceMaterial.HasProperty(source))
                        throw new InvalidOperationException("Source material property missing: " + property.source);
                    switch (property.kind)
                    {
                        case PropertyKind.Float:
                            float number = fromBlock ? sourceBlock.GetFloat(source) : sourceMaterial.GetFloat(source);
                            material.SetFloat(target, number); properties.SetFloat(target, number); break;
                        case PropertyKind.Vector:
                            Vector4 vector = fromBlock ? sourceBlock.GetVector(source) : sourceMaterial.GetVector(source);
                            material.SetVector(target, vector); properties.SetVector(target, vector); break;
                        case PropertyKind.Texture:
                            Texture texture = fromBlock ? sourceBlock.GetTexture(source) : sourceMaterial.GetTexture(source);
                            material.SetTexture(target, texture); properties.SetTexture(target, texture); break;
                        default: throw new InvalidOperationException("Unknown copied property kind.");
                    }
                }
                commands.Add(new DrawCommand { mesh = mesh, matrix = renderer.localToWorldMatrix, material = material,
                    submesh = entry.submeshIndex, pass = material.FindPass(entry.passName), properties = properties });
            }
            return commands;
        }
        CameraResources Resources(Camera camera, int width, int height)
        {
            var dead = new List<int>();
            foreach (var item in cameras) if (item.Value.camera == null) { item.Value.Release(); dead.Add(item.Key); }
            foreach (int key in dead) cameras.Remove(key);
            int id = camera.GetInstanceID();
            if (!cameras.TryGetValue(id, out var resources)) { resources = new CameraResources(); cameras[id] = resources; }
            // TEXTURE2D_X uses arrays on D3D12 even for a non-XR camera.
            // Match the platform's HDRP shader declaration, with one view.
            var dimension = TextureXR.dimension;
            if (resources.camera != camera || resources.width != width || resources.height != height || resources.format != classificationFormat ||
                resources.dimension != dimension)
            {
                resources.Release(); resources.camera = camera; resources.width = width; resources.height = height;
                resources.format = classificationFormat; resources.dimension = dimension;
                resources.classification = RTHandles.Alloc(width, height, slices: 1, dimension: dimension, colorFormat: classificationFormat,
                    filterMode: FilterMode.Point, wrapMode: TextureWrapMode.Clamp, name: "Current surface classification " + id);
                resources.mask = RTHandles.Alloc(width, height, slices: 1, dimension: dimension, colorFormat: GraphicsFormat.R8_UNorm,
                    filterMode: FilterMode.Point, wrapMode: TextureWrapMode.Clamp, name: "Current surface receiver mask " + id);
                var descriptor = new RenderTextureDescriptor(width, height)
                {
                    graphicsFormat = GraphicsFormat.None, depthStencilFormat = GraphicsFormat.D32_SFloat_S8_UInt,
                    msaaSamples = 1, volumeDepth = 1, dimension = dimension,
                    useMipMap = false, autoGenerateMips = false, sRGB = false
                };
                resources.depthStencil = RTHandles.Alloc(descriptor, FilterMode.Point, TextureWrapMode.Clamp,
                    name: "Private surface depth stencil " + id);
            }
            if (!string.IsNullOrWhiteSpace(auxiliaryResolvePassName) && resources.auxiliaryMask == null)
                resources.auxiliaryMask = RTHandles.Alloc(width, height, slices: 1, dimension: dimension, colorFormat: GraphicsFormat.R8_UNorm,
                    filterMode: FilterMode.Point, wrapMode: TextureWrapMode.Clamp, name: "Current surface auxiliary mask " + id);
            return resources;
        }
        static void Draw(CommandBuffer cmd, List<DrawCommand> commands)
        {
            foreach (var draw in commands) cmd.DrawMesh(draw.mesh, draw.matrix, draw.material, draw.submesh, draw.pass, draw.properties);
        }
        protected override void Execute(CustomPassContext ctx)
        {
            var cmd = ctx.cmd; var camera = ctx.hdCamera.camera;
            cmd.SetGlobalFloat(ValidId, 0);
            cmd.SetGlobalFloat(AuxiliaryValidId, 0);
            if (!Supports(camera)) return;
            bool changedTargets = false;
            try
            {
                var depth = ctx.cameraDepthBuffer?.rt; var color = ctx.cameraColorBuffer?.rt;
                if (depth == null || color == null || depth.dimension != color.dimension ||
                    (depth.dimension != TextureDimension.Tex2D && depth.dimension != TextureDimension.Tex2DArray) ||
                    depth.volumeDepth != 1 || color.volumeDepth != 1 || depth.antiAliasing != 1 || color.antiAliasing != 1)
                    throw new InvalidOperationException("This surface pass requires non-MSAA, single-view 2D or 2D-array camera targets.");
                if (color.width != depth.width || color.height != depth.height)
                    throw new InvalidOperationException("Camera color/depth backing dimensions differ; paired RTHandle binding is unsupported.");
                int width = ctx.hdCamera.actualWidth, height = ctx.hdCamera.actualHeight;
                if (width < 1 || height < 1 || !float.IsFinite(clearClassification) || clearStencil > 255)
                    throw new InvalidOperationException("Invalid classification dimensions or clear values.");
                if ((classificationFormat != GraphicsFormat.R16_SFloat && classificationFormat != GraphicsFormat.R8_UNorm) ||
                    !SystemInfo.IsFormatSupported(classificationFormat, GraphicsFormatUsage.Render) ||
                    !SystemInfo.IsFormatSupported(GraphicsFormat.R8_UNorm, GraphicsFormatUsage.Render) ||
                    !SystemInfo.IsFormatSupported(GraphicsFormat.D32_SFloat_S8_UInt, GraphicsFormatUsage.Render))
                    throw new InvalidOperationException("Required classification/depth-stencil formats are unsupported.");
                if (resolveMaterial == null || string.IsNullOrWhiteSpace(resolvePassName) || resolveMaterial.FindPass(resolvePassName) < 0)
                    throw new InvalidOperationException("An explicit receiver-mask resolve material/pass is required.");
                bool auxiliary = !string.IsNullOrWhiteSpace(auxiliaryResolvePassName);
                if (auxiliary && resolveMaterial.FindPass(auxiliaryResolvePassName) < 0)
                    throw new InvalidOperationException("The configured auxiliary resolve pass is missing.");
                bakedThisExecution.Clear();
                var classificationDraws = Prepare(classifiers, camera);
                var modifierDraws = Prepare(modifiers, camera);
                var resources = Resources(camera, width, height);
                var resolveInstance = Clone(resolveMaterial, ref resolve);
                var viewport = new Rect(0, 0, width, height);
                changedTargets = true;
                CoreUtils.SetRenderTarget(cmd, resources.classification, resources.depthStencil, depthSlice: 0);
                cmd.SetViewport(viewport); cmd.DisableScissorRect();
                // Clear only private attachments. Unity's logical far-depth=1
                // converts to the correct reversed/non-reversed native value.
                cmd.ClearRenderTarget(RTClearFlags.All, new Color(clearClassification, 0, 0, 0), 1, clearStencil);
                Draw(cmd, classificationDraws);

                CoreUtils.SetRenderTarget(cmd, resources.mask, resources.depthStencil, depthSlice: 0);
                cmd.SetViewport(viewport);
                cmd.ClearRenderTarget(RTClearFlags.Color, Color.clear, 1, 0);
                cmd.SetGlobalTexture(ClassificationId, resources.classification);
                // The private stencil attachment remains bound during resolve.
                // Do not bind its depth as an SRV: this candidate only resolves
                // source classification/stencil and uses real camera depth for
                // the final modifier's hardware occlusion test.
                cmd.SetGlobalTexture(CameraDepthId, ctx.cameraDepthBuffer, RenderTextureSubElement.Depth);
                cmd.SetGlobalVector(SizeId, new Vector4(width, height, 1f / width, 1f / height));
                cmd.SetGlobalVector(CameraDepthSizeId, new Vector4(depth.width, depth.height, 1f / depth.width, 1f / depth.height));
                CoreUtils.DrawFullScreen(cmd, resolveInstance, shaderPassId: resolveInstance.FindPass(resolvePassName));

                if (auxiliary)
                {
                    CoreUtils.SetRenderTarget(cmd, resources.auxiliaryMask, resources.depthStencil, depthSlice: 0);
                    cmd.SetViewport(viewport);
                    cmd.ClearRenderTarget(RTClearFlags.Color, Color.clear, 1, 0);
                    CoreUtils.DrawFullScreen(cmd, resolveInstance, shaderPassId: resolveInstance.FindPass(auxiliaryResolvePassName));
                    cmd.SetGlobalTexture(AuxiliaryId, resources.auxiliaryMask);
                    cmd.SetGlobalVector(AuxiliarySizeId, new Vector4(width, height, 1f / width, 1f / height));
                    cmd.SetGlobalFloat(AuxiliaryValidId, 1);
                }

                CoreUtils.SetRenderTarget(cmd, ctx.cameraColorBuffer, ctx.cameraDepthBuffer, depthSlice: 0);
                cmd.SetViewport(viewport);
                cmd.SetGlobalTexture(MaskId, resources.mask);
                cmd.SetGlobalFloat(ValidId, 1);
                Draw(cmd, modifierDraws);
            }
            catch (InvalidOperationException error) { cmd.SetGlobalFloat(AuxiliaryValidId, 0); Report(error.Message); }
            finally
            {
                cmd.SetGlobalFloat(ValidId, 0);
                if (changedTargets)
                {
                    CoreUtils.SetRenderTarget(cmd, ctx.cameraColorBuffer, ctx.cameraDepthBuffer, depthSlice: 0);
                    cmd.SetViewport(new Rect(0, 0, ctx.hdCamera.actualWidth, ctx.hdCamera.actualHeight));
                    cmd.DisableScissorRect();
                }
            }
        }
        CameraResources ForCamera(Camera camera) => camera != null && cameras != null &&
            cameras.TryGetValue(camera.GetInstanceID(), out var resources) && resources.camera == camera ? resources : null;
        public RenderTexture GetClassificationTexture(Camera camera) => ForCamera(camera)?.classification?.rt;
        public RenderTexture GetReceiverMaskTexture(Camera camera) => ForCamera(camera)?.mask?.rt;
        public RenderTexture GetAuxiliaryMaskTexture(Camera camera) => ForCamera(camera)?.auxiliaryMask?.rt;
        public RenderTexture GetPrivateDepthStencilTexture(Camera camera) => ForCamera(camera)?.depthStencil?.rt;
        protected override void Cleanup()
        {
            Shader.SetGlobalFloat(AuxiliaryValidId, 0);
            if (cameras != null) foreach (var item in cameras.Values) item.Release();
            cameras?.Clear();
            if (materials != null) foreach (var item in materials.Values) CoreUtils.Destroy(item.instance);
            materials?.Clear(); CoreUtils.Destroy(resolve?.instance); resolve = null;
            if (bakedMeshes != null) foreach (var mesh in bakedMeshes.Values) CoreUtils.Destroy(mesh);
            bakedMeshes?.Clear(); bakedThisExecution?.Clear();
        }
    }
}
