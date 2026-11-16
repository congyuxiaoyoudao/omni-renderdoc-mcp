using System;
using System.Collections.Generic;
using System.Runtime.CompilerServices;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;
namespace OmniRenderDoc
{
    // Run once after depth/normal (before AO), then refresh after transparents.
    // Both phases share this rendering's previous geometry, never advance twice.
    [Serializable]
    public sealed class NativeTemporalGeometryPass : CustomPass
    {
        [Serializable] public sealed class Draw {public Renderer renderer;public int materialIndex,submeshIndex;public string passName="NativeTemporalGeometry";public bool finalOnly,writesDepth;}
        public NativeTemporalProfile profile;
        public Draw[] draws=Array.Empty<Draw>();
        public bool finalGeometry;
        public sealed class FrameData
        {
            public RenderTexture motionClass,currentDepth;
            // Prepared AO adapter: XY = original quantized oct normal,
            // Z = producer coverage validity, A = original class. This is NOT
            // the original RGBA normal/roughness buffer; consumers must test Z.
            public RenderTexture sourceNormal;
            public bool previousFrameValid,sourceVerified,finalGeometry;
            public int renderSerial,historyChannel;
            public long continuityGeneration;
            public Matrix4x4 worldToSourceClip;
            public Vector2 jitter;
        }
        sealed class MeshHistory:IDisposable
        {
            public Mesh source,baked;
            public ComputeBuffer previous,current;
            public int count,serial=-1;
            public long indices;
            public Matrix4x4 previousMatrix,currentMatrix;
            public void Dispose(){previous?.Dispose();current?.Dispose();CoreUtils.Destroy(baked);}
        }
        sealed class CameraState:IDisposable
        {
            public HDCamera hd;
            public int width,height,serial=-1;
            public bool lastFinal;
            public Vector3 position;public Quaternion rotation;
            public long generation;
            public Matrix4x4 currentClip,previousClip;
            public FrameData data=new FrameData();
            public RenderTexture depthStencil;
            public readonly Dictionary<Renderer,MeshHistory> meshes=new Dictionary<Renderer,MeshHistory>();
            public void Dispose(){foreach(var mesh in meshes.Values)mesh.Dispose();foreach(var t in new[]{data.motionClass,data.sourceNormal,data.currentDepth,depthStencil}){if(t)t.Release();CoreUtils.Destroy(t);}meshes.Clear();}
        }
        static readonly Dictionary<HDCamera,CameraState> states=new Dictionary<HDCamera,CameraState>();
        static readonly Dictionary<Camera,int> serials=new Dictionary<Camera,int>();
        static int owners;
        Material copy,copyArray;
        readonly HashSet<string> warnings=new HashSet<string>();
        public NativeTemporalGeometryPass(){targetColorBuffer=TargetBuffer.None;targetDepthBuffer=TargetBuffer.None;}
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.SubsystemRegistration)] static void Register(){RenderPipelineManager.beginCameraRendering-=Begin;RenderPipelineManager.beginCameraRendering+=Begin;}
#if UNITY_EDITOR
        [UnityEditor.InitializeOnLoadMethod] static void RegisterEditor()=>Register();
#endif
        static void Begin(ScriptableRenderContext context,Camera camera){serials[camera]=serials.TryGetValue(camera,out int serial)?serial+1:1;}
        public static bool TryGetCurrent(HDCamera camera,int width,int height,out FrameData data)
        {
            data=null;if(camera==null||!states.TryGetValue(camera,out var s)||s.width!=width||s.height!=height||!serials.TryGetValue(camera.camera,out int serial)||s.serial!=serial)return false;
            data=s.data;return data.sourceVerified;
        }
        public static void NotifyGeometryChanged(Camera camera=null)
        {
            foreach(var state in states.Values)if(camera==null||state.hd.camera==camera){state.generation++;state.lastFinal=false;}
        }
        protected override void Setup(ScriptableRenderContext context,CommandBuffer cmd){Register();owners++;}
        static RenderTexture Allocate(int w,int h,GraphicsFormat format,string name,GraphicsFormat depth=GraphicsFormat.None)
        {
            var t=new RenderTexture(new RenderTextureDescriptor(w,h){graphicsFormat=format,depthStencilFormat=depth,msaaSamples=1,dimension=TextureDimension.Tex2D})
                {name=name,hideFlags=HideFlags.HideAndDontSave,filterMode=FilterMode.Point,wrapMode=TextureWrapMode.Clamp};
            if(!t.Create())throw new InvalidOperationException("Cannot allocate "+name);return t;
        }
        void Warn(string reason){if(warnings.Add(reason))Debug.LogWarning("Native temporal geometry: "+reason);}
        protected override void Execute(CustomPassContext ctx)
        {
            var deadCameras=new List<HDCamera>();foreach(var item in states)if(!item.Key.camera){item.Value.Dispose();deadCameras.Add(item.Key);}foreach(var item in deadCameras)states.Remove(item);
            var deadSerials=new List<Camera>();foreach(var item in serials)if(!item.Key)deadSerials.Add(item.Key);foreach(var item in deadSerials)serials.Remove(item);
            var camera=ctx.hdCamera.camera;if(!NativeTemporalJitter.IsActive(profile,camera)||!profile.copy)return;
            if(camera.cameraType==CameraType.SceneView?!profile.renderSceneView:camera.cameraType!=CameraType.Game||!profile.renderGame)return;
            if(!copy||copy.shader!=profile.copy)
            {
                CoreUtils.Destroy(copy);CoreUtils.Destroy(copyArray);copy=CoreUtils.CreateEngineMaterial(profile.copy);copyArray=CoreUtils.CreateEngineMaterial(profile.copy);
                copyArray.EnableKeyword("NATIVE_TEMPORAL_INPUT_ARRAY");
            }
            var copyMaterial=ctx.cameraDepthBuffer.rt.dimension==TextureDimension.Tex2DArray?copyArray:copy;
            int w=ctx.hdCamera.actualWidth,h=ctx.hdCamera.actualHeight;
            if(!serials.TryGetValue(camera,out int serial)){serial=1;serials[camera]=serial;}
            if(!states.TryGetValue(ctx.hdCamera,out var s)||s.width!=w||s.height!=h)
            {
                s?.Dispose();s=new CameraState{hd=ctx.hdCamera,width=w,height=h};states[ctx.hdCamera]=s;
                s.data.motionClass=Allocate(w,h,GraphicsFormat.A2B10G10R10_UNormPack32,"Current source velocity and class");
                s.data.sourceNormal=Allocate(w,h,GraphicsFormat.A2B10G10R10_UNormPack32,"Current source normal and class");
                s.data.currentDepth=Allocate(w,h,GraphicsFormat.R32_SFloat,"Current source depth");
                s.depthStencil=Allocate(w,h,GraphicsFormat.None,"Current temporal private depth",GraphicsFormat.D32_SFloat_S8_UInt);
            }
            if(s.serial!=serial)
            {
                var deadRenderers=new List<Renderer>();foreach(var item in s.meshes)if(!item.Key){item.Value.Dispose();deadRenderers.Add(item.Key);}foreach(var item in deadRenderers)s.meshes.Remove(item);
                bool cut=s.serial<0||!s.lastFinal||Vector3.Distance(s.position,camera.transform.position)>profile.settings.cameraCutDistance||Quaternion.Angle(s.rotation,camera.transform.rotation)>profile.settings.cameraCutAngle;
                s.previousClip=s.currentClip;s.serial=serial;s.lastFinal=false;
                NativeTemporalJitter.TryGet(camera,w,h,out s.currentClip,out var jitter);
                if(cut){s.previousClip=s.currentClip;s.generation++;}
                s.position=camera.transform.position;s.rotation=camera.transform.rotation;
                s.data.worldToSourceClip=s.currentClip;s.data.jitter=jitter;s.data.previousFrameValid=!cut;
                s.data.renderSerial=serial;s.data.historyChannel=RuntimeHelpers.GetHashCode(ctx.hdCamera);s.data.continuityGeneration=s.generation+profile.revision;
            }
            var data=s.data;data.sourceVerified=true;data.finalGeometry=finalGeometry;
            var cmd=ctx.cmd;var properties=new MaterialPropertyBlock();
            properties.SetTexture("_NativeTemporalCameraDepth",ctx.cameraDepthBuffer);
            properties.SetVector("_NativeTemporalSize",new Vector4(w,h,1f/w,1f/h));properties.SetVector("_NativeTemporalJitter",data.jitter);
            properties.SetMatrix("_NativeTemporalCurrentToPreviousClip",s.previousClip*s.currentClip.inverse);
            cmd.SetRenderTarget(new RenderTargetIdentifier[]{data.motionClass,data.sourceNormal,data.currentDepth},s.depthStencil);
            cmd.SetViewport(new Rect(0,0,w,h));cmd.ClearRenderTarget(true,false,Color.clear,1,0);cmd.DrawProcedural(Matrix4x4.identity,copyMaterial,1,MeshTopology.Triangles,3,1,properties);
            cmd.SetRenderTarget(new RenderTargetIdentifier[]{data.motionClass,data.sourceNormal},s.depthStencil);
            bool extraTargetBound=false;
            foreach(var draw in draws)
            {
                if(draw==null||draw.finalOnly&&!finalGeometry)continue;
                var renderer=draw?.renderer;if(!renderer||!renderer.enabled||!renderer.gameObject.activeInHierarchy||(camera.cullingMask&(1<<renderer.gameObject.layer))==0)continue;
                var materials=renderer.sharedMaterials;if(draw.materialIndex<0||draw.materialIndex>=materials.Length||!materials[draw.materialIndex]){data.sourceVerified=false;continue;}
                var material=materials[draw.materialIndex];int pass=material.FindPass(draw.passName);
                if(pass<0){data.sourceVerified=false;Warn("Missing source pass on "+renderer.name);continue;}
                bool extra=draw.writesDepth;
                if(extra!=extraTargetBound){cmd.SetRenderTarget(extra?new RenderTargetIdentifier[]{data.motionClass,data.sourceNormal,data.currentDepth}:new RenderTargetIdentifier[]{data.motionClass,data.sourceNormal},s.depthStencil);extraTargetBound=extra;}
                if(!s.meshes.TryGetValue(renderer,out var meshHistory)){meshHistory=new MeshHistory();s.meshes.Add(renderer,meshHistory);}
                Mesh mesh;
                if(renderer is SkinnedMeshRenderer skin)
                {
                    if(!meshHistory.baked)meshHistory.baked=new Mesh{name="Current editable temporal baked mesh",hideFlags=HideFlags.HideAndDontSave};
                    if(meshHistory.serial!=serial)skin.BakeMesh(meshHistory.baked);mesh=meshHistory.baked;
                }
                else mesh=renderer.GetComponent<MeshFilter>()?.sharedMesh;
                if(!mesh||!mesh.isReadable||draw.submeshIndex<0||draw.submeshIndex>=mesh.subMeshCount){data.sourceVerified=false;Warn("Mesh is unavailable/unreadable: "+renderer.name);continue;}
                if(meshHistory.serial!=serial)
                {
                    long indexCount=0;for(int i=0;i<mesh.subMeshCount;i++)indexCount+=mesh.GetIndexCount(i);
                    bool newTopology=meshHistory.source!=mesh||meshHistory.count!=mesh.vertexCount||meshHistory.indices!=indexCount;
                    var vertices=mesh.vertices; // Read current vertices even when mesh ID and count are unchanged.
                    if(newTopology)
                    {
                        meshHistory.previous?.Dispose();meshHistory.current?.Dispose();meshHistory.previous=new ComputeBuffer(vertices.Length,12);meshHistory.current=new ComputeBuffer(vertices.Length,12);
                        meshHistory.previous.SetData(vertices);meshHistory.current.SetData(vertices);meshHistory.previousMatrix=renderer.localToWorldMatrix;
                        meshHistory.source=mesh;meshHistory.count=vertices.Length;meshHistory.indices=indexCount;
                        s.generation++;data.continuityGeneration=s.generation+profile.revision;
                        data.previousFrameValid=false;
                    }
                    else
                    {
                        var swap=meshHistory.previous;meshHistory.previous=meshHistory.current;meshHistory.current=swap;
                        meshHistory.current.SetData(vertices);meshHistory.previousMatrix=meshHistory.currentMatrix;
                    }
                    meshHistory.currentMatrix=renderer.localToWorldMatrix;meshHistory.serial=serial;
                }
                properties=new MaterialPropertyBlock();renderer.GetPropertyBlock(properties,draw.materialIndex);
                properties.SetBuffer("_NativeTemporalPreviousPositions",data.previousFrameValid?meshHistory.previous:meshHistory.current);
                properties.SetMatrix("_NativeTemporalCurrentObjectToClip",s.currentClip*meshHistory.currentMatrix);
                properties.SetMatrix("_NativeTemporalPreviousObjectToClip",data.previousFrameValid?s.previousClip*meshHistory.previousMatrix:s.currentClip*meshHistory.currentMatrix);
                cmd.DrawMesh(mesh,renderer.localToWorldMatrix,material,draw.submeshIndex,pass,properties);
            }
            if(finalGeometry)s.lastFinal=data.sourceVerified;
            CoreUtils.SetRenderTarget(cmd,ctx.cameraColorBuffer,ctx.cameraDepthBuffer);
        }
        protected override void Cleanup()
        {
            CoreUtils.Destroy(copy);CoreUtils.Destroy(copyArray);copy=null;copyArray=null;if(--owners>0)return;
            foreach(var state in states.Values)state.Dispose();states.Clear();serials.Clear();owners=0;
        }
    }
}
