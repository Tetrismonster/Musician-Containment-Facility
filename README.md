# Musician Containment Facility (MCF)

An accessible Windows app for generating music with AI models that run on your own computer. It is designed for screen reader users first.

## What it does

1. Makes songs with five models, each installed separately: ACE-Step 1.5 (new songs, covers, extensions, variations, replaced sections, reference audio), HeartMuLa 3B (new songs with strong lyric control), YuE2 3B (new songs, and covers from a written score of the melody), MuLaCover (covers from a lead sheet of the melody and chords; noncommercial use only), and MiniMax Music 3 (full songs up to 5 minutes). The hardware scan decides how each one runs on the computer, and MCF states how long songs will take.
2. Covers: analyses a song (vocals, lyrics, tempo, key, chords and sections), then performs it again in a new style. Checkboxes control what the cover follows, and the melody is always followed. MuLaCover follows the melody most closely; the original recording never reaches it.
3. Artist profiles: trains an add-on on a set of songs and keeps it encrypted inside the MCF folder. Training needs an NVIDIA RTX 30 series or newer card.
4. An onboarding wizard, a hardware scan with plain verdicts, a model manager, a library with keyboard playback and Save As, and time estimates that learn from this computer.

## Running from source

1. Create the environment: `python -m venv .venv`
2. Install the dependencies: `.venv\Scripts\python -m pip install -r requirements-dev.txt`
3. Run MCF without a console window: `.venv\Scripts\pythonw -m mcf`

The first start opens the onboarding wizard, which installs the AI software and a model into folders inside MCF.

## Folders

Everything MCF writes stays inside its own folder, except where you choose otherwise (the models folder and the exported audio folder).

1. runtime: the AI software, a private Python and the libraries, about 9 GB.
2. models: the models, unless you chose another folder.
3. library: your songs and the library database.
4. profiles: artist profiles, encrypted.
5. settings: preferences, presets, the hardware scan, learned time estimates, and the profile key. Deleting this folder makes every profile unusable.
6. temp: working files, deleted after every job and on every start.
7. logs: troubleshooting logs.

Removing MCF means deleting its folder.

## Tests

`.venv\Scripts\python -m pytest`

## Building the portable app

`.venv\Scripts\python scripts\build.py`

This regenerates the credits list, then creates `dist\MCF`. Copy that whole folder anywhere and run `MCF.exe`.

## Maintainer scripts

1. `scripts\make_manifest.py` refreshes the model file list and fingerprints for the pinned model revisions.
2. `scripts\sign_manifest.py` creates a signing key and signs the model list for online updates.
3. `scripts\credits.py` regenerates the third-party credits. The build runs it automatically.
4. `scripts\make_release.py KEY_FILE` packages and signs the built app as `build\release\MCF-X.Y.Z.zip` for a GitHub release tagged `vX.Y.Z`.

## Updates

MCF can update itself from GitHub releases (Help, Check for updates). It is switched off until `RELEASES_URL` in `mcf\app_update.py` and the public key in `mcf\manifest_update.py` are filled in. The releases must be public, because MCF has no GitHub login. An update is accepted only when its download matches GitHub's SHA-256 digest and every file matches a list signed with the MCF project's key. Only `MCF.exe` and `_internal` are replaced, and the old files are restored if that fails.

The updater is adapted from [Sensor Readout](https://github.com/OnjLouis/accessible-sensor-readout) by Andre Louis, used with permission under the MIT license.

## License

MIT. See LICENSE, and the licenses folder for third-party software. Help, Licenses and attribution in the app also lists the AI software installed in the runtime.
