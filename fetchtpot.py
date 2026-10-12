import json, os, subprocess, sys
tids = json.load(open("tids.json"))
frm, to = sys.argv[1], sys.argv[2]
def query(tid):
    cmd = ["npx.cmd", "--yes", "langfuse-cli@latest", "api", "observations", "list",
           "--trace-id", tid, "--fields", "core,metadata",
           "--from-start-time", frm, "--to-start-time", to, "--limit", "50", "--json"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60)
    d = json.loads(r.stdout)["body"]["data"]
    root = [x for x in d if x.get("isRootObservation")][0]
    m = root["metadata"]
    return {
        "tpot_ms": m.get("attributes.observer.tpot_ms"),
        "decode_ms": m.get("attributes.observer.decode.total_ms"),
        "decode_count": m.get("attributes.observer.decode.count"),
        "prefill_ms": m.get("attributes.observer.prefill.total_ms"),
        "execute_mean_ms": m.get("attributes.observer.execute_model.mean_ms"),
        "contention": m.get("attributes.langfuse.observation.metadata.contention_steps",
                            m.get("attributes.observer.decode.count")),
    }
for label in ("short", "long"):
    print(label)
    for tid in tids[label]:
        print("  ", {k: (round(v,1) if isinstance(v,float) else v) for k,v in query(tid).items()})

