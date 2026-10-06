"""Marker/Surya modellerini YÜKLEMEDEN indirir (PROTOTİP, 29 Eylül 2026).

Kurulumun "PDF desteği" bileşeni için: modeller ilk PDF çıkarımında da iner, ama
o zaman kullanıcı ~3,5 dakikayı kitap işlenirken bekler. Bu betik indirmeyi
kuruluma taşır. Modeller surya'nın kendi indiricisiyle (surya.common.s3)
https://models.datalab.to adresinden iner; hedef MODEL_CACHE_DIR ortam
değişkeniyle seçilir (verilmezse surya varsayılanı ~/Library/Caches/datalab/models).

Ayrıca marker'ın ilk çalışmada site-packages/static/fonts altına indirdiği
GoNotoCurrent-Regular.ttf yazıtipini indirir.

Çıktı: her model için tek satır JSON (ad, yerel dizin, bayt, süre, atlandı mı).
Surya zaten tam olan modeli (manifest.json'daki tüm dosyalar yerinde) yeniden indirmez.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main() -> int:
    from surya.settings import settings
    from surya.common.s3 import check_manifest, download_directory

    checkpoints = []
    for name in dir(settings):
        if name.endswith("_MODEL_CHECKPOINT"):
            value = getattr(settings, name)
            if isinstance(value, str) and value.startswith("s3://") and value not in checkpoints:
                checkpoints.append(value)

    toplam = 0
    for ckpt in sorted(checkpoints):
        remote = ckpt.replace("s3://", "")
        local = Path(settings.MODEL_CACHE_DIR) / remote
        local.mkdir(parents=True, exist_ok=True)
        vardi = check_manifest(str(local))
        t0 = time.time()
        download_directory(remote, str(local))
        boyut = _dir_bytes(local)
        toplam += boyut
        print(json.dumps({"model": remote, "dizin": str(local), "bayt": boyut,
                          "sure_sn": round(time.time() - t0, 1), "zaten_vardi": vardi},
                         ensure_ascii=False), flush=True)

    from marker.settings import settings as marker_settings
    from marker.util import download_font

    font = Path(marker_settings.FONT_PATH)
    vardi = font.exists()
    t0 = time.time()
    download_font()
    print(json.dumps({"model": "marker-yazitipi", "dizin": str(font), "bayt": font.stat().st_size,
                      "sure_sn": round(time.time() - t0, 1), "zaten_vardi": vardi},
                     ensure_ascii=False), flush=True)
    print(json.dumps({"toplam_model_bayt": toplam, "model_kok": settings.MODEL_CACHE_DIR},
                     ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
