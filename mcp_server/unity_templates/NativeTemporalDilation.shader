Shader "Hidden/OmniRenderDoc/NativeTemporalDilation"
{
Properties { _CurrentDepth("_CurrentDepth",2D)="black" {}
_CurrentVelocityClass("_CurrentVelocityClass",2D)="black" {}
_PreviousDepth("_PreviousDepth",2D)="black" {}
_PreviousVelocityClass("_PreviousVelocityClass",2D)="black" {} }
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
// Empty-depth selection is an explicit profile policy.





SamplerState Temporal_point_clamp_sampler;
Texture2D<float4> _CurrentDepth;
Texture2D<float4> _CurrentVelocityClass;
Texture2D<float4> _PreviousDepth;
Texture2D<float4> _PreviousVelocityClass;

static float4 gl_FragCoord;
static float _4;
static float4 _5;

struct SPIRV_Cross_Input
{
    float4 gl_FragCoord : SV_Position;
};

struct SPIRV_Cross_Output
{
    float _4 : SV_Target0;
    float4 _5 : SV_Target1;
};

void frag_main()
{
    int3 _82 = int3(int(gl_FragCoord.x), int(gl_FragCoord.y), 0);
    float2 _84 = float2(_82.xy);
    float2 _88 = _84 * _Temporal6.zw;
    float4 _92 = _CurrentDepth.GatherRed(Temporal_point_clamp_sampler, _88);
    float4 _101 = _CurrentDepth.GatherRed(Temporal_point_clamp_sampler, _88 + (float2(_Temporal6.z, 0.0f) * 2.0f));
    float4 _110 = _CurrentDepth.GatherRed(Temporal_point_clamp_sampler, _88 + (float2(0.0f, _Temporal6.w) * 2.0f));
    float4 _116 = _CurrentDepth.GatherRed(Temporal_point_clamp_sampler, _88 + (_Temporal6.zw * 2.0f));
    float _117 = _92.w;
    float _118 = _92.z;
    float _119 = _101.w;
    float _120 = _92.x;
    float _121 = _92.y;
    float _122 = _101.x;
    float _123 = _110.w;
    float _124 = _110.z;
    float _125 = _116.w;
    bool _126 = 0.0f < _117;
    float _127 = _126 ? _117 : 0.0f;
    bool2 _128 = _126.xx;
    float2 _129 = float2(_128.x ? (-1.0f).xx.x : _TemporalEmptyDepthOffset.xy.x, _128.y ? (-1.0f).xx.y : _TemporalEmptyDepthOffset.xy.y);
    bool _130 = _127 < _118;
    float _131 = _130 ? _118 : _127;
    bool2 _132 = _130.xx;
    float2 _133 = float2(_132.x ? float2(0.0f, -1.0f).x : _129.x, _132.y ? float2(0.0f, -1.0f).y : _129.y);
    bool _134 = _131 < _119;
    float _135 = _134 ? _119 : _131;
    bool2 _136 = _134.xx;
    float2 _137 = float2(_136.x ? float2(1.0f, -1.0f).x : _133.x, _136.y ? float2(1.0f, -1.0f).y : _133.y);
    bool _138 = _135 < _120;
    float _139 = _138 ? _120 : _135;
    bool2 _140 = _138.xx;
    float2 _141 = float2(_140.x ? float2(-1.0f, 0.0f).x : _137.x, _140.y ? float2(-1.0f, 0.0f).y : _137.y);
    bool _142 = _139 < _121;
    float _143 = _142 ? _121 : _139;
    bool2 _144 = _142.xx;
    float2 _145 = float2(_144.x ? 0.0f.xx.x : _141.x, _144.y ? 0.0f.xx.y : _141.y);
    bool _146 = _143 < _122;
    float _147 = _146 ? _122 : _143;
    bool2 _148 = _146.xx;
    float2 _149 = float2(_148.x ? float2(1.0f, 0.0f).x : _145.x, _148.y ? float2(1.0f, 0.0f).y : _145.y);
    bool _150 = _147 < _123;
    float _151 = _150 ? _123 : _147;
    bool2 _152 = _150.xx;
    float2 _153 = float2(_152.x ? float2(-1.0f, 1.0f).x : _149.x, _152.y ? float2(-1.0f, 1.0f).y : _149.y);
    bool _154 = _151 < _124;
    float _155 = _154 ? _124 : _151;
    bool2 _156 = _154.xx;
    float2 _157 = float2(_156.x ? float2(0.0f, 1.0f).x : _153.x, _156.y ? float2(0.0f, 1.0f).y : _153.y);
    bool _158 = _155 < _125;
    float _159 = _158 ? _125 : _155;
    bool2 _160 = _158.xx;
    float2 _162 = _84 + float2(_160.x ? 1.0f.xx.x : _157.x, _160.y ? 1.0f.xx.y : _157.y);
    float4 _170 = _CurrentVelocityClass.Load(int3(int3(int(_162.x), int(_162.y), 0).xy, 0));
    float _171 = _170.w;
    float _177 = float(abs(_171 - 0.300000011920928955078125f) < 0.100000001490116119384765625f);
    float2 _178 = _170.xy;
    float2 _185 = (abs(_178) * 2.0f) - 1.0f.xx;
    float2 _186 = _185 * _185;
    float2 _188 = (_186 * _186) * float2(int2(sign(_178 - 0.5f.xx)));
    float2 _192 = _188 * _Temporal6.xy;
    int3 _200 = int3((float3(_82) + float3(0.5f, 0.5f, 0.0f)) - float3(int3(int(_192.x), int(_192.y), 0)));
    float2 _202 = (_88 * 2.0f) - 1.0f.xx;
    float4 _205 = float4(_202, _159, 1.0f);
    _205.y = -_202.y;
    float4 _210 = mul(_TemporalCurrentToPreviousClip, _205);
    int2 _214 = _200.xy;
    int _215 = _200.z;
    float4 _220 = _PreviousVelocityClass.Load(int3(_214, _215));
    float2 _221 = _220.xy;
    float2 _228 = (abs(_221) * 2.0f) - 1.0f.xx;
    float2 _229 = _228 * _228;
    float _232 = _220.w;
    float2 _251 = abs(_188.xy - ((_229 * _229) * float2(int2(sign(_221 - 0.5f.xx)))).xy);
    float _262 = _170.z;
    _170.z = (float(uint(float((uint(_262) | (uint(min(1.0f, clamp((clamp(float((_PreviousDepth.Load(int3(_214, _215)).x - (_210.z / _210.w)) > _Temporal1.y) + abs(_177 - float(abs(_232 - 0.300000011920928955078125f) < 0.100000001490116119384765625f)), 0.0f, 1.0f) * smoothstep(9.9999997473787516355514526367188e-05f, 0.0005000000237487256526947021484375f, _251.x + _251.y)) + abs(float(_171 > 0.89999997615814208984375f) - float(_232 > 0.89999997615814208984375f)), 0.0f, 1.0f)) * (1.0f - _177)) << 1u)) | (uint(float((_262 - float(uint((_220.z * 1023.0f) + 0.5f) & 1u)) < 0.0f)) << 2u)))) + 0.5f) * 0.000977517105638980865478515625f;
    _4 = _159;
    _5 = _170;
}


SPIRV_Cross_Output TemporalFragment(TemporalRaster i) { gl_FragCoord=i.position; frag_main(); SPIRV_Cross_Output o; o._4=_4; o._5=_5; return o; }
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
