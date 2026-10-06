"""Donanım kapısı (D kulvarı, 28 Eylül 2026; KARARLAR 17 ve 17a).

"Yerel model ile çevir" bu Mac'in belleğine bakar:

- ölçüm GERÇEKTİR (MLX ``mx.device_info()`` -> ``max_recommended_working_set_size``;
  yedek ``sysctl hw.memsize`` × kaynaklı oran; ikisi de yoksa "ölçülemedi",
  kapı açık + uyarı);
- genel kapı: referans 4 bit 7B toplu iş 1 ile sığmıyorsa kart kapalı, CLI
  çıkarımdan ÖNCE 5;
- model başına kapı: keşif her modelin tahmini gereken belleğini hesaplar;
  toplu iş 1 ile bile sığmayan model "bu Mac'e sığmaz", seçilirse "Çevir"
  kapalı; ``check_engine`` aynı hükmü verir (CLI 5);
- dar bellekte toplu iş küçülür; büyük bellekte (36 GB) hiçbir şey değişmez.

Makineden bağımsız: tests/conftest.py ``_hardware_isolation`` ölçümü büyük
sabit bir değere yamalar; buradaki 8 / 16 / 36 GB senaryoları kendi
testlerinde yamalanır. Senaryo değerleri sahte test verisidir (kaynakları
senaryo sabitlerinin yanında). Model yüklenmez; ağır ağırlık dosyaları
seyrek (sparse) dosyadır, diskte yer kaplamaz. Gerçek Ollama / LM Studio'ya
gidilmez (sahte sunucu).
"""

from __future__ import annotations

import functools
import json
import logging
import subprocess
import sys
import tempfile
import time
import types
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
import streamlit
from streamlit.testing.v1 import AppTest

import core.memory
import translate_book as tb
from config import defaults
from core import hardware
from core.mt_engines import contracts, discovery, registry
from core.mt_engines.contracts import DiscoveryReport, SourceStatus
from tests.fakes import write_fake_gguf, write_fake_mlx_dir
from tests.fakes.model_files import GGUF_TYPE_UINT32
from ui import branding, runner

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GIB = 2 ** 30
# Gerçek ölçüm fonksiyonu (conftest ``_hardware_isolation`` testte onu yamalar;
# modül içe aktarılırken henüz yamalı değildir).
REAL_MEASURE = hardware._measure

# ---------------------------------------------------------------------------
# Senaryolar (sahte test verisi)
# ---------------------------------------------------------------------------
# 8 GB M1: Metal recommendedMaxWorkingSetSize = 5461,34 MB = 8192 MB × 2/3
# (Ollama #1231 günlüğü). 16 GB: 10922,67 MB = 16384 MB × 2/3 (llama.cpp #2522
# günlüğü). 36 GB: bu Mac (M4 Max), 28 Eylül 2026'da mx.device_info() ile ölçüldü.
MAC_8GB = (8 * GIB, int(8 * GIB * 2 / 3))
MAC_16GB = (16 * GIB, int(16 * GIB * 2 / 3))
MAC_36GB = (38_654_705_664, 30_150_672_384)

# HY-MT1.5 7B (models/hymt-7b-mlx): diskte 15 009 177 951 bayt bf16 ağırlık;
# config.json: 32 katman, 8 KV başlığı, head_dim 128, bfloat16.
HYMT_WEIGHTS = 15_009_177_951
HYMT_CONFIG = {"model_type": "hunyuan_v1_dense", "num_hidden_layers": 32,
               "num_attention_heads": 32, "num_key_value_heads": 8,
               "hidden_size": 4096, "head_dim": 128, "torch_dtype": "bfloat16"}
HYMT_KV = hardware.KvShape(32, 8, 128, 128, 2, hardware.KV_SOURCE_CONFIG)
# 4 bit 7B (referans modelin boyutu ve şekli; config/defaults.py)
Q4_WEIGHTS = defaults.HW_REFERENCE_WEIGHTS_BYTES


def _info(scenario, *, source=hardware.SOURCE_METAL, chip="Test Mac") -> hardware.HardwareInfo:
    memory, available = scenario
    return hardware.HardwareInfo(available_bytes=available, memory_bytes=memory, chip=chip,
                                 source=source)


def _use(monkeypatch, scenario, **kwargs) -> hardware.HardwareInfo:
    """Bu testte ölçüm = senaryo (önbellek sıfırlanır)."""
    info = _info(scenario, **kwargs)
    monkeypatch.setattr(hardware, "_measure", lambda: info)
    monkeypatch.setattr(hardware, "_CACHE", None)
    return info


def _need_gb(value: int) -> str:
    return hardware.format_gb(value, round_up=True)


def _have_gb(value: int) -> str:
    return hardware.format_gb(value, round_up=False)


def _sparse(path: Path, size: int) -> Path:
    """Seyrek dosya: st_size = size, diskte yer kaplamaz. Var olan dosyanın
    başı (ör. GGUF başlığı) korunur, yalnız sonu uzatılır."""
    with open(path, "r+b" if path.exists() else "wb") as handle:
        handle.truncate(size)
    return path


def _hymt_dir(models_dir: Path, weights: int = HYMT_WEIGHTS, name: str = "hymt-7b-mlx",
              config: dict | None = None) -> Path:
    """Yerleşik motorun beklediği klasör: HY-MT config.json + seyrek ağırlık."""
    cfg = dict(config or HYMT_CONFIG)
    model_type = cfg.pop("model_type")
    path = write_fake_mlx_dir(models_dir / name, model_type, config_extra=cfg)
    small = (path / "model.safetensors").stat().st_size
    _sparse(path / "model-00002-of-00002.safetensors", weights - small)
    return path


@pytest.fixture
def mlx_arch(monkeypatch):
    """Kurulu mlx-lm yerine sahte mimari listesi (makineden bağımsız)."""
    layout = ("/yok/mlx_lm", ())
    monkeypatch.setattr(discovery, "_mlx_lm_layout", lambda: layout)
    monkeypatch.setattr(discovery, "mlx_arch_supported",
                        lambda model_type: model_type in ("hunyuan_v1_dense", "llama"))


# ===========================================================================
# 1. Ölçüm (core.hardware._measure)
# ===========================================================================

class _FakeMx(types.ModuleType):
    def __init__(self, info=None, *, old_api=False, fail=False):
        super().__init__("mlx.core")
        self._info = info
        if fail:
            def device_info():
                raise RuntimeError("aygıt yok")
            self.device_info = device_info
        elif old_api:
            self.metal = SimpleNamespace(device_info=lambda: dict(info))
        else:
            self.device_info = lambda: dict(info)


def _fake_mlx(monkeypatch, mx) -> None:
    pkg = types.ModuleType("mlx")
    pkg.core = mx
    monkeypatch.setitem(sys.modules, "mlx", pkg)
    monkeypatch.setitem(sys.modules, "mlx.core", mx)


def _fake_sysctl(monkeypatch, values: dict) -> list:
    calls = []

    def run(name):
        calls.append(name)
        return values.get(name, "")

    monkeypatch.setattr(hardware, "_run_sysctl", run)
    return calls


def _no_ctypes_metal(monkeypatch) -> None:
    """MLX'siz Metal ölçümü de yok (K6, 2026-10-05; TASARIM F3).

    F3'ten beri MLX ölçemezse aynı Metal değeri ctypes ile alınır; sysctl
    yedeğine ancak o da alınamazsa düşülür. Yedeği sınayan testler bu Mac'in
    gerçek Metal'ine ulaşmasın diye ctypes yolu kapatılır.
    """
    monkeypatch.setattr(hardware, "_metal_ctypes_info",
                        lambda: (None, "", "Metal aygıtı yok"))


class TestOlcum:
    def test_metal_olcumu(self, monkeypatch):
        _fake_mlx(monkeypatch, _FakeMx({"memory_size": MAC_36GB[0],
                                        "max_recommended_working_set_size": MAC_36GB[1],
                                        "device_name": "Apple M4 Max"}))
        _fake_sysctl(monkeypatch, {"machdep.cpu.brand_string": "Apple M4 Max"})
        info = REAL_MEASURE()
        assert info.measured and info.source == hardware.SOURCE_METAL
        assert info.available_bytes == MAC_36GB[1] and info.memory_bytes == MAC_36GB[0]
        assert info.chip == "Apple M4 Max" and info.problem == ""

    def test_eski_api_mx_metal(self, monkeypatch):
        _fake_mlx(monkeypatch, _FakeMx({"memory_size": MAC_16GB[0],
                                        "max_recommended_working_set_size": MAC_16GB[1]},
                                       old_api=True))
        _fake_sysctl(monkeypatch, {})
        info = REAL_MEASURE()
        assert info.source == hardware.SOURCE_METAL and info.available_bytes == MAC_16GB[1]

    def test_cip_adi_yoksa_mlx_aygit_adi(self, monkeypatch):
        _fake_mlx(monkeypatch, _FakeMx({"max_recommended_working_set_size": MAC_16GB[1],
                                        "device_name": "Apple M2"}))
        _fake_sysctl(monkeypatch, {"hw.memsize": str(MAC_16GB[0])})
        info = REAL_MEASURE()
        assert info.chip == "Apple M2" and info.memory_bytes == MAC_16GB[0]

    def test_mlx_yoksa_sysctl_yedegi(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "mlx", None)
        monkeypatch.setitem(sys.modules, "mlx.core", None)
        _no_ctypes_metal(monkeypatch)
        _fake_sysctl(monkeypatch, {"hw.memsize": str(MAC_16GB[0]),
                                   "machdep.cpu.brand_string": "Apple M1"})
        info = REAL_MEASURE()
        assert info.source == hardware.SOURCE_SYSCTL
        assert info.available_bytes == int(MAC_16GB[0] * defaults.HW_WORKING_SET_FALLBACK_RATIO)
        assert "MLX yüklenemedi" in info.problem
        gate = hardware.general_gate(info)
        assert gate.allowed and "Metal ölçümü alınamadı" in gate.warning
        assert "0,67" in gate.warning
        assert hardware.hardware_caption(info).endswith(" GB (yedek ölçüm)")

    def test_calisma_kumesi_yoksa_yedek(self, monkeypatch):
        _fake_mlx(monkeypatch, _FakeMx({"memory_size": MAC_16GB[0]}))
        _no_ctypes_metal(monkeypatch)
        _fake_sysctl(monkeypatch, {"hw.memsize": str(MAC_16GB[0])})
        info = REAL_MEASURE()
        assert info.source == hardware.SOURCE_SYSCTL
        assert "kullanılabilir bellek değeri vermedi" in info.problem

    def test_aygit_bilgisi_hata_verirse_yedek(self, monkeypatch):
        _fake_mlx(monkeypatch, _FakeMx(fail=True))
        _no_ctypes_metal(monkeypatch)
        _fake_sysctl(monkeypatch, {"hw.memsize": str(MAC_8GB[0])})
        info = REAL_MEASURE()
        assert info.source == hardware.SOURCE_SYSCTL
        assert "aygıt bilgisi alınamadı" in info.problem

    def test_hic_olculemezse_kapi_acik_uyari(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "mlx", None)
        monkeypatch.setitem(sys.modules, "mlx.core", None)
        _no_ctypes_metal(monkeypatch)
        _fake_sysctl(monkeypatch, {})
        info = REAL_MEASURE()
        assert not info.measured and info.available_bytes is None
        gate = hardware.general_gate(info)
        assert gate.allowed and gate.reason == ""
        assert gate.warning.startswith("Bu Mac'in kullanılabilir belleği ölçülemedi (")
        fit = hardware.assess(HYMT_WEIGHTS, HYMT_KV, hw=info)
        assert fit.fits is None and fit.batch_size is None and fit.available_bytes is None
        assert hardware.hardware_caption(info) == "Apple Silicon · bellek ölçülemedi"

    def test_sysctl_gercek_cagri_bicimi(self, monkeypatch):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append((argv, kwargs.get("timeout")))
            return SimpleNamespace(returncode=0, stdout="17179869184\n")

        monkeypatch.setattr(hardware.subprocess, "run", fake_run)
        assert hardware._run_sysctl("hw.memsize") == "17179869184"
        assert seen[0][0][1:] == ["-n", "hw.memsize"]
        assert seen[0][1] == defaults.HW_SYSCTL_TIMEOUT_S

    def test_sysctl_hatasi_bos(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("yok")

        monkeypatch.setattr(hardware.subprocess, "run", boom)
        assert hardware._run_sysctl("hw.memsize") == ""

    def test_onbellek_bir_kez_olcer(self, monkeypatch):
        calls = []
        info = _info(MAC_36GB)
        monkeypatch.setattr(hardware, "_measure", lambda: calls.append(1) or info)
        monkeypatch.setattr(hardware, "_CACHE", None)
        assert hardware.hardware_info() is info
        assert hardware.hardware_info() is info
        assert calls == [1]
        hardware.hardware_info(refresh=True)
        assert calls == [1, 1]

    def test_conftest_olcumu_buyuk_sabit(self, _hardware_isolation):
        info = hardware.hardware_info()
        assert info is _hardware_isolation and info.measured
        assert hardware.general_gate().allowed

    def test_hafif_ice_aktarma(self):
        code = ("import sys, core.hardware\n"
                "print([m for m in ('mlx', 'mlx_lm', 'torch', 'llama_cpp') "
                "if m in sys.modules])\n")
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(PROJECT_ROOT),
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "[]"


# ===========================================================================
# 2. KV şekli ve token üst sınırları
# ===========================================================================

class TestKvSekli:
    def test_hymt_config_128_kib(self):
        kv = hardware.kv_shape_from_hf_config(HYMT_CONFIG)
        assert kv == HYMT_KV
        assert kv.bytes_per_token == 2 * 32 * 8 * 128 * 2 == 128 * 1024

    def test_head_dim_gizli_boyuttan(self):
        cfg = {"num_hidden_layers": 32, "num_attention_heads": 32, "num_key_value_heads": 8,
               "hidden_size": 4096}
        assert hardware.kv_shape_from_hf_config(cfg).key_dim == 128

    def test_kv_basligi_yoksa_dikkat_basligi(self):
        cfg = {"num_hidden_layers": 2, "num_attention_heads": 4, "hidden_size": 64}
        kv = hardware.kv_shape_from_hf_config(cfg)
        assert kv.kv_heads == 4 and kv.key_dim == 16

    def test_text_config_icinde(self):
        cfg = {"model_type": "gemma3", "text_config": dict(HYMT_CONFIG)}
        assert hardware.kv_shape_from_hf_config(cfg).bytes_per_token == 128 * 1024

    def test_float32_dort_bayt(self):
        cfg = dict(HYMT_CONFIG, torch_dtype="float32")
        assert hardware.kv_shape_from_hf_config(cfg).dtype_bytes == 4

    @pytest.mark.parametrize("cfg", [
        None, [], {}, {"num_hidden_layers": 32},
        {"num_hidden_layers": 32, "num_attention_heads": 32, "hidden_size": 4095},
        dict(HYMT_CONFIG, kv_lora_rank=512),                  # MLA: formül farklı
        dict(HYMT_CONFIG, num_hidden_layers=True),
    ])
    def test_okunamayan_config(self, cfg):
        assert hardware.kv_shape_from_hf_config(cfg) is None

    def test_gguf_anahtarlari(self):
        values = {"general.architecture": "llama", "llama.block_count": 32,
                  "llama.attention.head_count": 32, "llama.attention.head_count_kv": 8,
                  "llama.embedding_length": 4096}
        kv = hardware.kv_shape_from_gguf_keys(values)
        assert kv.bytes_per_token == 128 * 1024 and kv.source == hardware.KV_SOURCE_GGUF

    def test_gguf_anahtar_deger_uzunlugu(self):
        values = {"general.architecture": "gemma", "gemma.block_count": 10,
                  "gemma.attention.head_count": 8, "gemma.attention.head_count_kv": 1,
                  "gemma.embedding_length": 2048, "gemma.attention.key_length": 256,
                  "gemma.attention.value_length": 128}
        kv = hardware.kv_shape_from_gguf_keys(values)
        assert (kv.key_dim, kv.value_dim) == (256, 128)
        assert kv.bytes_per_token == 10 * 1 * (256 + 128) * 2

    def test_ollama_model_info_belgedeki_ornek(self):
        # docs/api.md "Show Model Information" yanıt örneği (llama3 8B)
        info = {"general.architecture": "llama", "llama.attention.head_count": 32,
                "llama.attention.head_count_kv": 8, "llama.block_count": 32,
                "llama.context_length": 8192, "llama.embedding_length": 4096}
        kv = hardware.kv_shape_from_gguf_keys(info, hardware.KV_SOURCE_OLLAMA)
        assert kv.bytes_per_token == 128 * 1024 and kv.source == hardware.KV_SOURCE_OLLAMA

    @pytest.mark.parametrize("values", [
        None, {}, {"general.architecture": ""},
        {"general.architecture": "llama", "llama.block_count": 32},
        {"general.architecture": "deepseek2", "deepseek2.block_count": 27,
         "deepseek2.attention.head_count": 16, "deepseek2.embedding_length": 2048,
         "deepseek2.attention.kv_lora_rank": 512},
    ])
    def test_gguf_okunamayan(self, values):
        assert hardware.kv_shape_from_gguf_keys(values) is None

    def test_yedek_oran_referansla_ayni(self):
        ref = hardware.reference_kv().bytes_per_token
        assert hardware.fallback_kv_bytes_per_token(Q4_WEIGHTS) == ref
        assert hardware.fallback_kv_bytes_per_token(2 * Q4_WEIGHTS) == 2 * ref

    def test_ollama_baglami(self):
        assert hardware.ollama_context_tokens() == defaults.MT_CONTEXT_TOKENS
        assert hardware.ollama_context_tokens(16384) == 16384
        assert hardware.ollama_context_tokens(1024) == defaults.MT_CONTEXT_TOKENS
        assert hardware.ollama_context_tokens(None, 4096) == 4096


class TestTokenSinirlari:
    def test_cikti_kurali_defaults_ile(self):
        assert hardware.output_token_limit(0) == defaults.MT_OUTPUT_TOKENS_FLOOR
        assert hardware.output_token_limit(10 ** 7) == defaults.MT_OUTPUT_TOKENS_CAP
        chars = defaults.MAX_SEGMENT_LENGTH
        assert hardware.output_token_limit(chars) == int(
            chars / defaults.MT_CONTEXT_CHARS_PER_TOKEN * defaults.MT_OUTPUT_TOKENS_FACTOR)

    def test_ceviri_yoluyla_ayni_kural(self):
        """Donanım tahmini çevirinin gerçek çıktı sınırıyla aynı kuralı kullanır
        (T kulvarının tek hesabı core.mt_engines.backends.output_token_limit)."""
        from core.mt_engines import backends

        rule = getattr(backends, "output_token_limit", None)
        if rule is None:
            pytest.skip("backends.output_token_limit yok (T kulvarı birleşmedi)")
        for chars in (0, 1, 100, 699, 700, 1000, defaults.MAX_SEGMENT_LENGTH, 5587,
                      5600, 10 ** 6):
            assert hardware.output_token_limit(chars) == rule(chars), chars

    def test_toplu_ve_tek_parca(self):
        assert hardware.batch_sequence_tokens() == (
            defaults.MT_MAX_INPUT_TOKENS
            + hardware.output_token_limit(defaults.MAX_SEGMENT_LENGTH))
        assert hardware.single_sequence_tokens() == (
            defaults.MT_MAX_INPUT_TOKENS + defaults.MT_OUTPUT_TOKENS_CAP)
        assert hardware.batched_kv_tokens(1) == hardware.single_sequence_tokens()
        assert hardware.batched_kv_tokens(20) == 20 * hardware.batch_sequence_tokens()


# ===========================================================================
# 3. Tahmin ve kapılar (8 / 16 / 36 GB)
# ===========================================================================

class TestTahmin:
    def test_formul_toplu_kip(self):
        hw = _info(MAC_36GB)
        fit = hardware.assess(HYMT_WEIGHTS, HYMT_KV, hw=hw)
        workspace = -(-HYMT_WEIGHTS * 25 // 100)
        assert fit.workspace_bytes == workspace
        per_token = 128 * 1024
        assert fit.minimum_bytes == (HYMT_WEIGHTS + workspace
                                     + per_token * hardware.batched_kv_tokens(1))
        assert fit.required_bytes == (HYMT_WEIGHTS + workspace
                                      + per_token * hardware.batched_kv_tokens(fit.batch_size))
        assert fit.kv_tokens == hardware.batched_kv_tokens(fit.batch_size)
        assert fit.kv_source == hardware.KV_SOURCE_CONFIG and fit.mode == hardware.MODE_BATCHED
        assert fit.hardware_source == hardware.SOURCE_METAL

    def test_formul_baglam_kipi(self):
        hw = _info(MAC_36GB)
        fit = hardware.assess(Q4_WEIGHTS, HYMT_KV, mode=hardware.MODE_CONTEXT,
                              context_tokens=8192, hw=hw)
        assert fit.required_bytes == fit.minimum_bytes == (
            Q4_WEIGHTS + fit.workspace_bytes + 128 * 1024 * 8192)
        assert fit.fits and fit.batch_size == 1 and fit.kv_tokens == 8192

    def test_yedek_oran_kaynagi(self):
        fit = hardware.assess(HYMT_WEIGHTS, None, hw=_info(MAC_36GB))
        assert fit.kv_source == hardware.KV_SOURCE_RATIO
        assert fit.kv_bytes_per_token == hardware.fallback_kv_bytes_per_token(HYMT_WEIGHTS)

    def test_8gb_genel_kapi_kapali(self):
        hw = _info(MAC_8GB)
        gate = hardware.general_gate(hw)
        assert not gate.allowed
        ref = gate.reference
        assert ref.fits is False and ref.minimum_bytes > MAC_8GB[1]
        assert gate.reason == (
            f"Bu Mac yerel model için yetersiz: en az {_need_gb(ref.minimum_bytes)} GB "
            f"kullanılabilir bellek gerekir, bu Mac'te {_have_gb(MAC_8GB[1])} GB.")
        # Sayılar gerçek: gerekli (yukarı yuvarlı) > eldeki (aşağı yuvarlı)
        assert _need_gb(ref.minimum_bytes) == "6,0" and _have_gb(MAC_8GB[1]) == "5,7"

    def test_16gb_q4_7b_sigar_toplu_is_kuculur(self):
        hw = _info(MAC_16GB)
        assert hardware.general_gate(hw).allowed
        fit = hardware.assess(Q4_WEIGHTS, hardware.reference_kv(), hw=hw)
        assert fit.fits and 1 <= fit.batch_size < defaults.DEFAULT_BATCH_SIZE_HYMT_LOCAL
        assert fit.required_bytes <= MAC_16GB[1]
        bigger = hardware.assess(Q4_WEIGHTS, hardware.reference_kv(), hw=hw,
                                 max_batch=fit.batch_size + 1)
        assert bigger.batch_size == fit.batch_size       # bir fazlası sığmıyor
        note = hardware.batch_note(fit)
        assert note == (f"Bellek: aynı anda en çok {fit.batch_size} parça çevrilir "
                        f"(tahmini gerekli ~{_need_gb(fit.required_bytes)} GB, "
                        f"kullanılabilir {_have_gb(MAC_16GB[1])} GB).")

    def test_16gb_bf16_hymt_sigmaz(self):
        fit = hardware.assess(HYMT_WEIGHTS, HYMT_KV, hw=_info(MAC_16GB))
        assert fit.fits is False and fit.batch_size is None
        assert fit.required_bytes == fit.minimum_bytes
        assert hardware.too_big_message(fit) == (
            f"Bu model bu Mac'e sığmaz (gerekli ~{_need_gb(fit.minimum_bytes)} GB, "
            f"kullanılabilir {_have_gb(MAC_16GB[1])} GB).")
        assert hardware.too_big_label(fit).startswith("bu Mac'e sığmaz (gerekli ~")

    def test_36gb_hymt_toplu_is_degismez(self):
        hw = _info(MAC_36GB)
        assert hardware.general_gate(hw).allowed
        fit = hardware.assess(HYMT_WEIGHTS, HYMT_KV, hw=hw)
        assert fit.fits and fit.batch_size == defaults.DEFAULT_BATCH_SIZE_HYMT_LOCAL == 20
        assert hardware.batch_note(fit) == ""

    def test_esik_hassas_sinir(self):
        """Tam sınırda sığar, bir bayt eksikte sığmaz."""
        need = hardware.reference_fit(_info(MAC_36GB)).minimum_bytes
        exact = hardware.HardwareInfo(need, need, "Test", hardware.SOURCE_METAL)
        assert hardware.general_gate(exact).allowed
        short = hardware.HardwareInfo(need - 1, need, "Test", hardware.SOURCE_METAL)
        assert not hardware.general_gate(short).allowed

    @pytest.mark.parametrize("value,up,down", [
        (0, "0,0", "0,0"), (10 ** 9, "1,0", "1,0"), (5_902_156_637, "6,0", "5,9"),
        (5_726_623_061, "5,8", "5,7"), (30_150_672_384, "30,2", "30,1"),
    ])
    def test_gb_yuvarlama(self, value, up, down):
        assert hardware.format_gb(value, round_up=True) == up
        assert hardware.format_gb(value, round_up=False) == down

    def test_iletiler_turkce_ve_oom_alt_dizgisiz(self):
        texts = [hardware.MSG_HW_TOO_SMALL, hardware.MSG_HW_TOO_BIG, hardware.MSG_HW_UNMEASURED,
                 hardware.MSG_HW_FALLBACK, hardware.MSG_HW_CAPTION, hardware.MSG_HW_BATCH,
                 branding.MODEL_HW_CLOSED_LABEL, branding.MODEL_HW_TEX_HINT]
        for text in texts:
            assert not contracts.has_oom_trigger(text), text
            assert "Yol A" not in text and "Yol B" not in text
        assert "ğ" in hardware.MSG_HW_TOO_BIG and "ı" in hardware.MSG_HW_TOO_SMALL

    def test_kenar_cubugu_satiri(self):
        assert hardware.hardware_caption(_info(MAC_36GB, chip="Apple M4 Max")) == (
            "Apple M4 Max · yerel model için kullanılabilir bellek 30,1 GB")


# ===========================================================================
# 4. Keşif (model başına kapı) ve check_engine
# ===========================================================================

class TestKesif:
    def test_36gb_hymt_sigar_json(self, monkeypatch, mt_isolation, mlx_arch):
        _use(monkeypatch, MAC_36GB)
        _hymt_dir(mt_isolation.models_dir)
        report = discovery.discover_all()
        model = report.model_by_id(registry.DEFAULT_ENGINE)
        assert isinstance(model, discovery.MemoryAssessedModel)
        assert model.usable and model.size_bytes == HYMT_WEIGHTS
        assert model.memory.fits and model.memory.batch_size == 20
        data = discovery.report_to_json(report)
        memory = data["models"][0]["memory"]
        assert memory["fits"] is True and memory["batch_size"] == 20
        assert memory["kv_bytes_per_token"] == 128 * 1024
        assert memory["kv_source"] == "config.json"
        assert memory["available_bytes"] == MAC_36GB[1]
        assert memory["weights_bytes"] == HYMT_WEIGHTS

    def test_16gb_hymt_sigmaz(self, monkeypatch, mt_isolation, mlx_arch):
        _use(monkeypatch, MAC_16GB)
        _hymt_dir(mt_isolation.models_dir)
        model = discovery.discover_all().model_by_id(registry.DEFAULT_ENGINE)
        assert not model.usable and model.memory.fits is False
        assert model.reason == hardware.too_big_message(model.memory)
        label = runner.model_option_label(model)
        assert label.endswith(hardware.too_big_label(model.memory))
        assert branding.MODEL_UNUSABLE_LABEL not in label
        check = discovery.check_engine(registry.DEFAULT_ENGINE)
        assert not check.ok and check.reason == model.reason

    def test_8gb_genel_kapi_tum_modeller(self, monkeypatch, mt_isolation, mlx_arch):
        _use(monkeypatch, MAC_8GB)
        _hymt_dir(mt_isolation.models_dir)
        write_fake_mlx_dir(mt_isolation.models_dir / "kucuk-mlx", "llama")
        gate = hardware.general_gate()
        report = discovery.discover_all()
        assert report.models and all(not m.usable for m in report.models)
        tiny = report.model_by_id("mlx:kucuk-mlx")
        assert tiny.memory.fits is True          # küçük model sığar ama kart kapalı
        assert tiny.reason == gate.reason
        check = discovery.check_engine("mlx:kucuk-mlx")
        assert not check.ok and check.reason == gate.reason

    def test_boyut_yoksa_duz_kayit(self, monkeypatch, mt_isolation):
        # Yerleşik model kurulu değil: boyut yok -> tahmin yok, JSON biçimi aynı.
        report = discovery.discover_all()
        model = report.model_by_id(registry.DEFAULT_ENGINE)
        assert type(model) is contracts.DiscoveredModel
        assert "memory" not in discovery.report_to_json(report)["models"][0]

    def test_tablo_satirlari(self, monkeypatch, mt_isolation, mlx_arch, capsys):
        _use(monkeypatch, MAC_16GB, chip="Apple M2 Pro")
        _hymt_dir(mt_isolation.models_dir)
        assert discovery.main([]) == 0
        out = capsys.readouterr().out
        assert "Donanım: Apple M2 Pro · yerel model için kullanılabilir bellek 11,4 GB" in out
        assert "bellek: bu Mac'e sığmaz (gerekli ~" in out

    def test_tablo_8gb_neden(self, monkeypatch, mt_isolation, capsys):
        _use(monkeypatch, MAC_8GB)
        assert discovery.main([]) == 0
        assert hardware.general_gate().reason in capsys.readouterr().out

    def test_gguf_kv_basliktan_ve_sigmaz(self, monkeypatch, mt_isolation, fake_llama):
        _use(monkeypatch, MAC_16GB)
        path = write_fake_gguf(mt_isolation.models_dir / "buyuk-Q8_0.gguf", "llama",
                               extra_kv={"llama.block_count": (GGUF_TYPE_UINT32, 40),
                                         "llama.attention.head_count": (GGUF_TYPE_UINT32, 40),
                                         "llama.attention.head_count_kv": (GGUF_TYPE_UINT32, 8),
                                         "llama.embedding_length": (GGUF_TYPE_UINT32, 5120)})
        _sparse(path, 16 * 10 ** 9)
        model = discovery.discover_all().model_by_id("gguf:buyuk-Q8_0.gguf")
        assert model.memory.kv_source == hardware.KV_SOURCE_GGUF
        assert model.memory.kv_bytes_per_token == 40 * 8 * (128 + 128) * 2
        assert model.memory.mode == hardware.MODE_CONTEXT
        assert model.memory.kv_tokens == defaults.MT_CONTEXT_TOKENS
        assert not model.usable and model.reason == hardware.too_big_message(model.memory)
        check = discovery.check_engine("gguf:buyuk-Q8_0.gguf")
        assert not check.ok and check.reason == model.reason

    def test_gguf_kucuk_sigar(self, monkeypatch, mt_isolation, fake_llama):
        _use(monkeypatch, MAC_16GB)
        write_fake_gguf(mt_isolation.models_dir / "kucuk-Q4_K_M.gguf", "llama")
        model = discovery.discover_all().model_by_id("gguf:kucuk-Q4_K_M.gguf")
        assert model.usable and model.memory.fits
        assert model.memory.kv_source == hardware.KV_SOURCE_RATIO   # şekil anahtarı yok
        assert discovery.check_engine("gguf:kucuk-Q4_K_M.gguf").ok

    def test_ollama_buyuk_model_sigmaz_ek_istek_yok(self, monkeypatch, mt_isolation,
                                                    fake_llm_server):
        from tests.fakes.fake_llm_server import FakeModel

        _use(monkeypatch, MAC_16GB)
        srv = fake_llm_server("ollama", models=[FakeModel("dev:70b", size_bytes=40 * 10 ** 9),
                                                FakeModel("qwen3:8b")])
        cfg = contracts.SourcesConfig(ollama_url=srv.url,
                                      lmstudio_url=mt_isolation.closed_url + "/v1")
        monkeypatch.setattr(discovery, "load_sources", lambda path=None, *a, **k: cfg)
        report = discovery.discover_all()
        big, small = report.model_by_id("ollama:dev:70b"), report.model_by_id("ollama:qwen3:8b")
        assert not big.usable and big.memory.fits is False
        assert big.reason == hardware.too_big_message(big.memory)
        assert small.usable and small.memory.fits
        assert big.memory.mode == hardware.MODE_CONTEXT
        srv.reset_requests()
        check = discovery.check_engine("ollama:dev:70b")
        assert not check.ok and check.reason == big.reason
        # Donanım kapısı ek HTTP isteği göndermez (kilit testi istek dizisi aynı).
        assert [r.path for r in srv.request_details] == ["/api/tags", "/api/show"]

    def test_kayitli_sunucu_motoru_kendi_baglamiyla(self, monkeypatch, mt_isolation,
                                                    fake_llm_server):
        """mt_engines.json kaydı kendi num_ctx'iyle değerlendirilir; listedeki
        kaydın (varsayılan bağlam) hükmü kopyalanmaz."""
        from tests.fakes.fake_llm_server import FakeModel

        hw = _use(monkeypatch, MAC_16GB)
        weights = 6 * 10 ** 9
        srv = fake_llm_server("ollama", models=[FakeModel("orta:7b", size_bytes=weights,
                                                          context_length=131072)])
        cfg = contracts.SourcesConfig(ollama_url=srv.url,
                                      lmstudio_url=mt_isolation.closed_url + "/v1")
        monkeypatch.setattr(discovery, "load_sources", lambda path=None, *a, **k: cfg)
        # Varsayılan bağlamla sığan, 64k bağlamla sığmayan bir kayıt
        per_token = hardware.fallback_kv_bytes_per_token(weights)
        big_ctx = 65536
        assert hardware.assess(weights, None, mode=hardware.MODE_CONTEXT, hw=hw).fits
        assert not hardware.assess(weights, None, mode=hardware.MODE_CONTEXT,
                                   context_tokens=big_ctx, hw=hw).fits
        mt_isolation.user_specs_path.write_text(json.dumps({
            "genis": {"backend": "ollama", "model": "orta:7b", "base_url": srv.url,
                      "options": {"num_ctx": big_ctx}}}), encoding="utf-8")
        report = discovery.discover_all()
        listed, registered = report.model_by_id("ollama:orta:7b"), report.model_by_id("genis")
        assert listed.usable and listed.memory.kv_tokens == defaults.MT_CONTEXT_TOKENS
        assert registered.memory.kv_tokens == big_ctx
        assert registered.memory.kv_bytes_per_token == per_token
        assert not registered.usable
        assert registered.reason == hardware.too_big_message(registered.memory)

    def test_ollama_model_info_sekli(self):
        item = {"size": 4_920_753_328}
        show = {"model_info": {"general.architecture": "llama",
                               "llama.attention.head_count": 32,
                               "llama.attention.head_count_kv": 8, "llama.block_count": 32,
                               "llama.context_length": 8192, "llama.embedding_length": 4096}}
        mem = discovery._ollama_mem(item, show)
        assert mem.weights == 4_920_753_328 and mem.kv.source == hardware.KV_SOURCE_OLLAMA
        assert mem.context_tokens == 8192 and mem.mode == hardware.MODE_CONTEXT

    def test_lmstudio_boyutla_yedek_oran(self, monkeypatch, mt_isolation, fake_llm_server):
        from tests.fakes.fake_llm_server import FakeModel

        _use(monkeypatch, MAC_16GB)
        srv = fake_llm_server("lmstudio", models=[FakeModel("org/dev-70b",
                                                            size_bytes=40 * 10 ** 9)])
        cfg = contracts.SourcesConfig(ollama_url=mt_isolation.closed_url,
                                      lmstudio_url=srv.base_url)
        monkeypatch.setattr(discovery, "load_sources", lambda path=None, *a, **k: cfg)
        model = discovery.discover_all().model_by_id("lmstudio:org/dev-70b")
        assert model.memory.kv_source == hardware.KV_SOURCE_RATIO
        # 2026-09-30 (MT-12): sunucu modelinde oran yedeği kaba tahmindir; "sığmaz"
        # engel değil gerekçeli uyarıdır (yüklemeyi ve bellek denetimini sunucu yapar).
        assert model.usable and model.memory.fits is False
        assert model.memory.verdict == hardware.VERDICT_ADVISORY

    def test_hardware_check(self, monkeypatch, mt_isolation, mlx_arch):
        # Model yokken hüküm yok (ön kontrol söyler)
        _use(monkeypatch, MAC_16GB)
        assert discovery.hardware_check(registry.DEFAULT_ENGINE).ok
        _hymt_dir(mt_isolation.models_dir)
        check = discovery.hardware_check(registry.DEFAULT_ENGINE, hint="İPUCU")
        assert not check.ok and check.reason.startswith("Bu model bu Mac'e sığmaz")
        assert check.hint == ""                    # ipucu yalnız genel kapıda
        _use(monkeypatch, MAC_8GB)
        check = discovery.hardware_check(registry.DEFAULT_ENGINE, hint="İPUCU")
        assert not check.ok and check.reason == hardware.general_gate().reason
        assert check.hint == "İPUCU"
        _use(monkeypatch, MAC_36GB)
        assert discovery.hardware_check(registry.DEFAULT_ENGINE).ok
        assert discovery.hardware_check("gecersiz kimlik!").ok     # kimliği check_engine söyler

    def test_local_memory_fit(self, monkeypatch, mt_isolation, mlx_arch):
        _use(monkeypatch, MAC_36GB)
        assert discovery.local_memory_fit(registry.DEFAULT_ENGINE) is None   # model yok
        _hymt_dir(mt_isolation.models_dir)
        assert discovery.local_memory_fit(registry.DEFAULT_ENGINE).batch_size == 20
        assert discovery.local_memory_fit("ollama:qwen3:8b") is None         # sunucu
        _use(monkeypatch, MAC_16GB)
        _hymt_dir(mt_isolation.models_dir, weights=Q4_WEIGHTS, name="q4-mlx")
        fit = discovery.local_memory_fit("mlx:q4-mlx")
        assert fit.fits and fit.batch_size < 20


# ===========================================================================
# 5. Komut satırı (translate_book)
# ===========================================================================

def _make_pdf(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(50, 50, 550, 800),
                        "A small synthetic sentence about green apples.", fontname="helv",
                        fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def flow(monkeypatch):
    rec = SimpleNamespace(extraction=[], pipeline=[], batch=[], checks=[])
    monkeypatch.setattr(tb, "run_extraction",
                        lambda pdf, out, batch_size, device: rec.extraction.append(pdf) or True)
    monkeypatch.setattr(tb, "run_epub_extraction", lambda epub, out: True)
    monkeypatch.setattr(tb, "run_pipeline",
                        lambda output_dir, book_name, **kw: rec.pipeline.append(kw) or 0)
    monkeypatch.setattr(tb, "run_batch",
                        lambda files, args: rec.batch.append(list(files)) or {"failed": []})
    real_check = tb._check_engine
    monkeypatch.setattr(tb, "_check_engine",
                        lambda engine_id: rec.checks.append(engine_id) or real_check(engine_id))
    monkeypatch.delenv("KITAPCEVIR_EVENT_LOG", raising=False)
    yield rec
    for handler in list(logging.getLogger("pipeline.model").handlers):
        logging.getLogger("pipeline.model").removeHandler(handler)


def _main(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["translate_book.py", *map(str, argv)])
    with pytest.raises(SystemExit) as exc:
        tb.main()
    return exc.value.code


class TestKomutSatiri:
    def test_8gb_cikarimdan_once_5(self, tmp_path, monkeypatch, capsys, flow, mt_isolation):
        _use(monkeypatch, MAC_8GB)
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        out = tmp_path / "out"
        code = _main(monkeypatch, pdf, "--output-dir", out, "--dil-kontrolu-yok")
        assert code == tb.EXIT_MODEL_UNAVAILABLE == 5
        assert flow.extraction == [] and flow.pipeline == []
        err = capsys.readouterr().err
        reason = hardware.general_gate().reason
        assert f"HATA: {reason}" in err
        assert f"İpucu: Model gerektirmeyen {branding.quoted(branding.TEX_TITLE)}" in err
        assert "1_cikar.py" in err
        assert reason in (out / tb.PIPELINE_LOG_NAME).read_text(encoding="utf-8")
        assert not (out / "source_book.pdf").exists()

    def test_8gb_sunucu_motorunda_da(self, tmp_path, monkeypatch, capsys, flow, mt_isolation):
        _use(monkeypatch, MAC_8GB)
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        code = _main(monkeypatch, pdf, "--output-dir", tmp_path / "out", "--mt-engine",
                     "ollama:qwen3:8b", "--dil-kontrolu-yok")
        assert code == 5 and flow.checks == []          # ağa hiç gidilmedi
        assert hardware.general_gate().reason in capsys.readouterr().err

    def test_16gb_hymt_sigmaz_5(self, tmp_path, monkeypatch, capsys, flow, mt_isolation,
                                mlx_arch):
        _use(monkeypatch, MAC_16GB)
        _hymt_dir(mt_isolation.models_dir)
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        code = _main(monkeypatch, pdf, "--output-dir", tmp_path / "out", "--dil-kontrolu-yok")
        assert code == 5 and flow.extraction == []
        assert "HATA: Bu model bu Mac'e sığmaz (gerekli ~" in capsys.readouterr().err

    def test_36gb_degisiklik_yok(self, tmp_path, monkeypatch, flow, mt_isolation, mlx_arch):
        _use(monkeypatch, MAC_36GB)
        _hymt_dir(mt_isolation.models_dir)
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        assert _main(monkeypatch, pdf, "--output-dir", tmp_path / "out",
                     "--dil-kontrolu-yok") == 0
        assert flow.extraction and flow.pipeline[0]["mt_engine"] == registry.DEFAULT_ENGINE

    @pytest.mark.parametrize("flags", [["--export-tex"], ["--skip-translate"],
                                       ["--extract-only"]])
    def test_tex_yolu_8gb_de_calisir(self, tmp_path, monkeypatch, flow, flags):
        _use(monkeypatch, MAC_8GB)
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        code = _main(monkeypatch, pdf, "--output-dir", tmp_path / "out", *flags)
        assert code == 0 and flow.extraction

    def test_oncelik_kilit_sonra_dil_sonra_donanim(self, tmp_path, monkeypatch, flow):
        _use(monkeypatch, MAC_8GB)
        out = tmp_path / "out"
        (out / "source").mkdir(parents=True)
        (out / "source" / tb.MANUAL_MARKER_NAME).write_text("import: test\n", encoding="utf-8")
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        assert _main(monkeypatch, pdf, "--output-dir", out) == tb.EXIT_MANUAL_LOCK
        monkeypatch.setattr(tb, "_source_language_pregate",
                            lambda *a, **k: tb.EXIT_SOURCE_UNSUPPORTED)
        assert _main(monkeypatch, pdf, "--output-dir", tmp_path / "out2") == 4
        assert flow.extraction == []

    def test_batch_donguden_once_5(self, tmp_path, monkeypatch, capsys, flow):
        _use(monkeypatch, MAC_8GB)
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        assert _main(monkeypatch, pdf, "--batch") == 5
        assert flow.batch == []
        assert hardware.general_gate().reason in capsys.readouterr().err


class _Config:
    def __init__(self, mlx_batch_size=8):
        self.mlx_batch_size = mlx_batch_size


class TestUyarlanirTopluIs:
    def _fit(self, scenario, weights=Q4_WEIGHTS):
        return hardware.assess(weights, hardware.reference_kv(), hw=_info(scenario))

    def test_36gb_hic_bir_sey_degismez(self, caplog):
        config = _Config()
        assert tb._apply_memory_batch(config, self._fit(MAC_36GB, HYMT_WEIGHTS),
                                      logging.getLogger("t")) == {}
        assert config.mlx_batch_size == 8

    def test_hesap_yoksa_degismez(self):
        config = _Config()
        assert tb._apply_memory_batch(config, None, logging.getLogger("t")) == {}
        ctx = hardware.assess(Q4_WEIGHTS, None, mode=hardware.MODE_CONTEXT, hw=_info(MAC_16GB))
        assert tb._apply_memory_batch(config, ctx, logging.getLogger("t")) == {}
        assert config.mlx_batch_size == 8

    def test_16gb_ust_sinir_iner(self, caplog):
        fit = self._fit(MAC_16GB)
        config = _Config()
        with caplog.at_level(logging.INFO, logger="t"):
            extra = tb._apply_memory_batch(config, fit, logging.getLogger("t"))
        assert extra == {"translation_overrides": {"batch_size": fit.batch_size}}
        assert config.mlx_batch_size == min(8, fit.batch_size)
        assert hardware.batch_note(fit) in caplog.text

    def test_cok_dar_bellekte_alt_toplu_is_de_iner(self):
        need1 = hardware.assess(Q4_WEIGHTS, hardware.reference_kv(),
                                hw=_info(MAC_36GB)).minimum_bytes
        per_seq = 128 * 1024 * hardware.batch_sequence_tokens()
        tight = hardware.HardwareInfo(need1 + per_seq * 2, None, "Test", hardware.SOURCE_METAL)
        fit = hardware.assess(Q4_WEIGHTS, hardware.reference_kv(), hw=tight)
        assert fit.batch_size < 8
        config = _Config()
        extra = tb._apply_memory_batch(config, fit, logging.getLogger("t"))
        assert config.mlx_batch_size == fit.batch_size
        assert extra["translation_overrides"]["batch_size"] == fit.batch_size


class _FakeStats:
    segments_total = segments_translated = 10
    segments_failed = segments_skipped = 0


class _FakeTranslator:
    instances: list = []

    def __init__(self, **kwargs):
        type(self).instances.append(kwargs)
        self.stats = _FakeStats()

    def translate(self, latex_body, checkpoint_dir=None, resume=False,
                  progress_callback=None):
        return latex_body

    def backend_health(self):
        return None


@pytest.fixture
def pipeline_env(monkeypatch, tmp_path):
    root = logging.getLogger()
    old_handlers, old_level = list(root.handlers), root.level
    _FakeTranslator.instances = []
    monkeypatch.setattr("latex_pipeline.translator.LatexTranslator", _FakeTranslator)
    monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
    out = tmp_path / "kitap"
    (out / "source").mkdir(parents=True)
    (out / "full_book.md").write_text(
        "# Birinci Bölüm\n\n" + "A small synthetic sentence about green apples. " * 12,
        encoding="utf-8")
    yield out
    for handler in list(root.handlers):
        if handler not in old_handlers:
            handler.close()
    root.handlers[:] = old_handlers
    root.setLevel(old_level)


def _run(out: Path) -> int:
    return tb.run_pipeline(str(out), "Donanım Kitap", skip_translate=False, skip_compile=True,
                           skip_qc=True, book_type="science", pdf_path=None)


class TestRunPipeline:
    def test_36gb_cevirmene_ek_arguman_gecmez(self, pipeline_env, monkeypatch, mt_isolation,
                                              mlx_arch):
        _use(monkeypatch, MAC_36GB)
        _hymt_dir(mt_isolation.models_dir)
        _run(pipeline_env)
        kwargs = _FakeTranslator.instances[0]
        assert "translation_overrides" not in kwargs
        assert kwargs["pipeline_config"].mlx_batch_size == 8

    def test_16gb_q4_toplu_is_kuculur(self, pipeline_env, monkeypatch, mt_isolation, mlx_arch):
        _use(monkeypatch, MAC_16GB)
        _hymt_dir(mt_isolation.models_dir, weights=Q4_WEIGHTS)
        _run(pipeline_env)
        kwargs = _FakeTranslator.instances[0]
        batch = kwargs["translation_overrides"]["batch_size"]
        assert 1 <= batch < defaults.DEFAULT_BATCH_SIZE_HYMT_LOCAL
        assert kwargs["pipeline_config"].mlx_batch_size == min(8, batch)
        log = (pipeline_env / tb.PIPELINE_LOG_NAME).read_text(encoding="utf-8")
        assert f"Bellek: aynı anda en çok {batch} parça çevrilir" in log


# ===========================================================================
# 6. Arayüz (AppTest)
# ===========================================================================

APP = PROJECT_ROOT / "app.py"
TRANSLATE_KEY = "model_translate"


class _Discovery:
    def __init__(self, report):
        self.report = report
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.report


class _StubMemory:
    def __init__(self, *a, **k):
        pass

    def get_stats(self):
        return {"total_entries": 0, "confirmed_entries": 0}

    def find_similar(self, *a, **k):
        return []

    def close(self):
        pass


def _assessed(engine_id, label, source, backend, weights, kv, hw, **extra):
    base = contracts.DiscoveredModel(engine_id=engine_id, label=label, source=source,
                                     backend=backend, format_guess="hymt", usable=True,
                                     size_bytes=weights, **extra)
    fit = hardware.assess(weights, kv, hw=hw)
    usable = fit.fits is not False
    values = {f.name: getattr(base, f.name) for f in fields(contracts.DiscoveredModel)}
    values.update(usable=usable, reason="" if usable else hardware.too_big_message(fit))
    return discovery.MemoryAssessedModel(**values, memory=fit)


@pytest.fixture
def ui_env(monkeypatch, tmp_path, mt_isolation):
    store: dict = {}

    def cache_resource(func=None, **_options):
        if func is None:
            return cache_resource

        @functools.wraps(func)
        def cached(*args, **kwargs):
            if func.__name__ not in store:
                store[func.__name__] = func(*args, **kwargs)
            return store[func.__name__]

        return cached

    root = tmp_path / "proje"
    (root / "output").mkdir(parents=True)
    monkeypatch.setattr(runner, "PROJECT_ROOT", root)
    monkeypatch.setattr(streamlit, "cache_resource", cache_resource)
    monkeypatch.setattr(core.memory, "TranslationMemory", _StubMemory)
    monkeypatch.setattr(runner, "memory_db_path", lambda: tmp_path / "bellek_yok.db")
    monkeypatch.setattr(runner, "platform_problem", lambda: None)
    monkeypatch.setattr(runner, "local_model_dir", lambda: Path("/model"))
    temp_root = tmp_path / "gecici"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    status = SourceStatus("mlx", "MLX klasörleri", None,
                          contracts.MSG_SOURCE_COUNT_DIRS.format(n=0))
    disc = _Discovery(DiscoveryReport(models=(), sources=(status,), scanned_at=time.time()))
    monkeypatch.setattr(runner, "discover_models", disc)
    monkeypatch.setattr(runner, "engine_check",
                        lambda engine_id: contracts.EngineCheck(engine_id, True, engine_id))
    book = tmp_path / "Donanim Sentetik.pdf"
    with fitz.open() as doc:
        doc.new_page()
        doc.save(str(book))
    store["_job"] = {"book": book, "upload_key": (book.name, book.stat().st_size),
                     "run": None, "run_book": None, "run_name": None, "name": None}
    return SimpleNamespace(store=store, discovery=disc, status=status)


def _run_app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def _buttons(at) -> dict:
    return {button.key: button for button in at.button}


def _texts(elements) -> list:
    return [element.value for element in elements]


class TestArayuz:
    def test_8gb_kart_kapali_neden_tex_acik(self, monkeypatch, ui_env):
        _use(monkeypatch, MAC_8GB, chip="Apple M1")
        at = _run_app()
        reason = hardware.general_gate().reason
        assert reason in _texts(at.warning)
        assert branding.MODEL_HW_TEX_HINT in _texts(at.caption)
        buttons = _buttons(at)
        assert buttons[TRANSLATE_KEY].disabled
        assert not buttons["tex_extract"].disabled               # TeX kartı normal
        assert "model_scan" not in buttons                        # seçici / tarama yok
        assert not [s for s in at.selectbox if s.key == "mt_engine"]
        assert ui_env.discovery.calls == 0                        # model aranmadı
        captions = _texts(at.sidebar.caption)
        assert f"macOS · {hardware.hardware_caption()}" in captions
        assert f"Yerel model: {branding.MODEL_HW_CLOSED_LABEL}" in captions

    def test_16gb_seciside_sigmaz_ve_cevir_kapali(self, monkeypatch, ui_env):
        hw = _use(monkeypatch, MAC_16GB)
        big = _assessed(registry.DEFAULT_ENGINE, "HY-MT1.5 7B", "builtin", "mlx",
                        HYMT_WEIGHTS, HYMT_KV, hw, detail="mimari: hunyuan_v1_dense")
        ui_env.discovery.report = DiscoveryReport(models=(big,), sources=(ui_env.status,),
                                                  scanned_at=time.time())
        at = _run_app()
        picker = [s for s in at.selectbox if s.key == "mt_engine"][0]
        label = runner.model_option_label(big)
        assert "bu Mac'e sığmaz (gerekli ~" in label
        # K6 (2026-10-05): arayüz "ad · kaynak"tan sonra önerilen işaretini ve
        # düşünme sınıfını ekler; keşfin etiketinin geri kalanı aynen durur.
        head = "HY-MT1.5 7B · yerleşik"
        assert [o for o in picker.options
                if o.startswith(head) and o.endswith(label[len(head):])] == picker.options
        assert _buttons(at)[TRANSLATE_KEY].disabled
        assert hardware.too_big_message(big.memory) in _texts(at.caption)
        assert not _buttons(at)["tex_extract"].disabled

    def test_16gb_q4_model_hazir_toplu_is_notu(self, monkeypatch, ui_env):
        hw = _use(monkeypatch, MAC_16GB)
        q4 = _assessed(registry.DEFAULT_ENGINE, "HY-MT 4 bit", "builtin", "mlx",
                       Q4_WEIGHTS, HYMT_KV, hw)
        ui_env.discovery.report = DiscoveryReport(models=(q4,), sources=(ui_env.status,),
                                                  scanned_at=time.time())
        at = _run_app()
        assert not _buttons(at)[TRANSLATE_KEY].disabled
        assert hardware.batch_note(q4.memory) in _texts(at.caption)

    def test_36gb_degisiklik_yok(self, monkeypatch, ui_env):
        hw = _use(monkeypatch, MAC_36GB, chip="Apple M4 Max")
        model = _assessed(registry.DEFAULT_ENGINE, "HY-MT1.5 7B", "builtin", "mlx",
                          HYMT_WEIGHTS, HYMT_KV, hw)
        ui_env.discovery.report = DiscoveryReport(models=(model,), sources=(ui_env.status,),
                                                  scanned_at=time.time())
        at = _run_app()
        assert not _buttons(at)[TRANSLATE_KEY].disabled
        assert not at.warning
        captions = _texts(at.caption)
        assert not [c for c in captions if c.startswith("Bellek:")]
        assert ("macOS · Apple M4 Max · yerel model için kullanılabilir bellek 30,1 GB"
                in _texts(at.sidebar.caption))

    def test_olculemezse_uyari_kart_acik(self, monkeypatch, ui_env):
        info = hardware.HardwareInfo(None, None, "", "", "sysctl hw.memsize okunamadı")
        monkeypatch.setattr(hardware, "_measure", lambda: info)
        monkeypatch.setattr(hardware, "_CACHE", None)
        at = _run_app()
        assert hardware.general_gate().warning in _texts(at.caption)
        assert not _buttons(at)[TRANSLATE_KEY].disabled

    def test_runner_hazir_olma_kapida(self, monkeypatch):
        _use(monkeypatch, MAC_8GB)
        ready, reason = runner.engine_ready(registry.DEFAULT_ENGINE, None)
        assert not ready and reason == hardware.general_gate().reason


# ===========================================================================
# 7. Sabitler ve belge
# ===========================================================================

HW_NAMES = [name for name in defaults.__all__ if name.startswith("HW_")]
ASSUMPTIONS_DOC = PROJECT_ROOT / "docs" / "DONANIM_VARSAYIMLARI.md"


class TestSabitlerVeBelge:
    def test_sabitler_defaults_ta(self):
        assert len(HW_NAMES) >= 9
        for name in HW_NAMES:
            assert hasattr(defaults, name), name
        assert 0 < defaults.HW_WORKSPACE_FRACTION < 1
        assert 0 < defaults.HW_WORKING_SET_FALLBACK_RATIO < 1

    def test_belge_her_sabiti_kaynakliyor(self):
        text = ASSUMPTIONS_DOC.read_text(encoding="utf-8")
        for name in HW_NAMES:
            assert name in text, name
        for field_name in ("Claim:", "Evidence:", "Confidence:", "Date checked:",
                           "What may change:"):
            assert text.count(field_name) >= 6, field_name
        assert "https://" in text
        assert "yalnız sayı değişir" in text

    @pytest.mark.parametrize("relpath", ["README.md", "docs/KURULUM_VE_KULLANIM.md",
                                         "docs/Kullanma_Kilavuzu.tex"])
    def test_donanim_gereksinimi_bolumu(self, relpath):
        text = (PROJECT_ROOT / relpath).read_text(encoding="utf-8").replace("\\_", "_")
        assert "Donanım gereksinimi" in text
        assert "DONANIM_VARSAYIMLARI" in text

    def test_kilavuz_pdf_guncel(self):
        tex = PROJECT_ROOT / "docs" / "Kullanma_Kilavuzu.tex"
        pdf = PROJECT_ROOT / "docs" / "Kullanma_Kilavuzu.pdf"
        assert pdf.stat().st_mtime >= tex.stat().st_mtime - 1
