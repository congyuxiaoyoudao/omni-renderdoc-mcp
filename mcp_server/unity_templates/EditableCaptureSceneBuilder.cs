using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace OmniRenderDoc
{
    public static class EditableCaptureSceneBuilder
    {
        [Serializable] public sealed class CameraSpec
        {
            public float p00, p11, near = 0.1f, far = 10000f;
            public int width, height;
            public float[] projection;
        }
        [Serializable] public sealed class GroupSpec { public string id, name; public float[] pivot; }
        [Serializable] public sealed class DrawSpec
        {
            public int event_id, instance;
            public string group_id;
            public bool primary;
            public int position_varying = -1, normal_varying = -1, tangent_varying = -1;
            public float[] geometry_to_shading;
        }
        [Serializable] public sealed class Manifest
        {
            public int version;
            public CameraSpec camera;
            public GroupSpec[] groups;
            public DrawSpec[] draws;
        }
        [Serializable] public sealed class Result
        {
            public string status, error, scene = Destination;
            public int editable_draw_count, group_count;
        }
        const string Source = "Assets/RecoveredCaptureScene.unity";
        const string Destination = "Assets/CaptureScene.unity";
        const string Backup = "Assets/CaptureScene_HDRPLit.unity";
        const string ManifestPath = "Assets/RecoveredShaders/editable_capture.json";
        const string EditableAssets = "Assets/RecoveredShaders/Editable";
        static string ProjectRoot => Directory.GetParent(Application.dataPath).FullName;
        static string Key(int eventId, int instance) => eventId + ":" + instance;
        static bool FinitePositive(float value) => float.IsFinite(value) && value > 0;

        static Matrix4x4 Matrix(float[] values, string label)
        {
            if (values == null || values.Length != 16) throw new InvalidDataException(label + " requires 16 row-major values.");
            var matrix = new Matrix4x4();
            for (int i = 0; i < values.Length; i++)
            {
                if (!float.IsFinite(values[i])) throw new InvalidDataException(label + " must be finite.");
                matrix[i / 4, i % 4] = values[i];
            }
            if (!float.IsFinite(matrix.determinant) || matrix.determinant == 0)
                throw new InvalidDataException(label + " must be invertible.");
            return matrix;
        }

        static void Validate(Manifest manifest)
        {
            if (manifest == null || manifest.version != 1) throw new InvalidDataException("Unsupported editable capture manifest.");
            var camera = manifest.camera;
            if (camera == null || camera.width <= 0 || camera.height <= 0 ||
                !FinitePositive(camera.p00) || !FinitePositive(camera.p11) ||
                !FinitePositive(camera.near) || !FinitePositive(camera.far) || camera.far <= camera.near)
                throw new InvalidDataException("An explicit finite projection and positive camera dimensions/clip range are required.");
            Matrix(camera.projection, "Captured projection");
            var groups = new HashSet<string>();
            foreach (var group in manifest.groups ?? Array.Empty<GroupSpec>())
            {
                if (group == null || string.IsNullOrWhiteSpace(group.id) || !groups.Add(group.id))
                    throw new InvalidDataException("Editable group IDs must be nonempty and unique.");
                if (group.pivot != null && (group.pivot.Length != 3 ||
                    !float.IsFinite(group.pivot[0]) || !float.IsFinite(group.pivot[1]) || !float.IsFinite(group.pivot[2])))
                    throw new InvalidDataException("A group pivot requires three finite values.");
            }
            var draws = new HashSet<string>();
            if (manifest.draws == null || manifest.draws.Length == 0) throw new InvalidDataException("No editable draws were supplied.");
            foreach (var draw in manifest.draws)
            {
                if (draw == null || draw.event_id <= 0 || draw.instance < 0 || !groups.Contains(draw.group_id) ||
                    !draws.Add(Key(draw.event_id, draw.instance))) throw new InvalidDataException("Invalid or duplicate editable draw binding.");
                if (draw.position_varying < -1 || draw.normal_varying < -1 || draw.tangent_varying < -1)
                    throw new InvalidDataException("Varying locations must be -1 or nonnegative.");
                if (draw.geometry_to_shading != null) Matrix(draw.geometry_to_shading, "Geometry-to-shading mapping");
            }
        }

        static string Digest(string value)
        {
            using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(value))).Replace("-", "").Substring(0, 24);
        }

        static T EditableCopy<T>(T source, string path) where T : UnityEngine.Object
        {
            var existing = AssetDatabase.LoadAssetAtPath<T>(path);
            if (existing != null) return existing;
            var copy = UnityEngine.Object.Instantiate(source);
            copy.name = source.name + " (editable)";
            AssetDatabase.CreateAsset(copy, path);
            return copy;
        }

        static bool SameGeometry(Mesh first, Mesh second)
        {
            if (first == second) return true;
            if (first.vertexCount != second.vertexCount || first.subMeshCount != second.subMeshCount) return false;
            var a = first.vertices; var b = second.vertices;
            for (int i = 0; i < a.Length; i++)
                if (BitConverter.SingleToInt32Bits(a[i].x) != BitConverter.SingleToInt32Bits(b[i].x) ||
                    BitConverter.SingleToInt32Bits(a[i].y) != BitConverter.SingleToInt32Bits(b[i].y) ||
                    BitConverter.SingleToInt32Bits(a[i].z) != BitConverter.SingleToInt32Bits(b[i].z)) return false;
            for (int submesh = 0; submesh < first.subMeshCount; submesh++)
            {
                if (first.GetTopology(submesh) != second.GetTopology(submesh)) return false;
                var x = first.GetIndices(submesh); var y = second.GetIndices(submesh);
                if (x.Length != y.Length) return false;
                for (int i = 0; i < x.Length; i++) if (x[i] != y[i]) return false;
            }
            return true;
        }

        [MenuItem("Tools/Omni RenderDoc/Build editable original fragment scene")]
        public static void Build()
        {
            if (!Application.isBatchMode && !EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo()) return;
            var result = new Result { status = "building" };
            try
            {
                var manifest = JsonUtility.FromJson<Manifest>(File.ReadAllText(Path.Combine(ProjectRoot, ManifestPath)));
                Validate(manifest);
                if (AssetDatabase.LoadAssetAtPath<SceneAsset>(Source) == null) throw new InvalidDataException("Build the recovered fragment scene first.");
                var scene = EditorSceneManager.OpenScene(Source, OpenSceneMode.Single);
                var runtimes = new List<CaptureFragmentRuntime>();
                var cameras = new List<Camera>();
                foreach (var root in scene.GetRootGameObjects())
                {
                    runtimes.AddRange(root.GetComponentsInChildren<CaptureFragmentRuntime>(true));
                    cameras.AddRange(root.GetComponentsInChildren<Camera>(true));
                }
                if (runtimes.Count != 1 || cameras.Count != 1)
                    throw new InvalidDataException("The recovered scene must contain one render graph and one reference camera.");
                var runtime = runtimes[0];
                var camera = cameras[0];
                if (runtime.sourceWidth != manifest.camera.width || runtime.sourceHeight != manifest.camera.height)
                    throw new InvalidDataException("Editable camera dimensions differ from the recovered capture.");
                var originalDraws = new Dictionary<string, CaptureFragmentRuntime.FragmentDraw>();
                foreach (var draw in runtime.draws)
                {
                    string key = Key(draw.eventId, draw.instance);
                    if (originalDraws.ContainsKey(key)) throw new InvalidDataException("The recovered graph contains duplicate draw keys: " + key);
                    originalDraws.Add(key, draw);
                }
                foreach (var spec in manifest.draws)
                    if (!originalDraws.TryGetValue(Key(spec.event_id, spec.instance), out var draw) || draw.mesh == null || draw.material == null)
                        throw new InvalidDataException("Editable draw has no matching recovered mesh/material: " + Key(spec.event_id, spec.instance));

                // Keep the destination .meta intact so existing scene references survive.
                if (AssetDatabase.LoadAssetAtPath<SceneAsset>(Destination) != null && AssetDatabase.LoadAssetAtPath<SceneAsset>(Backup) == null &&
                    !AssetDatabase.CopyAsset(Destination, Backup)) throw new IOException("Could not preserve the existing capture scene.");
                Directory.CreateDirectory(Path.Combine(ProjectRoot, EditableAssets));
                AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
                var collection = new GameObject("Editable captured geometry");
                var groups = new Dictionary<string, Transform>();
                foreach (var group in manifest.groups)
                {
                    var transform = new GameObject(string.IsNullOrEmpty(group.name) ? group.id : group.name).transform;
                    transform.SetParent(collection.transform, false);
                    transform.localPosition = group.pivot == null ? Vector3.zero : new Vector3(group.pivot[0], group.pivot[1], group.pivot[2]);
                    groups.Add(group.id, transform);
                }
                var meshCopies = new Dictionary<Mesh, Mesh>();
                var groupSourceMeshes = new Dictionary<string, Mesh>();
                foreach (var spec in manifest.draws)
                {
                    var draw = originalDraws[Key(spec.event_id, spec.instance)];
                    var group = groups[spec.group_id];
                    string stem = "E" + spec.event_id + "_I" + spec.instance;
                    var go = new GameObject(stem + (spec.primary ? " (surface)" : " (pass)"));
                    go.transform.SetParent(group, false);
                    go.transform.localPosition = -group.localPosition;
                    var geometry = go.AddComponent<MeshFilter>();
                    var surface = go.AddComponent<MeshRenderer>();
                    if (!meshCopies.TryGetValue(draw.mesh, out var editableMesh))
                    {
                        // A shared transform alone does not propagate vertex edits
                        // between depth and color passes. Share an edit asset only
                        // after verifying identical original positions and indices.
                        if (groupSourceMeshes.TryGetValue(spec.group_id, out var groupMesh) && SameGeometry(groupMesh, draw.mesh))
                            editableMesh = meshCopies[groupMesh];
                        else
                        {
                            string identity = AssetDatabase.GetAssetPath(draw.mesh);
                            if (string.IsNullOrEmpty(identity)) identity = stem;
                            editableMesh = EditableCopy(draw.mesh, EditableAssets + "/mesh_" + Digest(identity) + ".asset");
                            if (!groupSourceMeshes.ContainsKey(spec.group_id)) groupSourceMeshes.Add(spec.group_id, draw.mesh);
                        }
                        meshCopies.Add(draw.mesh, editableMesh);
                    }
                    geometry.sharedMesh = editableMesh;
                    surface.sharedMaterial = EditableCopy(draw.material, EditableAssets + "/" + stem + ".mat");
                    surface.forceRenderingOff = true;
                    var editable = go.AddComponent<EditableCapturedDraw>();
                    editable.geometry = geometry; editable.surface = surface; editable.sharedTransform = group;
                    editable.eventId = spec.event_id; editable.instance = spec.instance; editable.primary = spec.primary;
                    editable.referenceLocalToWorld = go.transform.localToWorldMatrix;
                    editable.positionLocation = spec.position_varying; editable.normalLocation = spec.normal_varying; editable.tangentLocation = spec.tangent_varying;
                    if (spec.geometry_to_shading != null) editable.geometryToShading = Matrix(spec.geometry_to_shading, "Geometry-to-shading mapping");
                    draw.editableObject = editable;
                    draw.usesEditableObject = true;
                }
                var projection = camera.GetComponent<CapturedProjection>();
                if (projection != null) projection.enabled = false;
                camera.transform.SetPositionAndRotation(Vector3.zero, Quaternion.identity);
                camera.transform.localScale = Vector3.one;
                camera.nearClipPlane = manifest.camera.near; camera.farClipPlane = manifest.camera.far;
                camera.aspect = (float)manifest.camera.width / manifest.camera.height;
                camera.fieldOfView = 2f * Mathf.Atan(1f / manifest.camera.p11) * Mathf.Rad2Deg;
                camera.ResetProjectionMatrix();
                runtime.editingCamera = camera;
                runtime.capturedProjection = Matrix(manifest.camera.projection, "Captured projection");
                runtime.capturedCameraProjection = camera.projectionMatrix;
                runtime.editableGeometry = true;
                runtime.Release();
                runtime.Prepare();
                EditorUtility.SetDirty(runtime);
                EditorSceneManager.MarkSceneDirty(scene);
                if (!EditorSceneManager.SaveScene(scene, Destination)) throw new IOException("Could not save the editable capture scene.");
                AssetDatabase.SaveAssets();
                result.status = "complete"; result.editable_draw_count = manifest.draws.Length; result.group_count = groups.Count;
                Debug.Log("Editable original-fragment scene created: " + result.editable_draw_count + " draws in " + result.group_count + " groups.");
            }
            catch (Exception error) { result.status = "failed"; result.error = error.ToString(); throw; }
            finally { File.WriteAllText(Path.Combine(ProjectRoot, "editable-result.json"), JsonUtility.ToJson(result, true)); }
        }
    }

    [CustomEditor(typeof(EditableCapturedDraw))]
    public sealed class EditableCapturedDrawEditor : Editor
    {
        public override void OnInspectorGUI()
        {
            DrawDefaultInspector();
            var draw = (EditableCapturedDraw)target;
            EditorGUILayout.HelpBox("Move the shared transform to update every captured pass in this geometry group. Edit the MeshFilter mesh and MeshRenderer material to change this pass. Fragment inputs without an explicit mapping retain captured values.", MessageType.Info);
            if (draw.sharedTransform != null && GUILayout.Button("Select shared geometry transform")) Selection.activeTransform = draw.sharedTransform;
        }
    }
}
