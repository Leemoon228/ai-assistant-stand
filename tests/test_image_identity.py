import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from assemble import loaded_image_ids


class ImageIdentityTests(unittest.TestCase):
    def archive(self, path, oci=True, architecture="amd64"):
        config = json.dumps({"os": "linux", "architecture": architecture}).encode()
        config_hash = hashlib.sha256(config).hexdigest()
        config_path = f"blobs/sha256/{config_hash}"
        files = {config_path: config, "manifest.json": json.dumps([
            {"RepoTags": ["ai-assistant-frontend:v0.1.0"], "Config": config_path}
        ]).encode()}
        identities = {"sha256:" + config_hash}
        if oci:
            manifest = json.dumps({"config": {"digest": "sha256:" + config_hash}}).encode()
            manifest_hash = hashlib.sha256(manifest).hexdigest()
            files[f"blobs/sha256/{manifest_hash}"] = manifest
            index = json.dumps({"manifests": [{"digest": "sha256:" + manifest_hash}]}).encode()
            index_hash = hashlib.sha256(index).hexdigest()
            files[f"blobs/sha256/{index_hash}"] = index
            files["index.json"] = json.dumps({"manifests": [{"digest": "sha256:" + index_hash}]}).encode()
            identities.update({"sha256:" + manifest_hash, "sha256:" + index_hash})
        with tarfile.open(path, "w:gz") as target:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                target.addfile(member, io.BytesIO(data))
        image = {"service": "frontend", "image": "ai-assistant-frontend:v0.1.0",
                 "image_id": "sha256:" + config_hash}
        return image, identities

    def test_accepts_legacy_config_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.tar.gz"
            image, identities = self.archive(path, oci=False)
            self.assertEqual(set(loaded_image_ids(path, image)), identities)

    def test_accepts_containerd_manifest_and_index_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.tar.gz"
            image, identities = self.archive(path)
            self.assertEqual(set(loaded_image_ids(path, image)), identities)
            image["image_id"] = next(identity for identity in identities if identity != image["image_id"])
            self.assertEqual(set(loaded_image_ids(path, image)), identities)

    def test_rejects_unrelated_image_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.tar.gz"
            image, _ = self.archive(path)
            image["image_id"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                loaded_image_ids(path, image)

    def test_rejects_wrong_tag_and_platform(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.tar.gz"
            image, _ = self.archive(path)
            image["image"] = "wrong:v0.1.0"
            with self.assertRaisesRegex(ValueError, "exactly one"):
                loaded_image_ids(path, image)
            image, _ = self.archive(path, architecture="arm64")
            with self.assertRaisesRegex(ValueError, "platform"):
                loaded_image_ids(path, image)


if __name__ == "__main__":
    unittest.main()
