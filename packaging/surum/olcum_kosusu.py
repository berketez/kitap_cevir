"""Sürüm betiğinin ölçüm koşusu (packaging/TASARIM.md C.1-3, G.1-7).

``surum_cikar.py`` çağırır; DMG'deki taşınabilir Python'la, sahte HOME ile koşar
(yalnız stdlib; kurulum yöneticinin kendi ``kurulum.Kurulum`` sınıfıyla yapılır)::

    <kök>/calisma_zamani/python-<etiket>/bin/python3 -I -X utf8 olcum_kosusu.py \\
        --kok <kök> --uygulama <kök>/app/guncel --manifest <geçici manifest> \\
        --esleme <öğe kimliği → önbellek dosyası> --sira cekirdek,mlx,pdf,tex-hafif,gguf \\
        --lisans-kabul pdf --cikti olcum.json --olaylar olaylar.jsonl

Bileşenler SABİT sırayla tek tek kurulur; her birinin diski kurulum öncesi ve sonrası
``du`` farkıdır (yöneticinin ``disk.disk_kullanimi_bayt``'ı, ``du -sk`` ile birebir).
Önce ölçülür, SONRA bileşenin öğeleri sürüm önbelleğinden APFS klonuyla hedefe konur:
geçici öğeler (tekerlekler) kurulumda silindiği için farka girmez, kalıcı öğeler
(modeller, yazıtipi) girer. Ağdan indirme süresi ve baytı burada değil, DMG'den
uçtan uca sınamada (``uctan_uca.py``) ölçülür. tectonic ısıtması gerçek ağla koşar;
indirdiği dosya sayısı ``sayac_beklenen.n`` olur.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def klonla(kaynak: Path, hedef: Path) -> None:
    hedef.parent.mkdir(parents=True, exist_ok=True)
    if hedef.exists():
        hedef.unlink()
    if subprocess.run(["/bin/cp", "-c", str(kaynak), str(hedef)]).returncode != 0:
        shutil.copy2(kaynak, hedef)


def main() -> int:
    ay = argparse.ArgumentParser(description="Kitapçevir sürüm ölçüm koşusu")
    ay.add_argument("--kok", required=True)
    ay.add_argument("--uygulama", required=True)
    ay.add_argument("--manifest", required=True)
    ay.add_argument("--esleme", required=True)
    ay.add_argument("--sira", required=True)
    ay.add_argument("--lisans-kabul", default="")
    ay.add_argument("--cikti", required=True)
    ay.add_argument("--olaylar", required=True)
    a = ay.parse_args()

    uygulama = Path(a.uygulama)
    sys.path.insert(0, str(uygulama / "packaging"))
    from yonetici import disk, durum, kurulum  # noqa: E402 (uygulama kodundan)

    gunluk_dizini = Path(os.environ.get("KITAPCEVIR_GUNLUK_DIZINI") or Path(a.kok) / "gunluk")
    gunluk_dizini.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(gunluk_dizini / "olcum_kosusu.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    veri = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    esleme = json.loads(Path(a.esleme).read_text(encoding="utf-8"))
    kok = Path(a.kok)
    duzen = durum.KokDuzeni(kok)
    kur = kurulum.Kurulum(kok, uygulama, veri,
                          gunluk_yolu=str(gunluk_dizini / "olcum_kurulum.log"))
    for kimlik in [k.strip() for k in a.lisans_kabul.split(",") if k.strip()]:
        kur.lisans_kabul(kimlik)

    olay_dosyasi = open(a.olaylar, "a", encoding="utf-8")
    sonuc = {"bilesenler": {}, "sira": a.sira.split(",")}
    for kimlik in sonuc["sira"]:
        b = kur.bilesen(kimlik)
        once = disk.disk_kullanimi_bayt(kok)
        klon = 0
        for oge in kur.uygun_ogeler(b):
            kaynak = esleme.get(str(oge["kimlik"]))
            if kaynak:
                klonla(Path(kaynak), duzen.kok_yolu(str(oge["hedef"])))
                klon += 1
        izlenen = {"sayac_son": 0, "ag_bayt": {}, "olay": 0}

        def olay(tur, v, _k=kimlik, _i=izlenen):
            _i["olay"] += 1
            olay_dosyasi.write(json.dumps({"bilesen_olcum": _k, "tur": tur, **{
                k: v[k] for k in v if k != "gorunum"}}, ensure_ascii=False, default=str) + "\n")
            if tur == "sayac" and isinstance(v.get("i"), int):
                _i["sayac_son"] = max(_i["sayac_son"], v["i"])
            if tur == "ilerleme" and isinstance(v.get("bayt"), int):
                _i["ag_bayt"][str(v.get("oge"))] = v["bayt"]

        tex = kimlik if b.get("grup") == kurulum.TEX_GRUBU else None
        print(f"  ölçüm: {kimlik} kuruluyor ({klon} öğe önbellekten)", flush=True)
        t0 = time.monotonic()
        kur.calistir(adimlar=[{"bilesen": kimlik}], tex=tex, olay=olay)
        sure = time.monotonic() - t0
        sonra = disk.disk_kullanimi_bayt(kok)
        sonuc["bilesenler"][kimlik] = {
            "disk_bayt": sonra - once, "sure_sn": round(sure, 1), "klonlanan_oge": klon,
            "olay_sayisi": izlenen["olay"], "indirici_bayt": sum(izlenen["ag_bayt"].values()),
            "sayac_son": izlenen["sayac_son"]}
        print(f"  ölçüm: {kimlik} {sonra - once:,} B, {sure:.0f} sn", flush=True)
        if kimlik == "tex-hafif":
            sonuc["tectonic_dosya"] = izlenen["sayac_son"]
            sonuc["tectonic_onbellek_bayt"] = disk.disk_kullanimi_bayt(duzen.tex_cache)
    olay_dosyasi.close()
    sonuc["uygulama_hazir"] = bool(kur.uygulama_hazir())
    sonuc["kok_bayt"] = disk.disk_kullanimi_bayt(kok)
    Path(a.cikti).write_text(json.dumps(sonuc, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if sonuc["uygulama_hazir"] else 2


if __name__ == "__main__":
    sys.exit(main())
