"""Pinned Blender regression for curved shading and genuinely 3D typed text."""
import importlib.util
import json
from pathlib import Path
import sys
from uuid import uuid4
import bpy

out=Path(sys.argv[sys.argv.index('--')+1]).resolve()
out.mkdir(parents=True,exist_ok=True)
assert bpy.app.version_string.startswith('4.5.15')
spec=importlib.util.spec_from_file_location('bas14_quality_addon',Path(__file__).resolve().parents[1]/'addon.py')
addon=importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
server=addon.BlenderMCPServer(profile='studio-local')
results={}
for primitive in ('cube','sphere','cylinder','cone'):
    _,sid,rev=server._studio_scene_context_v1()
    server.studio_create_object_v1(sid,rev,str(uuid4()),'Quality_'+primitive,primitive,[0.,0.,0.])
    obj=bpy.context.object
    curved=[p for p in obj.data.polygons if primitive=='sphere' or len(p.vertices)<=4]
    caps=[p for p in obj.data.polygons if len(p.vertices)>4]
    if primitive=='cube':
        assert not any(p.use_smooth for p in obj.data.polygons)
    else:
        assert curved and all(p.use_smooth for p in curved)
        assert not any(p.use_smooth for p in caps)
    results[primitive]={'smooth_faces':sum(p.use_smooth for p in obj.data.polygons),
                        'faces':len(obj.data.polygons)}
_,sid,rev=server._studio_scene_context_v1()
text=server.studio_create_text_v1(sid,rev,str(uuid4()),'Quality_Text','AI STUDIO',[0.,0.,0.])
obj=bpy.data.objects[text['name']]
assert obj.type=='FONT' and obj.data.extrude>0 and obj.data.bevel_depth>0
_,sid,rev=server._studio_scene_context_v1()
server.studio_assign_material_v1(sid,rev,str(uuid4()),text['object_id'],
    {'kind':'pbr','base_color':[.6,.3,.05,1.],'metallic':.7,'roughness':.25})
assert obj.data.materials and obj.data.materials[-1].use_nodes
results['text']={'type':obj.type,'extrude':obj.data.extrude,'bevel_depth':obj.data.bevel_depth,
                 'pbr_material':obj.data.materials[-1].name}
(out/'bas14-quality.json').write_text(json.dumps(results,indent=2))
print('BAS14_CURVED_NORMALS_AND_3D_TEXT_OK',json.dumps(results),flush=True)
