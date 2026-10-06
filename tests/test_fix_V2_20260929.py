"""V2 denetimi düzeltmeleri (29 Eylül 2026): "Yerel model ile çevir" zinciri.

Denetimde (V2, iç denetim) kulvar düzeltmelerinin
üstünde bulunan kusurların kalıcı regresyon testleri:

* V2-01 — F3-03 kabuğu: vurgulu kısa sözcüğün kabuğu ('\\textit{' ve '}')
  iki ayrı yer tutucuya bölünüyor. Model ikisinin yerini değiştirince
  ('<<b>>sözcük<<a>>') geri yazım '}sözcük\\textit{' üretiyordu: süslü
  parantez sayısı dengeli göründüğü için hiçbir denetim yakalamıyor, TeX
  "Too many }'s" veriyor, italik paragrafın sonuna taşıyordu.
* V2-02 — '\\url{...}' korunmuyordu: model adresi çevirince
  ('\\url{https://…}' -> '\\dönüş{dönüş://…}') adres kayboluyor, tanımsız
  komut derlemeyi düşürüyordu (bugav c04).

Girdiler sentetiktir; model yüklenmez, gerçek sunucuya bağlanılmaz (sahte
Ollama: tests/fakes, rastgele port).
"""
from __future__ import annotations

import re
import zlib

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.translator import LatexTranslator

# ---------------------------------------------------------------------------
# Sahte çevirmen (yalnız test altyapısı): sözcük başına kararlı Türkçe sözcük,
# yer tutucular ve sayılar AYNEN.
# ---------------------------------------------------------------------------
_TR = ("çalışma", "değer", "ışık", "yörünge", "şekil", "güneş", "ağırlık",
       "kütle", "hız", "yüzey", "dönüş", "öğrenci", "kuvvet", "enerji",
       "sıcaklık", "basınç", "akış", "düzlem", "eğri", "açı")
_PH = re.compile(r"(<<[A-Z]+_\d+>>|\[\[[A-Za-z]+_\d+\]\]|@@[A-Za-z]+\d+@@)")
_LETTERS = re.compile(r"[^\W\d_]+")
# İki yer tutucu arasında kısa bir sözcük: '<<X_a>>sözcük<<X_b>>'
_PAIR = re.compile(r"(<<[A-Z]+_\d+>>)([^<>]{1,20}?)(<<[A-Z]+_\d+>>)")


def fake_turkish(text: str) -> str:
    def word(m: re.Match) -> str:
        src = m.group(0)
        tr = _TR[zlib.crc32(src.lower().encode("utf-8")) % len(_TR)]
        return tr[:1].upper() + tr[1:] if src[:1].isupper() else tr
    return "".join(p if _PH.fullmatch(p) else _LETTERS.sub(word, p)
                   for p in _PH.split(text))


def swap_pairs(text: str) -> str:
    """Kabuk çiftinin yerini değiştiren sahte model: '<<b>>sözcük<<a>>'."""
    return fake_turkish(_PAIR.sub(lambda m: m.group(3) + m.group(2) + m.group(1), text))


def _braces_in_order(text: str) -> bool:
    """Kaçışsız süslü parantezler SIRAYLA dengeli mi (derinlik hiç eksiye inmez)?"""
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


def _skeleton() -> LatexTranslator:
    """MT'siz iskelet (yalnız stash / restore yardımcıları için)."""
    return LatexTranslator.__new__(LatexTranslator)


@pytest.fixture
def ollama_translator(fake_llm_server, monkeypatch, tmp_path):
    """Sahte Ollama'ya bağlı LatexTranslator fabrikası: ``make(source_lang, reply)``."""
    from core.mt_engines import discovery
    from core.mt_engines.contracts import SourcesConfig
    from core.translator import BookTranslator

    real_init_memory = BookTranslator._init_memory
    memory_db = tmp_path / "bellek.db"

    def _tmp_memory(self):
        self.config.memory_db_path = str(memory_db)
        real_init_memory(self)

    monkeypatch.setattr(BookTranslator, "_init_memory", _tmp_memory)

    def make(source_lang: str = "en", reply=fake_turkish):
        srv = fake_llm_server("ollama", models=["sahte"], reply=reply)
        sources = SourcesConfig(ollama_url=srv.url,
                                lmstudio_url="http://127.0.0.1:9/v1",
                                openai_servers=())
        monkeypatch.setattr(discovery, "load_sources", lambda path=None: sources)
        lt = LatexTranslator(pipeline_config=PipelineConfig(translate=True),
                             mt_engine="ollama:sahte", source_lang=source_lang,
                             target_lang="tr", book_type="science",
                             project_name="V2 Deneme")
        return lt, srv

    return make


def _chat_texts(srv) -> list:
    return [b["messages"][-1]["content"] for b in srv.requests_to("/api/chat")]


# ===========================================================================
# V2-01 — ters sıraya konmuş kısa italik kabuğu düzeltilir
# ===========================================================================
EMPHASIS_BODY = r"""\chapter{Emphasis}

The committee did \textit{not} allow the second launch because the weather was bad during the whole week.

The engineers \textit{always} check the valves twice before the engine is fired on the test stand.
"""


class TestV201KabukSirasi:
    def test_restore_ters_kabugu_duzeltir(self):
        stash = ["\\textit{", "}"]
        out = _skeleton()._restore_stash("izin <<LATEX_1>>vermedi<<LATEX_0>> ki", stash)
        assert out == "izin \\textit{vermedi} ki"

    def test_dogru_sira_ve_kabuk_disi_yer_tutucu_degismez(self):
        from latex_pipeline.translator import (_LATEX_PLACEHOLDER_ID_RE,
                                               repair_italic_shell_order)
        stash = ["\\textit{", "}", "$x$", "\\textit{", "}"]
        text = "a <<LATEX_0>>b<<LATEX_1>> <<LATEX_2>> c <<LATEX_4>>d<<LATEX_3>>"
        out = repair_italic_shell_order(text, stash, _LATEX_PLACEHOLDER_ID_RE)
        assert out == "a <<LATEX_0>>b<<LATEX_1>> <<LATEX_2>> c <<LATEX_3>>d<<LATEX_4>>"
        # Kabuk olmayan ardışık öğeler ('$x$' + '}' değil) ve kopyalı kabuk
        # (hangisinin hangisi olduğu belirsiz) olduğu gibi kalır.
        dup = "<<LATEX_1>>b<<LATEX_0>> <<LATEX_1>>"
        assert repair_italic_shell_order(dup, stash, _LATEX_PLACEHOLDER_ID_RE) == dup

    def test_uctan_uca_govde_sirali_dengeli(self, ollama_translator):
        lt, srv = ollama_translator("en", reply=swap_pairs)
        out = lt.translate(EMPHASIS_BODY)
        assert _braces_in_order(out), out
        assert not re.search(r"\}[^\s{}\\]+\s*\\textit\{", out), out
        assert out.count("\\textit{") == 2
        assert re.search(r"\\textit\{[^\s{}]+\}", out)
        assert "<<" not in out
        # Vurgulu sözcük modele cümlesiyle gitti (F3-03 davranışı korunur).
        assert any("not" in t for t in _chat_texts(srv))

    def test_retranslator_satir_ve_baslik(self):
        from latex_pipeline.retranslator import (_retranslate_heading_line,
                                                 _retranslate_line)
        line = ("The prophet did \\textit{not} allow the people to leave the "
                "city before the long winter.")
        out = _retranslate_line(line, swap_pairs)
        assert _braces_in_order(out), out
        assert re.search(r"\\textit\{[^\s{}]+\}", out), out
        head = _retranslate_heading_line("\\section{Why \\textit{not} Leave Now}",
                                         swap_pairs)
        assert _braces_in_order(head), head
        assert re.search(r"\\textit\{[^\s{}]+\}", head), head


# ===========================================================================
# V2-02 — \url{...} MT'ye gitmez, adres aynen kalır
# ===========================================================================
class TestV202Url:
    def test_stash_url(self):
        masked, stash = _skeleton()._stash_protected(
            "The data are listed at \\url{https://example.org/orbits_2} for all planets.")
        assert "\\url{https://example.org/orbits_2}" in stash
        assert "example.org" not in masked

    def test_uctan_uca_url_korunur(self, ollama_translator):
        lt, srv = ollama_translator("en")
        body = ("The mean motion is small; see \\url{https://example.org/orbits} "
                "for the data of every planet in the solar system.\n")
        out = lt.translate(body)
        assert "\\url{https://example.org/orbits}" in out, out
        assert not any("example.org" in t for t in _chat_texts(srv))

    def test_retranslator_url_korunur(self):
        from latex_pipeline.retranslator import _retranslate_line
        line = ("See \\url{https://example.org/orbits} for the data of the planets "
                "and the moons.")
        out = _retranslate_line(line, fake_turkish)
        assert "\\url{https://example.org/orbits}" in out, out


# ===========================================================================
# V2-03 — F3-07 görünürlüğü: üretim sınırına dayanan parça sessiz geçmez
# ===========================================================================
_TRUNC_ITEM = {"source": "The total energy of the planet depends only on the size.",
               "output": "Eğri dönüş yüzey", "used": 8, "limit": 512}


class _FakeTruncatingTranslator:
    """Yalnız test altyapısı: core BookTranslator'ın kesilme raporu arayüzü."""

    def __init__(self, before=(), after=()):
        self._items = list(before)
        self._after = list(after)
        self.stats = {"segments_truncated": 0}

    def get_stats(self):
        return dict(self.stats)

    def truncation_report(self):
        return [dict(item) for item in self._items]

    def run(self):
        self._items.extend(self._after)
        self.stats["segments_truncated"] += len(self._after)


class TestV203KesikGorunur:
    def test_istatistige_yalniz_bu_kosunun_parcalari(self, caplog):
        from latex_pipeline.translator import TranslationStats
        old = dict(_TRUNC_ITEM, source="Önceki koşunun parçası.")
        lt = _skeleton()
        lt.stats = TranslationStats()
        lt._translator = _FakeTruncatingTranslator(before=[old], after=[_TRUNC_ITEM])
        lt._snapshot_model_stats()
        lt._translator.run()
        with caplog.at_level("WARNING"):
            lt._collect_model_stats()
        assert lt.stats.truncated_segments == [_TRUNC_ITEM]
        assert lt.stats.segments_truncated == 1
        assert any("paragrafın çevirisi eksik olabilir" in r.getMessage()
                   for r in caplog.records)

    def test_rapor_yontemi_olmayan_cevirmen(self):
        from latex_pipeline.translator import TranslationStats
        lt = _skeleton()
        lt.stats = TranslationStats()
        lt._translator = type("Eski", (), {"stats": {}, "get_stats": lambda s: {}})()
        lt._snapshot_model_stats()
        lt._collect_model_stats()
        assert lt.stats.truncated_segments == []

    def test_translate_book_listeyi_yazar_ve_uyarir(self, tmp_path, caplog):
        import json
        import logging
        import translate_book as tb
        from latex_pipeline.translator import TranslationStats
        stats = TranslationStats()
        stats.truncated_segments = [_TRUNC_ITEM]
        log = logging.getLogger("v2.test.kesik")
        with caplog.at_level("WARNING", logger="v2.test.kesik"):
            tb._write_truncation_report(tmp_path, stats, log)
        path = tmp_path / tb.TRUNCATION_REPORT_NAME
        assert json.loads(path.read_text(encoding="utf-8")) == [_TRUNC_ITEM]
        assert any("1 parçanın çevirisi üretim sınırına dayandı" in r.getMessage()
                   for r in caplog.records)
        # Kesilme olmayan koşu listeyi boşaltır (eski koşunun listesi kalmaz).
        tb._write_truncation_report(tmp_path, TranslationStats(), log)
        assert json.loads(path.read_text(encoding="utf-8")) == []

    def test_uctan_uca_sahte_sunucu_kesik_liste(self, ollama_translator):
        # done_reason=length + çevirinin üçte biri: çıktı üretim sınırında kesildi.
        from tests.fakes.fake_llm_server import FakeReply

        def reply(text):
            words = fake_turkish(text).split()
            if "total energy" in text:
                return FakeReply(content=" ".join(words[: len(words) // 3]),
                                 finish_reason="length")
            return " ".join(words)

        lt, _srv = ollama_translator("en", reply=reply)
        body = ("The total energy of the planet depends only on the size of the "
                "orbit, which is a surprising result for most students.\n\n"
                "A round orbit can be seen as an ellipse with both foci merged, "
                "so the radius from the centre never changes.\n")
        lt.translate(body)
        items = lt.stats.truncated_segments
        assert len(items) == 1, items
        assert items[0]["source"].startswith("The total energy"), items
        assert items[0]["output"]

    def test_arayuz_sayisi_yalniz_bu_kosudan(self, tmp_path):
        import json
        import os
        import time
        from ui import runner
        out = tmp_path / "kitap"
        out.mkdir()
        path = out / runner.TRUNCATION_REPORT_NAME
        path.write_text(json.dumps([_TRUNC_ITEM, _TRUNC_ITEM]), encoding="utf-8")
        now = time.time()
        assert runner.truncated_count(out, now - 60) == 2
        os.utime(path, (now - 3600, now - 3600))
        assert runner.truncated_count(out, now - 60) == 0      # önceki koşunun
        path.write_text("bozuk", encoding="utf-8")
        assert runner.truncated_count(out, now - 60) == 0      # sayı uydurulmaz


# Arayüz: yerel model sonucunda kesik parça uyarısı (U kulvarının AppTest
# altyapısı; yalnız test altyapısı içe aktarılır).
from tests.test_fix_U_20260929 import (  # noqa: E402,F401
    EN_TEXT, _epub, _finished, _fresh, _pdf, _run_app, _slot, _stale, _texts, env,
)


class TestV203ArayuzUyarisi:
    def _model_run(self, env, code: int):
        from ui import runner
        book = _epub(env.tmp, EN_TEXT * 40, "Model Kitap.epub")
        slug = runner.book_slug(book)
        out = env.root / "output" / slug
        _pdf(out / "compiled", f"{slug}_TR.pdf")
        env.store["_job"] = _slot(book, _finished("translate", code))
        return out

    def test_bu_kosunun_kesik_listesi_uyari_verir(self, env):
        import json
        from ui import runner
        out = self._model_run(env, 0)
        _fresh(out / runner.TRUNCATION_REPORT_NAME, json.dumps([_TRUNC_ITEM]))
        texts = _texts(_run_app())
        assert texts["success"][0].startswith("PDF hazır")
        assert any(w.startswith("1 paragrafın çevirisi modelin üretim sınırına dayandı")
                   for w in texts["warning"]), texts["warning"]
        assert str(out / runner.TRUNCATION_REPORT_NAME) in texts["caption"]

    def test_bos_ya_da_eski_liste_uyari_vermez(self, env):
        import json
        from ui import runner
        out = self._model_run(env, 0)
        path = _fresh(out / runner.TRUNCATION_REPORT_NAME, json.dumps([]))
        assert not any("üretim sınırına" in w for w in _texts(_run_app())["warning"])
        path.write_text(json.dumps([_TRUNC_ITEM]), encoding="utf-8")
        _stale(path)
        assert not any("üretim sınırına" in w for w in _texts(_run_app())["warning"])


# ===========================================================================
# V2-04 — BOM'lu mt_selection.json: arayüzün model seçimi sessizce düşmez
# ===========================================================================
class TestV204SecimBom:
    def test_bomlu_secim_dosyasi_okunur(self, tmp_path, monkeypatch):
        from ui import runner
        path = tmp_path / "mt_selection.json"
        path.write_bytes(b"\xef\xbb\xbf" + '{"engine": "ollama:qwen3:8b"}'.encode("utf-8"))
        monkeypatch.setattr(runner, "engine_choice_path", lambda: path)
        assert runner.load_engine_choice() == "ollama:qwen3:8b"
