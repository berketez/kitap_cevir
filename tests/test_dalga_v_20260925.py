"""Dalga sonu denetçisi (V, 25 Eylül 2026): kulvarlar arası sözleşme bekçileri.

Denetimde bulunan iki kusurun regresyonu ve sözleşmelerin iki ucunu
birbirine bağlayan testler:

  - "Sabitler tek yerde": 2_derle.py EPUB atlama notunun adını
    (EPUB_ATLANDI.txt) ve QC raporunun adını (qc_report.txt) kendi
    literaliyle tutuyordu; yazan modül (latex_pipeline/epub_output.py,
    translate_book.py) adı değiştirirse okuyucu sessizce null dönerdi.
    Artık adlar yazan modülden içe aktarılır.
  - S2 / S4: app.py'nin 1_cikar ve 2_derle --json sonucundan okuduğu her
    alan, o betiğin son satırında gerçekten bulunan alanlardandır (bir
    tarafta alan adı değişirse arayüz sessizce "yok" görmesin).

CLI koşturulmaz; alan kümeleri betiklerin kendi sabitlerinden ve
_print_json_line çıktısından okunur, app.py'nin okuduğu alanlar AST'den.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
from pathlib import Path

import translate_book as tb
from latex_pipeline import epub_output as eo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP = PROJECT_ROOT / "app.py"
derle = importlib.import_module("2_derle")


def _load_cikar():
    spec = importlib.util.spec_from_file_location(
        "cikar_dalga_v", PROJECT_ROOT / "1_cikar.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module_assigns(path: Path) -> set[str]:
    """Modül düzeyinde atanan adlar."""
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _result_keys(*functions: str) -> set[str]:
    """app.py'de verilen fonksiyonların ``result`` sözlüğünden okuduğu alanlar
    (``result.get("x")`` ve ``result["x"]``)."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    wanted = {node.name: node for node in tree.body
              if isinstance(node, ast.FunctionDef) and node.name in functions}
    assert set(wanted) == set(functions), set(functions) - set(wanted)
    keys = set()
    for func in wanted.values():
        for node in ast.walk(func):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "result"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                keys.add(node.args[0].value)
            elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                    and node.value.id == "result"
                    and isinstance(node.slice, ast.Constant)):
                keys.add(node.slice.value)
    return keys


class TestTekKaynak:
    def test_epub_notu_adi_yazan_modulden(self):
        assert derle.EPUB_SKIP_NOTE_NAME is eo.EPUB_SKIP_NOTE_NAME
        assert "EPUB_SKIP_NOTE_NAME" not in _module_assigns(PROJECT_ROOT / "2_derle.py")

    def test_qc_raporu_adi_yazan_modulden(self):
        assert derle.QC_REPORT_NAME is tb.QC_REPORT_NAME
        assert "QC_REPORT_NAME" not in _module_assigns(PROJECT_ROOT / "2_derle.py")
        # Yazan taraf da adı yalnız sabitten kurar.
        source = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        assert source.count('"qc_report.txt"') == 1


class TestSozlesmeAlanlari:
    def _derle_fields(self, capsys) -> set[str]:
        derle._print_json_line(True)
        return set(json.loads(capsys.readouterr().out.strip().splitlines()[-1]))

    def test_s4_derle_json_alanlari(self, capsys):
        fields = self._derle_fields(capsys)
        assert {"qc_score", "qc_report", "qc_top", "epub", "epub_note"} <= fields

    def test_arayuzun_derleme_sonucundan_okudugu_alanlar_var(self, capsys):
        fields = self._derle_fields(capsys)
        read = _result_keys("_result", "_qc_score", "_qc_details",
                            "_epub_actions", "_log_hint")
        assert read, "app.py derleme sonucundan hiç alan okumuyor mu?"
        assert read <= fields, read - fields

    def test_arayuzun_cikarim_sonucundan_okudugu_alanlar_var(self):
        fields = set(_load_cikar()._JSON_FIELDS)
        assert {"extraction_reused", "export_tr_exists", "chapters"} <= fields  # S2
        read = _result_keys("_extract_result", "_log_hint")
        assert read and read <= fields, read - fields
