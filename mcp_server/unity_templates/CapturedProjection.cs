using UnityEngine;

namespace OmniRenderDoc
{
    // Camera.projectionMatrix itself is not serialized by Unity. Keep the
    // replay projection in the scene so reopening and entering Play preserve it.
    [ExecuteAlways, RequireComponent(typeof(Camera))]
    public sealed class CapturedProjection : MonoBehaviour
    {
        public float p00, p11;
        public int width, height;

        public void Apply()
        {
            if (width <= 0 || height <= 0 || !float.IsFinite(p00) || !float.IsFinite(p11) || p00 <= 0 || p11 <= 0) return;
            var camera = GetComponent<Camera>();
            if (camera == null) return;
            camera.aspect = (float)width / height;
            camera.fieldOfView = 2f * Mathf.Atan(1f / p11) * Mathf.Rad2Deg;
            var matrix = Matrix4x4.Perspective(camera.fieldOfView, camera.aspect,
                camera.nearClipPlane, camera.farClipPlane);
            matrix.m00 = p00;
            matrix.m11 = p11;
            camera.projectionMatrix = matrix;
        }

        void OnEnable() { Apply(); }
        void OnValidate() { Apply(); }
        void LateUpdate() { Apply(); }
        void OnDisable()
        {
            var camera = GetComponent<Camera>();
            if (camera != null) camera.ResetProjectionMatrix();
        }
    }
}
