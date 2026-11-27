#!/usr/bin/env python3
"""Sign the application region of a panel bundle with Espressif Secure Boot V2."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from termokontur import sensor_firmware


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    key = os.environ.pop("TERMOKONTUR_OTA_SIGNING_KEY", "")
    if not key.strip():
        parser.exit(1, "Configure TERMOKONTUR_OTA_SIGNING_KEY for firmware releases.\n")

    archive_path = args.bundle.resolve()
    bundle = sensor_firmware.read_release(archive_path.read_bytes())
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))

    # Remove the secret from child environments. Only espsecure reads the
    # private file inside the temporary directory.
    with TemporaryDirectory(prefix=".ota-sign-", dir=archive_path.parent) as directory:
        folder = Path(directory)
        private_key = folder / "signing.pem"
        with private_key.open("x", encoding="ascii") as stream:
            private_key.chmod(0o600)
            stream.write(key.rstrip() + "\n")
        image = folder / "firmware.bin"
        image.write_bytes(bundle.esp32)
        espsecure = [sys.executable, "-m", "espsecure"]
        signed_image = image
        if not args.verify_only:
            signed_image = folder / "signed.bin"
            subprocess.run([*espsecure, "sign_data", "--version", "2", "--keyfile", str(private_key),
                            "--output", str(signed_image), str(image)], check=True)
        subprocess.run([*espsecure, "verify_signature", "--version", "2", "--keyfile", str(private_key),
                        str(signed_image)], check=True)
        if args.verify_only:
            return

        signed = signed_image.read_bytes()
        if len(signed) > sensor_firmware.MAX_FIRMWARE or not signed.startswith(bundle.esp32):
            raise ValueError("Signed application must fit in the OTA partition and preserve the application bytes")
        factory = bundle.factory.image[:sensor_firmware.APP_OFFSET] + signed
        manifest["files"]["factory.bin"] = {
            "size": len(factory), "sha256": hashlib.sha256(factory).hexdigest(),
        }
        packaged = folder / archive_path.name
        with zipfile.ZipFile(packaged, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("factory.bin", factory)
        verified = sensor_firmware.read_release(packaged.read_bytes())
        if verified.esp32 != signed:
            raise ValueError("Packaged OTA image differs from the signed bytes")
        packaged.replace(archive_path)


if __name__ == "__main__":
    main()
