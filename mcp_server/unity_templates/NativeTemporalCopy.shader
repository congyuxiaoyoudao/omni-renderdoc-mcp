Shader "Hidden/OmniRenderDoc/NativeTemporalCopy"
{
Properties { _InputTexture("Current 2D color",2D)="black"{} }
HLSLINCLUDE
#pragma target 5.0
#pragma multi_compile _ NATIVE_TEMPORAL_INPUT_ARRAY
#include "Packages/com.unity.render-pipelines.core/ShaderLibrary/Common.hlsl"
#include "Packages/com.unity.render-pipelines.high-definition/Runtime/ShaderLibrary/ShaderVariables.hlsl"
struct TemporalRaster { float4 position:SV_Position;float2 uv:TEXCOORD0; };
TemporalRaster TemporalVertex(uint id:SV_VertexID){TemporalRaster o;float2 uv=float2((id<<1)&2,id&2);o.position=float4(uv.x*2-1,1-uv.y*2,0,1);o.uv=uv;return o;}
#if defined(NATIVE_TEMPORAL_INPUT_ARRAY)
Texture2DArray<float4> _NativeTemporalCameraColor;
Texture2DArray<float> _NativeTemporalCameraDepth;
#define TEMPORAL_LOAD_COLOR(pixel) _NativeTemporalCameraColor.Load(int4(pixel,0,0))
#define TEMPORAL_LOAD_DEPTH(pixel) _NativeTemporalCameraDepth.Load(int4(pixel,0,0))
#else
Texture2D<float4> _NativeTemporalCameraColor;
Texture2D<float> _NativeTemporalCameraDepth;
#define TEMPORAL_LOAD_COLOR(pixel) _NativeTemporalCameraColor.Load(int3(pixel,0))
#define TEMPORAL_LOAD_DEPTH(pixel) _NativeTemporalCameraDepth.Load(int3(pixel,0))
#endif
Texture2D<float4> _InputTexture;
float4 _NativeTemporalSize,_NativeTemporalJitter;
float4x4 _NativeTemporalCurrentToPreviousClip;
float4 TemporalFragment(TemporalRaster i):SV_Target{return TEMPORAL_LOAD_COLOR(uint2(i.position.xy));}
struct TemporalPrepared {float4 motion:SV_Target0;float4 normal:SV_Target1;float depthColor:SV_Target2;float depth:SV_Depth;};
TemporalPrepared TemporalPrepare(TemporalRaster i)
{
    TemporalPrepared o;uint2 pixel=uint2(i.position.xy);float d=TEMPORAL_LOAD_DEPTH(pixel);
    float2 uv=(i.position.xy-_NativeTemporalJitter.xy)*_NativeTemporalSize.zw;
    float4 clip=float4(uv.x*2-1,1-uv.y*2,d,1);float4 previous=mul(_NativeTemporalCurrentToPreviousClip,clip);
    float2 delta=clip.xy-previous.xy/max(previous.w,1e-8);delta.y=-delta.y;
    o.motion=float4(sqrt(sqrt(abs(delta*.5)))*sign(delta)*.5+.5,0,0);
    o.normal=0;o.depthColor=d;o.depth=d;return o;
}
float4 TemporalPresent(TemporalRaster i):SV_Target{return _InputTexture.Load(int3(uint2(i.position.xy),0));}
ENDHLSL
SubShader { Tags {"RenderPipeline"="HDRenderPipeline"}
Pass { Name "ReadCurrentColor" Cull Off ZTest Always ZWrite Off Blend Off
HLSLPROGRAM
#pragma vertex TemporalVertex
#pragma fragment TemporalFragment
ENDHLSL
}
Pass { Name "PrepareCurrentGeometry" Cull Off ZTest Always ZWrite On Blend Off
HLSLPROGRAM
#pragma vertex TemporalVertex
#pragma fragment TemporalPrepare
ENDHLSL
}
Pass { Name "Present" Cull Off ZTest Always ZWrite Off Blend Off
HLSLPROGRAM
#pragma vertex TemporalVertex
#pragma fragment TemporalPresent
ENDHLSL
}
}
}
