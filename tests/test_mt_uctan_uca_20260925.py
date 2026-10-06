"""Yerel model katmanı — uçtan uca sahte test (TASARIM H.6.2; Vk, 28 Eylül 2026).

Model YÜKLENMEZ, gerçek Ollama / LM Studio'ya gidilmez: sahte sunucular
(tests/fakes) ve sahte ``llama_cpp`` kullanılır. Çeviri belleği kapalıdır
(``use_memory=False``); ``data/translation_memory.db``'ye dokunulmaz.

Kapsam (kulvarlar arası):

1. ``LatexTranslator(mt_engine="ollama:sahte")`` gerçek registry yolundan
   (K2 ``resolve_spec`` -> ``discovery.load_sources``) sahte Ollama'ya bağlanır
   (K1 ``OllamaBackend``), küçük bir LaTeX gövdesini (başlık, yer tutuculu
   paragraf, dipnot, atıf) çevirir; yer tutucular korunur, model ham LaTeX
   görmez, ``backend_health()`` None.
2. Yer tutucu yutan model: LaTeX yapıları yine kaybolmaz.
3. Sunucu ORTADA durdurulunca: devre açılır, ``backend_health()`` dolu
   (``MSG_SERVER_LOST``), kalan segmentler ağa gitmeden hızlı geçer; bu
   neden ``translate_book``'un F3 kapısının okuduğu yardımcıdan aynen gelir.
4. Sunucu baştan kapalı: yükleme hatası önbelleklenir (segment başına
   yeniden bağlanma yok).
5. Sözleşme turu: keşfin (K2) verdiği her kimlik registry'de çözülür, doğru
   arka uç sınıfını üretir (K1), ``check_engine`` ile keşif aynı hükmü verir,
   arayüzün komutu (K3) CLI ayrıştırıcısından aynen geçer.

Test girdileri sentetiktir (kitap metni kopyalanmadı).
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest

from config.defaults import (
    DEFAULT_RETRY_BASE_DELAY_S,
    DEFAULT_RETRY_COUNT,
    MT_CONTEXT_TOKENS,
    MT_GREEDY_SEED,
    MT_GREEDY_TEMPERATURE,
    MT_HTTP_CIRCUIT_MAX_FAILURES,
)
from core.mt_engines import backends, discovery, http_util, registry
from core.mt_engines.contracts import (
    MSG_OLLAMA_DOWN,
    MSG_SERVER_LOST,
    BackendUnavailable,
    OpenAIServer,
    SourcesConfig,
)

pytestmark = pytest.mark.sahte_sunucu

ENGINE_ID = "ollama:sahte"
MODEL = "sahte"
PARAGRAPH_COUNT = 12

# ---------------------------------------------------------------------------
# Sentetik gövde ve "çevirmen"
# ---------------------------------------------------------------------------
# Sözcük sözcük sahte Türkçe: yankı (untranslated) sezgisine takılmasın diye
# her İngilizce sözcük farklı bir Türkçe sözcüğe döner; yer tutucular AYNEN.
_WORDS = {
    "the": "bu", "equation": "denklem", "gives": "verir", "rest": "durgun",
    "energy": "enerji", "see": "bakınız", "appendix": "ek", "we": "biz",
    "cite": "anıyoruz", "and": "ve", "refer": "başvururuz", "to": "şuna",
    "section": "bölüm", "paragraph": "paragraf", "describes": "anlatır",
    "orbit": "yörünge", "of": "ait", "a": "bir", "small": "küçük", "moon": "uydu",
    "introduction": "giriş",
}
_PLACEHOLDER_SPLIT_RE = re.compile(r"(<<[A-Z]+_\d+>>)")
_WORD_RE = re.compile(r"[A-Za-z]+")


def _fake_turkish(text: str, *, keep_placeholders: bool = True) -> str:
    def word(m: re.Match) -> str:
        tr = _WORDS.get(m.group(0).lower(), "sözcük")
        return tr.capitalize() if m.group(0)[0].isupper() else tr

    parts = []
    for part in _PLACEHOLDER_SPLIT_RE.split(text):
        if _PLACEHOLDER_SPLIT_RE.fullmatch(part):
            if keep_placeholders:
                parts.append(part)
        else:
            parts.append(_WORD_RE.sub(word, part))
    return "".join(parts)


def _paragraph(i: int) -> str:
    return f"Paragraph {i} describes the orbit of a small moon."


def _body(paragraphs: int = PARAGRAPH_COUNT) -> str:
    return (
        "\\section{Introduction}\\label{sec:giris}\n\n"
        "The equation $E = mc^2$ gives the rest energy.\\footnote{See the appendix.}\n\n"
        "We cite \\cite{ref1} and refer to Section~\\ref{sec:giris}.\n\n"
        + "\n\n".join(_paragraph(i) for i in range(1, paragraphs + 1))
        + "\n"
    )


# Çıktıda kaybolmaması gereken LaTeX yapıları
_LATEX_KEPT = ("\\section{", "\\label{sec:giris}", "$E = mc^2$", "\\footnote{",
               "\\cite{ref1}", "\\ref{sec:giris}")
# Modelin ASLA görmemesi gereken ham LaTeX (yer tutucuya dönmüş olmalı)
_LATEX_HIDDEN = ("$E = mc^2$", "\\cite", "\\ref{", "\\footnote", "\\label")


def _make_translator(engine_id: str = ENGINE_ID):
    from latex_pipeline.config import PipelineConfig
    from latex_pipeline.translator import LatexTranslator

    return LatexTranslator(
        pipeline_config=PipelineConfig(translate=True, segment_timeout=30),
        mt_engine=engine_id,
        source_lang="en",
        target_lang="tr",
        translation_overrides=dict(use_memory=False, glossary_mode="off",
                                   run_qa=False, check_consistency=False),
    )


def _point_ollama_to(monkeypatch, url: str) -> None:
    """``ollama:`` kimlikleri bu sunucuya çözülsün (mt_sources.json eşdeğeri)."""
    sources = SourcesConfig(ollama_url=url, lmstudio_url="http://127.0.0.1:9/v1",
                            openai_servers=())
    monkeypatch.setattr(discovery, "load_sources", lambda path=None: sources)


@pytest.fixture
def net_spy(monkeypatch):
    """Arka uçların HTTP çağrılarını ve yeniden deneme beklemelerini say (bekleme yok)."""
    calls = {"post": [], "get": [], "sleep": []}
    real_post, real_get = http_util.post_json, http_util.get_json

    def post(url, body, timeout):
        calls["post"].append(url)
        return real_post(url, body, timeout)

    def get(url, timeout):
        calls["get"].append(url)
        return real_get(url, timeout)

    monkeypatch.setattr(http_util, "post_json", post)
    monkeypatch.setattr(http_util, "get_json", get)
    monkeypatch.setattr(backends, "_sleep", lambda s: calls["sleep"].append(s))
    return calls


def _chat_bodies(srv):
    return srv.requests_to("/api/chat")


def _user_text(body) -> str:
    return body["messages"][-1]["content"]


# ---------------------------------------------------------------------------
# 1. Mutlu yol
# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_govde_cevrilir_yer_tutucular_korunur(self, fake_llm_server, monkeypatch, net_spy):
        srv = fake_llm_server("ollama", models=[MODEL], reply=lambda t: _fake_turkish(t))
        _point_ollama_to(monkeypatch, srv.url)
        lt = _make_translator()

        out = lt.translate(_body())

        for token in _LATEX_KEPT:
            assert token in out, token
        assert "<<" not in out and ">>" not in out
        assert "[MT_FAIL]" not in out
        for i in range(1, PARAGRAPH_COUNT + 1):
            assert f"Paragraf {i} anlatır bu yörünge" in out
        assert "Bakınız bu ek." in out          # dipnot içi de çevrildi
        assert "Keep placeholder" not in out     # talimat cümlesi sızmadı
        assert lt.backend_health() is None
        stats = lt._translator.get_stats()
        assert stats["mt_engine"] == ENGINE_ID
        assert stats["mt_model"] == f"Ollama · {MODEL} · {srv.url}"
        assert net_spy["sleep"] == []

    def test_istek_govdeleri_sozlesmeye_uyar(self, fake_llm_server, monkeypatch, net_spy):
        from core.translator import _PLACEHOLDER_CLAUSE

        srv = fake_llm_server("ollama", models=[MODEL], reply=lambda t: _fake_turkish(t))
        _point_ollama_to(monkeypatch, srv.url)
        _make_translator().translate(_body())

        paths = [p for _, p, _ in srv.requests]
        # load(): /api/show + ısınma (/api/generate) BİR KEZ, sonra yalnız sohbet
        assert paths[:2] == ["/api/show", "/api/generate"]
        assert set(paths[2:]) == {"/api/chat"}
        bodies = _chat_bodies(srv)
        assert len(bodies) >= PARAGRAPH_COUNT
        num_ctx = {b["options"]["num_ctx"] for b in bodies}
        assert num_ctx == {MT_CONTEXT_TOKENS}     # koşu boyunca SABİT
        assert srv.requests_to("/api/generate")[0]["options"]["num_ctx"] == MT_CONTEXT_TOKENS
        for body in bodies:
            assert body["model"] == MODEL
            assert body["stream"] is False
            assert body["options"]["temperature"] == MT_GREEDY_TEMPERATURE
            assert body["options"]["seed"] == MT_GREEDY_SEED
            assert body["options"]["num_predict"] > 0
            assert "think" not in body            # model düşünme yeteneği bildirmedi
            assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
            instruction, text = _user_text(body).split("\n\n", 1)
            for raw in _LATEX_HIDDEN:
                assert raw not in text, (raw, text)
            # yer tutucu cümlesi YALNIZ yer tutuculu metinde
            assert (_PLACEHOLDER_CLAUSE.strip() in instruction) == ("<<" in text), text
        assert any("<<" in _user_text(b) for b in bodies)
        for req in srv.request_details:
            assert req.status == 200, (req.path, req.status)   # hiçbir yanıt hata değil
            assert "authorization" not in {k.lower() for k in req.headers}
        assert net_spy["sleep"] == []


# ---------------------------------------------------------------------------
# 2. Yer tutucu yutan model
# ---------------------------------------------------------------------------
class TestPlaceholderLoss:
    def test_yer_tutucu_yutan_modelde_latex_kaybolmaz(self, fake_llm_server, monkeypatch,
                                                      net_spy):
        srv = fake_llm_server("ollama", models=[MODEL],
                              reply=lambda t: _fake_turkish(t, keep_placeholders=False))
        _point_ollama_to(monkeypatch, srv.url)
        out = _make_translator().translate(_body(paragraphs=2))

        for token in _LATEX_KEPT:
            assert token in out, token
        assert "<<" not in out and "[MT_FAIL]" not in out
        # yer tutucusuz paragraflar yine çevrilir
        assert "Paragraf 1 anlatır" in out and "Paragraf 2 anlatır" in out


# ---------------------------------------------------------------------------
# 3. Sunucu ortada durur
# ---------------------------------------------------------------------------
class TestServerStopsMidRun:
    STOP_AT_CALL = 6   # altıncı çeviri yanıtı gönderilirken sunucu kapanır

    def _run(self, fake_llm_server, monkeypatch, net_spy):
        state = {"calls": 0, "posts_at_stop": None, "srv": None}

        def reply(text):
            state["calls"] += 1
            if state["calls"] == self.STOP_AT_CALL:
                # Bu yanıt yine gider (bağlantı kabul edilmişti); sonrakiler reddedilir.
                state["posts_at_stop"] = len(net_spy["post"])
                state["srv"].stop()
            return _fake_turkish(text)

        srv = fake_llm_server("ollama", models=[MODEL], reply=reply)
        state["srv"] = srv
        url = srv.url
        _point_ollama_to(monkeypatch, url)
        lt = _make_translator()
        t0 = time.monotonic()
        out = lt.translate(_body())
        elapsed = time.monotonic() - t0
        return lt, out, elapsed, state, url

    def test_devre_acilir_saglik_dolu_kalanlar_agsiz_gecer(self, fake_llm_server, monkeypatch,
                                                            net_spy):
        lt, out, elapsed, state, url = self._run(fake_llm_server, monkeypatch, net_spy)

        assert state["calls"] == self.STOP_AT_CALL      # durduktan sonra yanıt yok
        assert not state["srv"].running
        # Sağlık: devre açık, Türkçe neden
        assert lt.backend_health() == MSG_SERVER_LOST.format(url=url)
        # Durdurmadan sonraki bağlantı denemeleri: devre eşiğine kadar, her
        # başarısızlık yeniden denemelerle — sonra kalan segmentler AĞA GİTMEZ.
        attempts_per_failure = DEFAULT_RETRY_COUNT + 1
        after_stop = len(net_spy["post"]) - state["posts_at_stop"]
        assert after_stop == attempts_per_failure * MT_HTTP_CIRCUIT_MAX_FAILURES
        backoff = [DEFAULT_RETRY_BASE_DELAY_S * (2 ** k) for k in range(DEFAULT_RETRY_COUNT)]
        assert net_spy["sleep"] == backoff * MT_HTTP_CIRCUIT_MAX_FAILURES
        # Kalan (çevrilmemiş) segment sayısı devre eşiğinden fazla: kısa devre kanıtı
        untranslated = [i for i in range(1, PARAGRAPH_COUNT + 1) if _paragraph(i) in out]
        assert len(untranslated) > MT_HTTP_CIRCUIT_MAX_FAILURES
        assert _paragraph(PARAGRAPH_COUNT) in out        # son paragraf özgün kaldı
        assert elapsed < 10.0                            # bekleme yamalı; ağsız geçiş
        # Gövde bozulmadı: LaTeX yapıları yerinde, yer tutucu ya da hata öneki yok
        for token in _LATEX_KEPT:
            assert token in out, token
        assert "<<" not in out and "[MT_FAIL]" not in out
        # Başarısızlıklar sayıldı: F3 kalite kapısı bu oranı görür
        assert lt.stats.segments_failed >= len(untranslated)

    def test_translate_book_ayni_nedeni_okur_ve_ikinci_kosu_aga_gitmez(
            self, fake_llm_server, monkeypatch, net_spy):
        import translate_book

        lt, out, _, _, url = self._run(fake_llm_server, monkeypatch, net_spy)
        # K2'nin F3 kapısı yardımcısı K1'in sağlık iletisini AYNEN alır (çıkış 5 yolu)
        assert translate_book._translator_backend_problem(lt) == MSG_SERVER_LOST.format(url=url)
        # Aynı çevirmenle yeniden çeviri: devre açık kalır (yarı-açık yok), ağ yok
        posts_before = len(net_spy["post"])
        t0 = time.monotonic()
        again = lt.translate(_body(paragraphs=3))
        assert time.monotonic() - t0 < 5.0
        assert len(net_spy["post"]) == posts_before
        assert _paragraph(3) in again
        assert lt.backend_health() == MSG_SERVER_LOST.format(url=url)


class TestLateDropNotResumable:
    """Vk 2026-09-28: sunucu GEÇ koparsa (başarısız oran eşiğin altında) gövde
    FINAL checkpoint olarak yazılmaz; sonraki --resume çeviriyi yeniden yapar."""

    STOP_AT_CALL = 12   # son alt-batch'te kopar: başarısız oran < %50

    def test_final_checkpoint_yazilmaz_resume_yeniden_cevirir(self, fake_llm_server,
                                                              monkeypatch, net_spy, tmp_path):
        from config.defaults import TRANSLATION_MAX_FAIL_RATIO

        state = {"calls": 0}

        def reply(text):
            state["calls"] += 1
            if state["calls"] == self.STOP_AT_CALL:
                srv.stop()
            return _fake_turkish(text)

        srv = fake_llm_server("ollama", models=[MODEL], reply=reply)
        _point_ollama_to(monkeypatch, srv.url)
        ckpt = tmp_path / "ckpt"
        lt = _make_translator()
        out = lt.translate(_body(), checkpoint_dir=str(ckpt))
        stats = lt.stats
        # Senaryo gerçekten "eşiğin altında": eski kural gövdeyi kabul ederdi
        assert 0 < stats.segments_failed / stats.segments_total <= TRANSLATION_MAX_FAIL_RATIO
        assert stats.segments_translated > 0
        assert lt.backend_health() is not None
        assert _paragraph(PARAGRAPH_COUNT) in out            # İngilizce kaldı
        assert not (ckpt / "translated_body_final.tex").exists()

        # Sunucu yeniden açıldı: --resume eşdeğeri koşu checkpoint'ten DÖNMEZ, çevirir
        srv2 = fake_llm_server("ollama", models=[MODEL], reply=lambda t: _fake_turkish(t))
        _point_ollama_to(monkeypatch, srv2.url)
        again = _make_translator().translate(_body(), checkpoint_dir=str(ckpt), resume=True)
        for i in range(1, PARAGRAPH_COUNT + 1):
            assert f"Paragraf {i} anlatır" in again
        assert (ckpt / "translated_body_final.tex").exists()   # sağlıklı koşu yazar


# ---------------------------------------------------------------------------
# 4. Sunucu baştan kapalı
# ---------------------------------------------------------------------------
class TestServerDownFromStart:
    def test_yukleme_hatasi_onbelleklenir(self, mt_isolation, net_spy):
        # _point_ollama_to YOK: yalıtımın kapalı yerel portu (bağlantı anında reddedilir)
        lt = _make_translator()
        expected = MSG_OLLAMA_DOWN.format(url=mt_isolation.closed_url)
        t0 = time.monotonic()
        try:
            out = lt.translate(_body())
        except BackendUnavailable as exc:   # translate_book bunu yakalayıp 5 döner
            assert str(exc) == expected
            out = None
        assert time.monotonic() - t0 < 10.0
        if out is not None:
            assert "[MT_FAIL]" not in out and "<<" not in out
            for token in _LATEX_KEPT:
                assert token in out, token
        assert lt.backend_health() == expected
        # Model yüklemesi (/api/show) koşu başına BİR KEZ denenir; segment başına değil
        shows = [u for u in net_spy["post"] if u.endswith("/api/show")]
        assert len(shows) == 1
        assert not [u for u in net_spy["post"] if u.endswith("/api/chat")]

    def test_segment_basina_yigin_izi_basilmaz(self, mt_isolation, net_spy, caplog):
        # Vk 2026-09-28: önbellekteki yükleme hatası her segmentte yeniden
        # fırlıyor; pipeline.log'a segment başına tam traceback basılıyordu.
        import logging

        caplog.set_level(logging.ERROR, logger="latex_pipeline.translator")
        lt = _make_translator()
        try:
            lt.translate(_body(paragraphs=4))
        except BackendUnavailable:
            pass
        failed = [r for r in caplog.records
                  if r.getMessage().startswith("Translation failed for segment")]
        assert failed, "segment hatası günlüğe yazılmalı"
        expected = MSG_OLLAMA_DOWN.format(url=mt_isolation.closed_url)
        for record in failed:
            assert not record.exc_info                    # yığın izi yok
            assert expected in record.getMessage()        # Türkçe neden var


# ---------------------------------------------------------------------------
# 5. Kulvarlar arası sözleşme turu
# ---------------------------------------------------------------------------
class TestCrossLaneContract:
    def test_kesif_kimlikleri_registry_arka_uc_kontrol_ve_cli_ile_uyumlu(
            self, fake_llm_server, fake_llama, mt_isolation, monkeypatch):
        import translate_book
        from core.memory import memory_project_key
        from tests.fakes import write_fake_gguf, write_fake_mlx_dir
        from ui import branding, runner

        ollama = fake_llm_server("ollama", models=[
            "qwen3:8b", {"name": "gomme-modeli", "kind": "embedding"}])
        lmstudio = fake_llm_server("lmstudio", models=[
            "qwen/qwen3-8b", {"name": "pub/gomme", "kind": "embedding"}])
        generic = fake_llm_server("openai")
        sources = SourcesConfig(ollama_url=ollama.url, lmstudio_url=lmstudio.base_url,
                                openai_servers=(OpenAIServer(name="llama.cpp",
                                                             url=generic.base_url),))
        monkeypatch.setattr(discovery, "load_sources", lambda path=None: sources)
        models_dir = mt_isolation.models_dir
        write_fake_gguf(models_dir / "Sentetik-8B-Q4_K_M.gguf", "qwen3", size_label="8B")
        write_fake_mlx_dir(models_dir / "sentetik-mlx", "qwen3")

        report = discovery.discover_all()
        by_source = {}
        for m in report.models:
            by_source.setdefault(m.source, []).append(m)
        # her kaynaktan gerçek model geldi (sahte veri yok: yalnız sunucuların bildirdikleri)
        assert {"builtin", "gguf", "mlx", "ollama", "lmstudio", "openai"} <= set(by_source)
        assert {m.engine_id for m in by_source["ollama"]} == {"ollama:qwen3:8b",
                                                               "ollama:gomme-modeli"}

        for m in report.models:
            spec = registry.resolve_spec(m.engine_id)
            assert spec.backend == m.backend, m.engine_id
            backend = backends.create_backend(spec)
            if m.backend == "mlx":
                assert backend is None, m.engine_id          # bugünkü MLX yolu
            else:
                assert backend is not None and backend.kind == m.backend, m.engine_id
            check = discovery.check_engine(m.engine_id)
            assert check.ok == m.usable, (m.engine_id, check.reason, m.reason)
            if not m.usable:
                assert check.reason and m.reason
            # arayüz (K3): hazır olma hükmü keşifle aynı; etiket kaynak adını taşır
            if m.engine_id != registry.DEFAULT_ENGINE:
                ready, why = runner.engine_ready(m.engine_id, report)
                assert ready == m.usable and (ready or why), m.engine_id
            assert branding.SOURCE_LABELS[m.source] in runner.model_option_label(m)
            # arayüz komutu CLI ayrıştırıcısından AYNEN geçer
            command = runner.translate_command(Path("kitap.pdf"), engine=m.engine_id)
            monkeypatch.setattr(sys, "argv", ["translate_book.py", *command[2:]])
            assert translate_book.parse_args().mt_engine == m.engine_id
            # bellek havuzu (KARARLAR 8): yalnız varsayılan motor eski anahtarı kullanır
            key = memory_project_key("kitap", m.engine_id)
            assert (key == "kitap") == (m.engine_id == registry.DEFAULT_ENGINE)

        # kullanılabilir sunucu modelleri yüklenir ve üretir (model yükleme = sahte)
        from core.mt_engines.contracts import GenerationRequest

        for m in report.models:
            if m.usable and m.backend in ("ollama", "openai", "llamacpp"):
                engine = registry.create_engine(m.engine_id)
                backend = backends.create_backend(engine.spec)
                backend.load()
                result = backend.generate(engine, GenerationRequest(
                    "Translate from English to Turkish.", "Hello world.", 64))
                assert result.error is None and result.text.strip(), m.engine_id
                assert backend.health() is None
        # gömme modelleri: keşif de ön kontrol de reddeder (yukarıdaki döngü
        # check.ok == m.usable'ı zaten sınadı; burada modellerin gerçekten listede
        # olduğu doğrulanır)
        for embed_id in ("ollama:gomme-modeli", "lmstudio:pub/gomme"):
            embed = report.model_by_id(embed_id)
            assert embed is not None and not embed.usable, embed_id
