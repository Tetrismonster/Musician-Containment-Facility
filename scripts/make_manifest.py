"""Regenerate the file lists in mcf/data/manifest.json.

Downloads the small files and reads the large files' SHA-256 hashes from the
Hugging Face API, for the exact revisions pinned below. Run from the project
root when a pinned revision changes:

    .venv\\Scripts\\python scripts\\make_manifest.py
"""

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "mcf" / "data" / "manifest.json"

# (component id, repo, revision, folders or files to include ("" for everything),
#  destination folder, keep the repo's folder names in the destination)
SOURCES = [
    ("ace-core", "ACE-Step/Ace-Step1.5", "19671f406d603126926c1b7e2adc169acbcade22",
     ("acestep-v15-turbo/", "vae/", "Qwen3-Embedding-0.6B/"), "checkpoints/", True),
    ("ace-planner-large", "ACE-Step/Ace-Step1.5", "19671f406d603126926c1b7e2adc169acbcade22",
     ("acestep-5Hz-lm-1.7B/",), "checkpoints/", True),
    ("ace-planner-small", "ACE-Step/acestep-5Hz-lm-0.6B", "148d8ea0225bdab342ee1ae3a354275ccd60ca80",
     ("",), "checkpoints/acestep-5Hz-lm-0.6B/", False),
    ("whisper", "openai/whisper-large-v3-turbo", "41f01f3fe87f28c78e2fbf8b568835947dd65ed9",
     ("",), "whisper-large-v3-turbo/", False),
    ("heartmula-core", "HeartMuLa/HeartMuLaGen", "9906b2bcd4598772a32cad4aec0760170fe0d177",
     ("",), "ckpt/", False),
    ("heartmula-core", "HeartMuLa/HeartMuLa-oss-3B-happy-new-year", "41f6fc68490e11dc43fdabaa6b5767946408c903",
     ("",), "ckpt/HeartMuLa-oss-3B/", False),
    ("heartmula-core", "HeartMuLa/HeartCodec-oss-20260123", "f889dab0532cfa4bf459f2a3367eb6d346b8eeda",
     ("",), "ckpt/HeartCodec-oss/", False),
    ("yue2-core", "m-a-p/YuE2-3B", "c044757a011169583f363168348ae380946efff8",
     ("",), "models/YuE2-3B/", False),
    ("yue2-core", "m-a-p/YuE2-Vae", "152733a19ad43aa67e367f9b5503ef8075bb5126",
     ("",), "models/YuE2-Vae/", False),
    ("mulacover-core", "HeartMuLa/MuLaCover", "bbbaef2b31835c2ef5172ff528dbe325230d46fb",
     ("",), "ckpt/MuLaCover/", False),
    ("mulacover-core", "HeartMuLa/HeartCodec-oss-20260123", "f889dab0532cfa4bf459f2a3367eb6d346b8eeda",
     ("",), "ckpt/HeartCodec-oss/", False),
    ("mulacover-core", "Qwen/Qwen3-Embedding-0.6B", "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
     ("",), "ckpt/Qwen3-Embedding-0.6B/", False),
    # Only the diffusers components. The repository also holds a second copy of the
    # model for MiniMax's own code, including pickle files that can run code; MCF
    # never downloads those.
    ("minimax-core", "MiniMaxAI/MiniMax-Music3", "fbdf52fbaaca799592917417eb05f1899f1255ec",
     ("condition_encoder/", "language_model/", "rvq_depth_decoder/", "scheduler/", "tokenizer/", "transformer/",
      "vocoder/", "modular_model_index.json", "config.json"), "", True),
]
SKIP = {".gitattributes", "README.md", "SHA256SUMS"}
NEVER = (".py", ".pth", ".pt", ".bin", ".pkl", ".ckpt", ".whl", ".exe", ".dll", ".so")
# Samples and pictures in model repositories are not needed to run the model
SKIP_FOLDERS = ("assets/", "examples/", "figures/", "scripts/")


def fetch_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def sha256_url(url: str) -> str:
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as r:
        while chunk := r.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def files_for(repo, revision, prefixes, dest, keep_folders) -> list[dict]:
    tree = fetch_json(f"https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=true")
    result = []
    for entry in tree:
        path = entry["path"]
        if entry["type"] != "file" or path.rsplit("/", 1)[-1] in SKIP:
            continue
        # Model code and pickle files can run code when loaded. Code comes only from
        # the pinned engine sources, and weights only as safetensors or plain data.
        if path.endswith(NEVER) and not path.endswith("silence_latent.pt"):
            continue
        if path.startswith(SKIP_FOLDERS):
            continue
        prefix = next((p for p in prefixes if path == p or path.startswith(p)), None)
        if prefix is None:
            continue
        url = f"https://huggingface.co/{repo}/resolve/{revision}/{path}"
        lfs = entry.get("lfs")
        sha = lfs["oid"] if lfs else sha256_url(url)
        rel = path if keep_folders else path[len(prefix):]
        result.append({"path": dest + rel, "url": url, "size": entry["size"], "sha256": sha})
        print(f"  {dest + rel}  {entry['size']:,}")
    return result


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    components: dict[str, list] = {}
    for cid, repo, revision, prefixes, dest, keep in SOURCES:
        print(cid, repo)
        components.setdefault(cid, []).extend(files_for(repo, revision, prefixes, dest, keep))
    groups = [c for m in manifest["models"] for c in m["components"]] + manifest["cover_tools"]["components"]
    for component in groups:
        if component["id"] in components:
            component["files"] = components[component["id"]]
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("Updated", MANIFEST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
