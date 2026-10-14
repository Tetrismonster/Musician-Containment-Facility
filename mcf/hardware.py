"""Hardware scan. Quick, because it reads Windows directly and loads no AI software.

Nothing found here is sent anywhere.
"""

import ctypes
import hashlib
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from mcf.i18n import _

log = logging.getLogger(__name__)

GPU_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


@dataclass
class Gpu:
    name: str
    vendor: str  # nvidia, amd, intel or other
    vram_gb: float
    driver: str = ""
    compute: float = 0.0  # CUDA compute capability, NVIDIA only

    def describe(self) -> str:
        return _("{name} with {vram:.0f} GB of graphics memory").format(name=self.name, vram=self.vram_gb)


@dataclass
class Hardware:
    gpus: list[Gpu] = field(default_factory=list)
    ram_gb: float = 0.0
    cpu: str = ""
    cores: int = 0

    @property
    def best_gpu(self) -> Gpu | None:
        nvidia = [g for g in self.gpus if g.vendor == "nvidia"]
        pool = nvidia or self.gpus
        return max(pool, key=lambda g: g.vram_gb) if pool else None

    def fingerprint(self) -> str:
        """Changes when the hardware or graphics driver changes."""
        gpu = self.best_gpu
        text = f"{gpu.name if gpu else ''}|{gpu.driver if gpu else ''}|{round(self.ram_gb)}|{self.cpu}"
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Hardware":
        gpus = [Gpu(**g) for g in data.get("gpus", []) if isinstance(g, dict)]
        return cls(gpus=gpus, ram_gb=float(data.get("ram_gb", 0)), cpu=str(data.get("cpu", "")),
                   cores=int(data.get("cores", 0)))

    def summary(self) -> str:
        lines = []
        if self.gpus:
            for gpu in self.gpus:
                lines.append(_("Graphics card: {gpu}.").format(gpu=gpu.describe()))
        else:
            lines.append(_("Graphics card: none found."))
        lines.append(_("System memory (RAM): {ram:.0f} GB.").format(ram=self.ram_gb))
        lines.append(_("Processor: {cpu}, {cores} threads.").format(cpu=self.cpu or _("unknown"), cores=self.cores))
        return "\n".join(lines)


def _vendor(name: str, provider: str) -> str:
    text = f"{name} {provider}".lower()
    for vendor in ("nvidia", "amd", "intel"):
        if vendor in text or (vendor == "amd" and ("radeon" in text or "advanced micro" in text)):
            return vendor
    return "other"


def _registry_gpus() -> list[Gpu]:
    import winreg
    gpus = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, GPU_CLASS_KEY)
    except OSError:
        return gpus
    with root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if not sub.isdigit():
                continue
            try:
                with winreg.OpenKey(root, sub) as key:
                    values = {}
                    for name in ("DriverDesc", "ProviderName", "DriverVersion",
                                 "HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                        try:
                            values[name] = winreg.QueryValueEx(key, name)[0]
                        except OSError:
                            pass
            except OSError:
                continue
            name = values.get("DriverDesc")
            if not name or "basic display" in name.lower() or "remote" in name.lower():
                continue
            memory = values.get("HardwareInformation.qwMemorySize") or values.get("HardwareInformation.MemorySize") or 0
            if isinstance(memory, bytes):
                memory = int.from_bytes(memory[:8], "little")
            gpus.append(Gpu(name=name, vendor=_vendor(name, values.get("ProviderName", "")),
                            vram_gb=round(int(memory) / 1024**3, 1), driver=str(values.get("DriverVersion", ""))))
    return gpus


def _nvidia_smi() -> list[dict]:
    exe = shutil.which("nvidia-smi") or str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe")
    if not Path(exe).exists():
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total,driver_version,compute_cap",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    result = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 4:
            try:
                result.append({"name": parts[0], "vram_gb": round(float(parts[1]) / 1024, 1),
                               "driver": parts[2], "compute": float(parts[3])})
            except ValueError:
                continue
    return result


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _ram_gb() -> float:
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return round(status.ullTotalPhys / 1024**3, 1)


def _cpu_name() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except OSError:
        return ""


def scan() -> Hardware:
    if sys.platform != "win32":
        return Hardware(cores=os.cpu_count() or 0)
    gpus = _registry_gpus()
    for smi in _nvidia_smi():
        match = next((g for g in gpus if g.vendor == "nvidia" and g.name == smi["name"]), None)
        if match is None:
            match = Gpu(name=smi["name"], vendor="nvidia", vram_gb=0)
            gpus.append(match)
        # nvidia-smi is more accurate than the registry
        match.vram_gb = smi["vram_gb"]
        match.driver = smi["driver"]
        match.compute = smi["compute"]
    hardware = Hardware(gpus=gpus, ram_gb=_ram_gb(), cpu=_cpu_name(), cores=os.cpu_count() or 0)
    log.info("Hardware scan: %s", hardware)
    return hardware


# Drives

DRIVE_REMOVABLE = 2
DRIVE_REMOTE = 4


def free_bytes(folder: Path) -> int | None:
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def drive_kind(folder: Path) -> str:
    """fixed, removable, network or unknown."""
    if sys.platform != "win32":
        return "fixed"
    anchor = Path(folder).anchor
    if anchor.startswith("\\\\"):
        return "network"
    kind = ctypes.windll.kernel32.GetDriveTypeW(anchor)
    return {DRIVE_REMOVABLE: "removable", DRIVE_REMOTE: "network", 3: "fixed"}.get(kind, "unknown")


# The last scan, kept so MCF doesn't need to scan on every start


def _scan_file() -> Path:
    from mcf import paths
    return paths.settings_dir() / "hardware.json"


def save_scan(hardware: Hardware) -> None:
    import json
    path = _scan_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(hardware.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)


def load_scan() -> Hardware | None:
    import json
    try:
        data = json.loads(_scan_file().read_text(encoding="utf-8"))
        return Hardware.from_dict(data) if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None
