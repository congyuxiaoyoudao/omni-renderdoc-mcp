using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using UnityEditor;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;

namespace OmniRenderDoc
{
    // Build ordinary editable materials from explicit, externally verified
    // semantics, using HDRP/Lit or a supplied native mesh shader.
    public static class NativeMaterialFactory
    {
        [Serializable] public sealed class TextureSpec
        {
            public string asset;
            public int uv;
            public float[] scale_offset;
            public bool source_is_srgb, source_color_space_explicit;
        }
        [Serializable] public sealed class ChannelSpec
        {
            public TextureSpec texture;
            public int channel;
            public float scale = 1, bias;
            public bool invert;
        }
        [Serializable] public sealed class FloatParameter { public string name; public float value; }
        [Serializable] public sealed class VectorParameter { public string name; public float[] value; }
        [Serializable] public sealed class TextureParameter { public string name; public TextureSpec texture; }
        [Serializable] public sealed class ShaderSpec
        {
            public string asset;
            public int render_queue = 3000;
            public FloatParameter[] floats;
            public VectorParameter[] vectors;
            public TextureParameter[] textures;
        }
        [Serializable] public sealed class MaterialSpec
        {
            public string name, provenance, source_shader_sha256;
            public string renderer_category = "surface", renderer_reason;
            public bool renderer_enabled = true;
            public bool cast_shadows = true, receive_shadows = true;
            public ShaderSpec explicit_shader;
            public string fidelity = "explicit_pbr_mapping";
            public TextureSpec base_map, normal_map, emission_map;
            public string normal_encoding = "rg";
            public float[] base_color = { 1, 1, 1, 1 };
            public float base_intensity = 1, base_saturation = 1, constant_color_blend;
            public float normal_scale = 1;
            public bool normal_flip_y;
            public ChannelSpec metallic_map, roughness_map, occlusion_map, opacity_map;
            public float metallic, roughness = 0.7f, occlusion = 1;
            public float[] emission_color = { 0, 0, 0 };
            public float emission_intensity = 1;
            public bool alpha_from_base, alpha_clip, double_sided;
            public float alpha_cutoff = 0.5f, opacity = 1;
            public string surface = "opaque", blend = "alpha";
            public string cull_mode;
        }
        [Serializable] public sealed class MaterialEntry { public string key; public MaterialSpec spec; }
        [Serializable] public sealed class MaterialLibrary { public int version; public MaterialEntry[] entries; }
        static readonly Dictionary<string, Material> Cache = new Dictionary<string, Material>();
        static string importFolder;

        public static MaterialLibrary LoadLibrary(string assetPath)
        {
            var library = JsonUtility.FromJson<MaterialLibrary>(File.ReadAllText(assetPath));
            if (library == null || library.version != 1 || library.entries == null) throw new InvalidDataException("Expected material library version 1.");
            return library;
        }

        static string Digest(string value)
        {
            using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(value))).Replace("-", "").Substring(0, 24);
        }
        static string AssetFingerprint(string asset)
        {
            if (string.IsNullOrEmpty(asset)) return "none";
            if (!asset.StartsWith("Assets/", StringComparison.Ordinal) || asset.Contains(".."))
                throw new InvalidDataException("Material input paths must stay inside Assets.");
            // Includes source bytes and importer settings. A path alone becomes
            // stale when an editable input is overwritten and reimported.
            return asset + "@" + AssetDatabase.GetAssetDependencyHash(asset).ToString();
        }
        static string TextureFingerprint(TextureSpec spec) => spec == null ? "none" :
            JsonUtility.ToJson(spec) + "|" + AssetFingerprint(spec.asset);
        static string ChannelFingerprint(ChannelSpec spec) => spec == null ? "none" :
            JsonUtility.ToJson(spec) + "|" + TextureFingerprint(spec.texture);
        static string MaterialDependencies(MaterialSpec spec)
        {
            var paths = new SortedSet<string>(StringComparer.Ordinal);
            void Add(TextureSpec texture) { if (!string.IsNullOrEmpty(texture?.asset)) paths.Add(texture.asset); }
            Add(spec.base_map); Add(spec.normal_map); Add(spec.emission_map);
            Add(spec.metallic_map?.texture); Add(spec.roughness_map?.texture);
            Add(spec.occlusion_map?.texture); Add(spec.opacity_map?.texture);
            if (spec.explicit_shader != null)
            {
                if (!string.IsNullOrEmpty(spec.explicit_shader.asset)) paths.Add(spec.explicit_shader.asset);
                foreach (var parameter in spec.explicit_shader.textures ?? Array.Empty<TextureParameter>()) Add(parameter?.texture);
            }
            var dependencies = new StringBuilder();
            foreach (string path in paths) dependencies.Append('\n').Append(AssetFingerprint(path));
            return dependencies.ToString();
        }
        static UnityEngine.Texture ShaderTexture(TextureSpec spec)
        {
            if (spec == null || string.IsNullOrEmpty(spec.asset)) return null;
            string fingerprint = AssetFingerprint(spec.asset);
            string path = spec.asset;
            if (spec.source_color_space_explicit && AssetImporter.GetAtPath(path) is TextureImporter sourceImporter)
            {
                Directory.CreateDirectory(importFolder);
                string copyPath = importFolder + "/input_" + Digest(fingerprint + "|" + spec.source_is_srgb) + Path.GetExtension(path);
                if (AssetDatabase.LoadAssetAtPath<UnityEngine.Texture>(copyPath) == null)
                {
                    if (!AssetDatabase.CopyAsset(path, copyPath)) throw new IOException("Could not create independent material input: " + path);
                    var importer = (TextureImporter)AssetImporter.GetAtPath(copyPath);
                    // TextureType controls decoding; TextureShape controls 2D,
                    // cube, array or volume layout. Preserve the latter when
                    // requesting ordinary sampled values and explicit sRGB.
                    var shape = sourceImporter.textureShape;
                    importer.textureType = TextureImporterType.Default;
                    importer.textureShape = shape;
                    importer.sRGBTexture = spec.source_is_srgb;
                    importer.textureCompression = TextureImporterCompression.Uncompressed;
                    importer.SaveAndReimport();
                }
                path = copyPath;
            }
            var texture = AssetDatabase.LoadAssetAtPath<UnityEngine.Texture>(path);
            if (texture == null) throw new InvalidDataException("Missing material texture: " + spec.asset);
            if (!(texture is Texture2D || texture is Texture2DArray || texture is Texture3D || texture is Cubemap || texture is CubemapArray))
                throw new InvalidDataException("Unsupported material texture type: " + texture.GetType().Name);
            if (spec.source_color_space_explicit && GraphicsFormatUtility.IsSRGBFormat(texture.graphicsFormat) != spec.source_is_srgb)
                throw new InvalidDataException("Material texture color space differs from its explicit descriptor: " + spec.asset);
            return texture;
        }
        static Texture2D Texture(TextureSpec spec)
        {
            var texture = ShaderTexture(spec);
            if (texture == null) return null;
            if (!(texture is Texture2D image))
                throw new InvalidDataException("PBR material mapping requires a 2D texture: " + spec.asset);
            return image;
        }
        static float Scalar(float value, string name)
        {
            if (!float.IsFinite(value)) throw new InvalidDataException("Nonfinite material value: " + name);
            return value;
        }
        static Color ColorValue(float[] values, Color fallback)
        {
            if (values == null) return fallback;
            if (values.Length != 3 && values.Length != 4) throw new InvalidDataException("A material color requires RGB or RGBA.");
            return new Color(Scalar(values[0], "color"), Scalar(values[1], "color"), Scalar(values[2], "color"), values.Length == 4 ? Scalar(values[3], "alpha") : 1);
        }

        // Source native Texture2D assets are intentionally non-readable. GPU
        // decode obtains their sampled values without changing their importers.
        static Color[] ReadPixels(Texture2D source, int width, int height)
        {
            var target = RenderTexture.GetTemporary(width, height, 0, RenderTextureFormat.ARGBFloat, RenderTextureReadWrite.Linear);
            var previous = RenderTexture.active;
            bool previousSrgb = GL.sRGBWrite;
            Texture2D image = null;
            try
            {
                GL.sRGBWrite = false;
                Graphics.Blit(source, target);
                RenderTexture.active = target;
                image = new Texture2D(width, height, TextureFormat.RGBAFloat, false, true);
                image.ReadPixels(new Rect(0, 0, width, height), 0, 0); image.Apply();
                return image.GetPixels();
            }
            finally
            {
                RenderTexture.active = previous; GL.sRGBWrite = previousSrgb;
                RenderTexture.ReleaseTemporary(target);
                if (image != null) UnityEngine.Object.DestroyImmediate(image);
            }
        }
        static Texture2D WriteTexture(string path, Color[] pixels, int width, int height, bool srgb, bool normal, Texture2D source)
        {
            var image = new Texture2D(width, height, TextureFormat.RGBA32, false, true);
            try
            {
                if (srgb)
                    for (int i = 0; i < pixels.Length; i++) { float alpha = pixels[i].a; pixels[i] = pixels[i].gamma; pixels[i].a = alpha; }
                image.SetPixels(pixels); image.Apply();
                File.WriteAllBytes(path, image.EncodeToPNG());
            }
            finally { UnityEngine.Object.DestroyImmediate(image); }
            AssetDatabase.ImportAsset(path, ImportAssetOptions.ForceSynchronousImport);
            var importer = (TextureImporter)AssetImporter.GetAtPath(path);
            importer.textureType = normal ? TextureImporterType.NormalMap : TextureImporterType.Default;
            importer.sRGBTexture = srgb && !normal;
            importer.textureCompression = TextureImporterCompression.Uncompressed;
            importer.maxTextureSize = Mathf.NextPowerOfTwo(Mathf.Max(width, height));
            importer.mipmapEnabled = true;
            importer.wrapModeU = source.wrapModeU; importer.wrapModeV = source.wrapModeV;
            importer.filterMode = source.filterMode; importer.anisoLevel = source.anisoLevel;
            importer.SaveAndReimport();
            return AssetDatabase.LoadAssetAtPath<Texture2D>(path);
        }
        static Texture2D BaseMap(MaterialSpec spec, string folder)
        {
            var source = Texture(spec.base_map);
            var opacitySource = Texture(spec.opacity_map?.texture);
            if (source == null && opacitySource == null) return null;
            var basis = source != null ? source : opacitySource;
            // Base alpha often stores a material mask, not opacity. Preserve it
            // only when its alpha semantics were explicitly supplied.
            string identity = TextureFingerprint(spec.base_map) + "|" + spec.base_intensity + "|" + spec.base_saturation + "|" + spec.constant_color_blend + "|" + spec.alpha_from_base + "|" + ChannelFingerprint(spec.opacity_map);
            string path = folder + "/base_" + Digest(identity) + ".png";
            var existing = AssetDatabase.LoadAssetAtPath<Texture2D>(path); if (existing != null) return existing;
            var pixels = source == null ? new Color[basis.width * basis.height] : ReadPixels(source, basis.width, basis.height);
            if (source == null) for (int i = 0; i < pixels.Length; i++) pixels[i] = Color.white;
            var opacityPixels = opacitySource == null ? null : ReadPixels(opacitySource, basis.width, basis.height);
            for (int i = 0; i < pixels.Length; i++)
            {
                var color = pixels[i];
                float luminance = color.r * 0.2126729f + color.g * 0.7151522f + color.b * 0.0721750f;
                color.r = Mathf.LerpUnclamped(luminance, color.r, spec.base_saturation) * spec.base_intensity;
                color.g = Mathf.LerpUnclamped(luminance, color.g, spec.base_saturation) * spec.base_intensity;
                color.b = Mathf.LerpUnclamped(luminance, color.b, spec.base_saturation) * spec.base_intensity;
                color.r = Mathf.Lerp(Mathf.Clamp01(color.r), 1, spec.constant_color_blend);
                color.g = Mathf.Lerp(Mathf.Clamp01(color.g), 1, spec.constant_color_blend);
                color.b = Mathf.Lerp(Mathf.Clamp01(color.b), 1, spec.constant_color_blend);
                if (!spec.alpha_from_base) color.a = 1;
                if (opacityPixels != null) color.a = Channel(opacityPixels[i], spec.opacity_map);
                pixels[i] = color;
            }
            return WriteTexture(path, pixels, basis.width, basis.height, true, false, basis);
        }
        static Texture2D NormalMap(MaterialSpec spec, string folder)
        {
            var source = Texture(spec.normal_map); if (source == null) return null;
            if (spec.normal_encoding != "rg" && spec.normal_encoding != "ag_mul_r" && spec.normal_encoding != "rgb")
                throw new InvalidDataException("Unsupported explicit normal encoding: " + spec.normal_encoding);
            string path = folder + "/normal_" + Digest(TextureFingerprint(spec.normal_map) + "|" + spec.normal_encoding + "|" + spec.normal_flip_y) + ".png";
            var existing = AssetDatabase.LoadAssetAtPath<Texture2D>(path); if (existing != null) return existing;
            var pixels = ReadPixels(source, source.width, source.height);
            for (int i = 0; i < pixels.Length; i++)
            {
                var p = pixels[i];
                float x = (spec.normal_encoding == "ag_mul_r" ? p.a * p.r : p.r) * 2 - 1;
                float y = p.g * 2 - 1; if (spec.normal_flip_y) y = -y;
                float z = spec.normal_encoding == "rgb" ? p.b * 2 - 1 : Mathf.Sqrt(Mathf.Max(0, 1 - x * x - y * y));
                var normal = new Vector3(x, y, z).normalized;
                pixels[i] = new Color(normal.x * .5f + .5f, normal.y * .5f + .5f, normal.z * .5f + .5f, 1);
            }
            return WriteTexture(path, pixels, source.width, source.height, false, true, source);
        }
        static float Channel(Color value, ChannelSpec channel)
        {
            if (channel.channel < 0 || channel.channel > 3) throw new InvalidDataException("Material channel must be 0..3.");
            float result = value[channel.channel]; if (channel.invert) result = 1 - result;
            return Mathf.Clamp01(result * Scalar(channel.scale, "channel scale") + Scalar(channel.bias, "channel bias"));
        }
        static Texture2D MaskMap(MaterialSpec spec, string folder)
        {
            var channels = new[] { spec.metallic_map, spec.occlusion_map, spec.roughness_map };
            Texture2D basis = null;
            foreach (var channel in channels) { var tex = Texture(channel?.texture); if (tex != null && (basis == null || tex.width * tex.height > basis.width * basis.height)) basis = tex; }
            if (basis == null) return null;
            string identity = ChannelFingerprint(spec.metallic_map) + "|" + ChannelFingerprint(spec.occlusion_map) + "|" + ChannelFingerprint(spec.roughness_map) + "|" + spec.metallic + "|" + spec.roughness + "|" + spec.occlusion;
            string path = folder + "/mask_" + Digest(identity) + ".png";
            var existing = AssetDatabase.LoadAssetAtPath<Texture2D>(path); if (existing != null) return existing;
            var decoded = new Dictionary<Texture2D, Color[]>();
            var samples = new Color[3][];
            for (int c = 0; c < channels.Length; c++)
            {
                var tex = Texture(channels[c]?.texture); if (tex == null) continue;
                if (!decoded.TryGetValue(tex, out samples[c])) { samples[c] = ReadPixels(tex, basis.width, basis.height); decoded.Add(tex, samples[c]); }
            }
            var output = new Color[basis.width * basis.height];
            for (int i = 0; i < output.Length; i++)
                output[i] = new Color(samples[0] == null ? spec.metallic : Channel(samples[0][i], channels[0]),
                    samples[1] == null ? spec.occlusion : Channel(samples[1][i], channels[1]), 1,
                    1 - (samples[2] == null ? spec.roughness : Channel(samples[2][i], channels[2])));
            return WriteTexture(path, output, basis.width, basis.height, false, false, basis);
        }
        static void Mapping(Material material, string property, TextureSpec texture)
        {
            if (texture?.scale_offset != null)
            {
                if (texture.scale_offset.Length != 4) throw new InvalidDataException("Texture transform needs scale XY and offset XY.");
                material.SetTextureScale(property, new Vector2(texture.scale_offset[0], texture.scale_offset[1]));
                material.SetTextureOffset(property, new Vector2(texture.scale_offset[2], texture.scale_offset[3]));
            }
        }
        static void SharedMapping(TextureSpec basis, TextureSpec other, string role)
        {
            if (other == null) return;
            if (other.uv != (basis?.uv ?? 0)) throw new InvalidDataException("HDRP/Lit shares base UVs with " + role + "; provide a compatible material profile.");
            if (other.scale_offset == null) return; // Unspecified inherits base.
            var expected = basis?.scale_offset ?? new float[] { 1, 1, 0, 0 };
            if (other.scale_offset.Length != 4 || expected.Length != 4) throw new InvalidDataException("Texture transforms require four values.");
            for (int i = 0; i < 4; i++)
                if (other.scale_offset[i] != expected[i]) throw new InvalidDataException("HDRP/Lit cannot apply an independent " + role + " texture transform.");
        }

        public static Material Create(MaterialSpec spec, string generatedFolder, string key)
        {
            if (spec == null) throw new ArgumentNullException(nameof(spec));
            if (!generatedFolder.StartsWith("Assets/", StringComparison.Ordinal) || generatedFolder.Contains("..")) throw new InvalidDataException("Generated materials must stay in Assets.");
            if (spec.surface != "opaque" && spec.surface != "transparent") throw new InvalidDataException("Unknown material surface mode.");
            bool explicitAsset = spec.explicit_shader != null && !string.IsNullOrEmpty(spec.explicit_shader.asset);
            if (spec.blend != "alpha" && spec.blend != "additive" && spec.blend != "premultiply" &&
                !(explicitAsset && spec.blend == "multiply")) throw new InvalidDataException("Unknown or unsupported transparency blend mode.");
            Shader explicitShader = null;
            // A cached material still needs validation if its shader was edited.
            if (spec.explicit_shader != null && !string.IsNullOrEmpty(spec.explicit_shader.asset))
            {
                var asset = spec.explicit_shader.asset;
                if (!asset.StartsWith("Assets/", StringComparison.Ordinal) || asset.Contains(".."))
                    throw new InvalidDataException("Explicit material shaders must reference an Assets path.");
                explicitShader = AssetDatabase.LoadAssetAtPath<Shader>(asset);
                if (explicitShader == null) throw new InvalidDataException("Missing explicit material shader: " + asset);
                if (ShaderUtil.ShaderHasError(explicitShader))
                {
                    var errors = new StringBuilder("Explicit material shader has compilation errors: " + asset);
                    foreach (var message in ShaderUtil.GetShaderMessages(explicitShader))
                        errors.Append("\n").Append(message.severity).Append(": ").Append(message.file)
                            .Append(':').Append(message.line).Append(" ").Append(message.message);
                    throw new InvalidDataException(errors.ToString());
                }
            }
            var canonical = JsonUtility.FromJson<MaterialSpec>(JsonUtility.ToJson(spec));
            canonical.name = null; canonical.provenance = null; canonical.source_shader_sha256 = null;
            string digest = Digest(JsonUtility.ToJson(canonical) + MaterialDependencies(canonical)), cacheKey = generatedFolder + "/" + digest;
            if (Cache.TryGetValue(cacheKey, out var cached) && cached != null) return cached;
            Directory.CreateDirectory(generatedFolder);
            importFolder = generatedFolder + "/InputTextures";
            string path = generatedFolder + "/material_" + digest + ".mat";
            var existing = AssetDatabase.LoadAssetAtPath<Material>(path); if (existing != null) { Cache[cacheKey] = existing; return existing; }
            // Unity may deserialize an omitted nested serializable class as an
            // empty object. The optional asset path selects an explicit shader.
            if (spec.explicit_shader != null && !string.IsNullOrEmpty(spec.explicit_shader.asset))
            {
                var descriptor = spec.explicit_shader;
                var explicitMaterial = new Material(explicitShader) { name = string.IsNullOrEmpty(spec.name) ? key : spec.name, renderQueue = descriptor.render_queue };
                if (descriptor.floats != null) foreach (var parameter in descriptor.floats)
                {
                    if (!explicitMaterial.HasProperty(parameter.name)) throw new InvalidDataException("Unknown material property: " + parameter.name);
                    explicitMaterial.SetFloat(parameter.name, Scalar(parameter.value, parameter.name));
                }
                if (descriptor.vectors != null) foreach (var parameter in descriptor.vectors)
                {
                    if (!explicitMaterial.HasProperty(parameter.name)) throw new InvalidDataException("Unknown material property: " + parameter.name);
                    explicitMaterial.SetVector(parameter.name, ColorValue(parameter.value, Color.clear));
                }
                if (descriptor.textures != null) foreach (var parameter in descriptor.textures)
                {
                    if (!explicitMaterial.HasProperty(parameter.name)) throw new InvalidDataException("Unknown material property: " + parameter.name);
                    var texture = ShaderTexture(parameter.texture);
                    int property = explicitShader.FindPropertyIndex(parameter.name);
                    if (texture != null && property >= 0 && explicitShader.GetPropertyTextureDimension(property) != texture.dimension)
                        throw new InvalidDataException("Shader texture dimension mismatch for " + parameter.name + ": " + texture.dimension);
                    explicitMaterial.SetTexture(parameter.name, texture);
                    Mapping(explicitMaterial, parameter.name, parameter.texture);
                }
                AssetDatabase.CreateAsset(explicitMaterial, path);
                Cache[cacheKey] = explicitMaterial;
                return explicitMaterial;
            }
            SharedMapping(spec.base_map, spec.normal_map, "normal");
            SharedMapping(spec.base_map, spec.metallic_map?.texture, "metallic");
            SharedMapping(spec.base_map, spec.roughness_map?.texture, "roughness");
            SharedMapping(spec.base_map, spec.occlusion_map?.texture, "occlusion");
            SharedMapping(spec.base_map, spec.opacity_map?.texture, "opacity");
            var shader = Shader.Find("HDRP/Lit"); if (shader == null) throw new InvalidOperationException("HDRP/Lit is unavailable.");
            var material = new Material(shader) { name = string.IsNullOrEmpty(spec.name) ? key : spec.name };
            var tint = ColorValue(spec.base_color, Color.white); tint.a *= Mathf.Clamp01(Scalar(spec.opacity, "opacity"));
            material.SetVector("_BaseColor", tint);
            material.SetFloat("_Metallic", Mathf.Clamp01(Scalar(spec.metallic, "metallic")));
            material.SetFloat("_Smoothness", 1 - Mathf.Clamp01(Scalar(spec.roughness, "roughness")));
            material.SetFloat("_NormalScale", Scalar(spec.normal_scale, "normal scale"));
            material.SetTexture("_BaseColorMap", BaseMap(spec, generatedFolder));
            material.SetTexture("_NormalMap", NormalMap(spec, generatedFolder));
            var mask = MaskMap(spec, generatedFolder);
            if (mask != null)
            {
                material.SetTexture("_MaskMap", mask);
                material.SetFloat("_MetallicRemapMin", 0); material.SetFloat("_MetallicRemapMax", 1);
                material.SetFloat("_AORemapMin", 0); material.SetFloat("_AORemapMax", 1);
                material.SetFloat("_SmoothnessRemapMin", 0); material.SetFloat("_SmoothnessRemapMax", 1);
            }
            Mapping(material, "_BaseColorMap", spec.base_map);
            int uv = spec.base_map?.uv ?? 0;
            if (uv < 0 || uv > 3) throw new InvalidDataException("HDRP/Lit base mapping supports UV0..UV3.");
            material.SetFloat("_UVBase", uv);
            var uvMask = Vector4.zero; uvMask[uv] = 1; material.SetVector("_UVMappingMask", uvMask);
            material.SetFloat("_DoubleSidedEnable", spec.double_sided ? 1 : 0);
            material.SetFloat("_DoubleSidedNormalMode", 1);
            int cull = spec.double_sided ? 0 : 2;
            if (!string.IsNullOrEmpty(spec.cull_mode))
            {
                if (spec.cull_mode != "off" && spec.cull_mode != "front" && spec.cull_mode != "back") throw new InvalidDataException("Unknown cull mode.");
                cull = spec.cull_mode == "off" ? 0 : spec.cull_mode == "front" ? 1 : 2;
            }
            material.SetFloat("_DoubleSidedEnable", cull == 0 ? 1 : 0);
            material.SetFloat("_OpaqueCullMode", cull);
            material.SetFloat("_TransparentCullMode", cull);
            material.SetFloat("_AlphaCutoffEnable", spec.alpha_clip ? 1 : 0);
            material.SetFloat("_AlphaCutoff", Mathf.Clamp01(Scalar(spec.alpha_cutoff, "alpha cutoff")));
            bool transparent = spec.surface == "transparent";
            material.SetFloat("_SurfaceType", transparent ? 1 : 0);
            material.SetFloat("_BlendMode", spec.blend == "additive" ? 1 : spec.blend == "premultiply" ? 4 : 0);
            material.SetFloat("_ZWrite", transparent ? 0 : 1);
            var emission = ColorValue(spec.emission_color, Color.black) * Scalar(spec.emission_intensity, "emission intensity");
            material.SetVector("_EmissiveColor", emission);
            material.SetFloat("_AlbedoAffectEmissive", 0);
            var emissionMap = Texture(spec.emission_map);
            if (emissionMap != null) { material.SetTexture("_EmissiveColorMap", emissionMap); Mapping(material, "_EmissiveColorMap", spec.emission_map); }
            HDMaterial.ValidateMaterial(material);
            material.SetFloat("_CullMode", cull);
            if (material.HasProperty("_CullModeForward")) material.SetFloat("_CullModeForward", cull);
            AssetDatabase.CreateAsset(material, path);
            Cache[cacheKey] = material;
            return material;
        }
    }
}
