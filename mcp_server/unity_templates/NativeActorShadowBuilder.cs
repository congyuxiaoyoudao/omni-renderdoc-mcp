#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using OmniRenderDoc;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering.HighDefinition;
using UnityEngine.SceneManagement;

// Explicit one-shot attachment for the selected scene. No InitializeOnLoad,
// scene opening, saving, object movement or renderer/material replacement.
namespace OmniRenderDoc
{
public static class NativeActorShadowBuilder
{
    [Serializable] sealed class Config { public int version; public ActorConfig[] actors; }
    [Serializable] sealed class ActorConfig
    {
        public string label, anchor_key;
        public float[] reference_anchor_local_to_world, reference_world_to_shadow_uvz;
        public float[] reference_world_to_bias_space, forward, receiver_bias, filter_radius;
        public int width, height;
        public float depth_quantization;
        public float depth_bias;
        public int border_pixels;
        public CasterConfig[] casters;
        public ReceiverConfig[] receivers;
    }
    [Serializable] sealed class CasterConfig { public string key; public int material_index, submesh_index; public bool use_material_coverage; }
    [Serializable] sealed class ReceiverConfig { public string key; public bool all_materials; public int material_index; public float visibility_quantization; }
    const string PassName = "Native independent actor shadow";

    static Matrix4x4 Matrix(float[] values, string name)
    {
        if (values == null || values.Length != 16) throw new InvalidDataException(name + " requires 16 row-major values.");
        var result = new Matrix4x4();
        for (int row = 0; row < 4; row++) for (int col = 0; col < 4; col++) result[row,col] = values[row*4+col];
        if (Mathf.Abs(result.determinant) < 1e-12f) throw new InvalidDataException(name + " is singular.");
        return result;
    }
    static Vector3 Vector3(float[] values)
    {
        if (values == null || values.Length != 3) throw new InvalidDataException("A three-component direction is required.");
        return new Vector3(values[0], values[1], values[2]);
    }
    static Vector2 Vector2(float[] values)
    {
        if (values == null || values.Length != 2) throw new InvalidDataException("A two-component parameter is required.");
        return new Vector2(values[0], values[1]);
    }

    public static NativeActorShadowPass Attach(string absoluteConfigPath, bool recordUndo = true)
    {
        var scene = SceneManager.GetActiveScene();
        if (!scene.IsValid() || !scene.isLoaded) throw new InvalidOperationException("Open the intended scene before attaching the actor shadow.");
        var roots = scene.GetRootGameObjects();
        var volumes = roots.SelectMany(x => x.GetComponentsInChildren<CustomPassVolume>(true)).ToArray();
        var installed = volumes.SelectMany(x => x.customPasses).OfType<NativeActorShadowPass>().Where(x => x.name == PassName).ToArray();
        if (installed.Length > 1) throw new InvalidOperationException("More than one actor-shadow installation exists; refusing to choose one.");
        if (installed.Length == 1) return installed[0]; // Preserve any user edits to an existing configuration.
        var config = JsonUtility.FromJson<Config>(File.ReadAllText(absoluteConfigPath));
        if (config == null || config.version != 1 || config.actors == null || config.actors.Length == 0)
            throw new InvalidDataException("Invalid actor shadow profile.");
        var renderers = roots.SelectMany(x => x.GetComponentsInChildren<Renderer>(true)).ToArray();
        Renderer Resolve(string key)
        {
            var matches = renderers.Where(x => x.name == key).ToArray();
            if (matches.Length == 0)
                matches = renderers.Where(x => x.sharedMaterials.Length > 0 && x.sharedMaterials[0] != null && x.sharedMaterials[0].name == key).ToArray();
            if (matches.Length != 1) throw new InvalidDataException("Expected one existing renderer for " + key + "; found " + matches.Length + ". No scene changes have been made.");
            return matches[0];
        }
        var actors = new List<NativeActorShadowPass.Actor>();
        // Resolve and validate every reference before making a scene change.
        foreach (var source in config.actors)
        {
            if (source.width <= 0 || source.height <= 0) throw new InvalidDataException("Actor map dimensions must be positive.");
            var casters = new List<NativeActorShadowPass.Caster>();
            foreach (var item in source.casters)
            {
                var renderer = Resolve(item.key);
                var materials = renderer.sharedMaterials;
                if (item.material_index < 0 || item.material_index >= materials.Length ||
                    materials[item.material_index] == null || materials[item.material_index].FindPass("NativeActorShadowCaster") < 0)
                    throw new InvalidDataException("Missing live actor caster pass on " + item.key + ".");
                casters.Add(new NativeActorShadowPass.Caster { renderer = renderer, materialIndex = item.material_index, submeshIndex = item.submesh_index, useMaterialCoverage = item.use_material_coverage });
            }
            var receivers = new List<NativeActorShadowPass.Receiver>();
            foreach (var item in source.receivers)
            {
                var renderer = Resolve(item.key);
                int begin = item.all_materials ? 0 : item.material_index;
                int end = item.all_materials ? renderer.sharedMaterials.Length : item.material_index + 1;
                if (begin < 0 || end > renderer.sharedMaterials.Length) throw new InvalidDataException("Invalid receiver material index: " + item.key);
                for (int index = begin; index < end; index++)
                    receivers.Add(new NativeActorShadowPass.Receiver { renderer = renderer, materialIndex = index, visibilityQuantization = item.visibility_quantization });
            }
            actors.Add(new NativeActorShadowPass.Actor {
                label = source.label, anchor = Resolve(source.anchor_key).transform,
                referenceAnchorLocalToWorld = Matrix(source.reference_anchor_local_to_world, "reference anchor"),
                referenceWorldToShadowUVZ = Matrix(source.reference_world_to_shadow_uvz, "reference projection"),
                referenceWorldToBiasSpace = Matrix(source.reference_world_to_bias_space, "bias space"),
                forward = Vector3(source.forward), receiverBias = Vector2(source.receiver_bias), filterRadius = Vector2(source.filter_radius),
                depthQuantization = source.depth_quantization,
                depthBias = source.depth_bias, borderPixels = source.border_pixels,
                width = source.width, height = source.height, casters = casters.ToArray(), receivers = receivers.ToArray()
            });
        }
        var gameObject = new GameObject("Native independent actor shadow");
        SceneManager.MoveGameObjectToScene(gameObject, scene);
        if (recordUndo) Undo.RegisterCreatedObjectUndo(gameObject, "Attach live actor shadow");
        var volume = recordUndo ? Undo.AddComponent<CustomPassVolume>(gameObject) : gameObject.AddComponent<CustomPassVolume>();
        volume.isGlobal = true;
        volume.injectionPoint = CustomPassInjectionPoint.AfterOpaqueDepthAndNormal;
        var pass = new NativeActorShadowPass { name = PassName, actors = actors.ToArray() };
        volume.customPasses.Add(pass);
        EditorUtility.SetDirty(volume);
        EditorSceneManager.MarkSceneDirty(scene);
        SceneView.RepaintAll();
        return pass;
    }
}
}
#endif
