"""Artist profiles: a trained add-on (LoRA) and its settings, in one encrypted file.

Each profile is profiles/<id>.mcfp. The key lives in the settings folder,
so copying only the profiles folder gives nothing usable, and the file isn't
recognisable as a LoRA. Profiles are decrypted in memory only: MCF passes the
add-on to the worker through a pipe, never through a file.

This stops casual copying. MCF is open source, so a determined programmer
with the whole MCF folder could still extract a profile.
"""

import io
import json
import logging
import os
import secrets
import struct
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mcf import paths

log = logging.getLogger(__name__)

MAGIC = b"MCFP\x01"
MAX_ADAPTER_BYTES = 2 * 1024**3


class ProfileError(Exception):
    pass


@dataclass
class Profile:
    id: str
    name: str
    model: str  # Model id the add-on was trained for
    created: float
    strength: float = 1.0
    style: str = ""
    notes: str = ""
    songs: int = 0
    rights_confirmed: float = 0.0
    extra: dict = field(default_factory=dict)


def _key_file() -> Path:
    return paths.settings_dir() / "profiles.key"


def _load_key(create: bool) -> bytes | None:
    path = _key_file()
    try:
        key = path.read_bytes()
        if len(key) == 32:
            return key
        raise ProfileError("The profile key is damaged.")
    except FileNotFoundError:
        if not create:
            return None
    key = AESGCM.generate_key(bit_length=256)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(key)
    tmp.replace(path)
    return key


def pack_adapter(folder: Path) -> bytes:
    """The adapter folder as zip bytes, built in memory."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(folder).as_posix())
    return buffer.getvalue()


class ProfileStore:
    def __init__(self, folder: Path | None = None):
        self.folder = folder or paths.profiles_dir()

    def _path(self, profile_id: str) -> Path:
        if not profile_id.isalnum():
            raise ProfileError("Invalid profile id.")
        return self.folder / f"{profile_id}.mcfp"

    # Format: MAGIC | header length | header nonce | header | adapter nonce | adapter
    # Header and adapter are encrypted separately, so listing profiles doesn't
    # need to decrypt the large adapter.

    def _write(self, profile: Profile, adapter: bytes, key: bytes) -> None:
        aad = MAGIC + profile.id.encode()
        aes = AESGCM(key)
        header_nonce, adapter_nonce = os.urandom(12), os.urandom(12)
        header = aes.encrypt(header_nonce, json.dumps(asdict(profile)).encode(), aad)
        body = aes.encrypt(adapter_nonce, adapter, aad)
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self._path(profile.id)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(MAGIC + struct.pack("<I", len(header)) + header_nonce + header + adapter_nonce + body)
        tmp.replace(path)

    def _read(self, profile_id: str, with_adapter: bool) -> tuple[Profile, bytes | None]:
        key = _load_key(create=False)
        if key is None:
            raise ProfileError("The profile key is missing, so this profile can't be opened.")
        data = self._path(profile_id).read_bytes() if with_adapter else self._read_header_bytes(profile_id)
        if not data.startswith(MAGIC):
            raise ProfileError("Not an MCF profile.")
        aad = MAGIC + profile_id.encode()
        aes = AESGCM(key)
        offset = len(MAGIC)
        (header_len,) = struct.unpack_from("<I", data, offset)
        offset += 4
        header_nonce = data[offset:offset + 12]
        offset += 12
        try:
            header = json.loads(aes.decrypt(header_nonce, data[offset:offset + header_len], aad))
            offset += header_len
            adapter = None
            if with_adapter:
                adapter_nonce = data[offset:offset + 12]
                adapter = aes.decrypt(adapter_nonce, data[offset + 12:], aad)
        except InvalidTag:
            raise ProfileError("This profile has been changed or damaged, so MCF won't load it.")
        known = {f for f in Profile.__dataclass_fields__}
        profile = Profile(**{k: v for k, v in header.items() if k in known})
        if profile.id != profile_id:
            raise ProfileError("This profile has been changed or damaged, so MCF won't load it.")
        return profile, adapter

    def _read_header_bytes(self, profile_id: str) -> bytes:
        with open(self._path(profile_id), "rb") as f:
            start = f.read(len(MAGIC) + 4)
            if len(start) < len(MAGIC) + 4:
                return b""
            (header_len,) = struct.unpack_from("<I", start, len(MAGIC))
            if header_len > 1_000_000:
                raise ProfileError("This profile has been changed or damaged, so MCF won't load it.")
            return start + f.read(12 + header_len)

    # Public API

    def list(self) -> tuple[list[Profile], list[str]]:
        """Readable profiles, and the file names of damaged ones."""
        profiles, damaged = [], []
        if not self.folder.exists():
            return profiles, damaged
        for path in sorted(self.folder.glob("*.mcfp")):
            try:
                profiles.append(self._read(path.stem, with_adapter=False)[0])
            except (ProfileError, OSError, ValueError, struct.error):
                damaged.append(path.name)
        profiles.sort(key=lambda p: p.name.lower())
        return profiles, damaged

    def create(self, name: str, model: str, adapter_folder: Path, **details) -> Profile:
        adapter = pack_adapter(adapter_folder)
        if len(adapter) > MAX_ADAPTER_BYTES:
            raise ProfileError("The trained add-on is too large.")
        profile = Profile(id=uuid.uuid4().hex, name=name, model=model, created=time.time(), **details)
        self._write(profile, adapter, _load_key(create=True))
        return profile

    def adapter(self, profile_id: str) -> tuple[Profile, bytes]:
        profile, adapter = self._read(profile_id, with_adapter=True)
        return profile, adapter

    def update(self, profile: Profile) -> None:
        """Save changed details. The adapter is kept as it is."""
        _old, adapter = self._read(profile.id, with_adapter=True)
        self._write(profile, adapter, _load_key(create=False))

    def duplicate(self, profile_id: str, new_name: str) -> Profile:
        profile, adapter = self._read(profile_id, with_adapter=True)
        copy = Profile(**{**asdict(profile), "id": uuid.uuid4().hex, "name": new_name, "created": time.time()})
        self._write(copy, adapter, _load_key(create=False))
        return copy

    def delete(self, profile_id: str) -> None:
        path = self._path(profile_id)
        # Overwrite before deleting, so the encrypted data doesn't linger in the file's old clusters
        try:
            size = path.stat().st_size
            with open(path, "r+b") as f:
                f.write(secrets.token_bytes(min(size, 1024 * 1024)))
        except OSError:
            pass
        path.unlink(missing_ok=True)
