#!/bin/bash
# Kitapçevir kurulum prototipi (29 Eylül 2026) — uygulama içi kurulum sihirbazının arka planda
# çalıştıracağı adımların betik hâli. Her adım İDEMPOTENT: zaten tamam olan adım atlanır.
# Her adım GERÇEK ölçüm yazar: süre (sn), ağ arabiriminden alınan bayt, kök dizinde disk farkı (KiB).
# Ölçümler <kök>/durum/olcumler.jsonl dosyasına eklenir ve ekrana basılır.
#
# Kullanım:
#   packaging/kur_prototip.sh --kok <DİZİN> [seçenekler]
#
#   --kok <DİZİN>        ZORUNLU. Kurulum kökü (ör. sahte "Application Support/Kitapcevir").
#                        Varsayılan YOK: gerçek ~/Library'ye yanlışlıkla kurmayı önler.
#   --uygulama <TAR>     Uygulama kodu arşivi (ör. `git archive HEAD` çıktısı); <kök>/app'e açılır.
#                        Arşivin SHA-256'sı değişmediyse atlanır. Verilmezse bu adım atlanır.
#   --tex tectonic|sistem|yok
#                        tectonic (varsayılan): resmî tectonic ikilisi + xelatex sarmalayıcısı + ısıtma.
#                        sistem: makinedeki TeX Live/MacTeX xelatex'i algılanır, hiçbir şey kurulmaz.
#   --gguf               İsteğe bağlı GGUF desteği (llama-cpp-python, Metal derlemesi; Xcode KSA gerekir).
#   --marker-atla        Marker modellerini indirme (ilk PDF çıkarımında inerler).
#   --isitma-atla        tectonic paket önbelleğini ısıtma derlemesiyle doldurma.
#
# Ağ: yalnız resmî kaynaklar (GitHub release: astral-sh/python-build-standalone, astral-sh/uv,
# tectonic-typesetting/tectonic; PyPI; models.datalab.to). Her ikili SHA-256 ile doğrulanır.
# Betik boru hattı (curl | sh) KULLANILMAZ.
#
# macOS /bin/bash 3.2 ile çalışır (boş dizi + set -u tuzağından kaçınıldı); sistem python3'ü
# KULLANILMAZ (temiz Mac'te /usr/bin/python3 Xcode KSA kurulum penceresini tetikler).
set -eo pipefail

# ---- Sabitler (sürüm çıkarken güncellenir; SHA-256'lar GitHub release sayfasından) -------------
PBS_SURUM="20260924"
PY_SURUM="3.12.14"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_SURUM}/cpython-${PY_SURUM}%2B${PBS_SURUM}-aarch64-apple-darwin-install_only.tar.gz"
PY_SHA="9763f43db2481a6af36af82ec40302aab7a73632f880129d07a6e81aec846277"
UV_SURUM="0.12.20"
UV_URL="https://github.com/astral-sh/uv/releases/download/${UV_SURUM}/uv-aarch64-apple-darwin.tar.gz"
UV_SHA="848fdeb602ff1a1baacd4f6c8b7bdc6cf1ad026a6d9cf59475fda17c179743ca"
TECTONIC_SURUM="0.17.0"
TECTONIC_URL="https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_SURUM}/tectonic-${TECTONIC_SURUM}-aarch64-apple-darwin.tar.gz"
TECTONIC_SHA="a3f1cac7c5678f01661a92212f58480ae3b0634115d880dbc59e2953ded45667"

PAKET_DIZINI="$(cd "$(dirname "$0")" && pwd)"
REQ="$PAKET_DIZINI/requirements"

# ---- Argümanlar ---------------------------------------------------------------------------------
KOK=""; UYGULAMA_TAR=""; TEX_TURU="tectonic"; GGUF=0; MARKER=1; ISITMA=1
while [ $# -gt 0 ]; do
    case "$1" in
        --kok) KOK="$2"; shift 2 ;;
        --uygulama) UYGULAMA_TAR="$2"; shift 2 ;;
        --tex) TEX_TURU="$2"; shift 2 ;;
        --gguf) GGUF=1; shift ;;
        --marker-atla) MARKER=0; shift ;;
        --isitma-atla) ISITMA=0; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "Bilinmeyen seçenek: $1" >&2; exit 2 ;;
    esac
done
[ -n "$KOK" ] || { echo "HATA: --kok zorunlu (kurulum kökü)." >&2; exit 2; }
case "$TEX_TURU" in tectonic|sistem|yok) ;; *) echo "HATA: --tex tectonic|sistem|yok" >&2; exit 2 ;; esac

# ---- Platform kapısı ----------------------------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] || {
    echo "HATA: yalnız macOS + Apple Silicon (arm64) desteklenir." >&2; exit 3; }
MACOS_ANA="$(sw_vers -productVersion | cut -d. -f1)"
[ "$MACOS_ANA" -ge 14 ] || {
    echo "HATA: macOS 14 (Sonoma) ya da üstü gerekir (MLX tekerlekleri macOS 14'ten başlar); bu Mac: $(sw_vers -productVersion)." >&2; exit 3; }

mkdir -p "$KOK/durum" "$KOK/indirilen"
KOK="$(cd "$KOK" && pwd)"
OLCUM="$KOK/durum/olcumler.jsonl"
ARABIRIM="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
[ -n "$ARABIRIM" ] || ARABIRIM="en0"

ag_bayt() { netstat -ib -I "$ARABIRIM" 2>/dev/null | awk 'NR==2{print $7}'; }
disk_kib() { du -sk "$KOK" 2>/dev/null | awk '{print $1}'; }

# adim <ad> <komut...> : komutu çalıştırır, ölçer, JSON satırı yazar. Komut hata verirse betik durur.
adim() {
    local ad="$1"; shift
    local n0 d0 t0 n1 d1 t1 rc
    n0="$(ag_bayt)"; d0="$(disk_kib)"; t0="$(date +%s)"
    echo "==> $ad"
    set +e
    ( set -e; "$@" ) > "$KOK/durum/gunluk_$ad.txt" 2>&1   # alt kabukta errexit: adım içindeki ilk hata adımı durdurur
    rc=$?
    set -e
    n1="$(ag_bayt)"; d1="$(disk_kib)"; t1="$(date +%s)"
    local satir="{\"adim\":\"$ad\",\"rc\":$rc,\"sure_sn\":$((t1 - t0)),\"ag_alinan_bayt\":$((n1 - n0)),\"disk_kib_fark\":$((d1 - d0)),\"zaman\":\"$(date +%Y-%m-%dT%H:%M:%S)\",\"arabirim\":\"$ARABIRIM\"}"
    echo "$satir" >> "$OLCUM"
    echo "    $satir"
    if [ "$rc" -ne 0 ]; then
        echo "HATA: '$ad' adımı başarısız (çıkış $rc). Günlük: $KOK/durum/gunluk_$ad.txt" >&2
        tail -20 "$KOK/durum/gunluk_$ad.txt" >&2
        exit "$rc"
    fi
}

sha_dogrula() {  # <dosya> <beklenen>
    local gercek
    gercek="$(shasum -a 256 "$1" | awk '{print $1}')"
    [ "$gercek" = "$2" ] || { echo "SHA-256 UYUŞMUYOR: $1 ($gercek != $2)"; return 1; }
}

indir() {  # <url> <hedef> <sha>
    if [ -f "$2" ] && sha_dogrula "$2" "$3" >/dev/null 2>&1; then
        echo "zaten indirilmiş: $2"; return 0
    fi
    curl -fL --retry 3 --proto '=https' --tlsv1.2 -o "$2.part" -w 'indirilen_bayt=%{size_download} sure=%{time_total}\n' "$1"
    mv "$2.part" "$2"
    sha_dogrula "$2" "$3"
}

# ---- 1. Python (python-build-standalone) ---------------------------------------------------------
python_kur() {
    indir "$PY_URL" "$KOK/indirilen/cpython-${PY_SURUM}.tar.gz" "$PY_SHA"
    rm -rf "$KOK/python.yeni"; mkdir -p "$KOK/python.yeni"
    tar -xzf "$KOK/indirilen/cpython-${PY_SURUM}.tar.gz" -C "$KOK/python.yeni"
    rm -rf "$KOK/python"; mv "$KOK/python.yeni/python" "$KOK/python"; rmdir "$KOK/python.yeni"
    "$KOK/python/bin/python3" -c 'import sys, ssl; print(sys.version); print(ssl.OPENSSL_VERSION)'
}
if [ -x "$KOK/python/bin/python3" ] && [ "$("$KOK/python/bin/python3" -c 'import platform; print(platform.python_version())' 2>/dev/null)" = "$PY_SURUM" ]; then
    echo "==> python: $PY_SURUM kurulu, atlandı"
else
    adim python python_kur
fi

# ---- 2. uv (resmî ikili) -------------------------------------------------------------------------
uv_kur() {
    indir "$UV_URL" "$KOK/indirilen/uv-${UV_SURUM}.tar.gz" "$UV_SHA"
    rm -rf "$KOK/indirilen/uv-ac"; mkdir -p "$KOK/indirilen/uv-ac" "$KOK/bin"
    tar -xzf "$KOK/indirilen/uv-${UV_SURUM}.tar.gz" -C "$KOK/indirilen/uv-ac"
    cp "$KOK/indirilen/uv-ac/uv-aarch64-apple-darwin/uv" "$KOK/bin/uv"
    rm -rf "$KOK/indirilen/uv-ac"
    "$KOK/bin/uv" --version
}
if [ -x "$KOK/bin/uv" ] && [ "$("$KOK/bin/uv" --version 2>/dev/null | awk '{print $2}')" = "$UV_SURUM" ]; then
    echo "==> uv: $UV_SURUM kurulu, atlandı"
else
    adim uv uv_kur
fi
UV="$KOK/bin/uv"
export UV_NO_CACHE=1           # önbellek kökte birikmesin (ölçüm: gguf derlemesi önbelleği 420 MB)
export UV_PYTHON_DOWNLOADS=never

# ---- 3. Uygulama kodu ----------------------------------------------------------------------------
if [ -n "$UYGULAMA_TAR" ]; then
    YENI_SHA="$(shasum -a 256 "$UYGULAMA_TAR" | awk '{print $1}')"
    if [ -f "$KOK/durum/uygulama.sha256" ] && [ "$(cat "$KOK/durum/uygulama.sha256")" = "$YENI_SHA" ]; then
        echo "==> uygulama: aynı arşiv kurulu, atlandı"
    else
        uygulama_kur() {
            # Kullanıcı verisi (output/, models/, data/*.db, config/*.json) KORUNUR: yalnız kod dosyaları yazılır.
            mkdir -p "$KOK/app"
            tar -xf "$UYGULAMA_TAR" -C "$KOK/app" --exclude='tests' --exclude='.claude'
            echo "$YENI_SHA" > "$KOK/durum/uygulama.sha256"
        }
        adim uygulama uygulama_kur
    fi
fi

# ---- 4. venv -------------------------------------------------------------------------------------
if [ -x "$KOK/venv/bin/python" ] && "$KOK/venv/bin/python" -c 'import sys' 2>/dev/null; then
    echo "==> venv: var, atlandı"
else
    adim venv "$UV" venv --python "$KOK/python/bin/python3" "$KOK/venv"
fi
VPY="$KOK/venv/bin/python"

# ---- 5. Paket grupları ---------------------------------------------------------------------------
grup_kur() {  # <grup>
    "$UV" pip install --python "$VPY" -r "$REQ/$1.txt" --overrides "$REQ/girdi/overrides.txt"
}
grup_dogrula() {  # <grup> → içe aktarma sınaması (model YÜKLEMEZ)
    case "$1" in
        cekirdek) "$VPY" -c 'import streamlit, fitz, ebooklib, bs4, lxml, rapidfuzz, PIL, jinja2, docx, huggingface_hub; print("cekirdek tamam")' ;;
        pdf) "$VPY" -c 'import torch, marker, surya, transformers; print("pdf tamam; torch", torch.__version__, "mps", torch.backends.mps.is_available())' ;;
        mlx) "$VPY" -c 'import mlx.core as mx, mlx_lm; print("mlx tamam;", mx.default_device())' ;;
        gguf) "$VPY" -c 'import llama_cpp; print("gguf tamam; Metal:", llama_cpp.llama_supports_gpu_offload())' ;;
    esac
}
GRUPLAR="cekirdek pdf mlx"
[ "$GGUF" -eq 1 ] && GRUPLAR="$GRUPLAR gguf"
for g in $GRUPLAR; do
    KURU="$("$UV" pip install --python "$VPY" -r "$REQ/$g.txt" --overrides "$REQ/girdi/overrides.txt" --dry-run 2>&1 || true)"
    case "$KURU" in
        *"Would make no changes"*)
            if grup_dogrula "$g" >/dev/null 2>&1; then
                echo "==> $g: kurulu ve kilitle uyumlu, atlandı"
                continue
            fi ;;
    esac
    if [ "$g" = "gguf" ]; then
        # PyPI'da yalnız kaynak paketi var → derleyici gerekir. Temiz Mac'te /usr/bin/clang bir kabuktur.
        if ! KSA="$(xcode-select -p 2>/dev/null)" || [ ! -x "$KSA/usr/bin/clang" ]; then
            echo "HATA: GGUF desteği için Xcode Komut Satırı Araçları gerekir: Terminal'de 'xcode-select --install'." >&2
            exit 4
        fi
        export CMAKE_ARGS="-DGGML_METAL=on"
    fi
    adim "grup_$g" grup_kur "$g"
    grup_dogrula "$g"
done

# ---- 6. TeX ------------------------------------------------------------------------------------
tectonic_kur() {
    indir "$TECTONIC_URL" "$KOK/indirilen/tectonic-${TECTONIC_SURUM}.tar.gz" "$TECTONIC_SHA"
    mkdir -p "$KOK/tex/bin" "$KOK/tex/shim" "$KOK/tex/cache"
    tar -xzf "$KOK/indirilen/tectonic-${TECTONIC_SURUM}.tar.gz" -C "$KOK/tex/bin"
    "$KOK/tex/bin/tectonic" --version
}
tex_isit() {
    local d; d="$(mktemp -d "${TMPDIR:-/tmp}/kitapcevir-isitma.XXXXXX")"
    cp "$PAKET_DIZINI/tex/isitma.tex" "$d/"
    (cd "$d" && TECTONIC_CACHE_DIR="$KOK/tex/cache" "$KOK/tex/bin/tectonic" -X compile --untrusted -Z continue-on-errors isitma.tex 2>&1) | tee "$d/cikti.txt"
    echo "tectonic_indirilen_dosya=$(grep -c 'note: downloading' "$d/cikti.txt" || true)"
    [ -s "$d/isitma.pdf" ] || { echo "ısıtma PDF'i üretilmedi"; return 1; }
    rm -rf "$d"
    touch "$KOK/durum/tex_isitma.tamam"
}
case "$TEX_TURU" in
    tectonic)
        if [ -x "$KOK/tex/bin/tectonic" ] && [ "$("$KOK/tex/bin/tectonic" --version 2>/dev/null)" = "Tectonic $TECTONIC_SURUM" ]; then
            echo "==> tectonic: $TECTONIC_SURUM kurulu, atlandı"
        else
            adim tectonic tectonic_kur
        fi
        mkdir -p "$KOK/tex/shim"
        cp "$PAKET_DIZINI/tex/xelatex" "$KOK/tex/shim/xelatex"; chmod +x "$KOK/tex/shim/xelatex"
        echo "{\"tur\":\"tectonic\",\"xelatex\":\"$KOK/tex/shim/xelatex\"}" > "$KOK/durum/tex.json"
        if [ "$ISITMA" -eq 1 ]; then
            if [ -f "$KOK/durum/tex_isitma.tamam" ] && [ -n "$(ls -A "$KOK/tex/cache/formats" 2>/dev/null)" ]; then
                echo "==> tex_isitma: önbellek dolu, atlandı"
            else
                adim tex_isitma tex_isit
            fi
        fi
        ;;
    sistem)
        XE=""
        for aday in /Library/TeX/texbin/xelatex /usr/local/texlive/*/bin/universal-darwin/xelatex; do
            [ -x "$aday" ] && { XE="$aday"; break; }
        done
        [ -n "$XE" ] || { echo "HATA: makinede xelatex (TeX Live/MacTeX) bulunamadı." >&2; exit 5; }
        echo "==> tex: kurulu olan kullanılacak: $XE ($("$XE" --version | head -1))"
        echo "{\"tur\":\"sistem\",\"xelatex\":\"$XE\"}" > "$KOK/durum/tex.json"
        ;;
    yok) echo "==> tex: atlandı (--tex yok)" ;;
esac

# ---- 7. Marker modelleri -------------------------------------------------------------------------
if [ "$MARKER" -eq 1 ]; then
    mkdir -p "$KOK/modeller/datalab"
    # surya, zaten tam olan modeli (manifest'teki tüm dosyalar yerinde) yeniden indirmez.
    adim marker_modelleri env MODEL_CACHE_DIR="$KOK/modeller/datalab" "$VPY" "$PAKET_DIZINI/marker_modelleri.py"
    grep '^{' "$KOK/durum/gunluk_marker_modelleri.txt" || true
fi

# ---- 8. İndirilen arşivleri sil (ölçüm: ~63 MB; kurulu sürüm kontrolü arşive bakmaz) ----------------
rm -f "$KOK/indirilen/"*.tar.gz "$KOK/indirilen/"*.part 2>/dev/null || true

# ---- 9. Özet -------------------------------------------------------------------------------------
echo
echo "Kurulum kökü: $KOK"
for p in python bin venv tex modeller app; do
    [ -e "$KOK/$p" ] && printf '  %-9s %10s KiB\n' "$p" "$(du -sk "$KOK/$p" | awk '{print $1}')"
done
printf '  %-9s %10s KiB\n' "TOPLAM" "$(du -sk "$KOK" | awk '{print $1}')"
echo "Ölçümler: $OLCUM"
