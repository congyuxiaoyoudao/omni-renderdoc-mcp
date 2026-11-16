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
    // Capture data is consumed only while importing. The saved scene uses
    // ordinary renderers, meshes and HDRP materials, without a replay component.
    public static class NativeCaptureSceneBuilder
    {
        [Serializable] public class CameraSpec
        {
            public float p00, p11, near = 0.01f, far = 10000;
            public int width, height;
        }
        [Serializable] public class MeshReference
        {
            public string file, material_key;
            // Unity draws additional materials over the final submesh. This
            // lets verified layered passes share one truly editable mesh.
            public string[] additional_material_keys;
        }
        [Serializable] public class SurfaceDepthSpec
        {
            public string[] mesh_files;
            public string texture_property = "_NativeSurfaceDepthTexture";
        }
        [Serializable] public class Manifest
        {
            public int version;
            public CameraSpec camera;
            public MeshReference[] meshes;
            public string material_library, scene_path, generated_assets;
            public string sky_cubemap;
            public string post_process_material;
            public string source_bloom_profile;
            public string native_temporal_profile;
            public string source_sun_profile;
            public string opaque_color_texture;
            public string actor_shadow_profile;
            public string surface_modifier_profile;
            public SurfaceDepthSpec surface_depth;
            public float sky_rotation, sky_exposure;
            public string[] warnings;
            public CaptureSceneBuilder.LightingSpec lighting;
            public bool render_preview = true;
        }
        [Serializable] public class UVChannel
        {
            public int channel, dimension;
            public float[] values;
        }
        [Serializable] public class MeshSpec
        {
            public string name;
            public int event_id, instance;
            public float[] vertices, normals, tangents, colors, pivot;
            public int[] triangles;
            public UVChannel[] uv_channels;
        }
        [Serializable] public class Result
        {
            public string status = "building", scene, error;
            public string appearance_mode = "editable_meshes_hdrp";
            public int mesh_count, vertex_count, triangle_count, auxiliary_mesh_count;
            public List<string> warnings = new List<string>();
        }
        static string ProjectRoot => Directory.GetParent(Application.dataPath).FullName;
        static string generated;
        static Result result;

        public static string ProjectFile(string relative)
        {
            if (string.IsNullOrWhiteSpace(relative) || Path.IsPathRooted(relative))
                throw new InvalidDataException("Expected a project-relative path: " + relative);
            string root = Path.GetFullPath(ProjectRoot) + Path.DirectorySeparatorChar;
            string path = Path.GetFullPath(Path.Combine(root, relative));
            if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Path escapes project: " + relative);
            return path;
        }
        static string AssetPath(string value, string fallback)
        {
            value = string.IsNullOrEmpty(value) ? fallback : value.Replace('\\', '/');
            ProjectFile(value);
            if (!value.StartsWith("Assets/", StringComparison.Ordinal))
                throw new InvalidDataException("Generated assets must be below Assets/: " + value);
            return value;
        }

        [MenuItem("Tools/Omni RenderDoc/Build native editable HDRP scene")]
        public static void Build()
        {
            if (!Application.isBatchMode && !EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo()) return;
            var built = BuildScene();
            if (built.status != "complete")
            {
                if (Application.isBatchMode) EditorApplication.Exit(1);
                else throw new InvalidOperationException(built.error);
            }
        }

        public static Result BuildScene()
        {
            result = new Result();
            var graphicsErrors = new List<string>();
            Application.LogCallback recordError = (message, stack, type) =>
            {
                if (type == LogType.Error || type == LogType.Assert || type == LogType.Exception)
                    graphicsErrors.Add(message);
            };
            Application.logMessageReceived += recordError;
            try
            {
                var spec = JsonUtility.FromJson<Manifest>(File.ReadAllText(ProjectFile("Assets/NativeCapture/manifest.json")));
                if (spec == null || spec.version != 1 || spec.meshes == null || spec.meshes.Length == 0 || spec.camera == null)
                    throw new InvalidDataException("Expected a nonempty native editable mesh manifest.");
                ValidateCamera(spec.camera);
                result.scene = AssetPath(spec.scene_path, "Assets/CaptureScene.unity");
                generated = AssetPath(spec.generated_assets, "Assets/NativeCapture/Generated");
                if (!result.scene.EndsWith(".unity", StringComparison.Ordinal)) throw new InvalidDataException("Expected a Unity scene path.");
                Directory.CreateDirectory(ProjectFile(generated));
                AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
                ConfigurePipeline();
                var materials = new Dictionary<string, NativeMaterialFactory.MaterialSpec>();
                if (!string.IsNullOrEmpty(spec.material_library))
                    foreach (var entry in NativeMaterialFactory.LoadLibrary(ProjectFile(spec.material_library)).entries ?? Array.Empty<NativeMaterialFactory.MaterialEntry>())
                    {
                        if (string.IsNullOrEmpty(entry.key) || materials.ContainsKey(entry.key)) throw new InvalidDataException("Duplicate or missing native material key.");
                        materials.Add(entry.key, entry.spec);
                    }
                var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
                var root = new GameObject("True editable capture");
                var surfaceRenderers = new List<MeshRenderer>();
                var surfaceFiles = new HashSet<string>(spec.surface_depth?.mesh_files ?? Array.Empty<string>(), StringComparer.Ordinal);
                foreach (var reference in spec.meshes)
                {
                    var data = JsonUtility.FromJson<MeshSpec>(File.ReadAllText(ProjectFile(reference.file)));
                    materials.TryGetValue(reference.material_key ?? "", out var material);
                    var renderer = CreateMesh(data, material ?? new NativeMaterialFactory.MaterialSpec(), reference.material_key, root.transform);
                    if (reference.additional_material_keys != null && reference.additional_material_keys.Length > 0)
                    {
                        var layered = new List<Material> { renderer.sharedMaterial };
                        foreach (var key in reference.additional_material_keys)
                        {
                            if (!materials.TryGetValue(key, out var extra))
                                throw new InvalidDataException("Missing layered native material: " + key);
                            layered.Add(NativeMaterialFactory.Create(extra, generated, key));
                        }
                        renderer.sharedMaterials = layered.ToArray();
                    }
                    if (surfaceFiles.Remove(reference.file))
                        surfaceRenderers.Add(renderer);
                }
                if (surfaceFiles.Count > 0) throw new InvalidDataException("Surface-depth mesh references are missing from the scene.");
                if (surfaceRenderers.Count > 0) CreateSurfaceDepth(spec.surface_depth, surfaceRenderers);
                if (!string.IsNullOrEmpty(spec.opaque_color_texture))
                {
                    var volume = new GameObject("Current opaque scene color").AddComponent<CustomPassVolume>();
                    volume.isGlobal = true;
                    volume.injectionPoint = CustomPassInjectionPoint.BeforePreRefraction;
                    volume.customPasses.Add(new NativeCameraColorPass
                    {
                        name = "Current opaque color for editable materials",
                        textureProperty = spec.opaque_color_texture,
                        targetColorBuffer = CustomPass.TargetBuffer.None,
                        targetDepthBuffer = CustomPass.TargetBuffer.None,
                    });
                }
                var camera = CreateCamera(spec.camera, spec.lighting);
                CreateLighting(spec, camera, materials);
                if (!string.IsNullOrEmpty(spec.actor_shadow_profile))
                    NativeActorShadowBuilder.Attach(ProjectFile(AssetPath(spec.actor_shadow_profile, null)), false);
                if (!string.IsNullOrEmpty(spec.surface_modifier_profile))
                    NativeSurfaceModifierBuilder.Attach(ProjectFile(AssetPath(spec.surface_modifier_profile, null)), false);
                if (!string.IsNullOrEmpty(spec.native_temporal_profile))
                    NativeTemporalBuilder.Attach(ProjectFile(AssetPath(spec.native_temporal_profile, null)), false);
                if (!string.IsNullOrEmpty(spec.source_sun_profile))
                    NativeSunShadowBuilder.Attach(ProjectFile(AssetPath(spec.source_sun_profile, null)), false);
                if (spec.warnings != null) result.warnings.AddRange(spec.warnings);
                result.warnings.Add("Meshes are independent baked capture poses. Original hierarchy, hidden draws, rig and animation are not implied. HDRP evaluates current geometry, lighting, depth and shadows; original frame history is not used.");
                if (!EditorSceneManager.SaveScene(scene, result.scene)) throw new IOException("Could not save native editable scene.");
                EditorBuildSettings.scenes = new[] { new EditorBuildSettingsScene(result.scene, true) };
                AssetDatabase.SaveAssets();
                if (spec.render_preview)
                {
                    float scale = Mathf.Min(1f, Mathf.Min(1280f / spec.camera.width, 720f / spec.camera.height));
                    RenderPreview(camera, Mathf.Max(1, Mathf.RoundToInt(spec.camera.width * scale)), Mathf.Max(1, Mathf.RoundToInt(spec.camera.height * scale)));
                }
                if (graphicsErrors.Count > 0)
                    throw new InvalidOperationException("Native scene reported errors: " + string.Join("; ", graphicsErrors));
                result.status = "complete";
                Debug.Log("Native editable capture complete: " + result.mesh_count + " ordinary MeshRenderers.");
            }
            catch (Exception error)
            {
                result.status = "failed"; result.error = error.ToString(); Debug.LogException(error);
            }
            finally
            {
                Application.logMessageReceived -= recordError;
                File.WriteAllText(ProjectFile("native-result.json"), JsonUtility.ToJson(result, true));
            }
            return result;
        }

        static void ConfigurePipeline()
        {
            PlayerSettings.colorSpace = ColorSpace.Linear;
            var pipeline = GraphicsSettings.defaultRenderPipeline as HDRenderPipelineAsset;
            if (pipeline == null)
            {
                pipeline = ScriptableObject.CreateInstance<HDRenderPipelineAsset>();
                AssetDatabase.CreateAsset(pipeline, generated + "/NativeHDRP.asset");
                GraphicsSettings.defaultRenderPipeline = pipeline;
            }
            QualitySettings.renderPipeline = pipeline;
            var type = typeof(HDRenderPipelineAsset).Assembly.GetType("UnityEngine.Rendering.HighDefinition.HDRenderPipelineGlobalSettings");
            var ensure = type?.GetMethod("Ensure", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            if (ensure == null) throw new InvalidOperationException("HDRP global settings initializer is unavailable.");
            ensure.Invoke(null, new object[] { true });
        }

        static void CreateSurfaceDepth(SurfaceDepthSpec spec, List<MeshRenderer> surfaces)
        {
            var shader = Shader.Find("Hidden/OmniRenderDoc/NativeSurfaceDepth");
            if (shader == null || ShaderUtil.ShaderHasError(shader))
                throw new InvalidDataException("Selected-surface depth shader is unavailable.");
            var material = new Material(shader);
            SaveAsset(material, generated + "/SurfaceDepth.mat");
            var volume = new GameObject("Editable surface depth").AddComponent<CustomPassVolume>();
            volume.isGlobal = true;
            volume.injectionPoint = CustomPassInjectionPoint.AfterOpaqueDepthAndNormal;
            var pass = new NativeSurfaceDepthPass
            {
                name = "Current selected surface depth", surfaces = surfaces.ToArray(),
                depthMaterial = AssetDatabase.LoadAssetAtPath<Material>(generated + "/SurfaceDepth.mat"),
                textureProperty = spec.texture_property,
                targetColorBuffer = CustomPass.TargetBuffer.None,
                targetDepthBuffer = CustomPass.TargetBuffer.None,
            };
            volume.customPasses.Add(pass);
        }

        static void Check(float[] values, int expected, string label)
        {
            if (values == null || values.Length != expected) throw new InvalidDataException(label + " has the wrong length.");
            foreach (float value in values) if (!float.IsFinite(value)) throw new InvalidDataException(label + " contains nonfinite values.");
        }
        static Vector3[] Vectors3(float[] data)
        {
            var values = new Vector3[data.Length / 3];
            for (int i = 0; i < values.Length; i++) values[i] = new Vector3(data[i * 3], data[i * 3 + 1], data[i * 3 + 2]);
            return values;
        }
        static MeshRenderer CreateMesh(MeshSpec spec, NativeMaterialFactory.MaterialSpec material, string key, Transform root)
        {
            if (spec == null || spec.vertices == null || spec.vertices.Length < 9 || spec.vertices.Length % 3 != 0 ||
                spec.triangles == null || spec.triangles.Length == 0 || spec.triangles.Length % 3 != 0)
                throw new InvalidDataException("Invalid native mesh topology.");
            int count = spec.vertices.Length / 3;
            Check(spec.vertices, count * 3, "vertices");
            foreach (int index in spec.triangles) if (index < 0 || index >= count) throw new InvalidDataException("Mesh index is out of range.");
            string stem = "mesh_" + result.mesh_count.ToString("D6");
            var mesh = new Mesh { name = spec.name, indexFormat = count > 65535 ? IndexFormat.UInt32 : IndexFormat.UInt16 };
            mesh.vertices = Vectors3(spec.vertices); mesh.triangles = spec.triangles;
            if (spec.normals != null && spec.normals.Length > 0)
            { Check(spec.normals, count * 3, "normals"); mesh.normals = Vectors3(spec.normals); }
            else mesh.RecalculateNormals();
            var usedChannels = new HashSet<int>();
            foreach (var channel in spec.uv_channels ?? Array.Empty<UVChannel>())
            {
                if (channel.channel < 0 || channel.channel > 7 || !usedChannels.Add(channel.channel) || channel.dimension < 2 || channel.dimension > 4)
                    throw new InvalidDataException("Invalid or duplicate native UV channel.");
                Check(channel.values, count * channel.dimension, "UV channel");
                if (channel.dimension == 2)
                {
                    var values = new List<Vector2>(count);
                    for (int i = 0; i < count; i++) values.Add(new Vector2(channel.values[i * 2], channel.values[i * 2 + 1]));
                    mesh.SetUVs(channel.channel, values);
                }
                else if (channel.dimension == 3) mesh.SetUVs(channel.channel, new List<Vector3>(Vectors3(channel.values)));
                else
                {
                    var values = new List<Vector4>(count);
                    for (int i = 0; i < count; i++) values.Add(new Vector4(channel.values[i * 4], channel.values[i * 4 + 1], channel.values[i * 4 + 2], channel.values[i * 4 + 3]));
                    mesh.SetUVs(channel.channel, values);
                }
            }
            if (spec.tangents != null && spec.tangents.Length > 0)
            {
                Check(spec.tangents, count * 4, "tangents");
                var values = new Vector4[count];
                for (int i = 0; i < count; i++) values[i] = new Vector4(spec.tangents[i * 4], spec.tangents[i * 4 + 1], spec.tangents[i * 4 + 2], spec.tangents[i * 4 + 3]);
                mesh.tangents = values;
            }
            else if (usedChannels.Contains(0)) mesh.RecalculateTangents();
            if (spec.colors != null && spec.colors.Length > 0)
            {
                Check(spec.colors, count * 4, "colors");
                var values = new Color[count];
                for (int i = 0; i < count; i++) values[i] = new Color(spec.colors[i * 4], spec.colors[i * 4 + 1], spec.colors[i * 4 + 2], spec.colors[i * 4 + 3]);
                mesh.colors = values;
            }
            mesh.RecalculateBounds();
            SaveAsset(mesh, generated + "/" + stem + ".asset");
            var obj = new GameObject(string.IsNullOrEmpty(spec.name) ? stem : spec.name);
            var parent = root;
            if (!material.renderer_enabled)
            {
                if ((material.renderer_category != "rendering_proxy" && material.renderer_category != "surface_modifier") || string.IsNullOrWhiteSpace(material.renderer_reason))
                    throw new InvalidDataException("Disabled native meshes require an explicit rendering-proxy classification and reason.");
                parent = root.Find("Rendering proxies (disabled)");
                if (parent == null)
                {
                    parent = new GameObject("Rendering proxies (disabled)").transform;
                    parent.SetParent(root, false);
                }
                result.auxiliary_mesh_count++;
                result.warnings.Add(spec.name + ": retained auxiliary geometry, renderer disabled: " + material.renderer_reason);
            }
            obj.transform.SetParent(parent, false);
            if (spec.pivot != null && spec.pivot.Length > 0)
            { Check(spec.pivot, 3, "pivot"); obj.transform.localPosition = new Vector3(spec.pivot[0], spec.pivot[1], spec.pivot[2]); }
            obj.AddComponent<MeshFilter>().sharedMesh = AssetDatabase.LoadAssetAtPath<Mesh>(generated + "/" + stem + ".asset");
            var renderer = obj.AddComponent<MeshRenderer>();
            renderer.sharedMaterial = NativeMaterialFactory.Create(material, generated, string.IsNullOrEmpty(key) ? stem : key);
            renderer.enabled = material.renderer_enabled;
            renderer.shadowCastingMode = material.surface == "transparent" || !material.renderer_enabled || !material.cast_shadows
                ? ShadowCastingMode.Off : ShadowCastingMode.On;
            renderer.receiveShadows = material.receive_shadows;
            result.mesh_count++; result.vertex_count += count; result.triangle_count += spec.triangles.Length / 3;
            return renderer;
        }

        static void SaveAsset(UnityEngine.Object asset, string path)
        {
            var existing = AssetDatabase.LoadMainAssetAtPath(path);
            if (existing == null) AssetDatabase.CreateAsset(asset, path);
            else { EditorUtility.CopySerialized(asset, existing); EditorUtility.SetDirty(existing); UnityEngine.Object.DestroyImmediate(asset); }
        }
        static Color ColorFrom(float[] values, Color fallback)
        {
            if (values == null) return fallback;
            if (values.Length != 3 && values.Length != 4) throw new InvalidDataException("Expected RGB/RGBA color.");
            Check(values, values.Length, "color");
            return new Color(values[0], values[1], values[2], values.Length == 4 ? values[3] : 1);
        }
        static void ValidateCamera(CameraSpec spec)
        {
            if (spec.width <= 0 || spec.height <= 0 || !float.IsFinite(spec.p00) || spec.p00 <= 0 ||
                !float.IsFinite(spec.p11) || spec.p11 <= 0 || !float.IsFinite(spec.near) || !float.IsFinite(spec.far) ||
                spec.near <= 0 || spec.far <= spec.near)
                throw new InvalidDataException("Invalid native scene camera.");
        }
        static Camera CreateCamera(CameraSpec spec, CaptureSceneBuilder.LightingSpec lights)
        {
            ValidateCamera(spec);
            var obj = new GameObject("Capture camera"); obj.tag = "MainCamera";
            var camera = obj.AddComponent<Camera>();
            camera.fieldOfView = 2 * Mathf.Atan(1 / spec.p11) * Mathf.Rad2Deg;
            camera.nearClipPlane = spec.near; camera.farClipPlane = spec.far;
            camera.allowHDR = true; camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = ColorFrom(lights?.background_color_linear, new Color(0.12f, 0.16f, 0.19f));
            var hd = obj.AddComponent<HDAdditionalCameraData>();
            hd.clearColorMode = HDAdditionalCameraData.ClearColorMode.Color; hd.backgroundColorHDR = camera.backgroundColor;
            hd.antialiasing = HDAdditionalCameraData.AntialiasingMode.None;
            // Standard serialized camera settings remain editable. No LateUpdate
            // component restores the captured projection or moves this camera.
            return camera;
        }
        static void CreateLighting(Manifest manifest, Camera camera, Dictionary<string, NativeMaterialFactory.MaterialSpec> materials)
        {
            var spec = manifest.lighting;
            var root = new GameObject("Editable lighting");
            var lights = spec?.directional_lights ?? new[]
            {
                new CaptureSceneBuilder.DirectionalLightSpec { name = "Key", direction = new[] { 0.4f, -0.7f, 0.6f }, color_linear = new[] { 1f, 1f, 1f }, lux = 10000 },
                new CaptureSceneBuilder.DirectionalLightSpec { name = "Fill", direction = new[] { -0.5f, -0.3f, -0.7f }, color_linear = new[] { 0.78f, 0.86f, 1f }, lux = 2500 },
            };
            // HDRP supports one directional shadow map. Use the strongest
            // directional light as the key; fills remain ordinary editable lights.
            int shadowLight = -1;
            for (int i = 0; i < lights.Length; i++)
                if (lights[i].lux > 0 && (shadowLight < 0 || lights[i].lux > lights[shadowLight].lux)) shadowLight = i;
            for (int i = 0; i < lights.Length; i++)
            {
                var data = lights[i];
                Check(data.direction, 3, "light direction");
                var direction = new Vector3(data.direction[0], data.direction[1], data.direction[2]);
                if (direction.sqrMagnitude < 1e-10f || !float.IsFinite(data.lux) || data.lux < 0) throw new InvalidDataException("Invalid light.");
                var obj = new GameObject(data.name ?? "Directional light"); obj.transform.SetParent(root.transform, false);
                direction.Normalize(); obj.transform.rotation = Quaternion.LookRotation(direction, Mathf.Abs(direction.y) > 0.999f ? Vector3.right : Vector3.up);
                var light = obj.AddComponent<Light>(); light.type = LightType.Directional;
                light.color = ColorFrom(data.color_linear, Color.white).gamma;
                light.shadows = i == shadowLight ? LightShadows.Soft : LightShadows.None;
                obj.AddComponent<HDAdditionalLightData>(); light.lightUnit = LightUnit.Lux; light.intensity = data.lux;
            }
            var volume = new GameObject("Editable exposure").AddComponent<Volume>(); volume.isGlobal = true;
            var profile = ScriptableObject.CreateInstance<VolumeProfile>();
            var exposure = profile.Add<Exposure>(true); exposure.mode.Override(ExposureMode.Fixed); exposure.fixedExposure.Override(spec?.exposure_ev100 ?? 11);
            var tone = profile.Add<Tonemapping>(true);
            tone.mode.Override(Enum.TryParse<TonemappingMode>(spec?.tonemapping ?? "ACES", out var mode) ? mode : TonemappingMode.ACES);
            if (!string.IsNullOrEmpty(manifest.sky_cubemap))
            {
                var cubemap = AssetDatabase.LoadAssetAtPath<Cubemap>(AssetPath(manifest.sky_cubemap, null));
                if (cubemap == null) throw new InvalidDataException("Configured HDRI cubemap is missing.");
                var environment = profile.Add<VisualEnvironment>(true);
                environment.skyType.Override((int)SkyType.HDRI);
                environment.skyAmbientMode.Override(SkyAmbientMode.Dynamic);
                var sky = profile.Add<HDRISky>(true);
                sky.hdriSky.Override(cubemap); sky.rotation.Override(manifest.sky_rotation); sky.exposure.Override(manifest.sky_exposure);
                camera.GetComponent<HDAdditionalCameraData>().clearColorMode = HDAdditionalCameraData.ClearColorMode.Sky;
            }
            else profile.Add<VisualEnvironment>(true).skyType.Override(0);
            if (!string.IsNullOrEmpty(manifest.post_process_material))
            {
                if (!materials.TryGetValue(manifest.post_process_material, out var postSpec))
                    throw new InvalidDataException("Missing native post-process material: " + manifest.post_process_material);
                RegisterPostProcess();
                var post = profile.Add<NativeMaterialPostProcess>(true);
                post.material.Override(NativeMaterialFactory.Create(postSpec, generated, manifest.post_process_material));
                post.enabledEffect.Override(true);
                if (!string.IsNullOrEmpty(manifest.source_bloom_profile))
                    NativeSourceBloomBuilder.Configure(post, ProjectFile(AssetPath(manifest.source_bloom_profile, null)), false);
            }
            else if (!string.IsNullOrEmpty(manifest.source_bloom_profile))
                throw new InvalidDataException("A source Bloom profile requires a native post-process material.");
            SaveAsset(profile, generated + "/Lighting.asset");
            volume.sharedProfile = AssetDatabase.LoadAssetAtPath<VolumeProfile>(generated + "/Lighting.asset");
            foreach (var component in volume.sharedProfile.components)
                if (!AssetDatabase.Contains(component)) AssetDatabase.AddObjectToAsset(component, volume.sharedProfile);
            EditorUtility.SetDirty(volume.sharedProfile);
        }
        static void RegisterPostProcess()
        {
            // HDRP exposes these settings through an internal settings type in
            // Unity 6. Resolve the official settings object without binding to a
            // package-private type at compile time.
            var assembly = typeof(HDRenderPipelineAsset).Assembly;
            var type = assembly.GetType("UnityEngine.Rendering.HighDefinition.CustomPostProcessOrdersSettings");
            if (type == null) throw new InvalidOperationException("HDRP custom post-process settings are unavailable.");
            MethodInfo getter = null;
            foreach (var method in typeof(GraphicsSettings).GetMethods(BindingFlags.Public | BindingFlags.Static))
                if (method.Name == "GetRenderPipelineSettings" && method.IsGenericMethodDefinition && method.GetParameters().Length == 0)
                    getter = method;
            if (getter == null) throw new InvalidOperationException("Render-pipeline settings accessor is unavailable.");
            var settings = getter.MakeGenericMethod(type).Invoke(null, null);
            var list = type.GetProperty("afterPostProcessCustomPostProcesses").GetValue(settings);
            var add = list.GetType().GetMethod("Add", new[] { typeof(string) });
            if (add == null) throw new InvalidOperationException("Custom post-process registration is unavailable.");
            add.Invoke(list, new object[] { typeof(NativeMaterialPostProcess).AssemblyQualifiedName });
            var globalType = assembly.GetType("UnityEngine.Rendering.HighDefinition.HDRenderPipelineGlobalSettings");
            var global = globalType.GetMethod("Ensure", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic).Invoke(null, new object[] { true }) as UnityEngine.Object;
            if (global != null) EditorUtility.SetDirty(global);
        }
        static void RenderPreview(Camera camera, int width, int height)
        {
            var target = new RenderTexture(width, height, 24, RenderTextureFormat.ARGB32, RenderTextureReadWrite.sRGB);
            target.Create(); var previous = RenderTexture.active;
            var image = new Texture2D(width, height, TextureFormat.RGB24, false, false);
            try
            {
                var request = new RenderPipeline.StandardRequest { destination = target };
                // Warm HDRP histories and resource bindings before reading back.
                for (int i = 0; i < 8; i++) RenderPipeline.SubmitRenderRequest(camera, request);
                RenderTexture.active = target; image.ReadPixels(new Rect(0, 0, width, height), 0, 0); image.Apply();
                File.WriteAllBytes(ProjectFile("native-preview.png"), image.EncodeToPNG());
            }
            finally { RenderTexture.active = previous; target.Release(); UnityEngine.Object.DestroyImmediate(target); UnityEngine.Object.DestroyImmediate(image); }
        }
    }
}
