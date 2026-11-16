using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;

namespace OmniRenderDoc
{
/// <summary>
/// Source-equation temporal chain for explicitly prepared current inputs. This
/// core deliberately does not substitute HDRP motion for the source packed
/// velocity/material flags. The producer must provide current editable-geometry
/// inputs, a consistent source clip-space matrix and the actual rendered jitter.
/// Captured history is not accepted or loaded here.
/// </summary>
public sealed class NativeTemporalHistory : IDisposable
{
    public enum VelocityHistoryStorage { Unspecified, OriginalPackedVelocity, DilatedPackedVelocity }
    public enum DepthHistoryStorage { Unspecified, CurrentDepth, DilatedDepth }
    [Serializable] public sealed class Settings
    {
        public Vector4 parameters0,parameters1,parameters2,parameters3,parameters4,parameters5;
        public Vector4[] currentFilterWeights;
        public float sourceMipBias;
        public Vector2 emptyDepthOffset;
        public VelocityHistoryStorage velocityHistory;
        public DepthHistoryStorage depthHistory;
        public float cameraCutDistance=1;
        public float cameraCutAngle=30;
    }
    public sealed class Frame
    {
        public Camera camera;
        // Separate HDRP normal rendering from StandardRequest/history channels.
        public int historyChannel;
        public RenderTexture currentColor,currentDepth,currentVelocityClass;
        public Matrix4x4 currentWorldToSourceClip;
        public Vector2 renderedJitterPixels;
        public bool cameraCut;
        // Change this when topology, shader classification or temporal settings
        // change in ways for which previous-vertex correspondence is unknown.
        public long continuityGeneration;
        public bool sourceMotionAndMetadataVerified;
    }
    public sealed class Diagnostics
    {
        public int width,height,acceptedFrames;
        public bool reset;
        public string reason;
        public RenderTexture resolved,dilatedDepth,dilatedVelocity,reducedMask;
    }
    sealed class State
    {
        public Camera camera;
        public int width,height,index,frames;
        public long generation;
        public bool valid;
        public Vector3 position;
        public Quaternion rotation;
        public Matrix4x4 worldToClip;
        public RenderTexture[] color=new RenderTexture[2];
        public RenderTexture depth,velocity,dilatedDepth,dilatedVelocity,reduced;
        public void Dispose()
        {
            foreach(var t in new[]{color[0],color[1],depth,velocity,dilatedDepth,dilatedVelocity,reduced})
                if(t){t.Release();CoreUtils.Destroy(t);}
        }
    }
    readonly Dictionary<(int,int),State> states=new Dictionary<(int,int),State>();
    readonly Material dilation,reduction,resolve,seed;
    readonly Settings settings;
    public NativeTemporalHistory(Shader dilationShader,Shader reductionShader,Shader resolveShader,Shader seedShader,Settings configuration)
    {
        settings=configuration??throw new ArgumentNullException(nameof(configuration));
        if(settings.currentFilterWeights==null||settings.currentFilterWeights.Length!=3)throw new ArgumentException("Explicit 3x3 source filter weights required.");
        if(settings.velocityHistory==VelocityHistoryStorage.Unspecified||settings.depthHistory==DepthHistoryStorage.Unspecified)
            throw new ArgumentException("Explicit source history feedback contracts required; one frame cannot establish unknown previous-frame producers.");
        Material Make(Shader shader)=>shader?new Material(shader){hideFlags=HideFlags.HideAndDontSave}:throw new ArgumentException("Missing source temporal shader");
        dilation=Make(dilationShader);reduction=Make(reductionShader);resolve=Make(resolveShader);seed=Make(seedShader);
    }
    static RenderTexture Allocate(int width,int height,GraphicsFormat format,string name)
    {
        var texture=new RenderTexture(new RenderTextureDescriptor(width,height){graphicsFormat=format,depthStencilFormat=GraphicsFormat.None,msaaSamples=1,dimension=TextureDimension.Tex2D,volumeDepth=1})
        {name=name,hideFlags=HideFlags.HideAndDontSave,filterMode=FilterMode.Point,wrapMode=TextureWrapMode.Clamp};
        if(!texture.Create())throw new InvalidOperationException("Temporal allocation failed: "+name);return texture;
    }
    static void Check(RenderTexture texture,int width,int height,string name)
    {
        if(!texture||!texture.IsCreated()||texture.width!=width||texture.height!=height||texture.dimension!=TextureDimension.Tex2D||texture.antiAliasing!=1)
            throw new ArgumentException(name+" must be an owned, exact-viewport, single-sample 2D input. Crop HDRP backing/array targets explicitly before calling.");
    }
    void Bind(Material material,int width,int height,Vector2 jitter,Matrix4x4 currentToPrevious)
    {
        material.SetVector("_Temporal0",settings.parameters0);material.SetVector("_Temporal1",settings.parameters1);
        material.SetVector("_Temporal2",settings.parameters2);material.SetVector("_Temporal3",settings.parameters3);
        material.SetVector("_Temporal4",settings.parameters4);material.SetVector("_Temporal5",settings.parameters5);
        material.SetVector("_Temporal6",new Vector4(width,height,1f/width,1f/height));
        material.SetVector("_Temporal7",new Vector4(width,height,1f/width,1f/height));
        material.SetVector("_Temporal8",Vector4.one);material.SetVectorArray("_Temporal9",settings.currentFilterWeights);
        material.SetVector("_TemporalJitter",new Vector4(jitter.x,jitter.y,jitter.x/width,jitter.y/height));
        material.SetVector("_TemporalScreenInfo",new Vector4(0,height/(float)width,0,1));
        material.SetFloat("_TemporalMipBias",settings.sourceMipBias);material.SetMatrix("_TemporalCurrentToPreviousClip",currentToPrevious);
        material.SetVector("_TemporalEmptyDepthOffset",settings.emptyDepthOffset);
    }
    static void Draw(CommandBuffer cmd,Material material,int width,int height,params RenderTexture[] outputs)
    {
        // CommandBuffer execution is deferred. Snapshot all bindings per draw so
        // subsequent seed draws or other cameras cannot overwrite queued inputs.
        var properties=new MaterialPropertyBlock();
        for(int i=0;i<material.shader.GetPropertyCount();i++)
            if(material.shader.GetPropertyType(i)==ShaderPropertyType.Texture)
            {string name=material.shader.GetPropertyName(i);properties.SetTexture(name,material.GetTexture(name));}
        if(material.HasProperty("_SeedConfidence"))properties.SetFloat("_SeedConfidence",material.GetFloat("_SeedConfidence"));
        else
        {
            for(int i=0;i<9;i++)properties.SetVector("_Temporal"+i,material.GetVector("_Temporal"+i));
            properties.SetVectorArray("_Temporal9",material.GetVectorArray("_Temporal9"));
            properties.SetVector("_TemporalJitter",material.GetVector("_TemporalJitter"));
            properties.SetVector("_TemporalScreenInfo",material.GetVector("_TemporalScreenInfo"));
            properties.SetFloat("_TemporalMipBias",material.GetFloat("_TemporalMipBias"));
            properties.SetMatrix("_TemporalCurrentToPreviousClip",material.GetMatrix("_TemporalCurrentToPreviousClip"));
            properties.SetVector("_TemporalEmptyDepthOffset",material.GetVector("_TemporalEmptyDepthOffset"));
        }
        var ids=new RenderTargetIdentifier[outputs.Length];for(int i=0;i<outputs.Length;i++)ids[i]=outputs[i];
        cmd.SetRenderTarget(ids,BuiltinRenderTextureType.None);cmd.SetViewport(new Rect(0,0,width,height));
        cmd.DrawProcedural(Matrix4x4.identity,material,0,MeshTopology.Triangles,3,1,properties);
    }
    public Diagnostics Render(CommandBuffer cmd,Frame input)
    {
        if(cmd==null||input==null||!input.camera||!input.currentColor)throw new ArgumentException("Missing current frame inputs.");
        if(!input.sourceMotionAndMetadataVerified)throw new InvalidOperationException("Temporal source motion and material metadata producer is unverified; live history was not advanced.");
        int width=input.currentColor.width,height=input.currentColor.height;
        Check(input.currentColor,width,height,"color");Check(input.currentDepth,width,height,"depth");Check(input.currentVelocityClass,width,height,"packed velocity/class");
        if(input.currentDepth.graphicsFormat!=GraphicsFormat.R32_SFloat||input.currentVelocityClass.graphicsFormat!=GraphicsFormat.A2B10G10R10_UNormPack32)
            throw new ArgumentException("Source temporal input formats require R32Float depth and R10G10B10A2 packed velocity/class.");
        if(!float.IsFinite(input.currentWorldToSourceClip.determinant)||Mathf.Abs(input.currentWorldToSourceClip.determinant)<1e-12f)
            throw new ArgumentException("Singular/nonfinite source projection.");
        var dead=new List<(int,int)>();foreach(var item in states)if(!item.Value.camera){item.Value.Dispose();dead.Add(item.Key);}foreach(var key in dead)states.Remove(key);
        var id=(input.camera.GetInstanceID(),input.historyChannel);string resetReason=null;
        if(!states.TryGetValue(id,out var state)||state.width!=width||state.height!=height)
        {
            state?.Dispose();state=new State{camera=input.camera,width=width,height=height};states[id]=state;
            for(int i=0;i<2;i++)state.color[i]=Allocate(width,height,GraphicsFormat.R16G16B16A16_SFloat,"Current temporal history "+i);
            state.depth=Allocate(width,height,GraphicsFormat.R16_SFloat,"Previous temporal depth");
            state.velocity=Allocate(width,height,GraphicsFormat.A2B10G10R10_UNormPack32,"Previous temporal velocity/class");
            state.dilatedDepth=Allocate(width,height,GraphicsFormat.R16_SFloat,"Current dilated temporal depth");
            state.dilatedVelocity=Allocate(width,height,GraphicsFormat.A2B10G10R10_UNormPack32,"Current dilated temporal flags");
            state.reduced=Allocate(Mathf.Max(1,(width+3)/4),Mathf.Max(1,(height+3)/4),GraphicsFormat.R8_UNorm,"Current temporal rejection reduction");
            resetReason="new camera/history channel or viewport resize";
        }
        if(input.cameraCut)resetReason="explicit camera cut";
        if(state.valid&&state.generation!=input.continuityGeneration)resetReason="geometry/material temporal continuity changed";
        if(state.valid&&(Vector3.Distance(state.position,input.camera.transform.position)>settings.cameraCutDistance||Quaternion.Angle(state.rotation,input.camera.transform.rotation)>settings.cameraCutAngle))resetReason="camera cut threshold";
        bool reset=!state.valid||resetReason!=null;
        if(reset&&resetReason==null)resetReason="history invalidated";
        Matrix4x4 reprojection=reset?Matrix4x4.identity:state.worldToClip*input.currentWorldToSourceClip.inverse;
        foreach(var material in new[]{dilation,reduction,resolve})Bind(material,width,height,input.renderedJitterPixels,reprojection);
        if(reset)
        {
            // Bootstrap with CURRENT data; never inject a captured frame/history.
            seed.SetTexture("_InputTexture",input.currentDepth);seed.SetFloat("_SeedConfidence",0);
            Draw(cmd,seed,width,height,state.depth);cmd.CopyTexture(input.currentVelocityClass,state.velocity);
        }
        dilation.SetTexture("_CurrentDepth",input.currentDepth);dilation.SetTexture("_CurrentVelocityClass",input.currentVelocityClass);
        dilation.SetTexture("_PreviousDepth",state.depth);dilation.SetTexture("_PreviousVelocityClass",state.velocity);
        Draw(cmd,dilation,width,height,state.dilatedDepth,state.dilatedVelocity);
        reduction.SetTexture("_DilatedVelocityClass",state.dilatedVelocity);Draw(cmd,reduction,state.reduced.width,state.reduced.height,state.reduced);
        var destination=state.color[1-state.index];
        if(reset)
        {
            seed.SetTexture("_InputTexture",input.currentColor);seed.SetFloat("_SeedConfidence",0);Draw(cmd,seed,width,height,destination);
        }
        else
        {
            resolve.SetTexture("_CurrentColor",input.currentColor);resolve.SetTexture("_CurrentDepth",input.currentDepth);
            resolve.SetTexture("_DilatedVelocityClass",state.dilatedVelocity);resolve.SetTexture("_ReducedRejection",state.reduced);resolve.SetTexture("_PreviousColor",state.color[state.index]);
            Draw(cmd,resolve,width,height,destination);
        }
        if(settings.depthHistory==DepthHistoryStorage.DilatedDepth)cmd.CopyTexture(state.dilatedDepth,state.depth);
        else {seed.SetTexture("_InputTexture",input.currentDepth);seed.SetFloat("_SeedConfidence",0);Draw(cmd,seed,width,height,state.depth);}
        cmd.CopyTexture(settings.velocityHistory==VelocityHistoryStorage.DilatedPackedVelocity?state.dilatedVelocity:input.currentVelocityClass,state.velocity);
        state.index=1-state.index;state.valid=true;state.frames++;state.generation=input.continuityGeneration;
        state.worldToClip=input.currentWorldToSourceClip;state.position=input.camera.transform.position;state.rotation=input.camera.transform.rotation;
        return new Diagnostics{width=width,height=height,acceptedFrames=state.frames,reset=reset,reason=resetReason??"history reused",resolved=destination,dilatedDepth=state.dilatedDepth,dilatedVelocity=state.dilatedVelocity,reducedMask=state.reduced};
    }
    public void Invalidate(Camera camera,int historyChannel) {if(camera&&states.TryGetValue((camera.GetInstanceID(),historyChannel),out var state))state.valid=false;}
    public void Dispose() {foreach(var s in states.Values)s.Dispose();states.Clear();foreach(var m in new[]{dilation,reduction,resolve,seed})CoreUtils.Destroy(m);}
}
}
