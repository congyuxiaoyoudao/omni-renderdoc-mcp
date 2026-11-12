using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;

namespace OmniRenderDoc
{
    public static class RecoveredCaptureBuilder
    {
        [Serializable] public class BufferSpec { public string name, file; public int byte_count; public bool writable; public int uav_register = -1; }
        [Serializable] public class Subresource { public string file; public int mip, slice; }
        [Serializable] public class ColorClearSpec { public string resource_id; public float[] color; }
        [Serializable] public class DepthClearSpec
        {
            public string resource_id;
            public bool clear_depth, clear_stencil;
            public float depth;
            public uint stencil;
        }
        [Serializable] public class TextureSpec
        {
            public string name, file, dynamic, dimension = "Texture2D", format;
            public int width, height, depth = 1, array_size = 1, mip_count = 1;
            public string filter = "Bilinear", wrap_u = "Repeat", wrap_v = "Repeat", wrap_w = "Repeat";
            public int aniso = 1;
            public Subresource[] subresources;
        }
        [Serializable] public class DrawSpec
        {
            public string name, shader, mesh, varyings, clip_positions;
            public int event_id, instance, varying_stride;
            public float clip_y_sign = 1;
            public bool front_face_flip;
            public float[] output_scale;
            public BufferSpec[] constant_buffers, raw_buffers;
            public TextureSpec[] textures;
            public string[] color_targets;
            public string depth_target;
            public int viewport_width, viewport_height;
            public ColorClearSpec[] clear_color_targets;
            public DepthClearSpec clear_depth_target;
        }
        [Serializable] public class TargetSpec
        {
            public string id, format;
            public int width, height;
            public bool depth;
            public float[] clear_color;
            public float clear_depth = 0;
            public uint clear_stencil = 0;
        }
        [Serializable] public class Manifest
        {
            public int version = 1;
            public int source_width, source_height;
            public int preview_width, preview_height;
            public bool clear_color = true, isolated_depth = true;
            public bool render_preview = true;
            public DrawSpec[] draws;
            public TargetSpec[] render_targets;
            public string final_target;
            public bool final_is_srgb;
            public bool final_flip_y;
            public DiagnosticSpec[] diagnostic_targets;
        }
        [Serializable] public class DiagnosticSpec { public string target; public bool is_srgb; }
        [Serializable] public class MeshSpec { public string name; public float[] vertices; public int[] triangles; }
        [Serializable] public class Result
        {
            public string status, error, scene = "Assets/RecoveredCaptureScene.unity", preview = "recovered-preview.png";
            public string appearance_mode = "original_fragment_equations";
            public int draw_count, shader_count, texture_count;
            public List<string> storage_conversions = new List<string>();
            public List<string> diagnostic_previews = new List<string>();
        }

        const string Generated = "Assets/RecoveredShaders/Generated";
        static string Root => Directory.GetParent(Application.dataPath).FullName;
        static readonly Dictionary<string, Texture> TextureCache = new Dictionary<string, Texture>();
        static readonly List<string> StorageConversions = new List<string>();
        static string FullPath(string relative)
        {
            if (string.IsNullOrEmpty(relative) || Path.IsPathRooted(relative)) throw new InvalidDataException("Expected project-relative input: " + relative);
            var root = Path.GetFullPath(Root) + Path.DirectorySeparatorChar;
            var path = Path.GetFullPath(Path.Combine(root, relative));
            if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Path escapes project.");
            return path;
        }
        static T Read<T>(string path) => JsonUtility.FromJson<T>(File.ReadAllText(FullPath(path)));
        static void Save(UnityEngine.Object value, string path)
        {
            var old = AssetDatabase.LoadMainAssetAtPath(path);
            if (old == null) AssetDatabase.CreateAsset(value, path);
            else { EditorUtility.CopySerialized(value, old); EditorUtility.SetDirty(old); UnityEngine.Object.DestroyImmediate(value); }
        }
        static string Digest(string value)
        {
            using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(value))).Replace("-", "").Substring(0, 24);
        }

        static void ValidateDimensions(Manifest manifest)
        {
            if (manifest.source_width <= 0 || manifest.source_height <= 0)
                throw new InvalidDataException("Captured source width and height must be positive.");
            if (manifest.render_preview && (manifest.preview_width <= 0 || manifest.preview_height <= 0))
                throw new InvalidDataException("Preview width and height must be positive when rendering a preview.");
        }

        [MenuItem("Tools/Omni RenderDoc/Build recovered fragment scene")]
        public static void Build()
        {
            if (!Application.isBatchMode && !EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo()) return;
            var result = new Result { status = "building" };
            try
            {
                var manifest = Read<Manifest>("Assets/RecoveredShaders/draws.json");
                ValidateDimensions(manifest);
                if (manifest.draws == null || manifest.draws.Length == 0) throw new InvalidDataException("No recovered draws supplied.");
                Directory.CreateDirectory(FullPath(Generated));
                AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
                ConfigurePipeline();
                EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
                TextureCache.Clear();
                StorageConversions.Clear();
                var runtime = new GameObject("Original captured fragments").AddComponent<CaptureFragmentRuntime>();
                runtime.sourceWidth = manifest.source_width; runtime.sourceHeight = manifest.source_height;
                runtime.clearColor = manifest.clear_color; runtime.isolatedDepth = manifest.isolated_depth;
                runtime.finalTarget = manifest.final_target; runtime.finalIsSrgb = manifest.final_is_srgb;
                runtime.finalFlipY = manifest.final_flip_y;
                foreach (var target in manifest.render_targets ?? Array.Empty<TargetSpec>())
                {
                    Color clear = Color.clear;
                    if (target.clear_color != null && target.clear_color.Length == 4)
                        clear = new Color(target.clear_color[0], target.clear_color[1], target.clear_color[2], target.clear_color[3]);
                    runtime.renderTargets.Add(new CaptureFragmentRuntime.RenderTargetSpec { id = target.id, width = target.width, height = target.height, depth = target.depth, format = target.depth ? DepthFormat(target.format).ToString() : Format(target.format).ToString(), clearColor = clear, clearDepth = target.clear_depth, clearStencil = target.clear_stencil });
                }
                if (!string.IsNullOrEmpty(manifest.final_target))
                {
                    var copyShader = Shader.Find("Hidden/OmniRecovered/CopyFinal");
                    if (copyShader == null) throw new InvalidOperationException("Missing RecoveredFinalCopy.shader");
                    string copyPath = Generated + "/FinalColorTransport.mat";
                    Save(new Material(copyShader), copyPath);
                    runtime.finalCopyMaterial = AssetDatabase.LoadAssetAtPath<Material>(copyPath);
                }
                var shaders = new HashSet<string>();
                foreach (var spec in manifest.draws)
                {
                    var shader = Shader.Find(spec.shader);
                    if (shader == null || ShaderUtil.ShaderHasError(shader)) throw new InvalidOperationException("Recovered shader unavailable or failed compilation: " + spec.shader);
                    shaders.Add(spec.shader);
                    var geometry = Read<MeshSpec>(spec.mesh);
                    if (geometry.vertices == null || geometry.vertices.Length % 3 != 0 || geometry.triangles == null)
                        throw new InvalidDataException("Invalid recovered mesh: " + spec.mesh);
                    var vertices = new Vector3[geometry.vertices.Length / 3];
                    for (int i = 0; i < vertices.Length; i++) vertices[i] = new Vector3(geometry.vertices[i * 3], geometry.vertices[i * 3 + 1], geometry.vertices[i * 3 + 2]);
                    var mesh = new Mesh { name = spec.name, indexFormat = IndexFormat.UInt32 };
                    mesh.vertices = vertices; mesh.triangles = geometry.triangles; mesh.RecalculateBounds();
                    string stem = "event_" + spec.event_id + "_instance_" + spec.instance + "_" + result.draw_count;
                    string meshPath = Generated + "/" + stem + ".asset";
                    Save(mesh, meshPath);
                    var material = new Material(shader) { name = "Original PS " + spec.event_id };
                    var draw = new CaptureFragmentRuntime.FragmentDraw
                    {
                        name = spec.name, eventId = spec.event_id, instance = spec.instance,
                        mesh = AssetDatabase.LoadAssetAtPath<Mesh>(meshPath), varyingStride = spec.varying_stride,
                        varyings = string.IsNullOrEmpty(spec.varyings) ? null : Bytes(spec.varyings),
                        clipPositions = Bytes(spec.clip_positions), clipYSign = spec.clip_y_sign,
                        frontFaceFlip = spec.front_face_flip,
                        colorTargets = spec.color_targets, depthTarget = spec.depth_target,
                        viewportWidth = spec.viewport_width, viewportHeight = spec.viewport_height,
                    };
                    if (spec.output_scale != null && spec.output_scale.Length == 4)
                        draw.outputScale = new Vector4(spec.output_scale[0], spec.output_scale[1], spec.output_scale[2], spec.output_scale[3]);
                    foreach (var clear in spec.clear_color_targets ?? Array.Empty<ColorClearSpec>())
                    {
                        if (clear.color == null || clear.color.Length != 4) throw new InvalidDataException("A captured attachment clear requires four color components.");
                        draw.clearColorTargets.Add(new CaptureFragmentRuntime.ColorClear { resource = clear.resource_id, color = new Color(clear.color[0], clear.color[1], clear.color[2], clear.color[3]) });
                    }
                    if (spec.clear_depth_target != null && !string.IsNullOrEmpty(spec.clear_depth_target.resource_id))
                    {
                        var clear = spec.clear_depth_target;
                        draw.clearDepthTarget = new CaptureFragmentRuntime.DepthClear { resource = clear.resource_id, clearDepth = clear.clear_depth, clearStencil = clear.clear_stencil, depth = clear.depth, stencil = clear.stencil };
                    }
                    foreach (var binding in spec.constant_buffers ?? Array.Empty<BufferSpec>())
                        draw.constants.Add(new CaptureFragmentRuntime.BufferBinding { name = binding.name, data = Bytes(binding.file), byteCount = binding.byte_count });
                    foreach (var binding in spec.raw_buffers ?? Array.Empty<BufferSpec>())
                        draw.rawBuffers.Add(new CaptureFragmentRuntime.BufferBinding { name = binding.name, data = Bytes(binding.file), byteCount = binding.byte_count, writable = binding.writable, uavRegister = binding.uav_register });
                    foreach (var texture in spec.textures ?? Array.Empty<TextureSpec>())
                    {
                        if (texture.dynamic == "camera_color") { draw.cameraColorProperties.Add(texture.name); continue; }
                        if (texture.dynamic != null && texture.dynamic.StartsWith("resource:", StringComparison.Ordinal))
                        {
                            draw.resourceProperties.Add(new CaptureFragmentRuntime.DynamicBinding { name = texture.name, resource = texture.dynamic.Substring(9) });
                            continue;
                        }
                        material.SetTexture(texture.name, LoadTexture(texture));
                    }
                    string materialPath = Generated + "/" + stem + ".mat";
                    Save(material, materialPath);
                    draw.material = AssetDatabase.LoadAssetAtPath<Material>(materialPath);
                    runtime.draws.Add(draw); result.draw_count++;
                }
                result.shader_count = shaders.Count; result.texture_count = TextureCache.Count;
                result.storage_conversions.AddRange(StorageConversions);
                var camera = new GameObject("Recovered camera").AddComponent<Camera>();
                camera.tag = "MainCamera"; camera.clearFlags = CameraClearFlags.SolidColor; camera.backgroundColor = Color.black;
                camera.nearClipPlane = 0.01f; camera.farClipPlane = 10000;
                var hd = camera.gameObject.AddComponent<HDAdditionalCameraData>();
                hd.clearColorMode = HDAdditionalCameraData.ClearColorMode.Color; hd.backgroundColorHDR = Color.black;
                hd.antialiasing = HDAdditionalCameraData.AntialiasingMode.None;
                var volume = new GameObject("Recovered fragment ordering").AddComponent<CustomPassVolume>();
                volume.isGlobal = true; volume.injectionPoint = CustomPassInjectionPoint.AfterPostProcess;
                volume.customPasses.Add(new RecoveredFragmentPass { name = "Original captured fragment programs", source = runtime });
                EditorSceneManager.SaveScene(camera.gameObject.scene, result.scene);
                AssetDatabase.SaveAssets();
                runtime.Prepare();
                if (manifest.render_preview) Render(camera, manifest.preview_width, manifest.preview_height, result.preview);
                else result.preview = null;
                if (!string.IsNullOrEmpty(runtime.lastRenderError))
                    throw new InvalidOperationException("Recovered pass failed: " + runtime.lastRenderError);
                if (manifest.render_preview)
                {
                    foreach (var diagnostic in manifest.diagnostic_targets ?? Array.Empty<DiagnosticSpec>())
                    {
                        if (!ulong.TryParse(diagnostic.target, out _)) throw new InvalidDataException("Diagnostic resource IDs must be numeric.");
                        string filename = "recovered-target-" + diagnostic.target + ".png";
                        runtime.finalTarget = diagnostic.target; runtime.finalIsSrgb = diagnostic.is_srgb; runtime.finalFlipY = false;
                        try
                        {
                            Render(camera, manifest.preview_width, manifest.preview_height, filename);
                            if (!string.IsNullOrEmpty(runtime.lastRenderError)) throw new InvalidOperationException("Diagnostic pass failed: " + runtime.lastRenderError);
                            result.diagnostic_previews.Add(filename);
                        }
                        finally { runtime.finalTarget = manifest.final_target; runtime.finalIsSrgb = manifest.final_is_srgb; runtime.finalFlipY = manifest.final_flip_y; }
                    }
                }
                result.status = "complete";
                File.WriteAllText(Path.Combine(Root, "recovered-result.json"), JsonUtility.ToJson(result, true));
                Debug.Log("OMNI_ORIGINAL_FRAGMENTS_COMPLETE " + JsonUtility.ToJson(result));
            }
            catch (Exception error)
            {
                result.status = "failed"; result.error = error.ToString();
                File.WriteAllText(Path.Combine(Root, "recovered-result.json"), JsonUtility.ToJson(result, true));
                Debug.LogException(error);
                if (Application.isBatchMode) EditorApplication.Exit(1); else throw;
            }
        }

        static TextAsset Bytes(string path)
        {
            FullPath(path);
            var file = AssetDatabase.LoadAssetAtPath<TextAsset>(path);
            if (file == null) throw new InvalidDataException("Missing byte asset (use .bytes extension): " + path);
            return file;
        }
        static GraphicsFormat DepthFormat(string source)
        {
            switch ((source ?? "").ToUpperInvariant())
            {
                case "D16": case "D16_UNORM": return GraphicsFormat.D16_UNorm;
                case "D24S8": case "D24_UNORM_S8_UINT": return GraphicsFormat.D24_UNorm_S8_UInt;
                case "D32": case "D32_FLOAT": case "D32_SFLOAT": return GraphicsFormat.D32_SFloat;
                case "D32S8": case "D32_SFLOAT_S8_UINT": return GraphicsFormat.D32_SFloat_S8_UInt;
                default: throw new InvalidDataException("Unsupported captured depth target format: " + source);
            }
        }
        static GraphicsFormat Format(string source)
        {
            string name = source.Replace("VK_FORMAT_", "").Replace("_FLOAT", "_SFLOAT");
            var formats = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                { "BC1_UNORM", "RGBA_DXT1_UNorm" }, { "BC1_UNORM_SRGB", "RGBA_DXT1_SRGB" }, { "BC1_SRGB", "RGBA_DXT1_SRGB" },
                { "BC2_UNORM", "RGBA_DXT3_UNorm" }, { "BC2_UNORM_SRGB", "RGBA_DXT3_SRGB" },
                { "BC3_UNORM", "RGBA_DXT5_UNorm" }, { "BC3_UNORM_SRGB", "RGBA_DXT5_SRGB" },
                { "BC3_SRGB", "RGBA_DXT5_SRGB" },
                { "BC4_UNORM", "R_BC4_UNorm" }, { "BC4_SNORM", "R_BC4_SNorm" },
                { "BC5_UNORM", "RG_BC5_UNorm" }, { "BC5_SNORM", "RG_BC5_SNorm" },
                { "BC6_UFLOAT", "RGB_BC6H_UFloat" }, { "BC6_SFLOAT", "RGB_BC6H_SFloat" },
                { "BC7_UNORM", "RGBA_BC7_UNorm" }, { "BC7_UNORM_SRGB", "RGBA_BC7_SRGB" }, { "BC7_SRGB", "RGBA_BC7_SRGB" },
                { "D32_SFLOAT", "R32_SFloat" }, { "D32", "R32_SFloat" }, { "D16_UNORM", "R16_UNorm" },
                { "D16", "R16_UNorm" }, { "R10G10B10A2_UNORM", "A2B10G10R10_UNormPack32" },
                { "R11G11B10_UFLOAT", "B10G11R11_UFloatPack32" },
                { "R11G11B10_SFLOAT", "B10G11R11_UFloatPack32" },
                { "R8G8B8A8_UNORM_SRGB", "R8G8B8A8_SRGB" }, { "B8G8R8A8_UNORM_SRGB", "B8G8R8A8_SRGB" },
            };
            if (formats.TryGetValue(name, out var mapped)) name = mapped;
            if (!Enum.TryParse(name, true, out GraphicsFormat format) || format == GraphicsFormat.None)
                throw new InvalidDataException("Unsupported captured native format: " + source);
            return format;
        }
        static Texture LoadTexture(TextureSpec spec)
        {
            if (!string.IsNullOrEmpty(spec.file))
            {
                var imported = AssetDatabase.LoadAssetAtPath<Texture>(spec.file);
                if (imported == null) throw new InvalidDataException("Missing captured texture asset " + spec.file);
                return imported;
            }
            string key = Digest(JsonUtility.ToJson(spec));
            if (TextureCache.TryGetValue(key, out var cached)) return cached;
            string path = Generated + "/texture_" + key + ".asset";
            var format = Format(spec.format);
            if (spec.width <= 0 || spec.height <= 0 || spec.mip_count <= 0 || spec.subresources == null)
                throw new InvalidDataException("Invalid native texture description " + spec.name);
            var flags = spec.mip_count > 1 ? TextureCreationFlags.MipChain : TextureCreationFlags.None;
            Texture texture;
            string dimension = (spec.dimension ?? "Texture2D").ToLowerInvariant();
            // R11G11B10 uses the same five-bit exponent/bias as binary16.
            // Padding its mantissas with zero bits is exact, including subnormals.
            bool expandPackedFloat = format == GraphicsFormat.B10G11R11_UFloatPack32 &&
                (dimension.Contains("3d") || !SystemInfo.IsFormatSupported(format, GraphicsFormatUsage.Sample));
            bool expandPackedUNorm = format == GraphicsFormat.A2B10G10R10_UNormPack32 &&
                !SystemInfo.IsFormatSupported(format, GraphicsFormatUsage.Sample);
            if (expandPackedFloat)
            {
                format = GraphicsFormat.R16G16B16A16_SFloat;
                StorageConversions.Add(spec.name + ": " + spec.format + " -> RGBA16F storage; exact exponent/mantissa mapping, alpha=1; mip/volume layout preserved.");
            }
            if (expandPackedUNorm)
            {
                format = GraphicsFormat.R32G32B32A32_SFloat;
                StorageConversions.Add(spec.name + ": " + spec.format + " -> RGBA32F sampled storage; all original 10/10/10/2-bit UNorm codes preserved through float normalization. Live render targets retain native packed precision.");
            }
            var existing = AssetDatabase.LoadAssetAtPath<Texture>(path);
            if (existing != null) { TextureCache[key] = existing; return existing; }
            if (dimension.Contains("3d"))
            {
                var data = new Texture3D(spec.width, spec.height, spec.depth, format, flags, spec.mip_count);
                foreach (var sub in spec.subresources) data.SetPixelData(TextureBytes(sub.file, expandPackedFloat, expandPackedUNorm), sub.mip);
                data.Apply(false, true); texture = data;
            }
            else if (dimension.Contains("cube") && spec.array_size == 6)
            {
                var data = new Cubemap(spec.width, format, flags, spec.mip_count);
                foreach (var sub in spec.subresources) data.SetPixelData(TextureBytes(sub.file, expandPackedFloat, expandPackedUNorm), sub.mip, (CubemapFace)sub.slice);
                data.Apply(false, true); texture = data;
            }
            else if (dimension.Contains("array") || spec.array_size > 1)
            {
                var data = new Texture2DArray(spec.width, spec.height, spec.array_size, format, flags, spec.mip_count);
                foreach (var sub in spec.subresources) data.SetPixelData(TextureBytes(sub.file, expandPackedFloat, expandPackedUNorm), sub.mip, sub.slice);
                data.Apply(false, true); texture = data;
            }
            else
            {
                var data = new Texture2D(spec.width, spec.height, format, spec.mip_count, flags);
                foreach (var sub in spec.subresources) data.SetPixelData(TextureBytes(sub.file, expandPackedFloat, expandPackedUNorm), sub.mip);
                data.Apply(false, true); texture = data;
            }
            texture.name = spec.name;
            texture.filterMode = (FilterMode)Enum.Parse(typeof(FilterMode), spec.filter, true);
            texture.wrapModeU = (TextureWrapMode)Enum.Parse(typeof(TextureWrapMode), spec.wrap_u, true);
            texture.wrapModeV = (TextureWrapMode)Enum.Parse(typeof(TextureWrapMode), spec.wrap_v, true);
            texture.wrapModeW = (TextureWrapMode)Enum.Parse(typeof(TextureWrapMode), spec.wrap_w, true);
            texture.anisoLevel = spec.aniso;
            AssetDatabase.CreateAsset(texture, path); TextureCache[key] = texture;
            return texture;
        }

        static byte[] TextureBytes(string path, bool expandPackedFloat, bool expandPackedUNorm)
        {
            var input = File.ReadAllBytes(FullPath(path));
            if (expandPackedUNorm)
            {
                if (input.Length % 4 != 0) throw new InvalidDataException("Invalid packed R10G10B10A2 byte count: " + path);
                var normalized = new float[input.Length];
                for (int src = 0; src < input.Length; src += 4)
                {
                    uint packed = (uint)(input[src] | input[src + 1] << 8 | input[src + 2] << 16 | input[src + 3] << 24);
                    normalized[src] = (packed & 1023u) / 1023f;
                    normalized[src + 1] = ((packed >> 10) & 1023u) / 1023f;
                    normalized[src + 2] = ((packed >> 20) & 1023u) / 1023f;
                    normalized[src + 3] = (packed >> 30) / 3f;
                }
                var expanded = new byte[input.Length * 4];
                Buffer.BlockCopy(normalized, 0, expanded, 0, expanded.Length);
                return expanded;
            }
            if (!expandPackedFloat) return input;
            if (input.Length % 4 != 0) throw new InvalidDataException("Invalid packed R11G11B10 byte count: " + path);
            var output = new byte[input.Length * 2];
            for (int src = 0, dst = 0; src < input.Length; src += 4, dst += 8)
            {
                uint packed = (uint)(input[src] | input[src + 1] << 8 | input[src + 2] << 16 | input[src + 3] << 24);
                uint r = (packed & 0x7ffu) << 4;
                uint g = ((packed >> 11) & 0x7ffu) << 4;
                uint b = ((packed >> 22) & 0x3ffu) << 5;
                output[dst] = (byte)r; output[dst + 1] = (byte)(r >> 8);
                output[dst + 2] = (byte)g; output[dst + 3] = (byte)(g >> 8);
                output[dst + 4] = (byte)b; output[dst + 5] = (byte)(b >> 8);
                output[dst + 6] = 0; output[dst + 7] = 0x3c;
            }
            return output;
        }

        static void ConfigurePipeline()
        {
            PlayerSettings.colorSpace = ColorSpace.Linear;
            var asset = GraphicsSettings.defaultRenderPipeline as HDRenderPipelineAsset;
            if (asset == null)
            {
                asset = ScriptableObject.CreateInstance<HDRenderPipelineAsset>();
                AssetDatabase.CreateAsset(asset, Generated + "/RecoveredHDRP.asset");
                GraphicsSettings.defaultRenderPipeline = asset;
            }
            QualitySettings.renderPipeline = asset;
            var serialized = new SerializedObject(asset);
            var custom = serialized.FindProperty("m_RenderPipelineSettings.supportCustomPass");
            if (custom != null) { custom.boolValue = true; serialized.ApplyModifiedPropertiesWithoutUndo(); }
            var global = typeof(HDRenderPipelineAsset).Assembly.GetType("UnityEngine.Rendering.HighDefinition.HDRenderPipelineGlobalSettings");
            global?.GetMethod("Ensure", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic)?.Invoke(null, new object[] { true });
        }
        public static void Render(Camera camera, int width, int height, string filename)
        {
            var target = new RenderTexture(width, height, 24, RenderTextureFormat.ARGB32, RenderTextureReadWrite.sRGB);
            target.Create();
            var previous = RenderTexture.active;
            var image = new Texture2D(width, height, TextureFormat.RGB24, false, false);
            try
            {
                var request = new RenderPipeline.StandardRequest { destination = target };
                RenderPipeline.SubmitRenderRequest(camera, request);
                RenderPipeline.SubmitRenderRequest(camera, request);
                if (!(RenderPipelineManager.currentPipeline is HDRenderPipeline)) throw new InvalidOperationException("HDRP failed to initialize.");
                RenderTexture.active = target; image.ReadPixels(new Rect(0, 0, width, height), 0, 0); image.Apply();
                File.WriteAllBytes(Path.Combine(Root, filename), image.EncodeToPNG());
            }
            finally
            {
                RenderTexture.active = previous; target.Release(); UnityEngine.Object.DestroyImmediate(target); UnityEngine.Object.DestroyImmediate(image);
            }
        }
    }
}
