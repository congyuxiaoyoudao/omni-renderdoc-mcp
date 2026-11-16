#if UNITY_EDITOR
using System;
using System.IO;
using System.Linq;
using System.Reflection;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering.HighDefinition;
using UnityEngine.SceneManagement;

namespace OmniRenderDoc
{
    // Explicit installation only. Profiles contain source data; runtime contains no
    // capture/event identifiers. Existing scene edits are never regenerated.
    public static class NativeSunShadowBuilder
    {
        [Serializable] sealed class VectorValue { public string name; public Vector4 value; }
        [Serializable] sealed class MatrixValue { public string name; public float[] value; }
        [Serializable] sealed class VectorArray { public string name; public Vector4[] values; }
        [Serializable] sealed class MatrixArray { public string name; public MatrixValue[] values; }
        [Serializable] sealed class Config
        {
            public int version, cloudWidth, cloudHeight;
            public string cloudData, cloudAsset, cloudFormat, casterProfile;
            public float cloudTime;
            public VectorValue[] vectors;
            public MatrixValue[] matrices;
            public VectorArray[] vectorArrays;
            public MatrixArray[] matrixArrays;
        }
        static Matrix4x4 Matrix(float[] values)
        {
            if(values==null || values.Length!=16 || values.Any(v=>!float.IsFinite(v))) throw new InvalidDataException("Sun matrix requires sixteen finite row-major floats.");
            var result=new Matrix4x4();
            for(int r=0;r<4;r++) for(int c=0;c<4;c++) result[r,c]=values[r*4+c];
            return result;
        }
        static void Assign(NativeSunShadowPass pass,string name,object value)
        {
            var field=typeof(NativeSunShadowPass).GetField(name,BindingFlags.Public|BindingFlags.Instance);
            if(field==null || field.FieldType!=value.GetType()) throw new InvalidDataException("Unsupported source sun profile field: "+name);
            field.SetValue(pass,value);
        }
        static string AssetPath(string path)
        {
            if(string.IsNullOrWhiteSpace(path)) throw new InvalidDataException("Missing source sun asset path.");
            path=path.Replace('\\','/');
            NativeCaptureSceneBuilder.ProjectFile(path);
            if(!path.StartsWith("Assets/",StringComparison.Ordinal)) throw new InvalidDataException("Source sun assets must be below Assets/.");
            return path;
        }
        public static NativeSunShadowPass Attach(string configurationPath,bool recordUndo=true)
        {
            var scene=SceneManager.GetActiveScene();
            if(!scene.IsValid() || !scene.isLoaded) throw new InvalidOperationException("Open the intended editable scene first.");
            var existing=scene.GetRootGameObjects().SelectMany(g=>g.GetComponentsInChildren<CustomPassVolume>(true))
                .SelectMany(v=>v.customPasses).OfType<NativeSunShadowPass>().ToArray();
            if(existing.Length>1) throw new InvalidOperationException("Multiple source sun passes require explicit selection.");
            // Reopening an existing scene preserves user edits. Call Configure
            // explicitly to apply a changed profile to that existing pass.
            if(existing.Length==1) return existing[0];
            var config=JsonUtility.FromJson<Config>(File.ReadAllText(configurationPath));
            if(config==null || config.version!=1 || config.cloudWidth<=0 || config.cloudHeight<=0)
                throw new InvalidDataException("Invalid source sun profile.");
            var pass=new NativeSunShadowPass {name="Native source sun cloud and shadow"};
            ConfigureValues(pass,config);
            var go=new GameObject("Native source sun cloud and shadow");
            SceneManager.MoveGameObjectToScene(go,scene);
            if(recordUndo) Undo.RegisterCreatedObjectUndo(go,"Attach original sun cloud");
            var volume=recordUndo?Undo.AddComponent<CustomPassVolume>(go):go.AddComponent<CustomPassVolume>();
            volume.isGlobal=true;volume.injectionPoint=CustomPassInjectionPoint.AfterOpaqueDepthAndNormal;
            volume.customPasses.Add(pass);
            volume.priority=12000;
            if(!string.IsNullOrEmpty(config.casterProfile))
                NativeSunCasterBuilder.Attach(pass,NativeCaptureSceneBuilder.ProjectFile(AssetPath(config.casterProfile)),false,recordUndo);
            EditorUtility.SetDirty(volume);EditorSceneManager.MarkSceneDirty(scene);SceneView.RepaintAll();
            return pass;
        }
        public static void Configure(NativeSunShadowPass pass,string configurationPath,bool recordUndo=true)
        {
            if(pass==null) throw new ArgumentNullException(nameof(pass));
            var config=JsonUtility.FromJson<Config>(File.ReadAllText(configurationPath));
            var candidate=new NativeSunShadowPass();
            ConfigureValues(candidate,config); // Validate the entire candidate before changing scene state.
            var volume=UnityEngine.Object.FindObjectsByType<CustomPassVolume>(FindObjectsInactive.Include,FindObjectsSortMode.None)
                .SingleOrDefault(v=>v.customPasses.Contains(pass));
            if(recordUndo && volume!=null) Undo.RecordObject(volume,"Configure source sun profile");
            foreach(var item in config.vectors ?? Array.Empty<VectorValue>()) Assign(pass,item.name,item.value);
            foreach(var item in config.matrices ?? Array.Empty<MatrixValue>()) Assign(pass,item.name,Matrix(item.value));
            foreach(var item in config.vectorArrays ?? Array.Empty<VectorArray>()) Assign(pass,item.name,item.values);
            foreach(var item in config.matrixArrays ?? Array.Empty<MatrixArray>()) Assign(pass,item.name,item.values.Select(v=>Matrix(v.value)).ToArray());
            pass.cloudTexture=candidate.cloudTexture;pass.cloudTime=candidate.cloudTime;
            // Explicit updates retain separately authored caster membership and enable flags.
            if(volume!=null) {EditorUtility.SetDirty(volume);EditorSceneManager.MarkSceneDirty(volume.gameObject.scene);}
            SceneView.RepaintAll();
        }
        static void ConfigureValues(NativeSunShadowPass pass,Config config)
        {
            if(config==null || config.version!=1 || config.cloudWidth<=0 || config.cloudHeight<=0 || !float.IsFinite(config.cloudTime))
                throw new InvalidDataException("Invalid source sun profile.");
            if(!Enum.TryParse<TextureFormat>(config.cloudFormat,out var format) || !Enum.IsDefined(typeof(TextureFormat),format))
                throw new InvalidDataException("Source sun profile requires an explicit supported TextureFormat.");
            string dataPath=AssetPath(config.cloudData), assetPath=AssetPath(config.cloudAsset);
            pass.cloudTime=config.cloudTime;
            foreach(var item in config.vectors ?? Array.Empty<VectorValue>()) Assign(pass,item.name,item.value);
            foreach(var item in config.matrices ?? Array.Empty<MatrixValue>()) Assign(pass,item.name,Matrix(item.value));
            foreach(var item in config.vectorArrays ?? Array.Empty<VectorArray>()) Assign(pass,item.name,item.values);
            foreach(var item in config.matrixArrays ?? Array.Empty<MatrixArray>()) Assign(pass,item.name,item.values.Select(v=>Matrix(v.value)).ToArray());
            var texture=AssetDatabase.LoadAssetAtPath<Texture2D>(assetPath);
            if(texture!=null && (texture.width!=config.cloudWidth || texture.height!=config.cloudHeight || texture.format!=format))
                throw new InvalidDataException("Existing source cloud asset has incompatible dimensions or format.");
            if(texture==null)
            {
                var raw=File.ReadAllBytes(NativeCaptureSceneBuilder.ProjectFile(dataPath));
                texture=new Texture2D(config.cloudWidth,config.cloudHeight,format,false,true) {
                    name="Source authored cloud shadow",filterMode=FilterMode.Bilinear,wrapMode=TextureWrapMode.Repeat };
                if(raw.Length!=texture.GetRawTextureData<byte>().Length)
                { UnityEngine.Object.DestroyImmediate(texture);throw new InvalidDataException("Authored cloud texture byte count does not match its declared format."); }
                texture.LoadRawTextureData(raw);texture.Apply(false,true);
                Directory.CreateDirectory(Path.GetDirectoryName(NativeCaptureSceneBuilder.ProjectFile(assetPath)));
                AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
                AssetDatabase.CreateAsset(texture,assetPath);
            }
            pass.cloudTexture=texture;
            // The authored texture works with current world positions immediately.
            // Source atlases stay disabled until current caster coverage is validated.
            pass.enableSourceCascades=false;pass.enableSourceTiles=false;
        }
    }
}
#endif
