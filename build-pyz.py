#!/usr/bin/env python3
"""Build pdf-signer.pyz from src/."""
import shutil
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent

shutil.copy(ROOT / "watermark.png", ROOT / "src/image/watermark.png")
shutil.copy(ROOT / "LICENSE", ROOT / "src/LICENSE")
zipapp.create_archive(
    ROOT / "src",
    ROOT / "pdf-signer.pyz",
    interpreter="/usr/bin/env python3",
    compressed=True,
)
print(ROOT / "pdf-signer.pyz")
