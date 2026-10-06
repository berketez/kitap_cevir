"""Bug avi 2026-07-29 — core/translator.py + core/memory.py regresyon testleri.

Bulgu -> test eslesmesi:
- F19: near-echo zehri (nokta/harf buyuklugu farkli Ingilizce echo) hem
  memory.add hem get hem de translator'in cagiran-taraf guard'inda kesilir.
- F20: MT generate kilidi ard arda N kez timeout ederse devre kesici acilir;
  sonraki cagrilar kilidi HIC beklemez (segment basina 300 sn -> O(1)).
- F21: glossary-final desenleri artik segment dongusunde derlenmiyor
  (kalici cache) + ucuz on-filtre; eslesme semantigi degismedi.
- F29: glossary-final terimleri UZUNDAN KISAYA uygular ('bias vector'
  'bias'tan once) — cok kelimeli terim yarim cevrilmez.
- F41: dokuman seviyesi fazlar (tutarlilik + glossary-final) EPUB/TXT
  akisinda batch basina degil, TUM dokuman bitince bir kez kosar.
- F49: find_similar filtre sirasi (ucuz rakam reddi -> fuzz -> zehir
  guard'lari) + satir-turev cache; sonuc kumesi degismez.
- F50: checkpoint kaydi buyuyen dosyayi her seferinde json.load ETMEZ ve
  kompakt yazar; eski (indent'li) checkpoint dosyalari okunmaya devam eder.
"""

import json
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import translator as tr  # noqa: E402
from core.memory import (  # noqa: E402
    TranslationMemory,
    _is_poison_echo,
    _normalize_echo,
    is_echo_output,
)
from core.translator import (  # noqa: E402
    BookTranslator,
    TranslationConfig,
    _glossary_scan_key,
    _glossary_term_pattern,
)


def make_bt(**cfg):
    """__init__ atlayarak gercek metodlu BookTranslator (MT yuklenmez)."""
    bt = BookTranslator.__new__(BookTranslator)
    bt.config = TranslationConfig(**cfg)
    bt.memory = None
    bt.glossary = None
    bt.ner_protector = None
    bt.naturalizer = None
    bt.ai_client = None
    bt.qa_checker = None
    bt.consistency_analyzer = None
    bt._dict_loader = None
    bt.mt_model = object()
    bt.mt_tokenizer = object()
    bt.splitter = MagicMock()
    bt._use_mlx = False
    bt.stats = defaultdict(int)
    return bt


def fake_glossary(terms):
    g = MagicMock()
    g.terms = terms
    return g


# ============================================================================
# F19 — near-echo zehri
# ============================================================================

class TestF19NearEcho:
    def test_punctuation_drop_is_poison(self):
        # MT noktayi dusurmus: eskiden tam-esitlik sarti yuzunden kaciyordu
        assert _is_poison_echo("The force acts on the body.",
                               "The force acts on the body")

    def test_case_and_punctuation_variant_is_poison(self):
        assert _is_poison_echo("The Anchor Escapement.",
                               "the anchor escapement")

    def test_near_identical_english_target_is_poison(self):
        # Normalize esitlik yok ama hedef hala Ingilizce ve >%95 benzer
        assert _is_poison_echo("The value is 5 percent.",
                               "The value is 6 percent.")

    def test_real_turkish_translation_not_poison(self):
        assert not _is_poison_echo("The force acts on the body.",
                                   "Kuvvet cisme etki eder.")

    def test_number_only_variant_not_poison(self):
        # Sayi guard'i korunuyor: rakam echo'su mesru
        assert not _is_poison_echo("1990", "1990.")

    def test_single_proper_name_not_poison(self):
        assert not _is_poison_echo("Newton", "newton")

    def test_formula_echo_not_poison(self):
        assert not _is_poison_echo("E = mc^2", "E = mc^2 ")

    def test_url_never_poison(self):
        assert not _is_poison_echo("https://example.com/a-b",
                                   "https://example.com/a-b")

    def test_exact_equality_still_poison(self):
        # 2026-07-11 sozlesmesi bozulmadi
        assert _is_poison_echo("Kernel Regression", "Kernel Regression")

    def test_empty_target_not_poison(self):
        assert not _is_poison_echo("The force acts.", "")

    def test_normalize_echo_helper(self):
        assert _normalize_echo("The force acts.") == _normalize_echo("the FORCE acts")

    def test_add_rejects_near_echo(self, tmp_path):
        tm = TranslationMemory(db_path=str(tmp_path / "tm.db"),
                               project_name="p", auto_clean=False)
        try:
            assert tm.add("The force acts on the body.",
                          "The force acts on the body") is False
            assert tm.get("The force acts on the body.") is None
        finally:
            tm.close()

    def test_get_filters_legacy_near_echo_row(self, tmp_path):
        """Eski donemden kalan near-echo satiri okumada da kesilir."""
        db = str(tmp_path / "tm.db")
        tm = TranslationMemory(db_path=db, project_name="p", auto_clean=False)
        try:
            src = "The force acts on the body."
            tm.conn.execute(
                "INSERT INTO translations (hash, source_text, target_text, project) "
                "VALUES (?, ?, ?, ?)",
                (tm._hash_text(src), src, "The force acts on the body", "p"))
            tm.conn.commit()
            assert tm.get(src) is None
        finally:
            tm.close()

    def test_auto_clean_removes_near_echo(self, tmp_path):
        db = str(tmp_path / "tm.db")
        tm = TranslationMemory(db_path=db, project_name="p", auto_clean=False)
        src = "The force acts on the body."
        tm.conn.execute(
            "INSERT INTO translations (hash, source_text, target_text, project) "
            "VALUES (?, ?, ?, ?)",
            (tm._hash_text(src), src, "The force acts on the body", "p"))
        tm.conn.commit()
        tm.close()

        tm2 = TranslationMemory(db_path=db, project_name="p", auto_clean=True)
        try:
            assert tm2.get_stats()["total_entries"] == 0
        finally:
            tm2.close()

    def test_confirmed_near_echo_survives(self, tmp_path):
        """Insan onayli birebir kayit (confirmed=1) silinmez/engellenmez."""
        tm = TranslationMemory(db_path=str(tmp_path / "tm.db"),
                               project_name="p", auto_clean=False)
        try:
            assert tm.add("The force acts on the body.",
                          "The force acts on the body", confirmed=True) is True
            got = tm.get("The force acts on the body.")
            assert got is not None
        finally:
            tm.close()

    def test_is_echo_output_gate(self):
        assert is_echo_output("The force acts.", "The force acts")
        assert is_echo_output("Newton", "newton")       # tam-esitlik varyanti
        assert not is_echo_output("The force acts.", "Kuvvet etki eder.")

    def test_translate_text_does_not_cache_near_echo(self, monkeypatch):
        """translate_text cagiran-taraf guard'i near-echo'yu memory'e yazmaz."""
        bt = make_bt()
        bt.memory = MagicMock()
        bt.memory.get_or_similar.return_value = (None, "none")
        src = "The force acts on the body."
        monkeypatch.setattr(BookTranslator, "_translate_with_mt",
                            lambda self, text, is_heading=False:
                            "The force acts on the body")
        out = bt.translate_text(src)
        assert out == "The force acts on the body"
        bt.memory.add.assert_not_called()

    def test_translate_text_caches_real_translation(self, monkeypatch):
        bt = make_bt()
        bt.memory = MagicMock()
        bt.memory.get_or_similar.return_value = (None, "none")
        monkeypatch.setattr(BookTranslator, "_translate_with_mt",
                            lambda self, text, is_heading=False:
                            "Kuvvet cisme etki eder.")
        bt.translate_text("The force acts on the body.")
        bt.memory.add.assert_called_once()

    def test_batch_protected_does_not_cache_near_echo(self, monkeypatch):
        bt = make_bt()
        bt.memory = MagicMock()
        bt.memory.get_or_similar.return_value = (None, "none")
        monkeypatch.setattr(BookTranslator, "_translate_batch_hymt",
                            lambda self, texts: [t.rstrip(".") for t in texts])
        out = bt.translate_batch_protected(["The force acts on the body."])
        assert out == ["The force acts on the body"]
        bt.memory.add.assert_not_called()


# ============================================================================
# F20 — generate kilidi devre kesici
# ============================================================================

@pytest.fixture(autouse=True)
def _reset_circuit():
    BookTranslator.reset_generate_circuit()
    yield
    BookTranslator.reset_generate_circuit()


class TestF20CircuitBreaker:
    def _hold_lock(self):
        """Class-level generate kilidini baska bir thread'de tutar."""
        acquired = threading.Event()
        release = threading.Event()

        def _worker():
            BookTranslator._mt_generate_lock.acquire()
            acquired.set()
            release.wait(10)
            BookTranslator._mt_generate_lock.release()

        th = threading.Thread(target=_worker, daemon=True)
        th.start()
        assert acquired.wait(5)
        return release, th

    def test_circuit_opens_after_consecutive_timeouts(self):
        bt = make_bt()
        bt._GENERATE_LOCK_TIMEOUT_S = 0.05  # instance override (test hizi)
        release, th = self._hold_lock()
        try:
            n = BookTranslator._GENERATE_LOCK_MAX_TIMEOUTS
            for _ in range(n):
                assert not BookTranslator._generate_circuit_open
                with pytest.raises(RuntimeError):
                    with bt._generate_lock():
                        pass
            assert BookTranslator._generate_circuit_open is True
        finally:
            release.set()
            th.join(5)

    def test_open_circuit_fails_fast_without_waiting(self):
        bt = make_bt()
        bt._GENERATE_LOCK_TIMEOUT_S = 0.05
        release, th = self._hold_lock()
        try:
            for _ in range(BookTranslator._GENERATE_LOCK_MAX_TIMEOUTS):
                with pytest.raises(RuntimeError):
                    with bt._generate_lock():
                        pass
            # Devre acik: sonraki cagri kilidi HIC beklemez
            t0 = time.perf_counter()
            for _ in range(20):
                with pytest.raises(RuntimeError) as exc:
                    with bt._generate_lock():
                        pass
            elapsed = time.perf_counter() - t0
            assert elapsed < 0.05, f"devre acikken beklendi: {elapsed:.3f}s"
            assert "devre kesici ACIK" in str(exc.value)
        finally:
            release.set()
            th.join(5)

    def test_message_has_no_oom_keywords(self):
        """Mesaj OOM dalini tetiklememeli (_translate_batch_hymt sozlesmesi)."""
        bt = make_bt()
        bt._GENERATE_LOCK_TIMEOUT_S = 0.05
        release, th = self._hold_lock()
        try:
            msgs = []
            for _ in range(BookTranslator._GENERATE_LOCK_MAX_TIMEOUTS + 1):
                with pytest.raises(RuntimeError) as exc:
                    with bt._generate_lock():
                        pass
                msgs.append(str(exc.value).lower())
            for m in msgs:
                for bad in ("out of memory", "oom", "cuda", "mps"):
                    assert bad not in m
        finally:
            release.set()
            th.join(5)

    def test_successful_acquire_resets_counter(self):
        bt = make_bt()
        bt._GENERATE_LOCK_TIMEOUT_S = 0.05
        release, th = self._hold_lock()
        try:
            with pytest.raises(RuntimeError):
                with bt._generate_lock():
                    pass
            assert BookTranslator._generate_lock_timeouts == 1
        finally:
            release.set()
            th.join(5)
        # Kilit serbest: basarili acquire sayaci sifirlar
        with bt._generate_lock():
            pass
        assert BookTranslator._generate_lock_timeouts == 0
        assert BookTranslator._generate_circuit_open is False

    def test_reset_generate_circuit(self):
        BookTranslator._generate_circuit_open = True
        BookTranslator._generate_lock_timeouts = 99
        BookTranslator.reset_generate_circuit()
        assert BookTranslator._generate_circuit_open is False
        assert BookTranslator._generate_lock_timeouts == 0

    def test_new_run_resets_circuit(self, monkeypatch):
        """Uzun omurlu surecte (Streamlit) yeni cevirici devreyi temizler."""
        BookTranslator._generate_circuit_open = True
        bt = make_bt()
        for name in ("_init_mt_model", "_init_glossary", "_init_memory",
                     "_init_ai_client"):
            monkeypatch.setattr(BookTranslator, name,
                                lambda self, *a, **k: None, raising=False)
        bt.config.use_memory = False
        bt.config.use_ai_review = False
        bt.config.run_qa = False
        bt.config.check_consistency = False
        bt.config.glossary_mode = "off"
        BookTranslator._initialize(bt)
        assert BookTranslator._generate_circuit_open is False

    def test_open_circuit_makes_mt_fail_immediately(self, monkeypatch):
        """Devre acikken _translate_with_mt aninda MT_FAIL doner."""
        bt = make_bt()
        bt._use_mlx = True
        BookTranslator._generate_circuit_open = True

        def _boom(*a, **k):  # generate'e ULASILMAMALI
            raise AssertionError("devre acikken generate cagrildi")

        monkeypatch.setattr(BookTranslator, "_translate_with_hymt", _boom)
        # _translate_with_hymt cagrilmadan once kilit alinir -> RuntimeError
        monkeypatch.setattr(BookTranslator, "_translate_single",
                            lambda self, text, max_length=512, is_heading=False:
                            self._translate_with_hymt_mlx_stub())

        def _stub(self):
            with self._generate_lock():
                return "asla"

        monkeypatch.setattr(BookTranslator, "_translate_with_hymt_mlx_stub",
                            _stub, raising=False)
        t0 = time.perf_counter()
        out = bt._translate_with_mt("The force acts on the body.")
        elapsed = time.perf_counter() - t0
        assert out.startswith("[MT_FAIL]")
        assert elapsed < 1.0

    def test_max_timeouts_constant_is_sane(self):
        from config.defaults import DEFAULT_RETRY_COUNT
        assert BookTranslator._GENERATE_LOCK_MAX_TIMEOUTS == DEFAULT_RETRY_COUNT
        assert BookTranslator._GENERATE_LOCK_MAX_TIMEOUTS >= 2


# ============================================================================
# F21 — desen on-derleme + on-filtre
# ============================================================================

class TestF21PatternCache:
    def test_pattern_cached_per_term(self):
        assert _glossary_term_pattern("mesh") is _glossary_term_pattern("mesh")
        assert _glossary_term_pattern("mesh") is not _glossary_term_pattern("node")

    def test_compile_not_called_per_segment(self, monkeypatch):
        """re.compile cagrisi TERIM basina bir kez (segment basina degil)."""
        bt = make_bt(glossary_mode="post")
        bt.glossary = fake_glossary({
            "mesh": {"en": "mesh", "tr": "ağ", "keep_original": False},
            "node": {"en": "node", "tr": "düğüm", "keep_original": False},
        })
        segs = [{"text": f"Bir mesh ve node var {i}."} for i in range(50)]

        calls = []
        real_compile = re.compile

        def counting(pattern, *a, **k):
            calls.append(pattern)
            return real_compile(pattern, *a, **k)

        monkeypatch.setattr(re, "compile", counting)
        bt._post_process_glossary_final(segs)
        monkeypatch.undo()
        # 50 segment x 2 terim = 100 derleme YERINE en fazla 2
        assert len(calls) <= 2, calls

    def test_prefilter_does_not_change_result(self):
        """On-filtre yalnizca hiz icin: sonuc referans uygulamayla ayni."""
        terms = {
            "mesh": {"en": "mesh", "tr": "ağ", "keep_original": False},
            "field": {"en": "field", "tr": "alan", "keep_original": False},
            "ion": {"en": "ion", "tr": "iyon", "keep_original": False},
            "bias": {"en": "bias", "tr": "önyargı", "keep_original": False},
        }
        texts = [
            "Bir mesh oluşturuldu.",
            "BİR MESH OLUŞTURULDU.",
            "FİELD hesabı yapıldı.",       # Turkce noktali I
            "bıas değeri düştü.",           # Turkce noktasiz i
            "The ION concentration is high.",
            "Şirketin finansal raporu incelendi.",  # \b korumasi
            "Hiçbir terim yok.",
        ]
        bt = make_bt(glossary_mode="post")
        bt.glossary = fake_glossary(terms)
        got = [s["text"] for s in
               bt._post_process_glossary_final([{"text": t} for t in texts])]

        expected = []
        for t in texts:
            text = t
            for term_lower, td in sorted(terms.items(),
                                         key=lambda kv: len(kv[1]["en"]),
                                         reverse=True):
                pat = re.compile(r"\b" + re.escape(td["en"]) + r"\b", re.IGNORECASE)
                if pat.search(text) and not td["keep_original"]:
                    text = pat.sub(td["tr"], text, count=1)
            expected.append(text)
        assert got == expected

    def test_scan_key_normalizes_turkish_i(self):
        assert _glossary_scan_key("FİN") == "fin"
        assert _glossary_scan_key("bıas") == "bias"

    def test_word_boundary_still_enforced(self):
        """CORE-01 sozlesmesi: substring ikamesi yok."""
        bt = make_bt(glossary_mode="post")
        bt.glossary = fake_glossary(
            {"fin": {"en": "fin", "tr": "kanatcik", "keep_original": False}})
        out = bt._post_process_glossary_final(
            [{"text": "Şirketin finansal raporları incelendi."}])
        assert out[0]["text"] == "Şirketin finansal raporları incelendi."


# ============================================================================
# F29 — longest-first terim sirasi
# ============================================================================

class TestF29LongestFirst:
    def test_multiword_term_wins_over_single_word(self):
        # Ekleme sirasi bilerek 'yanlis': kisa terim once
        terms = {
            "bias": {"en": "bias", "tr": "önyargı", "keep_original": False},
            "bias vector": {"en": "bias vector", "tr": "önyargı vektörü",
                            "keep_original": False},
        }
        bt = make_bt(glossary_mode="post")
        bt.glossary = fake_glossary(terms)
        out = bt._post_process_glossary_final([{"text": "The bias vector is used."}])
        assert out[0]["text"] == "The önyargı vektörü is used."

    def test_three_word_term_wins(self):
        terms = {
            "control": {"en": "control", "tr": "kontrol", "keep_original": False},
            "control volume": {"en": "control volume", "tr": "kontrol hacmi",
                               "keep_original": False},
            "finite control volume": {"en": "finite control volume",
                                      "tr": "sonlu kontrol hacmi",
                                      "keep_original": False},
        }
        bt = make_bt(glossary_mode="post")
        bt.glossary = fake_glossary(terms)
        out = bt._post_process_glossary_final(
            [{"text": "A finite control volume is defined."}])
        assert out[0]["text"] == "A sonlu kontrol hacmi is defined."

    def test_real_curated_glossary_bias_vector(self):
        """Gercek curated veriyle: 'bias' idx < 'bias vector' idx."""
        from core.glossary import GlossaryManager
        gm = GlossaryManager()
        if "bias vector" not in gm.terms or "bias" not in gm.terms:
            pytest.skip("curated glossary'de bias/bias vector yok")
        bt = make_bt(glossary_mode="post")
        bt.glossary = gm
        out = bt._post_process_glossary_final([{"text": "The bias vector is used."}])
        assert "önyargı vektörü" in out[0]["text"]
        assert "önyargı vector" not in out[0]["text"]


# ============================================================================
# F41 — dokuman seviyesi fazlar
# ============================================================================

def _stub_batch_translation(bt, mapping=None):
    """MT'yi sozluk/echo ile taklit et (N->N)."""
    def _fake(texts):
        return [(mapping or {}).get(t, t) for t in texts]
    bt._translate_batch_segments = _fake


class TestF41DocumentPasses:
    def test_flag_false_skips_passes(self):
        bt = make_bt(glossary_mode="post", check_consistency=True)
        bt.consistency_analyzer = MagicMock()
        bt.glossary = fake_glossary(
            {"mesh": {"en": "mesh", "tr": "ağ", "keep_original": False}})
        _stub_batch_translation(bt)
        out = bt.translate_segments([{"text": "Bir mesh var."}],
                                    run_document_passes=False)
        bt.consistency_analyzer.fix_inconsistencies.assert_not_called()
        assert out[0]["text"] == "Bir mesh var."  # glossary-final de kosmadi

    def test_flag_default_runs_passes(self):
        bt = make_bt(glossary_mode="post", check_consistency=False)
        bt.glossary = fake_glossary(
            {"mesh": {"en": "mesh", "tr": "ağ", "keep_original": False}})
        _stub_batch_translation(bt)
        out = bt.translate_segments([{"text": "Bir mesh var."}])
        assert out[0]["text"] == "Bir ağ var."

    def test_run_document_passes_helper(self):
        bt = make_bt(glossary_mode="post", check_consistency=True,
                     auto_fix_consistency=True)
        report = MagicMock()
        report.auto_fixed = 3
        segs = [{"text": "Bir mesh var."}]
        bt.consistency_analyzer = MagicMock()
        bt.consistency_analyzer.fix_inconsistencies.return_value = (segs, report)
        bt.glossary = fake_glossary(
            {"mesh": {"en": "mesh", "tr": "ağ", "keep_original": False}})
        out = bt._run_document_passes(segs)
        bt.consistency_analyzer.fix_inconsistencies.assert_called_once()
        assert out[0]["text"] == "Bir ağ var."
        assert bt.stats["consistency_fixes"] == 3

    def test_epub_runs_consistency_once_over_whole_document(self, tmp_path,
                                                            monkeypatch):
        """F41 cekirdegi: 40 segmentlik EPUB'da analiz penceresi TUM dokuman."""
        import parsers.epub_parser as ep
        import exporters.epub_exporter as ee

        n = 40
        segments = [{"text": f"sentence number {i}", "type": "paragraph"}
                    for i in range(n)]

        class FakeDoc:
            title = "T"
            author = "A"

        class FakeParser:
            def parse(self, path):
                return FakeDoc()

            def extract_segments(self, doc):
                return [dict(s) for s in segments]

        class FakeExporter:
            def export(self, segs, path, title=None, author=None):
                Path(path).write_text("ok", encoding="utf-8")

        monkeypatch.setattr(ep, "EPUBParser", FakeParser)
        monkeypatch.setattr(ee, "EPUBExporter", FakeExporter)
        monkeypatch.setattr(tr, "CHECKPOINT_DIR", tmp_path / "ckpt")

        src = tmp_path / "book.epub"
        src.write_bytes(b"epub-bytes")
        out_path = tmp_path / "out.epub"

        bt = make_bt(batch_size=20, glossary_mode="off", check_consistency=True)
        _stub_batch_translation(bt)

        seen_lengths = []

        def _fix(segs, auto_fix=True):
            seen_lengths.append(len(segs))
            rep = MagicMock()
            rep.auto_fixed = 0
            return segs, rep

        bt.consistency_analyzer = MagicMock()
        bt.consistency_analyzer.fix_inconsistencies.side_effect = _fix

        bt._translate_epub(str(src), str(out_path), "epub", None)

        # Eskiden: 2 cagri x 20 segment (batch-yerel pencere)
        assert seen_lengths == [n], seen_lengths

    def test_epub_glossary_final_runs_once(self, tmp_path, monkeypatch):
        import parsers.epub_parser as ep
        import exporters.epub_exporter as ee

        n = 40
        segments = [{"text": f"a mesh number {i}", "type": "paragraph"}
                    for i in range(n)]

        class FakeDoc:
            title = "T"
            author = "A"

        class FakeParser:
            def parse(self, path):
                return FakeDoc()

            def extract_segments(self, doc):
                return [dict(s) for s in segments]

        class FakeExporter:
            def export(self, segs, path, title=None, author=None):
                Path(path).write_text("ok", encoding="utf-8")

        monkeypatch.setattr(ep, "EPUBParser", FakeParser)
        monkeypatch.setattr(ee, "EPUBExporter", FakeExporter)
        monkeypatch.setattr(tr, "CHECKPOINT_DIR", tmp_path / "ckpt")

        src = tmp_path / "book2.epub"
        src.write_bytes(b"epub-bytes-2")
        bt = make_bt(batch_size=20, glossary_mode="post", check_consistency=False)
        _stub_batch_translation(bt)
        bt.glossary = fake_glossary(
            {"mesh": {"en": "mesh", "tr": "ağ", "keep_original": False}})

        calls = []
        real = BookTranslator._post_process_glossary_final

        def counting(self, segs):
            calls.append(len(segs))
            return real(self, segs)

        monkeypatch.setattr(BookTranslator, "_post_process_glossary_final",
                            counting)
        bt._translate_epub(str(src), str(tmp_path / "o2.epub"), "epub", None)
        assert calls == [n], calls

    def test_txt_runs_passes_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tr, "CHECKPOINT_DIR", tmp_path / "ckpt")
        src = tmp_path / "in.txt"
        src.write_text("\n\n".join(f"line number {i}" for i in range(40)),
                       encoding="utf-8")
        out = tmp_path / "out.txt"

        bt = make_bt(batch_size=10, glossary_mode="off", check_consistency=True)
        bt.splitter = MagicMock()
        bt.splitter.split.side_effect = lambda p: [p]
        _stub_batch_translation(bt)

        seen = []

        def _fix(segs, auto_fix=True):
            seen.append(len(segs))
            rep = MagicMock()
            rep.auto_fixed = 0
            return segs, rep

        bt.consistency_analyzer = MagicMock()
        bt.consistency_analyzer.fix_inconsistencies.side_effect = _fix
        bt._translate_text_file(str(src), str(out), None)
        assert seen == [40], seen


# ============================================================================
# F49 — find_similar filtre sirasi + satir cache
# ============================================================================

class TestF49FindSimilar:
    def _tm(self, tmp_path, name="tm.db"):
        return TranslationMemory(db_path=str(tmp_path / name),
                                 project_name="p", auto_clean=False)

    def test_clean_match_still_returned(self, tmp_path):
        tm = self._tm(tmp_path)
        try:
            tm.add("The force acting on the body", "Cisme etki eden kuvvet")
            res = tm.find_similar("The force acting on the bodies", threshold=80)
            assert res and res[0]["target"] == "Cisme etki eden kuvvet"
        finally:
            tm.close()

    def test_number_mismatch_still_rejected(self, tmp_path):
        tm = self._tm(tmp_path)
        try:
            tm.add("in 1990 the company grew by 5 percent.",
                   "1990'da sirket yuzde 5 buyudu.")
            assert tm.find_similar("in 1991 the company grew by 7 percent.",
                                   threshold=90) == []
        finally:
            tm.close()

    def test_placeholder_target_still_filtered(self, tmp_path):
        tm = self._tm(tmp_path)
        try:
            src = "The force acting on the body"
            tm.conn.execute(
                "INSERT INTO translations (hash, source_text, target_text, project) "
                "VALUES (?, ?, ?, ?)",
                (tm._hash_text(src), src, "Cisme etki eden <<LATEX_3>>", "p"))
            tm.conn.commit()
            assert tm.find_similar("The force acting on the bodies",
                                   threshold=80) == []
        finally:
            tm.close()

    def test_echo_row_still_filtered(self, tmp_path):
        tm = self._tm(tmp_path)
        try:
            src = "The force acting on the body"
            tm.conn.execute(
                "INSERT INTO translations (hash, source_text, target_text, project) "
                "VALUES (?, ?, ?, ?)",
                (tm._hash_text(src), src, "The force acting on the body.", "p"))
            tm.conn.commit()
            assert tm.find_similar("The force acting on the bodies",
                                   threshold=80) == []
        finally:
            tm.close()

    def test_row_cache_invalidated_on_source_change(self, tmp_path):
        tm = self._tm(tmp_path)
        try:
            tm.add("The force acting on the body", "Cisme etki eden kuvvet")
            assert tm.find_similar("The force acting on the bodies", threshold=80)
            # Ayni id, farkli kaynak (rakam eklendi) -> cache tazelenmeli
            tm.conn.execute(
                "UPDATE translations SET source_text = ? WHERE source_text = ?",
                ("The force acting on 2 bodies", "The force acting on the body"))
            tm.conn.commit()
            assert tm.find_similar("The force acting on the bodies",
                                   threshold=80) == []
        finally:
            tm.close()

    def test_cache_cleared_when_too_big(self, tmp_path, monkeypatch):
        from core import memory as mem
        monkeypatch.setattr(mem, "_ROW_CACHE_MAX_ENTRIES", 1)
        tm = self._tm(tmp_path)
        try:
            for i in range(5):
                tm.add(f"The measured value number {i} was recorded here",
                       f"Olculen {i} numarali deger buraya kaydedildi")
            tm.find_similar("The measured value number 2 was recorded here")
            tm.find_similar("The measured value number 3 was recorded here")
            assert len(tm._similar_row_cache) <= 5
        finally:
            tm.close()

    def test_results_match_reference_implementation(self, tmp_path):
        """Yeni sira eski sonuc kumesini birebir korur."""
        from rapidfuzz import fuzz
        from core.memory import _PLACEHOLDER_RE
        tm = self._tm(tmp_path)
        try:
            rows = [
                ("The force acting on the body", "Cisme etki eden kuvvet"),
                ("The force acting on the bodys", "Cisimlere etki eden kuvvet"),
                ("The force acting on 3 bodies", "3 cisme etki eden kuvvet"),
                ("The force acting on the boxes", "Kutulara etki eden kuvvet"),
            ]
            for s, t in rows:
                tm.add(s, t)
            query = "The force acting on the bodies"
            got = {r["source"] for r in tm.find_similar(query, threshold=80,
                                                        limit=10)}
            # Referans: eski sira (guard -> fuzz -> rakam)
            q = query.strip().lower()
            qn = sorted(re.findall(r"\d+", q))
            expected = set()
            cur = tm.conn.execute("SELECT * FROM translations WHERE project='p'")
            for row in cur.fetchall():
                if _PLACEHOLDER_RE.search(row["target_text"]):
                    continue
                if _PLACEHOLDER_RE.search(row["source_text"]):
                    continue
                stored = row["source_text"].lower()
                score = fuzz.ratio(q, stored)
                if qn != sorted(re.findall(r"\d+", stored)):
                    continue
                if score >= 80:
                    expected.add(row["source_text"])
            assert got == expected
        finally:
            tm.close()


# ============================================================================
# F50 — checkpoint I/O
# ============================================================================

class TestF50CheckpointIO:
    def _bt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tr, "CHECKPOINT_DIR", tmp_path / "ckpt")
        bt = make_bt()
        src = tmp_path / "book.epub"
        src.write_bytes(b"content")
        return bt, str(src)

    def test_no_full_read_on_repeated_saves(self, tmp_path, monkeypatch):
        bt, src = self._bt(tmp_path, monkeypatch)
        loads = []
        real_load = json.load

        def counting(fp, *a, **k):
            loads.append(1)
            return real_load(fp, *a, **k)

        monkeypatch.setattr(json, "load", counting)
        segs = []
        for i in range(6):
            segs.append({"text": f"seg {i}"})
            bt._save_text_checkpoint(src, "epub", list(segs), 6)
        monkeypatch.undo()
        # Eskiden her kayitta buyuyen dosya bastan okunuyordu
        assert loads == [], f"{len(loads)} kez json.load yapildi"

    def test_created_at_stable_across_saves(self, tmp_path, monkeypatch):
        bt, src = self._bt(tmp_path, monkeypatch)
        bt._save_text_checkpoint(src, "epub", [{"text": "a"}], 2)
        first = bt._load_text_checkpoint(src, "epub")
        time.sleep(0.01)
        bt._save_text_checkpoint(src, "epub", [{"text": "a"}, {"text": "b"}], 2)
        second = bt._load_text_checkpoint(src, "epub")
        assert first["created_at"] == second["created_at"]
        assert first["updated_at"] != second["updated_at"]

    def test_created_at_read_from_existing_file_on_resume(self, tmp_path,
                                                          monkeypatch):
        """Yeni surec (yeni instance) eski dosyanin created_at'ini korur."""
        bt, src = self._bt(tmp_path, monkeypatch)
        bt._save_text_checkpoint(src, "epub", [{"text": "a"}], 2)
        original = bt._load_text_checkpoint(src, "epub")["created_at"]

        bt2 = make_bt()  # taze instance = yeni surec
        bt2._save_text_checkpoint(src, "epub", [{"text": "a"}, {"text": "b"}], 2)
        assert bt2._load_text_checkpoint(src, "epub")["created_at"] == original

    def test_old_indented_checkpoint_still_readable(self, tmp_path, monkeypatch):
        """Resume uyumlulugu: eski indent'li dosya aynen okunur."""
        bt, src = self._bt(tmp_path, monkeypatch)
        from core.translator import get_file_hash
        path = bt._get_checkpoint_path(get_file_hash(src), "epub")
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy = {
            "file_hash": get_file_hash(src),
            "file_type": "epub",
            "input_path": src,
            "total_segments": 2,
            "completed_segments": 1,
            "translated_segments": [{"text": "eski"}],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "metadata": {},
        }
        path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2),
                        encoding="utf-8")

        loaded = bt._load_text_checkpoint(src, "epub")
        assert loaded["translated_segments"] == [{"text": "eski"}]
        # Uzerine yazinca created_at korunur
        bt._save_text_checkpoint(src, "epub",
                                 [{"text": "eski"}, {"text": "yeni"}], 2)
        assert bt._load_text_checkpoint(src, "epub")["created_at"] == \
            "2026-01-01T00:00:00"

    def test_compact_output_is_smaller(self, tmp_path, monkeypatch):
        bt, src = self._bt(tmp_path, monkeypatch)
        segs = [{"text": f"segment {i}", "type": "paragraph"} for i in range(200)]
        bt._save_text_checkpoint(src, "epub", segs, 200)
        from core.translator import get_file_hash
        path = bt._get_checkpoint_path(get_file_hash(src), "epub")
        compact = path.read_text(encoding="utf-8")
        indented = json.dumps(json.loads(compact), ensure_ascii=False, indent=2)
        assert len(compact) < len(indented)
        assert "\n" not in compact  # tek satir
        assert json.loads(compact)["completed_segments"] == 200

    def test_delete_resets_created_at_cache(self, tmp_path, monkeypatch):
        bt, src = self._bt(tmp_path, monkeypatch)
        bt._save_text_checkpoint(src, "epub", [{"text": "a"}], 1)
        first = bt._load_text_checkpoint(src, "epub")["created_at"]
        bt._delete_text_checkpoint(src, "epub")
        time.sleep(0.01)
        bt._save_text_checkpoint(src, "epub", [{"text": "a"}], 1)
        assert bt._load_text_checkpoint(src, "epub")["created_at"] != first

    def test_roundtrip_unicode_preserved(self, tmp_path, monkeypatch):
        bt, src = self._bt(tmp_path, monkeypatch)
        segs = [{"text": "Türkçe çığır şişme öğün"}]
        bt._save_text_checkpoint(src, "epub", segs, 1)
        loaded = bt._load_text_checkpoint(src, "epub")
        assert loaded["translated_segments"] == segs
