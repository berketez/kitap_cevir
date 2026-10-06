"""Yol B (HY-MT) autofix düzeltme testleri — spec_autofix.md bulguları.

Kapsanan bulgular:
  AF-C4  — partial-match heading fix'in garbage üretmesi (silindi)
  AF-C6/DATA-04/DATA-C6 — ASCII-bozuk _HEADING_GLOSSARY silindi, translator'ın
           kanonik sözlüğü reuse edildi; _WRONG_TRANSLATION_MAP çelişkisi kaldırıldı
  AF-C7  — re-translate handler'larına kabul kapısı (echo/İngilizce/boş red)
  AF-10  — heading translator yolu bare_heading gönderir, prefix'i geri ekler
  AF-11  — _retranslate_with_context son çare dalı orijinali korur
  AF-06  — _fix_mixed_language_line doğrulamasız fallback dalı silindi
  AF-03  — Türkçe İ/ı case koruması
  AF-05  — glossary_compliance bayat math_spans yerine stash koruması
  DATA-01/AF-02/QC-N1 — yapısal wrong-translation map manual modda ÇALIŞMAZ (Yol A kilidi)
  QC-N2  — first_person_academic manual modda atlanır
  QC-N3  — antonym_guard word-boundary (substring garbage önlenir)
  DATA-05 — _TURKISH_DEASCIIFY dup/no-op temizliği
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latex_pipeline.autofix import (
    AutoFixer,
    _find_math_spans,
    _tr_upper,
    _tr_upper_first,
    _make_case_preserving_repl,
)
from latex_pipeline.qc_engine import QCIssue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _issue(check, line, sugg="", context=""):
    return QCIssue(
        check_name=check, severity="warning", line_num=line, message="",
        auto_fixable=True, confidence=0.9, fix_suggestion=sugg, context=context,
    )


class _EchoTranslator:
    """Translator that echoes English input (simulates MT giving up)."""
    def _translate_heading_text(self, t):
        return t


class _GlossaryTranslator:
    """Translator that translates a few known headings, echoes the rest."""
    _MAP = {"Introduction": "Giriş", "Conclusion": "Sonuç"}

    def _translate_heading_text(self, t):
        return self._MAP.get(t.strip(), t)


# ---------------------------------------------------------------------------
# Turkish case helpers (AF-03)
# ---------------------------------------------------------------------------

def test_af03_tr_upper_first_dotted_i():
    assert _tr_upper_first("için") == "İçin"
    assert _tr_upper_first("ışık") == "Işık"


def test_af03_tr_upper_all_caps():
    assert _tr_upper("için") == "İÇİN"
    assert _tr_upper("ışık") == "IŞIK"


def test_af03_case_preserving_repl_uppercases_turkish():
    import re
    repl = _make_case_preserving_repl("için")
    # Sentence-initial capital must survive as Turkish İ, not ASCII I
    assert re.sub(r"(?i)icin", repl, "Icin") == "İçin"


def test_af03_ascii_degradation_all_caps_turkish():
    af = AutoFixer()
    body = "ICIN BU ISLEM ONEMLI"
    out, fixes = af._fix_ascii_degradation(body, [], _find_math_spans(body))
    # All-caps must produce İ, not I (Turkish rule)
    assert "İÇİN" in out


# ---------------------------------------------------------------------------
# Acceptance gate (AF-C7)
# ---------------------------------------------------------------------------

def test_afc7_accept_gate_rejects_english():
    assert AutoFixer._accept_retranslation(
        "x", "This is clearly an English sentence with many words") is False


def test_afc7_accept_gate_rejects_echo_and_empty():
    assert AutoFixer._accept_retranslation("abc", "abc") is False
    assert AutoFixer._accept_retranslation("abc", "   ") is False
    assert AutoFixer._accept_retranslation("abc", None) is False


def test_afc7_accept_gate_accepts_turkish():
    assert AutoFixer._accept_retranslation(
        "Hello", "Bu yeterince uzun bir Türkçe cümledir.") is True


def test_afc7_heading_echo_rejected():
    af = AutoFixer(translator=_EchoTranslator())
    body = r"\section{Spanning Trees}"
    out, fixes = af._fix_untranslated_heading(body, [_issue("untranslated_heading", 1)],
                                              _find_math_spans(body))
    # Echoed English must be rejected -> heading unchanged, no garbage
    assert out == body
    assert not fixes


# ---------------------------------------------------------------------------
# Heading fix consolidation (AF-C4 / AF-10 / DATA-04)
# ---------------------------------------------------------------------------

def test_afc4_partial_match_no_garbage():
    """Compound heading, no exact match, no translator -> untouched (no garbage)."""
    af = AutoFixer(translator=None)
    body = r"\section{Deep Networks and Training}"
    out, fixes = af._fix_untranslated_heading(body, [_issue("untranslated_heading", 1)],
                                              _find_math_spans(body))
    assert out == body
    # Never produce the old "Derin aglar and training" style garbage
    assert "aglar" not in out.lower()


def test_afc4_no_substring_garbage_span():
    """'Spanning' must not partial-match glossary 'Span' into 'Gerilimning'."""
    af = AutoFixer(translator=None)
    body = r"\section{Spanning Trees}"
    out, fixes = af._fix_untranslated_heading(body, [_issue("untranslated_heading", 1)],
                                              _find_math_spans(body))
    assert out == body


def test_data04_glossary_reuses_canonical_translator_source():
    """Exact-match fallback comes from the proper-Turkish translator glossary."""
    af = AutoFixer(translator=None)
    body = r"\section{Introduction}"
    out, fixes = af._fix_untranslated_heading(body, [_issue("untranslated_heading", 1)],
                                              _find_math_spans(body))
    assert out == r"\section{Giriş}"  # proper Turkish, not ASCII "Giris"


def test_af10_heading_prefix_preserved():
    af = AutoFixer(translator=_GlossaryTranslator())
    body = r"\section{6.3. Introduction}"
    out, fixes = af._fix_untranslated_heading(body, [_issue("untranslated_heading", 1)],
                                              _find_math_spans(body))
    assert out == r"\section{6.3. Giriş}"


def test_autofix_no_local_heading_glossary_attribute():
    """The ASCII-broken local _HEADING_GLOSSARY was deleted (DATA-04)."""
    assert not hasattr(AutoFixer, "_HEADING_GLOSSARY")


# ---------------------------------------------------------------------------
# AF-11: last-resort marker loss keeps original
# ---------------------------------------------------------------------------

def test_af11_context_loss_returns_original():
    class _LoseMarkers:
        # Drops markers and collapses to a single blob -> heuristic fails
        def _safe_translate(self, text):
            return "hepsi tek satir oldu"

    af = AutoFixer(translator=_LoseMarkers())
    original = "This is the target block."
    out = af._retranslate_with_context(original, "Onceki baglam.", "Sonraki baglam.")
    # Must return the ORIGINAL, not context+target+context blob
    assert out == original


# ---------------------------------------------------------------------------
# AF-06: mixed_language_line fallback removed
# ---------------------------------------------------------------------------

def test_af06_mixed_language_no_english_left_untouched():
    """If no English sentence is found, the (false-positive) line is untouched."""
    calls = {"n": 0}

    class _T:
        _SENTENCE_SPLIT_RE = __import__("re").compile(r"(?<=[.!?])\s+")

        def _is_english_sentence(self, s):
            return False  # nothing looks English

        def _get_translator(self):
            calls["n"] += 1
            raise AssertionError("MT must not be called on a false positive")

    # Attach the real splitter used by the code path
    from latex_pipeline.translator import LatexTranslator
    _T._SENTENCE_SPLIT_RE = LatexTranslator._SENTENCE_SPLIT_RE

    af = AutoFixer(translator=_T())
    body = "Bu tamamen Türkçe bir cümledir ve yeterince uzundur burada."
    out, fixes = af._fix_mixed_language_line(body, [_issue("mixed_language_line", 1)],
                                             _find_math_spans(body))
    assert out == body
    assert not fixes
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# YOL A KİLİDİ: DATA-01 / AF-02 / QC-N1 — structural wrong-map guarded
# ---------------------------------------------------------------------------

def test_qcn1_manual_mode_structural_map_disabled():
    """manual_translation=True: yapısal wrong-translation map ÇALIŞMAZ."""
    af = AutoFixer(manual_translation=True)
    body = ("Kurumsal bilgisayar ağı güvenliği önemlidir.\n"
            r"\section{Dikgen Tabanlar}")
    out, fixes = af._apply_structural_fixes(body)
    # Agent-authored legitimate Turkish must survive verbatim
    assert "bilgisayar ağı" in out
    assert "Dikgen Tabanlar" in out
    assert "ortogonal" not in out


def test_qcn1_auto_mode_structural_map_active():
    """manual_translation=False: unambiguous MT error still fixed."""
    af = AutoFixer(manual_translation=False)
    out, fixes = af._apply_wrong_translation_map_always(
        "Bu eylem fonksiyonu kullanılır.")
    assert "aktivasyon fonksiyonu" in out


def test_data01_removed_context_destroying_entries():
    """'bilgisayar ağı', 'üst ses', 'dikgen' map'ten çıkarıldı."""
    m = AutoFixer._WRONG_TRANSLATION_MAP
    assert "bilgisayar ağı" not in m
    assert "üst ses" not in m
    assert "dikgen" not in m


def test_afc6_no_dikgen_ortogonal_conflict():
    """Auto mode: legit 'dikgen' no longer force-rewritten to 'ortogonal'."""
    af = AutoFixer(manual_translation=False)
    out, fixes = af._apply_wrong_translation_map_always(
        "Vektörler dikgen olacak şekilde seçilir.")
    assert "dikgen" in out
    assert "ortogonal" not in out


# ---------------------------------------------------------------------------
# QC-N2: first_person_academic skipped in manual mode
# ---------------------------------------------------------------------------

def test_qcn2_first_person_in_manual_skip_set():
    af = AutoFixer(manual_translation=True)
    # Reach into the dispatcher's skip set by exercising _apply_fixes with a
    # first_person_academic issue; in manual mode the fix must be skipped.
    body = "Bu durumu net görüyorum ve kadınları anlıyorum."
    issue = _issue("first_person_academic", 1, context="")
    out, fixes = af._apply_fixes(body, [issue])
    # Manual mode -> handler skipped -> line untouched, no passive garbage
    assert out == body
    assert not fixes


def test_qcn2_first_person_active_in_auto_mode():
    """Yol B (auto) still runs first_person_academic."""
    af = AutoFixer(manual_translation=False)
    # Should at least attempt (not skipped); we only assert it is NOT force-skipped
    # by checking the skip guard does not apply. Use a benign line.
    body = "Bunu yapıyorum."
    issue = _issue("first_person_academic", 1)
    # No exception, returns a tuple
    out, fixes = af._apply_fixes(body, [issue])
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# QC-N3: antonym_guard word boundary
# ---------------------------------------------------------------------------

def test_qcn3_antonym_no_substring_match():
    af = AutoFixer()
    body = "Tahmin edicinin hatası sonuçları etkiler."
    issue = _issue("antonym_guard", 1, sugg="Replace 'hata' with 'onyargi'")
    out, fixes = af._fix_antonym_guard(body, [issue], _find_math_spans(body))
    # 'hata' inside 'hatası' must NOT be replaced (no 'onyargisı' garbage)
    assert out == body
    assert not fixes


def test_qcn3_antonym_whole_word_replaced():
    af = AutoFixer()
    body = "Buradaki hata büyüktür."
    issue = _issue("antonym_guard", 1, sugg="Replace 'hata' with 'onyargi'")
    out, fixes = af._fix_antonym_guard(body, [issue], _find_math_spans(body))
    assert "onyargi" in out
    assert fixes


# ---------------------------------------------------------------------------
# DATA-05: deasciify cleanup
# ---------------------------------------------------------------------------

def test_data05_no_duplicate_inisi_and_no_pooling():
    patterns = [p.pattern for p, _ in AutoFixer._TURKISH_DEASCIIFY]
    # Duplicate '\bInisi\b' collapsed to one
    assert patterns.count(r"\bInisi\b") == 1
    # 'Pooling' translation removed from the deasciify map
    assert r"\bPooling\b" not in patterns


def test_data05_no_identity_noops():
    # Every deasciify entry must actually transform its input
    for pat, repl in AutoFixer._TURKISH_DEASCIIFY:
        # The pattern is \bWord\b; derive the literal word
        lit = pat.pattern.replace(r"\b", "")
        assert lit != repl, f"identity/no-op deasciify entry: {lit!r}"


def test_data05_suffix_no_op_removed():
    suffixes = [(p.pattern, r) for p, r in AutoFixer._TURKISH_SUFFIX_FIXES]
    assert (r"(\w)mesi\b", r"\1mesi") not in suffixes


# ---------------------------------------------------------------------------
# AF-05: glossary_compliance survives math-adjacent terms (stash protection)
# ---------------------------------------------------------------------------

def test_af05_glossary_compliance_fixes_term_after_math():
    af = AutoFixer()
    # A long inline-math region precedes the wrong term; stash (not stale
    # math_spans) must protect math while still fixing the term.
    body = ("$x + y + z + a + b + c + d + e + f + g + h + i + j$ "
            "Bu Sinir Agi modeli onemlidir.")
    issue = _issue("glossary_compliance", 1,
                   sugg="Replace 'Sinir Agi' with 'Sinir Ağı'")
    out, fixes = af._fix_glossary_compliance(body, [issue], _find_math_spans(body))
    assert "Sinir Ağı" in out
    # Math must be untouched
    assert "$x + y + z + a + b + c + d + e + f + g + h + i + j$" in out


# ---------------------------------------------------------------------------
# AF-09: cross-category line_num staleness after a block replaces M != K lines
# ---------------------------------------------------------------------------

def test_af09_remap_helper_maps_equal_and_deleted_regions():
    """Old->new line map: surviving lines exact, deleted region collapses."""
    old = "\n".join(f"L{i}" for i in range(8))          # L0..L7 (lines 1-8)
    new = "\n".join(["L0", "L3", "L4", "L5", "L6", "L7"])  # L1,L2 deleted
    issues = [_issue("untranslated_heading", n) for n in (1, 5, 8)]
    AutoFixer._remap_issue_line_nums(old, new, issues)
    # L0 stays line 1; old line 5 (L4) -> new line 3; old line 8 (L7) -> line 6
    assert [i.line_num for i in issues] == [1, 3, 6]


def test_af09_remap_helper_insertion_shifts_down():
    """Inserting lines shifts later issues' line_num downward."""
    old = "\n".join(["A", "B", "C"])                 # 3 lines
    new = "\n".join(["A", "X", "Y", "B", "C"])       # 2 lines inserted after A
    iss = _issue("untranslated_heading", 3)          # points at "C"
    AutoFixer._remap_issue_line_nums(old, new, [iss])
    assert iss.line_num == 5                          # "C" now on line 5


def test_af09_remap_helper_out_of_range_untouched():
    iss = _issue("untranslated_heading", 999)
    AutoFixer._remap_issue_line_nums("A\nB", "A\nB\nC", [iss])
    assert iss.line_num == 999                        # left as-is, bound-checked


def test_af09_second_category_targets_correct_line_after_deletion():
    """End-to-end: category A deletes lines, category B (a later, line-based
    handler) must still hit the shifted heading -- not miss it or hit an
    unrelated line."""
    af = AutoFixer(translator=_GlossaryTranslator(), manual_translation=False)
    lines = [
        "Birinci satir metni.",       # 1
        "<<LATEX_1>>",                # 2  placeholder_orphan deletes
        "<<LATEX_2>>",                # 3  placeholder_orphan deletes
        "",                           # 4
        "Ara metin.",                 # 5
        r"\section{Introduction}",    # 6  heading, shifts up after deletes
        "Son metin.",                 # 7
    ]
    body = "\n".join(lines)
    issues = [
        _issue("placeholder_orphans", 2),
        _issue("placeholder_orphans", 3),
        _issue("untranslated_heading", 6),  # stale after A deletes lines 2,3
    ]
    fixed, fixes = af._apply_fixes(body, issues)
    # Heading was found at the REMAPPED line and translated.
    assert "Giriş" in fixed
    assert "Introduction" not in fixed
    # The recorded fix line reflects the post-deletion position.
    head_fix = [f for f in fixes if f.check_name == "untranslated_heading"]
    assert head_fix and head_fix[0].line_number == 4


def test_af09_no_line_count_change_leaves_line_nums_untouched():
    """When a category edits in place (no newline delta), later issues keep
    their original line_num (remap must not run / must be a no-op)."""
    af = AutoFixer(translator=_GlossaryTranslator(), manual_translation=False)
    body = "\n".join([
        "Ilk paragraf yeterince uzun bir metin.",  # 1
        r"\section{Conclusion}",                    # 2  heading
    ])
    later = _issue("untranslated_heading", 2)
    # caption_inconsistency edits in place (no newline change); it sorts before
    # untranslated_heading so it is processed first.
    issues = [_issue("caption_inconsistency", 1), later]
    fixed, _ = af._apply_fixes(body, issues)
    assert later.line_num == 2  # unchanged
    assert "Sonuç" in fixed     # heading still correctly translated


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
