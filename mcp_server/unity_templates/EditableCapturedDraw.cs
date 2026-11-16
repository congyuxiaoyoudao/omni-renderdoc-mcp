using UnityEngine;

namespace OmniRenderDoc
{
    // A selectable scene object supplies geometry and material to the original
    // captured render graph. The renderer itself must not also render in HDRP.
    [ExecuteAlways, DisallowMultipleComponent]
    [RequireComponent(typeof(MeshFilter), typeof(MeshRenderer))]
    public sealed class EditableCapturedDraw : MonoBehaviour
    {
        public MeshFilter geometry;
        public MeshRenderer surface;
        public Transform sharedTransform;
        public Transform editingTransform { get => sharedTransform; set => sharedTransform = value; }
        public int eventId, instance;
        public bool primary;
        public Matrix4x4 referenceLocalToWorld = Matrix4x4.identity;
        [Tooltip("Explicit mapping from captured geometry space to fragment shading space.")]
        public Matrix4x4 geometryToShading = Matrix4x4.identity;
        [Tooltip("Original fragment TEXCOORD locations; -1 leaves captured values unchanged.")]
        public int positionLocation = -1, normalLocation = -1, tangentLocation = -1;

        void OnEnable() { ResolveComponents(); }
        void OnValidate() { ResolveComponents(); }
        void ResolveComponents()
        {
            if (geometry == null) geometry = GetComponent<MeshFilter>();
            if (surface == null) surface = GetComponent<MeshRenderer>();
            if (surface != null) surface.forceRenderingOff = true;
        }

        public bool TryGetDraw(out Mesh mesh, out Material material, out Matrix4x4 objectToWorld)
        {
            mesh = null;
            material = null;
            objectToWorld = Matrix4x4.identity;
            if (!isActiveAndEnabled || geometry == null || surface == null || !surface.enabled ||
                !geometry.gameObject.activeInHierarchy || !surface.gameObject.activeInHierarchy) return false;
            mesh = geometry.sharedMesh;
            material = surface.sharedMaterial;
            objectToWorld = geometry.transform.localToWorldMatrix;
            // forceRenderingOff suppresses only Unity's ordinary render path.
            return mesh != null && material != null;
        }

        public void ApplyVaryingProperties(MaterialPropertyBlock properties, Matrix4x4 objectToWorld)
        {
            properties.SetInteger("_CapturedPositionVarying", positionLocation);
            properties.SetInteger("_CapturedNormalVarying", normalLocation);
            properties.SetInteger("_CapturedTangentVarying", tangentLocation);
            var delta = geometryToShading * objectToWorld * referenceLocalToWorld.inverse * geometryToShading.inverse;
            properties.SetMatrix("_CapturedVaryingTransform", delta);
            properties.SetMatrix("_CapturedVaryingNormalTransform", delta.inverse.transpose);
            properties.SetMatrix("_CapturedMeshDisplacementToShading", geometryToShading * objectToWorld);
        }

        void OnDrawGizmosSelected()
        {
            if (geometry == null || geometry.sharedMesh == null) return;
            var previousMatrix = Gizmos.matrix;
            var previousColor = Gizmos.color;
            Gizmos.matrix = geometry.transform.localToWorldMatrix;
            Gizmos.color = new Color(0.2f, 0.8f, 1f, 0.9f);
            var bounds = geometry.sharedMesh.bounds;
            Gizmos.DrawWireCube(bounds.center, bounds.size);
            Gizmos.matrix = previousMatrix;
            Gizmos.color = previousColor;
        }
    }
}
