"""G2c bağlama testleri — kitap sözlüğünün boru hattına bağlanması.

Bu dosya G2c öneri yamalarıyla (oneri_core_translator.patch,
oneri_latex_translator.patch, oneri_translate_book.patch) BİRLİKTE gelir;
yamalar uygulanmadan koşmaz.

Kapsam (sahte Ollama, tests/fakes; model YÜKLENMEZ; metinler SENTETİK):
  * taze koşu: aday çıkarımı -> genel çağrıyla bağlamlı terim çevirisi ->
    kitap klasörüne glossary_shared.tsv (TeX yolu biçimi)
  * Phase 5.75: özgün dilde kalmış terim karşılığına çevrilir (Almanca)
  * QC: term_consistency UYARISI (sözlük dışı karşılık) — metin değişmez
  * istem desteği (3a) varsayılan KAPALI; ortam değişkeniyle açılır: segment
    isteminde terim bloğu, çıktıda blok yok
  * sürdürülen koşu: sözlük dosyadan yüklenir, terim çevirisi yinelenmez
  * BookTranslator._kitap_terimli_talimat / _strip_prompt_leaks birimleri
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from latex_pipeline import kitap_sozlugu as ks
from latex_pipeline.kitap_sozlugu import KitapSozlugu, SozlukGirdisi
from tests.test_kaynak_dil_20260928 import (  # noqa: F401 (fikstürler)
    OLLAMA_MODEL,
    _point_sources,
    make_book,
    pipeline_env,
    restore_root_logger,
    run_book,
)

P1 = "Der Wärmestrom durch die Wand hängt von der Temperaturdifferenz ab."
P2 = "Ein großer Wärmestrom entsteht bei einer großen Temperaturdifferenz."
P3 = "Die Temperaturdifferenz treibt den Wärmestrom an."
P4 = "Ohne Temperaturdifferenz gibt es keinen Wärmestrom."
GOVDE = "\\chapter{Wärmeleitung}\n\n" + "\n\n".join([P1, P2, P3, P4]) + "\n"

CEVIRI = {
    # terimler (bağlamlı isteğin son paragrafı)
    "Wärmestrom": "ısı akısı",
    "Temperaturdifferenz": "sıcaklık farkı",
    # segmentler: P1'de model terimi ÇEVİRMEMİŞ, P2'de sözlük dışı karşılık
    P1: "Duvardan geçen Wärmestrom sıcaklık farkına bağlıdır.",
    P2: "Büyük bir sıcaklık farkında büyük bir ısı akımı oluşur.",
    P3: "Sıcaklık farkı ısı akısını sürer.",
    P4: "Sıcaklık farkı olmadan ısı akısı yoktur.",
    "Wärmeleitung": "Isı iletimi",
}


def _yanit(text: str) -> str:
    return "\n\n".join(CEVIRI.get(p.strip(), "Kısa bir açıklama cümlesi.")
                       for p in text.split("\n\n"))


def _kos(fake_llm_server, monkeypatch, pipeline_env, ad="kitap", **kw):
    srv = fake_llm_server("ollama", models=[OLLAMA_MODEL], reply=_yanit)
    _point_sources(monkeypatch, ollama_url=srv.url)
    out = make_book(pipeline_env, GOVDE, ad)
    kod = run_book(out, f"ollama:{OLLAMA_MODEL}", source_lang="de",
                   skip_qc=False, **kw)
    return srv, out, kod


def _kullanici_icerikleri(srv):
    return [b["messages"][-1]["content"] for b in srv.requests_to("/api/chat")]


class TestQcAtlaninca:
    def test_skip_qc_sozluk_icin_model_cagrisi_yok(
            self, fake_llm_server, monkeypatch, pipeline_env):
        # Sözlük yalnız QC döngüsüne yarar: QC atlanırsa (istem desteği de
        # kapalıyken) terim çevirisi istenmez, dosya yazılmaz.
        monkeypatch.delenv(ks.ISTEM_ORTAM_DEGISKENI, raising=False)
        srv = fake_llm_server("ollama", models=[OLLAMA_MODEL], reply=_yanit)
        _point_sources(monkeypatch, ollama_url=srv.url)
        out = make_book(pipeline_env, GOVDE, "kitap_qcsiz")
        assert run_book(out, f"ollama:{OLLAMA_MODEL}", source_lang="de") == 0
        assert not (out / ks.SOZLUK_DOSYA_ADI).exists()
        assert not any(t.endswith("\n\nWärmestrom")
                       for t in _kullanici_icerikleri(srv))


class TestTazeKosu:
    def test_sozluk_cikarilir_cevrilir_yazilir(self, fake_llm_server, monkeypatch,
                                             pipeline_env):
        monkeypatch.delenv(ks.ISTEM_ORTAM_DEGISKENI, raising=False)
        srv, out, kod = _kos(fake_llm_server, monkeypatch, pipeline_env)
        assert kod in (0, 3), kod
        tsv = (out / ks.SOZLUK_DOSYA_ADI).read_text(encoding="utf-8")
        assert "Wärmestrom\tısı akısı\n" in tsv
        assert "Temperaturdifferenz\tsıcaklık farkı\n" in tsv
        log = (out / "pipeline.log").read_text(encoding="utf-8")
        assert "Kitap sözlüğü: 2 terim" in log
        # Terim isteği bağlamlı (ilk cümle + boş satır + terim) ve normal
        # talimatla gitti; istem desteği kapalıyken segment isteminde blok yok.
        icerik = _kullanici_icerikleri(srv)
        assert any(t.endswith(P1 + "\n\nWärmestrom") for t in icerik)
        assert all(t.startswith("Translate from German to Turkish.") or
                   t.startswith("Translate this heading from German")
                   for t in icerik)

    def test_phase575_ve_qc_uyarisi(self, fake_llm_server, monkeypatch,
                                    pipeline_env):
        monkeypatch.delenv(ks.ISTEM_ORTAM_DEGISKENI, raising=False)
        _srv, out, _kod = _kos(fake_llm_server, monkeypatch, pipeline_env)
        # Phase 5.75 bellekteki gövdeyi düzeltir; son kitap book_tr.tex.
        govde = (out / "source" / "book_tr.tex").read_text(encoding="utf-8")
        # Özgün dilde kalan terim karşılığına çevrildi (Phase 5.75)...
        assert "Duvardan geçen ısı akısı sıcaklık farkına bağlıdır." in govde
        assert "Wärmestrom" not in govde
        # ...sözlük dışı karşılık DEĞİŞMEDİ (QC yalnız uyarır).
        assert "ısı akımı" in govde
        log = (out / "pipeline.log").read_text(encoding="utf-8")
        assert "Phase 5.75: sözlük geçişi atlandı" in log      # curated (EN->TR) yok
        assert "Phase 5.75: kitap sözlüğü geçişi tamam (2 terim" in log
        rapor = (out / "qc_report.txt").read_text(encoding="utf-8")
        assert "term_consistency" in rapor and "'Wärmestrom'" in rapor

    def test_istem_destegi_acilinca_blok_gider_cikti_temiz(
            self, fake_llm_server, monkeypatch, pipeline_env):
        monkeypatch.setenv(ks.ISTEM_ORTAM_DEGISKENI, "1")
        srv, out, _kod = _kos(fake_llm_server, monkeypatch, pipeline_env)
        icerik = _kullanici_icerikleri(srv)
        blokta = [t for t in icerik if t.startswith(ks.GENEL_TERIM_BASLIGI)]
        assert blokta, icerik[:3]
        assert any("Wärmestrom = ısı akısı" in t for t in blokta)
        # Blok talimatın önünde; talimat + metin ayrımı (ilk boş satır) korunur.
        t = next(t for t in blokta if t.endswith(P3))
        assert t.split("\n\n", 1)[1] == P3
        govde = (out / "source" / "book_tr.tex").read_text(encoding="utf-8")
        assert ks.GENEL_TERIM_BASLIGI not in govde
        assert "Wärmestrom = " not in govde


class TestSurdurme:
    def test_ikinci_kosu_dosyadan_yukler(self, fake_llm_server, monkeypatch,
                                         pipeline_env):
        monkeypatch.delenv(ks.ISTEM_ORTAM_DEGISKENI, raising=False)
        srv, out, _ = _kos(fake_llm_server, monkeypatch, pipeline_env)
        (out / "pipeline.log").unlink()
        srv.reset_requests()
        kod = run_book(out, f"ollama:{OLLAMA_MODEL}", source_lang="de",
                       skip_qc=False, force_mt=True)
        assert kod in (0, 3)
        log = (out / "pipeline.log").read_text(encoding="utf-8")
        # Çeviriden önce (istem desteği kapalı) yalnız dosya okunur.
        assert "Kitap sözlüğü yüklendi: 2 terim" in log
        icerik = _kullanici_icerikleri(srv)
        assert not any(t.endswith("\n\nWärmestrom") for t in icerik)


class TestCekirdekBirim:
    S = KitapSozlugu([SozlukGirdisi("Wärmestrom", "ısı akısı")], "de",
                     istem_acik=True)

    def test_mock_self_talimati_degistirmez(self):
        from core.translator import BookTranslator
        assert BookTranslator._kitap_terimli_talimat(
            MagicMock(), "Talimat.", "Der Wärmestrom.") == "Talimat."

    def test_hymt_ve_sohbet_bicimi(self):
        from core.translator import BookTranslator
        hymt = SimpleNamespace(kitap_sozlugu=self.S, engine=SimpleNamespace(
            spec=SimpleNamespace(format="hymt")))
        out = BookTranslator._kitap_terimli_talimat(hymt, "Talimat.",
                                                    "Der Wärmestrom.")
        assert out == "参考下面的翻译：\nWärmestrom 翻译成 ısı akısı\n\nTalimat."
        sohbet = SimpleNamespace(kitap_sozlugu=self.S, engine=SimpleNamespace(
            spec=SimpleNamespace(format="chat")))
        out = BookTranslator._kitap_terimli_talimat(sohbet, "Talimat.",
                                                    "Der Wärmestrom.")
        assert out == "Use these term translations:\nWärmestrom = ısı akısı\nTalimat."
        yok = SimpleNamespace(kitap_sozlugu=None)
        assert BookTranslator._kitap_terimli_talimat(yok, "T.", "x") == "T."

    def test_sizinti_temizligi_cekirdekte(self):
        from core.translator import BookTranslator
        bt = BookTranslator.__new__(BookTranslator)
        out = BookTranslator._strip_prompt_leaks(
            bt, "参考下面的翻译：\nWärmestrom 翻译成 ısı akısı\n\nIsı akısı büyüktür.",
            original="Der Wärmestrom ist groß.")
        assert out == "Isı akısı büyüktür."

    def test_latex_translator_baglar(self):
        from latex_pipeline.translator import LatexTranslator
        lt = LatexTranslator.__new__(LatexTranslator)
        lt._translator = SimpleNamespace(kitap_sozlugu=None)
        lt.kitap_sozlugu_ata(self.S)
        assert lt.kitap_sozlugu is self.S
        assert lt._translator.kitap_sozlugu is self.S
