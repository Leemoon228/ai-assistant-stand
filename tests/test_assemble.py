import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import assemble


class ReleaseIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.responses = {}
        for repository, services in assemble.COMPONENTS.values():
            tag = "v0.1.0"
            prefix = f"https://github.com/{repository}/releases/download/{tag}/"
            images = []
            payloads = {}
            for service in services:
                archive = f"{service}-linux-amd64.tar.gz"
                payloads[archive] = b"fixture Docker archive"
                payloads[f"{service}.cdx.json"] = b"{}"
                payloads[f"{service}-vulnerabilities.json"] = b"{}"
                images.append({
                    "service": service, "image": f"ai-assistant-{service}:{tag}",
                    "image_id": "sha256:" + "a" * 64, "archive": archive,
                    "archive_sha256": hashlib.sha256(payloads[archive]).hexdigest(),
                    "sbom": f"{service}.cdx.json", "vulnerabilities": f"{service}-vulnerabilities.json",
                })
            payloads["manifest.json"] = json.dumps({
                "schema_version": 1, "repository": repository, "tag": tag,
                "revision": "b" * 40, "platform": "linux/amd64", "images": images,
            }).encode()
            sums = "\n".join(f"{hashlib.sha256(data).hexdigest()}  {name}"
                             for name, data in payloads.items())
            payloads["SHA256SUMS"] = sums.encode()
            self.responses.update({prefix + name: data for name, data in payloads.items()})
            self.responses[f"https://api.github.com/repos/{repository}/releases/latest"] = json.dumps({
                "id": 1, "tag_name": tag, "html_url": prefix, "draft": False, "prerelease": False,
                "assets": [{"name": name, "browser_download_url": prefix + name} for name in payloads],
            }).encode()

    def resolve(self):
        with patch.object(assemble, "fetch", side_effect=self.responses.__getitem__):
            return assemble.resolve()

    def test_resolves_all_four_components_and_five_images(self):
        resolved = self.resolve()
        self.assertEqual(set(resolved["components"]), set(assemble.COMPONENTS))
        self.assertEqual(sum(len(item["manifest"]["images"])
                             for item in resolved["components"].values()), 5)

    def test_rejects_changed_manifest(self):
        url = next(url for url in self.responses if url.endswith("/manifest.json"))
        self.responses[url] += b" "
        with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
            self.resolve()

    def test_rejects_legacy_release_without_manifest(self):
        url = next(url for url in self.responses if url.endswith("/releases/latest"))
        release = json.loads(self.responses[url])
        release["assets"] = []
        self.responses[url] = json.dumps(release).encode()
        with self.assertRaisesRegex(ValueError, "common format"):
            self.resolve()

    def test_rejects_missing_asset(self):
        url = next(url for url in self.responses if url.endswith("/releases/latest"))
        release = json.loads(self.responses[url])
        release["assets"] = [asset for asset in release["assets"] if not asset["name"].endswith(".cdx.json")]
        self.responses[url] = json.dumps(release).encode()
        with self.assertRaisesRegex(ValueError, "incomplete release"):
            self.resolve()

    def test_rejects_path_traversal_and_duplicate_checksums(self):
        for names in (["../outside"], ["C:outside"], ["manifest.json", "manifest.json"]):
            content = "\n".join(f"{'a' * 64}  {name}" for name in names).encode()
            with self.assertRaises(ValueError):
                assemble.parse_checksums(content)

    def test_rejects_corrupted_download_before_startup(self):
        selected = self.resolve()
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(assemble, "urlopen", return_value=io.BytesIO(b"corrupted")):
                with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                    assemble.assemble(selected, Path(temporary) / "stand")


if __name__ == "__main__":
    unittest.main()
