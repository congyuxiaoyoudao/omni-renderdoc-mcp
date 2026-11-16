#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;
using UnityEngine.SceneManagement;

namespace OmniRenderDoc
{
// Import-time attachment only. References are resolved from the supplied export
// profile; runtime draws use ordinary editable meshes and current transforms.
public static class NativeSurfaceModifierBuilder
{
    [Serializable] public sealed class Config
    {
        public int version;
        public string name = "Native surface modifiers";
        public string resolve_shader, resolve_pass = "ResolveReceiver";
        public string auxiliary_resolve_pass;
        public string generated_assets = "Assets/NativeCapture/Generated/SurfaceModifiers";
        public Draw[] classifiers, modifiers;
    }
    [Serializable] public sealed class Draw
    {
        public string key, pass_name, mesh_file;
        public int material_index, submesh_index;
        public NativeMaterialFactory.MaterialSpec material;
        public FloatSetting[] float_properties;
        public TextureSetting[] texture_properties;
        public PropertyCopy[] copy_texture_properties, copy_float_properties;
    }
    [Serializable] public sealed class FloatSetting { public string name; public float value; }
    [Serializable] public sealed class TextureSetting { public string name, asset; }
    [Serializable] public sealed class PropertyCopy { public string source, target; public int source_material_index = -1; }

    public static NativeSurfaceModifierPass Attach(string absoluteConfigPath, bool recordUndo = true)
    {
        var scene = SceneManager.GetActiveScene();
        if (!scene.IsValid() || !scene.isLoaded) throw new InvalidOperationException("Open the intended scene first.");
        var config = JsonUtility.FromJson<Config>(File.ReadAllText(absoluteConfigPath));
        if (config == null || config.version != 1 || string.IsNullOrWhiteSpace(config.name) ||
            config.classifiers == null || config.classifiers.Length == 0 || config.modifiers == null || config.modifiers.Length == 0)
            throw new InvalidDataException("Expected a nonempty surface-modifier profile version 1.");
        var roots = scene.GetRootGameObjects();
        var installed = roots.SelectMany(x => x.GetComponentsInChildren<CustomPassVolume>(true))
            .SelectMany(x => x.customPasses).OfType<NativeSurfaceModifierPass>().Where(x => x.name == config.name).ToArray();
        if (installed.Length > 1) throw new InvalidDataException("Ambiguous surface-modifier installation.");
        if (installed.Length == 1) return installed[0]; // Preserve edits to an existing configuration.
        var renderers = roots.SelectMany(x => x.GetComponentsInChildren<Renderer>(true)).ToArray();
        Renderer Resolve(Draw draw)
        {
            if (draw == null || string.IsNullOrWhiteSpace(draw.key)) throw new InvalidDataException("Missing surface draw key.");
            var matches = renderers.Where(x => x.name == draw.key).ToArray();
            if (matches.Length != 1) throw new InvalidDataException("Expected one existing renderer: " + draw.key);
            var renderer = matches[0];
            if (draw.material_index < 0 || draw.material_index >= renderer.sharedMaterials.Length || !renderer.sharedMaterials[draw.material_index] || string.IsNullOrWhiteSpace(draw.pass_name))
                throw new InvalidDataException("Invalid material/pass for " + draw.key);
            var mesh = renderer is SkinnedMeshRenderer skinned ? skinned.sharedMesh : renderer.GetComponent<MeshFilter>()?.sharedMesh;
            if (!(renderer is MeshRenderer || renderer is SkinnedMeshRenderer) || !mesh || draw.submesh_index < 0 || draw.submesh_index >= mesh.subMeshCount)
                throw new InvalidDataException("Invalid renderer/mesh/submesh for " + draw.key);
            return renderer;
        }
        // Validate all scene references and mesh data before changing any renderer.
        var classifierRenderers = config.classifiers.Select(Resolve).ToArray();
        var modifierRenderers = config.modifiers.Select(Resolve).ToArray();
        for (int i = 0; i < classifierRenderers.Length; i++)
        {
            var material = classifierRenderers[i].sharedMaterials[config.classifiers[i].material_index];
            if (!material || material.FindPass(config.classifiers[i].pass_name) < 0)
                throw new InvalidDataException("Missing classification pass: " + config.classifiers[i].key + " / " + config.classifiers[i].pass_name);
        }
        var resolveShader = AssetDatabase.LoadAssetAtPath<Shader>(config.resolve_shader);
        if (!resolveShader) throw new InvalidDataException("Missing surface-modifier resolve shader.");
        if (ShaderUtil.ShaderHasError(resolveShader) || string.IsNullOrWhiteSpace(config.resolve_pass)) throw new InvalidDataException("Invalid resolve shader/pass.");
        using (var probe = new MaterialProbe(resolveShader)) if (probe.material.FindPass(config.resolve_pass) < 0) throw new InvalidDataException("Missing resolve pass.");
        if (!string.IsNullOrWhiteSpace(config.auxiliary_resolve_pass))
            using (var probe = new MaterialProbe(resolveShader)) if (probe.material.FindPass(config.auxiliary_resolve_pass) < 0) throw new InvalidDataException("Missing auxiliary resolve pass.");
        var meshes = new NativeCaptureSceneBuilder.MeshSpec[config.modifiers.Length];
        for (int i = 0; i < meshes.Length; i++)
        {
            var item = config.modifiers[i];
            if (item.material?.explicit_shader == null) throw new InvalidDataException("A modifier needs an explicit source-derived shader.");
            var shader = AssetDatabase.LoadAssetAtPath<Shader>(item.material.explicit_shader.asset);
            if (!shader || ShaderUtil.ShaderHasError(shader)) throw new InvalidDataException("Missing or invalid modifier shader: " + item.material.explicit_shader.asset);
            using (var probe = new MaterialProbe(shader)) if (probe.material.FindPass(item.pass_name) < 0) throw new InvalidDataException("Missing modifier pass: " + item.pass_name);
            if (string.IsNullOrEmpty(item.mesh_file)) continue;
            meshes[i] = JsonUtility.FromJson<NativeCaptureSceneBuilder.MeshSpec>(File.ReadAllText(NativeCaptureSceneBuilder.ProjectFile(item.mesh_file)));
            var filter = modifierRenderers[i].GetComponent<MeshFilter>();
            if (!filter || !filter.sharedMesh || !filter.sharedMesh.isReadable || meshes[i]?.vertices == null || meshes[i].vertices.Length % 3 != 0 || filter.sharedMesh.vertexCount != meshes[i].vertices.Length / 3)
                throw new InvalidDataException("Modifier UV recovery needs the exported vertex ordering: " + item.key);
            var seen = new HashSet<int>(); bool missingUV = false;
            foreach (var channel in meshes[i].uv_channels ?? Array.Empty<NativeCaptureSceneBuilder.UVChannel>())
            {
                if (channel == null || channel.channel < 0 || channel.channel > 7 || !seen.Add(channel.channel) || channel.dimension != 2 || channel.values?.Length != filter.sharedMesh.vertexCount * 2 || channel.values.Any(v => !float.IsFinite(v)))
                    throw new InvalidDataException("Invalid modifier UV channel: " + item.key);
                var current = new List<Vector2>(); filter.sharedMesh.GetUVs(channel.channel, current); missingUV |= current.Count == 0;
            }
            if (missingUV)
            {
                var current = filter.sharedMesh.vertices;
                for (int vertex = 0; vertex < current.Length; vertex++)
                {
                    var source = new Vector3(meshes[i].vertices[vertex * 3], meshes[i].vertices[vertex * 3 + 1], meshes[i].vertices[vertex * 3 + 2]);
                    if (!float.IsFinite(source.x) || !float.IsFinite(source.y) || !float.IsFinite(source.z) || (current[vertex] - source).sqrMagnitude > 1e-10f)
                        throw new InvalidDataException("Cannot recover UVs after vertex edits/reordering without an explicit correspondence: " + item.key);
                }
                if (meshes[i].triangles == null || !filter.sharedMesh.triangles.SequenceEqual(meshes[i].triangles))
                    throw new InvalidDataException("Cannot recover UVs after topology edits: " + item.key);
            }
        }
        ValidateSlotChanges(config, classifierRenderers, modifierRenderers);
        // Resolve every property source and texture before changing scene data.
        var classifiers = config.classifiers.Select((draw, i) => Bind(classifierRenderers[i], draw)).ToList();
        var modifiers = config.modifiers.Select((draw, i) => Bind(modifierRenderers[i], draw)).ToList();
        NativeCaptureSceneBuilder.ProjectFile(config.generated_assets);
        if (!config.generated_assets.StartsWith("Assets/", StringComparison.Ordinal)) throw new InvalidDataException("Generated assets must be in Assets.");
        Directory.CreateDirectory(NativeCaptureSceneBuilder.ProjectFile(config.generated_assets));
        AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
        var resolvePath = config.generated_assets + "/ReceiverResolve_" + Hash128.Compute(config.name + "\n" + config.resolve_shader + "\n" + config.resolve_pass) + ".mat";
        var savedResolve = AssetDatabase.LoadAssetAtPath<Material>(resolvePath);
        if (AssetDatabase.LoadMainAssetAtPath(resolvePath) && !savedResolve) throw new InvalidDataException("Resolve asset path is occupied by a different asset type.");
        var resolve = savedResolve;
        if (resolve && (resolve.shader != resolveShader || resolve.FindPass(config.resolve_pass) < 0)) throw new InvalidDataException("Existing resolve material is incompatible; authored asset was preserved.");
        if (!resolve) { resolve = new Material(resolveShader) { name = config.name + " receiver resolve" }; AssetDatabase.CreateAsset(resolve, resolvePath); }
        var preparedMaterials = config.modifiers.Select(item => NativeMaterialFactory.Create(item.material, config.generated_assets, item.key)).ToArray();
        for (int i = 0; i < preparedMaterials.Length; i++) if (preparedMaterials[i].FindPass(config.modifiers[i].pass_name) < 0) throw new InvalidDataException("Existing modifier material has no required pass; authored asset was preserved.");
        for (int i = 0; i < config.modifiers.Length; i++)
        {
            var item = config.modifiers[i]; var renderer = modifierRenderers[i];
            if (recordUndo) Undo.RecordObject(renderer, "Restore editable surface modifier");
            var material = preparedMaterials[i];
            var materials = renderer.sharedMaterials; materials[item.material_index] = material; renderer.sharedMaterials = materials;
            if (meshes[i] != null)
            {
                var filter = renderer.GetComponent<MeshFilter>();
                var mesh = filter.sharedMesh;
                bool cloned = false;
                foreach (var channel in meshes[i].uv_channels ?? Array.Empty<NativeCaptureSceneBuilder.UVChannel>())
                {
                    var existing = new List<Vector2>(); mesh.GetUVs(channel.channel, existing);
                    if (existing.Count != 0) continue; // Keep any authored edits on reattachment.
                    if (!cloned)
                    {
                        if (recordUndo) Undo.RecordObject(filter, "Restore source modifier UV");
                        mesh = UnityEngine.Object.Instantiate(mesh); mesh.name += " recovered modifier UV";
                        AssetDatabase.CreateAsset(mesh, AssetDatabase.GenerateUniqueAssetPath(config.generated_assets + "/ModifierUV_" + Hash128.Compute(config.name + "\n" + item.key) + ".asset"));
                        filter.sharedMesh = mesh; EditorUtility.SetDirty(filter); cloned = true;
                    }
                    for (int vertex = 0; vertex < mesh.vertexCount; vertex++) existing.Add(new Vector2(channel.values[vertex * 2], channel.values[vertex * 2 + 1]));
                    mesh.SetUVs(channel.channel, existing); EditorUtility.SetDirty(mesh);
                }
            }
            renderer.enabled = true;
            renderer.shadowCastingMode = ShadowCastingMode.Off;
            EditorUtility.SetDirty(renderer);
        }
        var go = new GameObject(config.name);
        SceneManager.MoveGameObjectToScene(go, scene);
        if (recordUndo) Undo.RegisterCreatedObjectUndo(go, "Restore surface modifier");
        var volume = go.AddComponent<CustomPassVolume>();
        volume.isGlobal = true; volume.injectionPoint = CustomPassInjectionPoint.BeforeTransparent;
        var pass = new NativeSurfaceModifierPass { name = config.name, classifiers = classifiers.ToArray(), modifiers = modifiers.ToArray(),
            resolveMaterial = resolve, resolvePassName = config.resolve_pass, auxiliaryResolvePassName = config.auxiliary_resolve_pass,
            targetColorBuffer = CustomPass.TargetBuffer.None, targetDepthBuffer = CustomPass.TargetBuffer.None };
        volume.customPasses.Add(pass);
        EditorUtility.SetDirty(volume); EditorSceneManager.MarkSceneDirty(scene); SceneView.RepaintAll();
        return pass;
    }
    sealed class MaterialProbe : IDisposable
    {
        public readonly Material material;
        public MaterialProbe(Shader shader) { material = new Material(shader) { hideFlags = HideFlags.HideAndDontSave }; }
        public void Dispose() { UnityEngine.Object.DestroyImmediate(material); }
    }
    static string MaterialSignature(NativeMaterialFactory.MaterialSpec material)
    {
        var canonical = JsonUtility.FromJson<NativeMaterialFactory.MaterialSpec>(JsonUtility.ToJson(material));
        canonical.name = null; canonical.provenance = null; canonical.source_shader_sha256 = null;
        return JsonUtility.ToJson(canonical);
    }
    static void ValidateSlotChanges(Config config, Renderer[] classifierRenderers, Renderer[] modifierRenderers)
    {
        var replacements = new Dictionary<(Renderer renderer, int slot), Draw>();
        var meshSources = new Dictionary<Renderer, string>();
        for (int i = 0; i < config.modifiers.Length; i++)
        {
            var draw = config.modifiers[i]; var renderer = modifierRenderers[i];
            var slot = (renderer, draw.material_index);
            if (replacements.TryGetValue(slot, out var previous) && MaterialSignature(previous.material) != MaterialSignature(draw.material))
                throw new InvalidDataException("Conflicting modifier materials target the same renderer slot: " + draw.key + "/" + draw.material_index);
            replacements[slot] = draw;
            if (!string.IsNullOrEmpty(draw.mesh_file))
            {
                string path = NativeCaptureSceneBuilder.ProjectFile(draw.mesh_file);
                if (meshSources.TryGetValue(renderer, out var priorPath) && priorPath != path)
                    throw new InvalidDataException("Conflicting modifier UV sources target the same renderer: " + draw.key);
                meshSources[renderer] = path;
            }
        }
        for (int i = 0; i < config.classifiers.Length; i++)
        {
            var draw = config.classifiers[i]; var renderer = classifierRenderers[i];
            if (!replacements.TryGetValue((renderer, draw.material_index), out var replacement)) continue;
            var nextShader = AssetDatabase.LoadAssetAtPath<Shader>(replacement.material.explicit_shader.asset);
            if (renderer.sharedMaterials[draw.material_index].shader != nextShader)
                throw new InvalidDataException("A modifier would overwrite a classifier's source shader: " + draw.key + "/" + draw.material_index);
        }
    }
    static void PropertyName(string name)
    {
        if (string.IsNullOrWhiteSpace(name) || name == "_NativeSurfaceClassificationTexture" || name == "_NativeSurfaceReceiverMask" ||
            name == "_NativeSurfaceMaskSize" || name == "_NativeSurfaceMaskValid" || name == "_NativeSurfacePrivateDepthTexture" ||
            name == "_NativeSurfaceCameraDepthTexture" || name == "_NativeSurfaceCameraDepthSize" ||
            name == "_NativeSurfaceAuxiliaryMask" || name == "_NativeSurfaceAuxiliaryValid" || name == "_NativeSurfaceAuxiliarySize")
            throw new InvalidDataException("Invalid/reserved surface property: " + name);
    }
    static NativeSurfaceModifierPass.RendererDraw Bind(Renderer renderer, Draw draw)
    {
        foreach (var value in draw.float_properties ?? Array.Empty<FloatSetting>())
        { if (value == null || !float.IsFinite(value.value)) throw new InvalidDataException("Invalid float override."); PropertyName(value.name); }
        foreach (var value in draw.texture_properties ?? Array.Empty<TextureSetting>())
        { if (value == null) throw new InvalidDataException("Null texture override."); PropertyName(value.name); }
        var copies = new List<NativeSurfaceModifierPass.PropertyCopy>();
        void Copy(PropertyCopy[] values, NativeSurfaceModifierPass.PropertyKind kind)
        {
            foreach (var value in values ?? Array.Empty<PropertyCopy>())
            {
                if (value == null) throw new InvalidDataException("Null copied property."); PropertyName(value.source); PropertyName(value.target);
                int slot = value.source_material_index < 0 ? draw.material_index : value.source_material_index;
                var materials = renderer.sharedMaterials;
                if (value.source_material_index < -1 || slot < 0 || slot >= materials.Length || !materials[slot]) throw new InvalidDataException("Invalid copied-property source slot for " + draw.key);
                var block = new MaterialPropertyBlock(); renderer.GetPropertyBlock(block, slot); if (block.isEmpty) renderer.GetPropertyBlock(block);
                if (!block.HasProperty(value.source) && !materials[slot].HasProperty(value.source)) throw new InvalidDataException("Missing copied-property source: " + draw.key + "/" + value.source);
                copies.Add(new NativeSurfaceModifierPass.PropertyCopy { source = value.source, target = value.target, sourceMaterialIndex = value.source_material_index, kind = kind });
            }
        }
        Copy(draw.copy_texture_properties, NativeSurfaceModifierPass.PropertyKind.Texture); Copy(draw.copy_float_properties, NativeSurfaceModifierPass.PropertyKind.Float);
        return new NativeSurfaceModifierPass.RendererDraw {
        renderer = renderer, materialIndex = draw.material_index, submeshIndex = draw.submesh_index, passName = draw.pass_name,
        copyProperties = copies.ToArray(),
        floatProperties = (draw.float_properties ?? Array.Empty<FloatSetting>()).Select(x => new NativeSurfaceModifierPass.FloatProperty { name = x.name, value = x.value }).ToArray(),
        textureProperties = (draw.texture_properties ?? Array.Empty<TextureSetting>()).Select(x => new NativeSurfaceModifierPass.TextureProperty
        { name = x.name, value = AssetDatabase.LoadAssetAtPath<Texture>(x.asset) ?? throw new InvalidDataException("Missing classifier texture: " + x.asset) }).ToArray()
        };
    }
}
}
#endif
