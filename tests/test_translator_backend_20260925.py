"""K1 — BookTranslator / LatexTranslator arka uç entegrasyonu (TASARIM H.2 test 5, 6, 13, 15, 16, 17).

Sahte Ollama sunucusu (tests/fakes) + ``core.mt_engines.create_engine``
yaması ile motor tanımı doğrudan verilir (K2 kimlik çözümüne bağımlılık yok).
Model YÜKLENMEZ; çeviri belleği kapalıdır (data/translation_memory*.db'ye
dokunulmaz). Girdiler sentetiktir.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import core.mt_engines as cme
from core.mt_engines import backends, http_util
from core.mt_engines.backends import create_backend
from core.mt_engines.base import EngineSpec
from core.mt_engines.chat import ChatTemplateEngine
from core.mt_engines.contracts import (
    MSG_OLLAMA_DOWN,
    BackendUnavailable,
)
from core.mt_engines.hymt import HYMTFormatEngine
from core.translator import (
    _PLACEHOLDER_CLAUSE,
    BookTranslator,
    TranslationConfig,
    _active_backend,
)
from tests.fakes import FakeReply

_OFF = dict(use_memory=False, glossary_mode="off", run_qa=False, check_consistency=False)


def _spec(url, model="qwen3:8b", **kw):
    kw.setdefault("format", "chat")
    return EngineSpec(name=f"ollama:{model}", backend="ollama", model=model,
                      base_url=url, source="adhoc", **kw)


def _patch_engine(monkeypatch, spec_or_fn):
    """create_engine -> verilen spec'in motoru (spec_or_fn çağrılabilirse her seferinde)."""
    def _make(name, *a, **k):
        spec = spec_or_fn() if callable(spec_or_fn) else spec_or_fn
        return (HYMTFormatEngine if spec.format == "hymt" else ChatTemplateEngine)(spec)
    monkeypatch.setattr(cme, "create_engine", _make)


@pytest.fixture
def make_bt(monkeypatch):
    def _make(spec, **cfg):
        _patch_engine(monkeypatch, spec)
        return BookTranslator(TranslationConfig(mt_engine=spec.name, **{**_OFF, **cfg}))
    return _make


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(backends, "_sleep", lambda s: None)


def _chat_bodies(srv):
    return srv.requests_to("/api/chat")


def _user_content(body):
    return body["messages"][-1]["content"]


# ---------------------------------------------------------------------------
# Kurulum ve kısa devre (A.2, B.4 madde 2-3, H.2 test 15-16)
# ---------------------------------------------------------------------------
class TestKurulum:
    def test_arka_uc_kurulur_model_yok_kisa_devre_yok(self, fake_ollama, make_bt):
        bt = make_bt(_spec(fake_ollama.url))
        assert bt.mt_model is None and bt.mt_tokenizer is None
        assert bt._use_mlx is False
        assert _active_backend(bt) is not None
        # Kısa devre (model/tokenizer yoksa metni çevirmeden döndür) arka uçta YOK
        assert bt.translate_text("Hello world.") == "ÇEVRİLDİ: Hello world."
        assert len(_chat_bodies(fake_ollama)) == 1

    def test_mlx_yolunda_kisa_devre_aynen(self):
        bt = BookTranslator.__new__(BookTranslator)
        bt.config = TranslationConfig(mt_engine="hymt-7b", **_OFF)
        bt.mt_model = None
        bt.mt_tokenizer = None
        assert bt._translate_with_mt("Hello world.") == "Hello world."

    def test_sahte_self_arka_uc_sanilmaz(self):
        # MagicMock self'in kendiliğinden ürettiği _backend özniteliği MLX
        # yolunu atlatmamalı (mevcut testler metotları MagicMock ile çağırıyor).
        assert _active_backend(MagicMock()) is None

    def test_hymt7b_icin_mlx_yolu(self):
        from core.mt_engines import create_engine
        assert create_backend(create_engine("hymt-7b").spec) is None
        bt = BookTranslator.__new__(BookTranslator)
        bt.config = TranslationConfig(mt_engine="hymt-7b", **_OFF)
        bt._init_hymt_model = MagicMock()
        bt._init_mt_model()
        bt._init_hymt_model.assert_called_once_with(bt.config.hymt_7b_model)
        assert bt._backend is None

    def test_sahte_motor_mlx_yoluna_gider(self, monkeypatch):
        monkeypatch.setattr(cme, "create_engine", lambda name, *a, **k: MagicMock())
        bt = BookTranslator.__new__(BookTranslator)
        bt.config = TranslationConfig(mt_engine="benim-model", **_OFF)
        bt._init_hymt_model = MagicMock()
        bt._init_mt_model()
        bt._init_hymt_model.assert_called_once()
        assert bt._backend is None

    def test_yukleme_hatasi_sarilmadan_yukari_cikar(self, mt_isolation, make_bt):
        with pytest.raises(BackendUnavailable) as exc:
            make_bt(_spec(mt_isolation.closed_url))
        assert type(exc.value) is BackendUnavailable
        assert str(exc.value) == MSG_OLLAMA_DOWN.format(url=mt_isolation.closed_url)

    def test_dil_kodlari_arka_uc_yolunda_da_kurulur(self, fake_ollama, make_bt):
        bt = make_bt(_spec(fake_ollama.url))
        assert (bt._hymt_src_lang, bt._hymt_tgt_lang) == ("en", "tr")

    def test_istatistik_ve_saglik(self, fake_ollama, make_bt):
        bt = make_bt(_spec(fake_ollama.url))
        stats = bt.get_stats()
        assert stats["mt_model"] == f"Ollama · qwen3:8b · {fake_ollama.url}"
        assert stats["segments_truncated"] == 0
        assert bt.backend_health() is None

    def test_mlx_istatistigi_eski_deger(self):
        bt = BookTranslator.__new__(BookTranslator)
        bt.config = TranslationConfig(mt_engine="hymt-7b", **_OFF)
        bt.stats = {}
        bt.memory = None
        bt.glossary = None
        assert bt.get_stats()["mt_model"] == bt.config.hymt_7b_model
        assert bt.backend_health() is None


# ---------------------------------------------------------------------------
# Tekil yol: temizlik zinciri ve hata sözleşmesi (H.2 test 5, 6)
# ---------------------------------------------------------------------------
class TestTekilYol:
    def test_dusunme_blogu_temizlenir(self, fake_llm_server, make_bt):
        srv = fake_llm_server("ollama", think_block=True, reply=lambda t: "Bugün hava güzel.")
        bt = make_bt(_spec(srv.url))
        assert bt.translate_text("The weather is nice today.") == "Bugün hava güzel."

    @pytest.mark.parametrize("prefix", ["Here is the translation: ", "Çeviri: ",
                                        "İşte çeviri:\n"])
    def test_onsoz_temizlenir(self, fake_llm_server, make_bt, prefix):
        srv = fake_llm_server("ollama", prefix=prefix, reply=lambda t: "Bugün hava güzel.")
        bt = make_bt(_spec(srv.url))
        assert bt.translate_text("The weather is nice today.") == "Bugün hava güzel."

    def test_sarmalayici_tirnak(self, fake_llm_server, make_bt):
        srv = fake_llm_server("ollama", reply=lambda t: '"Bugün hava güzel."')
        bt = make_bt(_spec(srv.url))
        assert bt.translate_text("The weather is nice today.") == "Bugün hava güzel."
        # Kaynak tırnaklıysa tırnak korunur
        assert bt.translate_text('"The weather is nice today."') == '"Bugün hava güzel."'

    def test_kapanmamis_think_mt_fail(self, fake_llm_server, make_bt):
        srv = fake_llm_server("ollama", reply=lambda t: "<think>bütçe düşünmeye gitti")
        bt = make_bt(_spec(srv.url))
        text = "The weather is nice today."
        assert bt._translate_with_mt(text) == "[MT_FAIL]" + text
        # translate_text öneki siler, özgün metni döndürür (bugünkü sözleşme)
        assert bt.translate_text(text) == text

    def test_oge_hatasi_mt_fail(self, fake_llm_server, make_bt, no_sleep):
        srv = fake_llm_server("ollama", reply=lambda t: FakeReply(status=400, error="bozuk istek"))
        bt = make_bt(_spec(srv.url))
        text = "The weather is nice today."
        assert bt._translate_with_mt(text) == "[MT_FAIL]" + text
        assert bt.backend_health() is None

    def test_kesik_cikti_sayilir(self, fake_llm_server, make_bt):
        srv = fake_llm_server("ollama", finish_reason="length", reply=lambda t: "Bugün hava")
        bt = make_bt(_spec(srv.url))
        assert bt.translate_text("The weather is nice today.") == "Bugün hava"
        assert bt.get_stats()["segments_truncated"] == 1

    def test_yer_tutucu_cumlesi_ve_yanki_silinir(self, fake_llm_server, make_bt):
        srv = fake_llm_server("ollama", reply=lambda t: (
            _PLACEHOLDER_CLAUSE + "Değer <<LATEX_3>> burada gösterilir." if "<<LATEX_3>>" in t
            else "Bugün hava güzel."))
        bt = make_bt(_spec(srv.url))
        out = bt.translate_text("The value <<LATEX_3>> is shown here.")
        assert out == "Değer <<LATEX_3>> burada gösterilir."
        assert _PLACEHOLDER_CLAUSE.strip() in _user_content(_chat_bodies(srv)[-1])
        bt.translate_text("The weather is nice today.")
        assert _PLACEHOLDER_CLAUSE.strip() not in _user_content(_chat_bodies(srv)[-1])

    def test_baslik_talimati(self, fake_ollama, make_bt):
        bt = make_bt(_spec(fake_ollama.url))
        bt._translate_with_mt("Introduction", is_heading=True)
        assert _user_content(_chat_bodies(fake_ollama)[-1]).startswith(
            "Translate this heading from English to Turkish.")

    def test_devre_acikken_tekil_cagri_istisna_yukari_cikar(self, fake_ollama, make_bt,
                                                             monkeypatch):
        bt = make_bt(_spec(fake_ollama.url))
        backend = _active_backend(bt)
        monkeypatch.setattr(backend, "_circuit_reason", "Model sunucusuyla bağlantı koptu (x)")
        n = len(fake_ollama.requests)
        text = "The weather is nice today."
        # G2b-2 (h): kopma / devre istisnası yutulmaz; latex_pipeline parçayı özgün
        # bırakır, translate_book çıkış 5 verir
        with pytest.raises(BackendUnavailable):
            bt._translate_with_mt(text)
        assert len(fake_ollama.requests) == n
        assert bt.backend_health() == "Model sunucusuyla bağlantı koptu (x)"


# ---------------------------------------------------------------------------
# Toplu yol (H.2 test 13, 15)
# ---------------------------------------------------------------------------
class TestTopluYol:
    TEXTS = ["The weather is nice today.", "The garden is green again.",
             "The river flows to the sea."]

    def test_bire_bir(self, fake_ollama, make_bt):
        bt = make_bt(_spec(fake_ollama.url))
        out = bt.translate_batch_protected(list(self.TEXTS))
        assert out == ["ÇEVRİLDİ: " + t for t in self.TEXTS]

    def test_oge_hatasi_girdi_aynen_mt_fail_yok(self, fake_llm_server, make_bt):
        srv = fake_llm_server("ollama", reply=lambda t: (
            "<think>yarım" if "garden" in t else "Tamam: " + t))
        bt = make_bt(_spec(srv.url))
        errors_before = getattr(bt, "_mt_error_count", 0)
        out = bt.translate_batch_protected(list(self.TEXTS))
        assert len(out) == 3
        assert out[1] == self.TEXTS[1]                   # echo (girdi aynen)
        assert all("[MT_FAIL]" not in o for o in out)
        assert out[0] == "Tamam: " + self.TEXTS[0]
        assert bt._mt_error_count == errors_before + 1

    def test_ham_batch_yolu(self, fake_ollama, make_bt):
        bt = make_bt(_spec(fake_ollama.url))
        texts = ["A short line here.", "The value <<LATEX_1>> is here."]
        out = bt._translate_batch_hymt(texts)
        assert out == ["ÇEVRİLDİ: " + t for t in texts]
        contents = [_user_content(b) for b in _chat_bodies(fake_ollama)[-2:]]
        assert _PLACEHOLDER_CLAUSE.strip() not in contents[0]
        assert _PLACEHOLDER_CLAUSE.strip() in contents[1]
        assert bt._translate_batch_hymt([]) == []

    def test_kilit_tum_batchi_sarar(self, fake_ollama, make_bt, monkeypatch):
        bt = make_bt(_spec(fake_ollama.url))
        backend = _active_backend(bt)
        seen = []
        real = backend.generate_batch

        def spy(engine, reqs):
            seen.append(BookTranslator._mt_generate_lock.locked())
            return real(engine, reqs)

        monkeypatch.setattr(backend, "generate_batch", spy)
        bt._translate_batch_hymt(["One sentence here.", "Two sentence here."])
        assert seen == [True]
        assert not BookTranslator._mt_generate_lock.locked()

    def test_tekil_uretim_de_kilitli(self, fake_ollama, make_bt, monkeypatch):
        bt = make_bt(_spec(fake_ollama.url))
        backend = _active_backend(bt)
        seen = []
        real = backend.generate
        monkeypatch.setattr(backend, "generate", lambda e, r: (
            seen.append(BookTranslator._mt_generate_lock.locked()) or real(e, r)))
        bt._translate_with_mt("One sentence here.")
        assert seen == [True]

    def test_arka_uc_kullanilamaz_yukari_cikar(self, fake_ollama, make_bt, monkeypatch):
        # latex_pipeline _batch_mt_call bu istisnayı yakalayıp sıralı yedeğe düşer.
        bt = make_bt(_spec(fake_ollama.url))
        backend = _active_backend(bt)

        def down(engine, reqs):
            raise BackendUnavailable("Model sunucusuyla bağlantı koptu (x)")

        monkeypatch.setattr(backend, "generate_batch", down)
        with pytest.raises(BackendUnavailable):
            bt.translate_batch_protected(list(self.TEXTS))
        assert not BookTranslator._mt_generate_lock.locked()

    def test_uzun_metin_olceklenir(self, fake_ollama, make_bt):
        bt = make_bt(_spec(fake_ollama.url))
        long_text = "word " * 600
        bt._translate_batch_hymt([long_text.strip(), "Short one here."])
        body = _chat_bodies(fake_ollama)[-1]
        assert body["options"]["num_predict"] == bt._scale_max_tokens([long_text.strip()], 512)
        assert body["options"]["num_predict"] > 512


# ---------------------------------------------------------------------------
# LatexTranslator: yükleme hatası önbelleği ve sağlık (H.2 test 17)
# ---------------------------------------------------------------------------
def _latex_translator():
    from latex_pipeline.config import PipelineConfig
    from latex_pipeline.translator import LatexTranslator
    return LatexTranslator(
        pipeline_config=PipelineConfig(translate=True, segment_timeout=30),
        mt_engine="ollama:qwen3:8b", source_lang="en", target_lang="tr",
        translation_overrides=dict(_OFF))


class TestLatexTranslator:
    def test_yukleme_hatasi_onbelleklenir(self, monkeypatch, mt_isolation):
        _patch_engine(monkeypatch, _spec(mt_isolation.closed_url))
        calls = []
        real = http_util.post_json
        monkeypatch.setattr(http_util, "post_json",
                            lambda *a, **k: calls.append(a[0]) or real(*a, **k))
        lt = _latex_translator()
        expected = MSG_OLLAMA_DOWN.format(url=mt_isolation.closed_url)
        with pytest.raises(BackendUnavailable) as first:
            lt._get_translator()
        assert str(first.value) == expected
        n = len(calls)
        assert n >= 1
        with pytest.raises(BackendUnavailable) as second:
            lt._get_translator()
        assert str(second.value) == expected
        assert len(calls) == n                           # yeniden yükleme YOK
        assert lt.backend_health() == expected
        # Önbellekli yükleme hatasında tekil çağrı hızla None döner (metin korunur)
        assert lt._mt_call_with_timeout("Hello world.") is None
        assert len(calls) == n

    def test_translate_basinda_onbellek_temizlenir(self, monkeypatch, mt_isolation,
                                                   fake_ollama):
        holder = {"url": mt_isolation.closed_url}
        _patch_engine(monkeypatch, lambda: _spec(holder["url"]))
        lt = _latex_translator()
        with pytest.raises(BackendUnavailable):
            lt._get_translator()
        holder["url"] = fake_ollama.url                  # sunucu açıldı
        out = lt.translate("The weather is nice today.")
        assert "ÇEVRİLDİ" in out
        assert lt.backend_health() is None
        assert lt._translator is not None

    def test_yuklu_cevirmenin_sagligi(self, monkeypatch, fake_ollama):
        _patch_engine(monkeypatch, _spec(fake_ollama.url))
        lt = _latex_translator()
        assert lt.backend_health() is None               # henüz yüklenmedi
        translator = lt._get_translator()
        assert lt.backend_health() is None
        monkeypatch.setattr(_active_backend(translator), "_circuit_reason", "devre açık")
        assert lt.backend_health() == "devre açık"

    def test_mlx_hatasi_onbellege_alinmaz(self, monkeypatch):
        lt = _latex_translator()
        lt.mt_engine = "hymt-7b"

        class Boom(Exception):
            pass

        import core.translator as ct
        monkeypatch.setattr(ct.BookTranslator, "__init__",
                            lambda self, config: (_ for _ in ()).throw(Boom("mlx")))
        for _ in range(2):
            with pytest.raises(Boom):
                lt._get_translator()
        assert getattr(lt, "_translator_error", None) is None
        assert lt.backend_health() is None
