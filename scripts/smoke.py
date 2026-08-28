#!/usr/bin/env python3
"""Independent live broker smoke.  Never prints credentials or source keys."""
import json
import os
import sys
import urllib.request

base=os.getenv("BROKER_URL","http://192.168.50.2:8817").rstrip("/")
token=os.environ["BROKER_CLIENT_TOKEN"]

def request(path, payload):
    req=urllib.request.Request(base+path,data=json.dumps(payload).encode(),headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"},method="POST")
    with urllib.request.urlopen(req,timeout=90) as response:
        return response.status,json.load(response)

for model,effort in (("standard","high"),("smart","medium"),("expert","low")):
    status,data=request("/v1/generate",{"model":model,"effort":effort,"input":"Reply with exactly: provider-broker-ok"})
    if status != 200 or not data.get("actual_model"):
        raise SystemExit(f"{model}/{effort}: failed")
    print(f"{model}/{effort}: OK actual_model={data['actual_model']}")
