Shader "Hidden/OmniRenderDoc/NativeTemporalResolve"
{
Properties { _CurrentColor("_CurrentColor",2D)="black" {}
_CurrentDepth("_CurrentDepth",2D)="black" {}
_DilatedVelocityClass("_DilatedVelocityClass",2D)="black" {}
_ReducedRejection("_ReducedRejection",2D)="black" {}
_PreviousColor("_PreviousColor",2D)="black" {} }
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
static float _134;
static float4 _145;

static const uint _139[4] = { 27u, 54u, 216u, 432u };





SamplerState Temporal_point_clamp_sampler;
SamplerState Temporal_linear_clamp_sampler;
Texture2D<float4> _CurrentColor;
Texture2D<float4> _CurrentDepth;
Texture2D<float4> _DilatedVelocityClass;
Texture2D<float4> _ReducedRejection;
Texture2D<float4> _PreviousColor;

static float4 gl_FragCoord;
static float4 _4;

struct SPIRV_Cross_Input
{
    float4 gl_FragCoord : SV_Position;
};

struct SPIRV_Cross_Output
{
    float4 _4 : SV_Target0;
};

void frag_main()
{
    float2 _157 = gl_FragCoord.xy * _Temporal7.zw;
    float2 _161 = gl_FragCoord.xy * _Temporal8.xy;
    float2 _162 = floor(_161);
    int3 _167 = int3(int(_162.x), int(_162.y), 0);
    float2 _177 = floor(clamp(_161 - _TemporalJitter.xy, 0.5f.xx, _Temporal6.xy - 0.5f.xx));
    int3 _182 = int3(int(_177.x), int(_177.y), 0);
    float4 _191 = _CurrentDepth.SampleBias(Temporal_point_clamp_sampler, (_177 + 0.5f.xx) * _Temporal6.zw, _TemporalMipBias);
    float4 _195 = _DilatedVelocityClass.Load(int3(_182.xy, 0));
    float2 _196 = _195.xy;
    float2 _203 = (abs(_196) * 2.0f) - 1.0f.xx;
    float2 _204 = _203 * _203;
    float2 _206 = (_204 * _204) * float2(int2(sign(_196 - 0.5f.xx)));
    float2 _208 = _157 - _206.xy;
    float _216 = _195.w;
    float _218 = float(_216 > 0.89999997615814208984375f);
    float _222 = float(abs(_216 - 0.60000002384185791015625f) < 0.100000001490116119384765625f);
    bool3 _228 = (_222 != 0.0f).xxx;
    int3 _229 = int3(_228.x ? _167.x : _182.x, _228.y ? _167.y : _182.y, _228.z ? _167.z : _182.z);
    int3 _235 = int3(int(_Temporal6.x), int(_Temporal6.y), 0);
    int3 _236 = clamp(_229 + int3(-1, -1, 0), int3(0, 0, 0), _235);
    float3 _245 = (-min(-_CurrentColor.Load(int3(_236.xy, _236.z)).xyz, 0.0f.xxx)).xyz;
    float _246 = dot(_245, float3(1.0f, 2.0f, 1.0f));
    float3 _249 = float3(_246, dot(_245, float3(2.0f, 0.0f, -2.0f)), dot(_245, float3(-1.0f, 2.0f, -1.0f)));
    float4 _250 = float4(_249.x, _249.y, _249.z, _145.w);
    _250.w = 1.0f / ((_246 * _Temporal4.w) + 4.0f);
    int3 _262 = clamp(_229 + int3(0, -1, 0), int3(0, 0, 0), _235);
    float3 _271 = (-min(-_CurrentColor.Load(int3(_262.xy, _262.z)).xyz, 0.0f.xxx)).xyz;
    float _272 = dot(_271, float3(1.0f, 2.0f, 1.0f));
    float3 _275 = float3(_272, dot(_271, float3(2.0f, 0.0f, -2.0f)), dot(_271, float3(-1.0f, 2.0f, -1.0f)));
    float4 _276 = float4(_275.x, _275.y, _275.z, _250.w);
    _276.w = 1.0f / ((_272 * _Temporal4.w) + 4.0f);
    int3 _288 = clamp(_229 + int3(1, -1, 0), int3(0, 0, 0), _235);
    float3 _297 = (-min(-_CurrentColor.Load(int3(_288.xy, _288.z)).xyz, 0.0f.xxx)).xyz;
    float _298 = dot(_297, float3(1.0f, 2.0f, 1.0f));
    float3 _301 = float3(_298, dot(_297, float3(2.0f, 0.0f, -2.0f)), dot(_297, float3(-1.0f, 2.0f, -1.0f)));
    float4 _302 = float4(_301.x, _301.y, _301.z, _276.w);
    _302.w = 1.0f / ((_298 * _Temporal4.w) + 4.0f);
    int3 _314 = clamp(_229 + int3(-1, 0, 0), int3(0, 0, 0), _235);
    float3 _323 = (-min(-_CurrentColor.Load(int3(_314.xy, _314.z)).xyz, 0.0f.xxx)).xyz;
    float _324 = dot(_323, float3(1.0f, 2.0f, 1.0f));
    float3 _327 = float3(_324, dot(_323, float3(2.0f, 0.0f, -2.0f)), dot(_323, float3(-1.0f, 2.0f, -1.0f)));
    float4 _328 = float4(_327.x, _327.y, _327.z, _302.w);
    _328.w = 1.0f / ((_324 * _Temporal4.w) + 4.0f);
    int3 _339 = clamp(_229, int3(0, 0, 0), _235);
    float3 _348 = (-min(-_CurrentColor.Load(int3(_339.xy, _339.z)).xyz, 0.0f.xxx)).xyz;
    float _349 = dot(_348, float3(1.0f, 2.0f, 1.0f));
    float3 _352 = float3(_349, dot(_348, float3(2.0f, 0.0f, -2.0f)), dot(_348, float3(-1.0f, 2.0f, -1.0f)));
    float4 _353 = float4(_352.x, _352.y, _352.z, _328.w);
    _353.w = 1.0f / ((_349 * _Temporal4.w) + 4.0f);
    int3 _365 = clamp(_229 + int3(1, 0, 0), int3(0, 0, 0), _235);
    float3 _374 = (-min(-_CurrentColor.Load(int3(_365.xy, _365.z)).xyz, 0.0f.xxx)).xyz;
    float _375 = dot(_374, float3(1.0f, 2.0f, 1.0f));
    float3 _378 = float3(_375, dot(_374, float3(2.0f, 0.0f, -2.0f)), dot(_374, float3(-1.0f, 2.0f, -1.0f)));
    float4 _379 = float4(_378.x, _378.y, _378.z, _353.w);
    _379.w = 1.0f / ((_375 * _Temporal4.w) + 4.0f);
    int3 _391 = clamp(_229 + int3(-1, 1, 0), int3(0, 0, 0), _235);
    float3 _400 = (-min(-_CurrentColor.Load(int3(_391.xy, _391.z)).xyz, 0.0f.xxx)).xyz;
    float _401 = dot(_400, float3(1.0f, 2.0f, 1.0f));
    float3 _404 = float3(_401, dot(_400, float3(2.0f, 0.0f, -2.0f)), dot(_400, float3(-1.0f, 2.0f, -1.0f)));
    float4 _405 = float4(_404.x, _404.y, _404.z, _379.w);
    _405.w = 1.0f / ((_401 * _Temporal4.w) + 4.0f);
    int3 _417 = clamp(_229 + int3(0, 1, 0), int3(0, 0, 0), _235);
    float3 _426 = (-min(-_CurrentColor.Load(int3(_417.xy, _417.z)).xyz, 0.0f.xxx)).xyz;
    float _427 = dot(_426, float3(1.0f, 2.0f, 1.0f));
    float3 _430 = float3(_427, dot(_426, float3(2.0f, 0.0f, -2.0f)), dot(_426, float3(-1.0f, 2.0f, -1.0f)));
    float4 _431 = float4(_430.x, _430.y, _430.z, _405.w);
    _431.w = 1.0f / ((_427 * _Temporal4.w) + 4.0f);
    int3 _443 = clamp(_229 + int3(1, 1, 0), int3(0, 0, 0), _235);
    float3 _452 = (-min(-_CurrentColor.Load(int3(_443.xy, _443.z)).xyz, 0.0f.xxx)).xyz;
    float _453 = dot(_452, float3(1.0f, 2.0f, 1.0f));
    float3 _456 = float3(_453, dot(_452, float3(2.0f, 0.0f, -2.0f)), dot(_452, float3(-1.0f, 2.0f, -1.0f)));
    float4 _457 = float4(_456.x, _456.y, _456.z, _431.w);
    _457.w = 1.0f / ((_453 * _Temporal4.w) + 4.0f);
    float3 _469 = (((((((((_245 * _Temporal9[0u].x) + (_271 * _Temporal9[0u].y)) + (_297 * _Temporal9[0u].z)) + (_323 * _Temporal9[0u].w)) + (_348 * _Temporal9[1u].x)) + (_374 * _Temporal9[1u].y)) + (_400 * _Temporal9[1u].z)) + (_426 * _Temporal9[1u].w)) + (_452 * _Temporal9[2u].x)) * (1.0f.xxx / ((((((((_Temporal9[0u].x.xxx + _Temporal9[0u].y.xxx) + _Temporal9[0u].z.xxx) + _Temporal9[0u].w.xxx) + _Temporal9[1u].x.xxx) + _Temporal9[1u].y.xxx) + _Temporal9[1u].z.xxx) + _Temporal9[1u].w.xxx) + _Temporal9[2u].x.xxx));
    float _470 = dot(_469, float3(1.0f, 2.0f, 1.0f));
    float3 _476 = lerp(_353.xyz, _353.xyz, _218.xxx);
    float4 _477[9] = { _250, _276, _302, _328, _353, _379, _405, _431, _457 };
    float2 _479 = _208 * _Temporal7.xy;
    float2 _481 = floor(_479 - 0.5f.xx);
    float2 _482 = _481 + 0.5f.xx;
    float2 _483 = _479 - _482;
    float2 _484 = _483 * _483;
    float2 _485 = _484 * _483;
    float2 _488 = _484 - ((_485 + _483) * 0.5f);
    float2 _492 = ((_485 * 1.5f) - (_484 * 2.5f)) + 1.0f.xx;
    float2 _494 = (_485 - _484) * 0.5f;
    float2 _497 = ((1.0f.xx - _488) - _492) - _494;
    float2 _498 = _492 + _497;
    float2 _504 = (_481 + (-0.5f).xx) * _Temporal7.zw;
    float2 _505 = (_482 + (_497 * (1.0f.xx / _498))) * _Temporal7.zw;
    float2 _506 = (_481 + 2.5f.xx) * _Temporal7.zw;
    float _517 = _498.x;
    float _519 = _517 * _488.y;
    float _521 = _498.y;
    float _522 = _488.x * _521;
    float _523 = _517 * _521;
    float _525 = _494.x * _521;
    float _527 = _517 * _494.y;
    float _533[5] = { _519, _522, _523, _525, _527 };
    float2 _534[5] = { float2(_505.x, _504.y), float2(_504.x, _505.y), _505, float2(_506.x, _505.y), float2(_505.x, _506.y) };
    float2 _150[5] = _534;
    float _149[5] = _533;
    float3 _536;
    _536 = 0.0f.xxx;
    for (uint _539 = 0u; _539 < 5u; )
    {
        float3 _550 = _PreviousColor.SampleLevel(Temporal_linear_clamp_sampler, clamp(_150[_539], 0.0f.xx, 1.0f.xx), 0.0f).xyz;
        _536 += ((_550 / (1.0f + dot(_550, float3(0.2125999927520751953125f, 0.715200006961822509765625f, 0.072200000286102294921875f))).xxx) * _149[_539]);
        _539++;
        continue;
    }
    float3 _558 = _536 * (1.0f / ((((_519 + _522) + _523) + _525) + _527));
    float4 _566 = _PreviousColor.SampleLevel(Temporal_linear_clamp_sampler, _208, 0.0f);
    float3 _567 = _566.xyz;
    float3 _570 = clamp(_558 / (1.0f - dot(_558, float3(0.2125999927520751953125f, 0.715200006961822509765625f, 0.072200000286102294921875f))).xxx, _567 * 0.20000000298023223876953125f, _567 * 1.7999999523162841796875f);
    float3 _574 = float3(dot(_570, float3(1.0f, 2.0f, 1.0f)), dot(_570, float3(2.0f, 0.0f, -2.0f)), dot(_570, float3(-1.0f, 2.0f, -1.0f)));
    float _577 = abs(_206.x);
    float _579 = abs(_206.y);
    float _598 = float((uint((_195.z * 1023.0f) + 0.5f) >> 1u) & 1u);
    float2 _607 = abs(0.5f.xx - frac(_479)) * 2.0f;
    float2 _612 = 1.0f.xx - _Temporal7.zw;
    float _618 = float(any(bool2(_208.x > _612.x, _208.y > _612.y)) || any(bool2(_208.x < _Temporal7.zw.x, _208.y < _Temporal7.zw.y)));
    float _631 = clamp((_218 + _Temporal5.x) + _598, 0.0f, 1.0f);
    float4 _148[9] = _477;
    float _715;
    do
    {
        uint _657;
        float _660;
        float _662;
        _657 = 16u;
        _660 = 100000.0f;
        _662 = 0.0f;
        uint _658;
        float _661;
        float _663;
        for (int _664 = 0; _664 < 9; _657 = _658, _660 = _661, _662 = _663, _664++)
        {
            if (_664 == 4)
            {
                _658 = _657;
                _661 = _660;
                _663 = _662;
                continue;
            }
            float _676 = max(_148[_664].x, _148[4].x) / min(_148[_664].x, _148[4].x);
            uint _689;
            float _690;
            float _691;
            if ((_676 > 0.0f) && (_676 < 1.89999997615814208984375f))
            {
                _689 = _657 | (1u << (uint(_664) & 31u));
                _690 = _660;
                _691 = _662;
            }
            else
            {
                _689 = _657;
                _690 = min(_660, _148[_664].x);
                _691 = max(_662, _148[_664].x);
            }
            _658 = _689;
            _661 = _690;
            _663 = _691;
        }
        if (false == ((_148[4].x > _662) || (_148[4].x < _660)))
        {
            _715 = 0.0f;
            break;
        }
        float _712;
        bool _713;
        int _699 = 0;
        for (;;)
        {
            if (_699 < 4)
            {
                if ((_657 & _139[_699]) == _139[_699])
                {
                    _712 = 0.0f;
                    _713 = true;
                    break;
                }
                _699++;
                continue;
            }
            else
            {
                _712 = _134;
                _713 = false;
                break;
            }
        }
        if (_713)
        {
            _715 = _712;
            break;
        }
        _715 = 1.0f;
        break;
    } while(false);
    float _719 = max(_715, _566.w * 0.89999997615814208984375f) * max(0.0f, ((((((1.0f - float(_ReducedRejection.SampleLevel(Temporal_linear_clamp_sampler, _157, 0.0f).x > 0.0f)) - _631) - _618) - _Temporal1.w) - float(int((_577 + _579) > 0.300000011920928955078125f))) - _222) - _Temporal2.w);
    float _720 = step(0.100000001490116119384765625f, _719);
    float4 _147[9] = _477;
    float3 _723;
    float3 _726;
    _723 = 0.0f.xxx;
    _726 = 0.0f.xxx;
    for (uint _728 = 0u; _728 < 9u; )
    {
        _723 += (_147[_728].xyz * _147[_728].xyz);
        _726 += _147[_728].xyz;
        _728++;
        continue;
    }
    float3 _736 = _726 * 0.111111111938953399658203125f;
    float3 _741 = sqrt(abs((_723 * 0.111111111938953399658203125f) - (_736 * _736)));
    float3 _745 = (1.25f.xxx - (smoothstep(20.0f.xxx, 40.0f.xxx, _741) * 0.699999988079071044921875f)) * _741;
    float3 _754 = lerp(clamp(_574, min(_736 - _745, _476), max(_736 + _745, _476)), _574, (_720 * (1.0f - _618)).xxx);
    float3 _763 = lerp(lerp(_476, float3(_470, dot(_469, float3(2.0f, 0.0f, -2.0f)), dot(_469, float3(-1.0f, 2.0f, -1.0f))), _598.xxx) / (1.0f + lerp(_476.x, _470, _598)).xxx, _754 / (1.0f + _754.x).xxx, lerp(lerp(lerp(lerp(lerp(_Temporal0.z, _Temporal0.w, smoothstep(_Temporal3.y, _Temporal3.z, (_577 + (_579 * _TemporalScreenInfo.y)) * lerp(1.0f, 256.0f * _191.x, float(abs(_216 - 0.300000011920928955078125f) < 0.100000001490116119384765625f)))), 0.819999992847442626953125f, max(_607.x, _607.y)), _Temporal4.x, clamp((_618 + _222) + _Temporal2.w, 0.0f, 1.0f)), _Temporal2.z, _631), 0.89999997615814208984375f, _720).xxx);
    float3 _768 = _763 / max(1.0f - _763.x, 0.001000000047497451305389404296875f).xxx;
    float _770 = _768.x * 0.25f;
    float _772 = _768.y * 0.25f;
    float _774 = _768.z * 0.25f;
    _4 = float4(max(0.0f.xxx, float3((_770 + _772) - _774, _770 + _774, (_770 - _772) - _774)), _719);
}


float4 TemporalFragment(TemporalRaster i):SV_Target { gl_FragCoord=i.position; frag_main(); return _4; }
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
