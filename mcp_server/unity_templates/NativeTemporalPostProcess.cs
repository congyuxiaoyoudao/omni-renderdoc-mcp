using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;
namespace OmniRenderDoc
{
    [Serializable,VolumeComponentMenu("Post-processing/Omni RenderDoc/Native source temporal AA")]
    public sealed class NativeTemporalPostProcess:CustomPostProcessVolumeComponent,IPostProcessComponent
    {
        [Serializable] public sealed class ProfileParameter:VolumeParameter<NativeTemporalProfile>{public ProfileParameter(NativeTemporalProfile value,bool state=false):base(value,state){}}
        public ProfileParameter profile=new ProfileParameter(null);
        public BoolParameter enabledEffect=new BoolParameter(true);
        NativeTemporalHistory history;NativeTemporalProfile activeProfile;Material copy,copyArray;
        readonly Dictionary<HDCamera,RenderTexture> colors=new Dictionary<HDCamera,RenderTexture>();
        readonly Dictionary<HDCamera,int> serials=new Dictionary<HDCamera,int>();
        public NativeTemporalHistory.Diagnostics lastDiagnostics {get;private set;}
        public bool IsActive()=>enabledEffect.value&&profile.value&&profile.value.enabledEffect;
        public override bool visibleInSceneView=>true;
        public override CustomPostProcessInjectionPoint injectionPoint=>CustomPostProcessInjectionPoint.BeforePostProcess;
        public override void Render(CommandBuffer cmd,HDCamera camera,RTHandle source,RTHandle destination)
        {
            var dead=new List<HDCamera>();foreach(var item in colors)if(!item.Key.camera){if(item.Value)item.Value.Release();CoreUtils.Destroy(item.Value);dead.Add(item.Key);}foreach(var item in dead){colors.Remove(item);serials.Remove(item);}
            if(!NativeTemporalJitter.IsActive(profile.value,camera.camera)||!NativeTemporalGeometryPass.TryGetCurrent(camera,camera.actualWidth,camera.actualHeight,out var data)||!data.finalGeometry)
            {HDUtils.BlitCameraTexture(cmd,source,destination);return;}
            var p=profile.value;
            if(history==null||p!=activeProfile)
            {
                Cleanup();activeProfile=p;history=new NativeTemporalHistory(p.dilation,p.reduction,p.resolve,p.seed,p.settings);copy=CoreUtils.CreateEngineMaterial(p.copy);
                copyArray=CoreUtils.CreateEngineMaterial(p.copy);copyArray.EnableKeyword("NATIVE_TEMPORAL_INPUT_ARRAY");
            }
            int w=camera.actualWidth,h=camera.actualHeight;
            if(!colors.TryGetValue(camera,out var color)||color.width!=w||color.height!=h)
            {
                if(color)color.Release();CoreUtils.Destroy(color);
                color=new RenderTexture(new RenderTextureDescriptor(w,h){graphicsFormat=GraphicsFormat.B10G11R11_UFloatPack32,depthStencilFormat=GraphicsFormat.None,msaaSamples=1})
                    {name="Current source temporal input color",hideFlags=HideFlags.HideAndDontSave,filterMode=FilterMode.Point};color.Create();colors[camera]=color;
            }
            var properties=new MaterialPropertyBlock();properties.SetTexture("_NativeTemporalCameraColor",source);
            cmd.SetRenderTarget(color);cmd.SetViewport(new Rect(0,0,w,h));cmd.DrawProcedural(Matrix4x4.identity,source.rt.dimension==TextureDimension.Tex2DArray?copyArray:copy,0,MeshTopology.Triangles,3,1,properties);
            bool discontinuity=!data.previousFrameValid||(serials.TryGetValue(camera,out int previous)&&data.renderSerial!=previous+1);serials[camera]=data.renderSerial;
            lastDiagnostics=history.Render(cmd,new NativeTemporalHistory.Frame{camera=camera.camera,historyChannel=data.historyChannel,
                currentColor=color,currentDepth=data.currentDepth,currentVelocityClass=data.motionClass,currentWorldToSourceClip=data.worldToSourceClip,
                renderedJitterPixels=data.jitter,cameraCut=discontinuity,continuityGeneration=data.continuityGeneration,sourceMotionAndMetadataVerified=data.sourceVerified});
            properties=new MaterialPropertyBlock();properties.SetTexture("_InputTexture",lastDiagnostics.resolved);
            cmd.SetRenderTarget(destination.nameID,0,CubemapFace.Unknown,0);cmd.SetViewport(new Rect(0,0,w,h));cmd.DrawProcedural(Matrix4x4.identity,copy,2,MeshTopology.Triangles,3,1,properties);
        }
        public override void Cleanup(){history?.Dispose();history=null;CoreUtils.Destroy(copy);CoreUtils.Destroy(copyArray);copy=null;copyArray=null;foreach(var t in colors.Values){if(t)t.Release();CoreUtils.Destroy(t);}colors.Clear();serials.Clear();activeProfile=null;lastDiagnostics=null;}
    }
}
