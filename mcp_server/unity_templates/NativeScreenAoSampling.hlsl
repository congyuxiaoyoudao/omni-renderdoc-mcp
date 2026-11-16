#ifndef OMNI_NATIVE_SCREEN_AO_SAMPLING_INCLUDED
#define OMNI_NATIVE_SCREEN_AO_SAMPLING_INCLUDED

// These are command-buffer globals, deliberately absent from material
// Properties/UnityPerMaterial. The producer binds them for the current camera
// after the opaque depth/normal prepass and before opaque lighting.
Texture2D<float> _NativeEnvironmentAOTexture;
float _NativeEnvironmentAOValid;
float4 _NativeEnvironmentAOSize; // width, height, reciprocal width/height

// positionCS.xy is the current raster pixel position. A pixel load avoids
// RTHandle max-allocation scaling and avoids importing captured screen UVs.
// The runtime currently publishes an exact-viewport, full-resolution 2D AO.
float NativeEnvironmentScreenAO(float2 pixelPosition)
{
    if (_NativeEnvironmentAOValid < 0.5)
        return 1.0;
    int2 pixel = clamp(int2(pixelPosition), int2(0, 0),
        int2(_NativeEnvironmentAOSize.xy) - 1);
    return _NativeEnvironmentAOTexture.Load(int3(pixel, 0));
}

#endif
