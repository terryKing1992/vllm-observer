import json, urllib.request, uuid, time

def request(max_tokens):
    tid = uuid.uuid4().hex
    payload = json.dumps({
        "model": "qwen-observed",
        "messages": [{"role": "user", "content": "数数"}],
        "max_tokens": max_tokens, "ignore_eos": True, "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(
        "http://localhost:18000/v1/chat/completions", data=payload,
        headers={"Content-Type": "application/json", "x-trace-id": tid})
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()
    return tid

short = [request(2) for _ in range(4)]
long = [request(64) for _ in range(4)]
json.dump({"short": short, "long": long}, open("tids.json", "w"))
print("short:", [t[:10] for t in short])
print("long: ", [t[:10] for t in long])
