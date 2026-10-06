"""Kitap sözlüğü: V2 doğrulamasının Y4 ve Y5 bulguları (5 Ekim 2026)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline import kitap_sozlugu as ks  # noqa: E402


def _sozluk(kaynak, hedef, dil="de"):
    s = ks.KitapSozlugu(kaynak_dil=dil)
    s._girdiler = [ks.SozlukGirdisi(kaynak=kaynak, hedef=hedef, kaynak_turu=ks.KAYNAK_MODEL)]
    return s


def test_y5_sekil_etiketi_aday_olmaz():
    govde = ("Die Unruh schwingt periodisch, siehe Fig. 2.1 und Fig. 2.4. "
             "Die Unruh ist gekoppelt, wie Fig. 3.2 zeigt. Fig. 3.3 zeigt die Unruh.\n\n") * 3
    adaylar = [a.kaynak.lower() if hasattr(a, "kaynak") else str(a).lower()
               for a in ks.aday_terimler(govde, "de")]
    assert not any(a in ("fig", "fig.") or a.startswith("fig ") for a in adaylar)


def test_y4_terim_ardindan_parantezde_karsilik_dokunulmaz():
    s = _sozluk("Raumerfahrung", "mekân deneyimi")
    metin = "Bu bölümde Raumerfahrung (mekân deneyimi) kavramı ayrıntılı olarak ele alınır ve incelenir."
    assert s.verify_terms(metin) == metin
