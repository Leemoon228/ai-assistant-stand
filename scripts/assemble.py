"""Resolve published component releases and assemble a versioned local stand."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
from urllib.request import Request, urlopen


COMPONENTS = {
    "frontend": ("OlgaRhythm/ai-assistent-frontend", {"frontend"}),
    "backend": ("OlgaRhythm/Market_AI_assistent_backend", {"backend"}),
    "ml": ("Leemoon228/ai-assistant", {"ml"}),
    "marketplace-mock": ("OlgaRhythm/ai-assistent-marketplace-mock",
                         {"marketplace-mock", "webhook-receiver"}),
}
ROOT = Path(__file__).resolve().parents[1]


def fetch(url):
    headers = {"User-Agent": "ai-assistant-stand"}
    # Public asset downloads redirect to storage hosts. Send credentials only to the API.
    if url.startswith("https://api.github.com/") and os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"
    with urlopen(Request(url, headers=headers), timeout=120) as response:
        return response.read()


def parse_checksums(data):
    checksums = {}
    for line in data.decode("utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if (not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
                or name in checksums):
            raise ValueError("Invalid or duplicate release checksum entry")
        checksums[name] = digest
    if not checksums:
        raise ValueError("Empty release checksums")
    return checksums


def verify(data, digest, name):
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"Checksum mismatch: {name}")


def loaded_image_ids(archive, image):
    """Allow config and OCI manifest IDs for the same checksum-verified image."""
    with tarfile.open(archive, "r:gz") as contents:
        entries = json.load(contents.extractfile("manifest.json"))
        entries = [entry for entry in entries if image["image"] in (entry.get("RepoTags") or [])]
        if len(entries) != 1:
            raise ValueError(f"Archive must contain exactly one {image['image']}")
        raw_config = contents.extractfile(entries[0]["Config"]).read()
        config_id = "sha256:" + hashlib.sha256(raw_config).hexdigest()
        config = json.loads(raw_config)
        if (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
            raise ValueError("Unexpected archive image platform")

        def matching_ids(descriptor):
            digest = descriptor["digest"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("Invalid OCI image digest")
            document = contents.extractfile("blobs/sha256/" + digest[7:]).read()
            verify(document, digest[7:], "OCI image descriptor")
            metadata = json.loads(document)
            if metadata.get("config", {}).get("digest") == config_id:
                return {digest}
            children = set()
            for child in metadata.get("manifests", []):
                children.update(matching_ids(child))
            return children | {digest} if children else set()

        identities = {config_id}
        if "index.json" in contents.getnames():
            index = json.load(contents.extractfile("index.json"))
            for descriptor in index["manifests"]:
                identities.update(matching_ids(descriptor))
        if image["image_id"] not in identities:
            raise ValueError(f"Archive image identity mismatch for {image['service']}")
        return sorted(identities)


def resolve():
    components = {}
    for component, (repository, services) in COMPONENTS.items():
        release = json.loads(fetch(f"https://api.github.com/repos/{repository}/releases/latest"))
        tag = release["tag_name"]
        if release["draft"] or release["prerelease"] or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            raise ValueError(f"{repository}: latest must be a published v1.2.3 release")
        assets = {item["name"]: item["browser_download_url"] for item in release["assets"]}
        if not {"manifest.json", "SHA256SUMS"} <= assets.keys():
            raise ValueError(f"{repository}: publish a release in the common format first")
        checksums = parse_checksums(fetch(assets["SHA256SUMS"]))
        raw_manifest = fetch(assets["manifest.json"])
        verify(raw_manifest, checksums["manifest.json"], f"{repository}/manifest.json")
        manifest = json.loads(raw_manifest)
        if (manifest["schema_version"] != 1 or manifest["repository"] != repository
                or manifest["tag"] != tag or manifest["platform"] != "linux/amd64"
                or not re.fullmatch(r"[0-9a-f]{40}", manifest["revision"])
                or {image["service"] for image in manifest["images"]} != services
                or len(manifest["images"]) != len(services)):
            raise ValueError(f"{repository}: incompatible component manifest")
        for image in manifest["images"]:
            if (image["image"] != f"ai-assistant-{image['service']}:{tag}"
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", image["image_id"])
                    or image["archive_sha256"] != checksums.get(image["archive"])):
                raise ValueError(f"{repository}: invalid image identity")
            for key in ("archive", "sbom", "vulnerabilities"):
                if image[key] not in checksums:
                    raise ValueError(f"{repository}: unchecked {key}")
        if not checksums.keys() <= assets.keys():
            raise ValueError(f"{repository}: incomplete release assets")
        components[component] = {
            "release_id": release["id"], "release_url": release["html_url"],
            "manifest": manifest, "checksums": checksums,
            "assets": {name: assets[name] for name in checksums},
        }
    return {"schema_version": 1, "components": components}


def assemble(resolution, destination):
    if resolution["schema_version"] != 1 or resolution["components"].keys() != COMPONENTS.keys():
        raise ValueError("Resolution must contain the four expected components")
    destination.mkdir(parents=True, exist_ok=False)
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    for component, selected in resolution["components"].items():
        directory = destination / "components" / component
        directory.mkdir(parents=True)
        for name, digest in selected["checksums"].items():
            # Validate again when resuming a saved resolution.
            parse_checksums(f"{digest}  {name}\n".encode())
            url = selected["assets"][name]
            repository = COMPONENTS[component][0]
            tag = selected["manifest"]["tag"]
            if not url.startswith(f"https://github.com/{repository}/releases/download/{tag}/"):
                raise ValueError("Unexpected release asset URL")
            target = directory / name
            # Stream image archives instead of holding them in memory.
            digest_state = hashlib.sha256()
            with urlopen(Request(url, headers={"User-Agent": "ai-assistant-stand"}), timeout=120) as response:
                with target.open("wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        digest_state.update(chunk)
                        stream.write(chunk)
            if digest_state.hexdigest() != digest:
                raise ValueError(f"Checksum mismatch: {component}/{name}")
        sums = [f"{digest}  {name}" for name, digest in selected["checksums"].items()]
        (directory / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
        for image in selected["manifest"]["images"]:
            image["load_ids"] = loaded_image_ids(directory / image["archive"], image)
            variable = image["service"].upper().replace("-", "_") + "_IMAGE"
            environment, replacements = re.subn(
                rf"(?m)^{variable}=.*$", f"{variable}={image['image']}", environment)
            if replacements != 1:
                raise ValueError(f"Missing image variable: {variable}")
    for name in ("compose.yml", "start.ps1", "README.md"):
        shutil.copyfile(ROOT / name, destination / name)
    shutil.copytree(ROOT / "monitoring", destination / "monitoring")
    (destination / ".env.example").write_text(environment, encoding="utf-8")
    manifest = dict(resolution, revision=os.environ.get("GITHUB_SHA", "local-validation"),
                    tag=os.environ.get("GITHUB_REF_NAME", "local-validation"))
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolution", type=Path, required=True)
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.resolve:
        selection = resolve()
        args.resolution.parent.mkdir(parents=True, exist_ok=True)
        with args.resolution.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(selection, indent=2) + "\n")
    else:
        if args.output is None:
            parser.error("--output is required when assembling a stand")
        assemble(json.loads(args.resolution.read_text(encoding="utf-8")), args.output)


if __name__ == "__main__":
    main()
