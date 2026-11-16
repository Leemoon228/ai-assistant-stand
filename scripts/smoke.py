"""Check an isolated stand, optionally including a response from the configured LLM."""

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request(url, expected=200, token=None, payload=None, timeout=15):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as response:
            status, body = response.status, response.read()
    except HTTPError as error:
        status, body = error.code, error.read()
    if status != expected:
        raise ValueError(f"Unexpected HTTP {status} at {url}; expected {expected}")
    return body


def wait_for_health(url):
    for attempt in range(30):
        try:
            request(url)
            return
        except (URLError, TimeoutError, ValueError):
            if attempt == 29:
                raise
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stand", type=Path)
    parser.add_argument("--with-generation", action="store_true")
    args = parser.parse_args()
    stand = args.stand.resolve()
    manifest = json.loads((stand / "manifest.json").read_text(encoding="utf-8"))
    for component, selected in manifest["components"].items():
        for image in selected["manifest"]["images"]:
            subprocess.run(["docker", "image", "load", "--input",
                            str(stand / "components" / component / image["archive"])], check=True)
            identity = subprocess.check_output(
                ["docker", "image", "inspect", "--format", "{{.Id}}", image["image"]], text=True).strip()
            if identity not in image.get("load_ids", [image["image_id"]]):
                raise ValueError(f"Unexpected image identity for {image['service']}: {identity}")
    environment = dict(os.environ, SSO_SHARED_SECRET=secrets.token_hex(32),
                       GRAFANA_ADMIN_PASSWORD=secrets.token_hex(32),
                       FRONTEND_PORT="0", BACKEND_PORT="0", ML_PORT="0", MARKETPLACE_PORT="0")
    if not args.with_generation:
        environment.update(OLLAMA_BASE_URL="http://127.0.0.1:9", OLLAMA_FALLBACK_URL="http://127.0.0.1:9")
    compose = ["docker", "compose", "--project-name", f"stand-check-{secrets.token_hex(4)}",
               "--env-file", str(stand / ".env.example"), "-f", str(stand / "compose.yml")]

    def base(service, port):
        address = subprocess.check_output(compose + ["port", service, str(port)],
                                          env=environment, text=True).strip()
        return f"http://{address}"

    try:
        subprocess.run(compose + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "180"],
                       env=environment, check=True)
        frontend, backend = base("frontend", 80), base("backend", 5005)
        ml, marketplace = base("ml", 8000), base("marketplace-mock", 8081)
        for url in (frontend, backend + "/hello", ml + "/health", marketplace + "/actuator/health"):
            wait_for_health(url)
        request(frontend + "/hello")
        request(backend + "/orders", expected=401)
        alice = json.loads(request(marketplace + "/api/auth/login",
                                   payload={"username": "alice", "password": "alice123"}))["token"]
        boris = json.loads(request(marketplace + "/api/auth/login",
                                   payload={"username": "boris", "password": "boris123"}))["token"]
        orders = json.loads(request(marketplace + "/api/orders", token=alice))
        order_id = orders[0]["id"]
        request(marketplace + f"/api/orders/{order_id}", token=alice)
        request(marketplace + f"/api/orders/{order_id}", token=boris, expected=404)
        request(backend + "/orders", token=alice)
        prediction = json.loads(request(ml + "/assistant/classify", payload={"text": "Where is my order?"}))
        if not prediction.get("intent") or not 0 <= prediction["confidence"] <= 1:
            raise ValueError("Invalid classification response")
        report = {"passed": True, "external_inference": args.with_generation,
                  "checks": ["service health", "frontend proxy", "missing token",
                             "SSO login", "own order", "foreign order", "backend orders", "classification"]}
        if args.with_generation:
            generated = json.loads(request(
                ml + "/assistant/generate-answer", timeout=180,
                payload={"text": "Which payment methods do you accept?"}))
            if not isinstance(generated.get("content"), str) or not generated["content"].strip():
                raise ValueError("The configured LLM returned no answer")
            report["checks"].append("configured remote LLM response")
        (stand / "smoke-test.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report))
    except Exception:
        subprocess.run(compose + ["logs", "--no-color", "--tail", "60"], env=environment, check=False)
        raise
    finally:
        subprocess.run(compose + ["down", "--volumes", "--remove-orphans"], env=environment, check=True)


if __name__ == "__main__":
    main()
