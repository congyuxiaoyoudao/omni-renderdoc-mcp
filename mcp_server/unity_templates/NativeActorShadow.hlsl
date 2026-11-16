#ifndef OMNI_NATIVE_ACTOR_SHADOW_INCLUDED
#define OMNI_NATIVE_ACTOR_SHADOW_INCLUDED
// Current independently rendered geometry, never an imported image of a frame.
Texture2D<float> _NativeActorShadowDepth;
SamplerState sampler_NativeActorShadow_Linear_Clamp;
float _NativeActorShadowValid;
float _NativeActorShadowDebug;
float _NativeActorShadowVisibilityQuantization;
float4x4 _NativeActorWorldToShadowUVZ;
float4x4 _NativeActorShadowNormalToBiasSpace;
float4x4 _NativeActorShadowBiasToWorld;
float4 _NativeActorShadowForward;
float4 _NativeActorShadowBias;
float4 _NativeActorShadowFilterRadius;
float4x4 _NativeActorShadowCasterWorldToClip;
float4x4 _NativeActorShadowCasterWorldToUVZ;
float _NativeActorShadowCasterDepthQuantization;
float _NativeActorShadowCasterDepthBias;
float _NativeActorShadowCasterUseCoverage;
static const float2 NativeActorPoisson[16] = { float2(-0.94201624393463134765625f, -0.39906215667724609375f), float2(0.94558608531951904296875f, -0.768907248973846435546875f), float2(-0.094184100627899169921875f, -0.929388701915740966796875f), float2(0.34495937824249267578125f, 0.29387760162353515625f), float2(-0.91588580608367919921875f, 0.4577143192291259765625f), float2(-0.8154423236846923828125f, -0.87912464141845703125f), float2(-0.38277542591094970703125f, 0.2767684459686279296875f), float2(0.9748439788818359375f, 0.7564837932586669921875f), float2(0.4432332515716552734375f, -0.9751155376434326171875f), float2(0.5374298095703125f, -0.473734200000762939453125f), float2(-0.2649691104888916015625f, -0.418930232524871826171875f), float2(0.79197514057159423828125f, 0.19090187549591064453125f), float2(-0.24188840389251708984375f, 0.997065067291259765625f), float2(-0.8140995502471923828125f, 0.91437590122222900390625f), float2(0.1998412609100341796875f, 0.786413669586181640625f), float2(0.14383161067962646484375f, -0.141007900238037109375f) };
static const float2 NativeActorRotations[16] = { float2(-0.3996559083461761474609375f, 0.91666519641876220703125f), float2(0.12451229989528656005859375f, -0.992218077182769775390625f), float2(0.8523542881011962890625f, 0.5229647159576416015625f), float2(-0.22931249439716339111328125f, 0.973352909088134765625f), float2(-0.772406101226806640625f, 0.63512897491455078125f), float2(0.7927525043487548828125f, -0.60954368114471435546875f), float2(-0.578049719333648681640625f, 0.816001594066619873046875f), float2(-0.831129610538482666015625f, -0.55607879161834716796875f), float2(0.8077948093414306640625f, 0.589463770389556884765625f), float2(0.47141540050506591796875f, 0.88191127777099609375f), float2(-0.3139738142490386962890625f, -0.949431717395782470703125f), float2(-0.94500672817230224609375f, -0.3270510137081146240234375f), float2(-0.1850374042987823486328125f, -0.982731521129608154296875f), float2(-0.9337558746337890625f, 0.35791051387786865234375f), float2(-0.997614085674285888671875f, 0.069036297500133514404296875f), float2(0.3061277866363525390625f, 0.951990425586700439453125f) };

float NativeActorShadowVisibility(float2 pixel, float3 positionRWS, float3 normalWS)
{
    if (_NativeActorShadowValid < 0.5) return 1;
    float3 position=GetAbsolutePositionWS(positionRWS);
    float3 normalBias=normalize(mul((float3x3)_NativeActorShadowNormalToBiasSpace,normalWS));
    float bias=1-clamp(dot(normalBias,_NativeActorShadowForward.xyz),0,0.9);
    float3 displacement=-_NativeActorShadowForward.xyz*(bias*_NativeActorShadowBias.x)
        +normalBias*(bias*_NativeActorShadowBias.y);
    position+=mul((float3x3)_NativeActorShadowBiasToWorld,displacement);
    float3 projected=mul(_NativeActorWorldToShadowUVZ,float4(position,1)).xyz;
    projected.z=max(projected.z,0.01);
    if(any(projected<=0)||any(projected>=1)) return 1;
    uint2 tile=(uint2)pixel%uint2(4,4);
    float2 rotation=NativeActorRotations[tile.x*4+tile.y];
    float2x2 orientation=float2x2(rotation,float2(-rotation.y,rotation.x));
    float sumDistance=0, hits=0;
    [unroll] for(uint tap=0;tap<16;tap++)
    {
        float2 uv=projected.xy+mul(NativeActorPoisson[tap],orientation)*_NativeActorShadowFilterRadius.xy;
        float4 difference=_NativeActorShadowDepth.GatherRed(sampler_NativeActorShadow_Linear_Clamp,uv)-projected.zzzz;
        float4 hit=step(0.0.xxxx,difference);
        sumDistance+=dot(difference,hit);
        hits+=dot(hit,1.0.xxxx);
    }
    // Explicitly preserve the original SPIR-V NMin result for its 0/0 branch.
    if(hits==0) return 1;
    float remap=2*saturate(hits*(1.0/64.0))-1;
    float direction=sign(remap);
    float inverse=1-direction*remap;
    float shape=lerp(inverse*inverse*inverse,inverse,saturate((sumDistance/hits)/projected.z));
    float visibility=min(1,0.5-0.5*(1-shape)*direction);
    return _NativeActorShadowVisibilityQuantization>0
        ? round(saturate(visibility)*_NativeActorShadowVisibilityQuantization)/_NativeActorShadowVisibilityQuantization : visibility;
}
#endif
