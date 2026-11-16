Shader "Hidden/OmniRenderDoc/NativeTemporalSeed"
{
Properties { _InputTexture("Current source",2D)="black" {} _SeedConfidence("Initial confidence",Float)=0 }
SubShader { Tags { "RenderPipeline"="HDRenderPipeline" }
HLSLINCLUDE
Texture2D<float4> _InputTexture;
float _SeedConfidence;
struct Raster {float4 position:SV_Position;};
Raster TemporalVertex(uint id:SV_VertexID){Raster o;float2 uv=float2((id<<1)&2,id&2);o.position=float4(uv.x*2-1,1-uv.y*2,0,1);return o;}
float4 TemporalFragment(Raster i):SV_Target {return float4(_InputTexture.Load(int3(uint2(i.position.xy),0)).rgb,_SeedConfidence);}
ENDHLSL
Pass {Cull Off ZTest Always ZWrite Off Blend Off HLSLPROGRAM
#pragma target 5.0
#pragma vertex TemporalVertex
#pragma fragment TemporalFragment
ENDHLSL
}
}}
