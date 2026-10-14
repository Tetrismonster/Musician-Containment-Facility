"""Tests for estimates, profiles, recommendations, the library, presets and safety checks."""

import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from mcf import downloads, estimates, lyrics, models, paths, presets, recommend, runtime
from mcf.hardware import Gpu, Hardware
from mcf.library import Library
from mcf.profiles import ProfileError, ProfileStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def manifest():
    return json.loads((Path(__file__).resolve().parent.parent / "mcf" / "data" / "manifest.json").read_text())


# Estimates


def test_estimates_start_wide_and_learn(tmp_path):
    est = estimates.Estimator(tmp_path / "e.json")
    first = est.estimate("m", "generate", "auto", "hw", 120, 240)
    assert first.runs == 0 and first.low < 240 < first.high
    for _ in range(6):
        est.record("m", "generate", "auto", "hw", 60, 200)  # This computer is slower than the baseline
    learned = est.estimate("m", "generate", "auto", "hw", 60, 120)
    assert learned.runs == 6
    assert learned.low < 200 < learned.high
    assert learned.high - learned.low < first.high - first.low


def test_estimates_ignore_outliers(tmp_path):
    est = estimates.Estimator(tmp_path / "e.json")
    for _ in range(8):
        est.record("m", "generate", "auto", "hw", 60, 100)
    est.record("m", "generate", "auto", "hw", 60, 5000)  # The computer was busy with something else
    assert est.estimate("m", "generate", "auto", "hw", 60, 100).high < 200


def test_estimates_reject_tampered_values(tmp_path):
    path = tmp_path / "e.json"
    good = {"model": "m", "task": "generate", "mode": "auto", "hw": "h", "size": 60, "total": 100}
    bad = [dict(good, total=-5), dict(good, total=10**12), dict(good, task="rm -rf"), "junk", dict(good, size=0)]
    path.write_text(json.dumps({"version": 1, "runs": [good] + bad}))
    est = estimates.Estimator(path)
    est.load()
    assert len(est.runs) == 1


def test_estimates_set_aside_damaged_file(tmp_path):
    path = tmp_path / "e.json"
    path.write_text("{not json")
    est = estimates.Estimator(path)
    est.load()
    assert est.problem and est.runs == []
    assert (tmp_path / "e.damaged.json").exists()


def test_estimate_wording():
    assert estimates.describe(estimates.Estimate(170, 250, 1)) == "about 3 to 4 minutes"
    assert estimates.describe(estimates.Estimate(30, 40, 1)) == "about 30 to 40 seconds"


# Profiles


def _adapter(tmp_path) -> Path:
    folder = tmp_path / "adapter"
    folder.mkdir()
    (folder / "adapter_config.json").write_text('{"r": 4}')
    (folder / "adapter_model.safetensors").write_bytes(b"weights" * 100)
    return folder


def test_profile_round_trip_and_no_plain_data(app_dir, tmp_path):
    store = ProfileStore(app_dir / "profiles")
    profile = store.create("My Band", "ace-step-1.5", _adapter(tmp_path), style="indie rock")
    raw = (app_dir / "profiles" / f"{profile.id}.mcfp").read_bytes()
    assert b"My Band" not in raw and b"weights" not in raw and b"adapter_config" not in raw
    listed, damaged = store.list()
    assert [p.name for p in listed] == ["My Band"] and not damaged
    _p, data = store.adapter(profile.id)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.read("adapter_model.safetensors") == b"weights" * 100


def test_profile_tampering_is_detected(app_dir, tmp_path):
    store = ProfileStore(app_dir / "profiles")
    profile = store.create("My Band", "ace-step-1.5", _adapter(tmp_path))
    path = app_dir / "profiles" / f"{profile.id}.mcfp"
    data = bytearray(path.read_bytes())
    data[-10] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(ProfileError):
        store.adapter(profile.id)


def test_profiles_folder_alone_is_useless(app_dir, tmp_path):
    store = ProfileStore(app_dir / "profiles")
    store.create("My Band", "ace-step-1.5", _adapter(tmp_path))
    (app_dir / "settings" / "profiles.key").unlink()  # As if only the profiles folder was copied
    listed, damaged = store.list()
    assert listed == [] and len(damaged) == 1


def test_profile_rename_duplicate_delete(app_dir, tmp_path):
    store = ProfileStore(app_dir / "profiles")
    profile = store.create("A", "ace-step-1.5", _adapter(tmp_path))
    profile.name = "B"
    store.update(profile)
    copy = store.duplicate(profile.id, "C")
    assert sorted(p.name for p in store.list()[0]) == ["B", "C"]
    store.delete(copy.id)
    assert [p.name for p in store.list()[0]] == ["B"]


# Recommendations


def hw(vendor="nvidia", vram=6.0, compute=7.5, ram=32.0, name="Card"):
    gpus = [Gpu(name=name, vendor=vendor, vram_gb=vram, compute=compute if vendor == "nvidia" else 0)] if vendor else []
    return Hardware(gpus=gpus, ram_gb=ram, cpu="CPU", cores=8)


def test_verdicts(manifest):
    model = manifest["models"][0]
    assert recommend.assess(model, hw(vram=24, compute=8.9)).mode == "gpu"
    six_gb = recommend.assess(model, hw())
    assert six_gb.mode == "offload" and six_gb.planner == "ace-planner-small"
    old = recommend.assess(model, hw(compute=6.1, name="GTX 1060"))
    assert old.mode == "cpu" and any("too old" in r for r in old.reasons)
    amd = recommend.assess(model, hw(vendor="amd", vram=16))
    assert amd.mode == "cpu" and any("NVIDIA" in r for r in amd.reasons)
    nothing = recommend.assess(model, hw(vendor=None, ram=8))
    assert nothing.mode is None and nothing.verdict == "no"
    assert "can't run" in recommend.overall([nothing])


def test_training_needs_ampere(manifest):
    model = manifest["models"][0]
    assert not recommend.assess_training(model, hw()).possible
    ok = recommend.assess_training(model, hw(vram=12, compute=8.6))
    assert ok.possible and ok.settings["optimizer"] == "adafactor"


# Library and export


def test_library_add_update_delete(tmp_path):
    lib = Library(tmp_path / "lib")
    audio = tmp_path / "out.flac"
    sf.write(audio, np.zeros((48000, 2), dtype=np.float32), 48000)
    song = lib.add(audio, "Song", "ACE-Step 1.5", "cover", 1.0, "folk", "la", {"seed": 1},
                   {"original_title": "Orig", "writers": "W", "original_artist": "A"})
    assert not audio.exists() and lib.path(song).exists()
    song.title = "Renamed"
    lib.update(song)
    assert lib.get(song.id).title == "Renamed"
    assert lib.unique_title("Renamed") == "Renamed 2"
    from mcf import audio_export
    line = audio_export.credit_line(song)
    assert line.startswith("Cover of Orig, written by W, originally by A.")
    for fmt in ("wav", "flac", "mp3", "ogg"):
        dest = tmp_path / f"x{audio_export.FORMATS[fmt][2]}"
        audio_export.export(lib.path(song), dest, fmt, song)
        with sf.SoundFile(dest) as f:
            assert f.comment == line and f.title == "Renamed"
    lib.delete(song)
    assert lib.all() == [] and not lib.path(song).exists()
    lib.close()


def test_presets(tmp_path):
    store = presets.PresetStore(tmp_path / "p")
    store.save("Funk / 1", {"style": "funk"})
    assert store.names() == ["Funk / 1"]
    assert store.load("Funk / 1") == {"style": "funk"}
    store.delete("Funk / 1")
    assert store.names() == []
    builtin = presets.builtin_cover_presets()
    assert all(p["follow"]["melody"] for p in builtin.values())


def test_lyrics_for_ace_step():
    parsed = lyrics.parse("{language: French}\n{profile: My Band}\n[Verse: soft]\nBonjour")
    text, ignored = lyrics.for_ace_step(parsed)
    assert text == "[Verse]\nBonjour" and ignored == ["[Verse: soft]"]
    assert lyrics.language_code(parsed.settings["language"]) == "fr"


# Safety


def test_zip_extraction_rejects_escaping_paths(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("top/../../escaped.txt", "x")
    with zipfile.ZipFile(archive) as z, pytest.raises(runtime.RuntimeError_):
        runtime._safe_extract(z, tmp_path / "target", strip_top=True)
    assert not (tmp_path / "escaped.txt").exists()


def test_downloads_refuse_http(tmp_path):
    with pytest.raises(downloads.DownloadError):
        downloads.download("http://example.com/x", tmp_path / "x", "0" * 64)


def test_manifest_files_are_pinned(manifest):
    every = [f for m in manifest["models"] for c in m["components"] for f in c["files"]]
    every += [f for c in manifest["cover_tools"]["components"] for f in c["files"]]
    assert every
    for f in every:
        assert f["url"].startswith("https://") and len(f["sha256"]) == 64 and f["size"] > 0
        assert "/resolve/main/" not in f["url"]  # Pinned to a revision, never a moving branch
        assert ".." not in f["path"]
    for model in manifest["models"]:
        assert (Path(__file__).resolve().parent.parent / "licenses" / model["license"]["file"]).exists()


def test_isolated_env_keeps_files_inside_mcf(app_dir):
    env = runtime.isolated_env()
    for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "HF_HOME", "TORCH_HOME",
                 "CUDA_CACHE_PATH", "TRITON_CACHE_DIR", "UV_CACHE_DIR", "PIP_CACHE_DIR"):
        assert Path(env[name]).is_relative_to(app_dir), name
    assert env["UV_PYTHON_INSTALL_REGISTRY"] == "0" and env["UV_NO_MODIFY_PATH"] == "1"


def test_sweep_temp_removes_leftovers(app_dir):
    from mcf import jobs
    leftover = paths.temp_dir() / "job-abc" / "tmp"
    leftover.mkdir(parents=True)
    (leftover / "stem.wav").write_bytes(b"x")
    jobs.sweep_temp()
    assert list(paths.temp_dir().iterdir()) == []


# Covers


def test_cover_strength_and_ranges():
    from mcf_worker import cover_source
    everything = {a: True for a in presets.COVER_ASPECTS}
    assert cover_source.strength(dict(everything, instrumentation=False)) == 1.0
    assert cover_source.strength(dict(everything, instrumentation=False, phrasing=False)) == 0.9
    analysis = {"duration": 100.0, "sections": [{"start": 0, "end": 30}, {"start": 30, "end": 60},
                                                  {"start": 60, "end": 100}]}
    keep = cover_source._keep_ranges({"analysis": analysis, "follow": dict(everything, structure=False),
                                      "sections": [0, 2]})
    assert keep == [(0, 30), (60, 100)]
    short = cover_source._keep_ranges({"analysis": analysis, "follow": dict(everything, length=False),
                                       "max_seconds": 45})
    assert short == [(0.0, 45.0)]


def test_manifest_downloads_no_code(manifest):
    every = [f for m in manifest["models"] for c in m["components"] for f in c["files"]]
    assert not [f for f in every if f["path"].endswith((".py", ".pyc", ".exe", ".dll"))]


def test_every_model_is_complete(manifest):
    for model in manifest["models"]:
        assert model["engine"] in manifest["engines"], model["id"]
        assert set(model["preference"]) <= set(model["modes"]), model["id"]
        for mode in model["preference"]:
            assert mode in model["timing"]["load_seconds"] and mode in model["timing"]["seconds_per_audio_second"]
        assert (Path(__file__).resolve().parent.parent / "licenses" / model["license"]["file"]).exists()
        assert set(model["modes_supported"]) <= {"new", "cover", "extend", "variation", "inpaint", "reference"}
        assert model["components"][0]["files"], model["id"]
    for engine in manifest["engines"].values():
        for source in engine["sources"]:
            assert source["url"].startswith("https://") and len(source["tree_sha256"]) == 64
        if engine.get("project"):
            folder = Path(__file__).resolve().parent.parent / "mcf" / "data" / "engines" / engine["project"]
            assert (folder / "pyproject.toml").exists() and (folder / "uv.lock").exists()


def test_verdicts_follow_the_computer(manifest):
    """Each model's verdict comes from the computer it runs on, not from one reference machine."""
    by_id = {m["id"]: m for m in manifest["models"]}
    big = hw(vram=24, compute=8.9, ram=64)
    for model in manifest["models"]:
        assert recommend.assess(model, big).mode == "gpu", model["id"]
    old_card = hw(vram=6, compute=7.5, ram=32)
    assert recommend.assess(by_id["yue2-3b"], old_card).mode == "cpu"  # No bfloat16 on RTX 20 cards
    assert recommend.assess(by_id["heartmula-3b"], old_card).mode == "compressed"
    small = hw(vendor=None, ram=16)
    assert recommend.assess(by_id["minimax-music3"], small).mode is None
    assert recommend.assess(by_id["ace-step-1.5"], small).mode == "cpu"
