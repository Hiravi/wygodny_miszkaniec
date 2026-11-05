"""Reproducibly raise R5's detailed-path return capacity, without changing routing.

Upstream explicitly permits increasing this HTTP-oriented bound. The application
still computes every prepared destination; an unpatched runtime uses 5000 batches.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from tools.data.acquire import ROOT, sha256
from tools.router.bootstrap import download_verified
from tools.router.spike import R5_SHA256

BASE = "https://raw.githubusercontent.com/r5py/r5/v7.5.1-r5py/"
SOURCE = "src/main/java/com/conveyal/r5/analyst/cluster/PathResult.java"
SOURCE_SHA = "8a8dd4a7dc7e742f107cbf24b6bc1de431e12995ed6cbb124cc24caf1149c978"
LICENSE_SHA = "50ceabea3b9979201c7e4e888ccce89e03a788fa9d6a546cf106c49d0f896bba"
CAPACITY = 20_000


def runtime_jar(root=ROOT):
    base = root / ".cache/router/r5-v7.5.1-r5py-all.jar"
    # Experimental capacity change is never selected by normal startup.
    if os.environ.get("WM_R5_PATH_CAPACITY") != "expanded":
        from tools.router.instant_point_runtime import verified_runtime

        return verified_runtime(root) or base
    manifest = root / ".cache/router/path-capacity-v1.json"
    if not manifest.exists():
        return base
    proof = json.loads(manifest.read_text(encoding="utf-8"))
    target = root / ".cache/router/r5-path-capacity-v1.jar"
    if proof["base_sha256"] != R5_SHA256 or proof["capacity"] != CAPACITY:
        raise ValueError("Unexpected R5 capacity patch metadata")
    if sha256(base) != R5_SHA256 or sha256(target) != proof["output_sha256"]:
        raise ValueError("R5 capacity artifact checksum mismatch")
    return target


def main():
    cache = ROOT / ".cache/router"
    original = cache / "r5-v7.5.1-r5py-all.jar"
    if sha256(original) != R5_SHA256:
        raise ValueError("Pinned R5 checksum mismatch")
    source = cache / "path-capacity-source/PathResult.java"
    licence = source.with_name("LICENSE")
    download_verified(BASE + SOURCE, source, SOURCE_SHA)
    download_verified(BASE + "LICENSE", licence, LICENSE_SHA)
    old = "public static final int MAX_PATH_DESTINATIONS = 5_000;"
    text = source.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise ValueError("Unexpected upstream capacity declaration")
    modified = text.replace(old, f"public static final int MAX_PATH_DESTINATIONS = {CAPACITY};")
    target = cache / "r5-path-capacity-v1.jar"
    with tempfile.TemporaryDirectory(dir=cache, prefix="path-capacity-") as directory:
        build = Path(directory)
        java = build / "PathResult.java"
        java.write_text(modified, encoding="utf-8")
        classes = build / "classes"
        subprocess.run(
            [
                str(cache / "jdk-25.0.4.1+1/bin/javac.exe"),
                "--release",
                "17",
                "-g:none",
                "-classpath",
                str(original),
                "-d",
                str(classes),
                str(java),
            ],
            check=True,
            capture_output=True,
        )
        replacements = {
            path.relative_to(classes).as_posix(): path.read_bytes()
            for path in classes.rglob("*.class")
        }
        replacements.update(
            {
                "META-INF/wm-r5/PathResult.java": modified.encode("utf-8"),
                "META-INF/wm-r5/LICENSE": licence.read_bytes(),
            }
        )
        with ZipFile(original) as source_jar, ZipFile(target, "w") as output:
            for info in source_jar.infolist():
                if info.filename not in replacements:
                    output.writestr(info, source_jar.read(info))
            for name, content in sorted(replacements.items()):
                info = ZipInfo(name, (2026, 10, 22, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                output.writestr(info, content)
    proof = {
        "policy": "r5-path-capacity-v1",
        "base_sha256": R5_SHA256,
        "source_url": BASE + SOURCE,
        "source_sha256": SOURCE_SHA,
        "license_url": BASE + "LICENSE",
        "license_sha256": LICENSE_SHA,
        "capacity": CAPACITY,
        "output_sha256": sha256(target),
        "change": "Only MAX_PATH_DESTINATIONS:5000->20000; no timetable/search/access changes",
        "source_offer": "Modified source and MIT notice inside META-INF/wm-r5 in derived JAR",
    }
    (cache / "path-capacity-v1.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
