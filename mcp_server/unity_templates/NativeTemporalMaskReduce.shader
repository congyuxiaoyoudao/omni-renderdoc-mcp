Shader "Hidden/OmniRenderDoc/NativeTemporalMaskReduce"
{
Properties { _DilatedVelocityClass("_DilatedVelocityClass",2D)="black" {} }
SubShader { Tags { "RenderPipeline"="HDRenderPipeline" }
HLSLINCLUDE

struct TemporalRaster { float4 position:SV_Position; float2 uv:TEXCOORD0; };
TemporalRaster TemporalVertex(uint id:SV_VertexID)
{
    TemporalRaster o; float2 uv=float2((id<<1)&2,id&2);
    o.position=float4(uv.x*2-1,1-uv.y*2,0,1); o.uv=uv; return o;
}
float4 _Temporal0,_Temporal1,_Temporal2,_Temporal3,_Temporal4,_Temporal5;
float4 _Temporal6,_Temporal7,_Temporal8,_Temporal9[3];
float4 _TemporalScreenInfo,_TemporalJitter;
float _TemporalMipBias;
float4x4 _TemporalCurrentToPreviousClip;
float4 _TemporalEmptyDepthOffset;

// Source frag_main is unchanged except descriptive binding/uniform names.


SamplerState Temporal_linear_clamp_sampler;
Texture2D<float4> _DilatedVelocityClass;

static float2 _3;
static float _4;

struct SPIRV_Cross_Input
{
    float2 _3 : TEXCOORD0;
};

struct SPIRV_Cross_Output
{
    float _4 : SV_Target0;
};

void frag_main()
{
    _4 = float(max(max(max(max(_DilatedVelocityClass.SampleLevel(Temporal_linear_clamp_sampler, _3 + _Temporal6.zw, 0.0f).z, _DilatedVelocityClass.SampleLevel(Temporal_linear_clamp_sampler, _3 + (_Temporal6.zw * float2(-1.0f, 1.0f)), 0.0f).z), _DilatedVelocityClass.SampleLevel(Temporal_linear_clamp_sampler, _3 + (_Temporal6.zw * float2(1.0f, -1.0f)), 0.0f).z), _DilatedVelocityClass.SampleLevel(Temporal_linear_clamp_sampler, _3 + (_Temporal6.zw * (-1.0f).xx), 0.0f).z), _DilatedVelocityClass.SampleLevel(Temporal_linear_clamp_sampler, _3, 0.0f).z) > 0.0f);
}


float TemporalFragment(TemporalRaster i):SV_Target { _3=i.uv; frag_main(); return _4; }
ENDHLSL
Pass { Name "Temporal" Cull Off ZTest Always ZWrite Off Blend Off
HLSLPROGRAM
#pragma target 5.0
#pragma vertex TemporalVertex
#pragma fragment TemporalFragment
ENDHLSL
}
}
}
