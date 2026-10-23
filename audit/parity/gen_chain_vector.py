import json
from agentic_os.mission.store import entry_hash, MemoryEventStore
from agentic_os.mission.approval import mint_approval_token, inputs_hash
evs=[("MissionCreated",{"goal":"café ☃ \U0001F600","n":5,"f":10.0,"big":1e20,"z":{"b":1,"a":[1,2.5,None,True]}}),
     ("NodeParked",{"node_id":"n1","amount":12.50,"small":1e-07}),
     ("ApprovalGranted",{"node_id":"n1","inputs_sha256":"ab"})]
prev="";out=[]
for i,(t,p) in enumerate(evs,1):
    ts=1700000000.123456+i
    h=entry_hash(prev,i,ts,t,"m1",p)
    out.append({"seq":i,"ts":ts,"type":t,"mission_id":"m1","payload":p,"prev":prev,"hash":h}); prev=h
print(json.dumps({"chain":out,"head":prev},ensure_ascii=True,indent=1))
inp={"to":"a@b.com","amount":12.5,"note":"café"}
tok=mint_approval_token("s3cret",operator="op",capability="cap",mission_id="m1",node_id="n1",inputs=inp,marker={"approved":True},ttl_s=300,now=1700000000.0)
print("inputs_sha256",inputs_hash(inp)); print("token",tok)
