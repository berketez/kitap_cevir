"""
Layout Detector - DocLayout-YOLO Entegrasyonu

Teknik kitaplardaki sayfa elementlerini tespit eder:
- Basliklar, paragraflar (cevirilecek)
- Formuller, resimler, tablolar (korunacak)

Kullanim:
    detector = LayoutDetector()
    elements = detector.detect_page(pdf_path, page_num)
"""

import os
import numpy as np
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from enum import Enum
import logging

# Lazy imports
fitz = None
cv2 = None
ort = None

logger = logging.getLogger(__name__)

# onnxruntime projenin giriş noktalarından (1_cikar, 2_derle, translate_book,
# arayüz) kullanılmaz ve paketli kurulumda (DMG) bulunmaz (TASARIM F11). Bu
# algılayıcı doğrudan çağrılırsa eksiklik ham ModuleNotFoundError yerine bu
# iletiyle bildirilir; model de indirilmeye kalkılmaz.
ONNXRUNTIME_MISSING_MSG = (
    "onnxruntime kurulu değil: DocLayout-YOLO sayfa düzeni algılayıcısı "
    "(core/layout_detector.py) onu gerektirir. Bu algılayıcı uygulamanın "
    "çeviri yollarında kullanılmaz ve paketli kurulumda (DMG) bulunmaz. "
    "Geliştirici ortamında kurmak için: ./venv/bin/pip install onnxruntime"
)


def _import_onnxruntime():
    """onnxruntime'ı bir kez içe aktar; yoksa açık iletili ImportError."""
    global ort
    if ort is None:
        try:
            import onnxruntime as _ort
        except ImportError as exc:
            raise ImportError(ONNXRUNTIME_MISSING_MSG) from exc
        ort = _ort
    return ort


class ElementType(Enum):
    """Sayfa elementi turleri"""
    TITLE = "title"
    PLAIN_TEXT = "plain_text"
    ABANDONED = "abandoned"  # header, footer, page numbers
    FIGURE = "figure"
    FIGURE_CAPTION = "figure_caption"
    TABLE = "table"
    TABLE_CAPTION = "table_caption"
    TABLE_FOOTNOTE = "table_footnote"
    FORMULA = "isolate_formula"
    FORMULA_CAPTION = "formula_caption"


# Hangi elementler cevirilecek, hangileri korunacak
TRANSLATABLE_TYPES = {
    ElementType.TITLE,
    ElementType.PLAIN_TEXT,
    ElementType.FIGURE_CAPTION,
    ElementType.TABLE_CAPTION,
    ElementType.TABLE_FOOTNOTE,
    ElementType.FORMULA_CAPTION,
}

PRESERVE_TYPES = {
    ElementType.FIGURE,
    ElementType.TABLE,
    ElementType.FORMULA,
    ElementType.ABANDONED,
}


@dataclass
class BoundingBox:
    """Element sinir kutusu"""
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    def to_tuple(self) -> Tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)

    def to_fitz_rect(self):
        """PyMuPDF Rect formatina cevir"""
        global fitz
        if fitz is None:
            import fitz as _fitz
            fitz = _fitz
        return fitz.Rect(self.x1, self.y1, self.x2, self.y2)


@dataclass
class PageElement:
    """Tespit edilen sayfa elementi"""
    element_type: ElementType
    bbox: BoundingBox
    confidence: float
    text: Optional[str] = None  # Metin cikarildiginda doldurulur
    is_code: bool = False  # Kod blogu mu (font veya icerik bazli)
    is_monospace: bool = False  # Monospace font mu
    font_name: Optional[str] = None  # Ana font adi
    font_size: float = 0.0  # Ortalama font boyutu
    is_bold: bool = False  # Bold mu
    is_toc_or_index: bool = False  # Icindekiler veya indeks mi

    @property
    def should_translate(self) -> bool:
        """Bu element cevirilmeli mi?"""
        # Kod bloklari cevirilmez
        if self.is_code:
            return False
        # Kural: İçindekiler'in metin kısmı çevrilir
        # TOC'daki basliklar ve bolum isimleri cevirilir,
        # sadece yapisal elementler (cizgiler, sayfa numaralari) korunur
        if self.is_toc_or_index:
            return self.element_type in TRANSLATABLE_TYPES
        return self.element_type in TRANSLATABLE_TYPES

    @property
    def should_preserve(self) -> bool:
        """Bu element korunmali mi?"""
        if self.is_code:
            return True
        # Kural: İçindekiler'in metin öğeleri korunmaz, çevrilir
        if self.is_toc_or_index:
            return self.element_type in PRESERVE_TYPES
        return self.element_type in PRESERVE_TYPES


class LayoutDetector:
    """
    DocLayout-YOLO tabanli sayfa layout tespit sistemi

    Attributes:
        model_path: ONNX model dosyasi yolu
        confidence_threshold: Minimum guven esigi (varsayilan: 0.25)
        input_size: Model giris boyutu (varsayilan: 1024)
    """

    # DocLayout-YOLO sinif isimleri (model metadata'dan)
    CLASS_NAMES = [
        "title",           # 0
        "plain_text",      # 1
        "abandoned",       # 2
        "figure",          # 3
        "figure_caption",  # 4
        "table",           # 5
        "table_caption",   # 6
        "table_footnote",  # 7
        "isolate_formula", # 8
        "formula_caption", # 9
    ]

    MODEL_REPO = "wybxc/DocLayout-YOLO-DocStructBench-onnx"
    MODEL_FILENAME = "doclayout_yolo_docstructbench_imgsz1024.onnx"

    def __init__(
        self,
        model_path: Optional[str] = None,
        confidence_threshold: float = 0.25,
        input_size: int = 1024,
        device: str = "cpu"  # "cpu" veya "cuda"
    ):
        self.confidence_threshold = confidence_threshold
        self.input_size = input_size
        self.device = device
        self.session = None

        # Model yolunu belirle
        if model_path:
            self.model_path = Path(model_path)
        else:
            self.model_path = self._get_default_model_path()

        # Lazy loading - model ilk kullanimda yuklenecek
        self._model_loaded = False

    def _get_default_model_path(self) -> Path:
        """Varsayılan model yolu: ``<veri kökü>/models/doclayout-yolo/``.

        Veri kökü ``config.paths.veri_koku`` (TASARIM F1): paketli kipte
        ``KITAPCEVIR_VERI_KOKU``, geliştirici kipinde proje kökü (eski yol).
        """
        from config.paths import model_koku

        return (model_koku(Path(__file__).parent.parent) / "doclayout-yolo"
                / self.MODEL_FILENAME)

    def _ensure_model_downloaded(self) -> bool:
        """Modelin indirildiginden emin ol"""
        if self.model_path.exists():
            logger.info(f"Model mevcut: {self.model_path}")
            return True

        logger.info(f"Model indiriliyor: {self.MODEL_REPO}")

        try:
            from huggingface_hub import hf_hub_download

            # Dizini olustur
            self.model_path.parent.mkdir(parents=True, exist_ok=True)

            # Modeli indir
            downloaded_path = hf_hub_download(
                repo_id=self.MODEL_REPO,
                filename=self.MODEL_FILENAME,
                local_dir=self.model_path.parent,
                local_dir_use_symlinks=False
            )

            logger.info(f"Model indirildi: {downloaded_path}")
            return True

        except Exception as e:
            logger.error(f"Model indirme hatasi: {e}")
            return False

    def _load_model(self):
        """ONNX modelini yukle"""
        global ort

        if self._model_loaded:
            return

        # ONNX Runtime önce (F11): yoksa model hiç indirilmeden açık ileti.
        _import_onnxruntime()

        # Modeli indir (gerekirse)
        if not self._ensure_model_downloaded():
            raise RuntimeError("Model indirilemedi")

        # Session olustur - platform'a göre en iyi provider seç
        import platform
        import time

        providers = [('CPUExecutionProvider', {})]  # fallback
        requested_provider = "CPU"

        if self.device == "cuda":
            providers = [
                ('CUDAExecutionProvider', {}),
                ('CPUExecutionProvider', {})
            ]
            requested_provider = "CUDA"
        elif platform.system() == "Darwin":
            # macOS - Apple Silicon için CoreML (GPU + Neural Engine)
            available = ort.get_available_providers()
            logger.info(f"YOLO: Kullanılabilir providers: {available}")

            if 'CoreMLExecutionProvider' in available:
                # CoreML - Apple Silicon için GPU + Neural Engine
                # Not: onnxruntime 1.23+ basit string provider kullanır
                # Custom options çoğu versiyonda desteklenmiyor
                providers = [
                    'CoreMLExecutionProvider',
                    'CPUExecutionProvider'
                ]
                requested_provider = "CoreML (GPU + Neural Engine)"
                logger.info(f"YOLO: CoreML isteniyor (basit mod)")
            else:
                logger.warning("YOLO: CoreML provider bulunamadı!")
                logger.warning("  CoreML için: pip install onnxruntime-silicon veya onnxruntime ile CoreML desteği")

        # Session options - optimizasyonlar
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        # Bellek optimizasyonu
        sess_options.enable_mem_pattern = True
        sess_options.enable_cpu_mem_arena = True

        # Thread ayarları (CPU için)
        import os
        cpu_count = os.cpu_count() or 4
        sess_options.intra_op_num_threads = min(cpu_count, 8)
        sess_options.inter_op_num_threads = min(cpu_count // 2, 4)

        logger.info(f"YOLO: Session oluşturuluyor, istenen provider: {requested_provider}")

        # Model yükleme süresini ölç
        load_start = time.time()

        try:
            self.session = ort.InferenceSession(
                str(self.model_path),
                sess_options=sess_options,
                providers=providers
            )
        except Exception as e:
            logger.error(f"YOLO: Model yükleme hatası: {e}")
            logger.warning("YOLO: CPU fallback deneniyor...")

            # Sadece CPU ile dene
            self.session = ort.InferenceSession(
                str(self.model_path),
                sess_options=sess_options,
                providers=[('CPUExecutionProvider', {})]
            )

        load_time = time.time() - load_start

        # Hangi provider gerçekten kullanıldığını kontrol et
        active_providers = self.session.get_providers()

        self._model_loaded = True
        logger.info(f"YOLO: Model yüklendi ({load_time:.2f}s): {self.model_path}")
        logger.info(f"YOLO: Aktif providers: {active_providers}")

        # CoreML fallback diagnostiği
        if requested_provider.startswith("CoreML") and 'CoreMLExecutionProvider' not in active_providers:
            logger.warning("=" * 60)
            logger.warning("YOLO: CoreML AKTIF DEGIL - CPU'ya düşüldü!")
            logger.warning("Olası nedenler:")
            logger.warning("  1. Model CoreML tarafından desteklenmiyor olabilir")
            logger.warning("  2. Bazı operatörler CoreML'de mevcut değil")
            logger.warning("  3. macOS versiyonu çok eski (12+ gerekli)")
            logger.warning("")
            logger.warning("Performans iyileştirme önerileri:")
            logger.warning("  1. ONNX model optimizasyonu: python -m onnxruntime.transformers.optimizer")
            logger.warning("  2. Model quantization: onnxruntime-tools ile INT8 quantization")
            logger.warning("  3. Input boyutunu küçült (1024 -> 640)")
            logger.warning("=" * 60)
        elif 'CoreMLExecutionProvider' in active_providers:
            logger.info("YOLO: CoreML aktif - Apple Neural Engine + GPU kullanılacak")

    def _preprocess_image(self, image: np.ndarray) -> Tuple[np.ndarray, float, Tuple[int, int]]:
        """
        Goruntuyu model icin hazirla

        Args:
            image: BGR formatinda goruntu (OpenCV)

        Returns:
            preprocessed: Model girdisi
            scale: Olcekleme faktoru
            padding: (pad_w, pad_h) eklenen bosluk
        """
        global cv2
        if cv2 is None:
            import cv2 as _cv2
            cv2 = _cv2

        h, w = image.shape[:2]

        # En-boy oranini koruyarak yeniden boyutlandir
        scale = min(self.input_size / w, self.input_size / h)
        new_w = int(w * scale)
        new_h = int(h * scale)

        resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        # Padding ekle (32'nin kati olacak sekilde)
        pad_w = (self.input_size - new_w) // 2
        pad_h = (self.input_size - new_h) // 2

        padded = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        padded[pad_h:pad_h + new_h, pad_w:pad_w + new_w] = resized

        # Resized artık gerekli değil - bellek tasarrufu
        del resized

        # Normalize ve transpose (in-place operations where possible)
        blob = padded.astype(np.float32)
        blob /= 255.0  # in-place division
        blob = blob.transpose(2, 0, 1)  # HWC -> CHW
        blob = np.expand_dims(blob, 0)  # Batch dimension

        # Padded artık gerekli değil
        del padded

        return blob, scale, (pad_w, pad_h)

    def _postprocess_detections(
        self,
        outputs: np.ndarray,
        scale: float,
        padding: Tuple[int, int],
        original_size: Tuple[int, int]
    ) -> List[PageElement]:
        """
        Model ciktisini isle ve PageElement listesine cevir

        Args:
            outputs: Model raw ciktisi
            scale: Preprocessing'de kullanilan olcek
            padding: Preprocessing'de eklenen padding
            original_size: Orijinal goruntu boyutu (w, h)

        Returns:
            Tespit edilen elementlerin listesi
        """
        elements = []
        pad_w, pad_h = padding
        orig_w, orig_h = original_size

        # Output format: [batch, num_detections, 6]
        # Her detection: [x1, y1, x2, y2, confidence, class_id]
        detections = outputs[0]

        for det in detections:
            if len(det) < 6:
                continue

            x1_raw, y1_raw, x2_raw, y2_raw, confidence, class_id = det[:6]
            class_id = int(class_id)

            # Confidence check
            if confidence < self.confidence_threshold:
                continue

            # Padding'i cikar ve olcekle
            x1 = (x1_raw - pad_w) / scale
            y1 = (y1_raw - pad_h) / scale
            x2 = (x2_raw - pad_w) / scale
            y2 = (y2_raw - pad_h) / scale

            # Sinirlari kontrol et
            x1 = max(0, min(x1, orig_w))
            y1 = max(0, min(y1, orig_h))
            x2 = max(0, min(x2, orig_w))
            y2 = max(0, min(y2, orig_h))

            # Gecersiz kutulari atla
            if x2 <= x1 or y2 <= y1:
                continue

            # Element turunu belirle
            if class_id < len(self.CLASS_NAMES):
                class_name = self.CLASS_NAMES[class_id]
                try:
                    element_type = ElementType(class_name)
                except ValueError:
                    logger.warning(f"Bilinmeyen sinif: {class_name}")
                    continue
            else:
                logger.warning(f"Gecersiz sinif ID: {class_id}")
                continue

            element = PageElement(
                element_type=element_type,
                bbox=BoundingBox(x1, y1, x2, y2),
                confidence=float(confidence)
            )
            elements.append(element)

        # Confidence'a gore sirala
        elements.sort(key=lambda e: e.confidence, reverse=True)

        return elements

    def detect_from_image(self, image: np.ndarray) -> List[PageElement]:
        """
        Goruntuden layout elementlerini tespit et

        Args:
            image: BGR formatinda goruntu (OpenCV/numpy)

        Returns:
            Tespit edilen elementlerin listesi
        """
        self._load_model()

        h, w = image.shape[:2]

        # Preprocess
        blob, scale, padding = self._preprocess_image(image)

        # Inference - CoreML runtime hatası olursa CPU'ya düş
        input_name = self.session.get_inputs()[0].name
        try:
            outputs = self.session.run(None, {input_name: blob})
        except Exception as e:
            if 'CoreMLExecutionProvider' in self.session.get_providers():
                logger.warning(f"YOLO: CoreML runtime hatası: {e}")
                logger.warning("YOLO: CPU'ya geçiliyor...")
                # CPU-only session oluştur
                import onnxruntime as ort
                sess_options = ort.SessionOptions()
                sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                self.session = ort.InferenceSession(
                    str(self.model_path),
                    sess_options=sess_options,
                    providers=['CPUExecutionProvider']
                )
                logger.info(f"YOLO: CPU session aktif: {self.session.get_providers()}")
                # Tekrar dene
                input_name = self.session.get_inputs()[0].name
                outputs = self.session.run(None, {input_name: blob})
            else:
                raise

        # Postprocess
        elements = self._postprocess_detections(
            outputs[0], scale, padding, (w, h)
        )

        return elements

    def detect_page(self, pdf_path: str, page_num: int, dpi: int = 150) -> List[PageElement]:
        """
        PDF sayfasindan layout elementlerini tespit et

        Args:
            pdf_path: PDF dosya yolu
            page_num: Sayfa numarasi (0-indexed)
            dpi: Render DPI (varsayilan: 150)

        Returns:
            Tespit edilen elementlerin listesi
        """
        global fitz, cv2

        if fitz is None:
            import fitz as _fitz
            fitz = _fitz
        if cv2 is None:
            import cv2 as _cv2
            cv2 = _cv2

        # PDF'yi ac ve sayfayi render et
        doc = fitz.open(pdf_path)

        if page_num >= len(doc):
            raise ValueError(f"Sayfa {page_num} mevcut degil (toplam {len(doc)} sayfa)")

        page = doc[page_num]

        # Sayfayi goruntiye cevir
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)

        # Numpy array'e cevir
        image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )

        # RGB -> BGR (OpenCV formati)
        if pix.n == 4:  # RGBA
            image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
        elif pix.n == 3:  # RGB
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

        doc.close()

        # Layout tespiti yap
        elements = self.detect_from_image(image)

        # Koordinatlari PDF koordinatlarina cevir
        scale = 72 / dpi
        for element in elements:
            element.bbox.x1 *= scale
            element.bbox.y1 *= scale
            element.bbox.x2 *= scale
            element.bbox.y2 *= scale

        return elements

    def detect_all_pages(
        self,
        pdf_path: str,
        start_page: int = 0,
        end_page: Optional[int] = None,
        dpi: int = 150,
        progress_callback: Optional[callable] = None
    ) -> Dict[int, List[PageElement]]:
        """
        PDF'nin tum sayfalarini tara

        Args:
            pdf_path: PDF dosya yolu
            start_page: Baslangic sayfasi
            end_page: Bitis sayfasi (None = son sayfa)
            dpi: Render DPI
            progress_callback: Ilerleme callback'i (page_num, total)

        Returns:
            {sayfa_no: [elements]} sozlugu
        """
        global fitz
        if fitz is None:
            import fitz as _fitz
            fitz = _fitz

        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        doc.close()

        if end_page is None:
            end_page = total_pages

        results = {}

        for page_num in range(start_page, min(end_page, total_pages)):
            elements = self.detect_page(pdf_path, page_num, dpi)
            results[page_num] = elements

            if progress_callback:
                progress_callback(page_num + 1, end_page - start_page)

        return results

    def visualize_detections(
        self,
        image: np.ndarray,
        elements: List[PageElement],
        output_path: Optional[str] = None
    ) -> np.ndarray:
        """
        Tespitleri goruntu uzerine ciz

        Args:
            image: BGR goruntu
            elements: Tespit edilen elementler
            output_path: Kaydedilecek dosya yolu (opsiyonel)

        Returns:
            Annotated goruntu
        """
        global cv2
        if cv2 is None:
            import cv2 as _cv2
            cv2 = _cv2

        # Renk haritasi
        colors = {
            ElementType.TITLE: (0, 255, 0),           # Yesil
            ElementType.PLAIN_TEXT: (255, 0, 0),      # Mavi
            ElementType.ABANDONED: (128, 128, 128),   # Gri
            ElementType.FIGURE: (0, 165, 255),        # Turuncu
            ElementType.FIGURE_CAPTION: (0, 255, 255),# Sari
            ElementType.TABLE: (255, 0, 255),         # Mor
            ElementType.TABLE_CAPTION: (255, 128, 255),
            ElementType.TABLE_FOOTNOTE: (255, 200, 255),
            ElementType.FORMULA: (0, 0, 255),         # Kirmizi
            ElementType.FORMULA_CAPTION: (100, 100, 255),
        }

        annotated = image.copy()

        for element in elements:
            color = colors.get(element.element_type, (0, 255, 0))
            bbox = element.bbox

            # Kutu ciz
            cv2.rectangle(
                annotated,
                (int(bbox.x1), int(bbox.y1)),
                (int(bbox.x2), int(bbox.y2)),
                color,
                2
            )

            # Etiket yaz
            label = f"{element.element_type.value} ({element.confidence:.2f})"
            cv2.putText(
                annotated,
                label,
                (int(bbox.x1), int(bbox.y1) - 5),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1
            )

        if output_path:
            cv2.imwrite(output_path, annotated)

        return annotated

    def get_translatable_elements(self, elements: List[PageElement]) -> List[PageElement]:
        """Cevirilecek elementleri filtrele"""
        return [e for e in elements if e.should_translate]

    def get_preserved_elements(self, elements: List[PageElement]) -> List[PageElement]:
        """Korunacak elementleri filtrele"""
        return [e for e in elements if e.should_preserve]

    def extract_table_cells(
        self,
        page,
        table_bbox: Tuple[float, float, float, float],
        dpi: int = 150
    ) -> List[Dict]:
        """
        Tablo hücrelerini ayrı segment olarak çıkar.

        PyMuPDF'nin find_tables() API'sini kullanarak tablo yapısını analiz eder,
        her hücrenin bbox ve text içeriğini döndürür.

        Args:
            page: PyMuPDF page objesi
            table_bbox: Tablo bounding box (x1, y1, x2, y2)
            dpi: Render DPI (koordinat dönüşümü için)

        Returns:
            List[Dict]: Her hücre için:
                - bbox: (x1, y1, x2, y2)
                - text: Hücre metni
                - row: Satır indeksi
                - col: Sütun indeksi
                - is_header: Başlık satırı mı
        """
        global fitz
        if fitz is None:
            import fitz as _fitz
            fitz = _fitz

        cells = []

        try:
            # PyMuPDF 1.23+ find_tables API
            tables = page.find_tables(clip=fitz.Rect(table_bbox))

            if not tables or len(tables.tables) == 0:
                logger.debug(f"Tablo bulunamadı: {table_bbox}")
                return cells

            # En büyük tabloyu al (clip içinde birden fazla olabilir)
            table = max(tables.tables, key=lambda t: len(t.cells) if t.cells else 0)

            if not table.cells:
                logger.debug(f"Tablo hücreleri boş: {table_bbox}")
                return cells

            # Header tespiti - ilk satır genelde header
            first_row_cells = [c for c in table.cells if c[1] == 0]  # row_idx == 0
            has_header = len(first_row_cells) > 0

            for cell in table.cells:
                # cell format: (x0, y0, x1, y1, text, col_idx, row_idx, ...)
                if len(cell) < 7:
                    continue

                x0, y0, x1, y1, text = cell[0], cell[1], cell[2], cell[3], cell[4] if len(cell) > 4 else ""
                col_idx = cell[5] if len(cell) > 5 else 0
                row_idx = cell[6] if len(cell) > 6 else 0

                # Boş hücreleri atla
                if not text or not str(text).strip():
                    continue

                cells.append({
                    "bbox": (x0, y0, x1, y1),
                    "text": str(text).strip(),
                    "row": row_idx,
                    "col": col_idx,
                    "is_header": has_header and row_idx == 0,
                    "cell_width": x1 - x0,
                    "cell_height": y1 - y0,
                })

            logger.debug(f"Tablo hücreleri çıkarıldı: {len(cells)} hücre")

        except AttributeError:
            # PyMuPDF eski sürüm - find_tables yok
            logger.warning("PyMuPDF find_tables API bulunamadı. Manuel tablo çıkarma gerekli.")
            cells = self._extract_table_cells_manual(page, table_bbox)

        except Exception as e:
            logger.warning(f"Tablo hücre çıkarma hatası: {e}")
            cells = self._extract_table_cells_manual(page, table_bbox)

        return cells

    def _extract_table_cells_manual(
        self,
        page,
        table_bbox: Tuple[float, float, float, float]
    ) -> List[Dict]:
        """
        Manuel tablo hücre çıkarma (fallback).

        Tablo içindeki text bloklarını hücre olarak kullanır.
        PyMuPDF find_tables() olmayan eski sürümler için.
        """
        global fitz
        if fitz is None:
            import fitz as _fitz
            fitz = _fitz

        cells = []
        rect = fitz.Rect(table_bbox)

        try:
            # Tablo içindeki text bloklarını al
            blocks = page.get_text("dict", clip=rect, flags=fitz.TEXT_PRESERVE_WHITESPACE)

            for block in blocks.get("blocks", []):
                if block.get("type") != 0:  # Text block değilse atla
                    continue

                block_bbox = block.get("bbox", (0, 0, 0, 0))

                for line in block.get("lines", []):
                    line_text = ""
                    for span in line.get("spans", []):
                        line_text += span.get("text", "")

                    if line_text.strip():
                        line_bbox = line.get("bbox", block_bbox)
                        cells.append({
                            "bbox": tuple(line_bbox),
                            "text": line_text.strip(),
                            "row": -1,  # Manuel modda satır bilinmiyor
                            "col": -1,
                            "is_header": False,
                            "cell_width": line_bbox[2] - line_bbox[0],
                            "cell_height": line_bbox[3] - line_bbox[1],
                        })

            # Y koordinatına göre sırala (üstten alta)
            cells.sort(key=lambda c: (c["bbox"][1], c["bbox"][0]))

            logger.debug(f"Manuel tablo çıkarma: {len(cells)} metin bloğu")

        except Exception as e:
            logger.warning(f"Manuel tablo çıkarma hatası: {e}")

        return cells


def _demo_pdf() -> Optional[Path]:
    """Demonun varsayılan PDF'i: veri kökündeki ilk kitabın kaynak kopyası.

    ``<veri kökü>/output/<kitap>/source_book.pdf`` (1_cikar ve translate_book
    kaynak PDF'i kitap klasörüne bu adla kopyalar). Kitap yoksa None. Sabit
    bir makine yolu yazılmaz: kod her bilgisayarda ve paketli kurulumda aynı
    çalışsın (TASARIM F1).
    """
    from config.paths import cikti_koku

    books = sorted(cikti_koku(Path(__file__).parent.parent).glob("*/source_book.pdf"))
    return books[0] if books else None


# Test ve kullanim ornegi: python -m core.layout_detector [pdf]
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    # Betik olarak da çalışsın (python core/layout_detector.py): varsayılan
    # yollar config.paths'ten gelir. Proje kökü sys.path'in BAŞINA alınır;
    # betik kipinde sys.path[0] core/ olur ve oradaki core/config.py,
    # projenin config paketini gölgelerdi.
    _project_root = str(Path(__file__).resolve().parent.parent)
    sys.path[:] = [_project_root] + [p for p in sys.path if p != _project_root]

    # Test PDF yolu: argüman, yoksa veri kökündeki ilk kitap.
    if len(sys.argv) > 1:
        pdf_path = sys.argv[1]
    else:
        _default = _demo_pdf()
        if _default is None:
            print("Kullanım: python -m core.layout_detector <pdf> "
                  "(veri kökünde source_book.pdf'li kitap klasörü yok)",
                  file=sys.stderr)
            sys.exit(2)
        pdf_path = str(_default)

    # Detector olustur
    detector = LayoutDetector()

    # Ilk sayfayi test et
    print(f"\nPDF: {pdf_path}")
    print("Sayfa 1 analiz ediliyor...")

    elements = detector.detect_page(pdf_path, 0)

    print(f"\nToplam {len(elements)} element tespit edildi:")
    print("-" * 50)

    for e in elements:
        status = "CEVIR" if e.should_translate else "KORU"
        print(f"  [{status}] {e.element_type.value:20} | Guven: {e.confidence:.2f} | Bbox: ({e.bbox.x1:.0f}, {e.bbox.y1:.0f}, {e.bbox.x2:.0f}, {e.bbox.y2:.0f})")

    # Istatistikler
    translatable = detector.get_translatable_elements(elements)
    preserved = detector.get_preserved_elements(elements)

    print(f"\nOzet:")
    print(f"  Cevirilecek: {len(translatable)} element")
    print(f"  Korunacak:   {len(preserved)} element")
