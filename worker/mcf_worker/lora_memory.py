"""Apply an artist profile's add-on (LoRA) from memory.

The add-on arrives through a pipe as zip bytes and is never written to disk.
It's merged into the model's weights before any 8-bit compression, because
ACE-Step can't load add-ons into a compressed model.
"""

import io
import json
import logging
import zipfile

log = logging.getLogger("mcf_worker.lora")

ADAPTER = "profile"
IGNORED_CONFIG = {"peft_type", "task_type", "base_model_name_or_path", "revision", "inference_mode",
                  "auto_mapping"}


def read(data: bytes) -> tuple[dict, dict]:
    from safetensors.torch import load as load_safetensors
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        config_name = next((n for n in names if n.endswith("adapter_config.json")), None)
        weights_name = next((n for n in names if n.endswith("adapter_model.safetensors")), None)
        if not config_name or not weights_name:
            raise ValueError("The profile doesn't contain a LoRA add-on.")
        config = json.loads(z.read(config_name))
        weights = load_safetensors(z.read(weights_name))
    return config, weights


def merge(decoder, data: bytes, scale: float):
    """Returns the decoder with the add-on merged into its weights."""
    from peft import LoraConfig, get_peft_model, set_peft_model_state_dict
    from peft.tuners.lora import LoraLayer

    config, weights = read(data)
    fields = LoraConfig.__dataclass_fields__
    lora_config = LoraConfig(**{k: v for k, v in config.items() if k in fields and k not in IGNORED_CONFIG})
    model = get_peft_model(decoder, lora_config, adapter_name=ADAPTER)
    outcome = set_peft_model_state_dict(model, weights, adapter_name=ADAPTER)
    unexpected = getattr(outcome, "unexpected_keys", []) or []
    if unexpected:
        raise ValueError(f"The add-on doesn't match this model ({len(unexpected)} unexpected weights).")
    for module in model.modules():
        if isinstance(module, LoraLayer) and ADAPTER in module.scaling:
            module.scaling[ADAPTER] *= scale
    merged = model.merge_and_unload()
    del weights
    log.info("Merged the profile add-on at strength %.2f", scale)
    return merged
