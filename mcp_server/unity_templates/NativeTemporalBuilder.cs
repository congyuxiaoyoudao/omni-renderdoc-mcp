#if UNITY_EDITOR
using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Collections.Generic;
using UnityEditor;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;
using UnityEngine.SceneManagement;
namespace OmniRenderDoc
{
    public static class NativeTemporalBuilder
    {
        [Serializable] public sealed class Config
        {
            public int version;
            public string name="Native source temporal AA",generated_assets="Assets/NativeCapture/Generated/Temporal";
            public string dilation_shader,reduction_shader,resolve_shader,seed_shader,copy_shader;
            public string geometry_pass="NativeTemporalGeometry";
            public NativeTemporalHistory.Settings settings;
            public Vector2[] jitter_sequence;
            public bool render_scene_view=true,render_game=true;
            public bool motion_coverage_verified;
            public DrawSpec[] geometry_draws;
            public MaterialOverride[] material_overrides;
            public MeshUvOverride[] mesh_uv_overrides;
        }
        [Serializable] public sealed class DrawSpec{public string key,material_key,pass_name;public int material_index,submesh_index,order;public bool final_only,writes_depth;}
        [Serializable] public sealed class FloatSetting{public string name;public float value;}
        [Serializable] public sealed class VectorSetting{public string name;public Vector4 value;}
        [Serializable] public sealed class TextureSetting{public string name,asset;}
        [Serializable] public sealed class MaterialOverride{public string material_key;public FloatSetting[] floats;public VectorSetting[] vectors;public TextureSetting[] textures;}
        [Serializable] public sealed class MeshUvOverride{public string material_key;public int channel,dimension;public float[] values;}
        public static NativeTemporalProfile Attach(string absoluteConfigPath,bool recordUndo=true)
        {
            var config=JsonUtility.FromJson<Config>(File.ReadAllText(absoluteConfigPath));
            if(config==null||config.version!=1||config.settings==null||config.jitter_sequence==null||config.jitter_sequence.Length==0)throw new InvalidDataException("Expected temporal profile v1 with explicit equations/history/jitter policies.");
            var scene=SceneManager.GetActiveScene();if(!scene.IsValid()||!scene.isLoaded)throw new InvalidOperationException("Open the target scene first.");
            Shader Load(string path){var shader=AssetDatabase.LoadAssetAtPath<Shader>(path);if(!shader||ShaderUtil.ShaderHasError(shader))throw new InvalidDataException("Missing/invalid temporal shader: "+path);return shader;}
            var shaders=new[]{Load(config.dilation_shader),Load(config.reduction_shader),Load(config.resolve_shader),Load(config.seed_shader),Load(config.copy_shader)};
            var roots=scene.GetRootGameObjects();var existing=roots.SelectMany(r=>r.GetComponentsInChildren<NativeTemporalJitter>(true)).ToArray();
            if(existing.Length>1)throw new InvalidOperationException("Ambiguous temporal jitter installation.");
            if(existing.Length==1&&existing[0].profile)return existing[0].profile;
            var draws=new List<NativeTemporalGeometryPass.Draw>();
            var sourceMaterials=new Dictionary<string,Material>();
            var sourceRenderers=new Dictionary<string,Renderer>();
            var allRenderers=roots.SelectMany(r=>r.GetComponentsInChildren<Renderer>(true)).ToArray();
            if(config.geometry_draws!=null&&config.geometry_draws.Length>0)
            {
                foreach(var draw in config.geometry_draws.OrderBy(d=>d.order))
                {
                    var matches=allRenderers.Where(r=>r.name==draw.key).ToArray();if(matches.Length!=1)throw new InvalidDataException("Expected one temporal renderer: "+draw.key);
                    var renderer=matches[0];var materials=renderer.sharedMaterials;
                    if(draw.material_index<0||draw.material_index>=materials.Length||!materials[draw.material_index]||materials[draw.material_index].FindPass(draw.pass_name)<0)
                        throw new InvalidDataException("Missing temporal profile material/pass: "+draw.key+"/"+draw.pass_name);
                    draws.Add(new NativeTemporalGeometryPass.Draw{renderer=renderer,materialIndex=draw.material_index,submeshIndex=draw.submesh_index,passName=draw.pass_name,finalOnly=draw.final_only,writesDepth=draw.writes_depth});
                    if(!string.IsNullOrEmpty(draw.material_key)){sourceMaterials[draw.material_key]=materials[draw.material_index];sourceRenderers[draw.material_key]=renderer;}
                }
            }
            else foreach(var renderer in allRenderers)
            {
                var mesh=renderer is SkinnedMeshRenderer skin?skin.sharedMesh:renderer.GetComponent<MeshFilter>()?.sharedMesh;
                if(!mesh)continue;var materials=renderer.sharedMaterials;
                for(int i=0;i<materials.Length;i++)if(materials[i]&&materials[i].FindPass(config.geometry_pass)>=0)
                    draws.Add(new NativeTemporalGeometryPass.Draw{renderer=renderer,materialIndex=i,submeshIndex=Mathf.Min(i,mesh.subMeshCount-1),passName=config.geometry_pass});
            }
            if(draws.Count==0)throw new InvalidDataException("No current editable geometry provides the profile's source motion/class pass.");
            foreach(var edit in config.material_overrides??Array.Empty<MaterialOverride>())
            {
                if(!sourceMaterials.TryGetValue(edit.material_key,out var material))throw new InvalidDataException("Unknown temporal source material key: "+edit.material_key);
                foreach(var item in edit.floats??Array.Empty<FloatSetting>())if(!material.HasProperty(item.name)||!float.IsFinite(item.value))throw new InvalidDataException("Missing/nonfinite temporal source scalar: "+edit.material_key+"/"+item.name);
                foreach(var item in edit.vectors??Array.Empty<VectorSetting>())if(!material.HasProperty(item.name))throw new InvalidDataException("Missing temporal source vector: "+edit.material_key+"/"+item.name);
                foreach(var item in edit.textures??Array.Empty<TextureSetting>())if(!material.HasProperty(item.name)||!AssetDatabase.LoadAssetAtPath<Texture>(item.asset))throw new InvalidDataException("Missing temporal source material texture: "+edit.material_key+"/"+item.asset);
            }
            var uvMeshes=new Dictionary<MeshUvOverride,Mesh>();
            var uvClaims=new Dictionary<(Mesh,int),MeshUvOverride>();
            foreach(var edit in config.mesh_uv_overrides??Array.Empty<MeshUvOverride>())
            {
                if(!sourceRenderers.TryGetValue(edit.material_key,out var renderer))throw new InvalidDataException("Unknown temporal mesh material key: "+edit.material_key);
                var mesh=renderer is SkinnedMeshRenderer skin?skin.sharedMesh:renderer.GetComponent<MeshFilter>()?.sharedMesh;
                if(!mesh||!mesh.isReadable||edit.channel<0||edit.channel>7||edit.dimension<2||edit.dimension>4||edit.values==null||edit.values.Length!=mesh.vertexCount*edit.dimension||edit.values.Any(v=>!float.IsFinite(v)))
                    throw new InvalidDataException("Invalid ordinary mesh UV recovery: "+edit.material_key);
                if(uvClaims.TryGetValue((mesh,edit.channel),out var claimed)&&(claimed.dimension!=edit.dimension||!claimed.values.SequenceEqual(edit.values)))
                    throw new InvalidDataException("Conflicting source UV recoveries for the same mesh channel: "+edit.material_key+"/"+edit.channel);
                uvClaims[(mesh,edit.channel)]=edit;
                var present=new List<Vector4>();mesh.GetUVs(edit.channel,present);
                if(present.Count!=0)
                {
                    bool identical=present.Count==mesh.vertexCount&&mesh.GetVertexAttributeDimension((VertexAttribute)((int)VertexAttribute.TexCoord0+edit.channel))==edit.dimension;
                    for(int i=0;i<present.Count&&identical;i++)for(int c=0;c<edit.dimension;c++)if(present[i][c]!=edit.values[i*edit.dimension+c])identical=false;
                    if(!identical)throw new InvalidDataException("Temporal UV recovery would overwrite a different existing channel: "+edit.material_key+"/"+edit.channel);
                }
                uvMeshes.Add(edit,mesh);
            }
            // Fill exported source fields once. Runtime never overwrites later
            // user edits to these ordinary material properties.
            foreach(var edit in config.material_overrides??Array.Empty<MaterialOverride>())
            {
                var material=sourceMaterials[edit.material_key];if(recordUndo)Undo.RecordObject(material,"Restore source temporal material properties");
                foreach(var item in edit.floats??Array.Empty<FloatSetting>())material.SetFloat(item.name,item.value);
                foreach(var item in edit.vectors??Array.Empty<VectorSetting>())material.SetVector(item.name,item.value);
                foreach(var item in edit.textures??Array.Empty<TextureSetting>())material.SetTexture(item.name,AssetDatabase.LoadAssetAtPath<Texture>(item.asset));
                EditorUtility.SetDirty(material);
            }
            foreach(var pair in uvMeshes)
            {
                var edit=pair.Key;var mesh=pair.Value;if(recordUndo)Undo.RecordObject(mesh,"Restore editable source material UVs");
                if(edit.dimension==2){var uv=new List<Vector2>(mesh.vertexCount);for(int i=0;i<mesh.vertexCount;i++)uv.Add(new Vector2(edit.values[i*2],edit.values[i*2+1]));mesh.SetUVs(edit.channel,uv);}
                else if(edit.dimension==3){var uv=new List<Vector3>(mesh.vertexCount);for(int i=0;i<mesh.vertexCount;i++)uv.Add(new Vector3(edit.values[i*3],edit.values[i*3+1],edit.values[i*3+2]));mesh.SetUVs(edit.channel,uv);}
                else {var uv=new List<Vector4>(mesh.vertexCount);for(int i=0;i<mesh.vertexCount;i++)uv.Add(new Vector4(edit.values[i*4],edit.values[i*4+1],edit.values[i*4+2],edit.values[i*4+3]));mesh.SetUVs(edit.channel,uv);}
                EditorUtility.SetDirty(mesh);
            }
            Directory.CreateDirectory(config.generated_assets);AssetDatabase.Refresh();
            string assetPath=config.generated_assets+"/TemporalProfile.asset";
            var profile=AssetDatabase.LoadAssetAtPath<NativeTemporalProfile>(assetPath);
            if(!profile){profile=ScriptableObject.CreateInstance<NativeTemporalProfile>();AssetDatabase.CreateAsset(profile,assetPath);}
            profile.dilation=shaders[0];profile.reduction=shaders[1];profile.resolve=shaders[2];profile.seed=shaders[3];profile.copy=shaders[4];profile.settings=config.settings;profile.jitterSequence=config.jitter_sequence;
            profile.renderGame=config.render_game;profile.renderSceneView=config.render_scene_view;profile.motionCoverageVerified=config.motion_coverage_verified;EditorUtility.SetDirty(profile);
            var gameObject=new GameObject(config.name);if(recordUndo)Undo.RegisterCreatedObjectUndo(gameObject,"Install native temporal AA");
            var jitter=gameObject.AddComponent<NativeTemporalJitter>();jitter.profile=profile;
            var early=gameObject.AddComponent<CustomPassVolume>();early.isGlobal=true;early.priority=10000;early.injectionPoint=CustomPassInjectionPoint.AfterOpaqueDepthAndNormal;
            early.customPasses.Add(new NativeTemporalGeometryPass{name="Current source velocity/class for AO",profile=profile,draws=draws.ToArray()});
            var lateObject=new GameObject("Current source velocity/class for temporal resolve");lateObject.transform.SetParent(gameObject.transform,false);
            var late=lateObject.AddComponent<CustomPassVolume>();late.isGlobal=true;late.priority=10000;late.injectionPoint=CustomPassInjectionPoint.BeforePostProcess;
            late.customPasses.Add(new NativeTemporalGeometryPass{name="Current final source velocity/class",profile=profile,draws=draws.ToArray(),finalGeometry=true});
            // Dependencies in the same injection point are explicitly ordered.
            foreach(var volume in roots.SelectMany(r=>r.GetComponentsInChildren<CustomPassVolume>(true)))
            {
                if(volume.injectionPoint!=CustomPassInjectionPoint.AfterOpaqueDepthAndNormal)continue;
                if(volume.customPasses.Any(p=>p!=null&&p.GetType().Name=="NativeSurfaceDepthPass"))volume.priority=11000;
                if(volume.customPasses.Any(p=>p!=null&&p.GetType().Name=="NativeScreenAoPass"))volume.priority=9000;
                EditorUtility.SetDirty(volume);
            }
            var postObject=new GameObject("Original temporal resolve");postObject.transform.SetParent(gameObject.transform,false);
            var postVolume=postObject.AddComponent<Volume>();postVolume.isGlobal=true;postVolume.priority=10000;
            string volumePath=config.generated_assets+"/TemporalVolume.asset";var postProfile=AssetDatabase.LoadAssetAtPath<VolumeProfile>(volumePath);
            if(!postProfile){postProfile=ScriptableObject.CreateInstance<VolumeProfile>();AssetDatabase.CreateAsset(postProfile,volumePath);}
            if(!postProfile.TryGet<NativeTemporalPostProcess>(out var post)){post=postProfile.Add<NativeTemporalPostProcess>(true);AssetDatabase.AddObjectToAsset(post,postProfile);}
            post.profile.Override(profile);post.enabledEffect.Override(true);postVolume.sharedProfile=postProfile;EditorUtility.SetDirty(postProfile);EditorUtility.SetDirty(post);
            jitter.sourceVolume=postVolume;
            Register();
            foreach(var camera in roots.SelectMany(r=>r.GetComponentsInChildren<Camera>(true)))
            {
                var hd=camera.GetComponent<HDAdditionalCameraData>();if(hd&&hd.antialiasing==HDAdditionalCameraData.AntialiasingMode.TemporalAntialiasing){hd.antialiasing=HDAdditionalCameraData.AntialiasingMode.None;EditorUtility.SetDirty(hd);}
            }
            AssetDatabase.SaveAssets();return profile;
        }
        static void Register()
        {
            var assembly=typeof(HDRenderPipelineAsset).Assembly;var type=assembly.GetType("UnityEngine.Rendering.HighDefinition.CustomPostProcessOrdersSettings");
            var getter=typeof(GraphicsSettings).GetMethods(BindingFlags.Public|BindingFlags.Static).First(x=>x.Name=="GetRenderPipelineSettings"&&x.IsGenericMethodDefinition&&x.GetParameters().Length==0);
            var settings=getter.MakeGenericMethod(type).Invoke(null,null);var list=type.GetProperty("beforePostProcessCustomPostProcesses").GetValue(settings);
            bool changed=(bool)list.GetType().GetMethod("Add",new[]{typeof(string)}).Invoke(list,new object[]{typeof(NativeTemporalPostProcess).AssemblyQualifiedName});
            if(!changed)return;
            var globalType=assembly.GetType("UnityEngine.Rendering.HighDefinition.HDRenderPipelineGlobalSettings");var global=globalType.GetMethod("Ensure",BindingFlags.Static|BindingFlags.Public|BindingFlags.NonPublic).Invoke(null,new object[]{true})as UnityEngine.Object;if(global)EditorUtility.SetDirty(global);
        }
    }
}
#endif
