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
    // Generic explicit asset/profile import. Shader programs and source constants
    // are supplied by a verified profile; no source event identifiers are interpreted.
    public static class NativeSunCasterBuilder
    {
        [Serializable] sealed class UV {public int channel;public string data;}
        [Serializable] sealed class Geometry
        {
            public string key,vertices,indices,mesh_asset;
            public int vertex_count,triangle_count;
            public UV[] uvs;
        }
        [Serializable] sealed class TextureValue
        {
            public string key,data,asset,format,filter,wrap;
            public int width,height,mipCount;public bool linear;
        }
        [Serializable] sealed class FloatValue {public string name;public float value;}
        [Serializable] sealed class VectorValue {public string name;public Vector4 value;}
        [Serializable] sealed class TextureBinding {public string name,key;}
        [Serializable] sealed class MaterialValue
        {
            public string key,asset,shader;
            public FloatValue[] floats;public VectorValue[] vectors;public TextureBinding[] textures;
        }
        [Serializable] sealed class RegionValue {public float[] matrix;public RectInt viewport,scissor;}
        [Serializable] sealed class CasterValue
        {
            public string key,material,linkKey;public int cascade,material_index,submesh_index;
            public bool reuse_existing;public float depth_bias,slope_bias;
        }
        [Serializable] sealed class LinkValue
        {
            public string key,source,geometry,data,method;
            public Vector3 templateOrigin;
            public int sourceVertexCount;
        }
        [Serializable] sealed class Config
        {
            public int version,atlasWidth,atlasHeight;
            public Geometry[] geometries;public TextureValue[] textures;
            public MaterialValue[] materials;public RegionValue[] regions;public CasterValue[] casters;
            public LinkValue[] links;
        }
        static string PathInsideAssets(string value)
        {
            if(string.IsNullOrWhiteSpace(value)) throw new InvalidDataException("Missing sun caster asset path.");
            value=value.Replace('\\','/');
            NativeCaptureSceneBuilder.ProjectFile(value);
            if(!value.StartsWith("Assets/",StringComparison.Ordinal)) throw new InvalidDataException("Sun caster assets must be below Assets/.");
            return value;
        }
        static byte[] Read(string path) => File.ReadAllBytes(NativeCaptureSceneBuilder.ProjectFile(PathInsideAssets(path)));
        static void ParentDirectory(string path)
        {Directory.CreateDirectory(Path.GetDirectoryName(NativeCaptureSceneBuilder.ProjectFile(PathInsideAssets(path))));}
        static Matrix4x4 Matrix(float[] values)
        {
            if(values==null || values.Length!=16 || values.Any(v=>!float.IsFinite(v))) throw new InvalidDataException("Caster matrix requires sixteen finite floats.");
            var result=new Matrix4x4();for(int i=0;i<16;i++) result[i/4,i%4]=values[i];return result;
        }
        static float[] Floats(string path,int count)
        {
            var raw=Read(path);if(raw.Length!=checked(count*4)) throw new InvalidDataException("Caster attribute length mismatch: "+path);
            var values=new float[count];Buffer.BlockCopy(raw,0,values,0,raw.Length);
            if(values.Any(v=>!float.IsFinite(v))) throw new InvalidDataException("Nonfinite caster attribute: "+path);
            return values;
        }
        static Mesh LoadMesh(Geometry spec)
        {
            string path=PathInsideAssets(spec.mesh_asset);
            // Reapplying a membership profile preserves an edited existing mesh.
            var mesh=AssetDatabase.LoadAssetAtPath<Mesh>(path);if(mesh!=null) return mesh;
            if(spec.vertex_count<3 || spec.triangle_count<=0) throw new InvalidDataException("Invalid caster topology.");
            var xyz=Floats(spec.vertices,checked(spec.vertex_count*3));var vertices=new Vector3[spec.vertex_count];
            for(int i=0;i<vertices.Length;i++) vertices[i]=new Vector3(xyz[i*3],xyz[i*3+1],xyz[i*3+2]);
            var raw=Read(spec.indices);if(raw.Length!=checked(spec.triangle_count*12)) throw new InvalidDataException("Caster index length mismatch.");
            var indices=new int[checked(spec.triangle_count*3)];Buffer.BlockCopy(raw,0,indices,0,raw.Length);
            if(indices.Any(v=>v<0 || v>=vertices.Length)) throw new InvalidDataException("Caster index outside vertex buffer.");
            mesh=new Mesh {name=spec.key,indexFormat=vertices.Length>65535?IndexFormat.UInt32:IndexFormat.UInt16};
            mesh.vertices=vertices;mesh.triangles=indices;
            var channels=new HashSet<int>();
            foreach(var uv in spec.uvs ?? Array.Empty<UV>())
            {
                if(uv.channel<0 || uv.channel>7 || !channels.Add(uv.channel)) throw new InvalidDataException("Invalid caster UV channel.");
                var xy=Floats(uv.data,checked(vertices.Length*2));var values=new List<Vector2>(vertices.Length);
                for(int i=0;i<vertices.Length;i++) values.Add(new Vector2(xy[i*2],xy[i*2+1]));
                mesh.SetUVs(uv.channel,values);
            }
            mesh.RecalculateNormals();mesh.RecalculateBounds();ParentDirectory(path);AssetDatabase.CreateAsset(mesh,path);return mesh;
        }
        static Texture2D LoadTexture(TextureValue spec)
        {
            string path=PathInsideAssets(spec.asset);
            if(!Enum.TryParse<TextureFormat>(spec.format,out var format) || !Enum.IsDefined(typeof(TextureFormat),format) ||
                !Enum.TryParse<FilterMode>(spec.filter,out var filter) || !Enum.IsDefined(typeof(FilterMode),filter) ||
                !Enum.TryParse<TextureWrapMode>(spec.wrap,out var wrap) || !Enum.IsDefined(typeof(TextureWrapMode),wrap) ||
                spec.width<=0 || spec.height<=0 || spec.mipCount<=0)
                throw new InvalidDataException("Invalid explicit caster texture description.");
            var texture=AssetDatabase.LoadAssetAtPath<Texture2D>(path);
            if(texture!=null)
            {
                if(texture.width!=spec.width || texture.height!=spec.height || texture.format!=format || texture.mipmapCount!=spec.mipCount)
                    throw new InvalidDataException("Existing caster texture has incompatible layout.");
                return texture;
            }
            var raw=Read(spec.data);
            texture=new Texture2D(spec.width,spec.height,format,spec.mipCount,spec.linear) {name=spec.key,filterMode=filter,wrapMode=wrap,anisoLevel=0};
            if(raw.Length!=texture.GetRawTextureData<byte>().Length)
            {UnityEngine.Object.DestroyImmediate(texture);throw new InvalidDataException("Caster texture data differs from declared mip layout.");}
            texture.LoadRawTextureData(raw);texture.Apply(false,true);ParentDirectory(path);AssetDatabase.CreateAsset(texture,path);return texture;
        }
        static Material LoadMaterial(MaterialValue spec,Dictionary<string,Texture2D> textures)
        {
            string path=PathInsideAssets(spec.asset);var material=AssetDatabase.LoadAssetAtPath<Material>(path);
            if(material!=null) return material; // Preserve material edits on explicit membership reapplication.
            var shader=Shader.Find(spec.shader);if(shader==null) throw new InvalidDataException("Required verified caster shader is missing: "+spec.shader);
            material=new Material(shader) {name=spec.key};
            foreach(var item in spec.floats ?? Array.Empty<FloatValue>())
            {if(!float.IsFinite(item.value) || !material.HasProperty(item.name)) throw new InvalidDataException("Invalid caster float property.");material.SetFloat(item.name,item.value);}
            foreach(var item in spec.vectors ?? Array.Empty<VectorValue>())
            {if(!material.HasProperty(item.name)) throw new InvalidDataException("Invalid caster vector property.");material.SetVector(item.name,item.value);}
            foreach(var item in spec.textures ?? Array.Empty<TextureBinding>())
            {if(!material.HasProperty(item.name) || !textures.TryGetValue(item.key,out var texture)) throw new InvalidDataException("Missing caster texture binding.");material.SetTexture(item.name,texture);}
            if(material.FindPass("NativeSunShadowCaster")<0) throw new InvalidDataException("Verified caster shader has no NativeSunShadowCaster pass.");
            ParentDirectory(path);AssetDatabase.CreateAsset(material,path);return material;
        }
        public static void Attach(NativeSunShadowPass pass,string configurationPath,bool enableValidatedAtlas=false,bool recordUndo=true)
        {
            if(pass==null) throw new ArgumentNullException(nameof(pass));
            var scene=SceneManager.GetActiveScene();if(!scene.IsValid() || !scene.isLoaded) throw new InvalidOperationException("Open the target editable scene first.");
            var config=JsonUtility.FromJson<Config>(File.ReadAllText(configurationPath));
            if(config==null || config.version!=1 || config.atlasWidth<=0 || config.atlasHeight<=0 || config.regions==null || config.casters==null)
                throw new InvalidDataException("Invalid explicit source caster profile.");
            var volumes=scene.GetRootGameObjects().SelectMany(g=>g.GetComponentsInChildren<CustomPassVolume>(true));
            var volume=volumes.SingleOrDefault(v=>v.customPasses.Contains(pass));
            if(volume==null) throw new InvalidOperationException("The selected sun pass is not in the active scene.");
            var all=scene.GetRootGameObjects().SelectMany(g=>g.GetComponentsInChildren<Renderer>(true)).ToArray();
            var lookup=new Dictionary<string,List<Renderer>>();
            foreach(var renderer in all)
            {
                var names=new[]{renderer.name}.Concat(renderer.sharedMaterials.Where(m=>m!=null).Select(m=>m.name)).Distinct();
                foreach(var key in names) {if(!lookup.TryGetValue(key,out var list)) lookup[key]=list=new List<Renderer>();list.Add(renderer);}
            }
            // Check links before importing assets or creating objects. Ambiguity is
            // an explicit error; never silently substitute a frozen duplicate.
            foreach(var caster in config.casters.Where(c=>c.reuse_existing))
                if(!lookup.TryGetValue(caster.key,out var values) || values.Distinct().Count()!=1)
                    throw new InvalidDataException("Missing or ambiguous editable source caster: "+caster.key);
            var regions=new NativeSunShadowPass.Region[config.regions.Length];
            for(int i=0;i<regions.Length;i++)
            {
                var source=config.regions[i];var r=source.viewport;
                if(r.x<0 || r.y<0 || r.width<=0 || r.height<=0 || r.xMax>config.atlasWidth || r.yMax>config.atlasHeight)
                    throw new InvalidDataException("Caster viewport is outside atlas.");
                var s=source.scissor;
                if(s.width<0 || s.height<0 || (s.width>0 && (s.x<r.x || s.y<r.y || s.xMax>r.xMax || s.yMax>r.yMax)))
                    throw new InvalidDataException("Caster scissor is outside viewport.");
                regions[i]=new NativeSunShadowPass.Region {sourceWorldToUVZ=Matrix(source.matrix),viewport=r,scissor=s};
            }
            foreach(var c in config.casters)
                if(c.cascade<0 || c.cascade>=regions.Length || !float.IsFinite(c.depth_bias) || !float.IsFinite(c.slope_bias)) throw new InvalidDataException("Invalid caster region/bias.");
            var textures=(config.textures ?? Array.Empty<TextureValue>()).ToDictionary(t=>t.key,LoadTexture);
            var materials=(config.materials ?? Array.Empty<MaterialValue>()).ToDictionary(m=>m.key,m=>LoadMaterial(m,textures));
            var linkSpecs=config.links ?? Array.Empty<LinkValue>();
            var linkedGeometry=new HashSet<string>(linkSpecs.Select(l=>l.geometry));
            GameObject root=scene.GetRootGameObjects().SingleOrDefault(g=>g.name=="Editable source sun casters");
            if(root==null)
            {
                root=new GameObject("Editable source sun casters");SceneManager.MoveGameObjectToScene(root,scene);
                if(recordUndo) Undo.RegisterCreatedObjectUndo(root,"Import editable source sun casters");
            }
            foreach(var geometry in config.geometries ?? Array.Empty<Geometry>())
            {
                if(linkedGeometry.Contains(geometry.key)){LoadMesh(geometry);continue;}
                if(lookup.TryGetValue(geometry.key,out var present))
                {if(present.Count!=1) throw new InvalidDataException("Ambiguous existing caster object: "+geometry.key);continue;}
                var mesh=LoadMesh(geometry);var go=new GameObject(geometry.key);go.transform.SetParent(root.transform,false);
                if(recordUndo) Undo.RegisterCreatedObjectUndo(go,"Import editable source sun caster");
                go.AddComponent<MeshFilter>().sharedMesh=mesh;var renderer=go.AddComponent<MeshRenderer>();
                var caster=config.casters.First(c=>c.key==geometry.key);renderer.sharedMaterial=materials[caster.material];
                renderer.shadowCastingMode=ShadowCastingMode.ShadowsOnly;renderer.receiveShadows=false;
                lookup[geometry.key]=new List<Renderer>{renderer};
            }
            var links=new Dictionary<string,NativeSunCasterLink>();
            foreach(var spec in linkSpecs)
            {
                var renderer=lookup[spec.source].Single();var source=renderer.GetComponent<MeshFilter>();
                if(source==null || source.sharedMesh==null || source.sharedMesh.vertexCount!=spec.sourceVertexCount)
                    throw new InvalidDataException("Shadow LOD source mesh topology differs: "+spec.source);
                var component=renderer.GetComponents<NativeSunCasterLink>().SingleOrDefault(c=>c.bindingKey==spec.key);
                if(component==null) component=recordUndo?Undo.AddComponent<NativeSunCasterLink>(renderer.gameObject):renderer.gameObject.AddComponent<NativeSunCasterLink>();
                else if(recordUndo)Undo.RecordObject(component,"Configure source shadow LOD binding");
                var geometry=config.geometries.Single(g=>g.key==spec.geometry);
                component.bindingKey=spec.key;component.source=source;component.shadowTopology=LoadMesh(geometry);
                component.vertexBindings=AssetDatabase.LoadAssetAtPath<TextAsset>(PathInsideAssets(spec.data));
                if(component.vertexBindings==null)throw new InvalidDataException("Missing shadow LOD vertex binding: "+spec.data);
                component.templateOrigin=spec.templateOrigin;component.sourceVertexCount=spec.sourceVertexCount;component.bindingMethod=spec.method;
                EditorUtility.SetDirty(component);links[spec.key]=component;
                // Preserve any previous imported object's editable Mesh asset, but
                // stop drawing its old independent renderer after installing a link.
                if(lookup.TryGetValue(spec.geometry,out var old))foreach(var duplicate in old)
                    if(duplicate!=renderer && duplicate.transform.parent==root.transform){if(recordUndo)Undo.RecordObject(duplicate,"Replace independent shadow LOD");duplicate.enabled=false;}
            }
            for(int i=0;i<regions.Length;i++) regions[i].casters=config.casters.Where(c=>c.cascade==i).Select(c=>
            {
                var renderer=lookup[c.key].Single();var filter=renderer.GetComponent<MeshFilter>();
                if(filter==null || filter.sharedMesh==null || c.submesh_index<0 || c.submesh_index>=filter.sharedMesh.subMeshCount)
                    throw new InvalidDataException("Caster requires current editable mesh/submesh: "+c.key);
                return new NativeSunShadowPass.Caster {renderer=renderer,materialIndex=c.material_index,submeshIndex=c.submesh_index,
                    overrideMaterial=materials[c.material],depthBias=c.depth_bias,slopeBias=c.slope_bias,
                    geometryLink=string.IsNullOrEmpty(c.linkKey)?null:links[c.linkKey]};
            }).ToArray();
            if(recordUndo) Undo.RecordObject(volume,"Configure current geometry sun shadows");
            pass.cascadeDimensions=new Vector2Int(config.atlasWidth,config.atlasHeight);pass.cascadeRegions=regions;
            pass.enableSourceCascades=enableValidatedAtlas;volume.priority=12000;
            EditorUtility.SetDirty(volume);EditorSceneManager.MarkSceneDirty(scene);AssetDatabase.SaveAssets();SceneView.RepaintAll();
        }
    }
}
#endif
