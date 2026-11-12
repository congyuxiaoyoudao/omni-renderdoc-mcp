Shader "Hidden/OmniRecovered/CopyFinal"
{
    Properties { _MainTex("Rendered source", 2D) = "black" {} _DecodeSRGB("Decode already graded sRGB", Float) = 0 _FlipY("Present captured swapchain orientation", Float) = 0 }
    SubShader
    {
        Tags { "RenderPipeline"="HDRenderPipeline" }
        Pass
        {
            ZTest Always ZWrite Off Cull Off
            HLSLPROGRAM
            #pragma vertex vert_img
            #pragma fragment Fragment
            #include "UnityCG.cginc"
            sampler2D _MainTex;
            float _DecodeSRGB;
            float _FlipY;
            float4 Fragment(v2f_img input) : SV_Target
            {
                float2 uv = input.uv;
                if (_FlipY > 0.5) uv.y = 1 - uv.y;
                float4 color = tex2D(_MainTex, uv);
                if (_DecodeSRGB > 0.5)
                {
                    float3 low = color.rgb / 12.92;
                    float3 high = pow(max((color.rgb + 0.055) / 1.055, 0), 2.4);
                    color.rgb = lerp(low, high, step(0.04045, color.rgb));
                }
                return color;
            }
            ENDHLSL
        }
    }
}
