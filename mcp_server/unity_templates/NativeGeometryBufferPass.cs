using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;
namespace OmniRenderDoc
{
    // Ordered current-mesh producer. Shader passes own encoding, coverage and
    // depth/stencil state; consumers and property names come from the scene.
    [Serializable]
    public sealed class NativeGeometryBufferPass : CustomPass
    {
        [Serializable] public sealed class Draw
        {
            public Renderer renderer; public int materialIndex,submeshIndex; public string passName;
            public NativeSurfaceModifierPass.FloatProperty[] floatProperties=Array.Empty<NativeSurfaceModifierPass.FloatProperty>();
            public NativeSurfaceModifierPass.TextureProperty[] textureProperties=Array.Empty<NativeSurfaceModifierPass.TextureProperty>();
            public NativeSurfaceModifierPass.PropertyCopy[] copyProperties=Array.Empty<NativeSurfaceModifierPass.PropertyCopy>();
        }
        public Draw[] draws=Array.Empty<Draw>();
        public string colorProperty,depthProperty,validProperty,sizeProperty;
        public GraphicsFormat format=GraphicsFormat.A2B10G10R10_UNormPack32;
        public Color clearColor=Color.clear;
        public bool renderGame=true,renderSceneView=true;
        sealed class Frame:IDisposable
        {
            public Camera camera;public RenderTexture color,depth;public int width,height;
            public void Dispose(){foreach(var t in new[]{color,depth}){if(t)t.Release();CoreUtils.Destroy(t);}}
        }
        readonly Dictionary<Camera,Frame> frames=new Dictionary<Camera,Frame>();
        readonly Dictionary<SkinnedMeshRenderer,Mesh> skins=new Dictionary<SkinnedMeshRenderer,Mesh>();
        static readonly HashSet<NativeGeometryBufferPass> owners=new HashSet<NativeGeometryBufferPass>();
        readonly HashSet<string> warnings=new HashSet<string>();
        public NativeGeometryBufferPass(){targetColorBuffer=TargetBuffer.None;targetDepthBuffer=TargetBuffer.None;}
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.SubsystemRegistration)]
        static void Register(){RenderPipelineManager.beginCameraRendering-=Begin;RenderPipelineManager.beginCameraRendering+=Begin;}
#if UNITY_EDITOR
        [UnityEditor.InitializeOnLoadMethod] static void RegisterEditor()=>Register();
#endif
        static void Begin(ScriptableRenderContext context,Camera camera)
        {
            var cmd=CommandBufferPool.Get("Reset current geometry buffers");
            foreach(var owner in owners)if(!string.IsNullOrWhiteSpace(owner.validProperty))cmd.SetGlobalFloat(owner.validProperty,0);
            context.ExecuteCommandBuffer(cmd);CommandBufferPool.Release(cmd);
        }
        protected override void Setup(ScriptableRenderContext context,CommandBuffer cmd){owners.Add(this);Register();}
        static RenderTexture Allocate(int w,int h,GraphicsFormat color,GraphicsFormat depth)
        {
            var rt=new RenderTexture(new RenderTextureDescriptor(w,h){graphicsFormat=color,depthStencilFormat=depth,msaaSamples=1})
              {hideFlags=HideFlags.HideAndDontSave,filterMode=FilterMode.Point,wrapMode=TextureWrapMode.Clamp};
            if(!rt.Create())throw new InvalidOperationException("Cannot allocate current geometry buffer");return rt;
        }
        protected override void Execute(CustomPassContext ctx)
        {
            var camera=ctx.hdCamera.camera;var cmd=ctx.cmd;
            if(!string.IsNullOrWhiteSpace(validProperty))cmd.SetGlobalFloat(validProperty,0);
            if(camera.stereoEnabled||(camera.cameraType==CameraType.SceneView?!renderSceneView:camera.cameraType!=CameraType.Game||!renderGame))return;
            bool changedTarget=false;
            try
            {
                var names=new HashSet<string>();foreach(var name in new[]{colorProperty,depthProperty,validProperty,sizeProperty})
                    if(string.IsNullOrWhiteSpace(name)||!name.StartsWith("_",StringComparison.Ordinal)||!names.Add(name))throw new InvalidOperationException("Distinct geometry buffer property names are required");
                var dead=new List<Camera>();foreach(var pair in frames)if(!pair.Key)dead.Add(pair.Key);
                foreach(var key in dead){frames[key].Dispose();frames.Remove(key);}
                var deadSkins=new List<SkinnedMeshRenderer>();foreach(var pair in skins)if(!pair.Key)deadSkins.Add(pair.Key);
                foreach(var key in deadSkins){CoreUtils.Destroy(skins[key]);skins.Remove(key);}
                int w=ctx.hdCamera.actualWidth,h=ctx.hdCamera.actualHeight;
                if(!SystemInfo.IsFormatSupported(format,GraphicsFormatUsage.Render))throw new InvalidOperationException("Unsupported geometry color format");
                if(!frames.TryGetValue(camera,out var frame)||!frame.color||!frame.depth||frame.width!=w||frame.height!=h||frame.color.graphicsFormat!=format)
                {
                    frame?.Dispose();frame=new Frame{camera=camera,width=w,height=h};frames[camera]=frame;
                    frame.color=Allocate(w,h,format,GraphicsFormat.None);frame.depth=Allocate(w,h,GraphicsFormat.None,GraphicsFormat.D32_SFloat_S8_UInt);
                }
                // Unbind the last rendering's SRV before writing this camera's attachment.
                cmd.SetGlobalTexture(colorProperty,Texture2D.blackTexture);cmd.SetGlobalTexture(depthProperty,Texture2D.blackTexture);
                cmd.SetRenderTarget(new RenderTargetIdentifier(frame.color),new RenderTargetIdentifier(frame.depth));changedTarget=true;
                cmd.SetViewport(new Rect(0,0,w,h));cmd.ClearRenderTarget(RTClearFlags.All,clearColor,1,0);
                var baked=new HashSet<SkinnedMeshRenderer>();
                foreach(var draw in draws)
                {
                    var renderer=draw?.renderer;
                    if(!renderer||!renderer.enabled||renderer.forceRenderingOff||!renderer.gameObject.activeInHierarchy||(camera.cullingMask&(1<<renderer.gameObject.layer))==0)continue;
                    var materials=renderer.sharedMaterials;
                    if(draw.materialIndex<0||draw.materialIndex>=materials.Length||!materials[draw.materialIndex])throw new InvalidOperationException("Invalid geometry material slot: "+renderer.name);
                    var material=materials[draw.materialIndex];int pass=material.FindPass(draw.passName);
                    if(pass<0)throw new InvalidOperationException("Missing geometry pass: "+renderer.name+"/"+draw.passName);
                    Mesh mesh;
                    if(renderer is SkinnedMeshRenderer skin)
                    {
                        if(!skins.TryGetValue(skin,out mesh)||!mesh){mesh=new Mesh{hideFlags=HideFlags.HideAndDontSave};skins[skin]=mesh;}
                        if(baked.Add(skin))skin.BakeMesh(mesh,false);
                    }
                    else mesh=renderer.GetComponent<MeshFilter>()?.sharedMesh;
                    if(!mesh||draw.submeshIndex<0||draw.submeshIndex>=mesh.subMeshCount)throw new InvalidOperationException("Invalid geometry mesh/submesh: "+renderer.name);
                    var properties=new MaterialPropertyBlock();renderer.GetPropertyBlock(properties,draw.materialIndex);
                    if(properties.isEmpty)renderer.GetPropertyBlock(properties);
                    foreach(var property in draw.floatProperties)properties.SetFloat(property.name,property.value);
                    foreach(var property in draw.textureProperties)properties.SetTexture(property.name,property.value);
                    foreach(var property in draw.copyProperties)
                    {
                        int slot=property.sourceMaterialIndex<0?draw.materialIndex:property.sourceMaterialIndex;
                        if(slot>=materials.Length||!materials[slot])throw new InvalidOperationException("Invalid geometry property-copy slot");
                        var block=new MaterialPropertyBlock();renderer.GetPropertyBlock(block,slot);if(block.isEmpty)renderer.GetPropertyBlock(block);
                        int from=Shader.PropertyToID(property.source),to=Shader.PropertyToID(property.target);
                        bool overridden=block.HasProperty(from);var source=materials[slot];
                        if(!overridden&&!source.HasProperty(from))throw new InvalidOperationException("Missing geometry property-copy source: "+property.source);
                        switch(property.kind)
                        {
                            case NativeSurfaceModifierPass.PropertyKind.Float:properties.SetFloat(to,overridden?block.GetFloat(from):source.GetFloat(from));break;
                            case NativeSurfaceModifierPass.PropertyKind.Vector:properties.SetVector(to,overridden?block.GetVector(from):source.GetVector(from));break;
                            case NativeSurfaceModifierPass.PropertyKind.Texture:properties.SetTexture(to,overridden?block.GetTexture(from):source.GetTexture(from));break;
                        }
                    }
                    cmd.DrawMesh(mesh,renderer.localToWorldMatrix,material,draw.submeshIndex,pass,properties);
                }
                // Depth must no longer be a writable attachment when published as an SRV.
                CoreUtils.SetRenderTarget(cmd,ctx.cameraColorBuffer,ctx.cameraDepthBuffer);
                cmd.SetGlobalTexture(colorProperty,frame.color);cmd.SetGlobalTexture(depthProperty,frame.depth,RenderTextureSubElement.Depth);
                cmd.SetGlobalVector(sizeProperty,new Vector4(w,h,1f/w,1f/h));cmd.SetGlobalFloat(validProperty,1);
            }
            catch(InvalidOperationException error){if(warnings.Add(error.Message))Debug.LogWarning("Geometry buffer unavailable: "+error.Message);}
            finally{if(changedTarget){CoreUtils.SetRenderTarget(cmd,ctx.cameraColorBuffer,ctx.cameraDepthBuffer);cmd.SetViewport(new Rect(0,0,ctx.hdCamera.actualWidth,ctx.hdCamera.actualHeight));}}
        }
        public RenderTexture GetColor(Camera camera)=>frames.TryGetValue(camera,out var frame)?frame.color:null;
        public RenderTexture GetDepth(Camera camera)=>frames.TryGetValue(camera,out var frame)?frame.depth:null;
        protected override void Cleanup()
        {
            owners.Remove(this);if(!string.IsNullOrWhiteSpace(validProperty))Shader.SetGlobalFloat(validProperty,0);
            foreach(var frame in frames.Values)frame.Dispose();frames.Clear();
            foreach(var mesh in skins.Values)CoreUtils.Destroy(mesh);skins.Clear();
        }
    }
}
