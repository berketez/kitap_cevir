"""
Yol B (HY-MT otomatik ceviri) core grubu fix testleri.

Kapsam (spec_core.md bulgu -> test):
- SOZLESME 1: translate_text(is_heading) + _build_translation_instruction baslik modu
- SOZLESME 2: translate_batch_protected (koruma zinciri + toplu MT + 1:1 cikti)
- CORE-02 / B-10: _fix_unknown_tokens pozisyon-bazli sozluk enjeksiyonu SILINDI
- CORE-03: _strip_prompt_leaks fazla-silme korumasi (mesru Turkce silinmesin)
- CORE-04: memory.find_similar sayi guard'i
- CORE-15: memory.find_similar proje kapsami (default sizintisi yok)
- CORE-09: _protect_formulas para/olcum yanlis-pozitifi
- CORE-01: _post_process_glossary_final kelime siniri (\\b)
- CORE-06 / CORE-14: batch yolu memory esigi + erken-donus guard'lari
- A7: olu beam_size zinciri kaldirildi (imza kontrolu)
- DATA-08: DictionaryLoader.get_translation dejenere kayit atlama

Strateji: BookTranslator.__new__ ile __init__'i ATLAYARAK gercek metodlu ama
MT modeli yuklemeyen bir instance kuruyoruz; MT katmanini mock'luyoruz.
"""

import sys
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.translator import BookTranslator, TranslationConfig
from core.memory import TranslationMemory
from core.dictionary_loader import DictionaryLoader


def make_bt(**cfg):
    """__init__ atlayarak gercek metodlu BookTranslator; MT yuklenmez."""
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


# ============================================================================
# SOZLESME 1: is_heading
# ============================================================================

class TestHeadingInstruction:
    def test_heading_mode_distinct_and_no_echo(self):
        bt = make_bt(book_type="science")
        heading = bt._build_translation_instruction("English", "Turkish", is_heading=True)
        normal = bt._build_translation_instruction("English", "Turkish")
        assert heading != normal
        low = heading.lower()
        assert "heading" in low
        assert "only" in low
        assert "do not repeat" in low  # echo yasak

    def test_heading_overrides_booktype(self):
        # book_type ne olursa olsun baslik modu ayni kisa talimati verir
        sci = make_bt(book_type="science")._build_translation_instruction("English", "Turkish", is_heading=True)
        lit = make_bt(book_type="novel")._build_translation_instruction("English", "Turkish", is_heading=True)
        assert sci == lit

    def test_translate_with_mt_threads_is_heading(self):
        bt = make_bt()
        bt._translate_single = MagicMock(return_value="X")
        bt._translate_with_mt("kisa metin", is_heading=True)
        _, kwargs = bt._translate_single.call_args
        assert kwargs.get("is_heading") is True

    def test_translate_with_mt_default_not_heading(self):
        bt = make_bt()
        bt._translate_single = MagicMock(return_value="X")
        bt._translate_with_mt("kisa metin")
        _, kwargs = bt._translate_single.call_args
        assert kwargs.get("is_heading") is False


# ============================================================================
# CORE-02 / B-10: _fix_unknown_tokens
# ============================================================================

class TestFixUnknownTokens:
    def test_no_tokens_unchanged(self):
        bt = make_bt()
        assert bt._fix_unknown_tokens("orig", "çeviri metni") == "çeviri metni"

    def test_no_dictionary_injection_and_revert(self):
        """CORE-02: sozluk ASLA enjekte edilmez; cozulemezse orijinale doner."""
        bt = make_bt()
        # Sozluk cagrilirsa hemen fark ederiz
        bt._get_dictionary_translation = MagicMock(return_value="arap rakamları sistemi")
        out = bt._fix_unknown_tokens("we run the simulation daily.", "her gün [?].")
        assert "arap rakamları sistemi" not in out
        bt._get_dictionary_translation.assert_not_called()
        assert "[?]" not in out
        # kucuk harfli kaynak, ozel isim yok -> segment orijinaliyle geri doner
        assert out == "we run the simulation daily."

    def test_the_definition_not_injected(self):
        """B-10: sozlugun 'the' TANIM cumlesi metne girmemeli."""
        bt = make_bt()
        bt._get_dictionary_translation = MagicMock(
            return_value="belirli durumlarda isimden önce kullanılır"
        )
        out = bt._fix_unknown_tokens("the cat sat on the mat.", "[?] mindere oturdu.")
        assert "belirli durumlarda" not in out
        assert "[?]" not in out

    def test_proper_noun_preserved(self):
        bt = make_bt()
        out = bt._fix_unknown_tokens("Einstein was smart", "[?] zekiydi")
        assert out == "Einstein zekiydi"

    def test_unk_and_angle_variants(self):
        bt = make_bt()
        assert "[UNK]" not in bt._fix_unknown_tokens("source here", "çeviri [UNK] son")
        assert "<unk>" not in bt._fix_unknown_tokens("source here", "çeviri <unk> son")


# ============================================================================
# CORE-03: _strip_prompt_leaks fazla-silme korumasi
# ============================================================================

class TestStripPromptLeaks:
    def test_legit_turkish_not_deleted(self):
        bt = make_bt()
        txt = "Orijinal metinde Pavlus bu ifadeyi farklı kullanır. İkinci cümle sağlam."
        out = bt._strip_prompt_leaks(txt, original="English source here")
        assert out != "English source here"   # Ingilizce kaynaga DONMEZ
        assert out.strip() != ""
        assert "Pavlus" in out                # mesru icerik korundu

    def test_no_single_word_collapse(self):
        bt = make_bt()
        txt = "Kutsal metni Türkçeye çevirmek yüzyıllar süren bir tartışmadır."
        out = bt._strip_prompt_leaks(txt, original="EN")
        assert out != "EN"
        assert len(out) > len("Kutsal")       # tek kelimeye inmedi

    def test_empty_input_returns_input(self):
        bt = make_bt()
        assert bt._strip_prompt_leaks("", original="x") == ""


# ============================================================================
# CORE-09: _protect_formulas para/olcum yanlis-pozitifi
# ============================================================================

class TestProtectFormulasMoney:
    def test_money_span_not_protected(self):
        bt = make_bt()
        text = "The project cost $5 million in 2020 and $8 million in 2021."
        out, formulas = bt._protect_formulas(text)
        assert "[[FORMULA_" not in out
        assert formulas == []
        assert out == text

    def test_real_inline_math_protected(self):
        bt = make_bt()
        out, formulas = bt._protect_formulas("Denklem $x_i^2 + y$ onemli.")
        assert "[[FORMULA_0]]" in out
        assert len(formulas) == 1

    def test_short_number_formula_still_protected(self):
        bt = make_bt()
        out, formulas = bt._protect_formulas("Deger $5$ olarak alinir.")
        assert "[[FORMULA_0]]" in out
        assert len(formulas) == 1


# ============================================================================
# CORE-04 + CORE-15: memory
# ============================================================================

class TestMemoryGuards:
    def test_number_guard_rejects_diff_numbers(self, tmp_path):
        tm = TranslationMemory(db_path=str(tmp_path / "m.db"), project_name="p1")
        tm.add("in 1990 the company grew by 5 percent.",
               "1990 yilinda sirket yuzde 5 buyudu.")
        res = tm.find_similar("in 1991 the company grew by 7 percent.", threshold=90)
        assert res == []
        tm.close()

    def test_number_guard_allows_same_numbers(self, tmp_path):
        tm = TranslationMemory(db_path=str(tmp_path / "m.db"), project_name="p1")
        tm.add("figure 3 shows the result.", "sekil 3 sonucu gosterir.")
        res = tm.find_similar("figure 3 shows the results.", threshold=80)
        assert len(res) >= 1
        tm.close()

    def test_number_guard_allows_no_numbers(self, tmp_path):
        tm = TranslationMemory(db_path=str(tmp_path / "m.db"), project_name="p1")
        tm.add("the force acting on the object", "nesneye etkiyen kuvvet")
        res = tm.find_similar("the force acting on an object", threshold=80)
        assert len(res) >= 1
        tm.close()

    def test_project_scope_no_default_leak(self, tmp_path):
        db = str(tmp_path / "m.db")
        tm_def = TranslationMemory(db_path=db, project_name="default")
        tm_def.add("the force acting on the object", "nesneye etkiyen kuvvet")
        tm_p = TranslationMemory(db_path=db, project_name="physics")
        # CORE-15: default havuzu artik find_similar'a sizmaz
        assert tm_p.find_similar("the force acting on the object", threshold=90) == []
        tm_p.close()
        tm_def.close()


# ============================================================================
# CORE-01: _post_process_glossary_final kelime siniri
# ============================================================================

class TestGlossaryFinalWordBoundary:
    def _glossary(self, terms):
        g = MagicMock()
        g.terms = terms
        return g

    def test_no_substring_injection(self):
        bt = make_bt(glossary_mode="post")
        bt.glossary = self._glossary(
            {"fin": {"en": "fin", "tr": "kanatcik", "keep_original": False}}
        )
        segs = [{"text": "Şirketin finansal raporları incelendi."}]
        out = bt._post_process_glossary_final(segs)
        assert "kanatcik" not in out[0]["text"]
        assert out[0]["text"] == "Şirketin finansal raporları incelendi."

    def test_standalone_term_replaced(self):
        bt = make_bt(glossary_mode="post")
        bt.glossary = self._glossary(
            {"mesh": {"en": "mesh", "tr": "ağ", "keep_original": False}}
        )
        segs = [{"text": "Bir mesh oluşturuldu."}]
        out = bt._post_process_glossary_final(segs)
        assert out[0]["text"] == "Bir ağ oluşturuldu."

    def test_mt_failed_segment_skipped(self):
        bt = make_bt(glossary_mode="post")
        bt.glossary = self._glossary(
            {"mesh": {"en": "mesh", "tr": "ağ", "keep_original": False}}
        )
        segs = [{"text": "Bir mesh var.", "_mt_failed": True}]
        out = bt._post_process_glossary_final(segs)
        # _mt_failed -> dokunulmaz
        assert out[0]["text"] == "Bir mesh var."


# ============================================================================
# CORE-14 + CORE-06: batch guard + esik
# ============================================================================

class TestBatchSegmentGuards:
    def test_non_translatable_not_sent_to_mt(self):
        bt = make_bt(glossary_mode="post")
        bt.memory = None
        bt._translate_batch_hymt = MagicMock(side_effect=lambda ts: ["ÇEV:" + t for t in ts])
        out = bt._translate_batch_segments(["42", "$x$", "  ", "Gerçek cümle burada."])
        assert out[0] == "42"
        assert out[1] == "$x$"
        assert out[2] == "  "
        assert out[3] == "ÇEV:Gerçek cümle burada."
        # yalnizca gercek cumle MT'ye gitti
        bt._translate_batch_hymt.assert_called_once()
        assert bt._translate_batch_hymt.call_args[0][0] == ["Gerçek cümle burada."]

    def test_memory_threshold_from_config(self):
        bt = make_bt(memory_threshold=95, glossary_mode="post")
        bt.memory = MagicMock()
        bt.memory.get_or_similar = MagicMock(return_value=(None, "none"))
        bt.config.use_memory = True
        bt._translate_batch_hymt = MagicMock(side_effect=lambda ts: ["x"] * len(ts))
        bt._translate_batch_segments(["Gercek bir cumle buraya."])
        assert bt.memory.get_or_similar.call_args[0][1] == 95

    def test_soft_hint_no_longer_injected(self):
        """CORE-B5/RT-B5: '[Terimler: ...]' oneki artik metne eklenmiyor."""
        bt = make_bt(glossary_mode="soft")
        bt.memory = None
        bt.ner_protector = None
        g = MagicMock()
        g.protect_terms = MagicMock(
            side_effect=lambda t: (t, [{"term_en": "orbit", "term_tr": "yörünge", "mode": "soft"}])
        )
        g.restore_terms = MagicMock(side_effect=lambda t, terms: (t, []))
        bt.glossary = g
        captured = {}

        def fake_mt(ts):
            captured["texts"] = ts
            return ["ÇEV"] * len(ts)

        bt._translate_batch_hymt = MagicMock(side_effect=fake_mt)
        bt._translate_batch_segments(["Yörünge hesabı."])
        assert all("[Terimler:" not in t for t in captured["texts"])


# ============================================================================
# SOZLESME 2: translate_batch_protected
# ============================================================================

class TestTranslateBatchProtected:
    def test_length_preserved_and_protection(self):
        bt = make_bt(glossary_mode="post")
        bt.memory = None
        bt.naturalizer = None
        bt._translate_batch_hymt = MagicMock(side_effect=lambda ts: [t.upper() for t in ts])
        inp = ["Bak \\cite{x} burada.", "42", "Normal cümle."]
        out = bt.translate_batch_protected(inp)
        assert len(out) == len(inp)
        assert out[1] == "42"                 # cevrilemez korunur
        assert "\\cite{x}" in out[0]          # referans restore edildi

    def test_empty_input(self):
        bt = make_bt()
        assert bt.translate_batch_protected([]) == []

    def test_memory_lookup_short_circuits(self):
        bt = make_bt(glossary_mode="post")
        bt.memory = MagicMock()
        bt.memory.get_or_similar = MagicMock(return_value=("ÖNBELLEK", "exact"))
        bt._translate_batch_hymt = MagicMock(side_effect=lambda ts: ["MT"] * len(ts))
        out = bt.translate_batch_protected(["Cache olmali bu cumle."])
        assert out == ["ÖNBELLEK"]
        bt._translate_batch_hymt.assert_not_called()

    def test_memory_add_skips_echo(self):
        bt = make_bt(glossary_mode="post")
        bt.memory = MagicMock()
        bt.memory.get_or_similar = MagicMock(return_value=(None, "none"))
        bt.naturalizer = None
        # MT echo: cikti == girdi (cevrilmemis)
        bt._translate_batch_hymt = MagicMock(side_effect=lambda ts: list(ts))
        out = bt.translate_batch_protected(["This is a sentence."])
        assert len(out) == 1
        bt.memory.add.assert_not_called()     # CORE-05: echo cache'lenmez

    def test_memory_add_called_for_real_translation(self):
        bt = make_bt(glossary_mode="post")
        bt.memory = MagicMock()
        bt.memory.get_or_similar = MagicMock(return_value=(None, "none"))
        bt.naturalizer = None
        bt._translate_batch_hymt = MagicMock(side_effect=lambda ts: ["Bu bir cümledir."] * len(ts))
        bt.translate_batch_protected(["This is a sentence."])
        bt.memory.add.assert_called_once()


# ============================================================================
# A7: olu beam_size zinciri kaldirildi
# ============================================================================

class TestBeamSizeRemoved:
    def test_no_beam_size_param(self):
        import inspect
        for name in ("_translate_with_mt", "_translate_single", "_translate_with_hymt"):
            sig = inspect.signature(getattr(BookTranslator, name))
            assert "beam_size" not in sig.parameters, f"{name} hala beam_size aliyor"


# ============================================================================
# DATA-08: DictionaryLoader.get_translation dejenere atlama
# ============================================================================

class TestDictionaryLoaderDegenerate:
    def _loader(self, by_word):
        dl = DictionaryLoader.__new__(DictionaryLoader)
        dl._data = []
        dl._by_category = {}
        dl._by_word = by_word
        return dl

    def test_skips_identity_entry(self):
        dl = self._loader({"set": [{"word": "set", "tr": "set"}, {"word": "set", "tr": "küme"}]})
        # EN==TR ('set') atlanir, 'küme' doner
        assert dl.get_translation("set") == "küme"

    def test_skips_empty_tr(self):
        dl = self._loader({"x": [{"word": "x", "tr": ""}, {"word": "x", "tr": "gerçek"}]})
        assert dl.get_translation("x") == "gerçek"

    def test_missing_returns_none(self):
        dl = self._loader({})
        assert dl.get_translation("yok") is None
