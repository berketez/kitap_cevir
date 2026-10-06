"""Sahte model dosyası üreticileri: GGUF üst verisi ve MLX klasörü.

Gerçek ağırlık yazılmaz. GGUF dosyaları 0 tensörlü, GEÇERLİ bir GGUF v3
başlığıdır (sihirli sayı + sürüm + tensör/KV sayısı + KV çiftleri + hizalama
dolgusu): keşif modülünün başlık okuyucusu gerçek dosyadaki gibi çalışır.
MLX klasörü ``config.json`` + (isteğe bağlı) küçük ama biçimce geçerli bir
``model.safetensors`` + tokenizer dosyalarından oluşur.

Yalnız test altyapısıdır (kullanıcıya gösterilen veri değildir).
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

__all__ = [
    "GGUF_MAGIC",
    "GGUF_TYPE_UINT8", "GGUF_TYPE_INT8", "GGUF_TYPE_UINT16", "GGUF_TYPE_INT16",
    "GGUF_TYPE_UINT32", "GGUF_TYPE_INT32", "GGUF_TYPE_FLOAT32", "GGUF_TYPE_BOOL",
    "GGUF_TYPE_STRING", "GGUF_TYPE_ARRAY", "GGUF_TYPE_UINT64", "GGUF_TYPE_INT64",
    "GGUF_TYPE_FLOAT64",
    "gguf_split_name",
    "read_gguf_kv",
    "write_corrupt_gguf",
    "write_fake_gguf",
    "write_fake_gguf_split",
    "write_fake_mlx_dir",
]

GGUF_MAGIC = b"GGUF"
GGUF_VERSION = 3
GGUF_ALIGNMENT = 32
(GGUF_TYPE_UINT8, GGUF_TYPE_INT8, GGUF_TYPE_UINT16, GGUF_TYPE_INT16, GGUF_TYPE_UINT32,
 GGUF_TYPE_INT32, GGUF_TYPE_FLOAT32, GGUF_TYPE_BOOL, GGUF_TYPE_STRING, GGUF_TYPE_ARRAY,
 GGUF_TYPE_UINT64, GGUF_TYPE_INT64, GGUF_TYPE_FLOAT64) = range(13)
_SCALAR_FMT = {
    GGUF_TYPE_UINT8: "<B", GGUF_TYPE_INT8: "<b", GGUF_TYPE_UINT16: "<H", GGUF_TYPE_INT16: "<h",
    GGUF_TYPE_UINT32: "<I", GGUF_TYPE_INT32: "<i", GGUF_TYPE_FLOAT32: "<f", GGUF_TYPE_BOOL: "<?",
    GGUF_TYPE_UINT64: "<Q", GGUF_TYPE_INT64: "<q", GGUF_TYPE_FLOAT64: "<d",
}

# extra_kv değeri: düz Python değeri (tür otomatik) ya da (GGUF_TYPE_*, değer)
KVValue = Union[str, int, float, bool, list, Tuple[int, Any]]


# ---------------------------------------------------------------------------
# GGUF yazımı
# ---------------------------------------------------------------------------
def _gguf_str(s: str) -> bytes:
    data = s.encode("utf-8")
    return struct.pack("<Q", len(data)) + data


def _infer_type(value: Any) -> int:
    if isinstance(value, bool):
        return GGUF_TYPE_BOOL
    if isinstance(value, int):
        if 0 <= value < 2 ** 32:
            return GGUF_TYPE_UINT32
        if -2 ** 31 <= value < 0:
            return GGUF_TYPE_INT32
        return GGUF_TYPE_UINT64 if value >= 0 else GGUF_TYPE_INT64
    if isinstance(value, float):
        return GGUF_TYPE_FLOAT32
    if isinstance(value, str):
        return GGUF_TYPE_STRING
    if isinstance(value, (list, tuple)):
        return GGUF_TYPE_ARRAY
    raise TypeError(f"GGUF değeri desteklenmiyor: {value!r}")


def _encode_value(vtype: int, value: Any) -> bytes:
    if vtype in _SCALAR_FMT:
        return struct.pack(_SCALAR_FMT[vtype], value)
    if vtype == GGUF_TYPE_STRING:
        return _gguf_str(value)
    if vtype == GGUF_TYPE_ARRAY:
        items = list(value)
        etype = _infer_type(items[0]) if items else GGUF_TYPE_INT32
        body = b"".join(_encode_value(etype, v) for v in items)
        return struct.pack("<IQ", etype, len(items)) + body
    raise ValueError(f"bilinmeyen GGUF türü {vtype}")


def _encode_kv(key: str, value: KVValue) -> bytes:
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], int) \
            and not isinstance(value[0], bool) and value[0] in range(13):
        vtype, raw = value
    else:
        vtype, raw = _infer_type(value), value
    return _gguf_str(key) + struct.pack("<I", vtype) + _encode_value(vtype, raw)


def _write_gguf(path: Path, kvs: List[Tuple[str, KVValue]], pad_bytes: int = 0,
                version: int = GGUF_VERSION) -> Path:
    header = GGUF_MAGIC + struct.pack("<I", version) + struct.pack("<QQ", 0, len(kvs))
    body = b"".join(_encode_kv(k, v) for k, v in kvs)
    blob = header + body
    blob += b"\x00" * ((-len(blob)) % GGUF_ALIGNMENT)   # veri bölümü hizası (0 tensör)
    blob += b"\x00" * max(0, pad_bytes)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


def write_fake_gguf(path: Union[str, Path], arch: str, type_: Optional[str] = "model",
                    name: str = "", size_label: str = "",
                    chat_template: Optional[str] = None, *,
                    context_length: Optional[int] = None,
                    extra_kv: Optional[Dict[str, KVValue]] = None,
                    pad_bytes: int = 0) -> Path:
    """0 tensörlü geçerli GGUF v3 dosyası yaz.

    - ``arch``: ``general.architecture`` (projektör için "clip").
    - ``type_``: ``general.type`` ("model" | "mmproj" | None = alan yok, eski dosyalar gibi).
    - ``name`` / ``size_label`` boşsa ilgili alan yazılmaz.
    - ``chat_template``: ``tokenizer.chat_template`` (None = yok).
    - ``context_length``: ``<arch>.context_length`` (None = yok).
    - ``pad_bytes``: sona sıfır bayt (dosya boyutu benzetimi).
    """
    kvs: List[Tuple[str, KVValue]] = [("general.architecture", arch)]
    if type_ is not None:
        kvs.append(("general.type", type_))
    if name:
        kvs.append(("general.name", name))
    if size_label:
        kvs.append(("general.size_label", size_label))
    if context_length is not None:
        kvs.append((f"{arch}.context_length", (GGUF_TYPE_UINT32, context_length)))
    if chat_template is not None:
        kvs.append(("tokenizer.chat_template", chat_template))
    for key, value in (extra_kv or {}).items():
        kvs.append((key, value))
    return _write_gguf(Path(path), kvs, pad_bytes=pad_bytes)


def gguf_split_name(stem: str, index: int, count: int) -> str:
    """llama.cpp gguf-split adlandırması: ``<stem>-00001-of-00003.gguf`` (index 1'den)."""
    return f"{stem}-{index:05d}-of-{count:05d}.gguf"


def write_fake_gguf_split(directory: Union[str, Path], stem: str, count: int, arch: str,
                          *, pad_bytes: int = 0, **kwargs: Any) -> List[Path]:
    """Parçalı GGUF: ilk parça tam üst veri + ``split.*``, diğerleri yalnız ``split.*``.

    ``kwargs`` ilk parçaya ``write_fake_gguf`` argümanı olarak geçer.
    Dönüş: parça yolları (sıralı).
    """
    directory = Path(directory)
    paths: List[Path] = []
    for i in range(count):
        split_kv = {"split.no": (GGUF_TYPE_UINT16, i), "split.count": (GGUF_TYPE_UINT16, count),
                    "split.tensors.count": (GGUF_TYPE_INT32, 0)}
        target = directory / gguf_split_name(stem, i + 1, count)
        if i == 0:
            extra = dict(kwargs.pop("extra_kv", None) or {})
            extra.update(split_kv)
            paths.append(write_fake_gguf(target, arch, extra_kv=extra, pad_bytes=pad_bytes,
                                         **kwargs))
        else:
            paths.append(_write_gguf(target, list(split_kv.items()), pad_bytes=pad_bytes))
    return paths


def write_corrupt_gguf(path: Union[str, Path], kind: str = "magic") -> Path:
    """Bozuk GGUF: ``"magic"`` (sihirli sayı yanlış), ``"truncated"`` (KV yarıda
    kesik), ``"kv_count"`` (KV sayısı absürt büyük), ``"empty"`` (0 bayt)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "magic":
        path.write_bytes(b"GGML" + b"\x00" * 60)
    elif kind == "truncated":
        full = GGUF_MAGIC + struct.pack("<I", GGUF_VERSION) + struct.pack("<QQ", 0, 2) \
            + _encode_kv("general.architecture", "qwen3")
        path.write_bytes(full + _gguf_str("general.name")[:6])
    elif kind == "kv_count":
        path.write_bytes(GGUF_MAGIC + struct.pack("<I", GGUF_VERSION)
                         + struct.pack("<QQ", 0, 2 ** 40))
    elif kind == "empty":
        path.write_bytes(b"")
    else:
        raise ValueError(f"bilinmeyen bozukluk türü: {kind!r}")
    return path


def read_gguf_kv(path: Union[str, Path]) -> Dict[str, Any]:
    """Testlerin gidiş-dönüş denetimi için küçük GGUF başlık okuyucusu (dizi -> liste)."""
    def rstr(f) -> str:
        (n,) = struct.unpack("<Q", f.read(8))
        return f.read(n).decode("utf-8")

    def rval(f, t: int) -> Any:
        if t in _SCALAR_FMT:
            fmt = _SCALAR_FMT[t]
            return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]
        if t == GGUF_TYPE_STRING:
            return rstr(f)
        if t == GGUF_TYPE_ARRAY:
            et, n = struct.unpack("<IQ", f.read(12))
            return [rval(f, et) for _ in range(n)]
        raise ValueError(f"bilinmeyen GGUF türü {t}")

    out: Dict[str, Any] = {}
    with open(path, "rb") as f:
        if f.read(4) != GGUF_MAGIC:
            raise ValueError("GGUF değil")
        f.read(4)
        _, nkv = struct.unpack("<QQ", f.read(16))
        for _ in range(nkv):
            key = rstr(f)
            (t,) = struct.unpack("<I", f.read(4))
            out[key] = rval(f, t)
    return out


# ---------------------------------------------------------------------------
# MLX klasörü
# ---------------------------------------------------------------------------
def _tiny_safetensors() -> bytes:
    header = json.dumps({"__metadata__": {"format": "mlx"}}).encode("utf-8")
    header += b" " * ((-len(header)) % 8)
    return struct.pack("<Q", len(header)) + header


def write_fake_mlx_dir(path: Union[str, Path], model_type: str, safetensors: bool = True,
                       tokenizer: bool = True, chat_template: Optional[str] = None, *,
                       quantization: Optional[Dict[str, int]] = None,
                       config_extra: Optional[Dict[str, Any]] = None,
                       tokenizer_files: Iterable[str] = ("tokenizer.json",
                                                         "tokenizer_config.json")) -> Path:
    """Sahte MLX model klasörü yaz.

    - ``config.json``: ``model_type`` (+ ``quantization`` verilirse, + ``config_extra``).
    - ``safetensors``: küçük ama biçimce geçerli ``model.safetensors``.
    - ``tokenizer``: ``tokenizer_files`` listesindeki dosyalar;
      ``tokenizer_config.json`` ``chat_template``'i (verilmişse) taşır.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    config: Dict[str, Any] = {"model_type": model_type, "vocab_size": 32000,
                              "hidden_size": 64, "num_hidden_layers": 2}
    if quantization is not None:
        config["quantization"] = dict(quantization)
    config.update(config_extra or {})
    (path / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    if safetensors:
        (path / "model.safetensors").write_bytes(_tiny_safetensors())
    if tokenizer:
        for fname in tokenizer_files:
            if fname == "tokenizer_config.json":
                tcfg: Dict[str, Any] = {"tokenizer_class": "PreTrainedTokenizerFast"}
                if chat_template is not None:
                    tcfg["chat_template"] = chat_template
                (path / fname).write_text(json.dumps(tcfg, ensure_ascii=False), encoding="utf-8")
            elif fname == "tokenizer.json":
                (path / fname).write_text(json.dumps(
                    {"version": "1.0", "model": {"type": "BPE", "vocab": {}, "merges": []}}),
                    encoding="utf-8")
            else:
                (path / fname).write_bytes(b"\x00sahte-tokenizer")
    return path
