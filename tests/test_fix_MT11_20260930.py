"""MT-11 (2026-09-30): donanım kapısı gemma4:31b'yi yanlış reddediyordu.

Ollama'nın kısa /api/show yanıtı katman başına dizileri (head_count_kv,
sliding_window_pattern) null döndürüyor; tahmin MHA (32 baş × 512) sanıp
KV'yi ~32 GB, toplamı ~57 GB buluyordu. Gerçekte gemma4 GQA + kayan pencereli
(5 yerel : 1 genel katman); KV birkaç GB. Model bu Mac'te (30,1 GB) çalışıyor.
"""

from core import hardware
from core.mt_engines import discovery
from config.defaults import HW_KV_DTYPE_BYTES, HW_SWA_EXTRA_TOKENS

LAYERS = 60
KV_LIST = ([16] * 5 + [4]) * 10
PATTERN = ([True] * 5 + [False]) * 10


def _gemma4_info(verbose: bool) -> dict:
    return {
        "general.architecture": "gemma4",
        "gemma4.block_count": LAYERS,
        "gemma4.attention.head_count": 32,
        "gemma4.attention.head_count_kv": KV_LIST if verbose else None,
        "gemma4.attention.key_length": 512,
        "gemma4.attention.value_length": 512,
        "gemma4.attention.key_length_swa": 256,
        "gemma4.attention.value_length_swa": 256,
        "gemma4.attention.sliding_window": 1024,
        "gemma4.attention.sliding_window_pattern": PATTERN if verbose else None,
        "gemma4.embedding_length": 5376,
        "gemma4.context_length": 262144,
    }


def test_kisa_yanit_mha_sanilmaz():
    info = _gemma4_info(verbose=False)
    assert hardware.gguf_needs_per_layer(info)
    # Şekil bilinmiyor -> None (oran yedeği); eskiden MHA × 60 katman çıkıyordu
    assert hardware.kv_shape_from_gguf_keys(info, hardware.KV_SOURCE_OLLAMA) is None


def test_katman_katman_kv_ve_pencere():
    kv = hardware.kv_shape_from_gguf_keys(_gemma4_info(verbose=True), hardware.KV_SOURCE_OLLAMA)
    assert kv is not None
    full = 10 * 4 * (512 + 512) * HW_KV_DTYPE_BYTES
    swa = 50 * 16 * (256 + 256) * HW_KV_DTYPE_BYTES
    assert kv.bytes_per_token == full and kv.swa_bytes_per_token == swa
    assert kv.swa_window == 1024
    # Pencere sınırı: 8192 token'da kayan katmanlar yalnız pencere + pay kadar
    assert kv.kv_bytes(8192) == full * 8192 + swa * (1024 + HW_SWA_EXTRA_TOKENS)
    # Kısa bağlamda sınır devreye girmez
    assert kv.kv_bytes(500) == (full + swa) * 500


def test_gemma4_31b_bu_mac_boyunda_sigar():
    kv = hardware.kv_shape_from_gguf_keys(_gemma4_info(verbose=True), hardware.KV_SOURCE_OLLAMA)
    hw = hardware.HardwareInfo(available_bytes=30_100_000_000, memory_bytes=36 * 2**30,
                                chip="test", source="metal")
    fit = hardware.assess(19_868_981_791, kv, mode=hardware.MODE_CONTEXT,
                          context_tokens=hardware.ollama_context_tokens(None, 262144), hw=hw)
    assert fit.fits
    assert fit.required_bytes < 28_000_000_000


def test_tekduze_model_eskisi_gibi():
    info = {"general.architecture": "llama", "llama.block_count": 32,
            "llama.attention.head_count": 32, "llama.attention.head_count_kv": 8,
            "llama.embedding_length": 4096}
    kv = hardware.kv_shape_from_gguf_keys(info)
    assert kv.bytes_per_token == 32 * 8 * 256 * 2 and kv.swa_bytes_per_token == 0
    assert kv.kv_bytes(8192) == kv.bytes_per_token * 8192
    assert not hardware.gguf_needs_per_layer(info)


def test_anahtar_hic_yoksa_gguf_kurali_mha():
    info = {"general.architecture": "llama", "llama.block_count": 2,
            "llama.attention.head_count": 4, "llama.embedding_length": 256}
    kv = hardware.kv_shape_from_gguf_keys(info)
    assert kv.kv_heads == 4


def test_dizi_boyu_uyusmazsa_bilinmiyor():
    info = _gemma4_info(verbose=True)
    info["gemma4.attention.head_count_kv"] = KV_LIST[:-1]
    assert hardware.kv_shape_from_gguf_keys(info) is None


def test_ayrintili_show_yalniz_gerektiginde(monkeypatch):
    calls = []

    def fake_fetch(url, timeout, body=None):
        calls.append(dict(body or {}))
        verbose = bool((body or {}).get("verbose"))
        info = _gemma4_info(verbose)
        if verbose:
            info["tokenizer.ggml.tokens"] = ["a"] * 10
        return {"model_info": info, "capabilities": ["completion"]}, None

    monkeypatch.setattr(discovery, "_fetch", fake_fetch)
    show, err = discovery._ollama_show("http://127.0.0.1:11434", "gemma4:31b", 5.0)
    assert err is None and [c.get("verbose") for c in calls] == [None, True]
    assert show["model_info"]["gemma4.attention.head_count_kv"] == KV_LIST
    assert not any(k.startswith("tokenizer.") for k in show["model_info"])
    assert show["capabilities"] == ["completion"]

    calls.clear()

    def plain_fetch(url, timeout, body=None):
        calls.append(dict(body or {}))
        return {"model_info": {"general.architecture": "llama", "llama.block_count": 2}}, None

    monkeypatch.setattr(discovery, "_fetch", plain_fetch)
    discovery._ollama_show("http://127.0.0.1:11434", "llama3", 5.0)
    assert len(calls) == 1
