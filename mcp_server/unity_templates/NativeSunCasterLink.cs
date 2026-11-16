using System;
using System.Collections.Generic;
using System.IO;
using UnityEngine;
using UnityEngine.Rendering;

namespace OmniRenderDoc
{
    // A source game may use a different authored LOD for shadows. The imported
    // topology remains an ordinary editable Mesh asset. Current visible-mesh
    // vertex deltas are transferred through an explicit import-time binding.
    [ExecuteAlways]
    public sealed class NativeSunCasterLink : MonoBehaviour
    {
        public MeshFilter source;
        public string bindingKey;
        public Mesh shadowTopology;
        public TextAsset vertexBindings;
        public Vector3 templateOrigin;
        public int sourceVertexCount;
        public string bindingMethod;
        [NonSerialized] Mesh current;
        [NonSerialized] int[] indices;
        [NonSerialized] Vector3[] weights,reference;
        [NonSerialized] TextAsset loadedBindings;
        [NonSerialized] int lastSerial=-1;
        readonly List<Vector3> sourcePositions=new List<Vector3>(),templatePositions=new List<Vector3>();
        readonly List<Vector4> templateUVs=new List<Vector4>();
        public Mesh GetCurrentMesh(int renderSerial)
        {
            if(source==null || source.sharedMesh==null || shadowTopology==null || vertexBindings==null)
                throw new InvalidOperationException("Source shadow LOD requires its editable source mesh and vertex binding.");
            if(source.sharedMesh.vertexCount!=sourceVertexCount)
                throw new InvalidOperationException("Source mesh topology changed; rebuild the shadow LOD binding before using this source atlas: "+source.name);
            if(current!=null && renderSerial==lastSerial) return current;
            if(loadedBindings!=vertexBindings)
            {
                var raw=vertexBindings.bytes;int count=shadowTopology.vertexCount;
                if(raw.Length!=checked(count*36))throw new InvalidDataException("Shadow LOD binding size differs from topology.");
                indices=new int[count*3];weights=new Vector3[count];reference=new Vector3[count];
                using(var reader=new BinaryReader(new MemoryStream(raw))) for(int i=0;i<count;i++)
                {
                    for(int j=0;j<3;j++){indices[i*3+j]=reader.ReadInt32();if(indices[i*3+j]<0 || indices[i*3+j]>=sourceVertexCount)throw new InvalidDataException("Shadow LOD source vertex is out of bounds.");}
                    weights[i]=new Vector3(reader.ReadSingle(),reader.ReadSingle(),reader.ReadSingle());
                    reference[i]=new Vector3(reader.ReadSingle(),reader.ReadSingle(),reader.ReadSingle());
                    var w=weights[i];var r=reference[i];
                    if(!float.IsFinite(w.x+w.y+w.z+r.x+r.y+r.z) || Mathf.Abs(w.x+w.y+w.z-1)>0.001f)
                        throw new InvalidDataException("Invalid shadow LOD barycentric binding.");
                }
                loadedBindings=vertexBindings;
            }
            if(shadowTopology.vertexCount!=weights.Length)throw new InvalidOperationException("Shadow LOD topology changed; rebuild the explicit source binding.");
            source.sharedMesh.GetVertices(sourcePositions);shadowTopology.GetVertices(templatePositions);
            if(current==null)
            {
                current=Instantiate(shadowTopology);current.name=shadowTopology.name+" current editable geometry";
                current.hideFlags=HideFlags.HideAndDontSave;current.MarkDynamic();
            }
            for(int i=0;i<templatePositions.Count;i++)
            {
                var w=weights[i];var edited=sourcePositions[indices[i*3]]*w.x+sourcePositions[indices[i*3+1]]*w.y+sourcePositions[indices[i*3+2]]*w.z;
                templatePositions[i]=templatePositions[i]-templateOrigin+(edited-reference[i]);
            }
            current.SetVertices(templatePositions);
            current.subMeshCount=shadowTopology.subMeshCount;
            for(int i=0;i<shadowTopology.subMeshCount;i++)current.SetIndices(shadowTopology.GetIndices(i),shadowTopology.GetTopology(i),i,false);
            for(int channel=0;channel<8;channel++){shadowTopology.GetUVs(channel,templateUVs);current.SetUVs(channel,templateUVs);}
            current.RecalculateBounds();lastSerial=renderSerial;return current;
        }
        void OnDisable()
        {if(current!=null)CoreUtils.Destroy(current);current=null;lastSerial=-1;loadedBindings=null;}
        void OnValidate(){lastSerial=-1;loadedBindings=null;}
    }
}
