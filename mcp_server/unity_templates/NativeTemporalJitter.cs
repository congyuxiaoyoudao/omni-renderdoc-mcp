using System;
using System.Collections.Generic;
using System.Reflection;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;
namespace OmniRenderDoc
{
    // Context callbacks run before HDCamera.Update; beginCameraRendering is too
    // late to change the projection used by HDRP's view constants and culling.
    [ExecuteAlways,DisallowMultipleComponent]
    public sealed class NativeTemporalJitter : MonoBehaviour
    {
        public NativeTemporalProfile profile;
        public Volume sourceVolume;
        static readonly PropertyInfo projectionMode=typeof(Camera).GetProperty("projectionMatrixMode",BindingFlags.Instance|BindingFlags.NonPublic);
        static readonly HashSet<NativeTemporalJitter> owners=new HashSet<NativeTemporalJitter>();
        public static bool IsActive(NativeTemporalProfile profile,Camera camera)
        {
            if(projectionMode==null||!profile||!profile.enabledEffect||!profile.motionCoverageVerified||!camera||camera.stereoEnabled||!CoreUtils.ArePostProcessesEnabled(camera))return false;
            if(camera.cameraType==CameraType.SceneView?!profile.renderSceneView:camera.cameraType!=CameraType.Game||!profile.renderGame)return false;
            var hd=camera.GetComponent<HDAdditionalCameraData>();
            if(camera.cameraType!=CameraType.SceneView&&hd&&hd.antialiasing==HDAdditionalCameraData.AntialiasingMode.TemporalAntialiasing)return false;
            foreach(var owner in owners)if(owner&&owner.isActiveAndEnabled&&owner.profile==profile&&owner.sourceVolume&&owner.sourceVolume.isActiveAndEnabled&&owner.sourceVolume.sharedProfile&&
                owner.sourceVolume.sharedProfile.TryGet<NativeTemporalPostProcess>(out var post)&&post.active&&post.enabledEffect.value)return true;
            return false;
        }
        sealed class Frame
        {
            public Matrix4x4 savedProjection,savedNonJittered,sourceWorldToClip;
            public Vector2 jitter;
            public int width,height;
            public bool automaticProjection;
        }
        readonly Dictionary<Camera,Frame> active=new Dictionary<Camera,Frame>();
        readonly Dictionary<Camera,int> indices=new Dictionary<Camera,int>();
        static readonly Dictionary<Camera,Frame> current=new Dictionary<Camera,Frame>();
#if UNITY_EDITOR
        PropertyInfo sceneAaProperty;object savedSceneAa;
#endif
        public static bool TryGet(Camera camera,int width,int height,out Matrix4x4 sourceWorldToClip,out Vector2 jitter)
        {
            if(current.TryGetValue(camera,out var f))
            {
                sourceWorldToClip=f.sourceWorldToClip;
                jitter=new Vector2(f.jitter.x*width/f.width,f.jitter.y*height/f.height);return true;
            }
            sourceWorldToClip=GL.GetGPUProjectionMatrix(camera.projectionMatrix,true)*camera.worldToCameraMatrix;jitter=Vector2.zero;return false;
        }
        void OnEnable(){owners.Add(this);RenderPipelineManager.beginContextRendering+=Begin;RenderPipelineManager.endContextRendering+=End;}
        void OnDisable(){owners.Remove(this);RenderPipelineManager.beginContextRendering-=Begin;RenderPipelineManager.endContextRendering-=End;Restore();indices.Clear();}
        void Begin(ScriptableRenderContext context,List<Camera> cameras)
        {
            if(!profile||profile.jitterSequence==null||profile.jitterSequence.Length==0)return;
            var dead=new List<Camera>();foreach(var item in indices)if(!item.Key)dead.Add(item.Key);foreach(var item in dead)indices.Remove(item);
            foreach(var camera in cameras)
            {
                if(!IsActive(profile,camera)||current.ContainsKey(camera))continue;
                // The source algorithm owns AA; never stack stock HDRP TAA.
#if UNITY_EDITOR
                if(camera.cameraType==CameraType.SceneView&&sceneAaProperty==null)
                {
                    var type=typeof(HDCamera).Assembly.GetType("UnityEngine.Rendering.HighDefinition.HDAdditionalSceneViewSettings");
                    sceneAaProperty=type?.GetProperty("sceneViewAntialiasing",BindingFlags.Static|BindingFlags.Public|BindingFlags.NonPublic);
                    if(sceneAaProperty!=null){savedSceneAa=sceneAaProperty.GetValue(null);sceneAaProperty.SetValue(null,HDAdditionalCameraData.AntialiasingMode.None);}
                }
#endif
                int index=indices.TryGetValue(camera,out int old)?old:0;indices[camera]=index+1;
                var f=new Frame{savedProjection=camera.projectionMatrix,savedNonJittered=camera.nonJitteredProjectionMatrix,
                    automaticProjection=Convert.ToInt32(projectionMode.GetValue(camera))!=0,
                    width=Mathf.Max(1,camera.pixelWidth),height=Mathf.Max(1,camera.pixelHeight),jitter=profile.jitterSequence[index%profile.jitterSequence.Length]};
                var gpu=GL.GetGPUProjectionMatrix(f.savedProjection,true);f.sourceWorldToClip=gpu*camera.worldToCameraMatrix;
                float ySign=Mathf.Sign(gpu.m11/f.savedProjection.m11);
                var projection=f.savedProjection;
                // Requested jitter is a top-left texture pixel displacement.
                projection.SetRow(0,projection.GetRow(0)+projection.GetRow(3)*(2*f.jitter.x/f.width));
                projection.SetRow(1,projection.GetRow(1)+projection.GetRow(3)*(-2*f.jitter.y/(f.height*ySign)));
                camera.nonJitteredProjectionMatrix=f.savedProjection;camera.projectionMatrix=projection;
                active[camera]=f;current[camera]=f;
            }
        }
        void End(ScriptableRenderContext context,List<Camera> cameras)=>Restore();
        void Restore()
        {
            foreach(var item in active)
            {
                if(item.Key)
                {
                    // Assigning a matrix switches Unity's camera into explicit
                    // projection mode. Restore automatic/physical projection as
                    // a mode, so the later target-texture restoration and viewport
                    // changes can recompute its aspect-dependent matrix.
                    if(item.Value.automaticProjection)item.Key.ResetProjectionMatrix();
                    else item.Key.projectionMatrix=item.Value.savedProjection;
                    item.Key.nonJitteredProjectionMatrix=item.Value.savedNonJittered;
                }
                current.Remove(item.Key);
            }
            active.Clear();
#if UNITY_EDITOR
            if(sceneAaProperty!=null){sceneAaProperty.SetValue(null,savedSceneAa);sceneAaProperty=null;savedSceneAa=null;}
#endif
        }
    }
}
