"""Reproduce the narrow pinned-R5 fix for exact scheduled point departures.

The original short-window sampler can never advance when its integer step is1.
Only an explicit single-draw, scheduled, at-most-one-minute request is changed.
FastRaptor, network serialization, source schedules and access stay untouched.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from tools.data.acquire import ROOT, sha256
from tools.router.bootstrap import download_verified
from tools.router.spike import R5_SHA256

POLICY = "r5-exact-scheduled-point-v1"
EMBED = "META-INF/wm-r5-instant-point"
BASE = "https://raw.githubusercontent.com/r5py/r5/v7.5.1-r5py/"
SOURCE = "src/main/java/com/conveyal/r5/profile/McRaptorSuboptimalPathProfileRouter.java"
SOURCE_SHA = "6391d9366cbc28fd568464a38ec77371f49061666205697f4b7220049d5a0834"
LICENSE_SHA = "50ceabea3b9979201c7e4e888ccce89e03a788fa9d6a546cf106c49d0f896bba"
ANCHOR = "    private ArrayList<Integer> generateDepartureTimesToSample (ProfileRequest request) {"
GUARD = """
        // Exact scheduled point departure, matching one-minute FastRaptor queries.
        // No frequency sampling or wider-window request is changed.
        if (!network.transitLayer.hasFrequencies && request.monteCarloDraws == 1
                && request.toTime > request.fromTime && request.toTime - request.fromTime <= 60) {
            ArrayList<Integer> exact = new ArrayList<>();
            exact.add(request.fromTime);
            return exact;
        }
"""


def verified_runtime(root=ROOT):
    """Return a verified derived artifact, or None if it has not been prepared."""
    cache = root / ".cache/router"
    proof_path = cache / "instant-point-v1.json"
    if not proof_path.exists():
        return None
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    target = cache / "r5-instant-point-v1.jar"
    if (
        proof.get("policy") != POLICY
        or proof.get("base_sha256") != R5_SHA256
        or proof.get("source_sha256") != SOURCE_SHA
        or proof.get("license_sha256") != LICENSE_SHA
        or sha256(cache / "r5-v7.5.1-r5py-all.jar") != R5_SHA256
        or sha256(target) != proof.get("output_sha256")
    ):
        raise ValueError("Pinned exact-point runtime provenance mismatch")
    return target


def build(root=ROOT):
    existing = verified_runtime(root)
    if existing is not None:
        return existing
    cache = root / ".cache/router"
    original = cache / "r5-v7.5.1-r5py-all.jar"
    if sha256(original) != R5_SHA256:
        raise ValueError("Pinned R5 checksum mismatch")
    source = cache / "instant-point-source/McRaptorSuboptimalPathProfileRouter.java"
    license_path = source.with_name("LICENSE")
    download_verified(BASE + SOURCE, source, SOURCE_SHA)
    download_verified(BASE + "LICENSE", license_path, LICENSE_SHA)
    text = source.read_text(encoding="utf-8")
    if text.count(ANCHOR) != 1:
        raise ValueError("Unexpected pinned R5 point sampler")
    modified = text.replace(ANCHOR, ANCHOR + GUARD)
    target = cache / "r5-instant-point-v1.jar"
    if target.exists():
        raise ValueError("Unproven point runtime already exists; preserve it before rebuilding")
    with tempfile.TemporaryDirectory(dir=cache, prefix="instant-point-build-") as directory:
        build_dir = Path(directory)
        java = build_dir / source.name
        java.write_text(modified, encoding="utf-8")
        classes = build_dir / "classes"
        subprocess.run(
            [
                str(cache / "jdk-25.0.4.1+1/bin/javac.exe"),
                "--release",
                "17",
                "-g:none",
                "-encoding",
                "UTF-8",
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
            p.relative_to(classes).as_posix(): p.read_bytes() for p in classes.rglob("*.class")
        }
        prefix = "com/conveyal/r5/profile/McRaptorSuboptimalPathProfileRouter"
        if not replacements or not all(name.startswith(prefix) for name in replacements):
            raise ValueError("Unexpected classes in exact-point runtime build")
        replacements.update(
            {
                f"{EMBED}/McRaptorSuboptimalPathProfileRouter.java": modified.encode("utf-8"),
                f"{EMBED}/LICENSE": license_path.read_bytes(),
            }
        )
        with ZipFile(original) as source_jar, ZipFile(target, "w") as output:
            for info in source_jar.infolist():
                if info.filename not in replacements:
                    output.writestr(info, source_jar.read(info))
            for name, content in sorted(replacements.items()):
                info = ZipInfo(name, (2026, 10, 23, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                output.writestr(info, content)
    proof = {
        "policy": POLICY,
        "base_sha256": R5_SHA256,
        "source_url": BASE + SOURCE,
        "source_sha256": SOURCE_SHA,
        "license_url": BASE + "LICENSE",
        "license_sha256": LICENSE_SHA,
        "output_sha256": sha256(target),
        "changed_classes": sorted(name for name in replacements if name.endswith(".class")),
        "source_offer": f"Modified source and MIT notice are embedded in {EMBED}",
    }
    (cache / "instant-point-v1.json").write_text(
        json.dumps(proof, indent=2) + "\n", encoding="utf-8"
    )
    return verified_runtime(root)


if __name__ == "__main__":
    print(build())
