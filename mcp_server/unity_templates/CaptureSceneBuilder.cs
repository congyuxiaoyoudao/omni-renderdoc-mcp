using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;

namespace OmniRenderDoc
{
    // This builds editable HDRP approximations, not a translation of arbitrary
    // captured SPIR-V. Capture metadata and provenance stay separate.
    public static class CaptureSceneBuilder
    {
        [Serializable] public class CameraSpec
        {
            public float p00, p11;
            public float near = 0.01f, far = 10000;
            public int width, height;
        }
        [Serializable] public class MeshReference { public string file; }
        [Serializable] public class CaptureSpec
        {
            public int version;
            public string source_capture;
            public CameraSpec camera;
            public MeshReference[] meshes;
            public string[] warnings;
            public bool render_preview = true;
            public LightingSpec lighting;
        }
        [Serializable] public class DirectionalLightSpec
        {
            public string name, provenance;
            public float[] direction, color_linear;
            public float lux = 10000;
            public bool intensity_is_approximation = true;
        }
        [Serializable] public class LightingSpec
        {
            public DirectionalLightSpec[] directional_lights;
            public float[] background_color_linear;
            public float exposure_ev100 = 11;
            public string tonemapping = "ACES";
        }
        [Serializable] public class MaterialSpec
        {
            public string base_color_texture, normal_texture, normal_format;
            public float[] base_color;
            public float roughness = 0.7f, metallic = 0, alpha_cutoff = 0.5f;
            public bool base_color_srgb = true, alpha_clip;
        }
        [Serializable] public class MeshSpec
        {
            public string name, category;
            public int event_id, instance;
            public float[] vertices, uv, normals;
            public int[] triangles;
            public MaterialSpec material;
        }
        [Serializable] public class BuildResult
        {
            public string status = "building", render_pipeline = "HDRP";
            public string unity_version, scene, preview, error;
            public int mesh_count, vertex_count, triangle_count;
            public string appearance_mode = "editable_hdrp_approximation";
            public List<string> warnings = new List<string>();
        }

        const string Generated = "Assets/Capture/Generated";
        static readonly Dictionary<string, Texture2D> TextureCache = new Dictionary<string, Texture2D>();
        static string ProjectRoot => Directory.GetParent(Application.dataPath).FullName;
        static BuildResult result;

        [MenuItem("Tools/Omni RenderDoc/Rebuild captured HDRP scene")]
        public static void Build()
        {
            if (!Application.isBatchMode && !EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo()) return;
            result = new BuildResult { unity_version = Application.unityVersion };
            TextureCache.Clear();
            try
            {
                var spec = JsonUtility.FromJson<CaptureSpec>(File.ReadAllText(ProjectFile("Assets/Capture/capture.json")));
                if (spec == null || spec.version != 1 || spec.camera == null || spec.meshes == null)
                    throw new InvalidDataException("Expected capture format version 1 with camera and meshes.");
                ValidateCamera(spec.camera);
                if (spec.warnings != null) result.warnings.AddRange(spec.warnings);
                Directory.CreateDirectory(ProjectFile(Generated));
                AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
                ConfigurePipeline();
                var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
                var root = new GameObject("Captured geometry (posed camera space)");
                foreach (var reference in spec.meshes)
                {
                    var mesh = JsonUtility.FromJson<MeshSpec>(File.ReadAllText(ProjectFile(reference.file)));
                    CreateMesh(mesh, root.transform);
                }
                if (result.mesh_count == 0) throw new InvalidDataException("Capture contains no valid triangle meshes.");
                var camera = CreateCamera(spec.camera, spec.lighting);
                CreateApproximateLighting(spec.lighting);
                result.warnings.Add("Original shader equations, engine lighting, transparency, post effects, hierarchy and animation are not recovered. Materials and studio lights are editable HDRP approximations; capture metadata and provenance are retained separately.");
                result.warnings.Add("Normals are recomputed where absent; anonymous UV/texture bindings may require manual correction.");
                result.scene = "Assets/CaptureScene.unity";
                EditorSceneManager.SaveScene(scene, result.scene);
                EditorBuildSettings.scenes = new[] { new EditorBuildSettingsScene(result.scene, true) };
                AssetDatabase.SaveAssets();
                if (spec.render_preview)
                {
                    RenderPreview(camera, spec.camera);
                    result.preview = "preview.png";
                }
                result.status = "complete";
                WriteResult();
                Debug.Log("OMNI_HDRP_BUILD_COMPLETE " + JsonUtility.ToJson(result));
            }
            catch (Exception error)
            {
                result.status = "failed";
                result.error = error.ToString();
                WriteResult();
                Debug.LogException(error);
                if (Application.isBatchMode) EditorApplication.Exit(1);
                else throw;
            }
        }

        static void ValidateCamera(CameraSpec camera)
        {
            if (camera == null || camera.width <= 0 || camera.height <= 0 ||
                !float.IsFinite(camera.p00) || !float.IsFinite(camera.p11) || camera.p00 <= 0 || camera.p11 <= 0)
                throw new InvalidDataException("Captured camera requires positive dimensions and finite, positive p00/p11 projection values.");
        }

        static string ProjectFile(string relative)
        {
            if (string.IsNullOrWhiteSpace(relative) || Path.IsPathRooted(relative))
                throw new InvalidDataException("Expected a project-relative path: " + relative);
            string root = Path.GetFullPath(ProjectRoot) + Path.DirectorySeparatorChar;
            string path = Path.GetFullPath(Path.Combine(root, relative));
            if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Path escapes project: " + relative);
            return path;
        }

        static void WriteResult() => File.WriteAllText(ProjectFile("result.json"), JsonUtility.ToJson(result, true));

        static void ConfigurePipeline()
        {
            PlayerSettings.colorSpace = ColorSpace.Linear;
            var asset = AssetDatabase.LoadAssetAtPath<HDRenderPipelineAsset>(Generated + "/CapturedHDRP.asset");
            if (asset == null)
            {
                asset = ScriptableObject.CreateInstance<HDRenderPipelineAsset>();
                asset.name = "Captured HDRP";
                AssetDatabase.CreateAsset(asset, Generated + "/CapturedHDRP.asset");
            }
            GraphicsSettings.defaultRenderPipeline = asset;
            QualitySettings.renderPipeline = asset;
            // HDRP 17's global-settings type and Ensure API are internal. Invoke
            // HDRP's own initializer rather than fabricating serialized resources.
            var globalType = typeof(HDRenderPipelineAsset).Assembly.GetType("UnityEngine.Rendering.HighDefinition.HDRenderPipelineGlobalSettings");
            var ensure = globalType?.GetMethod("Ensure", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            if (ensure == null) throw new InvalidOperationException("This HDRP version has no supported global settings initializer.");
            ensure.Invoke(null, new object[] { true });
            EditorUtility.SetDirty(asset);
        }

        static void CreateMesh(MeshSpec spec, Transform parent)
        {
            if (spec == null || spec.vertices == null || spec.vertices.Length % 3 != 0 || spec.vertices.Length < 9 ||
                spec.triangles == null || spec.triangles.Length == 0 || spec.triangles.Length % 3 != 0)
                throw new InvalidDataException("Invalid mesh vertex or index data.");
            int count = spec.vertices.Length / 3;
            var vertices = new Vector3[count];
            for (int i = 0; i < count; i++)
            {
                vertices[i] = new Vector3(spec.vertices[3 * i], spec.vertices[3 * i + 1], spec.vertices[3 * i + 2]);
                if (!float.IsFinite(vertices[i].x) || !float.IsFinite(vertices[i].y) || !float.IsFinite(vertices[i].z))
                    throw new InvalidDataException("Non-finite vertex in " + spec.name);
            }
            foreach (int index in spec.triangles)
                if (index < 0 || index >= count) throw new InvalidDataException("Mesh index out of range in " + spec.name);
            var mesh = new Mesh { name = spec.name, indexFormat = IndexFormat.UInt32 };
            mesh.vertices = vertices;
            mesh.triangles = spec.triangles;
            if (spec.uv != null && spec.uv.Length == count * 2)
            {
                var uv = new Vector2[count];
                for (int i = 0; i < count; i++) uv[i] = new Vector2(spec.uv[2 * i], spec.uv[2 * i + 1]);
                mesh.uv = uv;
            }
            if (spec.normals != null && spec.normals.Length == count * 3)
            {
                var normals = new Vector3[count];
                for (int i = 0; i < count; i++) normals[i] = new Vector3(spec.normals[3 * i], spec.normals[3 * i + 1], spec.normals[3 * i + 2]);
                mesh.normals = normals;
            }
            else mesh.RecalculateNormals();
            if (mesh.uv.Length == count) mesh.RecalculateTangents();
            mesh.RecalculateBounds();
            string assetName = "draw_" + spec.event_id + "_instance_" + spec.instance + "_" + result.mesh_count;
            SaveOrUpdate(mesh, Generated + "/" + assetName + ".asset");
            var obj = new GameObject(string.IsNullOrEmpty(spec.name) ? assetName : spec.name);
            obj.transform.SetParent(parent, false);
            obj.AddComponent<MeshFilter>().sharedMesh = AssetDatabase.LoadAssetAtPath<Mesh>(Generated + "/" + assetName + ".asset");
            var renderer = obj.AddComponent<MeshRenderer>();
            renderer.sharedMaterial = CreateMaterial(spec.material ?? new MaterialSpec(), assetName);
            result.mesh_count++;
            result.vertex_count += count;
            result.triangle_count += spec.triangles.Length / 3;
        }

        static void SaveOrUpdate(UnityEngine.Object asset, string path)
        {
            var existing = AssetDatabase.LoadMainAssetAtPath(path);
            if (existing == null) AssetDatabase.CreateAsset(asset, path);
            else
            {
                EditorUtility.CopySerialized(asset, existing);
                EditorUtility.SetDirty(existing);
                UnityEngine.Object.DestroyImmediate(asset);
            }
        }

        static Texture2D LoadTexture(string path, bool normal, bool srgb)
        {
            if (string.IsNullOrEmpty(path)) return null;
            ProjectFile(path);
            string key = path + (normal ? "|normal" : "|color") + srgb;
            if (TextureCache.TryGetValue(key, out var cached)) return cached;
            var importer = AssetImporter.GetAtPath(path) as TextureImporter;
            if (importer == null)
            {
                result.warnings.Add("Texture missing or unsupported: " + path);
                return null;
            }
            importer.textureType = normal ? TextureImporterType.NormalMap : TextureImporterType.Default;
            importer.sRGBTexture = !normal && srgb;
            importer.textureCompression = TextureImporterCompression.Uncompressed;
            importer.maxTextureSize = 8192;
            importer.alphaSource = TextureImporterAlphaSource.FromInput;
            importer.SaveAndReimport();
            var texture = AssetDatabase.LoadAssetAtPath<Texture2D>(path);
            TextureCache[key] = texture;
            return texture;
        }

        static string PrepareNormal(string path, string format)
        {
            if (string.IsNullOrEmpty(path)) return null;
            if (string.IsNullOrEmpty(format) || format.IndexOf("BC5", StringComparison.OrdinalIgnoreCase) < 0)
                return path;
            if (format.IndexOf("SNORM", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                result.warnings.Add("Skipped signed BC5 normal texture: its PNG channel remapping has not been verified: " + path);
                return null;
            }
            // A BC5 export contains encoded X/Y, not a usable RGB normal. Build
            // a derived RGB file without changing the captured PNG evidence.
            string hash;
            using (var md5 = System.Security.Cryptography.MD5.Create())
                hash = BitConverter.ToString(md5.ComputeHash(System.Text.Encoding.UTF8.GetBytes(path))).Replace("-", "").Substring(0, 12);
            string derived = Generated + "/normal_bc5_" + hash + ".png";
            if (File.Exists(ProjectFile(derived))) return derived;
            var image = new Texture2D(2, 2, TextureFormat.RGBA32, false, true);
            try
            {
                if (!image.LoadImage(File.ReadAllBytes(ProjectFile(path))))
                    throw new InvalidDataException("Cannot decode BC5 normal image: " + path);
                var pixels = image.GetPixels32();
                for (int i = 0; i < pixels.Length; i++)
                {
                    float x = pixels[i].r / 127.5f - 1, y = pixels[i].g / 127.5f - 1;
                    float z = Mathf.Sqrt(Mathf.Max(0, 1 - x * x - y * y));
                    pixels[i].b = (byte)Mathf.RoundToInt((z + 1) * 127.5f);
                    pixels[i].a = 255;
                }
                image.SetPixels32(pixels);
                image.Apply();
                File.WriteAllBytes(ProjectFile(derived), image.EncodeToPNG());
                AssetDatabase.ImportAsset(derived, ImportAssetOptions.ForceSynchronousImport);
                result.warnings.Add("Reconstructed BC5 normal Z in derived RGB texture; source tangent basis/green convention remains approximate: " + path);
                return derived;
            }
            finally { UnityEngine.Object.DestroyImmediate(image); }
        }

        static Material CreateMaterial(MaterialSpec spec, string name)
        {
            var shader = Shader.Find("HDRP/Lit");
            if (shader == null) throw new InvalidOperationException("HDRP/Lit shader is unavailable.");
            if (ShaderUtil.ShaderHasError(shader)) throw new InvalidOperationException("HDRP/Lit has shader compilation errors.");
            var material = new Material(shader) { name = name + " (HDRP approximation)" };
            var tint = ReadLinearColor(spec.base_color, Color.white, "base_color", false);
            // SetVector preserves the captured linear float4 without SetColor's
            // property-dependent gamma conversion rules.
            material.SetVector("_BaseColor", new Vector4(tint.r, tint.g, tint.b, tint.a));
            material.SetFloat("_Smoothness", 1 - Mathf.Clamp01(spec.roughness));
            material.SetFloat("_Metallic", Mathf.Clamp01(spec.metallic));
            // Post-VS geometry may include intentionally reversed shell surfaces.
            material.SetFloat("_DoubleSidedEnable", 1);
            material.SetFloat("_DoubleSidedNormalMode", 0);
            material.SetFloat("_CullMode", (float)CullMode.Off);
            material.SetFloat("_CullModeForward", (float)CullMode.Off);
            var color = LoadTexture(spec.base_color_texture, false, spec.base_color_srgb);
            if (color != null) material.SetTexture("_BaseColorMap", color);
            var normal = LoadTexture(PrepareNormal(spec.normal_texture, spec.normal_format), true, false);
            if (normal != null) material.SetTexture("_NormalMap", normal);
            if (spec.alpha_clip)
            {
                material.SetFloat("_AlphaCutoffEnable", 1);
                material.SetFloat("_AlphaCutoff", Mathf.Clamp01(spec.alpha_cutoff));
            }
            HDMaterial.ValidateMaterial(material);
            string path = Generated + "/" + name + ".mat";
            SaveOrUpdate(material, path);
            return AssetDatabase.LoadAssetAtPath<Material>(path);
        }

        static Color ReadLinearColor(float[] values, Color fallback, string name, bool allowRgb)
        {
            if (values == null) return fallback;
            if (values.Length != 4 && !(allowRgb && values.Length == 3))
                throw new InvalidDataException(name + " requires " + (allowRgb ? "RGB or RGBA" : "RGBA") + ".");
            for (int i = 0; i < values.Length; i++)
                if (!float.IsFinite(values[i]) || values[i] < 0)
                    throw new InvalidDataException(name + " contains invalid color values.");
            if (values.Length == 4 && values[3] > 1)
                throw new InvalidDataException(name + " alpha must be in [0, 1].");
            return new Color(values[0], values[1], values[2], values.Length == 4 ? values[3] : 1);
        }

        static Camera CreateCamera(CameraSpec spec, LightingSpec lighting)
        {
            if (spec.p00 <= 0 || spec.p11 <= 0 || spec.width <= 0 || spec.height <= 0)
                throw new InvalidDataException("Invalid perspective camera specification.");
            var obj = new GameObject("Captured camera");
            obj.tag = "MainCamera";
            var camera = obj.AddComponent<Camera>();
            camera.nearClipPlane = Mathf.Max(0.0001f, spec.near);
            camera.farClipPlane = Mathf.Max(camera.nearClipPlane + 1, spec.far);
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = ReadLinearColor(lighting?.background_color_linear,
                new Color(0.08f, 0.09f, 0.11f, 1), "background_color_linear", true);
            camera.allowHDR = true;
            var data = obj.AddComponent<HDAdditionalCameraData>();
            data.clearColorMode = HDAdditionalCameraData.ClearColorMode.Color;
            data.backgroundColorHDR = camera.backgroundColor;
            if (lighting?.background_color_linear != null)
                result.warnings.Add("Background is a caller-supplied flat clear color approximation, not a recovered sky or fog shader.");
            data.antialiasing = HDAdditionalCameraData.AntialiasingMode.None;
            var projection = obj.AddComponent<CapturedProjection>();
            projection.p00 = spec.p00;
            projection.p11 = spec.p11;
            projection.width = spec.width;
            projection.height = spec.height;
            projection.Apply();
            return camera;
        }

        static void CreateApproximateLighting(LightingSpec spec)
        {
            var lighting = new GameObject("Approximate studio lighting (not recovered)");
            if (spec?.directional_lights == null)
            {
                AddLight(lighting.transform, "Approximate key", Quaternion.Euler(35, -30, 0) * Vector3.forward, 10000, Color.white);
                AddLight(lighting.transform, "Approximate fill", Quaternion.Euler(-20, 145, 0) * Vector3.forward, 2500, new Color(0.78f, 0.86f, 1f));
            }
            else
            {
                lighting.name = "Configured lighting (see capture provenance)";
                foreach (var light in spec.directional_lights)
                {
                    if (light == null || light.direction == null || light.direction.Length != 3 ||
                        light.color_linear == null || light.color_linear.Length != 3)
                        throw new InvalidDataException("Directional lights require three-component direction and color_linear arrays.");
                    var direction = new Vector3(light.direction[0], light.direction[1], light.direction[2]);
                    var linearColor = new Color(light.color_linear[0], light.color_linear[1], light.color_linear[2], 1);
                    if (!float.IsFinite(direction.x) || !float.IsFinite(direction.y) || !float.IsFinite(direction.z) || direction.sqrMagnitude < 1e-12f ||
                        !float.IsFinite(linearColor.r) || !float.IsFinite(linearColor.g) || !float.IsFinite(linearColor.b) ||
                        linearColor.r < 0 || linearColor.g < 0 || linearColor.b < 0 || !float.IsFinite(light.lux) || light.lux < 0)
                        throw new InvalidDataException("Invalid directional light values.");
                    string name = string.IsNullOrEmpty(light.name) ? "Configured directional light" : light.name;
                    // Light.color is the Inspector/display color. Supply gamma
                    // encoded RGB so Unity's linear lighting conversion preserves
                    // the captured linear radiance ratios.
                    AddLight(lighting.transform, name, direction, light.lux, linearColor.gamma);
                    result.warnings.Add(name + ": " + (light.intensity_is_approximation ? "HDRP lux is approximate. " : "") + (light.provenance ?? "Direction/color supplied by caller."));
                }
            }
            var volume = new GameObject("Approximate fixed exposure").AddComponent<Volume>();
            volume.isGlobal = true;
            volume.priority = 100;
            var profile = ScriptableObject.CreateInstance<VolumeProfile>();
            var exposure = profile.Add<Exposure>(true);
            exposure.mode.Override(ExposureMode.Fixed);
            float exposureValue = spec == null ? 11 : spec.exposure_ev100;
            if (!float.IsFinite(exposureValue)) throw new InvalidDataException("Exposure EV100 must be finite.");
            exposure.fixedExposure.Override(exposureValue);
            var tone = profile.Add<Tonemapping>(true);
            string tonemapping = spec == null || string.IsNullOrEmpty(spec.tonemapping) ? "ACES" : spec.tonemapping;
            if (tonemapping != "None" && tonemapping != "ACES" && tonemapping != "Neutral")
                throw new InvalidDataException("Tonemapping must be None, ACES or Neutral.");
            tone.mode.Override((TonemappingMode)Enum.Parse(typeof(TonemappingMode), tonemapping));
            SaveOrUpdate(profile, Generated + "/ApproximateExposure.asset");
            volume.sharedProfile = AssetDatabase.LoadAssetAtPath<VolumeProfile>(Generated + "/ApproximateExposure.asset");
            // Volume components must be subassets to survive closing the editor.
            foreach (var component in volume.sharedProfile.components)
                if (!AssetDatabase.Contains(component)) AssetDatabase.AddObjectToAsset(component, volume.sharedProfile);
            EditorUtility.SetDirty(volume.sharedProfile);
        }

        static void AddLight(Transform parent, string name, Vector3 direction, float lux, Color color)
        {
            var obj = new GameObject(name);
            obj.transform.SetParent(parent, false);
            direction.Normalize();
            var up = Mathf.Abs(Vector3.Dot(direction, Vector3.up)) > 0.999f ? Vector3.right : Vector3.up;
            obj.transform.rotation = Quaternion.LookRotation(direction, up);
            var light = obj.AddComponent<Light>();
            light.type = LightType.Directional;
            light.color = color;
            light.shadows = LightShadows.None;
            obj.AddComponent<HDAdditionalLightData>();
            light.lightUnit = LightUnit.Lux;
            light.intensity = lux;
        }

        static void RenderPreview(Camera camera, CameraSpec spec)
        {
            if (SystemInfo.graphicsDeviceType == GraphicsDeviceType.Null)
                throw new InvalidOperationException("Preview requires a graphics device; do not use -nographics.");
            int width = spec.width;
            int height = spec.height;
            var target = new RenderTexture(width, height, 24, RenderTextureFormat.ARGB32, RenderTextureReadWrite.sRGB);
            target.Create();
            var previous = RenderTexture.active;
            var image = new Texture2D(width, height, TextureFormat.RGB24, false, false);
            try
            {
                var request = new RenderPipeline.StandardRequest { destination = target };
                // An initial request creates HDRP and its camera history. A second
                // request also allows sky/exposure resources to initialize.
                RenderPipeline.SubmitRenderRequest(camera, request);
                RenderPipeline.SubmitRenderRequest(camera, request);
                if (!(RenderPipelineManager.currentPipeline is HDRenderPipeline))
                    throw new InvalidOperationException("HDRP failed to initialize for preview.");
                RenderTexture.active = target;
                image.ReadPixels(new Rect(0, 0, width, height), 0, 0);
                image.Apply();
                File.WriteAllBytes(ProjectFile("preview.png"), image.EncodeToPNG());
            }
            finally
            {
                RenderTexture.active = previous;
                target.Release();
                UnityEngine.Object.DestroyImmediate(target);
                UnityEngine.Object.DestroyImmediate(image);
            }
        }
    }
}
