using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;

namespace OmniRenderDoc
{
    // Install in a global volume at AfterOpaqueDepthAndNormal. The source
    // algorithm must adapt the *current* prepass inputs, never captured images.
    [Serializable]
    public sealed class NativeScreenAoPass : CustomPass
    {
        public NativeScreenAoProfile profile;
        public bool renderSceneView = true;
        public bool renderGame = true;

        public NativeScreenAoPass()
        {
            targetColorBuffer = TargetBuffer.None;
            targetDepthBuffer = TargetBuffer.None;
        }

        sealed class CameraResources
        {
            public Camera camera;
            public int width, height;
            public string layout;
            public bool historyValid;
            public bool historyWasUsed;
            public int historyFrames, lastRenderSerial;
            public long lastContinuityGeneration;
            public readonly Dictionary<string, RenderTexture> previous = new Dictionary<string, RenderTexture>();
            public readonly Dictionary<string, RenderTexture> textures = new Dictionary<string, RenderTexture>();
            public void Release()
            {
                foreach (var texture in textures.Values) CoreUtils.Destroy(texture);
                textures.Clear();
                foreach(var texture in previous.Values) CoreUtils.Destroy(texture);
                previous.Clear();historyValid=false;historyWasUsed=false;historyFrames=0;
            }
        }

        [NonSerialized] Dictionary<HDCamera, CameraResources> cameras;
        [NonSerialized] HashSet<string> reported;
        static readonly int OutputId = Shader.PropertyToID("_NativeEnvironmentAOTexture");
        static readonly int ValidId = Shader.PropertyToID("_NativeEnvironmentAOValid");
        static readonly int SizeId = Shader.PropertyToID("_NativeEnvironmentAOSize");

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.SubsystemRegistration)]
        static void InstallCameraReset()
        {
            RenderPipelineManager.beginCameraRendering -= ResetCamera;
            RenderPipelineManager.beginCameraRendering += ResetCamera;
            Shader.SetGlobalFloat(ValidId, 0);
            Shader.SetGlobalTexture(OutputId, Texture2D.whiteTexture);
        }

#if UNITY_EDITOR
        [UnityEditor.InitializeOnLoadMethod]
        static void InstallEditorCameraReset() => InstallCameraReset();
#endif

        static void ResetCamera(ScriptableRenderContext context, Camera camera)
        {
            // Runs even when a volume/pass is disabled or this camera does not
            // execute custom passes. Queue the reset in camera command order;
            // do not mutate per-renderer MPBs after HDRP has culled the scene.
            var cmd = CommandBufferPool.Get("Reset native screen AO for current camera");
            cmd.SetGlobalFloat(ValidId, 0);
            cmd.SetGlobalTexture(OutputId, Texture2D.whiteTexture);
            context.ExecuteCommandBuffer(cmd);
            CommandBufferPool.Release(cmd);
        }

        protected override void Setup(ScriptableRenderContext context, CommandBuffer cmd)
        {
            InstallCameraReset();
            cameras = new Dictionary<HDCamera, CameraResources>();
            reported = new HashSet<string>();
            cmd.SetGlobalFloat(ValidId, 0);
        }

        bool SupportsCamera(Camera camera) => camera != null &&
            (camera.cameraType == CameraType.SceneView ? renderSceneView :
             camera.cameraType == CameraType.Game && renderGame);

        void Report(string reason)
        {
            if (reported.Add(reason)) Debug.LogWarning("Native screen AO remains neutral: " + reason);
        }

        static Vector4 Size(int width, int height) => new Vector4(width, height, 1f / width, 1f / height);
        static int DivideRoundUp(int value, int divisor) => (value + divisor - 1) / divisor;
        static bool Finite(Vector4 value) => float.IsFinite(value.x) && float.IsFinite(value.y) &&
            float.IsFinite(value.z) && float.IsFinite(value.w);
        static bool ParameterName(string value) => !string.IsNullOrWhiteSpace(value) &&
            !value.StartsWith("_NativeAOCurrent", StringComparison.Ordinal) && value != "_NativeAOStageViewport";

        string Validate(int width, int height)
        {
            if (profile.resources == null || profile.resources.Length == 0 ||
                profile.stages == null || profile.stages.Length == 0) return "profile resource chain is empty";
            var names = new Dictionary<string, NativeScreenAoProfile.Resource>();
            var initialized = new HashSet<string>();
            var computed = new HashSet<string>();
            foreach (var resource in profile.resources)
            {
                if (resource == null || string.IsNullOrWhiteSpace(resource.name) || names.ContainsKey(resource.name) ||
                    resource.resolutionDivisor < 1 || resource.mipCount < 1)
                    return "profile has duplicate names or invalid resource dimensions";
                int w = DivideRoundUp(width, resource.resolutionDivisor), h = DivideRoundUp(height, resource.resolutionDivisor);
                int maxMipCount = 1 + Mathf.FloorToInt(Mathf.Log(Mathf.Max(w, h), 2));
                if (resource.mipCount > maxMipCount) return "camera is too small for the configured depth mip chain";
                if (!SystemInfo.IsFormatSupported(resource.format, GraphicsFormatUsage.LoadStore))
                    return "random-write format is unsupported: " + resource.format;
                if (!Finite(resource.clearValue)) return "resource clear value is not finite";
                names.Add(resource.name, resource);
                if (resource.clearBeforeUse)
                    for (int mip = 0; mip < resource.mipCount; mip++) initialized.Add(resource.name + ":" + mip);
            }
            if (string.IsNullOrEmpty(profile.outputResource) || !names.TryGetValue(profile.outputResource, out var output) ||
                output.resolutionDivisor != 1 || output.mipCount != 1 || output.format != GraphicsFormat.R8_UNorm)
                return "output must be a full-resolution single-mip R8_UNorm texture";
            foreach (var stage in profile.stages)
            {
                var program = stage?.program != null ? stage.program : profile.program;
                if (stage == null || program == null || string.IsNullOrEmpty(stage.kernel) || !program.HasKernel(stage.kernel) ||
                    string.IsNullOrEmpty(stage.dispatchResource) || !names.TryGetValue(stage.dispatchResource, out var domain) ||
                    stage.dispatchMip < 0 || stage.dispatchMip >= domain.mipCount ||
                    stage.pixelsPerGroup.x < 0 || stage.pixelsPerGroup.y < 0)
                    return "profile kernel or dispatch extent is invalid";
                program.GetKernelThreadGroupSizes(program.FindKernel(stage.kernel), out uint groupX, out uint groupY, out uint groupZ);
                if (groupX == 0 || groupY == 0 || groupZ != 1) return "profile requires nonempty 2D thread groups with Z equal to one";
                var reads = new HashSet<string>();
                var writes = new HashSet<string>();
                var readResources = new HashSet<string>();
                var writeResources = new HashSet<string>();
                var properties = new HashSet<string>();
                foreach (var binding in stage.textures ?? Array.Empty<NativeScreenAoProfile.TextureBinding>())
                {
                    if (binding == null || !ParameterName(binding.property) || !properties.Add(binding.property) ||
                        binding.writeMip < -1 || binding.readMipStart < 0 || binding.readMipCount < 0)
                        return "texture binding is invalid";
                    if (binding.input == NativeScreenAoProfile.Input.Resource)
                    {
                        if (string.IsNullOrEmpty(binding.resource) || !names.TryGetValue(binding.resource, out var resource) ||
                            binding.writeMip >= resource.mipCount) return "texture binding references an unavailable resource mip";
                        if (binding.writeMip >= 0)
                        {
                            if (binding.readMipStart != 0 || binding.readMipCount != 0)
                                return "a write binding cannot also declare a read range";
                            if (!writes.Add(binding.resource + ":" + binding.writeMip))
                                return "one stage aliases multiple writes to the same resource mip";
                            writeResources.Add(binding.resource);
                        }
                        else
                        {
                            int count = binding.readMipCount == 0 ? resource.mipCount - binding.readMipStart : binding.readMipCount;
                            if (binding.readMipStart >= resource.mipCount || count > resource.mipCount - binding.readMipStart)
                                return "read binding references an unavailable resource mip range";
                            for (int mip = binding.readMipStart; mip < binding.readMipStart + count; mip++)
                                reads.Add(binding.resource + ":" + mip);
                            readResources.Add(binding.resource);
                        }
                    }
                    else if (binding.input == NativeScreenAoProfile.Input.PreviousHistory)
                    {
                        if (string.IsNullOrEmpty(binding.resource) || !names.TryGetValue(binding.resource, out var prior) || !prior.retainHistory || prior.mipCount != 1 ||
                            binding.writeMip >= 0 || binding.readMipStart != 0 || binding.readMipCount > 1)
                            return "history input requires a retained single-mip resource";
                    }
                    else if (binding.input != NativeScreenAoProfile.Input.CameraDepth && binding.input != NativeScreenAoProfile.Input.CameraNormal &&
                        binding.input != NativeScreenAoProfile.Input.SourceMotion && binding.input != NativeScreenAoProfile.Input.SourceNormal &&
                        binding.input != NativeScreenAoProfile.Input.SourceDepth)
                        return "unknown texture input kind";
                    else if (binding.writeMip >= 0 || binding.readMipStart != 0 || binding.readMipCount > 1)
                        return "camera inputs are read-only single-mip bindings";
                }
                // Unity's normal texture read binding exposes the entire SRV.
                // A declared narrower read range proves initialization only;
                // it does not create a disjoint subresource SRV for D3D.
                foreach (string resource in readResources)
                    if (writeResources.Contains(resource)) return "same-stage texture read/write feedback is unsupported: " + resource;
                foreach (string read in reads)
                {
                    if (writes.Contains(read)) return "same-stage resource feedback is unsupported: " + read;
                    if (!initialized.Contains(read)) return "resource mip is read before it is written or explicitly cleared: " + read;
                }
                if (writes.Count == 0) return "each compute stage must declare its output writes";
                foreach (string write in writes) { initialized.Add(write); computed.Add(write); }
                foreach (var binding in stage.vectors ?? Array.Empty<NativeScreenAoProfile.VectorBinding>())
                    if (binding == null || !ParameterName(binding.property) || !Finite(binding.value))
                        return "vector binding is invalid or overrides reserved camera data";
                foreach (var binding in stage.vectorArrays ?? Array.Empty<NativeScreenAoProfile.VectorArrayBinding>())
                {
                    if (binding == null || !ParameterName(binding.property) || binding.values == null || binding.values.Length == 0)
                        return "vector-array binding is invalid";
                    foreach (var vector in binding.values) if (!Finite(vector)) return "vector-array binding is not finite";
                }
            }
            if (!computed.Contains(profile.outputResource + ":0")) return "no compute stage writes the final AO output";
            foreach(var resource in profile.resources)
                if(resource.retainHistory && !computed.Contains(resource.name+":0"))
                    return "retained history must be produced by a current compute stage: "+resource.name;
            return null;
        }

        CameraResources GetResources(HDCamera hdCamera, int width, int height)
        {
            // Include the full layout so inspector edits invalidate allocations.
            string layout = profile.GetInstanceID() + ":" + JsonUtility.ToJson(profile);
            Camera camera = hdCamera.camera;
            HDCamera id = hdCamera;
            var dead = new List<HDCamera>();
            foreach (var entry in cameras) if (entry.Value.camera == null) dead.Add(entry.Key);
            foreach (var key in dead) { cameras[key].Release(); cameras.Remove(key); }
            if (cameras.TryGetValue(id, out var resources) &&
                (resources.width != width || resources.height != height || resources.layout != layout))
            {
                resources.Release(); cameras.Remove(id); resources = null;
            }
            if (resources != null) return resources;
            resources = new CameraResources { camera = camera, width = width, height = height, layout = layout };
            try
            {
                foreach (var spec in profile.resources)
                {
                    int w = DivideRoundUp(width, spec.resolutionDivisor), h = DivideRoundUp(height, spec.resolutionDivisor);
                    var descriptor = new RenderTextureDescriptor(w, h)
                    {
                        graphicsFormat = spec.format,
                        depthStencilFormat = GraphicsFormat.None,
                        dimension = TextureDimension.Tex2D,
                        volumeDepth = 1,
                        msaaSamples = 1,
                        enableRandomWrite = true,
                        useMipMap = spec.mipCount > 1,
                        mipCount = spec.mipCount,
                        autoGenerateMips = false,
                        sRGB = false,
                    };
                    var texture = new RenderTexture(descriptor)
                    {
                        name = "Native AO " + camera.name + ": " + spec.name,
                        hideFlags = HideFlags.HideAndDontSave,
                        filterMode = spec.filterMode,
                        wrapMode = TextureWrapMode.Clamp,
                    };
                    resources.textures.Add(spec.name, texture);
                    if (!texture.Create()) throw new InvalidOperationException("Could not allocate " + texture.name);
                    if(spec.retainHistory)
                    {
                        if(spec.mipCount!=1) throw new InvalidOperationException("AO history requires a single-mip resource.");
                        var history=new RenderTexture(descriptor) { name=texture.name+" previous", hideFlags=HideFlags.HideAndDontSave,
                            filterMode=spec.filterMode,wrapMode=TextureWrapMode.Clamp };
                        resources.previous.Add(spec.name,history);
                        if(!history.Create()) throw new InvalidOperationException("Could not allocate AO history.");
                    }
                }
                cameras.Add(id, resources);
                return resources;
            }
            catch { resources.Release(); throw; }
        }

        static void BindCamera(CommandBuffer cmd, ComputeShader program, CustomPassContext ctx)
        {
            var hd = ctx.hdCamera;
            var view = hd.mainViewConstants;
            // These are HDRP's current GPU matrices, including current camera
            // jitter and camera-relative coordinates. Camera.projectionMatrix or
            // a saved capture matrix would not describe the prepass buffers.
            cmd.SetComputeMatrixParam(program, "_NativeAOCurrentProjection", view.projMatrix);
            cmd.SetComputeMatrixParam(program, "_NativeAOCurrentInverseProjection", view.invProjMatrix);
            cmd.SetComputeMatrixParam(program, "_NativeAOCurrentWorldToView", view.viewMatrix);
            cmd.SetComputeMatrixParam(program, "_NativeAOCurrentViewToWorld", view.invViewMatrix);
            cmd.SetComputeMatrixParam(program, "_NativeAOCurrentViewProjection", view.viewProjMatrix);
            cmd.SetComputeMatrixParam(program, "_NativeAOCurrentInverseViewProjection", view.invViewProjMatrix);
            cmd.SetComputeVectorParam(program, "_NativeAOCurrentCameraPosition", view.worldSpaceCameraPos);
            cmd.SetComputeVectorParam(program, "_NativeAOCurrentViewport", Size(hd.actualWidth, hd.actualHeight));
            cmd.SetComputeVectorParam(program, "_NativeAOCurrentDepthTextureSize", Size(ctx.cameraDepthBuffer.rt.width, ctx.cameraDepthBuffer.rt.height));
            cmd.SetComputeVectorParam(program, "_NativeAOCurrentNormalTextureSize", Size(ctx.cameraNormalBuffer.rt.width, ctx.cameraNormalBuffer.rt.height));
            cmd.SetComputeVectorParam(program, "_NativeAOCurrentNearFar", new Vector4(hd.camera.nearClipPlane, hd.camera.farClipPlane, 0, 0));
            cmd.SetComputeVectorParam(program, "_NativeAOCurrentConventions", new Vector4(
                SystemInfo.usesReversedZBuffer ? 1 : 0, SystemInfo.graphicsUVStartsAtTop ? 1 : 0,
                hd.camera.orthographic ? 1 : 0, 0));
        }

        static void InitializeResources(CommandBuffer cmd, NativeScreenAoProfile profile, CameraResources resources)
        {
            foreach (var spec in profile.resources)
            {
                if (!spec.clearBeforeUse) continue;
                var texture = resources.textures[spec.name];
                for (int mip = 0; mip < spec.mipCount; mip++)
                {
                    // Color only: never attach the full-size camera depth to a
                    // half-size AO target. These are fixed exact-viewport RTs.
                    cmd.SetRenderTarget(texture, mip);
                    cmd.SetViewport(new Rect(0, 0, Mathf.Max(1, texture.width >> mip), Mathf.Max(1, texture.height >> mip)));
                    cmd.ClearRenderTarget(false, true, spec.clearValue);
                }
            }
        }

        protected override void Execute(CustomPassContext ctx)
        {
            // A failed, skipped or unvalidated pipeline is neutral for this
            // camera. Last frame's output is never silently reused.
            ctx.cmd.SetGlobalFloat(ValidId, 0);
            if (!SupportsCamera(ctx.hdCamera.camera) || profile == null || !profile.validatedForLiveInputs) return;
            if (injectionPoint != CustomPassInjectionPoint.AfterOpaqueDepthAndNormal)
            { Report("injection point must be AfterOpaqueDepthAndNormal"); return; }
            if (targetColorBuffer != TargetBuffer.None || targetDepthBuffer != TargetBuffer.None)
            { Report("compute producer targetColorBuffer and targetDepthBuffer must both be None"); return; }
            if (ctx.hdCamera.camera.orthographic && !profile.supportsOrthographic)
            { Report("the source projection adaptation has not been validated for orthographic cameras"); return; }
            if (ctx.hdCamera.msaaEnabled || ctx.hdCamera.camera.stereoEnabled ||
                ctx.cameraDepthBuffer?.rt == null || ctx.cameraNormalBuffer?.rt == null ||
                ctx.cameraDepthBuffer.rt.dimension != ctx.cameraNormalBuffer.rt.dimension ||
                (ctx.cameraDepthBuffer.rt.dimension != TextureDimension.Tex2D &&
                 (ctx.cameraDepthBuffer.rt.dimension != TextureDimension.Tex2DArray ||
                  ctx.cameraDepthBuffer.rt.volumeDepth != 1 || ctx.cameraNormalBuffer.rt.volumeDepth != 1)))
            { Report("this source adaptation requires non-MSAA, non-XR matching 2D or single-slice-array depth and normal inputs"); return; }
            int width = ctx.hdCamera.actualWidth, height = ctx.hdCamera.actualHeight;
            if (width < 1 || height < 1) return;
            string invalid = Validate(width, height);
            if (invalid != null) { Report(invalid); return; }
            CameraResources resources;
            try { resources = GetResources(ctx.hdCamera, width, height); }
            catch (Exception error) { Report(error.Message); return; }
            bool freshGeometry=NativeTemporalGeometryPass.TryGetCurrent(ctx.hdCamera,width,height,out var geometry);
            bool historyValid=profile.temporalHistoryEnabled && freshGeometry && geometry.previousFrameValid && resources.historyValid &&
                geometry.renderSerial == resources.lastRenderSerial + 1 &&
                geometry.continuityGeneration == resources.lastContinuityGeneration;
            resources.historyWasUsed=historyValid;
            if(!historyValid)
            {
                resources.historyFrames=0;
                foreach(var prior in resources.previous.Values)
                {
                    ctx.cmd.SetRenderTarget(prior);ctx.cmd.SetViewport(new Rect(0,0,prior.width,prior.height));
                    ctx.cmd.ClearRenderTarget(false,true,Color.clear);
                }
            }
            InitializeResources(ctx.cmd, profile, resources);
            foreach (var stage in profile.stages)
            {
                var program = stage.program != null ? stage.program : profile.program;
                var arrayKeyword = program.keywordSpace.FindKeyword("NATIVE_AO_INPUT_ARRAY");
                if (arrayKeyword.isValid)
                    ctx.cmd.SetKeyword(program, arrayKeyword, ctx.cameraDepthBuffer.rt.dimension == TextureDimension.Tex2DArray);
                BindCamera(ctx.cmd, program, ctx);
                ctx.cmd.SetComputeFloatParam(program,"_NativeAOCurrentHistoryValid",historyValid?1:0);
                ctx.cmd.SetComputeFloatParam(program,"_NativeAOCurrentSourceNormalValid",freshGeometry?1:0);
                ctx.cmd.SetComputeFloatParam(program,"_NativeAOCurrentSourceDepthValid",freshGeometry?1:0);
                ctx.cmd.SetComputeMatrixParam(program,"_NativeAOCurrentSourceNormalToWorld",profile.sourceNormalToWorld);
                int kernel = program.FindKernel(stage.kernel);
                foreach (var binding in stage.textures ?? Array.Empty<NativeScreenAoProfile.TextureBinding>())
                {
                    if (binding.input == NativeScreenAoProfile.Input.CameraDepth)
                        ctx.cmd.SetComputeTextureParam(program, kernel, binding.property, ctx.cameraDepthBuffer);
                    else if (binding.input == NativeScreenAoProfile.Input.CameraNormal)
                        ctx.cmd.SetComputeTextureParam(program, kernel, binding.property, ctx.cameraNormalBuffer);
                    else if (binding.input == NativeScreenAoProfile.Input.PreviousHistory)
                        ctx.cmd.SetComputeTextureParam(program,kernel,binding.property,resources.previous[binding.resource]);
                    else if (binding.input == NativeScreenAoProfile.Input.SourceMotion)
                        ctx.cmd.SetComputeTextureParam(program,kernel,binding.property,freshGeometry?(Texture)geometry.motionClass:Texture2D.grayTexture);
                    else if (binding.input == NativeScreenAoProfile.Input.SourceNormal)
                        ctx.cmd.SetComputeTextureParam(program,kernel,binding.property,freshGeometry?(Texture)geometry.sourceNormal:Texture2D.blackTexture);
                    else if (binding.input == NativeScreenAoProfile.Input.SourceDepth)
                        ctx.cmd.SetComputeTextureParam(program,kernel,binding.property,freshGeometry?(Texture)geometry.currentDepth:Texture2D.blackTexture);
                    else if (binding.writeMip >= 0)
                        ctx.cmd.SetComputeTextureParam(program, kernel, binding.property, resources.textures[binding.resource], binding.writeMip);
                    else ctx.cmd.SetComputeTextureParam(program, kernel, binding.property, resources.textures[binding.resource]);
                }
                foreach (var binding in stage.vectors ?? Array.Empty<NativeScreenAoProfile.VectorBinding>())
                    ctx.cmd.SetComputeVectorParam(program, binding.property, binding.value);
                foreach (var binding in stage.vectorArrays ?? Array.Empty<NativeScreenAoProfile.VectorArrayBinding>())
                    ctx.cmd.SetComputeVectorArrayParam(program, binding.property, binding.values);
                var domain = resources.textures[stage.dispatchResource];
                int w = Mathf.Max(1, domain.width >> stage.dispatchMip), h = Mathf.Max(1, domain.height >> stage.dispatchMip);
                program.GetKernelThreadGroupSizes(kernel, out uint x, out uint y, out uint z);
                if (z != 1) { Report("this 2D profile requires a thread-group Z size of one"); return; }
                int groupX = stage.pixelsPerGroup.x > 0 ? stage.pixelsPerGroup.x : (int)x;
                int groupY = stage.pixelsPerGroup.y > 0 ? stage.pixelsPerGroup.y : (int)y;
                ctx.cmd.SetComputeVectorParam(program, "_NativeAOStageViewport", Size(w, h));
                ctx.cmd.DispatchCompute(program, kernel, DivideRoundUp(w, groupX), DivideRoundUp(h, groupY), 1);
            }
            foreach(var prior in resources.previous) ctx.cmd.CopyTexture(resources.textures[prior.Key],prior.Value);
            resources.historyValid=freshGeometry;
            resources.lastRenderSerial=freshGeometry?geometry.renderSerial:0;
            resources.lastContinuityGeneration=freshGeometry?geometry.continuityGeneration:0;
            if(freshGeometry) resources.historyFrames++;
            ctx.cmd.SetGlobalTexture(OutputId, resources.textures[profile.outputResource]);
            ctx.cmd.SetGlobalVector(SizeId, Size(width, height));
            ctx.cmd.SetGlobalFloat(ValidId, 1);
        }

        public bool TryGetHistoryStatus(HDCamera camera,out bool valid,out int frames)
        {
            valid=false;frames=0;
            if(cameras==null || !cameras.TryGetValue(camera,out var state)) return false;
            valid=state.historyValid;frames=state.historyFrames;return true;
        }

        public bool TryGetHistoryDiagnostics(HDCamera camera,out bool used,out int frames,out int serial,out long generation)
        {
            used=false;frames=0;serial=0;generation=0;
            if(cameras==null || !cameras.TryGetValue(camera,out var state)) return false;
            used=state.historyWasUsed;frames=state.historyFrames;serial=state.lastRenderSerial;
            generation=state.lastContinuityGeneration;return true;
        }

        protected override void Cleanup()
        {
            if (cameras != null) foreach (var resources in cameras.Values) resources.Release();
            cameras?.Clear();
            Shader.SetGlobalFloat(ValidId, 0);
            Shader.SetGlobalTexture(OutputId, Texture2D.whiteTexture);
        }
    }
}
