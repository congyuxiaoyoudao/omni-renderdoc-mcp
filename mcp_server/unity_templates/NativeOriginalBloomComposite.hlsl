#ifndef OMNI_SOURCE_BLOOM_COMPOSITE_INCLUDED
#define OMNI_SOURCE_BLOOM_COMPOSITE_INCLUDED

// Extracted from the original color transform. The source calls this after
// chromatic aberration / sharpening / inverse pre-exposure and before vignette.
// bloomRadiance is a bilinear-clamp sample of the live plain-2D Bloom result.
// parameters.x: composite intensity; parameters.z: energy subtraction amount.
float3 NativeOriginalBloomComposite(float3 radiance, float3 bloomRadiance,
    float4 threshold, float4 parameters, float3 tint)
{
    float3 probe = bloomRadiance * (1.0f - parameters.z);
    float3 compressed = (pow(bloomRadiance, 0.3300000131130218505859375f.xxx)
        * 1.49380004405975341796875f) - 0.699999988079071044921875f.xxx;
    float3 glow = float3(probe.x > 0.300000011920928955078125f ? compressed.x : bloomRadiance.x,
                        probe.y > 0.300000011920928955078125f ? compressed.y : bloomRadiance.y,
                        probe.z > 0.300000011920928955078125f ? compressed.z : bloomRadiance.z);
    float peak = max(max(radiance.x, radiance.y), radiance.z);
    float soft = clamp(peak - threshold.y, 0.0f, threshold.z);
    float amount = max((threshold.w * soft) * soft, peak - threshold.x)
        / max(peak, 9.9999997473787516355514526367188e-05f);
    return lerp(radiance, (radiance - ((radiance * amount) * parameters.z)) + (glow * tint), parameters.x.xxx);
}

#endif
