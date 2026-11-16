#if UNITY_EDITOR
using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.SceneManagement;

namespace OmniRenderDoc
{
    public static class NativeSourceBloomBuilder
    {
        [Serializable] public sealed class Config
        {
            public int version;
            public string name, profile_asset, provenance;
            public string copy_program, prefilter_program, downsample_program, upsample_program;
            public bool enabled = true;
            public int pyramid_levels;
            public float inverse_preexposure = 1, input_scale = 1;
            public float[] threshold, scatter, composite_parameters, tint;
        }

        public static NativeSourceBloomProfile CreateOrUpdateProfile(string absoluteConfigPath, bool recordUndo = true)
        {
            var config = JsonUtility.FromJson<Config>(File.ReadAllText(absoluteConfigPath));
            if (config == null || config.version != 1 || string.IsNullOrWhiteSpace(config.name))
                throw new InvalidDataException("Expected source Bloom profile version 1 with a name.");
            string assetPath = AssetPath(config.profile_asset);
            var candidate = ScriptableObject.CreateInstance<NativeSourceBloomProfile>();
            try
            {
                candidate.name = config.name;
                candidate.enabledEffect = config.enabled;
                candidate.copy = Program(config.copy_program);
                candidate.prefilter = Program(config.prefilter_program);
                candidate.downsample = Program(config.downsample_program);
                candidate.upsample = Program(config.upsample_program);
                candidate.pyramidLevels = config.pyramid_levels;
                candidate.inversePreExposure = config.inverse_preexposure;
                candidate.inputScale = config.input_scale;
                candidate.threshold = Vector(config.threshold, 4, "threshold");
                candidate.scatter = Vector(config.scatter, 4, "scatter");
                candidate.compositeParameters = Vector(config.composite_parameters, 4, "composite_parameters");
                candidate.tint = Vector(config.tint, 3, "tint");
                candidate.provenance = config.provenance;
                candidate.Validate();
                var existing = AssetDatabase.LoadMainAssetAtPath(assetPath);
                if (existing && !(existing is NativeSourceBloomProfile))
                    throw new InvalidDataException("Source Bloom profile path is occupied by another asset type.");
                if (existing is NativeSourceBloomProfile saved)
                {
                    if (recordUndo) Undo.RecordObject(saved, "Configure source Bloom profile");
                    EditorUtility.CopySerialized(candidate, saved);
                    EditorUtility.SetDirty(saved);
                    return saved;
                }
                Directory.CreateDirectory(Path.GetDirectoryName(NativeCaptureSceneBuilder.ProjectFile(assetPath)));
                AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
                AssetDatabase.CreateAsset(candidate, assetPath);
                var created = candidate;
                candidate = null;
                return created;
            }
            finally { if (candidate) UnityEngine.Object.DestroyImmediate(candidate); }
        }

        public static NativeMaterialPostProcess Configure(NativeMaterialPostProcess post,
            string absoluteConfigPath, bool recordUndo = true)
        {
            if (!post || !NativeSourceBloomProfile.SupportsMaterial(post.material.value))
                throw new InvalidDataException("Configure source Bloom on a native post-process material with the Bloom shader contract.");
            return Configure(post, CreateOrUpdateProfile(absoluteConfigPath, recordUndo), recordUndo);
        }

        public static NativeMaterialPostProcess Configure(NativeMaterialPostProcess post,
            NativeSourceBloomProfile profile, bool recordUndo = true)
        {
            if (!post || !profile || !NativeSourceBloomProfile.SupportsMaterial(post.material.value))
                throw new InvalidDataException("Source Bloom needs a compatible post-process component and profile.");
            profile.Validate();
            if (recordUndo) Undo.RecordObject(post, "Attach source Bloom profile");
            post.sourceBloom.Override(profile);
            EditorUtility.SetDirty(post);
            return post;
        }

        public static NativeMaterialPostProcess Attach(string absoluteConfigPath, bool recordUndo = true)
        {
            var scene = SceneManager.GetActiveScene();
            if (!scene.IsValid() || !scene.isLoaded) throw new InvalidOperationException("Open the intended scene first.");
            var posts = scene.GetRootGameObjects().SelectMany(root => root.GetComponentsInChildren<Volume>(true))
                .Where(volume => volume.sharedProfile != null)
                .SelectMany(volume => volume.sharedProfile.components).OfType<NativeMaterialPostProcess>()
                .Where(post => NativeSourceBloomProfile.SupportsMaterial(post.material.value)).Distinct().ToArray();
            if (posts.Length != 1)
                throw new InvalidDataException("Expected one compatible native post-process component; use Configure with an explicit component when several are present.");
            Configure(posts[0], absoluteConfigPath, recordUndo);
            EditorSceneManager.MarkSceneDirty(scene);
            SceneView.RepaintAll();
            return posts[0];
        }

        static string AssetPath(string path)
        {
            if (string.IsNullOrWhiteSpace(path)) throw new InvalidDataException("Missing source Bloom asset path.");
            path = path.Replace('\\', '/');
            NativeCaptureSceneBuilder.ProjectFile(path);
            if (!path.StartsWith("Assets/", StringComparison.Ordinal)) throw new InvalidDataException("Source Bloom assets must be below Assets/.");
            return path;
        }
        static ComputeShader Program(string path) => AssetDatabase.LoadAssetAtPath<ComputeShader>(AssetPath(path))
            ?? throw new InvalidDataException("Missing source Bloom compute: " + path);
        static Vector4 Vector(float[] values, int count, string label)
        {
            if (values == null || values.Length != count || values.Any(value => !float.IsFinite(value)))
                throw new InvalidDataException("Expected " + count + " finite components for " + label);
            return new Vector4(values[0], values[1], values[2], count == 4 ? values[3] : 0);
        }
    }
}
#endif
