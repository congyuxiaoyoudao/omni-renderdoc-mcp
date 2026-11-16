using System;
using UnityEngine;
namespace OmniRenderDoc
{
    [CreateAssetMenu(menuName="Omni RenderDoc/Native temporal profile")]
    public sealed class NativeTemporalProfile : ScriptableObject
    {
        public Shader dilation,reduction,resolve,seed,copy;
        public NativeTemporalHistory.Settings settings=new NativeTemporalHistory.Settings();
        // The sequence is an explicit export/runtime policy. A captured frame
        // establishes its sample, not the entire original host-side sequence.
        public Vector2[] jitterSequence=Array.Empty<Vector2>();
        public bool renderSceneView=true,renderGame=true;
        public bool enabledEffect=true;
        // Export-side audit must account for all current source motion/reactive
        // writers, including transparent partial-channel/additive metadata.
        public bool motionCoverageVerified;
        public int revision;
    }
}
