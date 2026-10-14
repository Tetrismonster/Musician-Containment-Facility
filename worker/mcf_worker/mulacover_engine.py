"""MuLaCover adapter: covers from a lead sheet.

MuLaCover is HeartMuLa with an extra input for symbolic music: a melody
piano roll and chord tones on a sixteenth-note grid. MCF builds that lead
sheet from its own cover analysis (lead_sheet.py), so the original recording
never reaches the model: the model sings the melody in a new voice instead of
copying the original singer. Loading follows HeartMuLa (heart_engine.py):

- gpu: the whole model on the graphics card in half precision.
- compressed: 8-bit or 4-bit weights on the graphics card (bitsandbytes).
- offload: layers split between the graphics card and system memory.
- cpu: everything on the processor in full precision.
"""

import gc
import logging
import os
import time
from pathlib import Path

from mcf_worker import lead_sheet, protocol
from mcf_worker.ace_engine import WorkerError, vram_gb
from mcf_worker.heart_engine import HeartEngine

log = logging.getLogger("mcf_worker.mulacover")

FRAME_MS = 80
RATE = 48000
MAX_SECONDS = 300  # The model's limit


def style_tags(caption: str) -> str:
    """MuLaCover's style line. Free text goes in the genre field when it isn't already in the model's format."""
    caption = caption.strip()
    if ":[" in caption:
        return caption
    words = [w.strip() for w in caption.replace(";", ",").split(",") if w.strip()] or ["pop"]
    return "genre:[" + ",".join(words) + "]"


def _match_cache_precision() -> None:
    """Compressed layers return full precision under MuLaCover's half-precision autocast, but the
    attention cache is half precision. Store new entries at the cache's precision."""
    from torchtune.modules.kv_cache import KVCache
    if getattr(KVCache.update, "mcf_patched", False):
        return
    original = KVCache.update

    def update(self, k_val, v_val):
        return original(self, k_val.to(self.k_cache.dtype), v_val.to(self.v_cache.dtype))

    update.mcf_patched = True
    KVCache.update = update


def _reuse_lead_sheet_work() -> None:
    """Process the lead sheet once per song instead of at every step.

    At each step (12.5 a second), MuLaCover runs its lead-sheet encoder and the
    cross-attention keys and values over the whole lead sheet, in 8 layers. The
    lead sheet doesn't change during a song, so the results are kept from the
    first step and reused. The output is the same; a 5-minute song was about
    ten times slower without this.
    """
    import torch
    from mulacover import modeling as m
    if getattr(m, "mcf_patched", False):
        return

    def layer_forward(self, hidden_states, pianoroll, drum_pianoroll, chord, context_mask, previous_context,
                      input_pos):
        context = getattr(self, "mcf_context", None)
        if context is None:
            current = self.adaptor(pianoroll, drum_pianoroll, chord, context_mask)
            context = current if previous_context is None else previous_context + current
            self.mcf_context = context
        return self.cross_attn(hidden_states, context, context_mask, input_pos), context

    def block_forward(self, hidden_states, context, mask, input_pos):
        residual = hidden_states
        hidden_states = self.norm(hidden_states)
        gate_context = getattr(self, "mcf_gate_context", None)
        if gate_context is None:
            gate_context = self.mcf_gate_context = self.context_gate_proj(context)
        attention_output = self.attn(hidden_states, context, mask, input_pos=input_pos)
        gate_output = self.gate(self.x_gate_proj(hidden_states), gate_context, mask, input_pos=input_pos)
        return residual + torch.tanh(self.gate_proj(gate_output)) * attention_output

    def attention_forward(self, x, context, mask, input_pos=None):
        batch_size, query_len, _ = x.shape
        query = self.q_proj(x).view(batch_size, query_len, self.num_heads, self.head_dim)
        query = self.query_rope(query, input_pos=input_pos).transpose(1, 2)
        kv = getattr(self, "mcf_kv", None)
        if kv is None:
            context_len = context.shape[1]
            per_kv = self.num_heads // self.num_kv_heads
            key = self.k_proj(context).view(batch_size, context_len, self.num_kv_heads, self.head_dim)
            value = self.v_proj(context).view(batch_size, context_len, self.num_kv_heads, self.head_dim)
            key = self.key_rope(key)
            key = key.unsqueeze(3).expand(batch_size, context_len, self.num_kv_heads, per_kv, self.head_dim)
            value = value.unsqueeze(3).expand_as(key)
            key = key.reshape(batch_size, context_len, self.num_heads, self.head_dim).transpose(1, 2)
            value = value.reshape(batch_size, context_len, self.num_heads, self.head_dim).transpose(1, 2)
            kv = self.mcf_kv = (key, value)
        attention_mask = m._build_attention_mask(mask, x).unsqueeze(1).expand(-1, self.num_heads, -1, -1)
        out = torch.nn.functional.scaled_dot_product_attention(query, kv[0], kv[1], attn_mask=attention_mask)
        return self.output_proj(out.transpose(1, 2).contiguous().view(batch_size, query_len, -1))

    generate_frame = m.MuLaCover.generate_frame

    def generate_frame_fresh(self, *args, first_step: bool = False, **kwargs):
        if first_step:  # A new song: forget the previous song's lead sheet
            for module in self.modules():
                for name in ("mcf_context", "mcf_gate_context", "mcf_kv"):
                    if hasattr(module, name):
                        setattr(module, name, None)
        return generate_frame(self, *args, first_step=first_step, **kwargs)

    m.CrossAttentionLayer.forward = layer_forward
    m.CrossAttentionBlock.forward = block_forward
    m.CrossAttentionRoPE.forward = attention_forward
    m.MuLaCover.generate_frame = generate_frame_fresh
    m.mcf_patched = True


def _cache_shared_heads() -> None:
    """Keep the attention cache at its real size.

    MuLaCover's main model has 24 query heads that share 8 key/value heads.
    torchtune copies the 8 into 24 before caching them, so the cache for a
    5-minute song took 2.8 GB instead of 0.9 and overflowed a 6 GB card. This
    caches the 8 and lets PyTorch's attention share them (enable_gqa), which
    pairs heads the same way torchtune's copies do.
    """
    import torch
    from torchtune.modules import attention
    from torchtune.modules.kv_cache import KVCache
    mha = attention.MultiHeadAttention
    if getattr(mha, "mcf_patched", False):
        return
    original_forward = mha.forward

    def setup_cache(self, batch_size, dtype, max_seq_len):
        if self.kv_cache is None:
            self.kv_cache = KVCache(batch_size=batch_size, max_seq_len=max_seq_len, num_heads=self.num_kv_heads,
                                    head_dim=self.head_dim, dtype=dtype)
            self.cache_enabled = True

    def forward(self, x, y=None, *, mask=None, input_pos=None):
        if y is None or self.kv_cache is None or not self.cache_enabled or self.num_heads == self.num_kv_heads:
            return original_forward(self, x, y, mask=mask, input_pos=input_pos)
        b, s_x, _ = x.shape
        s_y = y.shape[1]
        q = self.q_proj(x).view(b, s_x, self.num_heads, self.head_dim)
        k = self.k_proj(y).view(b, s_y, self.num_kv_heads, self.head_dim)
        v = self.v_proj(y).view(b, s_y, self.num_kv_heads, self.head_dim)
        if self.pos_embeddings is not None:
            q = self.pos_embeddings(q, input_pos=input_pos)
            k = self.pos_embeddings(k, input_pos=input_pos)
        q, k, v = q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)
        if self.q_norm is not None:
            q = self.q_norm(q)
        if self.k_norm is not None:
            k = self.k_norm(k)
        k, v = self.kv_cache.update(k, v)
        out = torch.nn.functional.scaled_dot_product_attention(
            q, k, v, attn_mask=None if mask is None else mask[:, None, :, :], enable_gqa=True)
        return self.output_proj(out.transpose(1, 2).contiguous().view(b, s_x, -1))

    mha.setup_cache = setup_cache
    mha.forward = forward
    mha.mcf_patched = True


def _embedding_on_processor(embedding, device):
    """An embedding table kept in system memory. Each step looks up only a few entries, so this
    costs almost nothing, and frees graphics memory (about 1.2 GB for MuLaCover's two tables)."""
    import torch

    class EmbeddingOnProcessor(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.table = embedding.to("cpu")

        def forward(self, ids):
            return self.table(ids.to("cpu")).to(device)

    return EmbeddingOnProcessor()


class MuLaCoverEngine(HeartEngine):
    def __init__(self, job: dict):
        super().__init__(job)
        self.ckpt = Path(job["model_dir"]) / "ckpt"

    def load(self, use_planner: bool = True, lora: bytes | None = None, lora_scale: float = 1.0) -> None:
        protocol.stage("load")
        protocol.progress(0.0)
        if lora:
            raise WorkerError("profile_mismatch", "MuLaCover can't use artist profiles.")
        for name in ("MuLaCover/config.json", "MuLaCover/gen_config.json", "MuLaCover/tokenizer.json",
                     "HeartCodec-oss/config.json", "Qwen3-Embedding-0.6B/config.json"):
            if not (self.ckpt / name).exists():
                raise WorkerError("model_missing", f"Model files are missing: {name}")
        import torch
        from mulacover import MuLaCoverGenPipeline
        from mulacover.modeling import MuLaCover
        from mulacover._codec.modeling import HeartCodec

        mode = self._pick_mode(torch)
        if mode != "cpu" and not torch.cuda.is_available():
            raise WorkerError("no_gpu", "The graphics card can't be used by the AI software.")
        self.device = torch.device("cpu" if mode == "cpu" else "cuda")
        bf16 = self.device.type == "cuda" and torch.cuda.get_device_capability(0)[0] >= 8
        half = torch.bfloat16 if bf16 else torch.float16
        self.mula_dtype = torch.float32 if mode == "cpu" else half
        log.info("Loading MuLaCover: mode=%s dtype=%s", mode, self.mula_dtype)
        # The style encoder is small and runs once, so it stays on the processor in full precision
        pipe = MuLaCoverGenPipeline.from_pretrained(
            str(self.ckpt),
            device={"mulacover": self.device, "codec": self.device, "qwen": torch.device("cpu"),
                    "transcriptor": torch.device("cpu")},
            dtype={"mulacover": self.mula_dtype, "codec": torch.float32, "qwen": torch.float32,
                   "transcriptor": torch.float32},
            lazy_load=True)
        kwargs = {"dtype": self.mula_dtype, "low_cpu_mem_usage": True, "local_files_only": True}
        if mode == "gpu":
            kwargs["device_map"] = {"": 0}
        elif mode == "compressed":
            from transformers import BitsAndBytesConfig
            vram = vram_gb(torch)
            if vram >= 10:
                kwargs["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)
            else:
                kwargs["quantization_config"] = BitsAndBytesConfig(
                    load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=half)
            kwargs["device_map"] = {"": 0}
        elif mode == "offload":
            vram = vram_gb(torch)
            kwargs["device_map"] = "auto"
            kwargs["max_memory"] = {0: f"{max(1.0, vram - 2.5):.1f}GiB", "cpu": "64GiB"}
        else:
            kwargs["device_map"] = {"": "cpu"}
        _reuse_lead_sheet_work()
        _cache_shared_heads()
        if mode == "compressed":
            _match_cache_precision()
        protocol.progress(0.2)
        model = MuLaCover.from_pretrained(str(self.ckpt / "MuLaCover"), **kwargs).eval()
        if mode == "compressed":
            # Without this, a 5-minute song needs about 7 GB and spills into system memory, which
            # made generation ten times slower on a 6 GB card
            model.text_embeddings = _embedding_on_processor(model.text_embeddings, self.device)
            model.audio_embeddings = _embedding_on_processor(model.audio_embeddings, self.device)
            torch.cuda.empty_cache()
        pipe._mulacover = model
        self.pipe = pipe
        self.mode_used = mode
        self._codec_class = HeartCodec
        protocol.progress(1.0)

    def generate(self, spec: dict, out_dir: Path) -> dict:
        import soundfile as sf
        import torch
        import mulacover.pipeline as mp
        from mulacover.symbolic import SymbolicCondition

        if spec.get("mode") != "cover":
            raise WorkerError("bad_job", "MuLaCover only makes covers.")
        cover = spec["cover"]
        follow = cover.get("follow", {})
        sheet = lead_sheet.build(cover["analysis"], follow, transpose=int(cover.get("transpose", 0)),
                                 sections=cover.get("sections"), max_seconds=cover.get("max_seconds"))
        if not sheet["melody"]:
            raise WorkerError("no_melody", "The analysis found no sung melody to follow.")
        condition = SymbolicCondition(melody=sheet["melody"], chords=sheet["chords"] or None,
                                      bpm=float(cover["analysis"].get("bpm") or 120))
        if cover.get("lead_sheet_dir"):
            condition.save_midi(cover["lead_sheet_dir"])

        pipe = self.pipe
        seed = int(spec.get("seed", -1))
        if seed < 0:
            seed = int.from_bytes(os.urandom(4), "little") & 0x7FFFFFFF
        torch.manual_seed(seed)
        # Long enough for the lead sheet at the original tempo, plus room for an ending
        seconds_per_step = 60.0 / float(cover["analysis"].get("bpm") or 120) / lead_sheet.STEPS_PER_BEAT
        duration = min(MAX_SECONDS, sheet["steps"] * seconds_per_step + 10)
        frames_total = int(duration * 1000 // FRAME_MS)

        def progress_tqdm(iterable, *args, **kwargs):
            for index, item in enumerate(iterable):
                if index % 25 == 0:
                    protocol.progress(index / max(1, frames_total))
                yield item

        mp.tqdm = progress_tqdm
        lyrics = spec.get("lyrics", "").strip() or "[instrumental]"
        tags = style_tags(spec.get("caption", ""))
        cfg = float(spec.get("guidance", 1.5))
        started = time.monotonic()
        protocol.stage("generate")
        original = pipe._symbolic_condition
        pipe._symbolic_condition = lambda _inputs: condition
        try:
            inputs = pipe.preprocess({"lyrics": lyrics, "tags": tags}, cfg_scale=cfg,
                                     max_audio_length_ms=int(duration * 1000))
            # The attention cache is sized for the longest possible song (8192 steps), which
            # overfills a 6 GB card. Size it for this song: the prompt plus its frames.
            pipe._mulacover.backbone.max_seq_len = inputs["tokens"].shape[1] + frames_total + 8
            outputs = pipe._forward(inputs, max_audio_length_ms=int(duration * 1000), temperature=1.0,
                                    topk=250, cfg_scale=cfg)
        finally:
            pipe._symbolic_condition = original
        frames = outputs["frames"]
        pipe._mulacover = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        protocol.stage("decode")
        protocol.progress(0.0)
        audio = self._decode(frames, torch)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "song.flac"
        sf.write(str(path), audio.T, RATE, subtype="PCM_24")
        protocol.progress(1.0)
        return {"file": str(path), "seed": seed, "seconds": time.monotonic() - started,
                "caption_used": tags, "mode_used": self.mode_used}
