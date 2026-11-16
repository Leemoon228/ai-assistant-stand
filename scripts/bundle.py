"""Package a smoke-tested stand and checksum its public release assets."""

import argparse
import hashlib
from pathlib import Path
import shutil
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stand", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if (args.stand / ".env").exists():
        raise ValueError("A stand release must not include a local .env")
    if not (args.stand / "smoke-test.json").is_file():
        raise ValueError("Run the stand smoke check before packaging")
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / "stand-linux-amd64.tar.gz"
    with tarfile.open(archive, "w:gz", compresslevel=1) as target:
        target.add(args.stand, arcname=".")
    if archive.stat().st_size >= 2 * 1024 ** 3:
        raise ValueError("The stand archive exceeds the GitHub Release asset size limit")
    for name in ("manifest.json", "smoke-test.json", "README.md"):
        shutil.copyfile(args.stand / name, args.output / name)
    sums = []
    for path in sorted(args.output.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS":
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            sums.append(f"{digest}  {path.name}")
    (args.output / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
