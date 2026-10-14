"""List every package installed in the AI runtime, with its license, for MCF's credits."""

import importlib.metadata as md
import re
from pathlib import Path


def license_of(meta) -> str:
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::") and not c.endswith("OSI Approved")]
    if classifiers:
        return ", ".join(classifiers)
    text = (meta.get("License") or "").strip()
    if text and text.upper() != "UNKNOWN":
        return text.splitlines()[0][:120]
    return "See the package's own documentation"


def write(path: Path, engine_name: str) -> int:
    dists = sorted({d.metadata["Name"].lower(): d for d in md.distributions() if d.metadata["Name"]}.values(),
                   key=lambda d: d.metadata["Name"].lower())
    lines = [f"AI software installed for {engine_name}", "",
             "MCF installed these packages into its runtime folder. This list is generated",
             "from the installed packages each time the runtime is installed or repaired.", ""]
    for dist in dists:
        meta = dist.metadata
        author = (meta.get("Author") or meta.get("Maintainer") or "").strip('"')
        if not author:
            author = re.sub(r"\s*<[^>]*>", "", meta.get("Author-email") or "").strip(" ,")
        lines.append(f"{meta['Name']} {dist.version}" + (f", by {author}" if author else ""))
        lines.append(f"  {license_of(meta)}.")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return len(dists)
