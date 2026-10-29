"""Report exact runtime-vs-committed Studio request-schema drift in CI."""
import json
from pathlib import Path
from blender_mcp.studio.contracts import json_schemas
saved=json.loads((Path(__file__).resolve().parents[1]/"docs/schemas/studio-request-v1.schema.json").read_text())
runtime=json_schemas()["request"]
def compare(a,b,p="$"):
    if isinstance(a,dict) and isinstance(b,dict):
        for k in sorted(a.keys()|b.keys()):
            if k not in a: print("MISSING_SAVED",p+"."+k,json.dumps(b[k],sort_keys=True)[:1500])
            elif k not in b: print("EXTRA_SAVED",p+"."+k,json.dumps(a[k],sort_keys=True)[:1500])
            else: compare(a[k],b[k],p+"."+k)
    elif isinstance(a,list) and isinstance(b,list):
        if len(a)!=len(b): print("LIST_LENGTH",p,len(a),len(b))
        for i,(x,y) in enumerate(zip(a,b)): compare(x,y,p+"["+str(i)+"]")
    elif a!=b: print("DIFF",p,"saved=",repr(a)[:200],"runtime=",repr(b)[:200])
compare(saved,runtime)
print("BAS11_SCHEMA_DRIFT_CHECK_COMPLETE")
