"""
Tests for QC Engine checks 17-21 (new checks from consultant findings).

Each check has at least one test case that verifies detection works.
"""

import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.qc_engine import QCEngine, QCReport


def _get_issues(report: QCReport, check_name: str):
    """Filter report issues by check_name."""
    return [i for i in report.issues if i.check_name == check_name]


# ------------------------------------------------------------------
# Check 17: section_number_consistency
# ------------------------------------------------------------------

def test_section_number_consistency_mismatch():
    """Section numbers differ between EN and TR -> CRITICAL."""
    engine = QCEngine()

    en = r"""
\section{4.4.1 Normal Shock}
\section{4.4.2 Overexpansion}
\subsection{4.4.2.1 Mach Disk}
"""
    tr = r"""
\section{4.4.1 Normal Sok}
\section{4.4.1 Asiri Genisleme}
\subsection{4.4.2.1 Mach Diski}
"""
    # EN has 4.4.2 but TR has 4.4.1 at position 2
    report = engine.analyze(tr, en)
    issues = _get_issues(report, "section_number_consistency")
    assert len(issues) >= 1, f"Expected section_number_consistency issue, got {issues}"
    assert issues[0].severity == "critical"
    print("PASS: test_section_number_consistency_mismatch")


def test_section_number_consistency_ok():
    """Matching section numbers -> no issue."""
    engine = QCEngine()

    en = r"""
\section{3.1 Introduction}
\section{3.2 Methods}
"""
    tr = r"""
\section{3.1 Giris}
\section{3.2 Yontemler}
"""
    report = engine.analyze(tr, en)
    issues = _get_issues(report, "section_number_consistency")
    assert len(issues) == 0, f"Expected no issues, got {issues}"
    print("PASS: test_section_number_consistency_ok")


# ------------------------------------------------------------------
# Check 18: href_preservation
# ------------------------------------------------------------------

def test_href_preservation_large_diff():
    """EN has 5 hrefs, TR has 1 -> HIGH."""
    engine = QCEngine()

    en = r"""
See \href{http://a.com}{A} and \href{http://b.com}{B}.
Also \href{http://c.com}{C}, \href{http://d.com}{D}, \href{http://e.com}{E}.
"""
    tr = r"""
Bakınız \href{http://a.com}{A}.
"""
    report = engine.analyze(tr, en)
    issues = _get_issues(report, "href_preservation")
    assert len(issues) >= 1, f"Expected href_preservation issue, got {issues}"
    assert issues[0].severity in ("high", "critical")
    print("PASS: test_href_preservation_large_diff")


def test_href_preservation_ok():
    """Same href count -> no issue (or low)."""
    engine = QCEngine()

    en = r"\href{http://a.com}{link}"
    tr = r"\href{http://a.com}{baglanti}"

    report = engine.analyze(tr, en)
    issues = _get_issues(report, "href_preservation")
    high_issues = [i for i in issues if i.severity in ("high", "critical")]
    assert len(high_issues) == 0, f"Expected no high/critical, got {high_issues}"
    print("PASS: test_href_preservation_ok")


# ------------------------------------------------------------------
# Check 19: foreign_language_leakage
# ------------------------------------------------------------------

def test_foreign_language_leakage_spanish():
    """Spanish words in Turkish text -> MEDIUM."""
    engine = QCEngine()

    tr = r"""
Bu bolumde como analiz yapilmistir.
Sonuc olarak porque terim kullanilmistir.
Ayrica tiene ve puede kelimeleri gorulmektedir.
"""
    report = engine.analyze(tr)
    issues = _get_issues(report, "foreign_language_leakage")
    assert len(issues) >= 1, f"Expected foreign_language_leakage, got {issues}"
    assert issues[0].severity == "medium"
    print("PASS: test_foreign_language_leakage_spanish")


def test_foreign_language_leakage_clean():
    """Pure Turkish text -> no issue."""
    engine = QCEngine()

    tr = r"""
Bu bolumde analiz yapilmistir.
Sonuc olarak bu terim kullanilmistir.
"""
    report = engine.analyze(tr)
    issues = _get_issues(report, "foreign_language_leakage")
    assert len(issues) == 0, f"Expected no foreign leakage, got {issues}"
    print("PASS: test_foreign_language_leakage_clean")


# ------------------------------------------------------------------
# Check 20: antonym_guard
# ------------------------------------------------------------------

def test_antonym_guard_subsonic_supersonic():
    """EN has 'subsonic' but TR says 'supersonik' -> CRITICAL."""
    engine = QCEngine()

    en = r"In the subsonic region, the flow accelerates through the convergent section."
    tr = r"Supersonik bolgede, akis daralan kesit boyunca hizlanir."

    report = engine.analyze(tr, en)
    issues = _get_issues(report, "antonym_guard")
    assert len(issues) >= 1, f"Expected antonym_guard issue, got {issues}"
    assert issues[0].severity == "critical"
    print("PASS: test_antonym_guard_subsonic_supersonic")


def test_antonym_guard_correct():
    """EN has 'subsonic' and TR correctly says 'ses alti' -> no issue."""
    engine = QCEngine()

    en = r"In the subsonic region, the flow is slow."
    tr = r"Ses alti bolgede, akis yavas."

    report = engine.analyze(tr, en)
    issues = _get_issues(report, "antonym_guard")
    assert len(issues) == 0, f"Expected no antonym issues, got {issues}"
    print("PASS: test_antonym_guard_correct")


def test_antonym_guard_overexpansion():
    """EN 'overexpansion' but TR says 'yetersiz genisleme' -> CRITICAL."""
    engine = QCEngine()

    en = r"\section{3.1 Overexpansion in a Bell Nozzle}"
    tr = r"\section{3.1 Çan Lülesinde Yetersiz Genişleme}"

    report = engine.analyze(tr, en)
    issues = _get_issues(report, "antonym_guard")
    assert len(issues) >= 1, f"Expected antonym_guard issue, got {issues}"
    assert issues[0].severity == "critical"
    print("PASS: test_antonym_guard_overexpansion")


# ------------------------------------------------------------------
# Check 21: first_person_academic
# ------------------------------------------------------------------

def test_first_person_academic_detected():
    """First person verb forms in academic text -> MEDIUM."""
    engine = QCEngine()

    tr = r"""
Bu denklemden anliyorum ki akis hizi artmaktadir.
Sonuc olarak yapacagim hesaplamalar soyledir.
Biliyorum ki bu teorem gecerlidir.
"""
    report = engine.analyze(tr)
    issues = _get_issues(report, "first_person_academic")
    assert len(issues) >= 1, f"Expected first_person_academic, got {issues}"
    assert issues[0].severity == "medium"
    print("PASS: test_first_person_academic_detected")


def test_first_person_academic_clean():
    """Impersonal academic text -> no issue."""
    engine = QCEngine()

    tr = r"""
Bu denklemden akis hizinin arttigi anlasilmaktadir.
Sonuc olarak hesaplamalar asagidaki gibidir.
Bu teoremin gecerli oldugu bilinmektedir.
"""
    report = engine.analyze(tr)
    issues = _get_issues(report, "first_person_academic")
    assert len(issues) == 0, f"Expected no first_person issues, got {issues}"
    print("PASS: test_first_person_academic_clean")


# ------------------------------------------------------------------
# Integration: all 5 new checks run without breaking existing ones
# ------------------------------------------------------------------

def test_all_checks_run():
    """Ensure analyze() runs all 21 checks without error."""
    engine = QCEngine()

    en = r"""
\section{1.1 Introduction}
\section{1.2 Methods}
\href{http://example.com}{Link}
In the subsonic convergent nozzle, the flow is laminar.
"""
    tr = r"""
\section{1.1 Giris}
\section{1.2 Yontemler}
\href{http://example.com}{Baglanti}
Ses alti daralan nozulde, akis laminerdir.
"""
    report = engine.analyze(tr, en)
    assert isinstance(report, QCReport)
    assert 0.0 <= report.score <= 100.0
    print(f"PASS: test_all_checks_run (score={report.score:.0f})")


# ------------------------------------------------------------------
# Run all tests
# ------------------------------------------------------------------

if __name__ == "__main__":
    test_section_number_consistency_mismatch()
    test_section_number_consistency_ok()
    test_href_preservation_large_diff()
    test_href_preservation_ok()
    test_foreign_language_leakage_spanish()
    test_foreign_language_leakage_clean()
    test_antonym_guard_subsonic_supersonic()
    test_antonym_guard_correct()
    test_antonym_guard_overexpansion()
    test_first_person_academic_detected()
    test_first_person_academic_clean()
    test_all_checks_run()
    print("\n=== ALL 12 TESTS PASSED ===")
