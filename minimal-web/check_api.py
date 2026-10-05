"""Run only inside a network-isolated test container with capture queues."""
import base64
import json
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8080"
HEADERS = {"X-Print-UI": "1"}
password = Path("/data/admin-password").read_text().strip()
AUTH = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()


def api(path, body=None, expected=200, auth=False, headers=None):
    request_headers = dict(HEADERS if headers is None else headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    if auth:
        request_headers["Authorization"] = AUTH
    request = urllib.request.Request(BASE + path, data=data, headers=request_headers)
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as exc:
        response = exc
    result = json.load(response)
    assert response.status == expected, (path, response.status, result)
    return result


def upload(path):
    boundary = secrets.token_hex(16)
    payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
               + path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode())
    request = urllib.request.Request(BASE + "/api/uploads", data=payload,
        headers={**HEADERS, "Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status == 201
        return json.load(response)


def wait_job(identifier):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        jobs = api("/api/jobs")["jobs"]
        job = next(j for j in jobs if j["id"] == identifier)
        if job["state"] >= 7:
            assert job["state"] == 9, job
            return job
        time.sleep(.2)
    raise AssertionError(f"Job {identifier} did not finish")


api("/health")
assert len(api("/api/printers")["printers"]) == 2
caps = api("/api/printers/Brother_QL1110NWB")
defaults = {o["key"]: o["default"] for o in caps["options"]}
assert defaults["BrPriority"] == "BrSpeed"
assert defaults["job-sheets"] == "none,none"
source = upload(Path("/tmp/sample-4x6.pdf"))
payload = {"upload": source["id"], "printer": caps["name"], "options": defaults,
           "layout": {"scale": "none", "percent": 100, "rotation": 0, "crop": False}}
assert api("/api/preview", payload)["pages"] == 1
api("/api/preview", payload, expected=403, headers={})
api("/api/preview", payload, expected=403, headers={**HEADERS, "Origin": "http://unrelated.example"})
bad = {**payload, "options": {**defaults, "BrBrightness": "-999"}}
api("/api/preview", bad, expected=400)
api("/api/printers/Brother_QL1110NWB/defaults", payload, expected=401)
api("/api/printers/Brother_QL1110NWB/pause", {}, expected=401)

records = []
for quality, scale in [("BrSpeed", 100), ("BrQuality", 100), ("BrQuality", 50)]:
    request = {**payload, "options": {**defaults, "BrPriority": quality},
               "layout": {"scale": "custom", "percent": scale}, "submission": secrets.token_hex(16)}
    record = api("/api/print", request, expected=201)
    assert api("/api/print", request)["id"] == record["id"], "Duplicate submission printed twice"
    wait_job(record["id"])
    records.append({"id": record["id"], "quality": quality, "scale": scale})

image = upload(Path("/tmp/sample-4x6.png"))
request = {**payload, "upload": image["id"], "layout": {"scale": "fit"}, "submission": secrets.token_hex(16)}
record = api("/api/print", request, expected=201)
wait_job(record["id"])
records.append({"id": record["id"], "kind": "image"})

held = {**payload, "options": {**defaults, "job-hold-until": "indefinite"}, "submission": secrets.token_hex(16)}
record = api("/api/print", held, expected=201)
assert next(j for j in api("/api/jobs")["jobs"] if j["id"] == record["id"])["state"] == 4
api(f"/api/jobs/{record['id']}/cancel", {"upload": "wrong"}, expected=401)
api(f"/api/jobs/{record['id']}/cancel", {"upload": source["id"]})
assert next(j for j in api("/api/jobs")["jobs"] if j["id"] == record["id"])["state"] == 7

generic = api("/api/printers/Generic_Color")
assert not any(o["key"].startswith("Br") for o in generic["options"])
assert {"ColorModel", "Duplex", "Resolution"} <= {o["key"] for o in generic["options"]}
assert generic["custom"] is not None
generic_payload = {"upload": source["id"], "printer": "Generic_Color", "options": {"PageSize": "Custom.102x152mm", "ColorModel": "Gray", "Duplex": "DuplexTumble", "Resolution": "600dpi"}, "layout": {"scale": "fit"}}
assert api("/api/preview", generic_payload)["pages"] == 1
bad = {**generic_payload, "options": {**generic_payload["options"], "ColorModel": "RGB"}}
api("/api/preview", bad, expected=400)
bad = {**generic_payload, "options": {**generic_payload["options"], "PageSize": "Custom.1x152mm"}}
api("/api/preview", bad, expected=400)

saved = {**payload, "options": {**defaults, "BrPriority": "BrQuality", "BrBrightness": "-10", "BrContrast": "15"}, "layout": {"scale": "custom", "percent": 80}}
api("/api/printers/Brother_QL1110NWB/defaults", saved, auth=True)
restored = api("/api/printers/Brother_QL1110NWB")
assert {o["key"]: o["default"] for o in restored["options"]}["BrPriority"] == "BrQuality"
assert restored["layout"]["percent"] == 80
api("/api/printers/Brother_QL1110NWB/pause", {}, auth=True)
assert api("/api/printers/Brother_QL1110NWB")["state"] == 5
api("/api/printers/Brother_QL1110NWB/resume", {}, auth=True)
api("/api/printers/Brother_QL1110NWB/reject", {}, auth=True)
assert api("/api/printers/Brother_QL1110NWB")["accepting"] is False
api("/api/printers/Brother_QL1110NWB/accept", {}, auth=True)
api("/api/printers/Generic_Color/make-default", {}, auth=True)
assert api("/api/printers")["default"] == "Generic_Color"
Path("/tmp/api-checks.json").write_text(json.dumps({"native_jobs": records, "driver_controls": sum(o["source"] == "driver" for o in caps["options"]), "total_controls": len(caps["options"]), "generic_custom_media": True, "constraint_validation": True, "authentication_and_csrf": True, "idempotency": True, "own_job_cancellation": True, "defaults_and_queue_controls": True}, indent=2))
print(Path("/tmp/api-checks.json").read_text())
