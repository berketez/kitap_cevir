#!/usr/bin/env bash
# Kilit dosyalarını (cekirdek/pdf/mlx/gguf.txt) girdi/*.in dosyalarından yeniden üretir.
#
# Yöntem:
#   1) Dört grubun birleşimi TEK çözümle çözülür (girdi/tum.txt) → gruplar arası sürümler tutarlı.
#      girdi/kisitlar.txt (mevcut venv'in pip freeze'i, ölü ağırlık çıkarılmış) geçişli bağımlılıkları
#      test edilmiş sürümlere sabitler; girdi/overrides.txt bilinen tek çelişkiyi (transformers) çözer.
#   2) Her grup, birleşimi kısıt alarak kendi TAM kapanışıyla (hash'li) yazılır. Her dosya kendi başına
#      kurulabilir; sıralı kurulumda ortak paketler zaten kurulu olduğundan yeniden inmez.
#
# Hedef: CPython 3.12, macOS arm64, en düşük macOS 14.0 (mlx / mlx-metal tekerlekleri macosx_14_0'dan başlar).
# Kullanım: packaging/requirements/derle_kilit.sh   (UV=<uv yolu> ile farklı uv verilebilir)
# Bu kilitlerin kullanıcıları: kök requirements.txt (-r ile gösterir; geliştirici kurulumu
# 'pip install --no-deps -r requirements.txt'), DMG kurulumu (yönetici, tekerlek_kur) ve sürüm
# betiği (packaging/surum/surum_cikar.py manifesti bu kilitlerden ölçer). Dosya adı değişirse
# üçü de güncellenir (TASARIM F6).
set -euo pipefail

BURASI="$(cd "$(dirname "$0")" && pwd)"
UV="${UV:-uv}"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-14.0}"
ORTAK=(--python-version 3.12 --python-platform aarch64-apple-darwin)

cd "$BURASI/girdi"

"$UV" pip compile cekirdek.in pdf.in mlx.in gguf.in \
    -c kisitlar.txt --overrides overrides.txt "${ORTAK[@]}" \
    --no-annotate --custom-compile-command "packaging/requirements/derle_kilit.sh" \
    -o tum.txt

for grup in cekirdek pdf mlx gguf; do
    "$UV" pip compile "$grup.in" -c tum.txt --overrides overrides.txt "${ORTAK[@]}" \
        --generate-hashes --custom-compile-command "packaging/requirements/derle_kilit.sh" \
        -o "../$grup.txt"
done
echo "Kilit dosyaları yazıldı: $BURASI/{cekirdek,pdf,mlx,gguf}.txt"
