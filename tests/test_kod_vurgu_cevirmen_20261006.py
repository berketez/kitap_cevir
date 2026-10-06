"""Yerel model çevirmeni alıntı ortamını ve \\ttfamily'yi korur (6 Ekim 2026).
Cümleler sentetiktir.

Bağlam: dönüştürücü artık düzyazı kod çitini (kod_vurgu) EXPORT.tex/gövdeye
doğrudan "\\begin{quote}\\n\\raggedright\\ttfamily ...\\end{quote}" olarak
basıyor; yerel model çevirmeni quote/quotation ortam satırını ve \\ttfamily'yi
korumuyordu (center/flushleft korunuyor). Model "quote"u çevirirse ortam
bozulur (autofix'in alıntı -> quote eşlemesi yalnız iki yazımı yakalar).
"""
from unittest.mock import MagicMock

from latex_pipeline.config import PipelineConfig
from latex_pipeline.translator import LatexTranslator

_BODY = ("\\chapter{Main Results}\n\nAn introductory sentence that sets the scene here.\n\n"
         "\\begin{quote}\n\\raggedright\\ttfamily \"Should we tidy the shared notes first? "
         "\\textbf{Maybe nobody reads the margins anyway?} Fine, notes it is.\"\n\\end{quote}\n\n"
         "A closing sentence follows the block here.\n")


def _lt(calls):
    lt = LatexTranslator(pipeline_config=PipelineConfig(translate=True, segment_timeout=60),
                         mt_engine="hymt-7b", source_lang="en", target_lang="tr")

    def tr(t):
        calls.append(t)
        return t.replace("quote", "alinti_yapildi")   # ortam adını çeviren model

    bt = MagicMock()
    bt.translate_batch_protected = lambda ts: [tr(t) for t in ts]
    bt.translate_text = lambda t, *a, **k: tr(t)
    lt._translator = bt
    return lt


def test_quote_ortami_ve_ttfamily_modele_gitmez():
    calls = []
    out = _lt(calls).translate(_BODY)
    assert not any("quote" in c or "ttfamily" in c for c in calls), calls
    assert "\\begin{quote}\n\\raggedright\\ttfamily " in out
    assert "\\end{quote}" in out
