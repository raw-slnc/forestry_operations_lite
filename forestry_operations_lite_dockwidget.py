import os
import shutil

from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import QEvent, QSettings, QSignalBlocker, Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsMapTool, QgsMapToolPan

FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "forestry_operations_lite_dockwidget_base.ui")
)


class _DsmPreparationCancelled(Exception):
    """The user cancelled DSM preparation before analysis starts."""


# 解析の各段階の重み（100万セルでの実測秒、Linux 開発機、2026-10-31。
# ポリゴン化にマスクを渡す版で測り直し）。ポリゴン化（stability / valley /
# integrate の大半）は対象域の形で大きく変わるので目安。残り時間は実行中の
# 経過から推定し直す。
_ANALYSIS_STAGE_WEIGHTS = {
    "prep":         0.25,  # 斜度・SHC の計算と保存
    "routing":      0.7,   # D8 流向・集水
    "stability":    2.3,   # FS ＋ 不安定域のポリゴン化
    "valley":       0.5,   # TWI ＋ 沢地形のポリゴン化（D8）
    "valley_multi": 9.0,   # 上に MFD/D∞ の分配率・集水を足したもの
    "flow":         5.7,   # Tc ＋ 時間‐面積法 ＋ 保存
    "integrate":    2.3,   # 統合リスク指標 ＋ High Risk Areas のポリゴン化
}


def _format_remaining(seconds):
    """残り秒数を "about 1 min 20 s left" の形にする（粗めに丸める）。"""
    s = max(0.0, float(seconds))
    if s < 10:
        return "a few seconds left"
    if s < 60:
        return f"about {int(round(s / 5.0) * 5)} s left"
    if s < 3600:
        m, r = divmod(int(round(s / 10.0) * 10), 60)
        return f"about {m} min {r} s left" if r else f"about {m} min left"
    h, r = divmod(int(round(s / 60.0)), 60)
    return f"about {h} h {r} min left"


class _ButtonProgress:
    """実行ボタンを、同じ場所・同じ高さの QProgressBar と入れ替える。

    バーは標準の QProgressBar なので、色や % の文字の出し方は QGIS の
    スタイルに従う。段階名・残り時間はその下のラベルに出す。
    finish() でボタンに戻す。setValue(0..100) は QProgressBar と同じ呼び方。
    """

    def __init__(self, button, bar):
        self._btn = button
        self._bar = bar
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setFixedHeight(button.height())
        button.setVisible(False)
        bar.setVisible(True)

    def setValue(self, pct):
        self._bar.setValue(int(pct))

    def finish(self):
        self._bar.setVisible(False)
        self._btn.setVisible(True)


class _AnalysisProgress:
    """地形解析の進捗表示（段階名・重み付きの%・残り時間）。

    bar: setValue(0..100) を持つもの（実行ボタンを使う _ButtonProgress）。
    stages: [(key, title, weight)]。stage(key) で段階に入り、update(frac) で
    段階内の 0..1 を伝える（解析ではワーカーの stage / progress signal に
    つなぐ）。どちらも最後に pump() を呼ぶ（計算をメインスレッドで回す
    場合に processEvents＋中止の確認を差し込むためのもの。不要なら何もしない
    関数を渡す）。
    estimate_s: 前回の実績から見積もった全体の秒数（無ければ None）。
    """

    _PAINT_INTERVAL = 0.2  # 秒。表示の書き換えはこれより頻繁にしない

    def __init__(self, bar, label, stages, pump, estimate_s=None):
        import time as _time
        self._now = _time.monotonic
        self._bar = bar
        self._label = label
        self._pump = pump
        self._estimate_s = estimate_s
        total = float(sum(w for _, _, w in stages)) or 1.0
        self._order = [k for k, _, _ in stages]
        self._titles = {k: t for k, t, _ in stages}
        self._start = {}
        self._span = {}
        acc = 0.0
        for k, _, w in stages:
            self._start[k] = acc / total
            self._span[k] = w / total
            acc += w
        self._cur = None
        self._frac = 0.0
        self._t0 = self._now()
        self._last_paint = -1.0

    def elapsed(self):
        return self._now() - self._t0

    def stage(self, key):
        self._cur = key
        self._set(self._start[key], force=True)

    def update(self, frac):
        if self._cur is None:
            return
        f = min(max(float(frac), 0.0), 1.0)
        self._set(self._start[self._cur] + self._span[self._cur] * f)

    def _set(self, overall, force=False):
        self._frac = max(self._frac, min(overall, 1.0))
        now = self._now()
        if force or now - self._last_paint >= self._PAINT_INTERVAL:
            self._last_paint = now
            self._paint()
        self._pump()

    def _remaining_text(self):
        elapsed = self.elapsed()
        # 出だしは推定がぶれるので、前回の実績があればそちらを使う
        if self._frac >= 0.05 and elapsed >= 3.0:
            return _format_remaining(elapsed * (1.0 - self._frac) / self._frac)
        if self._estimate_s:
            return _format_remaining(self._estimate_s - elapsed)
        return "estimating time…"

    def _paint(self):
        self._bar.setValue(int(self._frac * 100))
        i = self._order.index(self._cur) + 1
        self._label.setText(
            f"Step {i}/{len(self._order)}: {self._titles[self._cur]} "
            f"— {self._remaining_text()}"
        )


_RESAMPLE_THRESHOLD = 0.2   # これより細かい解像度（m）は自動リサンプル対象
_RESAMPLE_TARGET    = 0.5   # リサンプル後の解像度（m）


def _resample_dem(src, target_cell_size=_RESAMPLE_TARGET):
    """DEMを指定解像度にダウンサンプルして新しい DEMLoader を返す。
    整数倍の場合はブロック平均（NaN 対応）、非整数倍は scipy.ndimage.zoom を使用。"""
    import numpy as np
    from .terrain.dem_loader import DEMLoader

    factor = target_cell_size / src.cell_size
    int_factor = round(factor)
    data = src.data
    rows, cols = data.shape

    if abs(factor - int_factor) < 0.05 and int_factor > 1:
        # 整数倍：ブロック平均（高精度・NaN 対応）
        r_trim = (rows // int_factor) * int_factor
        c_trim = (cols // int_factor) * int_factor
        d = data[:r_trim, :c_trim]
        d_out = np.nanmean(
            d.reshape(r_trim // int_factor, int_factor,
                      c_trim // int_factor, int_factor),
            axis=(1, 3),
        )
    else:
        # 非整数倍：scipy zoom（NaN をノーデータ値で一時代替）
        from scipy.ndimage import zoom as _zoom
        d = data.copy()
        nan_mask = np.isnan(d)
        d[nan_mask] = 0.0
        scale = 1.0 / factor
        d_out = _zoom(d, scale, order=1)
        nan_out = _zoom(nan_mask.astype(np.float32), scale, order=1)
        d_out[nan_out > 0.5] = np.nan

    actual_cell = src.cell_size * (cols / d_out.shape[1])
    new_gt = (src.gt[0], actual_cell, 0.0, src.gt[3], 0.0, -actual_cell)

    result = DEMLoader()
    result.data = d_out
    result.gt = new_gt
    result.crs_wkt = src.crs_wkt
    result.cell_size = actual_cell
    result.nodata = src.nodata
    result._ds = src._ds
    result.path = src.path
    return result


class _ElidedPathLabel(QtWidgets.QLabel):
    """パスを省略表示し、クリックでファイルマネージャーを開くラベル。

    - 幅に収まらない場合は左側を ... で省略（パス末尾を優先表示）
    - ツールチップにフルパスを表示
    - 左クリックでシステムのファイルマネージャーを開く
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path = ""
        self._prefix = ""
        # Ignored にすることで sizeHint がレイアウト幅を超えないようにする
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Preferred,
        )

    def setPath(self, prefix, path):
        """表示を更新する。prefix='出力先: ' など固定テキスト、path=実パス文字列。"""
        self._prefix = prefix
        self._path = path
        self.setToolTip(path if path else "")
        self.setCursor(Qt.PointingHandCursor if path else Qt.ArrowCursor)
        self._update_elided()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided()

    def _update_elided(self):
        fm = self.fontMetrics()
        prefix_w = fm.horizontalAdvance(self._prefix)
        avail = max(self.width() - prefix_w - 70, 20)
        elided = fm.elidedText(self._path, Qt.ElideLeft, avail) if self._path else self._path
        super().setText(self._prefix + elided)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._path and os.path.isdir(self._path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))
        super().mousePressEvent(event)


class CrosshairOverlay(QtWidgets.QWidget):
    """プレビューキャンバス中央に十字線を描画する透明オーバーレイ。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setGeometry(parent.rect())
        parent.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.parent() and event.type() == QEvent.Resize:
            self.setGeometry(self.parent().rect())
        return False

    def paintEvent(self, event):
        from qgis.PyQt.QtGui import QPainter, QPen, QColor
        painter = QPainter(self)
        w, h = self.width(), self.height()
        cx, cy = w // 2, h // 2
        x1, x2 = w // 5, w * 4 // 5
        # 影
        pen_shadow = QPen(QColor(0, 0, 0, 80), 1)
        painter.setPen(pen_shadow)
        painter.drawLine(cx + 1, 0, cx + 1, h)
        painter.drawLine(0, cy + 1, w, cy + 1)
        # 本線（白）
        pen = QPen(QColor(255, 255, 255, 180), 1)
        painter.setPen(pen)
        painter.drawLine(cx, 0, cx, h)
        painter.drawLine(0, cy, w, cy)
        # 縦分割線（黄色、1/5・4/5）
        pen_y = QPen(QColor(255, 220, 0, 180), 1)
        painter.setPen(pen_y)
        painter.drawLine(x1, 0, x1, h)
        painter.drawLine(x2, 0, x2, h)
        painter.end()


class PreviewPanTool(QgsMapToolPan):
    """プレビューキャンバス用デフォルトツール。
    ドラッグでパン、スクロールホイールでズーム（QgsMapToolPan の標準機能）に加え、
    矢印キーでのパンをサポートする。
    """

    _PAN_FRACTION = 0.20  # キー1押しで現在の表示幅/高さの20%移動

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            ext = self.canvas().extent()
            dx = ext.width() * self._PAN_FRACTION
            dy = ext.height() * self._PAN_FRACTION
            c = self.canvas().center()
            if key == Qt.Key_Left:
                self.canvas().setCenter(QgsPointXY(c.x() - dx, c.y()))
            elif key == Qt.Key_Right:
                self.canvas().setCenter(QgsPointXY(c.x() + dx, c.y()))
            elif key == Qt.Key_Up:
                self.canvas().setCenter(QgsPointXY(c.x(), c.y() + dy))
            else:  # Key_Down
                self.canvas().setCenter(QgsPointXY(c.x(), c.y() - dy))
            self.canvas().refresh()
            event.accept()
        else:
            super().keyPressEvent(event)


class DemBrowserDialog(QtWidgets.QDialog):
    """DEM ファイル選択ダイアログ。
    プレビューキャンバスの領域でファイルをフィルタする機能付き。
    """

    _SK = "ForestryOperationsLite/dem_browse_dir"

    def __init__(self, preview_canvas, initial_dir="", parent=None, mode="dem"):
        super().__init__(parent)
        self._mode = mode
        self.setWindowTitle("Select DEM File" if mode == "dem" else "Select DSM File")
        self.setMinimumSize(540, 440)
        self._canvas = preview_canvas
        self._selected_path = None
        # 前回のディレクトリを復元
        saved_dir = QSettings().value(self._SK, "")
        self._current_dir = initial_dir or saved_dir or ""
        self._build_ui()
        if self._current_dir and os.path.isdir(self._current_dir):
            self._txt_dir.setText(self._current_dir)
        self._scan()

    # ── UI 構築 ─────────────────────────────────────────────────

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # ディレクトリ選択行
        dir_row = QtWidgets.QHBoxLayout()
        self._txt_dir = QtWidgets.QLineEdit()
        self._txt_dir.setPlaceholderText("Enter folder path or browse...")
        self._txt_dir.editingFinished.connect(self._on_dir_edited)
        btn_dir = QtWidgets.QPushButton("Browse...")
        btn_dir.clicked.connect(self._browse_dir)
        dir_row.addWidget(QtWidgets.QLabel("Folder:"))
        dir_row.addWidget(self._txt_dir, 1)
        dir_row.addWidget(btn_dir)
        layout.addLayout(dir_row)

        # フィルタチェック
        self._chk_filter = QtWidgets.QCheckBox("Show files overlapping project area only")
        self._chk_filter.setToolTip("List only files overlapping the preview canvas extent")
        # キャンバスのエクステントが空の場合はチェックボックスを無効化
        if self._canvas is None or self._canvas.extent().isEmpty():
            self._chk_filter.setEnabled(False)
            self._chk_filter.setToolTip("Filter unavailable: no extent set on preview canvas")
        self._chk_filter.toggled.connect(self._scan)
        layout.addWidget(self._chk_filter)

        # ファイルリスト
        self._list = QtWidgets.QListWidget()
        self._list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.itemDoubleClicked.connect(self._on_accept)
        layout.addWidget(self._list, 1)

        # ファイル情報ラベル
        self._lbl_info = QtWidgets.QLabel("Select a file")
        self._lbl_info.setStyleSheet("color:#555;font-size:9pt;")
        self._lbl_info.setWordWrap(True)
        self._lbl_info.setMinimumHeight(36)
        layout.addWidget(self._lbl_info)

        # 外部リンクを開くボタン（VIRTUAL SHIZUOKA 選択時のみ表示）
        self._btn_open_url = QtWidgets.QPushButton("🔗 Open download page in browser")
        self._btn_open_url.setVisible(False)
        self._btn_open_url.clicked.connect(self._open_selected_url)
        layout.addWidget(self._btn_open_url)
        self._selected_url = None

        # OK / Cancel
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        self._btn_ok = btns.button(QtWidgets.QDialogButtonBox.Ok)
        self._btn_ok.setEnabled(False)
        layout.addWidget(btns)

    # ── ディレクトリ操作 ─────────────────────────────────────────

    def _browse_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Folder", self._current_dir or ""
        )
        if d:
            self._txt_dir.setText(d)
            self._current_dir = d
            self._scan()

    def _on_dir_edited(self):
        d = self._txt_dir.text().strip()
        if os.path.isdir(d):
            self._current_dir = d
            self._scan()

    # ── エクステント判定 ─────────────────────────────────────────

    @staticmethod
    def _read_dem_extent(path):
        """GeoTIFF のエクステントと CRS を (xmin,ymin,xmax,ymax,wkt) で返す。失敗時 None。"""
        try:
            from osgeo import gdal
            ds = gdal.Open(path, gdal.GA_ReadOnly)
            if ds is None:
                return None
            gt = ds.GetGeoTransform()
            cols, rows = ds.RasterXSize, ds.RasterYSize
            wkt = ds.GetProjection()
            ds = None
            xmin = gt[0]
            xmax = gt[0] + cols * gt[1]
            ymax = gt[3]
            ymin = gt[3] + rows * gt[5]
            if ymin > ymax:
                ymin, ymax = ymax, ymin
            return xmin, ymin, xmax, ymax, wkt
        except Exception:
            return None

    def _canvas_rect_in_dem_crs(self, dem_wkt):
        """プレビューキャンバスのエクステントを DEM の CRS に変換した QgsRectangle を返す。"""
        if self._canvas is None:
            return None
        try:
            dem_crs = QgsCoordinateReferenceSystem()
            dem_crs.createFromWkt(dem_wkt)
            if not dem_crs.isValid():
                return None
            canvas_crs = self._canvas.mapSettings().destinationCrs()
            xform = QgsCoordinateTransform(canvas_crs, dem_crs, QgsProject.instance())
            return xform.transformBoundingBox(self._canvas.extent())
        except Exception:
            return None

    def _overlaps_canvas(self, path):
        """ファイルのエクステントがプレビューキャンバスと重なるか確認。"""
        info = self._read_dem_extent(path)
        if info is None:
            return True  # 読み取れない場合は表示する
        xmin, ymin, xmax, ymax, wkt = info
        canvas_rect = self._canvas_rect_in_dem_crs(wkt)
        if canvas_rect is None or canvas_rect.isEmpty():
            return True
        return not (
            canvas_rect.xMaximum() < xmin
            or canvas_rect.xMinimum() > xmax
            or canvas_rect.yMaximum() < ymin
            or canvas_rect.yMinimum() > ymax
        )

    # GSI タイルソースを示すセンチネル値
    GSI_DEM1A_SENTINEL  = "__GSI_DEM1A__"
    GSI_DEM5A_SENTINEL  = "__GSI_DEM5A__"
    GSI_DEM10B_SENTINEL = "__GSI_DEM10B__"

    # VIRTUAL SHIZUOKA LP/Grid 自動取得センチネル値
    VS_LP_GRID_SENTINEL = "__VS_LP_GRID__"

    # VIRTUAL SHIZUOKA LP/Ground DSM 自動取得センチネル値（全年度・全エリア）
    VS_LP_GROUND_SENTINEL = "__VS_LP_GROUND__"

    # Copernicus GLO-30 センチネル値
    COPERNICUS_GLO30_SENTINEL = "__COPERNICUS_GLO30__"

    # AWS Terrarium タイルソースを示すセンチネル値（全球）
    TERRARIUM_FINE_SENTINEL     = "__TERRARIUM_FINE__"
    TERRARIUM_STANDARD_SENTINEL = "__TERRARIUM_STD__"
    TERRARIUM_WIDE_SENTINEL     = "__TERRARIUM_WIDE__"

    # Public DEM/DSM sources fetched for the current canvas extent.
    AHN_DTM_SENTINEL         = "__AHN_DTM__"
    AHN_DSM_SENTINEL         = "__AHN_DSM__"
    IGN_LIDARHD_MNT_SENTINEL = "__IGN_LIDARHD_MNT__"
    IGN_LIDARHD_MNS_SENTINEL = "__IGN_LIDARHD_MNS__"
    LINZ_DEM_SENTINEL        = "__LINZ_DEM_1M__"
    REMOTE_DEM_SENTINELS = (
        AHN_DTM_SENTINEL, IGN_LIDARHD_MNT_SENTINEL, LINZ_DEM_SENTINEL)
    REMOTE_DSM_SENTINELS = (AHN_DSM_SENTINEL, IGN_LIDARHD_MNS_SENTINEL)
    REMOTE_DSM_PAIR = {
        AHN_DTM_SENTINEL: AHN_DSM_SENTINEL,
        IGN_LIDARHD_MNT_SENTINEL: IGN_LIDARHD_MNS_SENTINEL,
    }
    REMOTE_SHORT_NAMES = {
        AHN_DTM_SENTINEL: "AHN4 DTM (0.5m)",
        AHN_DSM_SENTINEL: "AHN4 DSM (0.5m)",
        IGN_LIDARHD_MNT_SENTINEL: "IGN LiDAR HD MNT (0.5m)",
        IGN_LIDARHD_MNS_SENTINEL: "IGN LiDAR HD MNS (0.5m)",
        LINZ_DEM_SENTINEL: "LINZ DEM (1m)",
    }
    SESSION_ONLY_SENTINELS = (
        VS_LP_GRID_SENTINEL, VS_LP_GROUND_SENTINEL,
    ) + REMOTE_DEM_SENTINELS + REMOTE_DSM_SENTINELS

    _REMOTE_REGIONS = [
        ("── Netherlands open data (AHN4, auto-fetch) ──",
         (3.2, 50.7, 7.3, 53.6), "#8a4a00",
         [(AHN_DTM_SENTINEL,
           "🌐  AHN4 DTM  0.5m  (Netherlands, auto-fetch)",
           "AHN4 0.5m DTM — EPSG:28992, CC0.\n"
           "Fetched from the PDOK WCS for the current canvas extent.\n"
           "The service can be slow; use a small extent.")],
         [(AHN_DSM_SENTINEL,
           "🌐  AHN4 DSM  0.5m  (Netherlands, auto-fetch)",
           "AHN4 0.5m DSM — EPSG:28992, CC0.\n"
           "Fetched from the PDOK WCS for the current canvas extent.\n"
           "The service can be slow; use a small extent.")]),
        ("── France open data (IGN LiDAR HD, auto-fetch) ──",
         (-5.5, 41.2, 9.8, 51.2), "#1a3a8a",
         [(IGN_LIDARHD_MNT_SENTINEL,
           "🌐  IGN LiDAR HD MNT  0.5m  (France, auto-fetch)",
           "IGN LiDAR HD MNT 0.5m — EPSG:2154, Licence Ouverte Etalab 2.0.\n"
           "Fetched from the Geoplateforme WMS-R for the current canvas extent.\n"
           "Coverage is incomplete in some areas.")],
         [(IGN_LIDARHD_MNS_SENTINEL,
           "🌐  IGN LiDAR HD MNS  0.5m  (France, auto-fetch)",
           "IGN LiDAR HD MNS 0.5m — EPSG:2154, Licence Ouverte Etalab 2.0.\n"
           "Fetched from the Geoplateforme WMS-R for the current canvas extent.\n"
           "Coverage is incomplete in some areas.")]),
        ("── New Zealand open data (LINZ, auto-fetch) ──",
         (166.0, -47.5, 179.0, -34.0), "#1a6b6b",
         [(LINZ_DEM_SENTINEL,
           "🌐  LINZ DEM  1m  (New Zealand, auto-fetch)",
           "LINZ New Zealand 1m DEM — EPSG:2193, CC BY 4.0.\n"
           "Read from Cloud Optimised GeoTIFFs on AWS for the current canvas extent.\n"
           "Requires GDAL LERC support.")],
         []),
    ]

    # (sentinel, リスト表示名, ダウンロードURL, 情報テキスト)
    _COPERNICUS_ITEMS = [
        ("__COPERNICUS_GLO30__",
         "🌍  Copernicus GLO-30  30m  (global, regional overview)",
         "https://portal.opentopography.org/raster?opentopoID=OTSDEM.032021.4326.3",
         "Copernicus DEM GLO-30 — 30m global coverage. Free, attribution required (© DLR/ESA).\n"
         "⚠ 30m resolution: suitable for regional overview only. Not recommended for detailed slope / flow analysis.\n"
         "Download GeoTIFF → reproject to UTM → load as local file."),
    ]

    # (sentinel, リスト表示名, 情報テキスト)
    _TERRARIUM_ITEMS = [
        ("__TERRARIUM_FINE__",
         "🌐  Terrarium  ~2m grid  (fine, slow for large areas)",
         "AWS Terrain Tiles (Mapzen Terrarium) ~2m — Global coverage. Free, no registration.\n"
         "Suitable for small areas. Extent will be automatically fetched from the preview canvas."),
        ("__TERRARIUM_STD__",
         "🌐  Terrarium  ~5m grid  (standard)",
         "AWS Terrain Tiles (Mapzen Terrarium) ~5m — Global coverage. Free, no registration.\n"
         "Extent will be automatically fetched from the preview canvas."),
        ("__TERRARIUM_WIDE__",
         "🌐  Terrarium  ~10m grid  (wide area)",
         "AWS Terrain Tiles (Mapzen Terrarium) ~10m — Global coverage. Free, no registration.\n"
         "Suitable for large-area analysis. Extent will be automatically fetched from the preview canvas."),
    ]

    # (sentinel, リスト表示名, 情報テキスト)
    _GSI_ITEMS = [
        ("__GSI_DEM1A__",
         "🌐  DEM1A  1m grid  (laser survey areas only)",
         "DEM1A 1m — Airborne laser survey. Surveyed areas only (Izu Pen., mountains, forests)."),
        ("__GSI_DEM5A__",
         "🌐  DEM5A  5m grid  (standard)",
         "DEM5A 5m — Standard resolution. Available nationwide."),
        ("__GSI_DEM10B__",
         "🌐  DEM10B  10m grid  (wide area)",
         "DEM10B 10m — Wide-area analysis. Available nationwide."),
    ]

    # VIRTUAL SHIZUOKA データソース（静岡県、WGS84 bbox）
    # 日本全域 bbox（WGS84）
    _JAPAN_BBOX_WGS84 = (122.0, 20.0, 154.0, 46.0)

    _VS_BBOX_WGS84 = (137.47410694, 34.57213583, 139.17655861, 35.64595651)
    _VS_ITEMS = [
        ("__VS_2019__",
         "📦  VIRTUAL SHIZUOKA 2019 — SE Fuji / E Izu",
         "https://www.geospatial.jp/ckan/dataset/shizuoka-2019-pointcloud/resource/723e3289-f0da-425d-b669-de71479f1946",
         "LAS point cloud / 0.5m DTM grid (CC BY 4.0)\nDownload by tile → use as local DEM"),
        ("__VS_2020__",
         "📦  VIRTUAL SHIZUOKA 2020 — W Izu",
         "https://www.geospatial.jp/ckan/dataset/shizuoka-2020-pointcloud/resource/d2b735ba-7689-4f27-8657-dceeb645e5f4",
         "LAS point cloud / 0.5m DTM grid (CC BY 4.0)\nDownload by tile → use as local DEM"),
        ("__VS_2021__",
         "📦  VIRTUAL SHIZUOKA 2021 — Fuji / E Shizuoka",
         "https://www.geospatial.jp/ckan/dataset/shizuoka-2021-pointcloud/resource/b1d6f7db-3097-4b91-87f9-61f50043bd8f",
         "LAS point cloud / 0.5m DTM grid (CC BY 4.0)\nDownload by tile → use as local DEM"),
        ("__VS_MW__",
         "📦  VIRTUAL SHIZUOKA — Central / West (incl. MMS)",
         "https://www.geospatial.jp/ckan/dataset/virtual-shizuoka-mw/resource/7e8cac4d-6ab8-4ec9-b730-41123c6ae0b2",
         "LAS / MMS point cloud / 0.5m DTM grid (CC BY 4.0)\nDownload by tile → use as local DEM"),
        ("__VS_1920__",
         "📦  VIRTUAL SHIZUOKA 2019+2020 merged — SE Fuji / all Izu",
         "https://www.geospatial.jp/ckan/dataset/shizuoka-19-20-pointcloud/resource/aa5e1c19-cab2-4852-8a46-8499f360aa23",
         "2019+2020 merged. LP/ALB/MMS point cloud (CC BY 4.0)\nDownload by tile → use as local DEM"),
        ("__VS_2022__",
         "📦  VIRTUAL SHIZUOKA 2022 — N (Southern Alps)",
         "https://www.geospatial.jp/ckan/dataset/shizuoka-2022-pointcloud/resource/346d480c-1709-4db4-87f6-f864c3f9b680",
         "LAS point cloud / 0.5m DTM grid (CC BY 4.0)\nDownload by tile → use as local DEM"),
        ("__VS_2025__",
         "📦  VIRTUAL SHIZUOKA 2025 — NW Shizuoka",
         "https://www.geospatial.jp/ckan/dataset/shizuoka-2025-pointcloud/resource/0abed3d9-1b89-4577-bba5-709953bfc611",
         "LAS point cloud / 0.5m DTM grid (CC BY 4.0)\nDownload by tile → use as local DEM"),
    ]

    # ── スキャン ─────────────────────────────────────────────────

    def _scan(self):
        self._list.clear()
        self._btn_ok.setEnabled(False)

        from qgis.PyQt.QtGui import QColor, QFont
        filter_on = self._chk_filter.isChecked()

        # ── GSI タイル（日本域のみ）──
        if self._canvas_overlaps_japan():
            sec = QtWidgets.QListWidgetItem("── GSI Elevation Tiles (Japan) ──────")
            sec.setFlags(Qt.NoItemFlags)
            sec.setForeground(QColor("#1a5276"))
            f2 = QFont(); f2.setBold(True)
            sec.setFont(f2)
            self._list.addItem(sec)
        for sentinel, label, _ in (self._GSI_ITEMS if self._canvas_overlaps_japan() else []):
            item = QtWidgets.QListWidgetItem(label)
            item.setData(Qt.UserRole, sentinel)
            f = QFont(); f.setBold(True)
            item.setFont(f)
            item.setForeground(QColor("#1a5276"))
            self._list.addItem(item)

        # ── AWS Terrarium タイル（全球・常時表示）──
        sec_ter = QtWidgets.QListWidgetItem("── Global DEM Tiles (AWS Terrarium) ──")
        sec_ter.setFlags(Qt.NoItemFlags)
        sec_ter.setForeground(QColor("#1a5276"))
        f5 = QFont(); f5.setBold(True)
        sec_ter.setFont(f5)
        self._list.addItem(sec_ter)
        for sentinel, label, _ in self._TERRARIUM_ITEMS:
            item = QtWidgets.QListWidgetItem(label)
            item.setData(Qt.UserRole, sentinel)
            f = QFont(); f.setBold(True)
            item.setFont(f)
            item.setForeground(QColor("#1a5276"))
            self._list.addItem(item)

        # Public country-specific DEM/DSM sources are shown only over coverage.
        for title, bbox, color, dem_items, dsm_items in self._REMOTE_REGIONS:
            items = dem_items if self._mode == "dem" else dsm_items
            if not items or not self._canvas_overlaps_bbox(bbox):
                continue
            section = QtWidgets.QListWidgetItem(title)
            section.setFlags(Qt.NoItemFlags)
            section.setForeground(QColor(color))
            section_font = QFont(); section_font.setBold(True)
            section.setFont(section_font)
            self._list.addItem(section)
            for sentinel, label, _info in items:
                item = QtWidgets.QListWidgetItem(label)
                item.setData(Qt.UserRole, sentinel)
                font = QFont(); font.setBold(True)
                item.setFont(font)
                item.setForeground(QColor(color))
                self._list.addItem(item)

        sep = QtWidgets.QListWidgetItem("── Local Files ──────────────────────")
        sep.setFlags(Qt.NoItemFlags)
        sep.setForeground(QColor("#999"))
        self._list.addItem(sep)

        d = self._txt_dir.text().strip()
        if not os.path.isdir(d):
            self._lbl_info.setText("Select a folder to browse local files")
        else:
            self._current_dir = d

            self._lbl_info.setText("Scanning...")
            QtWidgets.QApplication.processEvents()

            try:
                files = sorted(
                    f for f in os.listdir(d)
                    if f.lower().endswith((".tif", ".tiff", ".zip"))
                )
                added = 0
                for fname in files:
                    fpath = os.path.join(d, fname)
                    if filter_on and fname.lower().endswith((".tif", ".tiff")) \
                            and not self._overlaps_canvas(fpath):
                        continue
                    item = QtWidgets.QListWidgetItem(fname)
                    item.setData(Qt.UserRole, fpath)
                    self._list.addItem(item)
                    added += 1
                if added == 0:
                    msg = "No files found in project area" if filter_on else "No GeoTIFF files found"
                    self._lbl_info.setText(msg)
                else:
                    self._lbl_info.setText(f"{added} file(s)")

                # ZIP入りサブフォルダを結合候補として表示
                try:
                    subdirs = sorted(
                        sd for sd in os.listdir(d)
                        if os.path.isdir(os.path.join(d, sd))
                        and any(f.lower().endswith(".zip")
                                for f in os.listdir(os.path.join(d, sd)))
                    )
                    if subdirs:
                        sec_dir = QtWidgets.QListWidgetItem("── ZIP merge folders ────────────────")
                        sec_dir.setFlags(Qt.NoItemFlags)
                        sec_dir.setForeground(QColor("#999"))
                        self._list.addItem(sec_dir)
                        for sname in subdirs:
                            spath = os.path.join(d, sname)
                            n = sum(1 for f in os.listdir(spath) if f.lower().endswith(".zip"))
                            item = QtWidgets.QListWidgetItem(f"📁 {sname}  ({n} files)")
                            item.setData(Qt.UserRole, spath)
                            self._list.addItem(item)
                except OSError:
                    pass

            except OSError:
                self._lbl_info.setText("Cannot read folder")

        # ── Copernicus GLO-30（全球：常に表示）──
        sec_cop = QtWidgets.QListWidgetItem("── Copernicus DEM (Global) ───────────")
        sec_cop.setFlags(Qt.NoItemFlags)
        sec_cop.setForeground(QColor("#7b4f00"))
        f4 = QFont(); f4.setBold(True)
        sec_cop.setFont(f4)
        self._list.addItem(sec_cop)
        for sentinel, label, _url, _info in self._COPERNICUS_ITEMS:
            item = QtWidgets.QListWidgetItem(label)
            item.setData(Qt.UserRole, sentinel)
            f = QFont(); f.setBold(True)
            item.setFont(f)
            item.setForeground(QColor("#7b4f00"))
            self._list.addItem(item)

        # ── VIRTUAL SHIZUOKA（静岡県専用：フィルター状態に関わらず地理判定）──
        if self._canvas_overlaps_shizuoka():

            def _vs_section(label):
                sec = QtWidgets.QListWidgetItem(label)
                sec.setFlags(Qt.NoItemFlags)
                sec.setForeground(QColor("#1a6b1a"))
                f = QFont(); f.setBold(True)
                sec.setFont(f)
                self._list.addItem(sec)

            def _vs_subsection(label):
                sec = QtWidgets.QListWidgetItem(label)
                sec.setFlags(Qt.NoItemFlags)
                sec.setForeground(QColor("#4a8c4a"))
                self._list.addItem(sec)

            def _vs_item(label, sentinel):
                item = QtWidgets.QListWidgetItem(label)
                item.setData(Qt.UserRole, sentinel)
                f = QFont(); f.setBold(True)
                item.setFont(f)
                item.setForeground(QColor("#1a6b1a"))
                self._list.addItem(item)

            if self._mode == "dem":
                _vs_section("── VIRTUAL SHIZUOKA DTM (Shizuoka) ──")
                _vs_item("🌿  VS LP/Grid  0.5m DTM  (Shizuoka, auto-fetch)", self.VS_LP_GRID_SENTINEL)

            else:  # dsm
                _vs_section("── VIRTUAL SHIZUOKA DSM (Shizuoka) ──")
                _vs_item("🌿  VS LP → DSM  0.5m  (Shizuoka, auto-fetch)", self.VS_LP_GROUND_SENTINEL)

    # ── アイテム選択 ─────────────────────────────────────────────

    def _on_selection_changed(self):
        items = self._list.selectedItems()
        if not items:
            self._btn_ok.setEnabled(False)
            self._btn_open_url.setVisible(False)
            self._selected_url = None
            self._lbl_info.setText("Select a file")
            return
        self._handle_single_item(items[0])

    def _handle_single_item(self, current):
        path = current.data(Qt.UserRole)
        for _title, _bbox, _color, dem_items, dsm_items in self._REMOTE_REGIONS:
            for sentinel, _label, info_text in dem_items + dsm_items:
                if path == sentinel:
                    if path == self.LINZ_DEM_SENTINEL:
                        from .dem_worker import LERC_MISSING_MESSAGE, gdal_has_lerc
                        if not gdal_has_lerc():
                            info_text += "\n\n" + LERC_MISSING_MESSAGE
                            self._btn_ok.setEnabled(False)
                        else:
                            self._btn_ok.setEnabled(True)
                    else:
                        self._btn_ok.setEnabled(True)
                    self._lbl_info.setText(info_text)
                    self._btn_open_url.setVisible(False)
                    self._selected_url = None
                    return
        # VS LP/Grid 自動取得
        if path == self.VS_LP_GRID_SENTINEL:
            self._lbl_info.setText(
                "Virtual Shizuoka LP/Grid 0.5m DTM — EPSG:6676, CC BY 4.0.\n"
                "Tiles will be automatically fetched from S3 for the current canvas extent.\n"
                "Coverage: Shizuoka prefecture (2019–2025)."
            )
            self._btn_ok.setEnabled(True)
            self._btn_open_url.setVisible(False)
            self._selected_url = None
            return
        # VS LP 自動取得 → DSM 変換
        if path == self.VS_LP_GROUND_SENTINEL:
            self._lbl_info.setText(
                "Virtual Shizuoka LP → DSM 0.5m — EPSG:6676, CC BY 4.0.\n"
                "LAS tiles (all-return) fetched from S3 and converted to DSM.\n"
                "Uses LP/Original where available (2021 中西部・2025), LP/Ground elsewhere (all-return).\n"
                "Coverage: Shizuoka prefecture (2019–2025)."
            )
            self._btn_ok.setEnabled(True)
            self._btn_open_url.setVisible(False)
            self._selected_url = None
            return

        # GSI タイルソース
        for sentinel, _label, info_text in self._GSI_ITEMS:
            if path == sentinel:
                self._lbl_info.setText(info_text + "  Extent will be automatically fetched from the preview canvas.")
                self._btn_ok.setEnabled(True)
                self._btn_open_url.setVisible(False)
                self._selected_url = None
                return
        # AWS Terrarium タイルソース
        for sentinel, _label, info_text in self._TERRARIUM_ITEMS:
            if path == sentinel:
                self._lbl_info.setText(info_text)
                self._btn_ok.setEnabled(True)
                self._btn_open_url.setVisible(False)
                self._selected_url = None
                return
        # Copernicus GLO-30 リンクアイテム
        for sentinel, _label, url, info_text in self._COPERNICUS_ITEMS:
            if path == sentinel:
                self._lbl_info.setText(info_text)
                self._btn_ok.setEnabled(False)
                self._selected_url = url
                self._btn_open_url.setVisible(True)
                return
        self._btn_open_url.setVisible(False)
        self._selected_url = None

        if os.path.isdir(path):
            self._inspect_dir_item(path)
            return

        if path.lower().endswith(".zip"):
            self._inspect_zip_item(path)
            return

        info = self._read_dem_extent(path)
        if info:
            xmin, ymin, xmax, ymax, wkt = info
            try:
                from osgeo import osr
                srs = osr.SpatialReference(wkt=wkt)
                auth = srs.GetAuthorityCode(None)
                crs_str = f"EPSG:{auth}" if auth else "Unknown CRS"
            except Exception:
                crs_str = "Unknown CRS"
            w = round(xmax - xmin, 1)
            h = round(ymax - ymin, 1)
            self._lbl_info.setText(
                f"Extent: X {xmin:.1f}–{xmax:.1f}  /  Y {ymin:.1f}–{ymax:.1f}"
                f"  ({w} × {h})  |  {crs_str}"
            )
        else:
            self._lbl_info.setText(os.path.basename(path))
        self._btn_ok.setEnabled(True)

    # ── ZIP 内容確認 ─────────────────────────────────────────────

    def _inspect_dir_item(self, dir_path):
        """ZIP結合フォルダ選択時の情報表示。"""
        zips = sorted(f for f in os.listdir(dir_path) if f.lower().endswith(".zip"))
        if not zips:
            self._lbl_info.setText("No ZIP files found")
            self._btn_ok.setEnabled(False)
            return
        dir_name = os.path.basename(dir_path)
        merged_name = f"merged_{dir_name}.tif"
        out_path = os.path.join(os.path.dirname(dir_path), merged_name)
        if os.path.exists(out_path):
            status = f"(already merged: will overwrite {merged_name})"
        else:
            status = f"→ will generate {merged_name}"
        self._lbl_info.setText(
            f"{len(zips)} ZIPs to merge  {status}\n"
            "⚠ Non-adjacent data may reduce analysis accuracy"
        )
        self._btn_ok.setEnabled(True)

    def _inspect_zip_item(self, zip_path):
        """ZIP選択時に中身を確認してinfo表示・OKボタン有効化を制御する。"""
        import zipfile as _zf
        try:
            with _zf.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
        except Exception as e:
            self._lbl_info.setText(f"Cannot open ZIP: {e}")
            self._btn_ok.setEnabled(False)
            return

        tif_names = [n for n in names if n.lower().endswith((".tif", ".tiff"))]
        txt_names = [n for n in names if n.lower().endswith("_dem.txt")]

        if tif_names:
            vsipath = f"/vsizip/{zip_path}/{tif_names[0]}"
            info = self._read_dem_extent(vsipath)
            if info:
                xmin, ymin, xmax, ymax, wkt = info
                try:
                    from osgeo import osr
                    srs = osr.SpatialReference(wkt=wkt)
                    auth = srs.GetAuthorityCode(None)
                    crs_str = f"EPSG:{auth}" if auth else "Unknown CRS"
                except Exception:
                    crs_str = "Unknown CRS"
                w = round(xmax - xmin, 1)
                h = round(ymax - ymin, 1)
                self._lbl_info.setText(
                    f"[ZIP TIF] {tif_names[0]}\n"
                    f"Extent: X {xmin:.1f}–{xmax:.1f}  /  Y {ymin:.1f}–{ymax:.1f}"
                    f"  ({w} × {h})  |  {crs_str}"
                )
            else:
                self._lbl_info.setText(f"[ZIP TIF] {tif_names[0]}")
            self._btn_ok.setEnabled(True)

        elif txt_names:
            txt_name = txt_names[0]
            tif_name = os.path.splitext(txt_name)[0] + ".tif"
            tif_path = os.path.join(os.path.dirname(zip_path), tif_name)
            if os.path.exists(tif_path):
                info = self._read_dem_extent(tif_path)
                if info:
                    xmin, ymin, xmax, ymax, _ = info
                    w = round(xmax - xmin, 1)
                    h = round(ymax - ymin, 1)
                    self._lbl_info.setText(
                        f"[Converted] {tif_name}\n"
                        f"Extent: X {xmin:.1f}–{xmax:.1f}  /  Y {ymin:.1f}–{ymax:.1f}"
                        f"  ({w} × {h})  |  EPSG:6676"
                    )
                else:
                    self._lbl_info.setText(f"[Converted] {tif_name}")
            else:
                self._lbl_info.setText(
                    f"[XYZ DEM / EPSG:6676] {txt_name}\n"
                    f"→ will convert to {tif_name} on confirm"
                )
            self._btn_ok.setEnabled(True)

        else:
            self._lbl_info.setText("Unsupported ZIP format (no TIF or _DEM.txt found)")
            self._btn_ok.setEnabled(False)

    # ── エクステント判定（VIRTUAL SHIZUOKA）────────────────────────

    def _canvas_overlaps_japan(self):
        """プレビューキャンバスの範囲が日本域と重なるか確認。"""
        if self._canvas is None or self._canvas.extent().isEmpty():
            return True
        try:
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            canvas_crs = self._canvas.mapSettings().destinationCrs()
            xform = QgsCoordinateTransform(canvas_crs, wgs84, QgsProject.instance())
            ext84 = xform.transformBoundingBox(self._canvas.extent())
            lon_min, lat_min, lon_max, lat_max = self._JAPAN_BBOX_WGS84
            return not (
                ext84.xMaximum() < lon_min or ext84.xMinimum() > lon_max
                or ext84.yMaximum() < lat_min or ext84.yMinimum() > lat_max
            )
        except Exception:
            return True

    def _canvas_overlaps_bbox(self, bbox_wgs84):
        """Whether the preview canvas intersects a WGS84 bounding box."""
        if self._canvas is None or self._canvas.extent().isEmpty():
            return False
        try:
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            canvas_crs = self._canvas.mapSettings().destinationCrs()
            xform = QgsCoordinateTransform(
                canvas_crs, wgs84, QgsProject.instance())
            ext84 = xform.transformBoundingBox(self._canvas.extent())
            lon_min, lat_min, lon_max, lat_max = bbox_wgs84
            return not (
                ext84.xMaximum() < lon_min or ext84.xMinimum() > lon_max
                or ext84.yMaximum() < lat_min or ext84.yMinimum() > lat_max)
        except Exception:
            return False

    def _canvas_overlaps_shizuoka(self):
        """プレビューキャンバスの範囲が VIRTUAL SHIZUOKA エリア（静岡県）と重なるか確認。"""
        if self._canvas is None or self._canvas.extent().isEmpty():
            return True  # 範囲不明の場合は表示する
        try:
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            canvas_crs = self._canvas.mapSettings().destinationCrs()
            xform = QgsCoordinateTransform(canvas_crs, wgs84, QgsProject.instance())
            ext84 = xform.transformBoundingBox(self._canvas.extent())
            lon_min, lat_min, lon_max, lat_max = self._VS_BBOX_WGS84
            return not (
                ext84.xMaximum() < lon_min or ext84.xMinimum() > lon_max
                or ext84.yMaximum() < lat_min or ext84.yMinimum() > lat_max
            )
        except Exception:
            return True

    # ── 決定 ─────────────────────────────────────────────────────

    def _on_accept(self, *_):
        items = self._list.selectedItems()
        if not items:
            return
        paths = [i.data(Qt.UserRole) for i in items if i.data(Qt.UserRole)]
        if not paths:
            return

        path = paths[0]

        # フォルダ選択 → 内部ZIP全件を結合
        if os.path.isdir(path):
            zip_paths = sorted(
                os.path.join(path, f)
                for f in os.listdir(path) if f.lower().endswith(".zip")
            )
            if not zip_paths:
                return
            resolved = self._resolve_zip_paths(zip_paths, dir_path=path)
            if resolved is None:
                return
            self._selected_path = resolved

        # 単体ZIP
        elif path.lower().endswith(".zip"):
            resolved = self._zip_to_tif(path)
            if resolved is None:
                return
            self._selected_path = resolved

        else:
            self._selected_path = path

        QSettings().setValue(self._SK, self._current_dir)
        self.accept()

    def _resolve_zip_paths(self, zip_paths, dir_path=None):
        """複数ZIPを変換・結合して1枚のTIFパスを返す。失敗時None。
        dir_path: ZIPが入っているフォルダ（出力名・削除対象の決定に使用）
        """
        tif_paths = []
        for i, zp in enumerate(zip_paths):
            self._lbl_info.setText(f"Converting... ({i + 1}/{len(zip_paths)}) {os.path.basename(zp)}")
            QtWidgets.QApplication.processEvents()
            tif = self._zip_to_tif(zp)
            if tif is None:
                return None
            if not tif.startswith("/vsizip/") and not os.path.exists(tif):
                self._lbl_info.setText(f"Converted file not found: {os.path.basename(tif)}")
                return None
            tif_paths.append(tif)

        if len(tif_paths) == 1:
            return tif_paths[0]

        # 結合
        self._lbl_info.setText(f"{len(tif_paths)} files merging...")
        QtWidgets.QApplication.processEvents()
        folder = dir_path or os.path.dirname(zip_paths[0])
        dir_name = os.path.basename(folder)
        out_dir = os.path.dirname(folder)
        out_path = os.path.join(out_dir, f"merged_{dir_name}.tif")
        try:
            from osgeo import gdal
            gdal.UseExceptions()
            ds = gdal.Warp(out_path, tif_paths, format="GTiff")
            if ds is None:
                self._lbl_info.setText(f"Merge failed: {gdal.GetLastErrorMsg()}")
                return None
            ds = None
        except Exception as e:
            self._lbl_info.setText(f"Merge error: {e}")
            return None

        # フォルダ内の個別TIFを削除（/vsizipパスは物理ファイルなし）
        for p in tif_paths:
            if not p.startswith("/vsizip/") and os.path.isfile(p):
                try:
                    os.remove(p)
                except Exception:  # nosec B110
                    pass

        return out_path

    def _zip_to_tif(self, zip_path):
        """ZIPからDEMパスを解決する。TIF→vsizipパス、_DEM.txt→変換してTIFパスを返す。失敗時None。"""
        import zipfile as _zf
        try:
            with _zf.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                tif_names = [n for n in names if n.lower().endswith((".tif", ".tiff"))]
                txt_names = [n for n in names if n.lower().endswith("_dem.txt")]

                if tif_names:
                    return f"/vsizip/{zip_path}/{tif_names[0]}"

                if txt_names:
                    txt_name = txt_names[0]
                    tif_name = os.path.splitext(txt_name)[0] + ".tif"
                    tif_path = os.path.join(os.path.dirname(zip_path), tif_name)
                    if os.path.exists(tif_path):
                        return tif_path
                    # 変換実行
                    self._lbl_info.setText("Converting... please wait")
                    QtWidgets.QApplication.processEvents()
                    import tempfile
                    with tempfile.TemporaryDirectory() as tmpdir:
                        zf.extract(txt_name, tmpdir)
                        txt_path = os.path.join(tmpdir, txt_name)
                        from osgeo import gdal
                        ds = gdal.Translate(
                            tif_path, txt_path,
                            outputSRS="EPSG:6676",
                            format="GTiff",
                        )
                        if ds is None:
                            self._lbl_info.setText("Conversion failed")
                            return None
                        ds = None
                    return tif_path

        except Exception as e:
            self._lbl_info.setText(f"ZIP error: {e}")
        return None

    def _open_selected_url(self):
        """選択中の VIRTUAL SHIZUOKA アイテムの URL をブラウザで開く。"""
        if self._selected_url:
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(self._selected_url))

    def selected_path(self):
        return self._selected_path

    def filter_active(self):
        return self._chk_filter.isChecked()


class LockedMapTool(QgsMapTool):
    """地図ロック中のマップツール。パン・ズームをすべて不可にする。"""

    def __init__(self, canvas):
        super().__init__(canvas)
        from qgis.PyQt.QtGui import QCursor
        self.setCursor(QCursor(Qt.ArrowCursor))
        # イベントフィルターでホイールイベントを消費（キャンバスのズームを阻止）
        canvas.viewport().installEventFilter(self)

    def removeFromCanvas(self):
        try:
            self.canvas().viewport().removeEventFilter(self)
        except Exception:  # nosec B110
            pass
        super().deactivate()

    def deactivate(self):
        try:
            self.canvas().viewport().removeEventFilter(self)
        except Exception:  # nosec B110
            pass
        super().deactivate()

    def eventFilter(self, obj, event):
        from qgis.PyQt.QtCore import QEvent
        if event.type() == QEvent.Wheel:
            return True  # ホイールイベントを消費してズームを阻止
        return False

    def canvasPressEvent(self, event):
        pass  # パン開始を阻止

    def canvasMoveEvent(self, event):
        pass

    def canvasReleaseEvent(self, event):
        pass

    def wheelEvent(self, event):
        pass  # ズーム阻止


class ForestryOperationsLiteDockWidget(QtWidgets.QWidget, FORM_CLASS):
    closingPlugin = pyqtSignal()

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setupUi(self)

        self.preview_canvas = None
        self._analysis_worker = None  # 実行中の TerrainAnalysisWorker
        self._analysis_ctx = None     # その実行の後処理に要る値（dict）
        self._dem_worker = None       # 実行中の DemJobWorker（DEM 取得）
        self._dem_job_ctx = None      # その取得の後処理に要る値（dict）
        self._side_jobs = {}          # 書き出し等のワーカー {name: ctx}（VS / Named Export の DEM）

        self._syncing = False
        self._initializing = True     # 起動完了まで preview→main 同期をブロック
        # tc（Time load）専用のフィルタ/バッファ状態は _load_settings より前に
        # 参照され得るのでここで最初に初期化しておく。
        self._filter_state_tc = "off"
        self._flow_buffer_state_tc = "off"
        self._elev_tile_cache = {}    # {(url_template, zoom, x, y): QImage}
        self._map_locked = False          # 地図ロック状態
        self._lock_analysis_extent = None # ロック時の解析範囲（QgsRectangle）
        self._locked_tool = None          # LockedMapTool インスタンス
        self._pending_apply_layer_display = False
        self._preview_has_layers = False
        self._post_init_scheduled = False

        self._apply_japanese_base_labels()
        self._build_extended_ui()
        self._connect_extended_signals()
        self._refresh_layer_combos()
        QgsProject.instance().layersAdded.connect(self._refresh_layer_combos)
        QgsProject.instance().layersRemoved.connect(self._refresh_layer_combos)
        # プロジェクトの開く・保存・クリア（closeEvent で外し、開き直すときに
        # initialize_window_mode でつなぎ直す）
        self._project_signals_connected = False
        self._connect_project_signals()
        self._load_settings()
        self._restore_layer_combos_from_project()  # プロジェクト組み込み設定を優先適用
        # 起動時点で既存の解析結果を選択可能にする（呼ばないと解析番号コンボが
        # 空のままで、読込ボタンもグレーアウトしたまま = 起動直後は選択できない）。
        # 以前の書き出しの Named Export への移行もここで行われる。
        self._refresh_analysis_combo(select_latest=False)
        # Named Export から解析番号へ戻るときの、解析番号の表示の設定（初めは開いたときのもの）
        self._run_display_snapshot = self._run_display_snapshot_data()

    def _apply_japanese_base_labels(self):
        self.setWindowTitle("Forestry Operations Lite")
        self.grpTerrain.setTitle("Terrain Source")
        self.lblStatus.setText("Ready")

    def _build_extended_ui(self):
        while self.verticalLayout.count():
            item = self.verticalLayout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)

        # ── 解析データ表示管理ウィジェットを先行作成 ──────────────────
        # （_build_terrain_tab より前に作る必要があるため）
        self.btnTerrainToggle = QtWidgets.QPushButton("Analysis Data")
        self.btnTerrainToggle.setCheckable(True)
        self.btnTerrainToggle.setChecked(True)
        self.btnTerrainToggle.setFocusPolicy(Qt.NoFocus)
        self.btnTerrainToggle.setToolTip("Toggle all analysis layers (preserves individual state)")

        self.chkLoadStability  = QtWidgets.QPushButton("Slope Stability")
        self.chkLoadValley     = QtWidgets.QPushButton("Valley Terrain")
        self.chkLoadWetland    = QtWidgets.QPushButton("Wetland Terrain")
        self.chkLoadFlow       = QtWidgets.QPushButton("Flow Estimation")
        self.chkLoadIntegrated = QtWidgets.QPushButton("Overall Risk")
        for _b in (self.chkLoadStability, self.chkLoadValley,
                   self.chkLoadWetland, self.chkLoadFlow, self.chkLoadIntegrated):
            _b.setCheckable(True)
            _b.setEnabled(False)   # 解析データが存在するまでグレーアウト
            _b.setStyleSheet(self._BTN_STYLE_NORMAL)
            _b.setFocusPolicy(Qt.NoFocus)
        self.btnTerrainToggle.setStyleSheet(
            "QPushButton{padding:2px 10px;}"
            "QPushButton:checked{background:#f0ff1a;color:#222;"
            "border:1px solid #b8a010;border-radius:3px;}"
        )

        def _make_opacity_spin(default=70):
            sp = QtWidgets.QSpinBox()
            sp.setRange(0, 100)
            sp.setValue(default)
            sp.setSuffix("%")
            sp.setFixedWidth(54)
            sp.setToolTip("Opacity (0=opaque, 100=transparent)")
            return sp
        self.spinOpacityStability  = _make_opacity_spin()
        self.spinOpacityValley     = _make_opacity_spin()
        self.spinOpacityWetland    = _make_opacity_spin()
        self.spinOpacityFlow       = _make_opacity_spin()
        self.spinOpacityIntegrated = _make_opacity_spin()

        def _make_filter_btn():
            b = QtWidgets.QPushButton("off")
            b.setFixedWidth(32)
            b.setCheckable(False)
            b.setToolTip("low: fade low values / mid: transparent below median / off: no filter")
            b.setStyleSheet("font-size:8pt; padding:1px 2px;")
            return b
        self.btnFilterWetland = _make_filter_btn()
        self.btnFilterFlow    = _make_filter_btn()
        self.btnFlowBuffer = QtWidgets.QPushButton("Buffer: Off")
        self.btnFlowBuffer.setToolTip("Apply blur to flow layer: Off→Low→High")
        self.btnFlowBuffer.setStyleSheet("font-size:8pt; padding:1px 2px;")

        self.lblLoadStatus = QtWidgets.QLabel("")
        self.lblLoadStatus.setStyleSheet("color:#555;font-size:8pt;")
        # 1行目の空きいっぱいを使い、長い文は端を切る（文の長さで行＝右パネルの
        # 最小幅を広げない）。全文はツールチップで見られるようにする。
        self.lblLoadStatus.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                         QtWidgets.QSizePolicy.Preferred)

        # 解析番号セレクタ（解析データの表示管理 Row1）
        self.cmbAnalysisNumber = QtWidgets.QComboBox()
        self.cmbAnalysisNumber.addItem("Select run", None)
        self.cmbAnalysisNumber.setFixedWidth(180)
        self.cmbAnalysisNumber.setToolTip("Select analysis run to display")
        self.btnRefreshAnalysis = QtWidgets.QPushButton("↺")
        self.btnRefreshAnalysis.setFixedWidth(28)
        self.btnRefreshAnalysis.setToolTip("Refresh analysis list")

        self.mainSplitter = QtWidgets.QSplitter(Qt.Horizontal, self)
        self.leftPane = QtWidgets.QWidget(self.mainSplitter)
        self.rightPane = QtWidgets.QWidget(self.mainSplitter)
        self.leftPane.setMinimumWidth(480)
        self.leftPane.setMaximumWidth(480)
        left_layout = QtWidgets.QVBoxLayout(self.leftPane)
        right_layout = QtWidgets.QVBoxLayout(self.rightPane)

        self.grpPreviewCanvas = QtWidgets.QGroupBox("Design Preview")
        preview_layout = QtWidgets.QVBoxLayout(self.grpPreviewCanvas)
        self.preview_canvas = QgsMapCanvas(self)
        self.preview_canvas.setCanvasColor(Qt.white)
        self.preview_canvas.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        # キーボードフォーカスを有効化（矢印キーパンのため）
        self.preview_canvas.setFocusPolicy(Qt.StrongFocus)
        preview_layout.addWidget(self.preview_canvas)

        # デフォルトツール: ドラッグ・スクロール・矢印キーでのパン/ズーム
        self._pan_tool = PreviewPanTool(self.preview_canvas)
        self.preview_canvas.setMapTool(self._pan_tool)

        # 中心十字線オーバーレイ
        self._crosshair = CrosshairOverlay(self.preview_canvas)
        self._crosshair.raise_()

        _ps = "QLabel{border:1px solid palette(mid);padding:0 4px;font-size:9pt;}"
        _ts = "QLabel{font-size:9pt;}"
        self.lblPreviewStatus = QtWidgets.QLabel("---")
        self.lblPreviewStatusScale = QtWidgets.QLabel("---")
        self.lblPreviewStatusCrs = QtWidgets.QLabel("---")
        self.lblPreviewStatusArea = QtWidgets.QLabel("---")
        for _l in (self.lblPreviewStatus, self.lblPreviewStatusScale,
                   self.lblPreviewStatusCrs, self.lblPreviewStatusArea):
            _l.setStyleSheet(_ps)
            _l.setFixedHeight(20)
        # ステータスバー
        _sb = QtWidgets.QWidget()
        _sb.setFixedHeight(22)
        _sbl = QtWidgets.QHBoxLayout(_sb)
        _sbl.setContentsMargins(0, 1, 0, 1)
        _sbl.setSpacing(4)
        self._lbl_center_prefix = QtWidgets.QLabel("Center:")
        self._lbl_scale_prefix  = QtWidgets.QLabel("Scale:")
        self._lbl_crs_prefix    = QtWidgets.QLabel("CRS:")
        self._lbl_area_prefix   = QtWidgets.QLabel("Area:")
        for _prefix_lbl, _val in (
            (self._lbl_center_prefix, self.lblPreviewStatus),
            (self._lbl_scale_prefix,  self.lblPreviewStatusScale),
            (self._lbl_area_prefix,   self.lblPreviewStatusArea),
            (self._lbl_crs_prefix,    self.lblPreviewStatusCrs),
        ):
            _prefix_lbl.setStyleSheet(_ts)
            _prefix_lbl.setFixedHeight(20)
            _sbl.addWidget(_prefix_lbl)
            _sbl.addWidget(_val)
            _sbl.addSpacing(8)
        _sbl.addStretch(1)
        preview_layout.addWidget(_sb)

        self.chkStandaloneWindow = QtWidgets.QCheckBox("Open in standalone window")
        self.chkStandaloneWindow.setChecked(True)
        self.chkStandaloneWindow.hide()

        self.grpLayers = QtWidgets.QGroupBox("Layer Settings")
        layer_layout = QtWidgets.QGridLayout(self.grpLayers)
        self.cmbBackgroundLayer = QtWidgets.QComboBox()
        self.cmbTileLayer = QtWidgets.QComboBox()
        self.cmbGpkgLayer = QtWidgets.QComboBox()
        self.btnRefreshLayerList = QtWidgets.QPushButton("Refresh Layers")

        # 解析データの表示管理 Row2 で使うレイヤー表示管理ウィジェット（ここで先行作成）
        self.btnGpkgLayerVis = QtWidgets.QPushButton("GPKG")
        self.btnTileLayerVis = QtWidgets.QPushButton("Tile")
        self.btnBgLayerVis   = QtWidgets.QPushButton("Background")
        for _b in (self.btnGpkgLayerVis, self.btnTileLayerVis, self.btnBgLayerVis):
            _b.setCheckable(True)
            _b.setChecked(True)
            _b.setStyleSheet(self._BTN_STYLE_LAYER)
            _b.setFocusPolicy(Qt.NoFocus)

        # Qt5(QGIS3): QSS を当てた表示ボタンが素の QPushButton より低く描画されるため、
        # Terrain Data タブの Browse ボタン相当（素の QPushButton）の高さに合わせる。
        _ref_btn_h = QtWidgets.QPushButton("Browse").sizeHint().height()
        for _b in (self.btnTerrainToggle,
                   self.chkLoadStability, self.chkLoadValley, self.chkLoadWetland,
                   self.chkLoadFlow, self.chkLoadIntegrated,
                   self.btnGpkgLayerVis, self.btnTileLayerVis, self.btnBgLayerVis):
            _b.setMinimumHeight(_ref_btn_h)

        self.spinGpkgOpacity = QtWidgets.QSpinBox()
        self.spinGpkgOpacity.setRange(0, 100)
        self.spinGpkgOpacity.setValue(100)
        self.spinGpkgOpacity.setSuffix("%")
        self.spinGpkgOpacity.setFixedWidth(54)
        self.spinGpkgOpacity.setToolTip("GPKG layer opacity")
        self.spinTileOpacity = QtWidgets.QSpinBox()
        self.spinTileOpacity.setRange(0, 100)
        self.spinTileOpacity.setValue(60)
        self.spinTileOpacity.setSuffix("%")
        self.spinTileOpacity.setFixedWidth(54)
        self.spinTileOpacity.setToolTip("Tile layer opacity")
        self.spinBgOpacity = QtWidgets.QSpinBox()
        self.spinBgOpacity.setRange(0, 100)
        self.spinBgOpacity.setValue(100)
        self.spinBgOpacity.setSuffix("%")
        self.spinBgOpacity.setFixedWidth(54)
        self.spinBgOpacity.setToolTip("Background map opacity")

        # Row 0: GPKGレイヤ | combo
        layer_layout.addWidget(QtWidgets.QLabel("GPKG Layer"),  0, 0)
        layer_layout.addWidget(self.cmbGpkgLayer,               0, 1)
        # Row 1: タイルレイヤ | combo
        layer_layout.addWidget(QtWidgets.QLabel("Tile Layer"), 1, 0)
        layer_layout.addWidget(self.cmbTileLayer,               1, 1)
        # Row 2: 背景地図 | combo
        layer_layout.addWidget(QtWidgets.QLabel("Background Map"),    2, 0)
        layer_layout.addWidget(self.cmbBackgroundLayer,         2, 1)
        # Row 3: 更新ボタン（span 2）
        layer_layout.addWidget(self.btnRefreshLayerList,        3, 0, 1, 2)
        layer_layout.setColumnStretch(1, 1)

        # ── 地表データ（tabDataSettings へ）──────────────────────────────
        self.grpDem = QtWidgets.QGroupBox("Terrain Data")
        dem_lay = QtWidgets.QGridLayout(self.grpDem)
        dem_lay.addWidget(QtWidgets.QLabel("DEM Data"),   0, 0)
        self.txtDemPath = QtWidgets.QLineEdit()
        self.txtDemPath.setReadOnly(True)
        self.txtDemPath.setPlaceholderText("Select file...")
        self.txtDemPath.setMaximumWidth(200)
        self.btnBrowseDem = QtWidgets.QPushButton("Browse")
        dem_lay.addWidget(self.txtDemPath,   0, 1)
        dem_lay.addWidget(self.btnBrowseDem, 0, 2)
        self.lblDemInfo = QtWidgets.QLabel("Not set")
        self.lblDemInfo.setWordWrap(True)
        dem_lay.addWidget(self.lblDemInfo,   1, 0, 1, 3)
        dem_lay.addWidget(QtWidgets.QLabel("DSM Data"), 2, 0)
        self.txtDsmPath = QtWidgets.QLineEdit()
        self.txtDsmPath.setReadOnly(True)
        self.txtDsmPath.setPlaceholderText("Select file... (optional)")
        self.txtDsmPath.setMaximumWidth(200)
        self.btnBrowseDsm = QtWidgets.QPushButton("Browse")
        dem_lay.addWidget(self.txtDsmPath,   2, 1)
        dem_lay.addWidget(self.btnBrowseDsm, 2, 2)
        self.lblDsmInfo = QtWidgets.QLabel("Not set")
        self.lblDsmInfo.setWordWrap(True)
        dem_lay.addWidget(self.lblDsmInfo,   3, 0, 1, 3)
        self._dem_path = ""
        self._dsm_path = ""
        self._partial_outside_warned = False   # reset when DEM changes
        self._dem_loading = False   # DEM をワーカーで取得中（Browse が Cancel）
        self._dsm_loading = False   # DSM をワーカーで取得中（DSM の Browse が Cancel）
        self._vs_export_dir = ""
        self._vs_exporting = False
        self._vs_wodmi_opened = False
        self._vs_dem_codes = []   # VS LP/Grid 読込時のタイルコード（Ortho 範囲合わせ用）
        self._vs_dsm_codes = []   # VS LP/Ground 読込時のタイルコード（同上）

        # ── VS Export グループ ──────────────────────────────────────────────
        self.grpVsExport = QtWidgets.QGroupBox("Export for WebODM Importer (Virtual Shizuoka data only)")
        _vs_lay = QtWidgets.QHBoxLayout(self.grpVsExport)
        _vs_lay.setContentsMargins(8, 4, 8, 6)
        _vs_lay.setSpacing(6)
        self.btnVsExport = QtWidgets.QPushButton("Export")
        self.btnVsExport.setToolTip("Load DEM/DSM to enable export")
        self.btnVsExport.setEnabled(False)
        self.btnOpenWodmi = QtWidgets.QPushButton("Open in WODMI")
        self.btnOpenWodmi.setEnabled(False)
        self.lblVsExportStatus = QtWidgets.QLabel("—")
        self.lblVsExportStatus.setStyleSheet("color: #888; font-size: 8pt;")
        self.btnVsCancel = QtWidgets.QPushButton("Cancel")
        self.btnVsCancel.setEnabled(False)
        _vs_lay.addWidget(self.btnVsExport)
        _vs_lay.addWidget(self.btnOpenWodmi)
        _vs_lay.addWidget(self.lblVsExportStatus, 1)
        _vs_lay.addWidget(self.btnVsCancel)

        # .ui 由来の旧テキストフィールドを非表示
        for _w in (self.lblTileUrl, self.txtTileUrl, self.btnLoadTerrain):
            _w.hide()

        self.leftTabs = QtWidgets.QTabWidget(self.leftPane)

        # ── 地形データ設定タブ ───────────────────────────────────────────
        self.tabDataSettings = QtWidgets.QWidget()
        _ds_scroll = QtWidgets.QScrollArea(self.tabDataSettings)
        _ds_scroll.setWidgetResizable(True)
        _ds_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        _ds_outer = QtWidgets.QVBoxLayout(self.tabDataSettings)
        _ds_outer.setContentsMargins(0, 0, 0, 0)
        _ds_outer.addWidget(_ds_scroll)
        _ds_inner = QtWidgets.QWidget()
        _ds_scroll.setWidget(_ds_inner)
        ds_layout = QtWidgets.QVBoxLayout(_ds_inner)
        ds_layout.addWidget(self.grpDem)
        ds_layout.addWidget(self.grpVsExport)
        ds_layout.addWidget(self.grpLayers)

        grpScipy = QtWidgets.QGroupBox(
            "scipy required (Flow Buffer / DEM resampling / SHC)"
        )
        scipy_lay = QtWidgets.QVBoxLayout(grpScipy)
        scipy_lay.setSpacing(4)
        for text, style in (
            ("- Without scipy, Flow Buffer display, fine-resolution DEM "
             "resampling, and SHC computation do not work. Other features "
             "are unaffected.", "font-size: 8pt; color: #cc6600;"),
            ("- Windows (standard installer): scipy is normally already "
             "included. If it's genuinely missing, close QGIS and run in "
             "Command Prompt from your QGIS install's bin folder:",
             "font-size: 8pt; color: #555;"),
            ("      python-qgis-ltr.bat -m pip install --user scipy",
             "font-size: 8pt; color: #333; font-family: monospace;"),
            ("  (filename may be python-qgis.bat depending on your "
             "install; adjust the path to match your QGIS version.)",
             "font-size: 8pt; color: #555;"),
            ("- Linux: close QGIS and run in a terminal:",
             "font-size: 8pt; color: #555;"),
            ("      sudo apt install python3-scipy",
             "font-size: 8pt; color: #333; font-family: monospace;"),
            ("- To check what's actually loaded after installing, in "
             "QGIS's Python Console run:", "font-size: 8pt; color: #555;"),
            ("      import numpy, scipy", "font-size: 8pt; color: #333; font-family: monospace;"),
            ("      print(numpy.__file__, numpy.__version__)", "font-size: 8pt; color: #333; font-family: monospace;"),
            ("      print(scipy.__file__, scipy.__version__)", "font-size: 8pt; color: #333; font-family: monospace;"),
        ):
            lbl = QtWidgets.QLabel(text)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(style)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            scipy_lay.addWidget(lbl)
        try:
            import scipy  # noqa: F401
            grpScipy.setVisible(False)
        except ImportError:
            pass
        ds_layout.addWidget(grpScipy)

        grpHint = QtWidgets.QGroupBox("Data Setup")
        hint_lay = QtWidgets.QVBoxLayout(grpHint)
        hint_lay.setSpacing(4)
        _warn = QtWidgets.QLabel(
            "⚠ DEM must be in a projected CRS (metres), not geographic (degrees).\n"
            "  Using EPSG:4326 (lat/lon) will produce incorrect slope, area and flow values."
        )
        _warn.setWordWrap(True)
        _warn.setStyleSheet("color: #cc0000; font-size: 8pt;")
        hint_lay.addWidget(_warn)
        for text in (
            "- Select a general-purpose DEM or one covering the analysis area.",
            "- Select a DSM/DTM that covers the same area as the DEM.",
            "- The elevation source should match the DEM extent.",
            "",
            "[TIF / ZIP Layout]",
            "- Single TIF: select the file directly.",
            "- Single ZIP (containing a TIF): select the ZIP directly.",
            "- Multiple ZIPs (same region): place all ZIPs in one folder and select that folder.\n"
            "  A merged_<folder>.tif is auto-generated in the parent folder.",
            "- Reuse merged TIF: select the generated merged_*.tif to skip re-merging.",
        ):
            lbl = QtWidgets.QLabel(text)
            lbl.setWordWrap(True)
            hint_lay.addWidget(lbl)
        ds_layout.addWidget(grpHint)

        # ── Tips アコーディオン ──────────────────────────────────
        _btn_tips = QtWidgets.QPushButton("▶  Tips")
        _btn_tips.setCheckable(True)
        _btn_tips.setChecked(False)
        _btn_tips.setStyleSheet(
            "QPushButton { text-align:left; padding:4px 6px; font-size:8pt; "
            "background:#e8e8e8; border:1px solid #ccc; border-radius:3px; }"
            "QPushButton:checked { background:#d0d8e8; }"
        )
        ds_layout.addWidget(_btn_tips)

        _tips_body = QtWidgets.QWidget()
        _tips_lay = QtWidgets.QVBoxLayout(_tips_body)
        _tips_lay.setContentsMargins(8, 4, 8, 4)
        _tips_lay.setSpacing(3)
        for _t in (
            "DEM source selection",
            "  • GSI tiles (Japan): auto-fetched from canvas extent. No download needed.",
            "  • Local file: must be in a projected CRS (metres). Reproject if in EPSG:4326.",
            "  • Copernicus GLO-30: free account required at OpenTopography to download.",
            "",
            "Reproject in QGIS (if needed)",
            "  1. Raster → Projections → Warp (Reproject)",
            "  2. Target CRS — UTM zone for your area:",
            "     lon 126–132° → EPSG:32653",
            "     lon 132–138° → EPSG:32654",
            "     lon 138–144° → EPSG:32655",
            "  3. Resampling: Bilinear  →  Save & use the output file",
            "",
            "Multiple ZIPs (same region)",
            "  • Place all ZIPs in one folder → select that folder.",
            "  • merged_<folder>.tif is auto-generated in the parent folder.",
            "  • To reuse: select the generated merged_*.tif directly.",
            "",
            "Global analysis — CRS recommendations",
            "  • Set project CRS to the UTM zone covering your area.",
            "  • UTM is defined up to ±84° latitude. Beyond ±70° accuracy degrades.",
            "  • Areas above ±70° (Arctic/Antarctic) are outside the intended use range.",
            "  • South America example: EPSG:32718 (Zone 18S) or EPSG:32719 (Zone 19S).",
            "  • AWS Terrarium tiles work worldwide but resolution is ~30 m equivalent.",
        ):
            _tl = QtWidgets.QLabel(_t)
            _tl.setWordWrap(True)
            _tl.setStyleSheet("font-size:8pt;" + ("color:#555;" if _t.startswith("  ") else "font-weight:bold; color:#333;") if _t else "")
            _tips_lay.addWidget(_tl)
        _tips_body.setVisible(False)
        ds_layout.addWidget(_tips_body)

        def _toggle_tips(checked):
            _btn_tips.setText(("▼" if checked else "▶") + "  Tips")
            _tips_body.setVisible(checked)
        _btn_tips.toggled.connect(_toggle_tips)

        _lbl_credit = QtWidgets.QLabel("Developed by Avid Tree Work")
        _lbl_credit.setAlignment(Qt.AlignCenter)
        _lbl_credit.setStyleSheet("color: #888; font-size: 8pt; padding: 4px 0;")
        ds_layout.addWidget(_lbl_credit)
        ds_layout.addStretch()

        self.tabTerrain = QtWidgets.QWidget()
        self._build_terrain_tab(self.tabTerrain)
        # タブ順: 地形データ設定 → 地形解析
        self.leftTabs.addTab(self.tabDataSettings, "Terrain Data")
        self.leftTabs.addTab(self.tabTerrain,      "Analysis")

        import pathlib
        _fol_uri = pathlib.Path(os.path.dirname(__file__), "data", "FOL.html").as_uri()
        _corner_lnk = QtWidgets.QLabel(f'<a href="{_fol_uri}">Help</a>')
        _corner_lnk.setOpenExternalLinks(True)
        _corner_lnk.setToolTip("Open FOL documentation")
        _corner_lnk.setContentsMargins(0, 0, 4, 0)
        self.leftTabs.setCornerWidget(_corner_lnk, Qt.TopRightCorner)

        left_layout.addWidget(self.leftTabs)

        # ── 解析データの表示管理（プレビュー上部・2行）────────────────
        grpDisplayMgmt = QtWidgets.QGroupBox("Analysis Layer Display")
        self._grpDisplayMgmt = grpDisplayMgmt  # 解析中は操作を止める
        dm_vlay = QtWidgets.QVBoxLayout(grpDisplayMgmt)
        dm_vlay.setContentsMargins(6, 2, 6, 4)
        dm_vlay.setSpacing(2)
        # Row1: 解析番号セレクタ
        dm_row1 = QtWidgets.QHBoxLayout()
        dm_row1.setSpacing(4)
        dm_row1.addWidget(self.cmbAnalysisNumber)
        dm_row1.addWidget(self.btnRefreshAnalysis)
        # 選んだ解析をプロジェクトへ書き出すボタンは、選択欄・更新ボタンのすぐ右
        dm_row1.addWidget(self.btnExport)
        dm_row1.addSpacing(20)
        dm_row1.addWidget(self.lblLoadStatus, 1)
        dm_vlay.addLayout(dm_row1)
        # Row2: 種別トグルボタン + 透過率
        dm_row2 = QtWidgets.QHBoxLayout()
        dm_row2.setSpacing(2)
        dm_row2.addWidget(self.btnTerrainToggle)
        dm_row2.addSpacing(6)
        for _b, _sp, _fb, _fb2 in (
            (self.chkLoadValley,     self.spinOpacityValley,     None,                   None),
            (self.chkLoadWetland,    self.spinOpacityWetland,    self.btnFilterWetland,  None),
            (self.chkLoadFlow,       self.spinOpacityFlow,       self.btnFilterFlow,     self.btnFlowBuffer),
            (self.chkLoadIntegrated, self.spinOpacityIntegrated, None,                   None),
        ):
            dm_row2.addWidget(_b)
            if _fb2 is not None:
                dm_row2.addWidget(_fb2)   # 流量推測ボタンと透過率の間
            dm_row2.addWidget(_sp)
            if _fb is not None:
                dm_row2.addWidget(_fb)
            dm_row2.addSpacing(4)
        dm_row2.addStretch(1)
        self.chkMapLock = QtWidgets.QCheckBox("Lock Map")
        dm_row2.addWidget(self.chkMapLock)
        dm_vlay.addLayout(dm_row2)
        # Row3: レイヤー表示 ON/OFF + 透過率（GPKG / Tile / Background）
        dm_row3 = QtWidgets.QHBoxLayout()
        dm_row3.setSpacing(2)
        for _b, _sp in (
            (self.btnGpkgLayerVis, self.spinGpkgOpacity),
            (self.btnTileLayerVis, self.spinTileOpacity),
            (self.btnBgLayerVis,   self.spinBgOpacity),
            (self.chkLoadStability,  self.spinOpacityStability),
        ):
            dm_row3.addWidget(_b)
            dm_row3.addWidget(_sp)
            dm_row3.addSpacing(2)
        dm_row3.addStretch(1)
        dm_vlay.addLayout(dm_row3)

        right_layout.addWidget(grpDisplayMgmt)
        right_layout.addWidget(self.grpPreviewCanvas, stretch=1)

        self.mainSplitter.setStretchFactor(0, 1)
        self.mainSplitter.setStretchFactor(1, 1)
        self.mainSplitter.setSizes([480, 720])
        self.verticalLayout.addWidget(self.mainSplitter)

    def _build_terrain_tab(self, parent):
        scroll = QtWidgets.QScrollArea(parent)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        outer = QtWidgets.QVBoxLayout(parent)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        inner = QtWidgets.QWidget()
        scroll.setWidget(inner)
        lay = QtWidgets.QVBoxLayout(inner)
        lay.setSpacing(6)

        # 地表データグループは _build_extended_ui で作成済み（tabDataSettings に配置）

        # 解析読込セクションはプレビュー上部の「解析データの表示管理」バーに移動済み。
        # chkLoad* / spinOpacity* / lblLoadStatus は _build_extended_ui で先行作成。
        self._loaded_terrain_layers = {}   # key → [layer_id, ...]
        self._terrain_cycle_state = {}     # key → int (-1=非表示, 0..N-1=表示中ファイル番号)
        self._exclusive_hidden = {}        # 排他非表示中のキー → 保存済みcycle state
        self._filter_state = {"wetland": "off", "flow": "off"}  # off/low/mid
        self._flow_buffer_state = "off"  # off/weak/strong
        self._filter_state_tc = "off"  # Time load (tc) 専用 off/low/mid
        self._flow_buffer_state_tc = "off"  # Time load 用バッファ off/weak/strong
        self._flow_buffer_layer_ids = []  # バッファ滲みレイヤーのID管理
        self._flow_buffer_mem_paths = []  # vsimem パス（解放用）
        self._loaded_terrain_basenames = {}  # key → base_name（フィルタ再適用用）
        self._terrain_layer_group = None   # QgsLayerTreeGroup or None
        self._orig_base_opacity = {}       # layer_id → 元 opacity（Layer Settings 選択レイヤーの復元用）

        # --- 解析種別 ---
        grpTypes = QtWidgets.QGroupBox("Analysis Types")
        self._grpAnalysisTypes = grpTypes  # 解析中は操作を止める
        types_lay = QtWidgets.QHBoxLayout(grpTypes)
        types_lay.setContentsMargins(6, 6, 6, 6)
        self.chkStability = QtWidgets.QCheckBox("Slope Stability")
        self.chkValley    = QtWidgets.QCheckBox("Valley Terrain")
        self.chkFlow      = QtWidgets.QCheckBox("Flow Estimation")
        self.chkStability.setChecked(True)
        self.chkValley.setChecked(True)
        for chk in (self.chkStability, self.chkValley, self.chkFlow):
            types_lay.addWidget(chk)
        lay.addWidget(grpTypes)

        # --- 斜面安定 パラメータ ---
        self.grpParamStability = QtWidgets.QGroupBox("Slope Stability Parameters")
        ps_lay = QtWidgets.QGridLayout(self.grpParamStability)
        ps_lay.setVerticalSpacing(2)
        ps_lay.setColumnStretch(0, 1)
        self.spinPhiDeg = QtWidgets.QDoubleSpinBox()
        self.spinPhiDeg.setRange(0, 60); self.spinPhiDeg.setValue(30); self.spinPhiDeg.setSuffix("°")
        self.spinCKpa   = QtWidgets.QDoubleSpinBox()
        self.spinCKpa.setRange(0, 200);  self.spinCKpa.setValue(3);  self.spinCKpa.setSuffix(" kPa")
        self.spinZm     = QtWidgets.QDoubleSpinBox()
        self.spinZm.setRange(0.1, 10);   self.spinZm.setValue(0.5); self.spinZm.setSuffix(" m")
        self.spinMSat   = QtWidgets.QDoubleSpinBox()
        self.spinMSat.setRange(0, 1);    self.spinMSat.setValue(0.5); self.spinMSat.setSingleStep(0.1)
        self.spinFsThresh = QtWidgets.QDoubleSpinBox()
        self.spinFsThresh.setRange(0.5, 3.0); self.spinFsThresh.setValue(1.2); self.spinFsThresh.setSingleStep(0.1)
        _ps_defs = [
            ("内部摩擦角 φ'",       "Friction angle φ'",  self.spinPhiDeg,   "Loam: 30–35°, gravel mix: 38°+"),
            ("粘着力 c'",            "Cohesion c'",         self.spinCKpa,     "Sandy young forest: 0, gravelly loam: 5 kPa"),
            ("土壌深度 z",           "Soil depth z",        self.spinZm,       "Depth to hard layer (change in color/hardness/grain size)"),
            ("飽和率 m",             "Saturation m",        self.spinMSat,     "0–1  (0=dry, 0.5=half-saturated, 1=fully saturated)"),
            ("FS 閾値（要注意以下）", "FS threshold",        self.spinFsThresh, "0.5–3.0  (caution ≤1.5, danger ≤1.0)"),
        ]
        for i, (ja, en, w, hint) in enumerate(_ps_defs):
            row = i * 2
            ql = QtWidgets.QLabel(en)
            ps_lay.addWidget(ql, row, 0)
            ps_lay.addWidget(w, row, 1)
            _hl = QtWidgets.QLabel(hint)
            _hl.setStyleSheet("color:#888;font-size:8pt;margin-bottom:10px;")
            ps_lay.addWidget(_hl, row + 1, 0, 1, 2)
        ps_lay.setRowStretch(len(_ps_defs) * 2, 1)

        # --- 沢地形 パラメータ ---
        self.grpParamValley = QtWidgets.QGroupBox("Valley Terrain (TWI) Parameters")
        pv_lay = QtWidgets.QGridLayout(self.grpParamValley)
        pv_lay.setVerticalSpacing(2)
        pv_lay.setColumnStretch(0, 1)

        # --- Flow direction method (mutually exclusive) ---
        pv_lay.addWidget(QtWidgets.QLabel("Flow Direction Method (Valley/TWI only)"), 0, 0, 1, 2)
        _fm_col = QtWidgets.QVBoxLayout()
        _fm_col.setSpacing(1)
        self.chkFlowMFD  = QtWidgets.QCheckBox("")
        self.chkFlowDinf = QtWidgets.QCheckBox("")
        self.chkFlowD8   = QtWidgets.QCheckBox("")
        self._grpFlowMethod = QtWidgets.QButtonGroup(self)
        self._grpFlowMethod.setExclusive(True)
        _fm_defs = [
            (self.chkFlowMFD,  "MFD", "Multi-direction · smoothest wetness surface, but valleys look fuzzy"),
            (self.chkFlowDinf, "D∞",  "Balanced · no striping, valleys stay fairly sharp"),
            (self.chkFlowD8,   "D8",  "Single steepest · crisp single-line channels, but wetness shows stripes"),
        ]
        for _chk, _name, _desc in _fm_defs:
            self._grpFlowMethod.addButton(_chk)
            _chk.setToolTip(_desc)
            _row = QtWidgets.QHBoxLayout()
            _nl = QtWidgets.QLabel(_name)           # name (left-aligned)
            _nl.setToolTip(_desc)
            _row.addWidget(_nl)
            _row.addStretch(1)
            _row.addWidget(_chk)                    # checkbox (right-aligned)
            _fm_col.addLayout(_row)
            _dl = QtWidgets.QLabel(_desc)           # description (next line)
            _dl.setWordWrap(True)
            _dl.setStyleSheet("color:#888;font-size:8pt;margin-bottom:4px;")
            _fm_col.addWidget(_dl)
        self.chkFlowD8.setChecked(True)   # default = current behaviour
        pv_lay.addLayout(_fm_col, 1, 0, 1, 2)

        self.spinTwiThresh = QtWidgets.QDoubleSpinBox()
        self.spinTwiThresh.setRange(1, 20); self.spinTwiThresh.setValue(8.0); self.spinTwiThresh.setSingleStep(0.5)
        self.spinMinArea = QtWidgets.QDoubleSpinBox()
        self.spinMinArea.setRange(100, 100000); self.spinMinArea.setValue(1000); self.spinMinArea.setSuffix(" m²"); self.spinMinArea.setSingleStep(500)
        _pv_defs = [
            ("TWI Threshold",       self.spinTwiThresh, "1–20  (higher = more wetland areas captured)"),
            ("Min. Catchment Area", self.spinMinArea,   "100–100000 m²  (smaller = finer stream network)"),
        ]
        for i, (en, w, hint) in enumerate(_pv_defs):
            row = 2 + i * 2                     # rows 0–1 used by flow-method selector
            ql = QtWidgets.QLabel(en)
            pv_lay.addWidget(ql, row, 0)
            pv_lay.addWidget(w, row, 1)
            _hl = QtWidgets.QLabel(hint)
            _hl.setStyleSheet("color:#888;font-size:8pt;margin-bottom:10px;")
            pv_lay.addWidget(_hl, row + 1, 0, 1, 2)
        pv_lay.setRowStretch(2 + len(_pv_defs) * 2, 1)

        # --- 流量 パラメータ ---
        self.grpParamFlow = QtWidgets.QGroupBox("Flow (Rational Method) Parameters")
        pf_lay = QtWidgets.QGridLayout(self.grpParamFlow)
        pf_lay.setVerticalSpacing(2)
        pf_lay.setColumnStretch(0, 1)
        self.spinRainfall = QtWidgets.QDoubleSpinBox()
        self.spinRainfall.setRange(1, 500); self.spinRainfall.setValue(50); self.spinRainfall.setSuffix(" mm/h")
        self.spinRainfall.setToolTip(
            "Peak rainfall intensity i_peak [mm/h] for the design storm.\n"
            "Must be >= total / duration (mean intensity); lower values are clamped.")
        self.spinRunoff   = QtWidgets.QDoubleSpinBox()
        self.spinRunoff.setRange(0.1, 1.0); self.spinRunoff.setValue(0.8); self.spinRunoff.setSingleStep(0.05)
        self.spinRunoff.setToolTip("Runoff coefficient C")
        self.spinTotalRainfall = QtWidgets.QDoubleSpinBox()
        self.spinTotalRainfall.setRange(1, 2000); self.spinTotalRainfall.setValue(100); self.spinTotalRainfall.setSuffix(" mm")
        self.spinTotalRainfall.setToolTip(
            "Total rainfall depth for the event [mm]. Sets the design storm volume\n"
            "(Mean load, and internal Total volume).")
        self.spinDuration = QtWidgets.QDoubleSpinBox()
        self.spinDuration.setRange(0.5, 72); self.spinDuration.setValue(6.0); self.spinDuration.setSuffix(" h"); self.spinDuration.setSingleStep(0.5)
        self.spinDuration.setToolTip(
            "Storm duration [h]. Drives the design hyetograph and the time-area\n"
            "convolution (Clark unit hydrograph).")
        # 3値の受付範囲を相互整合で動的更新（peak >= total / duration）
        self.spinRainfall.valueChanged.connect(
            lambda _v: self._sync_rain_bounds("peak"))
        self.spinTotalRainfall.valueChanged.connect(
            lambda _v: self._sync_rain_bounds("total"))
        self.spinDuration.valueChanged.connect(
            lambda _v: self._sync_rain_bounds("duration"))
        self.spinVelocityCoef = QtWidgets.QDoubleSpinBox()
        self.spinVelocityCoef.setRange(0.01, 5.0); self.spinVelocityCoef.setValue(0.3)
        self.spinVelocityCoef.setSingleStep(0.05); self.spinVelocityCoef.setDecimals(2)
        self.spinVelocityCoef.setToolTip("Flow velocity coef. v_coef [m/s]  velocity = v_coef × tan(slope)^0.5\nForest: 0.3, grass: 0.6, pavement: 1.5")
        self._lbl_vel_coef = QtWidgets.QLabel("Flow velocity coef.")
        self._lbl_runoff_coef = QtWidgets.QLabel("Runoff coef. C")
        _pf_defs = [
            ("i_peak",              self.spinRainfall,      "Peak rainfall intensity  1–500 mm/h"),
            (self._lbl_runoff_coef,  self.spinRunoff,        "0.1–1.0  (forest 0.3, grass 0.6, bare soil 0.9)"),
            ("Total rainfall",      self.spinTotalRainfall, "Total precipitation for the period  1–2000 mm"),
            ("Duration T",          self.spinDuration,      "Rainfall duration vs. Tc  0.5–72 h"),
            (self._lbl_vel_coef,    self.spinVelocityCoef,  "Forest 0.3, grass 0.6, pavement 1.5  (0.01–5.0 m/s)"),
        ]
        self._rain_hint = {}   # spinbox -> 動的更新するグレーの注記ラベル
        self._flow_coef_hints = {}
        for i, (en, w, hint) in enumerate(_pf_defs):
            row = i * 2
            ql = en if isinstance(en, QtWidgets.QLabel) else QtWidgets.QLabel(en)
            pf_lay.addWidget(ql, row, 0)
            pf_lay.addWidget(w, row, 1)
            _hl = QtWidgets.QLabel(hint)
            _hl.setStyleSheet("color:#888;font-size:8pt;margin-bottom:10px;")
            pf_lay.addWidget(_hl, row + 1, 0, 1, 2)
            if w in (self.spinRainfall, self.spinTotalRainfall, self.spinDuration):
                self._rain_hint[w] = _hl
            if w in (self.spinRunoff, self.spinVelocityCoef):
                self._flow_coef_hints[w] = (_hl, hint)
        self._sync_rain_bounds("peak")   # 初期の受付範囲・注記を反映
        pf_lay.setRowStretch(len(_pf_defs) * 2, 1)

        # --- パラメータ タブ ---
        self.tabParams = QtWidgets.QTabWidget()
        self.tabParams.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Minimum)
        self.grpParamPlaceholder = QtWidgets.QGroupBox("Parameters")
        _ph_lay = QtWidgets.QVBoxLayout(self.grpParamPlaceholder)
        _ph_lbl = QtWidgets.QLabel("Select analysis types above")
        _ph_lbl.setAlignment(Qt.AlignCenter)
        _ph_lbl.setStyleSheet("color:#888;font-size:9pt;padding:12px 0;")
        _ph_lay.addWidget(_ph_lbl)
        lay.addWidget(self.tabParams)
        lay.addWidget(self.grpParamPlaceholder)

        # --- 解析条件 ---
        self.grpAnalysisCondition = QtWidgets.QGroupBox("Analysis Conditions")
        self.grpAnalysisCondition.setStyleSheet(
            "QGroupBox{border:1px solid #ccc;border-radius:3px;margin-top:6px;padding:4px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:6px;}"
        )
        _ac_lay = QtWidgets.QVBoxLayout(self.grpAnalysisCondition)
        _ac_lay.setContentsMargins(4, 2, 4, 4)
        self.lblAnalysisCondition = QtWidgets.QLabel("Select a run to view conditions")
        self.lblAnalysisCondition.setWordWrap(True)
        self.lblAnalysisCondition.setStyleSheet("color:#555;font-size:8pt;")
        _ac_lay.addWidget(self.lblAnalysisCondition)
        lay.addWidget(self.grpAnalysisCondition)

        # --- 解析制限 ---
        grpAreaLimit = QtWidgets.QGroupBox("Area Limit")
        grpAreaLimit.setStyleSheet(
            "QGroupBox{border:1px solid #ccc;border-radius:3px;margin-top:6px;padding:4px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:6px;}"
        )
        _al_lay = QtWidgets.QHBoxLayout(grpAreaLimit)
        _al_lay.setContentsMargins(4, 2, 4, 4)
        _al_lay.setSpacing(4)
        # 左カラム
        _al_left = QtWidgets.QWidget()
        self._areaLimitControls = _al_left  # 解析中は操作を止める（Stop は別）
        _al_left_lay = QtWidgets.QHBoxLayout(_al_left)
        _al_left_lay.setContentsMargins(0, 0, 0, 0)
        _al_left_lay.setSpacing(4)
        _al_left_lay.addWidget(QtWidgets.QLabel("Warn above"))
        self.cmbAreaLimit = QtWidgets.QComboBox()
        for _ha_val, _ha_lbl in [(30, "30"), (50, "50"), (150, "150"), (300, "300"),
                                  (800, "800"), (1500, "1500"), (2000, "2000"),
                                  (0, "No Limit")]:
            self.cmbAreaLimit.addItem(_ha_lbl, _ha_val)
        self.cmbAreaLimit.setCurrentIndex(1)  # デフォルト: 50ha
        _al_left_lay.addWidget(self.cmbAreaLimit)
        _al_left_lay.addWidget(QtWidgets.QLabel("ha"))
        _al_left.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        _al_lay.addWidget(_al_left, 2)
        # Resample インジケーター（解像度 < 0.2m のとき表示）
        self.lblResample = QtWidgets.QLabel("Resample")
        self.lblResample.setAlignment(Qt.AlignCenter)
        self.lblResample.setStyleSheet(
            "QLabel{background:#27ae60;color:white;border-radius:3px;"
            "padding:2px 5px;font-size:8pt;}"
        )
        self.lblResample.setVisible(False)
        self.lblResample.setToolTip(
            f"解像度が {_RESAMPLE_THRESHOLD}m 未満のため、"
            f"解析前に {_RESAMPLE_TARGET}m へ自動リサンプルします。"
        )
        _al_lay.addWidget(self.lblResample)
        # 右カラム（Stop ボタン）
        self.btnStopAnalysis = QtWidgets.QPushButton("Stop")
        self.btnStopAnalysis.setEnabled(False)
        self.btnStopAnalysis.setStyleSheet(
            "QPushButton:enabled{background:#c0392b;color:white;border-radius:3px;}"
            "QPushButton:disabled{color:#aaa;}"
        )
        _al_lay.addWidget(self.btnStopAnalysis, 1)
        lay.addWidget(grpAreaLimit)

        # --- 出力 ---
        grpOut = QtWidgets.QGroupBox("Output")
        out_lay = QtWidgets.QVBoxLayout(grpOut)
        self.lblOutDir = _ElidedPathLabel()
        self.lblOutDir.setStyleSheet("color:#555;font-size:8pt;text-decoration:underline;")
        self._update_out_dir_label()
        out_lay.addWidget(self.lblOutDir)
        self.chkOverwrite = QtWidgets.QCheckBox("Overwrite existing files")
        self.chkOverwrite.setChecked(True)
        out_lay.addWidget(self.chkOverwrite)
        # 高さは他のボタン（Export 等）と同じ標準の高さにする
        self.btnRunAnalysis = QtWidgets.QPushButton("Run Analysis")
        out_lay.addWidget(self.btnRunAnalysis)
        # 解析中だけ btnRunAnalysis と入れ替えて出す（_ButtonProgress）
        self.progressAnalysis = QtWidgets.QProgressBar()
        self.progressAnalysis.setRange(0, 100)
        self.progressAnalysis.setVisible(False)
        out_lay.addWidget(self.progressAnalysis)
        self.lblAnalysisStatus = QtWidgets.QLabel("Ready")
        self.lblAnalysisStatus.setWordWrap(True)
        out_lay.addWidget(self.lblAnalysisStatus)
        lay.addWidget(grpOut)

        # --- エクスポート（選んだ解析のレイヤーをプロジェクトへ書き出す）---
        # 選択中の解析番号の全レイヤーを、プラグイン管理グループとは別の
        # 独立した永続グループとして書き出す。ボタンは右パネル上部
        # （Analysis Layer Display）の1行目、選択欄・更新ボタンの右に置く
        # （_build_extended_ui で配置）。ここでは作るだけ。
        self.btnExport = QtWidgets.QPushButton("Export to Project")
        self.btnExport.setToolTip(
            "Add all layers of the selected analysis run to the project as a\n"
            "separate group, with the current display settings. The group stays\n"
            "after switching runs or closing the plugin.")

        _lbl_credit_a = QtWidgets.QLabel("Developed by Avid Tree Work")
        _lbl_credit_a.setAlignment(Qt.AlignCenter)
        _lbl_credit_a.setStyleSheet("color: #888; font-size: 8pt; padding: 4px 0;")
        lay.addWidget(_lbl_credit_a)
        lay.addStretch(1)

        # チェックボックス連動でパラメータタブを更新
        for _chk in (self.chkStability, self.chkValley, self.chkFlow):
            _chk.toggled.connect(self._sync_param_tabs)
        # タブバー空白 + パラメータ表示領域の背景クリックで次タブへ
        self.tabParams.tabBar().installEventFilter(self)
        for _grp in (self.grpParamStability, self.grpParamValley,
                     self.grpParamFlow):
            _grp.installEventFilter(self)
        # ヒントセクション連動

        self._sync_param_tabs()


    def eventFilter(self, obj, event):
        """タブバー空白 / パラメータ表示領域背景クリックで次タブへ切り替える。"""
        if event.type() == QEvent.MouseButtonPress and self.tabParams.count() > 0:
            nxt = (self.tabParams.currentIndex() + 1) % self.tabParams.count()
            # タブバーの空白部分
            if obj is self.tabParams.tabBar():
                if self.tabParams.tabBar().tabAt(event.pos()) == -1:
                    self.tabParams.setCurrentIndex(nxt)
                    return True
            # パラメータグループの背景部分（子ウィジェット上のクリックは通過）
            elif obj in (self.grpParamStability, self.grpParamValley,
                         self.grpParamFlow):
                self.tabParams.setCurrentIndex(nxt)
                return True
        return super().eventFilter(obj, event)


    def _sync_param_tabs(self):
        """解析種別チェックに応じてパラメータタブを更新する。"""
        ITEMS = [
            (self.chkStability, self.grpParamStability, "Slope Stability"),
            (self.chkValley,    self.grpParamValley,    "Valley Terrain"),
            (self.chkFlow,      self.grpParamFlow,      "Flow"),
        ]
        # 現在のタブ構成を一旦クリア（ウィジェットは破棄しない）
        while self.tabParams.count() > 0:
            self.tabParams.removeTab(0)
        for chk, widget, label in ITEMS:
            if chk.isChecked():
                self.tabParams.addTab(widget, label)
        has_tabs = self.tabParams.count() > 0
        self.tabParams.setVisible(has_tabs)
        self.grpParamPlaceholder.setVisible(not has_tabs)

    def _flash_widget(self, w):
        """入力欄の背景を短く点滅させて「制約で丸められた」ことを知らせる。"""
        from qgis.PyQt.QtCore import QTimer
        base = getattr(w, "_flash_base_ss", None)
        if base is None:
            base = w.styleSheet()
            w._flash_base_ss = base
        seq = ["background:#ffcf5c;", "background:#fff0c4;",
               "background:#ffcf5c;", base]
        for i, ss in enumerate(seq):
            QTimer.singleShot(i * 190, lambda s=ss: w.setStyleSheet(s))

    def _sync_rain_bounds(self, which):
        """i_peak / total / duration の受付範囲を相互整合で動的更新する（モデル II）。

        物理制約: i_peak >= total / duration。これを 3 欄それぞれの min/max
        （他2値から算出）に落とし込む。範囲外の入力は Qt 側で最も近い有効値へ
        丸められる。編集した欄が制約由来の限界に張り付いたら背景を点滅させ、
        各欄下の注記に現在の有効範囲をライブ表示する。他の2欄は動かさない。
        """
        if getattr(self, "_rain_bounds_busy", False):
            return
        self._rain_bounds_busy = True
        try:
            IP, TOT, DUR = (1.0, 500.0), (1.0, 2000.0), (0.5, 72.0)
            ip = self.spinRainfall.value()
            tot = self.spinTotalRainfall.value()
            dur = self.spinDuration.value()

            specs = [
                (self.spinRainfall, "peak", IP,
                 min(max(IP[0], tot / max(dur, DUR[0])), IP[1]), IP[1],
                 ">= total/duration", "mm/h"),
                (self.spinTotalRainfall, "total", TOT,
                 TOT[0], max(min(TOT[1], ip * max(dur, DUR[0])), TOT[0]),
                 "<= i_peak x duration", "mm"),
                (self.spinDuration, "duration", DUR,
                 min(max(DUR[0], tot / max(ip, IP[0])), DUR[1]), DUR[1],
                 ">= total/i_peak", "h"),
            ]
            for sb, key, hard, lo, hi, rule, unit in specs:
                with QSignalBlocker(sb):
                    sb.setRange(lo, hi)
                hl = self._rain_hint.get(sb)
                if hl is not None:
                    hl.setText(
                        '<span style="color:#888;">{}</span>&nbsp;&nbsp;'
                        '<span style="color:#d9822b;">{:g}-{:g} {}</span>'.format(
                            rule, sb.minimum(), sb.maximum(), unit))
                if key == which and not getattr(self, "_initializing", False):
                    v = sb.value()
                    at_dyn_min = (sb.minimum() > hard[0] + 1e-9
                                  and abs(v - sb.minimum()) < 1e-6)
                    at_dyn_max = (sb.maximum() < hard[1] - 1e-9
                                  and abs(v - sb.maximum()) < 1e-6)
                    if at_dyn_min or at_dyn_max:
                        self._flash_widget(sb)
        finally:
            self._rain_bounds_busy = False

    def _connect_extended_signals(self):
        self.btnRefreshLayerList.clicked.connect(self._refresh_layer_combos)
        self._setup_canvas_sync()
        self.chkStandaloneWindow.toggled.connect(self._apply_window_mode)
        self.chkMapLock.toggled.connect(self._on_map_lock_toggled)
        self.btnBrowseDem.clicked.connect(self._on_browse_dem)
        self.btnBrowseDsm.clicked.connect(self._on_browse_dsm)
        self.btnVsExport.clicked.connect(self._on_vs_export)
        self.btnOpenWodmi.clicked.connect(self._on_open_wodmi)
        self.btnVsCancel.clicked.connect(self._on_vs_cancel)
        self.btnRunAnalysis.clicked.connect(self._run_terrain_analysis)
        self.btnExport.clicked.connect(self._on_export_clicked)
        self.btnStopAnalysis.clicked.connect(self._on_stop_analysis)
        self.chkLoadStability.clicked.connect(lambda: self._cycle_terrain_layer("stability"))
        self.chkLoadValley.clicked.connect(lambda: self._cycle_terrain_layer("valley"))
        self.chkLoadWetland.clicked.connect(lambda: self._cycle_terrain_layer("wetland"))
        self.chkLoadFlow.clicked.connect(lambda: self._cycle_terrain_layer("flow"))
        self.chkLoadIntegrated.clicked.connect(lambda: self._cycle_terrain_layer("integrated"))
        for _key, _sp in (
            ("stability",  self.spinOpacityStability),
            ("valley",     self.spinOpacityValley),
            ("wetland",    self.spinOpacityWetland),
            ("flow",       self.spinOpacityFlow),
            ("integrated", self.spinOpacityIntegrated),
        ):
            _sp.valueChanged.connect(
                lambda val, k=_key: self._on_key_opacity_changed(k, val)
            )
        self.cmbAnalysisNumber.currentIndexChanged.connect(self._on_analysis_number_changed)
        self.btnRefreshAnalysis.clicked.connect(self._refresh_analysis_combo)
        self.btnFilterWetland.clicked.connect(lambda: self._toggle_filter("wetland"))
        self.btnFilterFlow.clicked.connect(lambda: self._toggle_filter("flow"))
        self.btnFlowBuffer.clicked.connect(self._cycle_flow_buffer)
        self.btnTerrainToggle.toggled.connect(self._on_terrain_toggle)

    def _connect_interactive_signals(self):
        """Connect signals for user interactions after initial setup is complete."""
        self.cmbBackgroundLayer.currentIndexChanged.connect(
            lambda *_: self.apply_layer_display()
        )
        self.cmbTileLayer.currentIndexChanged.connect(lambda *_: self.apply_layer_display())
        self.cmbGpkgLayer.currentIndexChanged.connect(lambda *_: self.apply_layer_display())
        self.spinTileOpacity.valueChanged.connect(lambda *_: self.apply_layer_display())
        self.spinGpkgOpacity.valueChanged.connect(lambda *_: self.apply_layer_display())
        self.spinBgOpacity.valueChanged.connect(lambda *_: self.apply_layer_display())
        self.btnGpkgLayerVis.toggled.connect(lambda *_: self.apply_layer_display())
        self.btnTileLayerVis.toggled.connect(lambda *_: self.apply_layer_display())
        self.btnBgLayerVis.toggled.connect(lambda *_: self.apply_layer_display())

    def _refresh_layer_combos(self, *args):
        del args
        bg_data = [("None", "")]
        tile_data = [("None", "")]
        gpkg_data = [("None", "")]
        terrain_ids = {
            lid
            for ids in getattr(self, "_loaded_terrain_layers", {}).values()
            for lid in ids
        }
        for layer in QgsProject.instance().mapLayers().values():
            if layer.id() in terrain_ids:
                continue
            if layer.type() == layer.RasterLayer:
                bg_data.append((layer.name(), layer.id()))
                tile_data.append((layer.name(), layer.id()))
            elif layer.type() == layer.VectorLayer:
                gpkg_data.append((layer.name(), layer.id()))
        self._set_combo_data(self.cmbBackgroundLayer, bg_data)
        self._set_combo_data(self.cmbTileLayer, tile_data)
        self._set_combo_data(self.cmbGpkgLayer, gpkg_data)
        self._restore_layer_combos_if_unset()
        if self.preview_canvas is not None:
            if self.preview_canvas.width() == 0 or self.preview_canvas.height() == 0:
                self._pending_apply_layer_display = True
            else:
                self.apply_layer_display()

    @staticmethod
    def _set_combo_data(combo, data):
        current = combo.currentData()
        with QSignalBlocker(combo):
            combo.clear()
            for text, value in data:
                combo.addItem(text, value)
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)

    def _update_preview_status(self):
        if self.preview_canvas is None:
            return
        center = self.preview_canvas.center()
        scale = self.preview_canvas.scale()
        crs = self.preview_canvas.mapSettings().destinationCrs()
        crs_text = crs.authid() if crs.isValid() else "Unknown CRS"
        self.lblPreviewStatus.setText("{:.2f}, {:.2f}".format(center.x(), center.y()))
        self.lblPreviewStatusScale.setText("1 : {:,}".format(int(round(scale))))
        self.lblPreviewStatusCrs.setText(crs_text)
        # 面積（独立表示）
        try:
            import math as _math
            ext = self.preview_canvas.extent()
            w, h = ext.width(), ext.height()
            if crs.isValid() and crs.isGeographic():
                cy = (ext.yMinimum() + ext.yMaximum()) / 2.0
                mx = 111320.0 * _math.cos(_math.radians(abs(cy)))
                area_m2 = (w * mx) * (h * 111320.0)
            else:
                area_m2 = w * h
            area_ha = area_m2 / 1e4
            if area_ha >= 100:
                area_text = "{:.0f} ha".format(area_ha)
            elif area_ha >= 1:
                area_text = "{:.1f} ha".format(area_ha)
            else:
                area_text = "{:.2f} ha".format(area_ha)
        except Exception:
            area_text = "---"
        self.lblPreviewStatusArea.setText(area_text)

    def _setup_canvas_sync(self):
        if self.iface is not None:
            self.iface.mapCanvas().extentsChanged.connect(self._on_main_canvas_changed)
        self.preview_canvas.extentsChanged.connect(self._on_preview_canvas_changed)
        self.preview_canvas.extentsChanged.connect(self._update_preview_status)
        self._on_main_canvas_changed()

    def _sync_main_to_preview(self, force=False):
        if self._syncing or self.preview_canvas is None or self.iface is None:
            return
        if self._map_locked and not force:
            return  # ロック中はメイン→プレビュー同期をスキップ
        if self.preview_canvas.width() == 0 or self.preview_canvas.height() == 0:
            return  # 0px キャンバスへの zoom 操作は内部状態を壊すためスキップ
        self._syncing = True
        self.preview_canvas.blockSignals(True)
        try:
            main = self.iface.mapCanvas()
            self.preview_canvas.setDestinationCrs(main.mapSettings().destinationCrs())
            self.preview_canvas.setCenter(main.center())
            self.preview_canvas.zoomScale(main.scale())
            self.preview_canvas.refresh()
        finally:
            self.preview_canvas.blockSignals(False)
            self._syncing = False
        self._update_preview_status()

    def _on_main_canvas_changed(self):
        self._sync_main_to_preview(force=False)

    def _on_preview_canvas_changed(self):
        if self._initializing or self._syncing or self.preview_canvas is None:
            return
        if self._map_locked:
            return  # ロック中はプレビュー→メイン同期しない
        if self.preview_canvas.extent().isEmpty():
            return  # 空範囲はメインへ同期しない（1:1 への巻き込み防止）
        _cx = self.preview_canvas.center().x()
        _cy = self.preview_canvas.center().y()
        if _cx != _cx or _cy != _cy:
            return  # NaN 座標はメインへ同期しない（NaN != NaN が True になる性質を利用）
        if not getattr(self, "_preview_has_layers", False):
            return  # 表示レイヤがない場合はメインを動かさない
        if self.iface is None:
            return
        self._syncing = True
        try:
            main = self.iface.mapCanvas()
            main.setCenter(self.preview_canvas.center())
            main.zoomScale(self.preview_canvas.scale())
            main.refresh()
        finally:
            self._syncing = False

    def _get_analysis_extent(self):
        """現在の解析番号で表示中レイヤの範囲を返す。なければ None。"""
        analysis_number = self.cmbAnalysisNumber.currentData()
        if not analysis_number:
            return None
        out_dir = self._terrain_output_dir()
        from osgeo import gdal, ogr
        from qgis.core import QgsRectangle, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
        ext = None
        an_dir = os.path.join(out_dir, analysis_number)
        if not os.path.isdir(an_dir):
            return None
        canvas_crs = (self.preview_canvas.mapSettings().destinationCrs()
                      if self.preview_canvas is not None else None)
        for fname in os.listdir(an_dir):
            fpath = os.path.join(an_dir, fname)
            if fname.endswith(".tif"):
                ds = gdal.Open(fpath)
                if ds is None:
                    continue
                gt = ds.GetGeoTransform()
                w, h = ds.RasterXSize, ds.RasterYSize
                xmin = gt[0]
                xmax = gt[0] + gt[1] * w
                ymax = gt[3]
                ymin = gt[3] + gt[5] * h
                r = QgsRectangle(xmin, ymin, xmax, ymax)
                try:
                    src_crs = QgsCoordinateReferenceSystem()
                    src_crs.createFromWkt(ds.GetProjectionRef())
                    if (canvas_crs is not None and src_crs.isValid()
                            and canvas_crs.isValid() and src_crs != canvas_crs):
                        xf = QgsCoordinateTransform(src_crs, canvas_crs, QgsProject.instance())
                        r = xf.transformBoundingBox(r)
                except Exception:  # nosec B110
                    pass
                ds = None
                if r.isEmpty():
                    continue
                ext = r if ext is None else ext.combineExtentWith(r)
            elif fname.endswith(".gpkg"):
                ds = ogr.Open(fpath)
                if ds is None:
                    continue
                for i in range(ds.GetLayerCount()):
                    lyr = ds.GetLayerByIndex(i)
                    env = lyr.GetExtent()  # (xmin, xmax, ymin, ymax)
                    r = QgsRectangle(env[0], env[2], env[1], env[3])
                    try:
                        srs = lyr.GetSpatialRef()
                        if srs is not None:
                            src_crs = QgsCoordinateReferenceSystem()
                            src_crs.createFromWkt(srs.ExportToWkt())
                            if (canvas_crs is not None and src_crs.isValid()
                                    and canvas_crs.isValid() and src_crs != canvas_crs):
                                xf = QgsCoordinateTransform(src_crs, canvas_crs, QgsProject.instance())
                                r = xf.transformBoundingBox(r)
                    except Exception:  # nosec B110
                        pass
                    if not r.isEmpty():
                        ext = r if ext is None else ext.combineExtentWith(r)
                ds = None
        return ext

    def _enforce_lock_extent(self):
        """ロック中に解析範囲へズームする（解析番号切り替え時等に使用）。"""
        if self.preview_canvas is None or self._lock_analysis_extent is None:
            return
        self.preview_canvas.setExtent(self._lock_analysis_extent)
        self.preview_canvas.refresh()

    def _apply_map_lock(self, locked):
        """地図ロックを適用/解除する内部メソッド。"""
        if self.preview_canvas is None:
            return
        if locked:
            self._lock_analysis_extent = self._get_analysis_extent()
            self._locked_tool = LockedMapTool(self.preview_canvas)
            self.preview_canvas.setMapTool(self._locked_tool)
            # 解析範囲があればそこへズーム
            if self._lock_analysis_extent is not None:
                self.preview_canvas.setExtent(self._lock_analysis_extent)
                self.preview_canvas.refresh()
        else:
            self._lock_analysis_extent = None
            self._locked_tool = None
            self.preview_canvas.setMapTool(self._pan_tool)

    def _on_map_lock_toggled(self, checked):
        """地図ロックチェックボックスの切り替えハンドラ。"""
        self._map_locked = checked
        self._apply_map_lock(checked)
        if not checked and self.iface is not None and self.preview_canvas is not None:
            # ロック解除時：一度だけメインキャンバスをプレビューに同期
            self._syncing = True
            try:
                main = self.iface.mapCanvas()
                main.setCenter(self.preview_canvas.center())
                main.zoomScale(self.preview_canvas.scale())
                main.refresh()
            finally:
                self._syncing = False

    def _finish_init(self):
        """show()後にキャンバスサイズが確定してから呼ばれる。
        保存済みメイン状態を復元してからプレビューに同期し、双方向同期を有効化する。"""
        if self.preview_canvas is None:
            return
        if self.preview_canvas.width() == 0 or self.preview_canvas.height() == 0:
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(50, self._finish_init)
            return

        main = self.iface.mapCanvas() if self.iface is not None else None

        # ─── Step 1: メインの現在 extent を setExtent() でプレビューへ直接適用 ───
        # setCenter()+zoomScale() は未初期化キャンバスで NaN になる。
        # _zoom_preview_to_layer_if_needed() が動く理由と同じ：setExtent() は
        # 現在の canvas 状態に依存せず直接セットできる。
        # メインは触らない（触るほど壊れる）。
        self._syncing = True
        self.preview_canvas.blockSignals(True)
        try:
            if main is not None:
                crs = main.mapSettings().destinationCrs()
                if crs.isValid():
                    self.preview_canvas.setDestinationCrs(crs)
                main_ext = main.extent()
                if not main_ext.isNull() and main_ext.width() > 0:
                    self.preview_canvas.setExtent(main_ext)
        finally:
            self._syncing = False
            self.preview_canvas.blockSignals(False)

        # ─── Step 2: レイヤーをプレビューに適用 ───
        # Step 1 で有効な extent が確立されたので、_refresh_preview_canvas() 内の
        # setCenter()+zoomScale() も正常動作する。
        # _initializing=True のままなので preview→main 逆同期はまだ起きない。
        self._restore_layer_combos_if_unset()
        self._refresh_preview_canvas()
        self._pending_apply_layer_display = False

        # ─── Step 3: 初期化完了 → 双方向同期を有効化 ───
        self._initializing = False
        self._connect_interactive_signals()

        self.preview_canvas.refresh()
        self._update_preview_status()
        self._schedule_post_init_apply()

    def _schedule_post_init_apply(self):
        if self._post_init_scheduled:
            return
        self._post_init_scheduled = True
        from qgis.PyQt.QtCore import QTimer
        QTimer.singleShot(0, self._post_init_apply)

    def _post_init_apply(self):
        self._post_init_scheduled = False
        if self.preview_canvas is None:
            return
        if self.preview_canvas.width() == 0 or self.preview_canvas.height() == 0:
            self._schedule_post_init_apply()
            return
        self._restore_layer_combos_if_unset()
        self.apply_layer_display()
        self._pending_apply_layer_display = False
        self._sync_main_to_preview(force=True)

    def _ensure_startup_geometry(self):
        screen = None
        if self.iface is not None:
            try:
                center = self.iface.mainWindow().frameGeometry().center()
                screen = QtWidgets.QApplication.screenAt(center)
            except Exception:  # nosec B110
                pass
        if screen is None:
            screen = QtWidgets.QApplication.screenAt(self.pos())
        if screen is None:
            screen = QtWidgets.QApplication.primaryScreen()
        if screen is None:
            return

        avail = screen.availableGeometry()
        geom = self.frameGeometry() if self.isVisible() else self.geometry()
        width = geom.width()
        height = geom.height()
        x = avail.x() + max((avail.width() - width) // 2, 0)
        y = avail.y() + max((avail.height() - height) // 2, 0)
        self.move(x, y)

    def _connect_project_signals(self, include_main_canvas=False):
        """プロジェクトの signal（と本体地図の移動）をつなぐ。つないだら True。

        closeEvent でこれらを外すので、パネルを閉じて開き直したときは
        つなぎ直さないとプロジェクトの切り替え等に反応しなくなる（解析中の
        プロジェクト切り替えで中止されない、など）。二重につながないよう、
        つないだかどうかを _project_signals_connected で覚えておく。
        本体地図の extentsChanged は起動時は _setup_canvas_sync がつなぐので、
        開き直したときだけ include_main_canvas=True で一緒につなぐ。
        """
        if self._project_signals_connected:
            return False
        proj = QgsProject.instance()
        # プロジェクトを開いた後にもレイヤー選択を復元する
        proj.readProject.connect(self._on_project_read)
        # プロジェクト保存時に出力先ラベル更新 + レイヤー設定をプロジェクトへ書込
        proj.projectSaved.connect(self._update_out_dir_label)
        proj.projectSaved.connect(self._save_layer_settings_to_project)
        # プロジェクトクリア時に地形グループ参照をリセット（解析中なら中止）
        proj.cleared.connect(self._on_project_cleared)
        # レイヤーパネルでグループが足された・外されたら Export to Project の状態を更新
        proj.layerTreeRoot().addedChildren.connect(self._update_export_button)
        proj.layerTreeRoot().removedChildren.connect(self._update_export_button)
        if include_main_canvas and self.iface is not None:
            self.iface.mapCanvas().extentsChanged.connect(self._on_main_canvas_changed)
        self._project_signals_connected = True
        return True

    def initialize_window_mode(self):
        # パネルを閉じた後に開き直したとき: closeEvent で外した signal を
        # つなぎ直し、閉じている間に開かれたかもしれないプロジェクトに合わせる
        # （プロジェクトを開いたときと同じ処理）。起動直後はつないであるので何もしない。
        if self._connect_project_signals(include_main_canvas=True):
            self._on_project_read()
        standalone = True
        QSettings().setValue("forestry_operations_lite/standalone_window", True)
        with QSignalBlocker(self.chkStandaloneWindow):
            self.chkStandaloneWindow.setChecked(standalone)
        self._apply_window_mode(standalone)

    def _apply_window_mode(self, checked):
        from qgis.PyQt.QtCore import QTimer
        QSettings().setValue("forestry_operations_lite/standalone_window", bool(checked))
        if checked:
            # ウィンドウ操作前にメインキャンバス状態を保存（起動初回のみ）
            if getattr(self, '_initializing', False) and self.iface is not None:
                mc = self.iface.mapCanvas()
                self._saved_main_center = mc.center()
                self._saved_main_scale  = mc.scale()
                self._saved_main_extent = mc.extent()   # extent を直接保存（setExtent で確実に復元できる）
            self.setWindowFlags(
                Qt.Window
                | Qt.WindowMinimizeButtonHint
                | Qt.WindowMaximizeButtonHint
                | Qt.WindowCloseButtonHint
            )
            self.show()
            QTimer.singleShot(0, self._ensure_startup_geometry)
            QTimer.singleShot(150, self._ensure_startup_geometry)
            QTimer.singleShot(300, self._finish_init)
        else:
            self.show()

    def _refresh_preview_canvas(self):
        if self.preview_canvas is None:
            return
        if self.preview_canvas.width() == 0 or self.preview_canvas.height() == 0:
            self._pending_apply_layer_display = True
            return
        bg   = self._get_selected_layer(self.cmbBackgroundLayer)
        tile = self._get_selected_layer(self.cmbTileLayer)
        gpkg = self._get_selected_layer(self.cmbGpkgLayer)
        # レイヤー表示 ON/OFF ボタンの状態を反映
        if not getattr(self, "btnGpkgLayerVis", None) or not self.btnGpkgLayerVis.isChecked():
            gpkg = None
        if not getattr(self, "btnTileLayerVis", None) or not self.btnTileLayerVis.isChecked():
            tile = None
        if not getattr(self, "btnBgLayerVis", None) or not self.btnBgLayerVis.isChecked():
            bg = None
        # 地形解析ラスターを収集（ONになっているもののみ）
        proj = QgsProject.instance()
        terrain_lyrs = []
        if getattr(self, "_terrain_layers_visible", True):
            # ツリーと同じく _KEY_RANK 降順（flow が最前面）で積む。
            # dict の挿入順（＝ボタン押下順）だとプレビューの重なりが安定しない。
            loaded = getattr(self, "_loaded_terrain_layers", {})
            for key in sorted(loaded, key=lambda k: self._KEY_RANK.get(k, 0),
                              reverse=True):
                for lid in loaded.get(key, []):
                    lyr = proj.mapLayer(lid)
                    if lyr is not None and lyr.isValid():
                        terrain_lyrs.append(lyr)
                # flow レイヤーの直後にバッファ滲みレイヤーを挿入
                if key == "flow":
                    for blid in getattr(self, "_flow_buffer_layer_ids", []):
                        blyr = proj.mapLayer(blid)
                        if blyr is not None and blyr.isValid():
                            terrain_lyrs.append(blyr)
        # Top -> bottom order: terrain raster > gpkg > tile > bg
        layer_stack = []
        for lyr in terrain_lyrs + [gpkg, tile, bg]:
            if lyr is not None and lyr.isValid():
                layer_stack.append(lyr)
        self._preview_has_layers = bool(layer_stack)
        _prev_syncing = getattr(self, "_syncing", False)
        self._syncing = True
        self.preview_canvas.blockSignals(True)
        try:
            if self.iface is not None:
                main = self.iface.mapCanvas()
                dest_crs = main.mapSettings().destinationCrs()
                if dest_crs.isValid():
                    # CRS が変わったときだけ更新（毎回 set すると内部リセットが走る）
                    if self.preview_canvas.mapSettings().destinationCrs() != dest_crs:
                        self.preview_canvas.setDestinationCrs(dest_crs)
            self.preview_canvas.setLayers(layer_stack)
            if self.iface is not None:
                main = self.iface.mapCanvas()
                # レイヤー更新後も主ウィンドウの位置に追従させる（0px キャンバスはスキップ）
                if (not self._map_locked or self._lock_analysis_extent is None) and self.preview_canvas.width() > 0:
                    self.preview_canvas.setCenter(main.center())
                    self.preview_canvas.zoomScale(main.scale())
                elif self.preview_canvas.extent().isEmpty():
                    pass  # 0px キャンバスには setExtent も不可
        finally:
            self.preview_canvas.blockSignals(False)
            self._syncing = _prev_syncing
        self.preview_canvas.refresh()
        self._update_preview_status()

    def _zoom_preview_to_layer_if_needed(self, lyr):
        """レイヤ範囲がプレビューキャンバスと重ならない場合のみズーム。
        レイヤ CRS → キャンバス CRS へ変換してから比較する。"""
        if self.preview_canvas is None or lyr is None or not lyr.isValid():
            return
        try:
            lyr_ext = lyr.extent()
            if lyr_ext.isEmpty():
                return
            canvas_crs = self.preview_canvas.mapSettings().destinationCrs()
            lyr_crs = lyr.crs()
            if lyr_crs.isValid() and canvas_crs.isValid() and lyr_crs != canvas_crs:
                from qgis.core import QgsCoordinateTransform as _CT
                xf = _CT(lyr_crs, canvas_crs, QgsProject.instance())
                lyr_ext = xf.transformBoundingBox(lyr_ext)
            if lyr_ext.isEmpty():
                return
            if not self.preview_canvas.extent().intersects(lyr_ext):
                _prev = getattr(self, "_syncing", False)
                self._syncing = True
                try:
                    self.preview_canvas.setExtent(lyr_ext)
                finally:
                    self._syncing = _prev
                self.preview_canvas.refresh()
                # _syncing 中は _on_preview_canvas_changed がスキップされるので
                # マップロックでなければメインキャンバスに明示的に同期する（初期化中は除く）
                if not getattr(self, "_initializing", False) and not getattr(self, "_map_locked", False) and self.iface is not None:
                    self._syncing = True
                    try:
                        main = self.iface.mapCanvas()
                        main.setCenter(self.preview_canvas.center())
                        main.zoomScale(self.preview_canvas.scale())
                        main.refresh()
                    finally:
                        self._syncing = _prev
        except Exception:  # nosec B110
            pass

    def _zoom_preview_to_base_layers_if_needed(self, bg, tile, gpkg):
        if self.preview_canvas is None:
            return
        canvas_crs = self.preview_canvas.mapSettings().destinationCrs()
        combined = None
        for lyr in (bg, tile, gpkg):
            if lyr is None or not lyr.isValid():
                continue
            ext = lyr.extent()
            if ext.isEmpty():
                continue
            try:
                lyr_crs = lyr.crs()
                if lyr_crs.isValid() and canvas_crs.isValid() and lyr_crs != canvas_crs:
                    from qgis.core import QgsCoordinateTransform as _CT
                    xf = _CT(lyr_crs, canvas_crs, QgsProject.instance())
                    ext = xf.transformBoundingBox(ext)
            except Exception:  # nosec B110
                pass
            if ext.isEmpty():
                continue
            combined = ext if combined is None else combined.combineExtentWith(ext)
        if combined is None or combined.isEmpty():
            return
        if self.preview_canvas.extent().isEmpty() or not self.preview_canvas.extent().intersects(combined):
            _prev = getattr(self, "_syncing", False)
            self._syncing = True
            try:
                self.preview_canvas.setExtent(combined)
            finally:
                self._syncing = _prev
            self.preview_canvas.refresh()

    def _get_selected_layer(self, combo):
        layer_id = combo.currentData()
        if not layer_id:
            return None
        return QgsProject.instance().mapLayer(layer_id)

    def apply_layer_display(self):
        # Layer Settings で選んだレイヤーの不透明度:
        #   初回のみレイヤー自身の不透明度でスピンを初期化し、元値を退避
        #   （プラグイン終了時 / 選択解除時に復元する）。以降はスピン操作を反映。
        proj = QgsProject.instance()
        bg   = self._get_selected_layer(self.cmbBackgroundLayer)
        tile = self._get_selected_layer(self.cmbTileLayer)
        gpkg = self._get_selected_layer(self.cmbGpkgLayer)
        has_base_layers = any(
            lyr is not None and lyr.isValid() for lyr in (bg, tile, gpkg)
        )
        _cur_ids = {lyr.id() for lyr in (bg, tile, gpkg) if lyr is not None}
        for _lid in list(self._orig_base_opacity):
            if _lid in _cur_ids:
                continue
            _l = proj.mapLayer(_lid)
            if _l is not None:
                self._set_layer_opacity(_l, self._orig_base_opacity[_lid])
                _l.triggerRepaint()
            del self._orig_base_opacity[_lid]
        for _l, _spin in ((bg, self.spinBgOpacity),
                          (tile, self.spinTileOpacity),
                          (gpkg, self.spinGpkgOpacity)):
            if _l is None:
                continue
            if _l.id() not in self._orig_base_opacity:
                self._orig_base_opacity[_l.id()] = self._layer_opacity(_l)
                with QSignalBlocker(_spin):
                    _spin.setValue(int(round(self._layer_opacity(_l) * 100.0)))
            self._set_layer_opacity(_l, _spin.value() / 100.0)
            _l.triggerRepaint()

        if self.preview_canvas is None:
            self._pending_apply_layer_display = True
            return
        if self.preview_canvas.width() == 0 or self.preview_canvas.height() == 0:
            self._pending_apply_layer_display = True
            return

        # _finish_init 完了後（キャンバスサイズ確定後）に、
        # 初めて有効なレイヤーが設定された場合（＝起動時にレイヤー設定がなかった場合）、
        # ここで双方向同期を有効化する。
        if self._initializing and self.preview_canvas.width() > 0:
            if has_base_layers:
                force = self._lock_analysis_extent is None
                self._sync_main_to_preview(force=force) # main -> preview を一度実行
                self._initializing = False

        self._refresh_preview_canvas()

    def _restore_base_layer_opacity(self):
        """Layer Settings で触った実レイヤーの不透明度を元値へ戻す。"""
        proj = QgsProject.instance()
        for _lid, _op in list(self._orig_base_opacity.items()):
            _l = proj.mapLayer(_lid)
            if _l is not None:
                self._set_layer_opacity(_l, _op)
                _l.triggerRepaint()
        self._orig_base_opacity.clear()

    # ------------------------------------------------------------------ #
    #  地形解析                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _apply_vector_style(lyr, base_name):
        """base_name ごとにベクターレイヤーの色を設定"""
        from qgis.PyQt.QtGui import QColor
        from qgis.core import QgsSimpleFillSymbolLayer, QgsUnitTypes
        from qgis.PyQt.QtCore import Qt

        renderer = lyr.renderer()
        if renderer is None:
            return
        symbol = renderer.symbol()
        if symbol is None:
            return

        if base_name == "valley_zones":
            # Layer 0: 青半透明塗り + 暗アウトライン
            sl0 = symbol.symbolLayer(0)
            if sl0:
                sl0.setColor(QColor(30, 80, 220, 120))
                sl0.setStrokeColor(QColor(35, 35, 35, 255))
                sl0.setStrokeWidth(0.26)
            # Layer 1: 外周グロー（塗りなし、青半透明の太いアウトライン）
            sl1 = QgsSimpleFillSymbolLayer()
            sl1.setBrushStyle(Qt.NoBrush)
            sl1.setStrokeColor(QColor(30, 80, 220, 52))
            sl1.setStrokeWidth(6)
            sl1.setStrokeWidthUnit(QgsUnitTypes.RenderPixels)
            symbol.appendSymbolLayer(sl1)
        elif base_name == "unstable_zones":
            symbol.setColor(QColor(220, 0, 0, 140))
        elif base_name == "integrated_high_risk":
            symbol.setColor(QColor(200, 0, 0, 160))
        lyr.triggerRepaint()

    @staticmethod
    def _notify_layer_style_changed(lyr):
        """Renderer changes made before adding a layer still need an explicit update."""
        if lyr is None:
            return
        try:
            lyr.triggerRepaint()
        except Exception:  # nosec B110
            pass
        try:
            lyr.emitStyleChanged()
        except Exception:  # nosec B110
            pass

    def _refresh_render_canvases(self):
        for canvas in (
            getattr(self, "preview_canvas", None),
            self.iface.mapCanvas() if self.iface is not None else None,
        ):
            if canvas is None:
                continue
            try:
                if hasattr(canvas, "clearCache"):
                    canvas.clearCache()
                canvas.refresh()
            except Exception:  # nosec B110
                pass

    # 低値透過フィルタ対象キー（twi, 流量系）
    _FILTER_KEYS = {"twi", "flow_peak", "flow_mean", "tc"}

    @staticmethod
    def _apply_raster_color(lyr, base_name, filter_mode="off"):
        """base_name ごとに目的別カラーランプを設定。
        固定値型: FS・統合リスクなど物理的意味のある値を使用。
        動的型: 実データの min/max に合わせてスケール（流量は対数スケール）。
        "range" 付きは絶対値スケール（データに依らず固定レンジ）。
        filter_mode: 'off'=フラット, 'low'=低値透明線形, 'mid'=下半透明→上半線形"""
        import math
        from qgis.PyQt.QtGui import QColor
        from qgis.core import (
            QgsColorRampShader, QgsRasterShader,
            QgsSingleBandPseudoColorRenderer,
        )

        # 固定値型: (value, QColor, label) のリスト。値はそのまま使用。
        FIXED = {
            "stability_fs": [
                (0.0, QColor(215,  25,  28), "0"),
                (1.0, QColor(245, 251, 182), "1"),
                (2.0, QColor(206, 234, 144), "2"),
                (3.0, QColor(166, 217, 106), "3"),
                (4.0, QColor( 26, 150,  65), "4"),
            ],
            "integrated_risk_index": [
                (0.0, QColor( 60, 180,  60), "Low 0"),
                (2.0, QColor(255, 220,   0), "Mid 2"),
                (3.0, QColor(255, 130,   0), "High 3"),
                (5.0, QColor(200,   0,   0), "Max 5"),
            ],
        }

        # 動的型: (QColor, label) のみ。値は実データ range に合わせて自動生成。
        # log=True のものは対数スケール（流量など分布が裾の長いデータ向け）。
        DYNAMIC = {
            # Time load (Tc): 源頭≈0・幹川で立ち上がり出口で最大の右に裾長い場。
            # 対数 + 小 floor で低い側を開き、外れ値は mean+3σ で頭打ち(robust)。
            "tc": {
                "log": True,
                "floor": 0.02,
                "robust": True,
                "colors": [
                    (QColor(255, 255, 220), "Short (headwater)"),
                    (QColor(150, 200, 130), "Mid"),
                    (QColor( 30, 100,  40), "Long (basin outlet)"),
                ],
            },
            "twi": {
                "log": False,
                "colors": [
                    (QColor(255, 255, 200), "Low"),
                    (QColor( 70, 130, 230), "Mid"),
                    (QColor(  0,  50, 180), "High"),
                ],
            },
            # Peak/Mean 負荷は絶対値スケール（"range" 固定）。両者で同一レンジ
            # にして、色 = 実流量 [m³/s] を保ち Peak と Mean を見比べられる。
            # Peak/Mean 負荷は log スケール。レンジは両レイヤー共通の
            # データ実測 min/max（下の共有レンジ計算で "range" を注入）。
            "flow_peak": {
                "log": True,
                "colors": [
                    (QColor(255, 235, 230), "Low"),
                    (QColor(220,  70,  40), "Mid"),
                    (QColor(150,   0,   0), "High"),
                ],
            },
            "flow_mean": {
                "log": True,
                "colors": [
                    (QColor(255, 240, 220), "Low"),
                    (QColor(235, 120,  30), "Mid"),
                    (QColor(170,  50,   0), "High"),
                ],
            },
        }

        # サンプル数を制限して高速化（全ピクセルスキャンは大規模ラスターで数秒かかる）
        from qgis.core import QgsRasterBandStats
        stats = lyr.dataProvider().bandStatistics(
            1,
            QgsRasterBandStats.Min | QgsRasterBandStats.Max
            | QgsRasterBandStats.Mean | QgsRasterBandStats.StdDev,
            lyr.extent(),
            250_000,
        )
        vmin, vmax = stats.minimumValue, stats.maximumValue
        vmean, vstd = stats.mean, stats.stdDev
        if vmin >= vmax:
            return

        # Peak/Mean 負荷: 同フォルダの相方ラスターと min/max を共有し、
        # 同一スケールで塗る（絶対値比較を保ちつつ実データ範囲に合わせる）。
        if base_name in ("flow_peak", "flow_mean"):
            lo, hi = vmin, vmax
            _src = lyr.source().split("|")[0]
            _sib = os.path.join(
                os.path.dirname(_src),
                "flow_mean.tif" if base_name == "flow_peak" else "flow_peak.tif")
            if os.path.exists(_sib):
                from qgis.core import QgsRasterLayer as _QRL
                _sl = _QRL(_sib, "sib")
                try:
                    if _sl.isValid():
                        _ss = _sl.dataProvider().bandStatistics(
                            1, QgsRasterBandStats.Min | QgsRasterBandStats.Max,
                            _sl.extent(), 250_000)
                        lo = min(lo, _ss.minimumValue)
                        hi = max(hi, _ss.maximumValue)
                finally:
                    # プロジェクト未追加の一時レイヤー。GDAL ハンドルを
                    # 即手放す（Windows で相方 .tif がロック扱いにならない
                    # よう明示破棄）。
                    _sl = None
                    del _sl
            DYNAMIC[base_name]["range"] = (max(lo, 1e-6), hi)

        if base_name in FIXED:
            items = [
                QgsColorRampShader.ColorRampItem(v, c, l)
                for v, c, l in FIXED[base_name]
            ]
        elif base_name in DYNAMIC:
            cfg = DYNAMIC[base_name]
            colors = cfg["colors"]
            n = len(colors)
            # "range" 指定があれば絶対値スケール、無ければ実データ min/max
            rng = cfg.get("range")
            smin, smax = (rng if rng else (vmin, vmax))
            if cfg.get("floor") is not None:
                smin = max(smin, cfg["floor"])
            if cfg.get("robust") and vstd > 0:
                smax = max(min(smax, vmean + 3.0 * vstd), smin * 1.0001)
            if cfg["log"] and smin > 0:
                log_min = math.log10(smin)
                log_max = math.log10(smax)
                values = [10 ** (log_min + (log_max - log_min) * i / (n - 1))
                          for i in range(n)]
            else:
                values = [smin + (smax - smin) * i / (n - 1) for i in range(n)]
            items = [
                QgsColorRampShader.ColorRampItem(v, c, l)
                for v, (c, l) in zip(values, colors)
            ]

            # 低値透過フィルタ適用（対象キーのみ）
            if base_name in {"twi", "flow_peak", "flow_mean", "tc"} and filter_mode in ("low", "mid"):
                n = len(items)
                if filter_mode == "low":
                    # 中間ストップを上限の85%に固定（low は弱めのフィルタ）
                    alphas = [0] + [int(255 * 0.85)] * (n - 2) + [255]
                else:  # mid: 下半=0, 上半=線形
                    mid = n // 2
                    upper = n - mid
                    alphas = ([0] * mid +
                              [int(255 * i / (upper - 1)) for i in range(upper)])
                new_items = []
                for item, a in zip(items, alphas):
                    c2 = QColor(item.color.red(), item.color.green(),
                                item.color.blue(), a)
                    new_items.append(
                        QgsColorRampShader.ColorRampItem(item.value, c2, item.label))
                items = new_items
        else:
            return

        shader_func = QgsColorRampShader()
        shader_func.setColorRampType(QgsColorRampShader.Interpolated)
        # 分類モードを明示（未設定だとプロパティの Mode 欄が不定になる）
        if hasattr(shader_func, "setClassificationMode"):
            shader_func.setClassificationMode(QgsColorRampShader.Continuous)
        shader_func.setColorRampItemList(items)
        shader_func.setMinimumValue(items[0].value)
        shader_func.setMaximumValue(items[-1].value)

        raster_shader = QgsRasterShader()
        raster_shader.setRasterShaderFunction(shader_func)

        renderer = QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, raster_shader)
        # renderer 自身の classification min/max も設定する。未設定だと NaN の
        # ままで、レイヤープロパティのシンボロジタブが Min/Max 欄を空で開き、
        # そのまま OK を押すとランプが再生成されて真っ黒になる。
        renderer.setClassificationMin(items[0].value)
        renderer.setClassificationMax(items[-1].value)
        lyr.setRenderer(renderer)
        # triggerRepaint は呼び出し元が管理する（レイヤ追加前に呼ぶと無駄な描画になるため）

    def _on_key_opacity_changed(self, key, value):
        opacity = value / 100.0
        proj = QgsProject.instance()
        for lid in self._loaded_terrain_layers.get(key, []):
            lyr = proj.mapLayer(lid)
            if lyr is None:
                continue
            if lyr.type() == lyr.RasterLayer:
                lyr.renderer().setOpacity(opacity)
            else:
                lyr.setOpacity(opacity)
            lyr.triggerRepaint()
        if self.preview_canvas is not None:
            self.preview_canvas.refresh()

    # ── Flow フィルタ/バッファ状態アクセサ ─────────────────────────────
    # Peak load / Mean load（レート）は共通状態、Time load（tc）のみ別トラック。
    # tc はレートでないのでバッファは使わない（フィルタ状態のみ独立）。
    # 読み書きは必ずこのアクセサ経由で行う。
    def _flow_filter_state(self, base_name=None):
        if base_name == "tc":
            return getattr(self, "_filter_state_tc", "off")
        return self._filter_state.get("flow", "off")

    def _set_flow_filter_state(self, base_name, value):
        if base_name == "tc":
            self._filter_state_tc = value
        else:
            self._filter_state["flow"] = value

    def _flow_buffer_state_for_base(self, base_name=None):
        if base_name == "tc":
            return getattr(self, "_flow_buffer_state_tc", "off")
        return self._flow_buffer_state

    def _set_flow_buffer_state_for_base(self, base_name, value):
        if base_name == "tc":
            self._flow_buffer_state_tc = value
        else:
            self._flow_buffer_state = value

    def _set_flow_buffer_button_state(self, state):
        labels = {
            "off":    "Buffer: Off",
            "weak":   "Buffer: Low",
            "strong": "Buffer: High",
        }
        self.btnFlowBuffer.setText(labels.get(state, labels["off"]))
        active = state != "off"
        self.btnFlowBuffer.setStyleSheet(
            ("font-size:8pt; padding:1px 2px;"
             "background:#e07050; color:white; border-radius:2px;")
            if active else
            "font-size:8pt; padding:1px 2px;"
        )

    def _sync_flow_aux_controls(self):
        """現在表示中の flow ベースに対応するフィルタ/バッファ状態を
        Filter/Buffer ボタンの表示へ反映する。Peak/Mean と Tc で
        フィルタ/バッファ状態は独立トラック。"""
        base_name = getattr(self, "_loaded_terrain_basenames", {}).get("flow")
        self.btnFilterFlow.setText(self._flow_filter_state(base_name))
        self.btnFlowBuffer.setEnabled(True)
        self._set_flow_buffer_button_state(
            self._flow_buffer_state_for_base(base_name)
        )

    def _toggle_filter(self, key):
        """湿潤地形/流量推測の低値透過フィルタを off→low→mid→off と循環する。"""
        states = ["off", "low", "mid"]
        base_name = self._loaded_terrain_basenames.get(key)
        if key == "flow":
            current = self._flow_filter_state(base_name)
        else:
            current = self._filter_state.get(key, "off")
        next_state = states[(states.index(current) + 1) % len(states)]
        if key == "flow":
            self._set_flow_filter_state(base_name, next_state)
        else:
            self._filter_state[key] = next_state

        btn = self.btnFilterWetland if key == "wetland" else self.btnFilterFlow
        btn.setText(next_state)

        # 表示中のラスタレイヤへ即時再適用
        if base_name is None:
            return
        proj = QgsProject.instance()
        for lid in self._loaded_terrain_layers.get(key, []):
            lyr = proj.mapLayer(lid)
            if lyr is None or lyr.type() != lyr.RasterLayer:
                continue
            opacity = lyr.renderer().opacity()
            self._apply_raster_color(lyr, base_name, next_state)
            lyr.renderer().setOpacity(opacity)
            lyr.triggerRepaint()
        if key == "flow" and self._flow_buffer_state_for_base(base_name) != "off":
            self._apply_flow_buffer()
        if self.preview_canvas is not None:
            self.preview_canvas.refresh()

    # バッファ強度ごとのブラー半径（ピクセル）。滲みレイヤーはノーフィルタで
    # 本体の下から染み出すため、控えめな sigma でも視認できる。全 flow ベース
    # 共通（Mean/Total の専用 override は廃止。4.x の vtotal 特例も持ち込まない）。
    _FLOW_BUFFER_SIGMA  = {"weak": 1.5, "strong": 2.5}   # Gaussian sigma（ピクセル）
    _FLOW_BUFFER_OPACITY = {"weak": 0.30, "strong": 0.33}  # 滲みレイヤーの不透明度

    @classmethod
    def _flow_buffer_sigma(cls, base_name, state):
        return cls._FLOW_BUFFER_SIGMA[state]

    def _on_terrain_toggle(self, checked: bool):
        """解析データ表示トグル: ON=表示・OFF=非表示（個別設定は保持）。"""
        self._terrain_layers_visible = checked
        self._refresh_preview_canvas()

    def _cycle_flow_buffer(self):
        """流量レイヤーのバッファ（滲み）表現を off→弱→強→off と循環する。"""
        states = ["off", "weak", "strong"]
        base_name = self._loaded_terrain_basenames.get("flow")
        cur = self._flow_buffer_state_for_base(base_name)
        nxt = states[(states.index(cur) + 1) % len(states)]
        self._set_flow_buffer_state_for_base(base_name, nxt)
        self._set_flow_buffer_button_state(nxt)
        self._apply_flow_buffer()

    def _apply_flow_buffer(self):
        """Gaussian blur した滲みレイヤーを /vsimem/ 経由で追加/削除する。
        ラスターレイヤーは setPaintEffect 非対応のため、scipy でデータを直接ぼかす。"""
        import numpy as np
        from scipy.ndimage import gaussian_filter
        from osgeo import gdal
        from qgis.core import QgsLayerTreeLayer

        proj = QgsProject.instance()

        # 既存バッファレイヤーを削除
        for lid in self._flow_buffer_layer_ids:
            if proj.mapLayer(lid):
                proj.removeMapLayer(lid)
        self._flow_buffer_layer_ids = []
        from osgeo import gdal as _gdal
        for mp in self._flow_buffer_mem_paths:
            _gdal.Unlink(mp)
        self._flow_buffer_mem_paths = []

        base_name = self._loaded_terrain_basenames.get("flow")
        state = self._flow_buffer_state_for_base(base_name)
        if state == "off":
            self._normalize_terrain_group_order()
            self._refresh_preview_canvas()
            return

        opacity = self._FLOW_BUFFER_OPACITY[state]

        for lid in self._loaded_terrain_layers.get("flow", []):
            lyr = proj.mapLayer(lid)
            if lyr is None or lyr.type() != lyr.RasterLayer:
                continue
            sigma = self._flow_buffer_sigma(base_name, state)
            src_path = lyr.source()
            ds = gdal.Open(src_path)
            if ds is None:
                continue

            data     = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
            gt       = ds.GetGeoTransform()
            wkt      = ds.GetProjection()
            nodata   = ds.GetRasterBand(1).GetNoDataValue()
            ds = None

            mask = np.isnan(data)
            if nodata is not None:
                mask |= (data == nodata)
            filled  = np.where(mask, 0.0, data)
            blurred = gaussian_filter(filled.astype(np.float64), sigma=sigma).astype(np.float32)
            blurred[mask] = nodata if nodata is not None else np.nan

            mem_path = f"/vsimem/flow_buf_{id(lyr)}_{state}.tif"
            drv = gdal.GetDriverByName("GTiff")
            out = drv.Create(mem_path, data.shape[1], data.shape[0], 1, gdal.GDT_Float32)
            if out is None:
                continue
            # Create 成功直後に登録 — 以降の例外でも Unlink が保証される
            self._flow_buffer_mem_paths.append(mem_path)
            out.SetGeoTransform(gt)
            out.SetProjection(wkt)
            band = out.GetRasterBand(1)
            band.WriteArray(blurred)
            if nodata is not None:
                band.SetNoDataValue(nodata)
            band.FlushCache()
            out.FlushCache()
            out = None

            blur_lyr = QgsRasterLayer(mem_path, f"{lyr.name()} blur")
            if not blur_lyr.isValid():
                continue

            # 滲みレイヤーにはフィルタ（低値透過）をかけない。生の高位を
            # そのままぼかして本体レイヤーの下から染み出させることで、
            # 本体フィルタが off でも「バッファをかけた」効果が出る。
            if base_name:
                self._apply_raster_color(blur_lyr, base_name, "off")
            blur_lyr.renderer().setOpacity(opacity)
            proj.addMapLayer(blur_lyr, False)

            # flow レイヤーの直下に挿入
            group = self._terrain_layer_group
            if group is not None:
                children = group.children()
                pos = 0
                for i, child in enumerate(children):
                    if hasattr(child, 'layerId') and child.layerId() == lid:
                        pos = i + 1  # 元レイヤーの直下
                        break
                group.insertChildNode(pos, QgsLayerTreeLayer(blur_lyr))
                node = group.findLayer(blur_lyr.id())
                if node:
                    node.setExpanded(False)

            self._flow_buffer_layer_ids.append(blur_lyr.id())

        self._normalize_terrain_group_order()
        self._refresh_preview_canvas()

    # (base_name, label, kind, ext)  ← 解析番号プレフィクスは _toggle_terrain_layer で付加
    _TERRAIN_PATTERNS = {
        "stability": [
            ("stability_fs",  "Slope Stability FS", "raster", ".tif"),
        ],
        "valley": [
            ("valley_zones",  "Valley Terrain", "vector", ".gpkg"),
        ],
        "wetland": [
            ("twi",  "Wetland Terrain", "raster", ".tif"),
        ],
        "flow": [
            ("flow_peak", "Peak load: Qp[m³/s]", "raster", ".tif"),
            ("flow_mean", "Mean load: Qm[m³/s]", "raster", ".tif"),
            ("tc",        "Time load: Tc[h]",    "raster", ".tif"),
        ],
        "integrated": [
            ("integrated_risk_index", "Overall Risk Index", "raster", ".tif"),
            ("integrated_high_risk",  "High Risk Areas",    "vector", ".gpkg"),
        ],
    }

    _BTN_LABELS = {
        "stability": ("Slope Stability",  None),
        "valley":    ("Valley Terrain",   None),
        "wetland":   ("Wetland Terrain",  None),
        "flow":      ("Flow Estimation",  None),
        "integrated":("Overall Risk",     None),
    }

    # レイヤパネル内の順序: 値が大きいほど上（描画前面）
    # 下から: Slope Stability → Overall Risk → Valley → Wetland → Flow（最前面）
    _KEY_RANK = {"stability": 0, "integrated": 1, "valley": 2, "wetland": 3, "flow": 4}

    def _btn(self, key):
        return {
            "stability":  self.chkLoadStability,
            "valley":     self.chkLoadValley,
            "wetland":    self.chkLoadWetland,
            "flow":       self.chkLoadFlow,
            "integrated": self.chkLoadIntegrated,
        }[key]

    def _opacity_spinbox(self, key):
        return {
            "stability":  self.spinOpacityStability,
            "valley":     self.spinOpacityValley,
            "wetland":    self.spinOpacityWetland,
            "flow":       self.spinOpacityFlow,
            "integrated": self.spinOpacityIntegrated,
        }[key]

    def _normalize_terrain_group_order(self):
        """グループ内の子ノード順を _KEY_RANK（上位＝高ランク）に正規化する。
        flow のバッファ滲みレイヤーは flow 本体の直下に固める。挿入位置の
        計算がバッファ層でずれても、切替後にここで並びを立て直す。
        並べ替え中はキャンバスを freeze してノード単位の再描画を抑止する。"""
        group = self._terrain_layer_group
        if group is None:
            return
        try:
            group.name()  # 削除済みなら RuntimeError
        except RuntimeError:
            return
        proj = QgsProject.instance()
        desired_ids = []
        for _k in sorted(self._loaded_terrain_layers,
                         key=lambda x: self._KEY_RANK.get(x, 0), reverse=True):
            for _lid in self._loaded_terrain_layers.get(_k, []):
                if proj.mapLayer(_lid) is not None:
                    desired_ids.append(_lid)
            if _k == "flow":
                for _lid in self._flow_buffer_layer_ids:
                    if proj.mapLayer(_lid) is not None:
                        desired_ids.append(_lid)
        if not desired_ids:
            return
        current = [n.layerId() for n in group.findLayers()]
        if current == desired_ids:
            return
        checked_by_id = {}
        for _n in group.findLayers():
            try:
                checked_by_id[_n.layerId()] = _n.itemVisibilityChecked()
            except Exception:  # nosec B110
                pass
        # 並べ替え中は preview / メイン両方を freeze しないと、ノード
        # remove/insert の途中状態でメインキャンバスの描画が崩れる
        # （v0.2.11 で本メソッド追加時からの対策。GUI が一瞬引っかかるが必要）。
        _canvases = [_c for _c in (
            getattr(self, "preview_canvas", None),
            self.iface.mapCanvas() if self.iface is not None else None,
        ) if _c is not None]
        for _c in _canvases:
            try:
                _c.freeze(True)
            except Exception:  # nosec B110
                pass
        try:
            # QGIS3.44 / Qt5 の QgsLayerTreeGroup.reorderGroupLayers() は
            # Windows でアクセス違反クラッシュを起こすため使わない。
            # clone → remove → insert で並べ替える（clone はチェック状態を
            # 落とすことがあるので、保存した値で明示復元する）。
            for _i, _lid in enumerate(desired_ids):
                _n = group.findLayer(_lid)
                if _n is None:
                    continue
                _was_checked = checked_by_id.get(_lid, _n.itemVisibilityChecked())
                _clone = _n.clone()
                group.removeChildNode(_n)
                _pos = min(_i, len(group.children()))
                group.insertChildNode(_pos, _clone)
                _cn = group.findLayer(_lid)
                if _cn is not None:
                    _cn.setItemVisibilityChecked(_was_checked)
                    _cn.setExpanded(False)
        except Exception:  # nosec B110
            pass
        finally:
            for _c in _canvases:
                try:
                    _c.freeze(False)
                    _c.refresh()
                except Exception:  # nosec B110
                    pass

    def _insert_terrain_layer_ordered(self, key, lyr):
        """_KEY_RANK に従いグループ内の正しい位置にレイヤを挿入する。
        ランクが高いキー（上位）が既にあればその下、なければ先頭に挿入する。"""
        from qgis.core import QgsLayerTreeLayer
        rank = self._KEY_RANK.get(key, 0)
        group = self._terrain_layer_group
        children = group.children()

        # ランク > rank の既存レイヤ数 = そのぶん下に挿入位置をずらす
        insert_pos = 0
        for child in children:
            if not hasattr(child, 'layerId'):
                continue
            child_lid = child.layerId()
            for k, lids in self._loaded_terrain_layers.items():
                if child_lid in lids and self._KEY_RANK.get(k, 0) > rank:
                    insert_pos += 1
                    break
        group.insertChildNode(insert_pos, QgsLayerTreeLayer(lyr))
        node = group.findLayer(lyr.id())
        if node:
            node.setExpanded(False)

    # stability ↔ integrated の排他ペア
    _EXCLUSIVE = {"stability": "integrated", "integrated": "stability"}

    # 表示ボタンの通常スタイル（青ON）と排他抑制中スタイル（赤ON）
    _BTN_STYLE_NORMAL = (
        "QPushButton{padding:2px 10px;}"
        "QPushButton:checked{background:#3a7fd5;color:white;"
        "border:1px solid #2563b0;border-radius:3px;}"
        "QPushButton:disabled{color:#aaa;background:#f2f2f2;"
        "border:1px solid #ddd;border-radius:3px;}"
    )
    # レイヤー表示ボタン（緑ON）— padding・border-radius は NORMAL と共通
    _BTN_STYLE_LAYER = (
        "QPushButton{padding:2px 10px;}"
        "QPushButton:checked{background:#27ae60;color:white;"
        "border:1px solid #1e8449;border-radius:3px;}"
    )
    _BTN_STYLE_BLOCKING = (
        "QPushButton{padding:2px 10px;}"
        "QPushButton:checked{background:#c0392b;color:white;"
        "border:1px solid #922b21;border-radius:3px;}"
        "QPushButton:disabled{color:#aaa;background:#f2f2f2;"
        "border:1px solid #ddd;border-radius:3px;}"
    )

    def _hide_key(self, key):
        """キーのレイヤをプロジェクトから削除し、ボタンをOFF状態にする。
        cycle state は -1 にリセットする（状態保存は呼び出し側が行う）。"""
        proj = QgsProject.instance()
        if key == "flow":
            for lid in self._flow_buffer_layer_ids:
                if proj.mapLayer(lid):
                    proj.removeMapLayer(lid)
            self._flow_buffer_layer_ids = []
            from osgeo import gdal as _gdal
            for mp in self._flow_buffer_mem_paths:
                _gdal.Unlink(mp)
            self._flow_buffer_mem_paths = []
        for lid in self._loaded_terrain_layers.pop(key, []):
            if proj.mapLayer(lid):
                proj.removeMapLayer(lid)
        self._terrain_cycle_state[key] = -1
        btn = self._btn(key)
        with QSignalBlocker(btn):
            btn.setChecked(False)
            btn.setText(self._BTN_LABELS[key][0])
            btn.setStyleSheet(self._BTN_STYLE_NORMAL)

    def _reset_load_buttons(self):
        """すべての読込ボタンを非表示状態にリセットする。"""
        self._terrain_cycle_state.clear()
        for key, (label, _) in self._BTN_LABELS.items():
            btn = self._btn(key)
            with QSignalBlocker(btn):
                btn.setChecked(False)
                btn.setText(label)

    def _cycle_terrain_layer(self, key):
        """クリック毎に 非表示→ファイル1→ファイル2→...→非表示 と循環する。"""
        analysis_number = self.cmbAnalysisNumber.currentData()
        btn = self._btn(key)
        base_label = self._BTN_LABELS[key][0]

        if not analysis_number:
            self._set_load_status("Select an analysis run")
            with QSignalBlocker(btn):
                btn.setChecked(False)
            return

        out_dir = self._terrain_output_dir()
        available = []
        for pat in self._TERRAIN_PATTERNS[key]:
            color_name, label, kind, ext = pat[0], pat[1], pat[2], pat[3]
            file_base = pat[4] if len(pat) > 4 else color_name
            path = os.path.join(out_dir, analysis_number, f"{file_base}{ext}")
            if os.path.exists(path):
                available.append((color_name, label, kind, path))

        if not available:
            self._set_load_status("No file")
            with QSignalBlocker(btn):
                btn.setChecked(False)
            return

        # 現在表示中のレイヤを削除
        proj = QgsProject.instance()
        for lid in self._loaded_terrain_layers.pop(key, []):
            if proj.mapLayer(lid):
                proj.removeMapLayer(lid)

        # 次の状態へ (-1=非表示, 0..N-1=ファイルインデックス)
        current = self._terrain_cycle_state.get(key, -1)
        n = len(available)
        next_state = current + 1 if current + 1 < n else -1
        self._terrain_cycle_state[key] = next_state

        if next_state == -1:
            # 非表示状態に戻る
            with QSignalBlocker(btn):
                btn.setChecked(False)
                btn.setText(base_label)
                btn.setStyleSheet(self._BTN_STYLE_NORMAL)
            self._set_load_status("")
            # 流量 OFF 時はバッファ層も非表示（state は保持）
            if key == "flow":
                for lid in self._flow_buffer_layer_ids:
                    if proj.mapLayer(lid):
                        proj.removeMapLayer(lid)
                self._flow_buffer_layer_ids = []
                from osgeo import gdal as _gdal
                for mp in self._flow_buffer_mem_paths:
                    _gdal.Unlink(mp)
                self._flow_buffer_mem_paths = []
            # 排他ペアを復元（stability ↔ integrated）
            partner = self._EXCLUSIVE.get(key)
            if partner and partner in self._exclusive_hidden:
                saved = self._exclusive_hidden.pop(partner)
                self._terrain_cycle_state[partner] = saved - 1
                self._cycle_terrain_layer(partner)
            self._refresh_preview_canvas()
            # レイヤ削除後にメインキャンバスを明示的にリフレッシュ
            if self.iface is not None:
                self.iface.mapCanvas().refresh()
            return

        # ファイルを読み込んで表示
        self._ensure_terrain_group(analysis_number)
        # 初回ON時のみ排他ペアを非表示（stability ↔ integrated）
        _blocking = False
        if current == -1:
            partner = self._EXCLUSIVE.get(key)
            if partner:
                partner_state = self._terrain_cycle_state.get(partner, -1)
                if partner_state != -1:
                    self._exclusive_hidden.pop(key, None)   # 自身の古い記録をクリア
                    self._exclusive_hidden[partner] = partner_state
                    self._hide_key(partner)
                    _blocking = True

        base_name, label, kind, path = available[next_state]
        lyr = (QgsRasterLayer(path, label) if kind == "raster"
               else QgsVectorLayer(path, label, "ogr"))
        if lyr.isValid():
            opacity = self._opacity_spinbox(key).value() / 100.0
            if kind == "raster":
                filter_mode = (
                    self._flow_filter_state(base_name)
                    if key == "flow" else self._filter_state.get(key, "off")
                )
                self._apply_raster_color(lyr, base_name, filter_mode)
                self._loaded_terrain_basenames[key] = base_name
                lyr.renderer().setOpacity(opacity)
            else:
                self._apply_vector_style(lyr, base_name)
                lyr.setOpacity(opacity)
            self._notify_layer_style_changed(lyr)
            proj.addMapLayer(lyr, False)
            self._insert_terrain_layer_ordered(key, lyr)
            self._loaded_terrain_layers[key] = [lyr.id()]
            self._refresh_preview_canvas()
            # プレビュー表示域にレイヤが含まれない場合はレイヤ範囲へズーム（CRS変換あり）
            if self.preview_canvas is not None:
                self._zoom_preview_to_layer_if_needed(lyr)
            self._zoom_preview_to_analysis_extent_if_available()
            status = f"Showing {label} ({next_state + 1}/{n})." if n > 1 else f"Showing {label}."
            self._set_load_status(status)
            # 流量レイヤー切替後: 表示中ベースのフィルタ/バッファ設定を
            # ボタンへ反映し、バッファを再適用（off なら旧バッファ層を除去）。
            if key == "flow":
                self._sync_flow_aux_controls()
                self._apply_flow_buffer()
        else:
            self._set_load_status(f"Load error: {label}")
            self._terrain_cycle_state[key] = current  # 状態を戻す

        with QSignalBlocker(btn):
            btn.setChecked(True)
            btn.setText(base_label)
            btn.setStyleSheet(
                self._BTN_STYLE_BLOCKING if _blocking else self._BTN_STYLE_NORMAL)

    def _toggle_terrain_layer(self, key, checked):
        """チェックON→選択解析番号のファイルを読込、OFF→該当レイヤを削除"""
        if checked:
            analysis_number = self.cmbAnalysisNumber.currentData()
            if not analysis_number:
                self._set_load_status("Select an analysis run")
                btn_map = {
                    "stability":  self.chkLoadStability,
                    "valley":     self.chkLoadValley,
                    "wetland":    self.chkLoadWetland,
                    "flow":       self.chkLoadFlow,
                    "integrated": self.chkLoadIntegrated,
                }
                btn = btn_map.get(key)
                if btn:
                    with QSignalBlocker(btn):
                        btn.setChecked(False)
                return
            out_dir = self._terrain_output_dir()
            # グループがなければ作成
            self._ensure_terrain_group(analysis_number)
            added = []
            ids = []
            patterns = self._TERRAIN_PATTERNS[key]
            for pat in patterns:
                color_name, label, kind, ext = pat[0], pat[1], pat[2], pat[3]
                file_base = pat[4] if len(pat) > 4 else color_name
                path = os.path.join(out_dir, analysis_number, f"{file_base}{ext}")
                if not os.path.exists(path):
                    continue
                lyr = (QgsRasterLayer(path, label) if kind == "raster"
                       else QgsVectorLayer(path, label, "ogr"))
                if lyr.isValid():
                    opacity = self._opacity_spinbox(key).value() / 100.0
                    if kind == "raster":
                        try:
                            self._apply_raster_color(lyr, color_name)
                        except Exception:  # nosec B110
                            pass
                        lyr.renderer().setOpacity(opacity)
                    else:
                        try:
                            self._apply_vector_style(lyr, color_name)
                        except Exception:  # nosec B110
                            pass
                        lyr.setOpacity(opacity)
                    self._notify_layer_style_changed(lyr)
                    ids.append(lyr.id())
                    QgsProject.instance().addMapLayer(lyr, False)
                    self._insert_terrain_layer_ordered(key, lyr)
                    added.append(label)
            self._loaded_terrain_layers[key] = ids
            self._refresh_preview_canvas()
            self._zoom_preview_to_analysis_extent_if_available()
            if added:
                names = ", ".join(added)
                n_added = len(added)
                n_total = len(patterns)
                if n_added > 1 or n_total > 1:
                    status = f"Showing {names} ({n_added}/{n_total})."
                else:
                    status = f"Showing {names}."
                self._set_load_status(status)
            else:
                self._set_load_status("No file")
        else:
            ids = self._loaded_terrain_layers.pop(key, [])
            proj = QgsProject.instance()
            for lid in ids:
                if proj.mapLayer(lid):
                    proj.removeMapLayer(lid)
            self._refresh_preview_canvas()
            self._set_load_status("")
            self._zoom_preview_to_analysis_extent_if_available()

    def _on_browse_dem(self):
        if self._dem_loading:
            # 取得中は Cancel ボタン: ワーカーを止める
            self._cancel_dem_job()
            return
        if self._dem_path:
            # クリア
            _was_vs = (self._dem_path == DemBrowserDialog.VS_LP_GRID_SENTINEL)
            _paired_dsm = DemBrowserDialog.REMOTE_DSM_PAIR.get(self._dem_path)
            _clear_dsm = _was_vs or (
                _paired_dsm is not None and self._dsm_path == _paired_dsm)
            self._dem_path = ""
            self._dem_actual_path = None
            self._terrain_loader = None
            self._vs_dem_codes = []
            self._partial_outside_warned = False
            self.txtDemPath.clear()
            self.txtDemPath.setToolTip("")
            self.lblDemInfo.setText("Not set")
            self.btnBrowseDem.setText("Browse")
            self._update_resample_indicator()
            if _clear_dsm:
                # Paired DSM sources are cleared together with their DEM.
                self._dsm_path = ""
                self._dsm_loader = None
                self._vs_dsm_codes = []
                self.txtDsmPath.clear()
                self.txtDsmPath.setToolTip("")
                self.lblDsmInfo.setText("Not set")
                self._update_flow_coef_state()
            self.btnBrowseDsm.setEnabled(True)
            self.btnBrowseDsm.setText("Browse" if not self._dsm_path else "Clear")
            self._reset_vs_export_state()
            self._update_vs_export_buttons()
            return
        initial_dir = ""
        dlg = DemBrowserDialog(self.preview_canvas, initial_dir=initial_dir, parent=self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            path = dlg.selected_path()
            if not path:
                return
            self._reset_vs_export_state()
            _tile_labels = {
                DemBrowserDialog.GSI_DEM1A_SENTINEL:       ("GSI DEM1A (1m)",      "Fetch DEM1A 1m tiles for canvas extent"),
                DemBrowserDialog.GSI_DEM5A_SENTINEL:       ("GSI DEM5A (5m)",      "Fetch DEM5A 5m tiles for canvas extent"),
                DemBrowserDialog.GSI_DEM10B_SENTINEL:      ("GSI DEM10B (10m)",    "Fetch DEM10B 10m tiles for canvas extent"),
                DemBrowserDialog.TERRARIUM_FINE_SENTINEL:     ("Terrarium (~2m)",   "Fetch Terrarium ~2m tiles for canvas extent"),
                DemBrowserDialog.TERRARIUM_STANDARD_SENTINEL: ("Terrarium (~5m)",   "Fetch Terrarium ~5m tiles for canvas extent"),
                DemBrowserDialog.TERRARIUM_WIDE_SENTINEL:     ("Terrarium (~10m)",  "Fetch Terrarium ~10m tiles for canvas extent"),
            }
            if path == DemBrowserDialog.VS_LP_GRID_SENTINEL:
                # VS LP/Grid: DSM は自動設定 → btnBrowseDsm を無効化
                self._dem_path = path
                self.txtDemPath.setText("VS LP/Grid (0.5m)")
                self.txtDemPath.setToolTip("Virtual Shizuoka LP/Grid — auto-fetch from S3 for canvas extent")
                self.btnBrowseDem.setText("Clear")
                self.btnBrowseDsm.setEnabled(False)
                self.lblDsmInfo.setText("Auto: VS LP → DSM (fetching after DEM…)")
                self._load_vs_lp_grid()  # auto_dsm=True (default)
            elif path in DemBrowserDialog.REMOTE_DEM_SENTINELS:
                self._dem_path = path
                self.txtDemPath.setText(
                    DemBrowserDialog.REMOTE_SHORT_NAMES[path])
                self.txtDemPath.setToolTip(
                    "Auto-fetch for the current canvas extent")
                self.btnBrowseDem.setText("Clear")
                self.btnBrowseDsm.setEnabled(True)
                pair = DemBrowserDialog.REMOTE_DSM_PAIR.get(path)

                def _after_dsm(_ok):
                    self._update_vs_export_buttons()

                def _after_dem(ok):
                    if not ok:
                        error_text = self.lblDemInfo.text()
                        self._dem_path = ""
                        self._dem_actual_path = None
                        self._terrain_loader = None
                        self.txtDemPath.clear()
                        self.txtDemPath.setToolTip("")
                        self.btnBrowseDem.setText("Browse")
                        self.lblDemInfo.setText(error_text)
                        return
                    if pair:
                        self._dsm_path = pair
                        self.txtDsmPath.setText(
                            DemBrowserDialog.REMOTE_SHORT_NAMES[pair])
                        self.txtDsmPath.setToolTip(
                            "Auto-fetch within the DEM extent")
                        self.btnBrowseDsm.setText("Clear")
                        self._load_remote_raster(
                            pair, target="dsm", on_done=_after_dsm)
                        return
                    self._update_vs_export_buttons()

                self._load_remote_raster(
                    path, target="dem", on_done=_after_dem)
            elif path in _tile_labels:
                display, tooltip = _tile_labels[path]
                self._dem_path = path
                self.txtDemPath.setText(display)
                self.txtDemPath.setToolTip(f"Elevation tile ({tooltip})")
                self.btnBrowseDem.setText("Clear")
                self.btnBrowseDsm.setEnabled(True)
                self._load_gsi_dem(path)
            else:
                # ローカルファイル（従来処理）
                self._dem_path = path
                self._dem_actual_path = path  # 実パスと一致
                self.txtDemPath.setText(os.path.basename(path))
                self.txtDemPath.setToolTip(path)
                self.btnBrowseDsm.setEnabled(True)
                if not dlg.filter_active():
                    self._move_preview_to_dem_extent(path)
                self._load_dem_info()
        self._update_vs_export_buttons()

    @staticmethod
    def _reproject_to_utm(src_path, centre_lon, centre_lat):
        """EPSG:4326 の GeoTIFF をキャンバス中心座標から自動判定した UTM に変換して返す。
        失敗時は元パスをそのまま返す。"""
        try:
            from osgeo import gdal
            utm_zone = int((centre_lon + 180) / 6) + 1
            epsg = 32600 + utm_zone if centre_lat >= 0 else 32700 + utm_zone
            base, ext = os.path.splitext(src_path)
            dst_path = f"{base}_utm{utm_zone}{ext}"
            result = gdal.Warp(
                dst_path, src_path,
                dstSRS=f"EPSG:{epsg}",
                resampleAlg=gdal.GRA_Bilinear,
                multithread=True,
            )
            if result is None:
                return src_path
            result = None  # GDALデータセットをフラッシュ・クローズ
            return dst_path
        except Exception:
            return src_path

    def _load_remote_raster(self, sentinel, target="dem", on_done=None,
                            mirror_label=None):
        """Fetch an AHN, IGN, or LINZ raster for the current analysis extent."""
        from .dem_worker import REMOTE_SOURCES, fetch_remote_raster

        spec = REMOTE_SOURCES[sentinel]
        label = self.lblDsmInfo if target == "dsm" else self.lblDemInfo

        def _done(ok):
            if on_done is not None:
                on_done(ok)

        source_crs = QgsCoordinateReferenceSystem(f"EPSG:{spec['epsg']}")
        bbox = self._dem_extent_in(source_crs) if target == "dsm" else None
        if bbox is None:
            if self.preview_canvas is None or self.preview_canvas.extent().isEmpty():
                label.setText(
                    "⚠ No extent on preview canvas. Display a map and try again.")
                _done(False)
                return
            canvas_crs = self.preview_canvas.mapSettings().destinationCrs()
            extent = QgsCoordinateTransform(
                canvas_crs, source_crs, QgsProject.instance()
            ).transformBoundingBox(self.preview_canvas.extent())
            bbox = (extent.xMinimum(), extent.yMinimum(),
                    extent.xMaximum(), extent.yMaximum())
        out_dir = os.path.join(self._terrain_output_dir(), "remote_dem")
        label.setText(f"Fetching {spec['label']}...")

        def _job(ctx):
            return fetch_remote_raster(ctx, sentinel, bbox, out_dir)

        def _finish(result, cancelled, error):
            ok = self._finish_remote_raster(
                result, cancelled, error, spec, target)
            _done(ok)

        self._start_dem_job(
            _job, _finish, mirror_label=mirror_label,
            button="dsm" if target == "dsm" else "dem", label=label)

    def _dem_extent_in(self, crs):
        """Return the loaded DEM extent transformed to crs."""
        loader = getattr(self, "_terrain_loader", None)
        path = getattr(loader, "path", None) if loader is not None else None
        if not path or not os.path.isfile(path) or not getattr(loader, "crs_wkt", None):
            return None
        try:
            from .dem_worker import _raster_bbox
            x0, y0, x1, y1 = _raster_bbox(path)
            dem_crs = QgsCoordinateReferenceSystem()
            dem_crs.createFromWkt(loader.crs_wkt)
            rect = QgsRectangle(x0, y0, x1, y1)
            if dem_crs.isValid() and dem_crs != crs:
                rect = QgsCoordinateTransform(
                    dem_crs, crs, QgsProject.instance()
                ).transformBoundingBox(rect)
            return (rect.xMinimum(), rect.yMinimum(),
                    rect.xMaximum(), rect.yMaximum())
        except Exception:
            return None

    def _finish_remote_raster(self, result, cancelled, error, spec, target):
        """Apply a completed overseas DEM/DSM fetch on the main thread."""
        label = self.lblDsmInfo if target == "dsm" else self.lblDemInfo
        if cancelled:
            label.setText("Cancelled")
            return False
        if error:
            label.setText(error)
            return False
        loader = result["loader"]
        tif_path = result["tif_path"]
        if target == "dsm":
            self._dsm_loader = loader
            self.txtDsmPath.setToolTip(tif_path)
            self.lblDsmInfo.setText(
                f"{loader.info_text()}  [{spec['label']}]")
            self._update_flow_coef_state()
            return True
        self._terrain_loader = loader
        self._dem_actual_path = tif_path
        self._vs_dem_codes = []
        self._partial_outside_warned = False
        self.txtDemPath.setToolTip(tif_path)
        self.lblDemInfo.setText(
            f"{loader.info_text()}  [{spec['label']}]")
        self._update_resample_indicator()
        return True

    def _load_gsi_dem(self, sentinel=None, on_done=None, mirror_label=None):
        """国土地理院タイルをキャンバス範囲で取得し GeoTIFF に保存後 DEMLoader で読み込む。
        sentinel で DEM1A/DEM5A/DEM10B を選択（省略時は _dem_path から判定）。

        取得はワーカースレッドで行うので、この関数はすぐ戻る。終わったら
        on_done(ok) を呼ぶ（ok=取得できて _terrain_loader を差し替えたか）。
        取得を始めずに戻る場合（プレビューに範囲が無い）も on_done(False) を呼ぶ。
        mirror_label: 状況の文言を lblDemInfo に加えて出すラベル（解析タブ等）。
        """
        from qgis.core import (QgsCoordinateReferenceSystem,
                               QgsCoordinateTransform, QgsProject)

        if sentinel is None:
            sentinel = getattr(self, "_dem_path", DemBrowserDialog.GSI_DEM5A_SENTINEL)

        # sentinel → (sources リスト, ファイル名プレフィクス)
        _S = DemBrowserDialog
        _TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
        _SOURCE_MAP = {
            _S.GSI_DEM1A_SENTINEL:  (
                [("https://cyberjapandata.gsi.go.jp/xyz/dem1a_png/{z}/{x}/{y}.png", 17, "DEM1A 1m",   "gsi")],
                "gsi_dem1a"),
            _S.GSI_DEM5A_SENTINEL:  (
                [("https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png", 15, "DEM5A 5m",   "gsi")],
                "gsi_dem5a"),
            _S.GSI_DEM10B_SENTINEL: (
                [("https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png",   14, "DEM10B 10m", "gsi")],
                "gsi_dem10b"),
            _S.TERRARIUM_FINE_SENTINEL:     (
                [(_TERRARIUM_URL, 14, "Terrarium ~2m",  "terrarium")],
                "terrarium_fine"),
            _S.TERRARIUM_STANDARD_SENTINEL: (
                [(_TERRARIUM_URL, 13, "Terrarium ~5m",  "terrarium")],
                "terrarium_std"),
            _S.TERRARIUM_WIDE_SENTINEL:     (
                [(_TERRARIUM_URL, 12, "Terrarium ~10m", "terrarium")],
                "terrarium_wide"),
        }
        sources, fname_prefix = _SOURCE_MAP.get(sentinel, _SOURCE_MAP[_S.GSI_DEM5A_SENTINEL])

        if self.preview_canvas is None or self.preview_canvas.extent().isEmpty():
            self.lblDemInfo.setText("⚠ No extent on preview canvas. Display a map and try again.")
            if on_done is not None:
                on_done(False)
            return

        canvas_ext = self.preview_canvas.extent()
        canvas_crs = self.preview_canvas.mapSettings().destinationCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        xform = QgsCoordinateTransform(canvas_crs, wgs84, QgsProject.instance())
        ext84 = xform.transformBoundingBox(canvas_ext)

        _is_terrarium = sentinel in (
            DemBrowserDialog.TERRARIUM_FINE_SENTINEL,
            DemBrowserDialog.TERRARIUM_STANDARD_SENTINEL,
            DemBrowserDialog.TERRARIUM_WIDE_SENTINEL,
        )
        _fetch_msg = "Fetching Terrarium elevation tiles..." if _is_terrarium else "Fetching GSI elevation tiles..."
        self.lblDemInfo.setText(_fetch_msg)

        # 取得・GeoTIFF 化・UTM への投影はワーカーで行い、終わったら
        # _finish_gsi_dem（メインスレッド）で画面へ反映して on_done を呼ぶ。
        from .dem_worker import fetch_tile_dem
        out_dir = os.path.join(self._terrain_output_dir(), "dem")
        _ext84 = (ext84.xMinimum(), ext84.yMinimum(), ext84.xMaximum(), ext84.yMaximum())
        _reproject = self._reproject_to_utm

        def _job(ctx):
            return fetch_tile_dem(ctx, sources, _ext84, out_dir, fname_prefix,
                                  _fetch_msg, _reproject)

        def _finish(result, cancelled, error):
            ok = self._finish_gsi_dem(result, cancelled, error)
            if on_done is not None:
                on_done(ok)

        self._start_dem_job(_job, _finish, mirror_label=mirror_label)

    def _finish_gsi_dem(self, result, cancelled, error):
        """タイル取得の結果を画面へ反映する（メインスレッド）。成功なら True。"""
        if cancelled:
            self.lblDemInfo.setText("Cancelled")
            return False
        if error:
            self.lblDemInfo.setText(error)
            return False
        dem_loader = result["loader"]
        tif_path = result["tif_path"]
        self._terrain_loader = dem_loader
        self._dem_actual_path = tif_path  # 実ファイルパス（params.json 用）
        # _dem_path は sentinel のまま維持（GSI ソースを追跡するため）
        self._vs_dem_codes = []  # 非 VS ソース: コードなし
        self._partial_outside_warned = False

        self.txtDemPath.setToolTip(tif_path)
        self.lblDemInfo.setText(dem_loader.info_text())
        self._update_resample_indicator()
        return True

    def _load_vs_lp_grid(self, auto_dsm=True, on_done=None, mirror_label=None):
        """Virtual Shizuoka LP/Grid タイルをキャンバス範囲で S3 取得し DEM としてロード。
        auto_dsm=True のとき、DEM 成功後に LP/Ground DSM を自動取得する（初回選択時）。
        解析時の再取得など DSM 不要の場合は auto_dsm=False を指定する。

        取得はワーカースレッドで行うので、この関数はすぐ戻る。終わったら
        （auto_dsm のときは DSM まで終わったら）on_done(ok) を呼ぶ。
        ok は DEM を取得できて _terrain_loader を差し替えたか（DSM の成否は含まない）。
        取得を始めずに戻る場合（範囲が無い・タイルが無い）も on_done(False)。
        mirror_label: 状況の文言を lblDemInfo に加えて出すラベル（解析タブ等）。
        """
        from .vs_lp import tiles_for_extent
        from qgis.core import (QgsCoordinateReferenceSystem,
                               QgsCoordinateTransform, QgsProject)

        def _done(ok):
            if on_done is not None:
                on_done(ok)

        if self.preview_canvas is None or self.preview_canvas.extent().isEmpty():
            self.lblDemInfo.setText("⚠ No extent on preview canvas.")
            _done(False)
            return

        canvas_ext = self.preview_canvas.extent()
        canvas_crs = self.preview_canvas.mapSettings().destinationCrs()
        crs_6676 = QgsCoordinateReferenceSystem("EPSG:6676")
        xform = QgsCoordinateTransform(canvas_crs, crs_6676, QgsProject.instance())
        ext6 = xform.transformBoundingBox(canvas_ext)

        codes = tiles_for_extent(
            ext6.xMinimum(), ext6.yMinimum(),
            ext6.xMaximum(), ext6.yMaximum(),
        )
        if not codes:
            self.lblDemInfo.setText("⚠ No VS LP tiles for this area.")
            _done(False)
            return

        self.lblDemInfo.setText(f"Resolving {len(codes)} tile(s)...")
        from .dem_worker import fetch_vs_grid
        out_dir = os.path.join(self._terrain_output_dir(), "vs_lp_grid")

        def _job(ctx):
            return fetch_vs_grid(ctx, codes, out_dir)

        def _finish(result, cancelled, error):
            ok = self._finish_vs_lp_grid(result, cancelled, error)
            # ── 自動 DSM（VS LP/Ground）──────────────────────────────
            if ok and auto_dsm:
                self._dsm_path = DemBrowserDialog.VS_LP_GROUND_SENTINEL
                self.txtDsmPath.setText("VS LP → DSM (0.5m)")
                self.txtDsmPath.setToolTip(
                    "Virtual Shizuoka LP → DSM — LP/Original (2021中西部・2025) or LP/Ground all-return (others)"
                )
                self.btnBrowseDsm.setText("")

                def _after_dsm(_dsm_ok):
                    # 取得失敗・キャンセル時は DSM パスをクリア
                    if not getattr(self, "_dsm_loader", None):
                        self._dsm_path = ""
                        self.txtDsmPath.clear()
                        self.txtDsmPath.setToolTip("")
                    self._update_vs_export_buttons()
                    _done(True)

                self._load_vs_lp_ground(auto_mode=True, on_done=_after_dsm,
                                        mirror_label=mirror_label)
                return
            if ok:
                self._update_vs_export_buttons()
            _done(ok)

        self._start_dem_job(_job, _finish, mirror_label=mirror_label)

    def _finish_vs_lp_grid(self, result, cancelled, error):
        """VS LP/Grid の取得結果を画面へ反映する（メインスレッド）。成功なら True。"""
        if cancelled:
            self.lblDemInfo.setText("Cancelled")
            return False
        if error:
            self.lblDemInfo.setText(error)
            return False
        dem_loader = result["loader"]
        tif_path = result["tif_path"]
        resolved = result["resolved"]
        self._terrain_loader = dem_loader
        self._dem_actual_path = tif_path
        self.txtDemPath.setToolTip(tif_path)
        self.lblDemInfo.setText(
            f"{dem_loader.info_text()}  [{len(resolved)} tile(s), "
            f"year(s): {', '.join(str(y) for y in sorted(set(resolved.values())))}]"
        )
        self._vs_dem_codes = list(resolved.keys())
        self._partial_outside_warned = False
        self._update_resample_indicator()
        return True

    # ── DEM 取得のワーカー（dem_worker.DemJobWorker）──────────────────────
    # 取得中は DEM（または DSM）の Browse ボタンが Cancel になる（今までどおり）。
    # 結果とぶつかる操作（もう一方の Browse・VS の書き出し・Run Analysis）は止める。

    def _start_dem_job(self, job, on_finish, mirror_label=None, button="dem", label=None):
        """job をワーカーで始める。終わったら on_finish(result, cancelled, error)
        をメインスレッドで呼ぶ。中止・エラー時は job が作ったファイルを消す。

        button: Cancel にするボタン（"dem"=btnBrowseDem / "dsm"=btnBrowseDsm）
        label : 状況の文言を出すラベル（省略時は button 側の Info ラベル）
        """
        from .dem_worker import DemJobWorker
        if label is None:
            label = self.lblDsmInfo if button == "dsm" else self.lblDemInfo
        worker = DemJobWorker(job, parent=self)
        self._dem_worker = worker
        self._dem_job_ctx = {
            "on_finish": on_finish,
            "mirror": mirror_label,
            "button": button,
            "label": label,
            "project_changed": False,
        }
        if button == "dsm":
            self._dsm_loading = True
        else:
            self._dem_loading = True
        self._set_dem_busy(True, button)
        worker.message.connect(self._on_dem_job_message)
        worker.done.connect(self._on_dem_job_done)
        worker.start()

    def _set_dem_busy(self, busy, button="dem"):
        """取得中の画面の状態。button 側の Browse を Cancel にし、もう一方は止める。"""
        cancel_btn = self.btnBrowseDsm if button == "dsm" else self.btnBrowseDem
        other_btn = self.btnBrowseDem if button == "dsm" else self.btnBrowseDsm
        if busy:
            self._other_browse_enabled_before = other_btn.isEnabled()
            other_btn.setEnabled(False)
            cancel_btn.setText("Cancel")
            cancel_btn.setEnabled(True)
        else:
            other_btn.setEnabled(getattr(self, "_other_browse_enabled_before", True))
            cancel_btn.setEnabled(True)
            if button == "dsm":
                cancel_btn.setText("Clear" if self._dsm_path else "Browse")
            else:
                cancel_btn.setText("Clear" if self._dem_path else "Browse")
        if self._analysis_worker is None:
            self.btnRunAnalysis.setEnabled(not busy)
        self._update_vs_export_buttons()

    def _on_dem_job_message(self, text):
        ctx = self._dem_job_ctx
        if ctx is None:
            return
        ctx["label"].setText(text)
        if ctx["mirror"] is not None:
            ctx["mirror"].setText(text)

    def _on_dem_job_done(self, result, cancelled, error):
        worker = self._dem_worker
        ctx = self._dem_job_ctx
        self._dem_worker = None
        self._dem_job_ctx = None
        files = []
        if worker is not None:
            files = worker.new_files()
            worker.deleteLater()
        if ctx is None:
            return
        if ctx["project_changed"]:
            # 取得中にプロジェクトが切り替わった: 中止として扱い、結果は使わない
            result, cancelled, error = None, True, ""
        if cancelled or error:
            for f in files:
                try:
                    os.remove(f)
                except OSError:
                    pass
        if ctx["button"] == "dsm":
            self._dsm_loading = False
        else:
            self._dem_loading = False
        self._set_dem_busy(False, ctx["button"])
        ctx["on_finish"](result, cancelled, error)
        self._update_vs_export_buttons()

    def _cancel_dem_job(self):
        worker = self._dem_worker
        ctx = self._dem_job_ctx
        if worker is not None and worker.isRunning() and ctx is not None:
            worker.cancel()
            (self.btnBrowseDsm if ctx["button"] == "dsm" else self.btnBrowseDem).setEnabled(False)
            ctx["label"].setText("Cancelling…")
            return True
        return False

    def _shutdown_dem_job(self):
        """パネルを閉じる・プラグインを外すとき: DEM 取得を中止して終わるのを待つ。"""
        worker = self._dem_worker
        ctx = self._dem_job_ctx
        if worker is None:
            return
        try:
            worker.done.disconnect(self._on_dem_job_done)
        except (TypeError, RuntimeError):
            pass
        worker.cancel()
        if worker.isRunning():
            worker.wait(30000)
        for f in worker.new_files():
            try:
                os.remove(f)
            except OSError:
                pass
        self._dem_worker = None
        self._dem_job_ctx = None
        self._dem_loading = False
        self._dsm_loading = False
        # 取得中に止めた部品を戻す（開き直したときに押せないまま残らないように）
        button = ctx["button"] if ctx is not None else "dem"
        if ctx is not None:
            ctx["label"].setText("Cancelled")
        self._set_dem_busy(False, button)

    # ── 書き出し（VS）のワーカー ───────────────────────────────
    # DEM 取得（_dem_worker）とは別に、name ごとに1つずつ動かす。

    def _start_side_job(self, name, job, on_message, on_finish, on_abort=None):
        """job をワーカーで始める。終わったら on_finish(result, cancelled, error)。
        中止・エラー時は job が作ったファイルを消す。on_abort はパネルを閉じる
        ときに画面の状態を戻すために呼ぶ（on_finish は呼ばない）。"""
        from .dem_worker import DemJobWorker
        worker = DemJobWorker(job, parent=self)
        self._side_jobs[name] = {
            "worker": worker,
            "on_finish": on_finish,
            "on_abort": on_abort,
            "project_changed": False,
        }
        worker.message.connect(on_message)
        worker.done.connect(
            lambda r, c, e, _n=name: self._on_side_job_done(_n, r, c, e))
        worker.start()

    def _on_side_job_done(self, name, result, cancelled, error):
        ctx = self._side_jobs.pop(name, None)
        if ctx is None:
            return  # パネルを閉じるときに片付け済み
        worker = ctx["worker"]
        files = worker.new_files()
        worker.deleteLater()
        if ctx["project_changed"]:
            # 書き出し中にプロジェクトが切り替わった: 中止として扱う
            result, cancelled, error = None, True, ""
        if cancelled or error:
            for f in files:
                try:
                    os.remove(f)
                except OSError:
                    pass
        ctx["on_finish"](result, cancelled, error)

    def _cancel_side_job(self, name):
        ctx = self._side_jobs.get(name)
        if ctx is not None and ctx["worker"].isRunning():
            ctx["worker"].cancel()
            return True
        return False

    def _shutdown_side_jobs(self):
        """パネルを閉じる・プラグインを外すとき: 書き出しを中止して終わるのを待つ。"""
        for name in list(self._side_jobs):
            ctx = self._side_jobs.pop(name)
            worker = ctx["worker"]
            worker.cancel()
            if worker.isRunning():
                worker.wait(30000)
            for f in worker.new_files():
                try:
                    os.remove(f)
                except OSError:
                    pass
            if ctx["on_abort"] is not None:
                ctx["on_abort"]()

    def _load_vs_lp_ground(self, auto_mode=False, on_done=None, mirror_label=None):
        """Virtual Shizuoka LP タイルをキャンバス範囲で S3 取得し LAS→DSM 変換して DSM としてロード。

        取得優先順位:
            1. LP/Original（全リターン）が存在する場合はそれを使用 → 2021中西部・2025
            2. 存在しない場合は LP/Ground を使用 → 2019/2020/2021富士山東部/2022 は全リターン
        いずれも全リターンデータのため DSM 計算に有効。

        auto_mode=True のとき DEM ロードの続きとして呼ばれ、DEM の Browse ボタンが
        Cancel になる（単独で呼ぶときは DSM の Browse ボタン）。

        取得・LAS→DSM 変換はワーカースレッドで行うので、この関数はすぐ戻る。
        終わったら on_done(ok) を呼ぶ（ok=DSM を取得できて _dsm_loader を差し替えたか）。
        取得を始めずに戻る場合（範囲・タイルが無い・確認で No）も on_done(False)。
        """
        from .vs_lp import tiles_for_extent
        from qgis.core import (QgsCoordinateReferenceSystem,
                               QgsCoordinateTransform, QgsProject)

        def _done(ok):
            if on_done is not None:
                on_done(ok)

        if self.preview_canvas is None or self.preview_canvas.extent().isEmpty():
            self.lblDsmInfo.setText("⚠ No canvas extent.")
            _done(False)
            return

        canvas_crs = self.preview_canvas.mapSettings().destinationCrs()
        crs6676 = QgsCoordinateReferenceSystem("EPSG:6676")
        xform = QgsCoordinateTransform(canvas_crs, crs6676, QgsProject.instance())
        ext6 = xform.transformBoundingBox(self.preview_canvas.extent())
        codes = tiles_for_extent(
            ext6.xMinimum(), ext6.yMinimum(),
            ext6.xMaximum(), ext6.yMaximum(),
        )
        if not codes:
            self.lblDsmInfo.setText("⚠ No VS LP tiles for this area.")
            _done(False)
            return

        # ── DSM 作成確認ダイアログ ───────────────────────────────────────────
        reply = QtWidgets.QMessageBox.question(
            self,
            "DSM Creation",
            "DSM creation may require a large amount of data.\n"
            "Basic analysis is possible without LAS.\n\n"
            "Do you want to continue DSM creation?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            self.lblDsmInfo.setText("Skipped — no LAS")
            self._update_vs_export_buttons()
            _done(False)
            return

        self.lblDsmInfo.setText(f"Resolving {len(codes)} tile(s)...")
        from .dem_worker import fetch_vs_dsm
        out_dir = os.path.join(self._terrain_output_dir(), "vs_lp_ground")

        def _job(ctx):
            return fetch_vs_dsm(ctx, codes, out_dir)

        def _finish(result, cancelled, error):
            ok = self._finish_vs_lp_ground(result, cancelled, error, auto_mode)
            _done(ok)

        self._start_dem_job(_job, _finish, mirror_label=mirror_label,
                            button="dem" if auto_mode else "dsm",
                            label=self.lblDsmInfo)

    def _finish_vs_lp_ground(self, result, cancelled, error, auto_mode):
        """VS LP → DSM の取得結果を画面へ反映する（メインスレッド）。成功なら True。

        LAS の一覧（VS の書き出しで使う）は、成功したときだけ差し替える
        （中止・エラーでは前の DSM と一覧のまま）。
        """
        if cancelled:
            self.lblDsmInfo.setText("Cancelled")
            return False
        if error:
            self.lblDsmInfo.setText(error)
            return False
        dsm_loader = result["loader"]
        tif_path = result["tif_path"]
        resolved = result["resolved"]
        self._vs_las_paths = result["las_paths"]
        self._vs_las_urls = result["las_urls"]
        self._dsm_loader = dsm_loader
        self.txtDsmPath.setToolTip(tif_path)
        self.lblDsmInfo.setText(
            f"{dsm_loader.info_text()}  [{len(resolved)} tile(s), "
            f"year(s): {', '.join(str(y) for y in sorted(set(resolved.values())))}]"
        )
        self._vs_dsm_codes = list(resolved.keys())
        self._update_flow_coef_state()
        if not auto_mode:
            self._update_vs_export_buttons()
        return True

    def _zoom_preview_to_analysis_extent_if_available(self):
        if self.preview_canvas is None:
            return
        ext = self._analysis_layers_extent_in_canvas_crs()
        if ext is None or ext.isEmpty():
            ext = self._get_analysis_extent()
        if ext is None or ext.isEmpty():
            return
        # 解析範囲がすでにプレビュー内に見えている場合はズームしない
        if self.preview_canvas.extent().intersects(ext):
            return
        _prev = getattr(self, "_syncing", False)
        self._syncing = True
        try:
            self.preview_canvas.setExtent(ext)
        finally:
            self._syncing = _prev
        self.preview_canvas.refresh()

    def _analysis_layers_extent_in_canvas_crs(self):
        if self.preview_canvas is None:
            return None
        proj = QgsProject.instance()
        canvas_crs = self.preview_canvas.mapSettings().destinationCrs()
        combined = None
        for ids in getattr(self, "_loaded_terrain_layers", {}).values():
            for lid in ids:
                lyr = proj.mapLayer(lid)
                if lyr is None or not lyr.isValid():
                    continue
                ext = lyr.extent()
                if ext.isEmpty():
                    continue
                try:
                    lyr_crs = lyr.crs()
                    if lyr_crs.isValid() and canvas_crs.isValid() and lyr_crs != canvas_crs:
                        from qgis.core import QgsCoordinateTransform as _CT
                        xf = _CT(lyr_crs, canvas_crs, QgsProject.instance())
                        ext = xf.transformBoundingBox(ext)
                except Exception:  # nosec B110
                    pass
                if ext.isEmpty():
                    continue
                combined = ext if combined is None else combined.combineExtentWith(ext)
        return combined

    def _move_preview_to_dem_extent(self, path):
        """DEM のエクステントにプレビューキャンバスを移動して再描画する。"""
        if self.preview_canvas is None:
            return
        info = DemBrowserDialog._read_dem_extent(path)
        if info is None:
            return
        xmin, ymin, xmax, ymax, wkt = info
        try:
            dem_crs = QgsCoordinateReferenceSystem()
            dem_crs.createFromWkt(wkt)
            dem_rect = QgsRectangle(xmin, ymin, xmax, ymax)
            canvas_crs = self.preview_canvas.mapSettings().destinationCrs()
            if dem_crs.isValid() and canvas_crs.isValid() and dem_crs != canvas_crs:
                xform = QgsCoordinateTransform(dem_crs, canvas_crs, QgsProject.instance())
                dem_rect = xform.transformBoundingBox(dem_rect)
            self.preview_canvas.setExtent(dem_rect)
            self.preview_canvas.refresh()
        except Exception:  # nosec B110
            pass

    def _update_out_dir_label(self):
        """出力セクションのパス表示ラベルを更新する。"""
        if not hasattr(self, "lblOutDir"):
            return
        home = QgsProject.instance().homePath()
        if home:
            out = os.path.join(home, "forestry_operations_lite")
            self.lblOutDir.setPath("Output: ", out)
        else:
            self.lblOutDir.setPath("Output: (save project first)", "")

    def _terrain_output_dir(self):
        """プロジェクトフォルダ/forestry_operations_lite を返す。未保存時はフォールバック。"""
        home = QgsProject.instance().homePath()
        if home:
            out = os.path.join(home, "forestry_operations_lite")
        else:
            out = os.path.join(os.path.expanduser("~"), ".qgis", "forestry_operations_lite")
        os.makedirs(out, exist_ok=True)
        return out

    # ── 解析番号ユーティリティ ──────────────────────────────────────

    _MOVED_MARK = ".fol_moved_to_archive"

    def _remove_moved_run_folder(self, analysis_number, key):
        """Named Export へコピーし終えた元の解析番号のフォルダを消す。

        消せなければ（Windows でファイルが掴まれているなど）印を付けて一覧から外し、
        次に一覧を更新したときにもう一度消す（待たない）。
        """
        folder = os.path.join(self._terrain_output_dir(), analysis_number)
        try:
            shutil.rmtree(folder)
        except OSError:
            try:
                with open(os.path.join(folder, self._MOVED_MARK), "w", encoding="utf-8") as f:
                    f.write(key)
            except OSError:
                pass
        # 一覧から元の解析番号を外す（選択は書き出した Named Export のまま）
        idx = self.cmbAnalysisNumber.findData(analysis_number)
        if idx >= 0:
            with QSignalBlocker(self.cmbAnalysisNumber):
                self.cmbAnalysisNumber.removeItem(idx)

    def _purge_moved_run_folders(self):
        """印の付いた（Named Export へコピー済みの）解析番号のフォルダを、もう一度消してみる。"""
        out_dir = self._terrain_output_dir()
        if not os.path.isdir(out_dir):
            return
        for name in os.listdir(out_dir):
            folder = os.path.join(out_dir, name)
            if os.path.isfile(os.path.join(folder, self._MOVED_MARK)):
                shutil.rmtree(folder, ignore_errors=True)

    def _scan_analysis_numbers(self, include_moved=False):
        """出力ディレクトリから解析番号サブフォルダの一覧を返す。

        Named Export へコピー済みで消しきれていないフォルダ（印付き）は、
        include_moved=True のときだけ含める（新しい番号が重ならないようにするため）。
        """
        import re as _re
        out_dir = self._terrain_output_dir()
        if not os.path.isdir(out_dir):
            return []
        numbers = [
            name for name in os.listdir(out_dir)
            if os.path.isdir(os.path.join(out_dir, name))
            and _re.fullmatch(r'\d{4}(\+\d+)?', name)
            and (include_moved
                 or not os.path.isfile(os.path.join(out_dir, name, self._MOVED_MARK)))
        ]
        return sorted(numbers, key=self._analysis_number_sort_key)

    @staticmethod
    def _analysis_number_sort_key(n: str):
        """解析番号のソートキー (seq, n_files) を返す"""
        if '+' in n:
            base, extra = n.split('+', 1)
            return (int(base[:3]), 10 + int(extra))
        return (int(n[:3]), int(n[3]))

    @staticmethod
    def _format_analysis_number(seq: str, n_files: int) -> str:
        """解析番号文字列を生成 (例: "0013", "0010+0", "0010+2")"""
        if n_files <= 9:
            return f"{seq}{n_files}"
        return f"{seq}0+{n_files - 10}"

    def _next_seq(self, overwrite: bool) -> str:
        """
        次の解析シーケンス番号(3桁)を返す。
        overwrite=True の場合は最新シーケンス番号を返す。
        既存フォルダの削除は解析実行前に行う。
        """
        numbers = self._scan_analysis_numbers()
        if overwrite and numbers:
            return f"{max(int(n[:3]) for n in numbers):03d}"
        # 新しい番号は、消しきれていないコピー済みのフォルダとも重ならないようにする
        all_numbers = self._scan_analysis_numbers(include_moved=True)
        if not all_numbers:
            return "001"
        max_seq = max(int(n[:3]) for n in all_numbers)
        if overwrite:
            return f"{max_seq:03d}"
        return f"{max_seq + 1:03d}"

    def _refresh_analysis_combo(self, select_latest=True, gc_cache=True, select_data=None):
        """解析番号コンボを出力ディレクトリの内容で更新する。
        select_latest=True（デフォルト）: 最新番号を自動選択して表示待機状態にする。
        select_latest=False: 現在の選択を維持する（解析完了後の更新など）。
        gc_cache=False: DEM キャッシュの掃除をしない（プロジェクト切り替えの途中など）。
        select_data: 更新後に選ぶ項目（archive へ移した直後など）。
        解析番号の下に「── archive ──」の区切りを入れ、archive を作った順に並べる。
        長い名前は途中を省略して表示し、ツールチップに全体を出す。
        """
        try:
            migrated, migration_errors = self._reconnect_moved_exports()
        except Exception:  # 移行で思わぬエラーが出ても、一覧とパネルは開く
            import traceback as _tb
            from qgis.core import Qgis, QgsMessageLog
            QgsMessageLog.logMessage(
                "Named Exports reconnect failed:\n" + _tb.format_exc(),
                "Forestry Operations Lite", Qgis.Warning)
            migrated, migration_errors = [], ["see Log Messages (Forestry Operations Lite)"]
        self._purge_moved_run_folders()
        current = self.cmbAnalysisNumber.currentData()
        if select_data is not None:
            current, select_latest = select_data, False
        with QSignalBlocker(self.cmbAnalysisNumber):
            self.cmbAnalysisNumber.clear()
            self.cmbAnalysisNumber.addItem("Select run", None)
            numbers = self._scan_analysis_numbers()
            for num in reversed(numbers):
                self.cmbAnalysisNumber.addItem(num, num)
            archives = self._scan_archives()
            if archives:
                self.cmbAnalysisNumber.addItem("── Named Exports ──", None)
                _sep = self.cmbAnalysisNumber.model().item(self.cmbAnalysisNumber.count() - 1)
                if _sep is not None:
                    _sep.setEnabled(False)
                _fm = self.cmbAnalysisNumber.fontMetrics()
                _width = max(60, self.cmbAnalysisNumber.width() - 40)
                for _name in archives:
                    self.cmbAnalysisNumber.addItem(
                        _fm.elidedText(_name, Qt.ElideMiddle, _width), self._archive_key(_name))
                    self.cmbAnalysisNumber.setItemData(
                        self.cmbAnalysisNumber.count() - 1, _name, Qt.ToolTipRole)
            if select_latest and numbers:
                # 最新番号（降順先頭 = インデックス1）を選択して表示待機状態にする
                self.cmbAnalysisNumber.setCurrentIndex(1)
            else:
                # 以前の選択を復元
                idx = self.cmbAnalysisNumber.findData(current)
                self.cmbAnalysisNumber.setCurrentIndex(idx if idx >= 0 else 0)
        # 選択変更を手動で通知（QSignalBlocker スコープ内に setCurrentIndex したため）
        self._on_analysis_number_changed(self.cmbAnalysisNumber.currentIndex())
        # トグルボタンの有効/無効を更新
        has_data = self.cmbAnalysisNumber.count() > 1
        for _b in (self.chkLoadStability, self.chkLoadValley,
                   self.chkLoadWetland, self.chkLoadFlow, self.chkLoadIntegrated):
            _b.setEnabled(has_data)
        # 解析番号に紐づかない DEM キャッシュを削除
        if gc_cache:
            self._gc_dem_cache()
        if migrated:
            self._set_load_status(
                f"Reconnected {len(migrated)} earlier export(s) to Named Exports. "
                "Save the project to keep the new locations.")
        if migrated or migration_errors:
            # パネルを作っている途中（描く前）に窓を出すとパネルが白いまま止まるので、
            # 表示して描き終わってから知らせる
            from qgis.PyQt.QtCore import QTimer as _QTimer
            _QTimer.singleShot(
                500, lambda: self._notify_legacy_export_migration(migrated, migration_errors))

    def _gc_dem_cache(self):
        """解析番号に紐づかない DEM キャッシュファイルを削除する。
        dem/、vs_lp_grid/、remote_dem/ 内のファイルのうち、いずれかの解析
        フォルダの params.json に DEM/DSM として記録されていないものを削除する。
        現在ロード中の DEM/DSM は保持する。
        """
        import json as _json
        out_dir = self._terrain_output_dir()

        # 参照されている dem_path を収集
        referenced = set()
        for num in self._scan_analysis_numbers():
            params_path = os.path.join(out_dir, num, "params.json")
            try:
                with open(params_path, "r", encoding="utf-8") as f:
                    p = _json.load(f)
                dp = p.get("dem_path", "")
                if dp:
                    referenced.add(os.path.normpath(dp))
                flow_conditions = p.get("flow_coefficients", {})
                dsm = (flow_conditions.get("dsm", {})
                       if isinstance(flow_conditions, dict) else {})
                dsm_path = dsm.get("path", "") if isinstance(dsm, dict) else ""
                if dsm_path:
                    referenced.add(os.path.normpath(dsm_path))
            except Exception:  # nosec B110
                pass
        # Named Export は自分の DEM（elev_relief.tif）を持つ。まだ写していない間は
        # 元の DEM を消さない（_copy_archive_dem）。
        referenced.update(self._archive_dems_not_yet_copied(out_dir))

        # 現在ロード中のファイルも保持
        cur = getattr(self, "_dem_actual_path", None)
        if cur:
            referenced.add(os.path.normpath(cur))
        dsm_loader = getattr(self, "_dsm_loader", None)
        cur = getattr(dsm_loader, "path", None) if dsm_loader is not None else None
        if cur:
            referenced.add(os.path.normpath(cur))

        # 管理対象の DEM/DSM キャッシュから未参照ファイルを削除
        for sub in ("dem", "vs_lp_grid", "remote_dem"):
            sub_dir = os.path.join(out_dir, sub)
            if not os.path.isdir(sub_dir):
                continue
            for fname in os.listdir(sub_dir):
                fpath = os.path.normpath(os.path.join(sub_dir, fname))
                if os.path.isfile(fpath) and fpath not in referenced:
                    try:
                        os.remove(fpath)
                    except Exception:  # nosec B110
                        pass

    _GROUP_PROP = "fop_managed"  # プラグイン管理グループ識別用プロパティ

    @staticmethod
    def _layer_opacity(layer):
        """レイヤーの不透明度。QGIS 3.16 のラスタはレイヤーに不透明度がないので renderer から読む。"""
        if hasattr(layer, "opacity"):
            return layer.opacity()
        renderer = layer.renderer() if hasattr(layer, "renderer") else None
        return renderer.opacity() if renderer is not None else 1.0

    @staticmethod
    def _set_layer_opacity(layer, value):
        """レイヤーの不透明度を付ける。QGIS 3.16 のラスタは renderer に付ける。"""
        if hasattr(layer, "setOpacity"):
            layer.setOpacity(value)
            return
        renderer = layer.renderer() if hasattr(layer, "renderer") else None
        if renderer is not None:
            renderer.setOpacity(value)

    # ── archive（Export to Project で名前を付けて移した解析）─────────────
    # 出力フォルダの archive/<名前>/ に解析番号のフォルダを移したもの。一覧での
    # キーは "archive/<名前>"（os.path.join(out_dir, key) でそのままフォルダになる）。
    # 上書き解析・DEM キャッシュの掃除の対象にはしない（解析番号として数えない）。
    _ARCHIVE_DIR = "archive"
    _ARCHIVE_INFO = "fol_archive.json"
    # 書き出したグループの目印（4.x の Named Exports と同じ名前・同じ値）
    _ARCHIVE_GROUP_PROP = "fop_archive_key"
    _INVALID_NAME_CHARS = '\\/:*?"<>|'

    @classmethod
    def _archive_key(cls, name):
        return f"{cls._ARCHIVE_DIR}/{name}"

    @classmethod
    def _archive_name(cls, key):
        """archive のキーなら名前、解析番号なら None。"""
        prefix = cls._ARCHIVE_DIR + "/"
        if key and str(key).startswith(prefix):
            return str(key)[len(prefix):]
        return None

    def _run_label(self, key):
        name = self._archive_name(key)
        return name if name is not None else key

    def _scan_archives(self):
        """archive の名前の一覧（作った順）。"""
        import json as _json
        base = os.path.join(self._terrain_output_dir(), self._ARCHIVE_DIR)
        if not os.path.isdir(base):
            return []
        items = []
        for name in os.listdir(base):
            folder = os.path.join(base, name)
            if not os.path.isdir(folder) or name.startswith("."):
                continue
            created = ""
            try:
                with open(os.path.join(folder, self._ARCHIVE_INFO), "r", encoding="utf-8") as f:
                    created = str(_json.load(f).get("archived_at", ""))
            except (OSError, ValueError):
                pass
            if not created:
                try:
                    import datetime as _dt
                    created = _dt.datetime.fromtimestamp(os.path.getmtime(folder)).isoformat()
                except OSError:
                    created = ""
            items.append((created, name))
        return [name for _created, name in sorted(items)]

    def _current_display_state_data(self):
        """書き出したときの表示の状態（4.x の Named Exports と同じ形）。

        3.x の書き出しには Flow Buffer を含めないので、Buffer は常に off。
        """
        return {
            "filter_wetland": self._filter_state.get("wetland", "off"),
            "filter_flow": self._filter_state.get("flow", "off"),
            "filter_tc": getattr(self, "_filter_state_tc", "off"),
            "buffer_flow": "off",
            "buffer_tc": "off",
            "cycle_state": dict(self._terrain_cycle_state),
            "opacity": {key: self._opacity_spinbox(key).value() for key in self._BTN_LABELS},
        }

    def _update_archive_info(self, key, **updates):
        """fol_archive.json の内容を保ったまま、updates を書き足す。"""
        import json as _json
        path = os.path.join(self._terrain_output_dir(), key, self._ARCHIVE_INFO)
        info = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                info = _json.load(f)
            if not isinstance(info, dict):
                info = {}
        except (OSError, ValueError):
            info = {}
        info.update(updates)
        try:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(info, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _archive_key_of_group(self, group):
        """利用者側のグループが archive を書き出したものなら、そのキー（無ければ None）。

        目印（_ARCHIVE_GROUP_PROP）が無くても、レイヤーが archive/<名前>/ の
        ファイルを見ていれば、その archive とみなす。
        """
        stored = str(group.customProperty(self._ARCHIVE_GROUP_PROP, "") or "")
        if self._archive_name(stored) is not None:
            return stored
        root_dir = os.path.normcase(os.path.abspath(
            os.path.join(self._terrain_output_dir(), self._ARCHIVE_DIR)))
        for node in group.findLayers():
            lyr = node.layer()
            if lyr is None:
                continue
            path = os.path.normcase(os.path.abspath(str(lyr.source() or "").split("|", 1)[0]))
            try:
                if os.path.commonpath((path, root_dir)) != root_dir:
                    continue
            except ValueError:
                continue
            parts = os.path.relpath(path, root_dir).split(os.sep, 1)
            if len(parts) == 2 and parts[0]:
                return self._archive_key(parts[0])
        return None

    @staticmethod
    def _all_tree_groups(node=None):
        """レイヤーツリーのグループを、中のグループまで全部返す。

        QGIS 3.16 の findGroups() は引数（recursive）を取らないので、自分でたどる。
        """
        from qgis.core import QgsLayerTree
        if node is None:
            node = QgsProject.instance().layerTreeRoot()
        groups = []
        for child in node.children():
            if QgsLayerTree.isGroup(child):
                groups.append(child)
                groups.extend(ForestryOperationsLiteDockWidget._all_tree_groups(child))
        return groups

    def _archive_in_project(self, key):
        """archive の key が、利用者側のグループとしてプロジェクトにあるか。"""
        for group in self._all_tree_groups():
            node, managed = group, False
            while node is not None:
                if hasattr(node, "customProperty") and node.customProperty(self._GROUP_PROP) == "1":
                    managed = True
                    break
                node = node.parent()
            if not managed and self._archive_key_of_group(group) == key:
                return True
        return False

    def _update_export_button(self, *_args):
        """読み込み済みの archive を選んでいるときは Export to Project を止める。"""
        btn = getattr(self, "btnExport", None)
        if btn is None:
            return
        key = self.cmbAnalysisNumber.currentData()
        if self._archive_name(key) is not None and self._archive_in_project(key):
            btn.setEnabled(False)
            btn.setToolTip("This Named Export is already in the project. Remove its group from "
                           "the Layers panel to add it again.")
        else:
            btn.setEnabled(True)
            btn.setToolTip(
                "Name the selected run, keep it as a Named Export and add its layers "
                "to the project with the current display settings. Later changes are made "
                "directly in QGIS (Layer Properties)."
                if self._archive_name(key) is None else
                "Add this Named Export to the project again (its saved style files are used).")

    def _archive_name_problem(self, name):
        """名前が使えなければ理由、使えれば ""。"""
        if not name:
            return "Enter a name."
        if any(c in self._INVALID_NAME_CHARS for c in name) or name.endswith("."):
            return ("The name contains characters that cannot be used in a folder name "
                    "(\\ / : * ? \" < > |, or a trailing period).")
        folder = os.path.join(self._terrain_output_dir(), self._ARCHIVE_DIR, name)
        if os.path.exists(folder):
            return "A Named Export with this name already exists. Choose another name."
        return ""

    def _ask_archive_name(self, default, legacy_names=None):
        """書き出す名前（= レイヤーグループ名・archive のフォルダ名）を尋ねる。"""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Export to Project")
        lay = QtWidgets.QVBoxLayout(dlg)
        _msg = QtWidgets.QLabel(
            "Name this export. The run is kept as a Named Export under this name.\n"
            "The current display is saved with the export. To change it later, edit the "
            "layers directly in QGIS (Layer Properties).")
        _msg.setWordWrap(True)
        lay.addWidget(_msg)
        if legacy_names:
            # 以前の書き出しは外して置き換えるので、前もって知らせる
            _legacy = QtWidgets.QLabel(
                "This run has an earlier export in the project (" + ", ".join(legacy_names)
                + "). It will be removed from the project and replaced by this export.")
            _legacy.setWordWrap(True)
            _legacy.setStyleSheet("color:#b05a00;")
            lay.addWidget(_legacy)
        edit = QtWidgets.QLineEdit(default)
        edit.selectAll()
        lay.addWidget(edit)
        warn = QtWidgets.QLabel("")
        warn.setStyleSheet("color:#c0392b;")
        warn.setWordWrap(True)
        lay.addWidget(warn)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        ok_btn = btns.button(QtWidgets.QDialogButtonBox.Ok)

        def _check(_text=None):
            problem = self._archive_name_problem(edit.text().strip())
            warn.setText(problem)
            ok_btn.setEnabled(not problem)

        edit.textChanged.connect(_check)
        _check()
        dlg.setMinimumWidth(420)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return None
        name = edit.text().strip()
        return name if not self._archive_name_problem(name) else None

    def _archive_run(self, analysis_number, name):
        """解析番号のフォルダを archive/<name>/ へコピーし、一覧でそれを選ぶ。成功なら True。

        元のフォルダは、書き出しのレイヤーを作ったあとで消す（_remove_moved_run_folder）。
        """
        import datetime as _dt
        import gc as _gc
        import json as _json
        from qgis.PyQt.QtCore import QCoreApplication as _QCA
        out_dir = self._terrain_output_dir()
        src = os.path.join(out_dir, analysis_number)
        base = os.path.join(out_dir, self._ARCHIVE_DIR)
        dst = os.path.join(base, name)
        if not os.path.isdir(src) or os.path.exists(dst):
            return False
        os.makedirs(base, exist_ok=True)
        # この解析番号を見ている以前の書き出し（0.3.0 / 1.3.0 より前の Export to Project）の
        # グループは、レイヤーごとプロジェクトから外してからコピーする（名前を付けたグループに
        # 置き換わる）。コピーできなかったときは元に戻す。
        removed = self._remove_legacy_export_groups(analysis_number)
        if removed is None:
            QtWidgets.QMessageBox.warning(
                self, "Export to Project",
                f"Could not create the Named Export from {analysis_number} because a layer "
                "of an earlier export of this run is being edited. Stop editing and try again.")
            return False
        # 表示中のプラグインのレイヤーも外す（あとで元のフォルダを消せるように）。
        # 利用者が自分で追加したレイヤーは触らない。
        self._unload_terrain_group(reset_controls=False)
        _gc.collect()
        _QCA.processEvents()
        # 1. レイヤーを外した → 2. ファイルをコピーする（元のフォルダは、書き出しの
        # レイヤーを作ったあとで消す: _remove_moved_run_folder）。コピーは、Windows で
        # ファイルが掴まれていてもできる。
        try:
            shutil.copytree(src, dst)
            err = None
        except (OSError, shutil.Error) as exc:
            err = exc
            shutil.rmtree(dst, ignore_errors=True)
        if err is not None:
            self._restore_legacy_export_groups(removed)
            QtWidgets.QMessageBox.warning(
                self, "Export to Project",
                f"Could not create the Named Export from {analysis_number} because the run "
                f"could not be copied to the archive folder.\n\n{err}")
            self._on_analysis_number_changed(self.cmbAnalysisNumber.currentIndex())
            return False
        try:
            with open(os.path.join(dst, self._ARCHIVE_INFO), "w", encoding="utf-8") as f:
                _json.dump({"name": name, "source_run": analysis_number,
                            "archived_at": _dt.datetime.now().isoformat(timespec="seconds")},
                           f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        self._refresh_analysis_combo(select_data=self._archive_key(name))
        self._copy_archive_dem(dst)
        return True

    def _copy_archive_dem(self, folder):
        """Named Export に解析の DEM（elev_relief.tif）を持たせる（ワーカー）。

        Named Export は解析とは別に管理するので、元の解析や DEM キャッシュを消しても
        書き出しの DEM は残る。3.x は表示に使わない（4.x がヒルシェードと Elevation
        Background に使う）。写し終えるまで元の DEM は掃除しない（_gc_dem_cache）。
        """
        import json as _json
        if os.path.exists(os.path.join(folder, "elev_relief.tif")):
            return
        reference_path = os.path.join(folder, "slope.tif")
        try:
            with open(os.path.join(folder, "params.json"), encoding="utf-8") as f:
                dem_path = str(_json.load(f).get("dem_path") or "")
        except (OSError, ValueError, AttributeError):
            dem_path = ""
        if not os.path.isfile(reference_path) or not os.path.isfile(dem_path):
            return
        from .dem_worker import copy_analysis_dem

        def finish(_result, cancelled, error):
            if error:
                self._set_load_status(error)
            elif not cancelled:
                self._set_load_status("")

        self._start_side_job(
            "archive_dem",
            lambda ctx: copy_analysis_dem(ctx, folder, reference_path, dem_path),
            self._set_load_status,
            finish)

    def _archive_dems_not_yet_copied(self, out_dir):
        """elev_relief.tif がまだ無い Named Export の params.json の DEM（正規化したパス）。"""
        import json as _json
        paths = set()
        base = os.path.join(out_dir, self._ARCHIVE_DIR)
        try:
            names = os.listdir(base)
        except OSError:
            return paths
        for name in names:
            folder = os.path.join(base, name)
            if (not os.path.isdir(folder)
                    or os.path.exists(os.path.join(folder, "elev_relief.tif"))):
                continue
            try:
                with open(os.path.join(folder, "params.json"), encoding="utf-8") as f:
                    dem_path = _json.load(f).get("dem_path", "")
            except (OSError, ValueError, AttributeError):
                continue
            if dem_path:
                paths.add(os.path.normpath(dem_path))
        return paths

    # ── 0.3.0 より前の Export to Project の書き出し（解析番号のフォルダを見ている
    #    「Forestry Operations Lite - 0123」グループ）を Named Export へ移す ──
    # 4.x の Named Exports と同じ処理。自動では移さない。その解析番号を Export to Project で
    # 書き出すときに、以前のグループを外してから移す（_archive_run）。移したあと保存せずに閉じた場合だけ、
    # 開いたときに archive へ付け直す（_reconnect_moved_exports）。

    def _legacy_export_groups(self):
        """以前の書き出しグループを解析番号ごとに返す {解析番号: [グループ]}。"""
        import re as _re
        out_dir = self._terrain_output_dir()
        result = {}
        pattern = _re.compile(r"Forestry Operations Lite - (\d{4}(?:\+\d+)?)$")
        for group in self._all_tree_groups():
            node, managed = group, False
            while node is not None:
                if hasattr(node, "customProperty") and node.customProperty(self._GROUP_PROP) == "1":
                    managed = True
                    break
                node = node.parent()
            if managed or group.customProperty(self._ARCHIVE_GROUP_PROP, ""):
                continue
            match = pattern.fullmatch(group.name())
            if match is None:
                continue
            run = match.group(1)
            run_dir = os.path.normcase(os.path.abspath(os.path.join(out_dir, run)))
            if not os.path.isdir(run_dir):
                continue
            for lnode in group.findLayers():
                lyr = lnode.layer()
                if lyr is None:
                    continue
                path = os.path.normcase(os.path.abspath(str(lyr.source() or "").split("|", 1)[0]))
                try:
                    inside = os.path.commonpath((path, run_dir)) == run_dir
                except ValueError:
                    inside = False
                if inside:
                    result.setdefault(run, []).append(group)
                    break
        return result

    def _notify_legacy_export_migration(self, moved, errors):
        """以前の書き出しを Named Export へ移したことを知らせる（保存を促す）。"""
        if not moved and not errors:
            return
        lines = []
        if moved:
            lines.append("Earlier exports in this project were reconnected to Named Exports:\n\n"
                         + "\n".join(f"• {name}" for name in moved)
                         + "\n\nTheir run folders had already been moved to the archive "
                           "folder, so the layers now point there. Save the project to keep "
                           "the new locations.")
        if errors:
            lines.append("These earlier exports could not be reconnected:\n\n"
                         + "\n".join(f"• {e}" for e in errors))
        QtWidgets.QMessageBox.information(self, "Named Exports", "\n\n".join(lines))

    def _broken_legacy_export_groups(self):
        """移したあと保存されずに閉じたため、無くなった解析番号のフォルダを見ている
        以前の書き出しのグループ {解析番号: ([グループ], archive のキー)}。

        元の解析番号が fol_archive.json の source_run に記録された Named Export が
        あるときだけ対象にする（同じ名前のものを優先、無ければ1件だけのとき）。
        """
        import json as _json
        import re as _re
        out_dir = self._terrain_output_dir()
        by_run = {}
        for archive_name in self._scan_archives():
            try:
                with open(os.path.join(out_dir, self._ARCHIVE_DIR, archive_name, self._ARCHIVE_INFO),
                          "r", encoding="utf-8") as f:
                    run = str(_json.load(f).get("source_run", "") or "")
            except (OSError, ValueError, AttributeError):
                continue
            if run:
                by_run.setdefault(run, []).append(archive_name)
        pattern = _re.compile(r"Forestry Operations Lite - (\d{4}(?:\+\d+)?)$")
        result = {}
        for group in self._all_tree_groups():
            node, managed = group, False
            while node is not None:
                if hasattr(node, "customProperty") and node.customProperty(self._GROUP_PROP) == "1":
                    managed = True
                    break
                node = node.parent()
            if managed:
                continue
            if group.customProperty(self._ARCHIVE_GROUP_PROP, ""):
                continue
            match = pattern.fullmatch(group.name())
            if match is None:
                continue
            run = match.group(1)
            run_dir = os.path.normcase(os.path.abspath(os.path.join(out_dir, run)))
            if os.path.isdir(run_dir) and not os.path.isfile(os.path.join(run_dir, self._MOVED_MARK)):
                continue  # まだ書き出していない以前の書き出し（そのまま使える）
            names = by_run.get(run, [])
            chosen = group.name() if group.name() in names else (names[0] if len(names) == 1 else None)
            if chosen is None:
                continue
            pointing = False
            for lnode in group.findLayers():
                lyr = lnode.layer()
                if lyr is None:
                    continue
                path = os.path.normcase(os.path.abspath(str(lyr.source() or "").split("|", 1)[0]))
                try:
                    pointing = os.path.commonpath((path, run_dir)) == run_dir
                except ValueError:
                    pointing = False
                if pointing:
                    break
            if pointing:
                entry = result.setdefault(run, ([], self._archive_key(chosen)))
                entry[0].append(group)
        return result

    def _repair_legacy_export_run(self, analysis_number, groups, key):
        """以前の書き出しのレイヤーを、移し済みの Named Export（archive）へ付け直す。

        フォルダは移さない（もう archive にある）。見つからないレイヤーもスタイルを
        保っているので、参照先だけ切り替える。
        """
        from qgis.core import QgsDataProvider
        _options = QgsDataProvider.ProviderOptions()
        out_dir = self._terrain_output_dir()
        src_abs = os.path.abspath(os.path.join(out_dir, analysis_number))
        src_norm = os.path.normcase(src_abs)
        dst = os.path.join(out_dir, key)
        invalid = []
        changed = 0
        for group in groups:
            for lnode in group.findLayers():
                lyr = lnode.layer()
                if lyr is None:
                    continue
                path, sep, suffix = str(lyr.source() or "").partition("|")
                norm = os.path.normcase(os.path.abspath(path))
                try:
                    if os.path.commonpath((norm, src_norm)) != src_norm:
                        continue
                except ValueError:
                    continue
                new_path = os.path.join(dst, os.path.relpath(os.path.abspath(path), src_abs))
                if not os.path.exists(new_path):
                    invalid.append(lyr.name())
                    continue
                name = lyr.name()
                lyr.setDataSource(new_path + (sep + suffix if sep else ""), name,
                                  lyr.providerType(), _options, False)
                lyr.setName(name)
                changed += 1
                if not lyr.isValid():
                    invalid.append(name)
        if invalid:
            return False, "could not reconnect: " + ", ".join(invalid)
        if not changed:
            return False, "no matching exported layers were found"
        for group in groups:
            group.setCustomProperty(self._ARCHIVE_GROUP_PROP, key)
        QgsProject.instance().setDirty(True)
        return True, ""

    def _reconnect_moved_exports(self):
        """移したあと保存せずに閉じたプロジェクトで、以前の書き出しのグループを
        archive へ付け直す（フォルダは動かさない）。(付け直した名前の一覧, エラーの一覧)"""
        fixed, errors = [], []
        for run, (groups, key) in self._broken_legacy_export_groups().items():
            name = groups[0].name()
            try:
                ok, detail = self._repair_legacy_export_run(run, groups, key)
            except Exception as exc:  # 思わぬエラーでもパネルを止めず、知らせる
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            if ok:
                fixed.append(name)
            else:
                errors.append(f"{name}: {detail}")
        return fixed, errors

    def _remove_legacy_export_groups(self, analysis_number):
        """解析番号を見ている以前の書き出しのグループを、レイヤーごと外す。

        戻り値: 戻すための記録 [(記録, 親, 位置)]。編集中のレイヤーがあれば None（外さない）。
        """
        groups = self._legacy_export_groups().get(analysis_number, [])
        for group in groups:
            for lnode in group.findLayers():
                lyr = lnode.layer()
                if isinstance(lyr, QgsVectorLayer) and lyr.isEditable():
                    return None
        removed = []
        for group in groups:
            parent = group.parent()
            removed.append((self._snapshot_tree_node(group), parent, parent.children().index(group)))
        project = QgsProject.instance()
        for group in groups:
            ids = [n.layerId() for n in group.findLayers()]
            group.parent().removeChildNode(group)
            project.removeMapLayers(ids)
        return removed

    def _restore_legacy_export_groups(self, removed):
        """_remove_legacy_export_groups で外したグループを、元の場所・並び・スタイルで戻す。"""
        invalid = []
        for snap, parent, index in sorted(removed or [], key=lambda r: r[2]):
            self._rebuild_tree_node(snap, parent, index, None, invalid)

    def _snapshot_tree_node(self, node):
        """レイヤーツリーのグループ（中のサブグループ・レイヤーごと）を記録する。"""
        from qgis.core import QgsLayerTreeGroup, QgsMapLayerStyle
        if isinstance(node, QgsLayerTreeGroup):
            return {
                "kind": "group", "name": node.name(),
                "visible": node.itemVisibilityChecked(), "expanded": node.isExpanded(),
                "props": {k: node.customProperty(k) for k in node.customProperties()},
                "children": [c for c in (self._snapshot_tree_node(ch) for ch in node.children())
                             if c is not None],
            }
        lyr = node.layer() if hasattr(node, "layer") else None
        if lyr is None:
            return None
        style = QgsMapLayerStyle()
        style.readFromLayer(lyr)
        return {
            "kind": "raster" if isinstance(lyr, QgsRasterLayer) else "vector",
            "name": lyr.name(), "provider": lyr.providerType(), "source": lyr.source(),
            "style": style, "visible": node.itemVisibilityChecked(), "expanded": node.isExpanded(),
        }

    def _rebuild_tree_node(self, snap, parent, index, map_source, invalid):
        """_snapshot_tree_node の記録から開き直す。map_source(旧ソース) が新ソース（None なら旧のまま）。"""
        project = QgsProject.instance()
        if snap["kind"] == "group":
            group = parent.insertGroup(index, snap["name"])
            for key, value in snap["props"].items():
                group.setCustomProperty(key, value)
            for child in snap["children"]:
                self._rebuild_tree_node(child, group, len(group.children()), map_source, invalid)
            group.setItemVisibilityChecked(snap["visible"])
            group.setExpanded(snap["expanded"])
            return group
        source = snap["source"]
        if map_source is not None:
            source = map_source(source) or source
        cls = QgsRasterLayer if snap["kind"] == "raster" else QgsVectorLayer
        lyr = cls(source, snap["name"], snap["provider"])
        snap["style"].writeToLayer(lyr)
        lyr.setName(snap["name"])
        if not lyr.isValid():
            invalid.append(snap["name"])
        project.addMapLayer(lyr, False)
        node = parent.insertLayer(index, lyr)
        if node is not None:
            node.setItemVisibilityChecked(snap["visible"])
            node.setExpanded(snap["expanded"])
        return node

    def _ensure_terrain_group(self, analysis_number: str):
        """グループが None またはC++オブジェクト削除済みであれば再作成する。"""
        try:
            if self._terrain_layer_group is not None:
                self._terrain_layer_group.name()  # 削除済みなら RuntimeError
                return  # 有効
        except RuntimeError:
            self._terrain_layer_group = None
        self._create_terrain_group(analysis_number)

    def _create_terrain_group(self, analysis_number: str):
        """QGIS レイヤパネルに解析グループを作成する（折り畳み状態）"""
        root = QgsProject.instance().layerTreeRoot()
        group = root.insertGroup(0, f"Run {self._run_label(analysis_number)}")
        group.setExpanded(False)
        group.setCustomProperty(self._GROUP_PROP, "1")
        self._terrain_layer_group = group
        return group

    def _capture_terrain_display_state(self):
        """現在表示中の地形レイヤ状態を、解析切替後に復元できる形で保持する。"""
        return {
            key: self._terrain_cycle_state.get(key, -1)
            for key in self._BTN_LABELS
            if self._terrain_cycle_state.get(key, -1) >= 0
        }

    def _restore_terrain_display_state(self, state, analysis_number=None):
        """保存済みの地形レイヤ表示状態を再適用する。"""
        if not (analysis_number or self.cmbAnalysisNumber.currentData()):
            return
        for key in self._BTN_LABELS:
            desired_state = state.get(key, -1)
            if desired_state < 0:
                continue
            # _cycle_terrain_layer() は現在 state から次状態へ進めるため、
            # 目標の1つ手前を仕込んでから1回進めて復元する。
            self._terrain_cycle_state[key] = desired_state - 1
            self._cycle_terrain_layer(key)
        self._refresh_render_canvases()

    def _restore_pre_run_display_state(self, analysis_number=None):
        """解析実行前に退避した表示状態を再適用する（使い切りで空にする）。"""
        state = getattr(self, "_pre_run_display_state", {})
        if state:
            self._restore_terrain_display_state(state, analysis_number)
        self._pre_run_display_state = {}

    def _unload_terrain_group(self, reset_controls=True):
        """プラグイン管理グループをカスタムプロパティで検索して削除する。
        Python参照（_terrain_layer_group）が stale でも確実に除去できる。"""
        import sip
        layer_ids = set()
        for ids in getattr(self, "_loaded_terrain_layers", {}).values():
            layer_ids.update(ids)
        layer_ids.update(getattr(self, "_flow_buffer_layer_ids", []))

        try:
            root = QgsProject.instance().layerTreeRoot()
            if not sip.isdeleted(root):
                for child in list(root.children()):
                    if sip.isdeleted(child):
                        continue
                    if (hasattr(child, "customProperty") and
                            child.customProperty(self._GROUP_PROP) == "1"):
                        try:
                            layer_ids.update(
                                n.layerId() for n in child.findLayers()
                            )
                            root.removeChildNode(child)
                        except Exception:  # nosec B110
                            pass
        except Exception:  # nosec B110
            pass
        self._terrain_layer_group = None
        proj = QgsProject.instance()
        if getattr(self, "preview_canvas", None) is not None:
            try:
                self.preview_canvas.setLayers([
                    lyr for lyr in self.preview_canvas.layers()
                    if lyr.id() not in layer_ids
                ])
                if hasattr(self.preview_canvas, "clearCache"):
                    self.preview_canvas.clearCache()
                self.preview_canvas.refresh()
            except Exception:  # nosec B110
                pass
        for lid in layer_ids:
            try:
                if proj.mapLayer(lid):
                    proj.removeMapLayer(lid)
            except Exception:  # nosec B110
                pass
        self._loaded_terrain_layers = {}
        self._flow_buffer_layer_ids = []
        self._loaded_terrain_basenames = {}
        if reset_controls:
            self._reset_load_buttons()
        try:
            self._refresh_preview_canvas()
        except Exception:  # nosec B110
            pass
        if self.iface is not None:
            try:
                if hasattr(self.iface.mapCanvas(), "clearCache"):
                    self.iface.mapCanvas().clearCache()
                self.iface.mapCanvas().refresh()
            except Exception:  # nosec B110
                pass

    def _on_project_cleared(self):
        """プロジェクトクリア時: QGISが既にレイヤを削除済みなので参照だけリセット"""
        # 解析中にプロジェクトが切り替わった（閉じた・新規・開いた）ときは、
        # ユーザーが中止を望んだものとして扱う。計算を止め、途中のデータは
        # 残さない（片付けは _on_analysis_done）。
        worker = self._analysis_worker
        if worker is not None and worker.isRunning() and self._analysis_ctx is not None:
            self._analysis_ctx["project_changed"] = True
            worker.cancel()
            self.btnStopAnalysis.setEnabled(False)
            self.lblAnalysisStatus.setText("Cancelling (the project was changed)…")
        # DEM の取得中も同じく中止として扱い、取得したファイルは残さない
        # （保存先が前のプロジェクトのフォルダのため）。
        if self._dem_worker is not None and self._dem_job_ctx is not None:
            self._dem_job_ctx["project_changed"] = True
            self._cancel_dem_job()
        # 書き出し（VS）の途中も同じく中止として扱う
        for _ctx in self._side_jobs.values():
            _ctx["project_changed"] = True
            _ctx["worker"].cancel()
        self._terrain_layer_group = None
        self._loaded_terrain_layers = {}
        self._orig_base_opacity.clear()
        self._reset_load_buttons()
        try:
            self._refresh_preview_canvas()
        except Exception:  # nosec B110
            pass
        # 出力先はプロジェクトのフォルダで決まるので、出力先の表示と解析番号の
        # 一覧を今の保存先に合わせる（新規プロジェクトで前のフォルダが表示された
        # ままにならないように）。前のプロジェクトで選んでいた番号は持ち越さない。
        # プロジェクトを開く場合はこのあと _on_project_read で合わせ直すので、
        # 途中の仮の保存先で DEM キャッシュを消さないよう掃除はしない。
        self._update_out_dir_label()
        with QSignalBlocker(self.cmbAnalysisNumber):
            self.cmbAnalysisNumber.setCurrentIndex(0)
        self._refresh_analysis_combo(select_latest=False, gc_cache=False)

    def _run_display_snapshot_data(self):
        """解析番号を見ているときの表示の設定（Named Export へ切り替える前に控える）。"""
        return {
            "filter_wetland": self._filter_state.get("wetland", "off"),
            "filter_flow": self._filter_state.get("flow", "off"),
            "filter_tc": getattr(self, "_filter_state_tc", "off"),
            "buffer_flow": self._flow_buffer_state,
            "buffer_tc": self._flow_buffer_state_tc,
            "cycle_state": self._capture_terrain_display_state(),
            "opacity": {key: self._opacity_spinbox(key).value() for key in self._BTN_LABELS},
        }

    def _apply_display_controls(self, data):
        """表示の設定（Filter・Buffer・不透明度）をボタンに戻し、表示するファイルの状態を返す。"""
        data = data or {}
        for key in ("wetland", "flow"):
            value = data.get(f"filter_{key}")
            if value in ("off", "low", "mid"):
                self._filter_state[key] = value
        if data.get("filter_tc") in ("off", "low", "mid"):
            self._filter_state_tc = data["filter_tc"]
        if data.get("buffer_flow") in ("off", "weak", "strong"):
            self._flow_buffer_state = data["buffer_flow"]
        if data.get("buffer_tc") in ("off", "weak", "strong"):
            self._flow_buffer_state_tc = data["buffer_tc"]
        for key, value in (data.get("opacity") or {}).items():
            if key in self._BTN_LABELS:
                spin = self._opacity_spinbox(key)
                with QSignalBlocker(spin):
                    spin.setValue(int(value))
        self.btnFilterWetland.setText(self._filter_state.get("wetland", "off"))
        self._sync_flow_aux_controls()
        state = {}
        for key, value in (data.get("cycle_state") or {}).items():
            try:
                if key in self._BTN_LABELS and int(value) >= 0:
                    state[key] = int(value)
            except (TypeError, ValueError):
                continue
        return state

    def _archive_display(self, key):
        """Named Export に書き出したときの表示の設定（fol_archive.json の display）。"""
        import json as _json
        try:
            with open(os.path.join(self._terrain_output_dir(), key, self._ARCHIVE_INFO),
                      "r", encoding="utf-8") as f:
                display = _json.load(f).get("display")
        except (OSError, ValueError, AttributeError):
            return None
        return display if isinstance(display, dict) else None

    def _on_analysis_number_changed(self, _index):
        """解析番号コンボ変更時: 対象の解析へ切り替える。

        解析番号どうしは表示の設定を引き継ぐ。Named Export は自分の設定（書き出したときの
        表示）で見せ、解析番号の設定を引き継がない・引き継がせない（Named Export から
        解析番号へ戻ったら、Named Export を選ぶ前の解析番号の設定に戻す）。
        """
        analysis_number = self.cmbAnalysisNumber.currentData()
        previous = getattr(self, "_active_analysis_key", None)
        previous_named = self._archive_name(previous) is not None
        target_named = self._archive_name(analysis_number) is not None
        display_state = self._capture_terrain_display_state()
        if previous and not previous_named and target_named:
            self._run_display_snapshot = self._run_display_snapshot_data()
        self._unload_terrain_group(reset_controls=False)
        if analysis_number:
            if target_named:
                own = self._archive_display(analysis_number)
                if own:
                    # 前の表示のボタンのオン・オフを残さないよう、いったん全部切る
                    self._reset_load_buttons()
                    display_state = self._apply_display_controls(own)
            elif previous_named:
                self._reset_load_buttons()
                display_state = self._apply_display_controls(
                    getattr(self, "_run_display_snapshot", None))
            self._create_terrain_group(analysis_number)
            self._restore_terrain_display_state(display_state)
        else:
            self._reset_load_buttons()
        self._active_analysis_key = analysis_number
        self._update_analysis_condition_label(analysis_number)
        self._update_export_button()
        # ロック中は新しい解析範囲へ更新し、現在の表示が範囲外なら移動
        if self._map_locked:
            new_ext = self._get_analysis_extent()
            self._lock_analysis_extent = new_ext
            if new_ext is not None and self.preview_canvas is not None:
                if not self.preview_canvas.extent().intersects(new_ext):
                    self.preview_canvas.setExtent(new_ext)
                    self.preview_canvas.refresh()

    def _update_analysis_condition_label(self, analysis_number):
        lbl = getattr(self, "lblAnalysisCondition", None)
        if lbl is None:
            return
        lbl.setToolTip("")
        if not analysis_number:
            lbl.setText("Select a run to view conditions")
            return
        out_dir = self._terrain_output_dir()
        params_path = os.path.join(out_dir, analysis_number, "params.json")
        if not os.path.exists(params_path):
            lbl.setText("No condition data (legacy run)")
            return
        try:
            import json as _json
            with open(params_path, "r", encoding="utf-8") as f:
                p = _json.load(f)
        except Exception:
            lbl.setText("Error reading condition file")
            return
        parts = []
        analyses = p.get("analyses", [])
        if "stability" in analyses:
            parts.append(
                f"Stability: φ{p.get('phi_deg','-')}° · "
                f"C={p.get('c_kpa','-')}kPa · "
                f"z={p.get('z_m','-')}m · "
                f"m={p.get('m_sat','-')} · "
                f"FS≤{p.get('fs_thresh','-')}"
            )
        if "valley" in analyses:
            _flow_method_label = {
                "mfd": "MFD",
                "dinf": "D∞",
                "d8": "D8",
            }.get(str(p.get("flow_method", "d8")).lower(), "D8")
            parts.append(
                f"Valley: {_flow_method_label} · "
                f"TWI≥{p.get('twi_thresh','-')} · "
                f"min area {p.get('min_area','-')} m²"
            )
        if "flow" in analyses:
            parts.append(
                f"Flow: {p.get('duration_h','-')}h · "
                f"{p.get('rainfall_mmh','-')}mm/h · "
                f"total {p.get('total_mm','-')}mm"
            )
            applied = p.get("flow_coefficients")
            if not isinstance(applied, dict) or applied.get("mode") not in ("manual", "dsm", "mixed"):
                parts.append("DSM usage: not recorded (legacy run)")
            else:
                dsm_info = applied.get("dsm", {})
                if applied["mode"] == "manual":
                    reason = {
                        "not_selected": "no DSM",
                        "no_valid_cells": "DSM has no valid coverage",
                        "unavailable": "DSM unavailable",
                    }.get(dsm_info.get("status"), "no valid DSM")
                    parts.append(f"Manual coefficients ({reason})")
                else:
                    coverage = applied["dsm_coverage"] * 100
                    parts.append(f"DSM auto: {coverage:.1f}% / Manual fallback: {100 - coverage:.1f}%")
                if applied["fallback_cells"]:
                    fallback = applied["fallback"]
                    prefix = "Manual" if applied["mode"] == "manual" else "Fallback"
                    parts.append(f"{prefix}: C={fallback['runoff_coef']:g} · "
                                 f"velocity coef.={fallback['velocity_coef']:g}")
                lbl.setToolTip(
                    f"DSM: {dsm_info.get('path') or 'Not used'}\n"
                    f"Model: {applied.get('model_version', '-')}\n"
                    f"DSM cells: {applied['dsm_valid_cells']} / {applied['dem_valid_cells']}")
        lbl.setText("\n".join(parts) if parts else "No conditions")

    def _load_dem_info(self):
        path = self._dem_path
        if not path:
            self.lblDemInfo.setText("Not set")
            self.btnBrowseDem.setText("Browse")
            return
        try:
            from .terrain.dem_loader import DEMLoader
            loader = DEMLoader()
            loader.open_metadata(path)
            self.lblDemInfo.setText(loader.info_text())
            self._terrain_loader = loader
        except Exception as e:
            self.lblDemInfo.setText(f"Error: {e}")
            self._terrain_loader = None
        self._partial_outside_warned = False
        self.btnBrowseDem.setText("Clear")
        self._update_resample_indicator()

    def _on_browse_dsm(self):
        if self._dsm_loading:
            # 取得中は Cancel ボタン: ワーカーを止める
            self._cancel_dem_job()
            return
        if self._dsm_path:
            # クリア
            self._dsm_path = ""
            self._dsm_loader = None
            self._vs_dsm_codes = []
            self.txtDsmPath.clear()
            self.txtDsmPath.setToolTip("")
            self.lblDsmInfo.setText("Not set")
            self.btnBrowseDsm.setText("Browse")
            self._update_flow_coef_state()
            self._reset_vs_export_state()
            self._update_vs_export_buttons()
            return
        dlg = DemBrowserDialog(self.preview_canvas, "", self, mode="dsm")
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            path = dlg.selected_path()
            self._reset_vs_export_state()
            _orig_labels = {
                DemBrowserDialog.VS_LP_GROUND_SENTINEL: ("VS LP → DSM (0.5m)", "LP/Original (2021中西部・2025) or LP/Ground all-return (others)"),
            }
            if path in _orig_labels:
                txt, tip = _orig_labels[path]
                self._dsm_path = path
                self.txtDsmPath.setText(txt)
                self.txtDsmPath.setToolTip(
                    f"Virtual Shizuoka LP → DSM — {tip}"
                )
                self.btnBrowseDsm.setText("Clear")
                self._load_vs_lp_ground()
            elif path in DemBrowserDialog.REMOTE_DSM_SENTINELS:
                self._dsm_path = path
                self.txtDsmPath.setText(
                    DemBrowserDialog.REMOTE_SHORT_NAMES[path])
                self.txtDsmPath.setToolTip(
                    "Auto-fetch within the DEM extent")
                self.btnBrowseDsm.setText("Clear")
                self._load_remote_raster(path, target="dsm")
            elif path:
                self._dsm_path = path
                self.txtDsmPath.setText(os.path.basename(path))
                self.txtDsmPath.setToolTip(path)
                self._load_dsm_info()

    def _load_dsm_info(self):
        path = self._dsm_path
        if not path:
            self.lblDsmInfo.setText("Not set")
            self._dsm_loader = None
            self.btnBrowseDsm.setText("Browse")
            self._update_flow_coef_state()
            return
        try:
            from .terrain.dem_loader import DEMLoader
            loader = DEMLoader()
            loader.open_metadata(path)
            self.lblDsmInfo.setText(loader.info_text())
            self._dsm_loader = loader
        except Exception as e:
            self.lblDsmInfo.setText(f"Error: {e}")
            self._dsm_loader = None
        self._vs_dsm_codes = []  # 非 VS ソース: コードなし
        self.btnBrowseDsm.setText("Clear")
        self._update_flow_coef_state()
        self._update_vs_export_buttons()

    def _update_flow_coef_state(self):
        """DSM設定時も欠損セル用の補完係数を編集可能にする。"""
        has_dsm = bool(self._dsm_path)
        self._lbl_runoff_coef.setText("Fallback runoff coef. C" if has_dsm else "Runoff coef. C")
        self._lbl_vel_coef.setText("Fallback flow velocity coef." if has_dsm else "Flow velocity coef.")
        for w, tooltip in (
            (self.spinRunoff, "Runoff coefficient C"),
            (self.spinVelocityCoef,
             "Flow velocity coef. [m/s]: velocity = coef. x tan(slope)^0.5"),
        ):
            w.setEnabled(True)
            w.setToolTip("Used for missing DSM cells; valid DSM cells use canopy height"
                         if has_dsm else tooltip)
            hint_label, manual_hint = self._flow_coef_hints[w]
            hint_label.setText("DSM gaps only. " + manual_hint if has_dsm else manual_hint)

    def _update_resample_indicator(self):
        """terrain_loader の解像度に応じて Resample インジケーターを更新する。"""
        loader = getattr(self, "_terrain_loader", None)
        if loader is not None and getattr(loader, "cell_size", None) is not None \
                and loader.cell_size < _RESAMPLE_THRESHOLD:
            self.lblResample.setText(
                f"Resample → {_RESAMPLE_TARGET}m"
            )
            self.lblResample.setVisible(True)
        else:
            self.lblResample.setVisible(False)

    # ── VS Export / WODMI 連携 ──────────────────────────────────────────────

    def _reset_vs_export_state(self):
        """DEM/DSM 変更時に export 状態をリセットする。"""
        self._vs_export_dir = ""
        self._vs_wodmi_opened = False

    def _canvas_outside_loader(self, loader):
        """現在のキャンバス範囲が loader の地理的範囲外を含む場合 True を返す。"""
        if loader is None or loader.gt is None or loader.crs_wkt is None:
            return False
        gt = loader.gt
        if loader.data is not None:
            rows, cols = loader.data.shape
        elif getattr(loader, "_ds", None) is not None:
            cols = loader._ds.RasterXSize
            rows = loader._ds.RasterYSize
        else:
            return False
        # loader の地理的範囲（loader CRS 座標系）
        xmin = gt[0]
        xmax = gt[0] + cols * gt[1]
        ymax = gt[3]
        ymin = gt[3] + rows * gt[5]  # gt[5] は負
        if xmin > xmax:
            xmin, xmax = xmax, xmin
        if ymin > ymax:
            ymin, ymax = ymax, ymin
        if self.preview_canvas is None:
            return False
        canvas_ext = self.preview_canvas.extent()
        canvas_crs = self.preview_canvas.mapSettings().destinationCrs()
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
        loader_crs = QgsCoordinateReferenceSystem()
        loader_crs.createFromWkt(loader.crs_wkt)
        if loader_crs.isValid() and canvas_crs.isValid() and canvas_crs != loader_crs:
            xform = QgsCoordinateTransform(canvas_crs, loader_crs, QgsProject.instance())
            canvas_ext = xform.transformBoundingBox(canvas_ext)
        tol = abs(gt[1]) * 2  # 2ピクセル分の余裕（浮動小数点誤差対策）
        return (canvas_ext.xMinimum() < xmin - tol or
                canvas_ext.xMaximum() > xmax + tol or
                canvas_ext.yMinimum() < ymin - tol or
                canvas_ext.yMaximum() > ymax + tol)

    def _update_vs_export_buttons(self):
        """Export・Open in WODMI ボタンの有効/無効を状態に応じて更新する。

        状態遷移:
          初期/terrain未設定  → 全off
          VS DEM セット       → Export on / Open off / Cancel off（DSM は任意）
          エクスポート処理中  → Export off / Open off / Cancel on
          エクスポート完了    → Export on / Open on  / Cancel off
          Open in WODMI後    → 全off（DEM変更で Export が復活）
        """
        import qgis.utils
        has_wodmi = any("webodm_importer" in k for k in qgis.utils.plugins)

        # WODMIプラグインがない場合はすべて無効
        if not has_wodmi:
            self.btnVsExport.setEnabled(False)
            self.btnVsExport.setToolTip("webodm_importer plugin is not installed")
            self.btnOpenWodmi.setEnabled(False)
            self.btnOpenWodmi.setToolTip("webodm_importer plugin is not installed")
            self.btnVsCancel.setEnabled(False)
            self.lblVsExportStatus.setText("—")
            return

        _dem = getattr(self, "_terrain_loader", None)
        has_dem = bool(_dem and getattr(_dem, "path", None) and os.path.isfile(_dem.path))
        # Export は VS Shizuoka ソース専用（DSM は任意）
        is_vs_source = (self._dem_path == DemBrowserDialog.VS_LP_GRID_SENTINEL)
        can_export = is_vs_source and has_dem
        has_export = bool(self._vs_export_dir and os.path.isfile(self._vs_export_dir))

        if getattr(self, "_dem_worker", None) is not None and not self._vs_exporting:
            # DEM をワーカーで取得中: 終わるまで書き出し・送信は使わない
            # （終わったら _on_dem_job_done から呼び直す）
            self.btnVsExport.setEnabled(False)
            self.btnOpenWodmi.setEnabled(False)
            self.btnVsCancel.setEnabled(False)
            return

        if self._vs_exporting:
            # 処理中: Cancel のみ on
            self.btnVsExport.setEnabled(False)
            self.btnOpenWodmi.setEnabled(False)
            self.btnVsCancel.setEnabled(True)

        elif self._vs_wodmi_opened:
            # WODMI 送信済: DEM/DSM 変更があるまで全 off
            self.btnVsExport.setEnabled(False)
            self.btnOpenWodmi.setEnabled(False)
            self.btnVsCancel.setEnabled(False)
            self.lblVsExportStatus.setText("Sent to WODMI")

        elif has_export:
            # エクスポート完了: Export（再エクスポート）と Open in WODMI が使える
            self.btnVsExport.setEnabled(True)
            self.btnVsExport.setToolTip("Re-export to ZIP")
            self.btnOpenWodmi.setEnabled(True)
            self.btnOpenWodmi.setToolTip("Open webodm_importer with exported ZIP")
            self.btnVsCancel.setEnabled(False)
            self.lblVsExportStatus.setText(os.path.basename(self._vs_export_dir))
            self.lblVsExportStatus.setToolTip(self._vs_export_dir)

        elif can_export:
            # VS DEM セット: Export が使える（DSM は任意）
            self.btnVsExport.setEnabled(True)
            self.btnVsExport.setToolTip(
                "Virtual Shizuoka LP/Grid (DTM) を WODMI ZIP にエクスポートします。\n"
                "DSM (LAS) が読み込まれている場合は合わせてエクスポートします。"
            )
            self.btnOpenWodmi.setEnabled(False)
            self.btnOpenWodmi.setToolTip("Export first to enable")
            self.btnVsCancel.setEnabled(False)
            self.lblVsExportStatus.setText("Ready")

        else:
            # VS ソース未設定 / terrain 未設定: 全 off
            self.btnVsExport.setEnabled(False)
            self.btnVsExport.setToolTip(
                "DEM に VS LP/Grid を選択すると使用できます\n"
                "（Virtual Shizuoka 専用エクスポート）"
            )
            self.btnOpenWodmi.setEnabled(False)
            self.btnOpenWodmi.setToolTip("Export first to enable")
            self.btnVsCancel.setEnabled(False)
            self.lblVsExportStatus.setText("—")

    def _set_vs_export_lock(self, locked):
        """VS の書き出し中は DEM/DSM の選択と Run Analysis を止める（ZIP に入れる
        DTM・DSM のファイルが、取り直しやキャッシュの掃除で途中で消えないように）。"""
        if locked:
            self._vs_lock_dsm_enabled_before = self.btnBrowseDsm.isEnabled()
            self.btnBrowseDem.setEnabled(False)
            self.btnBrowseDsm.setEnabled(False)
            self.btnRunAnalysis.setEnabled(False)
        else:
            self.btnBrowseDem.setEnabled(True)
            self.btnBrowseDsm.setEnabled(getattr(self, "_vs_lock_dsm_enabled_before", True))
            if self._analysis_worker is None and self._dem_worker is None:
                self.btnRunAnalysis.setEnabled(True)

    def _on_vs_export(self):
        """設定済みDTM/DSMを使い、LP/Ortho を S3 から取得して WODMI ZIP に書き出す。

        Ortho の有無の確認と、Ortho の取得・ZIP の作成はワーカーで行う（間で
        確認の窓を出す）。この関数はすぐ戻る。
        """
        from .vs_lp import tiles_for_extent
        from qgis.core import (QgsCoordinateReferenceSystem,
                               QgsCoordinateTransform, QgsProject)

        if "vs_export" in self._side_jobs or self._vs_exporting:
            return

        # ── DTM チェック ─────────────────────────────────────────────
        dtm_loader = getattr(self, "_terrain_loader", None)
        if dtm_loader is None or not dtm_loader.path or not os.path.isfile(dtm_loader.path):
            QtWidgets.QMessageBox.warning(self, "VS Export",
                                          "DEMデータが設定されていません。先にDEMを読み込んでください。")
            return

        dsm_loader = getattr(self, "_dsm_loader", None)
        has_dsm = bool(dsm_loader is not None
                       and dsm_loader.path
                       and os.path.isfile(dsm_loader.path))

        # ── キャンバス範囲 → EPSG:6676 → タイルコード（Ortho用）────
        # ── Ortho タイルコード（DEM/DSM 読込時の範囲に合わせる）────────
        # VS LP ソースで読み込んだ場合は保存済みコードを使う。
        # 非 VS ソースの場合はキャンバス範囲にフォールバック。
        dem_codes = getattr(self, "_vs_dem_codes", [])
        dsm_codes = getattr(self, "_vs_dsm_codes", [])
        if dem_codes or dsm_codes:
            codes = list(set(dem_codes) | set(dsm_codes))
        else:
            if self.preview_canvas is None or self.preview_canvas.extent().isEmpty():
                QtWidgets.QMessageBox.warning(self, "VS Export",
                                              "No canvas extent. Display a map first.")
                return
            canvas_crs = self.preview_canvas.mapSettings().destinationCrs()
            crs_6676 = QgsCoordinateReferenceSystem("EPSG:6676")
            xform = QgsCoordinateTransform(canvas_crs, crs_6676, QgsProject.instance())
            ext6 = xform.transformBoundingBox(self.preview_canvas.extent())
            codes = tiles_for_extent(
                ext6.xMinimum(), ext6.yMinimum(),
                ext6.xMaximum(), ext6.yMaximum(),
            )

        # 書き出しに使う値は、ここで写し取っておく（途中で DEM 等が変わっても使わない）
        ctx_vals = {
            "dtm_path": dtm_loader.path,
            "dsm_path": dsm_loader.path if has_dsm else None,
            "las_paths": [p for p in getattr(self, "_vs_las_paths", []) if os.path.isfile(p)],
            "las_urls": dict(getattr(self, "_vs_las_urls", {})),
        }

        self._vs_exporting = True
        self._update_vs_export_buttons()
        self.btnVsCancel.setEnabled(False)  # Ortho の確認の間は中止しない（すぐ終わる）
        self._set_vs_export_lock(True)
        self.lblVsExportStatus.setText("Checking Ortho…")

        from .dem_worker import vs_resolve_ortho

        def _job(ctx):
            return vs_resolve_ortho(ctx, codes)

        def _finish(resolved_ortho, cancelled, error):
            self._on_vs_ortho_resolved(resolved_ortho, cancelled, error, ctx_vals)

        self._start_side_job("vs_export", _job, self.lblVsExportStatus.setText,
                             _finish, on_abort=self._end_vs_export)

    def _end_vs_export(self, tmp_dir=None):
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        self._vs_exporting = False
        self._set_vs_export_lock(False)
        self._update_vs_export_buttons()

    def _on_vs_ortho_resolved(self, resolved_ortho, cancelled, error, vals):
        """Ortho の有無が分かったとき: 確認の窓を出し、書き出しをワーカーで始める。"""
        if cancelled or error:
            self._end_vs_export()
            self.lblVsExportStatus.setText(error if error else "Cancelled")
            return
        resolved_ortho = resolved_ortho or {}
        has_ortho = bool(resolved_ortho)
        dsm_path = vals["dsm_path"]
        msg = (f"• DTM: {os.path.basename(vals['dtm_path'])}\n"
               f"• DSM: {'設定済み (' + os.path.basename(dsm_path) + ')' if dsm_path else '未設定（スキップ）'}\n"
               f"• Ortho: LP/Ortho  — {'included' if has_ortho else 'not available for this area'}\n\n"
               "Proceed?")
        if QtWidgets.QMessageBox.question(
            self, "VS Export", msg,
            QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel
        ) != QtWidgets.QMessageBox.Ok:
            self._end_vs_export()
            self.lblVsExportStatus.setText("—")
            return

        # ── 作業ディレクトリ（Ortho用）───────────────────────────────
        tmp_dir = os.path.join(self._terrain_output_dir(), ".vs_export_tmp")
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        os.makedirs(tmp_dir)
        zip_dir = os.path.join(self._terrain_output_dir(), "zip")

        self._update_vs_export_buttons()  # 書き出し中: Cancel のみ on

        from .dem_worker import vs_export_zip

        def _job(ctx):
            return vs_export_zip(ctx, resolved_ortho, tmp_dir, zip_dir,
                                 vals["dtm_path"], dsm_path, vals["las_paths"],
                                 vals["las_urls"])

        def _finish(result, cancelled2, error2):
            self._on_vs_export_done(result, cancelled2, error2, tmp_dir)

        self._start_side_job("vs_export", _job, self.lblVsExportStatus.setText, _finish,
                             on_abort=lambda: self._end_vs_export(tmp_dir))

    def _on_vs_export_done(self, result, cancelled, error, tmp_dir):
        """ZIP を書き出し終えたとき（メインスレッド）。"""
        if cancelled or error:
            # 片付け（ボタンの状態の更新）は表示も書き換えるので、先に済ませてから
            # 中止・エラーの表示を出す（でないと前回の ZIP 名などに戻ってしまう）
            self._end_vs_export(tmp_dir)
            if cancelled:
                self.lblVsExportStatus.setText("Cancelled")
            elif error.startswith("ZIP creation failed"):
                QtWidgets.QMessageBox.critical(self, "VS Export", error)
                self.lblVsExportStatus.setText("—")
            else:
                self.lblVsExportStatus.setText(error)
            return
        try:
            self._vs_export_dir = result["zip_path"]
            parts = ["DTM included"]
            parts.append("DSM included" if result["has_dsm"] else "DSM not available for this area")
            parts.append("Ortho included" if result["has_ortho"] else "Ortho not available for this area")
            QtWidgets.QMessageBox.information(
                self, "VS Export",
                f"Export complete.\n{result['zip_name']}\n" + ", ".join(parts)
            )
        finally:
            self._end_vs_export(tmp_dir)

    def _on_vs_cancel(self):
        """書き出しのワーカーを止める。"""
        if self._cancel_side_job("vs_export"):
            self.btnVsCancel.setEnabled(False)
            self.lblVsExportStatus.setText("Cancelling…")

    def _on_open_wodmi(self):
        """エクスポート ZIP を展開して webodm_importer パネルを開く。"""
        import qgis.utils

        zip_path = self._vs_export_dir
        if not zip_path or not os.path.isfile(zip_path):
            return

        _wodmi_key = next((k for k in qgis.utils.plugins if "webodm_importer" in k), None)
        wodmi = qgis.utils.plugins.get(_wodmi_key) if _wodmi_key else None
        if not wodmi:
            return

        if wodmi.panel is None:
            wodmi._toggle_panel()
        else:
            wodmi.panel.setVisible(True)
            wodmi.panel.raise_()

        if hasattr(wodmi.panel, "_src_edit"):
            wodmi.panel._source_path = zip_path
            wodmi.panel._is_zip = True
            wodmi.panel._src_edit.setText(zip_path)
            wodmi.panel._detect_assets()

        # ZIP を WODMI に渡したので送信済み状態へ遷移
        self._vs_export_dir = ""
        self._vs_wodmi_opened = True
        self._update_vs_export_buttons()

    def _on_export_clicked(self):
        """Export to Project。

        解析番号を選んでいるとき: 名前を付けてもらい、解析番号のフォルダを
        archive/<名前>/ へ移してから、その名前のグループとして書き出す。
        解析番号のフォルダを整理・上書きしても、書き出したものが消えないように。
        archive を選んでいるとき: 移さずに、同じグループをもう一度プロジェクトへ
        追加する（.qml は書き直さないので、利用者が保存したスタイルが残る）。
        """
        key = self.cmbAnalysisNumber.currentData()
        if not key:
            self._set_load_status("Select an analysis run to export.")
            return
        name = self._archive_name(key)
        if name is None:
            _legacy_names = [g.name() for g in self._legacy_export_groups().get(key, [])]
            name = self._ask_archive_name(f"Forestry Operations Lite - {key}", _legacy_names)
            if not name:
                return
            if not self._archive_run(key, name):
                return
            self._export_run_group(self._archive_key(name), name, write_styles=True)
            self._remove_moved_run_folder(key, self._archive_key(name))
            return
        if self._archive_in_project(key):
            QtWidgets.QMessageBox.information(
                self, "Export to Project",
                f"'{name}' is already in the project.")
            self._update_export_button()
            return
        self._export_run_group(key, name, write_styles=False)

    def _export_run_group(self, analysis_number, group_name, write_styles=True):
        """analysis_number（解析番号か archive/<名前>）の全レイヤー（現在表示中か
        どうかを問わない）を、プラグイン管理下（_GROUP_PROP付きグループ）から
        独立した永続グループとして書き出す。解析の切替やプラグイン終了で消えない、
        プラグインを開かずに書類作成等で参照できる実レイヤーにするためのもの。
        write_styles=False のときは配色を付け直さず、.qml も書かない。"""
        out_dir = self._terrain_output_dir()
        proj = QgsProject.instance()
        root = proj.layerTreeRoot()
        # キーごとに現在表示中のレイヤー名を集めておき、エクスポート先の
        # 表示ON/OFFへ引き継ぐ（integrated等、キー内に複数ファイルが
        # あるものは「今表示中の1件」だけがONの対象になる）。
        visible_labels = {}
        for key, ids in self._loaded_terrain_layers.items():
            for lid in ids:
                lyr0 = proj.mapLayer(lid)
                if lyr0 is not None:
                    visible_labels.setdefault(key, set()).add(lyr0.name())
        group = root.insertGroup(0, group_name)
        group.setExpanded(False)
        display = self._current_display_state_data()
        if self._archive_name(analysis_number) is not None:
            # 4.x の Named Exports が同じグループ・同じ状態として扱えるように残す
            group.setCustomProperty(self._ARCHIVE_GROUP_PROP, analysis_number)
            for _prop, _key in (("fop_filter_wetland", "filter_wetland"),
                                ("fop_filter_flow", "filter_flow"),
                                ("fop_filter_tc", "filter_tc"),
                                ("fop_buffer_flow", "buffer_flow"),
                                ("fop_buffer_tc", "buffer_tc")):
                group.setCustomProperty(_prop, display[_key])
        added = 0
        style_fail = 0
        # プレビュー/ツリー/メインと同じ _KEY_RANK 降順（flow が最前面）で
        # 追加する。group.addLayer() は末尾（＝下）に足すので、高ランクから
        # 順に追加すると上から flow → … → stability の並びになる。
        for key in sorted(self._TERRAIN_PATTERNS,
                          key=lambda k: self._KEY_RANK.get(k, 0), reverse=True):
            patterns = self._TERRAIN_PATTERNS[key]
            for pat in patterns:
                color_name, label, kind, ext = pat[0], pat[1], pat[2], pat[3]
                file_base = pat[4] if len(pat) > 4 else color_name
                path = os.path.join(out_dir, analysis_number, f"{file_base}{ext}")
                if not os.path.exists(path):
                    continue
                lyr = (QgsRasterLayer(path, label) if kind == "raster"
                       else QgsVectorLayer(path, label, "ogr"))
                if not lyr.isValid():
                    continue
                opacity = self._opacity_spinbox(key).value() / 100.0
                if not write_styles:
                    # archive をもう一度追加: 隣の .qml（QGIS が読み込み時に付ける）をそのまま使う
                    pass
                elif kind == "raster":
                    filter_mode = (
                        self._flow_filter_state(color_name)
                        if key == "flow" else self._filter_state.get(key, "off")
                    )
                    try:
                        self._apply_raster_color(lyr, color_name, filter_mode)
                    except Exception:  # nosec B110
                        pass
                    if lyr.renderer() is not None:
                        lyr.renderer().setOpacity(opacity)
                    if hasattr(lyr, "setOpacity"):  # QGIS 3.16 のラスタにはない（renderer で持つ）
                        lyr.setOpacity(opacity)
                else:
                    try:
                        self._apply_vector_style(lyr, color_name)
                    except Exception:  # nosec B110
                        pass
                    lyr.setOpacity(opacity)
                lyr.triggerRepaint()
                lyr.emitStyleChanged()
                # 適用済みスタイルを .qml サイドカーへ保存。プロジェクトを
                # またいでも（他PC・他ソフト・プラグイン未使用）生ファイルに
                # 配色が付いてくる。低値透過フィルターはレンダラーに含まれる
                # ため再現される。Flow Buffer は別レイヤー（別データ）なので
                # 対象外 — エクスポートにも含めない方針。
                if write_styles:
                    try:
                        _qml = os.path.splitext(path)[0] + ".qml"
                        _msg, _ok = lyr.saveNamedStyle(_qml)
                        if not _ok:
                            style_fail += 1
                    except Exception:  # nosec B110
                        style_fail += 1
                proj.addMapLayer(lyr, False)
                is_visible = label in visible_labels.get(key, set())
                node = group.addLayer(lyr)
                if node:
                    node.setExpanded(False)
                    node.setItemVisibilityChecked(is_visible)
                added += 1
        if added == 0:
            root.removeChildNode(group)
            self._set_load_status(f"No output files found for {analysis_number}.")
        else:
            _msg = f"Exported {added} layer(s) to '{group.name()}'."
            if style_fail:
                _msg += f" ({style_fail} style file(s) not written.)"
            self._set_load_status(_msg)
            if write_styles and self._archive_name(analysis_number) is not None:
                self._update_archive_info(analysis_number, name=group_name, display=display)
        self._update_export_button()

    def _set_load_status(self, text):
        """1行目の状態の欄に出す（長い文は端が切れるので、全文はツールチップに）。"""
        self.lblLoadStatus.setText(text)
        self.lblLoadStatus.setToolTip(text)

    def _run_terrain_analysis(self):
        # 解析中（ワーカー実行中）・取得中は受け付けない。上書きロックの自動再実行
        # （QTimer）などで重ねて呼ばれても二重に起動しないように。
        if self._analysis_worker is not None or self._dem_worker is not None:
            return
        # 解析の前の取り直し（ワーカー）が終わって始め直した回か。
        # "dsm" = DSM を取り直し済み、"dem" = DEM まで取り直し済み。
        # 範囲の警告・DSM の取り直しの確認は答え済みなので聞き直さない。
        _refetch_phase = getattr(self, "_analysis_refetch_phase", None)
        self._analysis_refetch_phase = None
        # ── 解析タイプ選択チェック ──
        if not any([
            self.chkStability.isChecked(),
            self.chkValley.isChecked(),
            self.chkFlow.isChecked(),
        ]):
            self.lblAnalysisStatus.setText("Select at least one analysis type.")
            return

        # 実行中に表示していた地形レイヤの表示状態を、クリーンアップで消える前に
        # 退避しておき、完了後に新成果へ再適用する（上書き実行でも引き継ぐ）。
        # ロック自動再実行の再入時は既に表示が消えているので取り直さない。
        if not getattr(self, "_overwrite_retried", False):
            self._pre_run_display_state = self._capture_terrain_display_state()

        # ── 解析面積チェック（プレビューキャンバス範囲） ──
        _limit_ha = self.cmbAreaLimit.currentData()
        if _limit_ha and _limit_ha > 0 and _refetch_phase is None:
            try:
                import math as _math
                _ext = self.preview_canvas.extent()
                _crs = self.preview_canvas.mapSettings().destinationCrs()
                _w, _h = _ext.width(), _ext.height()
                if _crs.isValid() and _crs.isGeographic():
                    _cy = (_ext.yMinimum() + _ext.yMaximum()) / 2.0
                    _mx = 111320.0 * _math.cos(_math.radians(abs(_cy)))
                    _area_ha = (_w * _mx) * (_h * 111320.0) / 1e4
                else:
                    _area_ha = _w * _h / 1e4
                if _area_ha > _limit_ha:
                    _reply = QtWidgets.QMessageBox.warning(
                        self, "Area Warning",
                        f"Analysis area is {_area_ha:.0f} ha, "
                        f"which exceeds the warning limit of {_limit_ha} ha.\n\nContinue?",
                        QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel,
                    )
                    if _reply != QtWidgets.QMessageBox.Ok:
                        return
            except Exception:  # nosec B110
                pass

        # ── DEM タイルソース / VS LP 範囲外チェック ──
        # タイルソースは、取得済みの DEM が解析範囲を覆っていればそのまま使い、
        # 範囲外の時だけ確認の上で再取得する（以前は解析ごとに必ず再取得していた）。
        _TILE_SENTINELS = (
            DemBrowserDialog.GSI_DEM1A_SENTINEL,
            DemBrowserDialog.GSI_DEM5A_SENTINEL,
            DemBrowserDialog.GSI_DEM10B_SENTINEL,
            DemBrowserDialog.TERRARIUM_FINE_SENTINEL,
            DemBrowserDialog.TERRARIUM_STANDARD_SENTINEL,
            DemBrowserDialog.TERRARIUM_WIDE_SENTINEL,
            DemBrowserDialog.VS_LP_GRID_SENTINEL,
        ) + DemBrowserDialog.REMOTE_DEM_SENTINELS
        # ── 解析範囲外チェック ──
        # VS LP/Ground DSM のみ再取得可能 → 範囲外なら再取得ダイアログを表示。
        # 実データ（WebODM 等の手動ファイル）は範囲外でもダイアログなし:
        #   clip_to_extent がデータ存在領域のみを返すため、そのまま解析を続行する。
        _dem_path_cur = getattr(self, "_dem_path", "")
        _dsm_path_cur = getattr(self, "_dsm_path", "")
        _vs_dsm_outside = (
            _dsm_path_cur == DemBrowserDialog.VS_LP_GROUND_SENTINEL
            and getattr(self, "_dsm_loader", None) is not None
            and self._canvas_outside_loader(self._dsm_loader)
        )
        self._analysis_refetch_stopped = False
        if _vs_dsm_outside and _refetch_phase is None:
            reply = QtWidgets.QMessageBox.question(
                self, "解析範囲の確認",
                "解析範囲にDSM/DTM設定外が含まれます。\n再取得しますか？",
                QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Ok:
                return
            # DSM をワーカーで取り直し、終わったら（成否に関わらず、今までどおり）
            # 解析を始め直す（ここではいったん戻る）。取得中は Stop で中止できる。
            self.btnStopAnalysis.setEnabled(True)
            self._load_vs_lp_ground(
                on_done=lambda _ok: self._on_analysis_refetch_done("dsm", True),
                mirror_label=self.lblAnalysisStatus)
            return

        _dem_outside = (
            _dem_path_cur in _TILE_SENTINELS and _refetch_phase != "dem"
            and (getattr(self, "_terrain_loader", None) is None
                 or self._canvas_outside_loader(self._terrain_loader))
        )
        if _dem_outside:
            reply = QtWidgets.QMessageBox.question(
                self, "解析範囲の確認",
                "解析範囲に現在のDEMの範囲外が含まれます。\n再取得しますか？",
                QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Ok:
                return
            self._terrain_loader = None  # 古いデータをクリアしてから再取得
            self.lblAnalysisStatus.setText("Fetching elevation tiles...")
            # ワーカーで取得し、取れたら解析を始め直す（ここではいったん戻る）。
            # 取得中は Stop で中止できる。
            self.btnStopAnalysis.setEnabled(True)

            def _done(ok):
                self._on_analysis_refetch_done("dem", ok)

            if self._dem_path == DemBrowserDialog.VS_LP_GRID_SENTINEL:
                self._load_vs_lp_grid(auto_dsm=False, on_done=_done,
                                      mirror_label=self.lblAnalysisStatus)
            elif self._dem_path in DemBrowserDialog.REMOTE_DEM_SENTINELS:
                _want_dsm = (
                    _dsm_path_cur in DemBrowserDialog.REMOTE_DSM_SENTINELS)

                def _after_remote_dem(ok):
                    if (ok and _want_dsm
                            and not self._analysis_refetch_stopped):
                        self._load_remote_raster(
                            _dsm_path_cur, target="dsm",
                            on_done=lambda _dsm_ok:
                                self._on_analysis_refetch_done("dem", True),
                            mirror_label=self.lblAnalysisStatus)
                        return
                    _done(ok)

                self._load_remote_raster(
                    self._dem_path, target="dem",
                    on_done=_after_remote_dem,
                    mirror_label=self.lblAnalysisStatus)
            else:
                self._load_gsi_dem(self._dem_path, on_done=_done,
                                   mirror_label=self.lblAnalysisStatus)
            return

        loader = getattr(self, "_terrain_loader", None)
        if loader is None:
            self._load_dem_info()
            loader = getattr(self, "_terrain_loader", None)
        if loader is None:
            self.lblAnalysisStatus.setText("Specify a DEM file.")
            return

        out_dir = self._terrain_output_dir()

        def _release_folder_layers(_folder):
            _proj = QgsProject.instance()
            _folder_norm = os.path.normcase(os.path.abspath(os.path.normpath(_folder)))
            _remove_ids = []
            _source_paths = set()

            def _norm_source_path(_path):
                if not _path:
                    return ""
                _path = str(_path).strip().strip('"').strip("'")
                if _path.lower().startswith("file:"):
                    try:
                        _path = QUrl(_path).toLocalFile() or _path
                    except Exception:  # nosec B110
                        pass
                if _path.lower().startswith("gpkg:"):
                    _path = _path[5:]
                return os.path.normcase(os.path.abspath(os.path.normpath(_path)))

            def _uri_paths(_lyr):
                import re as _re
                _uris = []
                for _getter in (
                    lambda: _lyr.source(),
                    lambda: _lyr.dataProvider().dataSourceUri(),
                ):
                    try:
                        _uri = _getter()
                    except Exception:  # nosec B112
                        continue
                    if _uri:
                        _uris.append(str(_uri))
                try:
                    from qgis.core import QgsProviderRegistry as _QPR
                    _provider_key = _lyr.providerType()
                    _metadata = _QPR.instance().providerMetadata(_provider_key)
                    for _uri in list(_uris):
                        try:
                            _decoded = _metadata.decodeUri(_uri)
                        except Exception:  # nosec B112
                            continue
                        if isinstance(_decoded, dict):
                            for _key in ("path", "filename", "database", "dbname"):
                                _val = _decoded.get(_key)
                                if _val:
                                    _uris.append(str(_val))
                except Exception:  # nosec B110
                    pass
                _paths = set()
                for _uri in _uris:
                    for _m in _re.finditer(
                            r"""(?:dbname|database)\s*=\s*'([^']+)'|(?:dbname|database)\s*=\s*"([^"]+)"|(?:dbname|database)\s*=\s*([^\s|]+)""",
                            _uri):
                        _paths.add(_norm_source_path(_m.group(1) or _m.group(2) or _m.group(3)))
                    _paths.add(_norm_source_path(_uri.split("|", 1)[0]))
                return {p for p in _paths if p}

            def _flush_provider_connections():
                # QgsProviderMetadata.invalidateConnections() は接続「名」を
                # 期待する（ファイルパスではない）。以前はパスを渡していて
                # 常に TypeError → no-op だった。保存済み GeoPackage 接続の
                # うち対象ファイルを指すものを名前で特定して破棄する。解析
                # 出力は通常ブラウザ未登録なので該当なし＝何もしないことが
                # 多いが、登録済みフォルダを上書きするケースで効く。
                # 引数なしの全体 invalidation は QGIS 3.44 + Windows で
                # provider 側 access violation を誘発するため呼ばない。
                if not _source_paths:
                    return
                try:
                    from qgis.core import QgsProviderRegistry as _QPR
                    _registry = _QPR.instance()
                except Exception:  # nosec B110
                    return
                for _provider_key in ("ogr", "gdal"):
                    try:
                        _metadata = _registry.providerMetadata(_provider_key)
                    except Exception:  # nosec B112
                        continue
                    if _metadata is None:
                        continue
                    if not hasattr(_metadata, "connections") or \
                            not hasattr(_metadata, "invalidateConnections"):
                        continue
                    try:
                        _conns = _metadata.connections(False)
                    except Exception:  # nosec B112
                        continue
                    if not _conns:
                        continue
                    for _name, _conn in list(_conns.items()):
                        try:
                            _cn_uri = _norm_source_path(_conn.uri())
                        except Exception:  # nosec B110
                            _cn_uri = ""
                        if not _cn_uri or _cn_uri not in _source_paths:
                            continue
                        try:
                            _metadata.invalidateConnections(_name)
                        except Exception:  # nosec B110
                            pass

            try:
                for _root, _dirs, _files in os.walk(_folder):
                    for _fname in _files:
                        if _fname.lower().endswith((".gpkg", ".tif", ".tiff")):
                            _source_paths.add(
                                _norm_source_path(os.path.join(_root, _fname)))
            except OSError:
                pass

            for _lid, _lyr in list(_proj.mapLayers().items()):
                try:
                    _paths = _uri_paths(_lyr)
                    if any(_src_norm == _folder_norm or
                           _src_norm.startswith(_folder_norm + os.sep)
                           for _src_norm in _paths):
                        _remove_ids.append(_lid)
                        _source_paths.update(_paths)
                except Exception:  # nosec B110
                    pass
            if not _remove_ids:
                _flush_provider_connections()
                return
            _remove_id_set = set(_remove_ids)
            if getattr(self, "preview_canvas", None) is not None:
                try:
                    self.preview_canvas.setLayers([
                        _lyr for _lyr in self.preview_canvas.layers()
                        if _lyr.id() not in _remove_id_set
                    ])
                    self.preview_canvas.refresh()
                except Exception:  # nosec B110
                    pass
            for _key in list(getattr(self, "_loaded_terrain_layers", {}).keys()):
                _kept = [
                    _lid for _lid in self._loaded_terrain_layers.get(_key, [])
                    if _lid not in _remove_id_set
                ]
                if _kept:
                    self._loaded_terrain_layers[_key] = _kept
                else:
                    self._loaded_terrain_layers.pop(_key, None)
                    self._loaded_terrain_basenames.pop(_key, None)
                    self._terrain_cycle_state[_key] = -1
            _proj.removeMapLayers(_remove_ids)
            for _canvas in (
                getattr(self, "preview_canvas", None),
                self.iface.mapCanvas() if self.iface is not None else None,
            ):
                if _canvas is None:
                    continue
                try:
                    if hasattr(_canvas, "clearCache"):
                        _canvas.clearCache()
                    _canvas.refresh()
                except Exception:  # nosec B110
                    pass
            _flush_provider_connections()
            QtWidgets.QApplication.processEvents()

        # プレビュー可視範囲でクリップ（キャンバスCRS → DEM CRS に変換してから渡す）
        try:
            ext = self.preview_canvas.extent()
            canvas_crs = self.preview_canvas.mapSettings().destinationCrs()
            from qgis.core import QgsCoordinateReferenceSystem as _QgsCRS
            dem_crs = _QgsCRS()
            dem_crs.createFromWkt(loader.crs_wkt)
            if dem_crs.isValid() and canvas_crs.isValid() and canvas_crs != dem_crs:
                from qgis.core import QgsCoordinateTransform as _QgsCT
                _xf = _QgsCT(canvas_crs, dem_crs, QgsProject.instance())
                ext = _xf.transformBoundingBox(ext)
            # 部分重なり検出（タイルソースは毎回再取得のためスキップ）
            _partial_outside = (
                _dem_path_cur not in _TILE_SENTINELS
                and self._canvas_outside_loader(loader)
            )
            dem = loader.clip_to_extent(ext.xMinimum(), ext.yMinimum(),
                                        ext.xMaximum(), ext.yMaximum())
        except ValueError:
            # キャンバスがDEMと完全に重ならない
            self.lblAnalysisStatus.setVisible(True)
            self.lblAnalysisStatus.setText(
                "No DEM data in the analysis range. Adjust the view."
            )
            return
        except Exception as e:
            self.lblAnalysisStatus.setText(f"Clip failed: {e}")
            return

        # 部分重なり：初回のみ確認ダイアログ
        if _partial_outside and not self._partial_outside_warned:
            reply = QtWidgets.QMessageBox.question(
                self, "Analysis Range",
                "The analysis range extends beyond the DEM data.\n"
                "Proceed with the available data only?",
                QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel,
            )
            if reply != QtWidgets.QMessageBox.Ok:
                return
            self._partial_outside_warned = True

        # 解像度チェック：閾値より細かければ自動リサンプル（解除不可）
        if dem.cell_size < _RESAMPLE_THRESHOLD:
            self.lblAnalysisStatus.setVisible(True)
            self.lblAnalysisStatus.setText(
                f"Resampling {dem.cell_size:.3f}m → {_RESAMPLE_TARGET}m ..."
            )
            QtWidgets.QApplication.processEvents()
            dem = _resample_dem(dem, _RESAMPLE_TARGET)

        # DSM is prepared against the final DEM grid before overwrite cleanup.
        # A cancelled alignment must not remove the previous analysis results.
        _dsm_data, _dsm_info = None, {}
        if self.chkFlow.isChecked():
            try:
                _dsm_data, _dsm_info = self._clip_dsm_for_analysis(dem)
            except _DsmPreparationCancelled:
                self._overwrite_retried = False
                self.lblAnalysisStatus.setVisible(True)
                self.lblAnalysisStatus.setText("Analysis cancelled.")
                return
            except Exception as e:
                self._overwrite_retried = False
                self.lblAnalysisStatus.setVisible(True)
                self.lblAnalysisStatus.setText(f"DSM preparation error: {e}")
                return

        # 解析シーケンス番号を決定（ファイル保存前に確定）
        seq = self._next_seq(self.chkOverwrite.isChecked())
        # 隠し一時フォルダ（解析完了後に1回だけリネーム → Thunar inotify を最小化）
        import shutil as _shutil
        import time as _time

        # 前回リネーム退避した .trash_* を掃除（GPKG 等のハンドルが解放されて
        # いれば消える。まだロック中なら次回に持ち越す）。
        try:
            for _t in os.listdir(out_dir):
                if _t.startswith(".trash_"):
                    _shutil.rmtree(os.path.join(out_dir, _t), ignore_errors=True)
        except OSError:
            pass

        if self.chkOverwrite.isChecked():
            import re as _re
            import gc as _gc
            from qgis.PyQt.QtCore import QCoreApplication as _QCA
            # Windows では表示中の GeoPackage がロックされるため、上書き
            # 削除に入る前に全表示ボタンを OFF にし、管理レイヤを外す。
            self._unload_terrain_group(reset_controls=True)
            _gc.collect()
            _QCA.processEvents()
            _lock_err = None
            for name in os.listdir(out_dir):
                if not (_re.fullmatch(r'\d{4}(\+\d+)?', name) and name[:3] == seq):
                    continue
                folder = os.path.join(out_dir, name)
                if not os.path.isdir(folder):
                    continue
                _release_folder_layers(folder)
                _gc.collect()
                _QCA.processEvents()
                # まず「リネームで退避」を試す。Windows は開いているファイルを
                # 含むフォルダの削除は失敗するが、フォルダ名のリネームは通ることが
                # 多い（SQLite/OGR は .gpkg ファイルを開くがフォルダは掴まない）。
                _trash = os.path.join(out_dir, f".trash_{name}_{int(_time.time())}")
                _cleared = False
                try:
                    os.rename(folder, _trash)
                    _shutil.rmtree(_trash, ignore_errors=True)  # 消えなければ残す
                    _cleared = True
                except OSError:
                    pass
                # リネームも無理なら従来の rmtree リトライ
                for _attempt in range(30):
                    if _cleared:
                        break
                    try:
                        _shutil.rmtree(folder)
                        _cleared = True
                        break
                    except (PermissionError, OSError) as _e:
                        _gc.collect()
                        _QCA.processEvents()
                        if _attempt < 29:
                            _time.sleep(0.5)
                        else:
                            _lock_err = (name, str(_e))
                if _lock_err or not _cleared:
                    if _lock_err is None:
                        _lock_err = (name, "cleanup failed")
                    break

            if _lock_err is not None:
                _lname, _lmsg = _lock_err
                _base_msg = f"Overwrite failed: {_lname} is still locked. {_lmsg}"
                # 1回目のロック失敗はメッセージを出したうえで解析を丸ごと再実行
                # する。再入すると再実行側が "Resampling..." 等で lblAnalysisStatus
                # を上書きするので、それまでに読めるだけの間（1.5秒）だけ遅らせる。
                if not getattr(self, "_overwrite_retried", False):
                    self._overwrite_retried = True
                    self.lblAnalysisStatus.setVisible(True)
                    self.lblAnalysisStatus.setText(_base_msg + "\n再度実行します…")
                    _gc.collect()
                    _QCA.processEvents()
                    from qgis.PyQt.QtCore import QTimer as _QTimer
                    _QTimer.singleShot(1500, self._run_terrain_analysis)
                    return
                # 2回目も駄目ならエラー表示して諦める
                self._overwrite_retried = False
                self._pre_run_display_state = {}
                self.lblAnalysisStatus.setVisible(True)
                self.lblAnalysisStatus.setText(_base_msg)
                return
        tmp_folder = os.path.join(out_dir, f".tmp_{seq}")
        if os.path.exists(tmp_folder):      # 中断残骸をクリア
            _shutil.rmtree(tmp_folder)
        os.makedirs(tmp_folder)

        import numpy as np

        # ── 解析条件を写し取る ──
        # 計算はワーカースレッドで行い、ワーカーは画面に触らない。ここで
        # 写し取った値で計算し params.json も書くので、実行中に画面の値が
        # 変わっても結果と記録はそろう。
        if self.chkFlowMFD.isChecked():
            _flow_method = "mfd"
        elif self.chkFlowDinf.isChecked():
            _flow_method = "dinf"
        else:
            _flow_method = "d8"
        _p = {
            "stability":     self.chkStability.isChecked(),
            "valley":        self.chkValley.isChecked(),
            "flow":          self.chkFlow.isChecked(),
            "flow_method":   _flow_method,
            "phi_deg":       self.spinPhiDeg.value(),
            "c_kpa":         self.spinCKpa.value(),
            "z_m":           self.spinZm.value(),
            "m_sat":         self.spinMSat.value(),
            "fs_thresh":     self.spinFsThresh.value(),
            "twi_thresh":    self.spinTwiThresh.value(),
            "min_area":      self.spinMinArea.value(),
            "duration_h":    self.spinDuration.value(),
            "rainfall_mmh":  self.spinRainfall.value(),
            "total_mm":      self.spinTotalRainfall.value(),
            "runoff":        self.spinRunoff.value(),
            "velocity_coef": self.spinVelocityCoef.value(),
            "plugin_version": self._plugin_version(),
            "dsm_info":      _dsm_info,
        }
        _dem_path_for_params = (
            getattr(self, "_dem_actual_path", None) or getattr(self, "_dem_path", ""))

        # ── 進捗の段階（チェックされた解析だけ）と、前回実績からの見積もり ──
        _multi_flow = _flow_method in ("mfd", "dinf")
        _W = _ANALYSIS_STAGE_WEIGHTS
        _stages = [
            ("prep", "Slope / hillshade", _W["prep"]),
            ("routing", "Flow direction / accumulation", _W["routing"]),
        ]
        if _p["stability"]:
            _stages.append(("stability", "Slope stability", _W["stability"]))
        if _p["valley"]:
            _stages.append((
                "valley",
                "Valley terrain (MFD)" if _flow_method == "mfd"
                else "Valley terrain (D∞)" if _flow_method == "dinf"
                else "Valley terrain",
                _W["valley_multi"] if _multi_flow else _W["valley"],
            ))
        if _p["flow"]:
            _stages.append(("flow", "Flow estimation", _W["flow"]))
        _stages.append(("integrate", "Overall risk index", _W["integrate"]))
        # 時間はほぼ有効セル数に比例する（NoData のセルはループに入らない）
        _n_cells = int(np.count_nonzero(~np.isnan(dem.data)))
        _rate_key = self._analysis_rate_key()
        _rate = self._load_analysis_rate(_rate_key)
        _estimate_s = _rate * _n_cells / 1e6 if _rate else None

        # ここまで来たら上書き前のロック処理は通過済み。自動リトライ時に
        # "still locked / Retrying..." を残すと、解析中もロック処理中に見える。
        # 解析中は段階名と残り時間の表示に使う。
        self.lblAnalysisStatus.clear()
        self.lblAnalysisStatus.setVisible(True)
        self.lblAnalysisStatus.setText("Starting…")
        # 実行ボタンを同じ場所の進捗バーと入れ替える（終わったら戻す）
        _btn_prog = _ButtonProgress(self.btnRunAnalysis, self.progressAnalysis)
        # ワーカーからの signal で表示するので、処理を回す pump は要らない
        prog = _AnalysisProgress(_btn_prog, self.lblAnalysisStatus,
                                 _stages, lambda: None, estimate_s=_estimate_s)
        self._set_analysis_running(True)

        from .analysis_worker import TerrainAnalysisWorker
        worker = TerrainAnalysisWorker(dem, _dsm_data, _p, tmp_folder, parent=self)
        self._analysis_ctx = {
            "seq": seq,
            "out_dir": out_dir,
            "tmp_folder": tmp_folder,
            "params": _p,
            "dem_path": _dem_path_for_params,
            "n_cells": _n_cells,
            "rate_key": _rate_key,
            "prog": prog,
            "btn_prog": _btn_prog,
            "release_folder_layers": _release_folder_layers,
            "project_changed": False,
        }
        worker.stage.connect(prog.stage)
        worker.progress.connect(prog.update)
        worker.done.connect(self._on_analysis_done)
        self._analysis_worker = worker
        worker.start()

    def _clip_dsm_for_analysis(self, dem):
        """Read the DSM on the final DEM grid and retain its source record."""
        from .terrain.dem_loader import DEMLoader

        dsm_loader = getattr(self, "_dsm_loader", None)
        source = getattr(self, "_dsm_path", "")
        info = {
            "selected_source": source or None,
            "path": getattr(dsm_loader, "path", None),
            "status": "not_selected",
            "resampling": None,
        }
        if not source and dsm_loader is None:
            return None, info
        while True:
            try:
                if dsm_loader is None:
                    path = info["path"] or source
                    dsm_loader = DEMLoader().open_metadata(path)
                info["path"] = dsm_loader.path
                data, resampling = dsm_loader.read_aligned_to(dem)
                info.update({
                    "status": "aligned",
                    "resampling": resampling,
                    "source_grid": {
                        "geotransform": list(dsm_loader.gt),
                        "crs_wkt": dsm_loader.crs_wkt,
                    },
                })
                self._dsm_loader = dsm_loader
                return data, info
            except Exception as exc:
                action = self._ask_dsm_alignment_recovery(str(exc))
                if action == "continue":
                    info.update({"status": "unavailable", "error": str(exc)})
                    return None, info
                if action != "retry":
                    raise _DsmPreparationCancelled() from exc
            finally:
                if dsm_loader is not None:
                    dsm_loader.data = None
            dsm_loader = None

    def _ask_dsm_alignment_recovery(self, details):
        msg = QtWidgets.QMessageBox(self)
        msg.setIcon(QtWidgets.QMessageBox.Warning)
        msg.setWindowTitle("DSM Preparation Problem")
        msg.setText("DSM could not be read or aligned to the analysis DEM.")
        msg.setInformativeText(
            "Continue without DSM uses the specified runoff and velocity coefficients.")
        msg.setDetailedText(details)
        retry = msg.addButton("Retry", QtWidgets.QMessageBox.AcceptRole)
        proceed = msg.addButton(
            "Continue without DSM", QtWidgets.QMessageBox.DestructiveRole)
        cancel = msg.addButton("Cancel", QtWidgets.QMessageBox.RejectRole)
        msg.setDefaultButton(retry)
        msg.setEscapeButton(cancel)
        msg.exec_()
        clicked = msg.clickedButton()
        return "retry" if clicked is retry else "continue" if clicked is proceed else "cancel"

    def _analysis_lock_widgets(self):
        """解析中に操作を止める部品（結果やフォルダの扱いとぶつかるもの）。"""
        return [
            self.tabDataSettings,        # DEM/DSM の選択・CS Map / VS の書き出し
            self._grpAnalysisTypes,      # 解析の種類・流れ方向の方式
            self.tabParams,              # 解析パラメータ
            self._areaLimitControls,     # 面積の警告
            self.lblOutDir,              # 出力先
            self.chkOverwrite,
            self._grpDisplayMgmt,        # 解析番号の切り替え・地形レイヤーの表示・Export
        ]

    def _set_analysis_running(self, running):
        for w in self._analysis_lock_widgets():
            w.setEnabled(not running)
        self.btnRunAnalysis.setEnabled(not running)
        self.btnStopAnalysis.setEnabled(running)

    def _on_analysis_done(self, saved, cancelled, error, warning):
        """ワーカー終了時（メインスレッド）: 後片付けと、成功時の結果の確定・表示。"""
        ctx = self._analysis_ctx
        worker = self._analysis_worker
        self._analysis_ctx = None
        self._analysis_worker = None
        if worker is not None:
            if ctx is not None:
                ctx["flow_conditions"] = worker.flow_conditions
            worker.deleteLater()
        if ctx is None:
            return
        ctx["btn_prog"].finish()
        self._set_analysis_running(False)
        # 上書きロックの自動リトライ枠をリセット（この実行が cleanup を
        # 通過したので、次の実行はまた1回だけ黙って再試行できる）。
        self._overwrite_retried = False
        tmp_folder = ctx["tmp_folder"]

        if cancelled or error:
            # 中止（プロジェクトの切り替えを含む）・エラーでは途中のデータを残さない
            shutil.rmtree(tmp_folder, ignore_errors=True)
            self.lblAnalysisStatus.setVisible(True)
            if ctx["project_changed"]:
                self.lblAnalysisStatus.setText(
                    "Analysis cancelled because the project was changed.")
            elif cancelled:
                self.lblAnalysisStatus.setText("Analysis cancelled.")
            else:
                self.lblAnalysisStatus.setText(f"Analysis error: {error}")
            return

        try:
            self._finish_analysis(ctx, saved, warning)
        except Exception as e:
            shutil.rmtree(tmp_folder, ignore_errors=True)
            self.lblAnalysisStatus.setVisible(True)
            self.lblAnalysisStatus.setText(f"Analysis error: {e}")

    def _finish_analysis(self, ctx, saved, warning):
        """計算が終わった一時フォルダを解析番号のフォルダに確定し、表示する。"""
        import json as _json
        import time as _time
        out_dir = ctx["out_dir"]
        tmp_folder = ctx["tmp_folder"]
        _p = ctx["params"]

        # 計算にかかった時間を次回の見積もり用に残す（同じ解析の組み合わせごと）
        _elapsed_s = ctx["prog"].elapsed()
        if ctx["n_cells"] > 0:
            self._save_analysis_rate(ctx["rate_key"], _elapsed_s * 1e6 / ctx["n_cells"])

        # ── ファイル数が確定したので解析番号を決定し tmp_folder を1回だけリネーム ──
        N = len(saved)
        analysis_number = self._format_analysis_number(ctx["seq"], N)
        folder = os.path.join(out_dir, analysis_number)

        # 解析条件を params.json に保存（リネーム前に tmp_folder へ書き込む）
        _params = {
            "dem_path": ctx["dem_path"],
            "analyses": [k for k in ("stability", "valley", "flow") if _p[k]],
        }
        if _p["flow"]:
            _params.update({k: _p[k] for k in (
                "duration_h", "rainfall_mmh", "total_mm", "runoff", "velocity_coef")})
            _params["flow_coefficients"] = ctx["flow_conditions"]
        if _p["stability"]:
            _params.update({k: _p[k] for k in (
                "phi_deg", "c_kpa", "z_m", "m_sat", "fs_thresh")})
        if _p["valley"]:
            _params["flow_method"] = _p["flow_method"]
            _params.update({k: _p[k] for k in ("twi_thresh", "min_area")})
        with open(os.path.join(tmp_folder, "params.json"), "w", encoding="utf-8") as _f:
            _json.dump(_params, _f, ensure_ascii=False, indent=2, allow_nan=False)

        # tmp_folder を最終フォルダ名に一括リネーム（inotify イベントを1回に集約）
        # 通常は上の事前クリーンアップで folder は存在しないが、番号衝突等の
        # 保険としてここでも「リネーム退避 → 無理なら rmtree」で退ける。
        if os.path.exists(folder):
            ctx["release_folder_layers"](folder)
            import gc as _gc
            from qgis.PyQt.QtCore import QCoreApplication as _QCA
            _gc.collect()
            _QCA.processEvents()
            _trash2 = os.path.join(
                out_dir, f".trash_{os.path.basename(folder)}_{int(_time.time())}")
            try:
                os.rename(folder, _trash2)
                shutil.rmtree(_trash2, ignore_errors=True)
            except OSError:
                for _attempt in range(10):
                    try:
                        shutil.rmtree(folder)
                        break
                    except (PermissionError, OSError):
                        if _attempt < 9:
                            _time.sleep(0.3)
                            _gc.collect()
                            _QCA.processEvents()
                        else:
                            raise
        os.rename(tmp_folder, folder)
        # saved のパスを新フォルダに更新
        saved = [(label, os.path.join(folder, os.path.basename(p)), kind)
                 for label, p, kind in saved]
        # ── 解析完了後処理（コンボ更新・UI表示） ──
        # 解析番号コンボを更新して新番号を選択（解析完了後は最新が当該番号なのでそのまま）
        self._refresh_analysis_combo(select_latest=False)
        idx = self.cmbAnalysisNumber.findData(analysis_number)
        if idx >= 0:
            with QSignalBlocker(self.cmbAnalysisNumber):
                self.cmbAnalysisNumber.setCurrentIndex(idx)

        # 実行前に表示していた地形レイヤを新成果へ再適用する（上書き実行でも
        # 引き継ぐ）。クリーンアップで表示が消えているため、_refresh_analysis_combo
        # 経由の復元は空振りする。事前退避しておいた状態をここで適用する。
        self._restore_pre_run_display_state(analysis_number)

        self._update_analysis_condition_label(analysis_number)
        names = ", ".join(n for n, _, _ in saved)
        self.lblAnalysisStatus.setVisible(True)
        _text = f"Done [{analysis_number}] in {self._format_duration(_elapsed_s)}: {names}"
        if warning:
            _text += f"\n{warning}"
        self.lblAnalysisStatus.setText(_text)
        if self.iface is not None:
            self.iface.mapCanvas().refresh()

    def _on_analysis_refetch_done(self, phase, ok):
        """解析の前の取り直し（phase="dsm" / "dem"）が終わったとき: 解析を始め直す。"""
        self.btnStopAnalysis.setEnabled(False)
        self.lblAnalysisStatus.setVisible(True)
        if getattr(self, "_analysis_refetch_stopped", False):
            # 取り直しの途中で Stop が押された
            self._analysis_refetch_stopped = False
            self.lblAnalysisStatus.setText("Analysis cancelled.")
            return
        if phase == "dsm" or (ok and getattr(self, "_terrain_loader", None) is not None):
            self._analysis_refetch_phase = phase
            self._run_terrain_analysis()
            return
        # DEM が取れなかった（中止・エラー）: 理由は DEM の欄と同じ文言を出す
        self.lblAnalysisStatus.setText(
            f"DEM fetch did not complete: {self.lblDemInfo.text()}")

    def _on_stop_analysis(self):
        # 解析の前の DEM/DSM 取り直し中なら、取得を止める（続く取得・解析もしない）
        if self._dem_worker is not None:
            self._analysis_refetch_stopped = True
            self._cancel_dem_job()
            self.btnStopAnalysis.setEnabled(False)
            self.lblAnalysisStatus.setText("Cancelling…")
            return
        worker = self._analysis_worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            self.btnStopAnalysis.setEnabled(False)
            self.lblAnalysisStatus.setText("Cancelling…")

    def shutdown_analysis(self):
        """パネルを閉じる・プラグインを外すとき: 解析を中止して終わるのを待つ。

        スレッドを残したまま閉じると QGIS が落ちる原因になる。待ったあとの
        done は受け取らず（閉じた後の画面を触らないため）、ここで片付ける。
        """
        worker = self._analysis_worker
        ctx = self._analysis_ctx
        if worker is None:
            return
        try:
            worker.done.disconnect(self._on_analysis_done)
        except (TypeError, RuntimeError):
            pass
        worker.cancel()
        if worker.isRunning():
            worker.wait(30000)
        self._analysis_worker = None
        self._analysis_ctx = None
        if ctx is not None:
            shutil.rmtree(ctx["tmp_folder"], ignore_errors=True)
            ctx["btn_prog"].finish()
            self._set_analysis_running(False)

    # ── 解析時間の見積もり（前回実績：100万セルあたりの秒数） ──
    # 時間は機械と解析の組み合わせで大きく違うので、固定値でなく前回の
    # 実測を QSettings に残して使う。DSM の有無は差が小さいので区別しない。

    def _analysis_rate_key(self):
        parts = []
        if self.chkStability.isChecked():
            parts.append("stability")
        if self.chkValley.isChecked():
            parts.append(
                "valley_mfd" if self.chkFlowMFD.isChecked()
                else "valley_dinf" if self.chkFlowDinf.isChecked()
                else "valley_d8")
        if self.chkFlow.isChecked():
            parts.append("flow")
        return "analysis_sec_per_mcell/" + "+".join(parts)

    def _load_analysis_rate(self, key):
        s = QSettings()
        s.beginGroup(self._SK)
        try:
            v = float(s.value(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        s.endGroup()
        return v if v > 0 else None

    def _save_analysis_rate(self, key, sec_per_mcell):
        s = QSettings()
        s.beginGroup(self._SK)
        s.setValue(key, float(sec_per_mcell))
        s.endGroup()

    @staticmethod
    def _plugin_version():
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read(os.path.join(os.path.dirname(__file__), "metadata.txt"))
        return cfg.get("general", "version", fallback="")

    @staticmethod
    def _format_duration(seconds):
        s = int(round(max(0.0, seconds)))
        if s < 60:
            return f"{s} s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m} min {s} s"
        h, m = divmod(m, 60)
        return f"{h} h {m} min"

    # ── 設定の保存・復元 ──────────────────────────────────────────

    _SK = "ForestryOperationsLite"  # QSettings グループキー

    # ── 設定の保存・復元 ──────────────────────────────────────────

    _SK = "ForestryOperationsLite"  # QSettings グループキー

    def _on_project_read(self):
        """プロジェクトを開いた後にレイヤーコンボを再構築し、保存済み選択を復元する。"""
        self._refresh_layer_combos()
        self._load_settings()                          # QSettings フォールバック
        self._restore_layer_combos_from_project()      # プロジェクト設定を優先
        self._update_out_dir_label()
        self.apply_layer_display()

    def _save_settings(self):
        """地形ソース・レイヤー設定・解析パラメータを QSettings に保存する。"""
        s = QSettings()
        s.beginGroup(self._SK)

        # ── 地表データ ──
        # Canvas-bound auto-fetch sources are valid only for this session.
        session_only = DemBrowserDialog.SESSION_ONLY_SENTINELS
        s.setValue(
            "dem_path", "" if self._dem_path in session_only else self._dem_path)
        s.setValue(
            "dsm_path", "" if self._dsm_path in session_only else self._dsm_path)
        s.setValue("flow_buffer_state", self._flow_buffer_state)
        s.setValue("flow_buffer_state_tc", self._flow_buffer_state_tc)

        # ── レイヤー設定（layer ID） ──
        s.setValue("bg_layer_id",   self.cmbBackgroundLayer.currentData() or "")
        s.setValue("tile_layer_id", self.cmbTileLayer.currentData() or "")
        s.setValue("gpkg_layer_id", self.cmbGpkgLayer.currentData() or "")
        s.setValue("bg_layer_name",   self.cmbBackgroundLayer.currentText() or "")
        s.setValue("tile_layer_name", self.cmbTileLayer.currentText() or "")
        s.setValue("gpkg_layer_name", self.cmbGpkgLayer.currentText() or "")
        s.setValue("tile_opacity",  self.spinTileOpacity.value())
        s.setValue("gpkg_opacity",  self.spinGpkgOpacity.value())
        s.setValue("bg_opacity",    self.spinBgOpacity.value())
        s.setValue("gpkg_vis",      self.btnGpkgLayerVis.isChecked())
        s.setValue("tile_vis",      self.btnTileLayerVis.isChecked())
        s.setValue("bg_vis",        self.btnBgLayerVis.isChecked())
        s.setValue("map_locked",    self.chkMapLock.isChecked())


        # ── 解析設定 ──
        s.setValue("opacity_stability",  self.spinOpacityStability.value())
        s.setValue("opacity_valley",     self.spinOpacityValley.value())
        s.setValue("opacity_wetland",    self.spinOpacityWetland.value())
        s.setValue("opacity_flow",       self.spinOpacityFlow.value())
        s.setValue("opacity_integrated", self.spinOpacityIntegrated.value())
        s.setValue("filter_state_wetland", self._filter_state.get("wetland", "off"))
        s.setValue("filter_state_flow",    self._filter_state.get("flow", "off"))
        s.setValue("filter_state_flow_tc", self._filter_state_tc)
        s.setValue("chk_overwrite",       self.chkOverwrite.isChecked())
        s.setValue("chk_stability",       self.chkStability.isChecked())
        s.setValue("chk_valley",          self.chkValley.isChecked())
        s.setValue("chk_flow",            self.chkFlow.isChecked())
        if self.chkFlowMFD.isChecked():
            s.setValue("flow_method", "mfd")
        elif self.chkFlowDinf.isChecked():
            s.setValue("flow_method", "dinf")
        else:
            s.setValue("flow_method", "d8")
        s.setValue("area_limit_idx",      self.cmbAreaLimit.currentIndex())

        # ── 解析パラメータ ──
        s.setValue("spin_phi_deg",     self.spinPhiDeg.value())
        s.setValue("spin_c_kpa",       self.spinCKpa.value())
        s.setValue("spin_zm",          self.spinZm.value())
        s.setValue("spin_m_sat",       self.spinMSat.value())
        s.setValue("spin_fs_thresh",   self.spinFsThresh.value())
        s.setValue("spin_twi_thresh",  self.spinTwiThresh.value())
        s.setValue("spin_min_area",    self.spinMinArea.value())
        s.setValue("spin_rainfall",    self.spinRainfall.value())
        s.setValue("spin_runoff",      self.spinRunoff.value())
        s.setValue("spin_total_rainfall",    self.spinTotalRainfall.value())
        s.setValue("spin_duration",          self.spinDuration.value())
        s.setValue("spin_velocity_coef",     self.spinVelocityCoef.value())

        s.endGroup()

    def _save_layer_settings_to_project(self):
        """レイヤーコンボの選択をQgsProjectプロパティに保存する（プロジェクト固有）。"""
        proj = QgsProject.instance()
        proj.writeEntry("ForestryOperationsLite", "bg_layer_id",
                        self.cmbBackgroundLayer.currentData() or "")
        proj.writeEntry("ForestryOperationsLite", "tile_layer_id",
                        self.cmbTileLayer.currentData() or "")
        proj.writeEntry("ForestryOperationsLite", "gpkg_layer_id",
                        self.cmbGpkgLayer.currentData() or "")
        proj.writeEntry("ForestryOperationsLite", "bg_layer_name",
                        self.cmbBackgroundLayer.currentText() or "")
        proj.writeEntry("ForestryOperationsLite", "tile_layer_name",
                        self.cmbTileLayer.currentText() or "")
        proj.writeEntry("ForestryOperationsLite", "gpkg_layer_name",
                        self.cmbGpkgLayer.currentText() or "")

    def _find_layer_id_by_name(self, name, kind):
        if not name:
            return ""
        for layer in QgsProject.instance().mapLayers().values():
            if layer.name() != name:
                continue
            if kind == "raster" and layer.type() != layer.RasterLayer:
                continue
            if kind == "vector" and layer.type() != layer.VectorLayer:
                continue
            return layer.id()
        return ""

    def _restore_layer_combos_from_project(self):
        """QgsProjectプロパティからレイヤーコンボを復元する（QSettingsより優先）。"""
        proj = QgsProject.instance()
        for combo, key in [
            (self.cmbBackgroundLayer, "bg_layer_id"),
            (self.cmbTileLayer,       "tile_layer_id"),
            (self.cmbGpkgLayer,       "gpkg_layer_id"),
        ]:
            val, ok = proj.readEntry("ForestryOperationsLite", key, "")
            if ok and val:
                idx = combo.findData(val)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                    continue
            # ID が見つからない場合は name で復元
            name_key = key.replace("_id", "_name")
            nval, okn = proj.readEntry("ForestryOperationsLite", name_key, "")
            if okn and nval:
                kind = "vector" if key.startswith("gpkg") else "raster"
                lid = self._find_layer_id_by_name(nval, kind)
                if lid:
                    idx = combo.findData(lid)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)

    def _restore_layer_combos_if_unset(self):
        """レイヤが後から読み込まれる場合に、未選択コンボだけ再復元する。"""
        proj = QgsProject.instance()
        s = QSettings()
        s.beginGroup(self._SK)
        try:
            for combo, key in [
                (self.cmbBackgroundLayer, "bg_layer_id"),
                (self.cmbTileLayer,       "tile_layer_id"),
                (self.cmbGpkgLayer,       "gpkg_layer_id"),
            ]:
                if combo.currentData():
                    continue
                val, ok = proj.readEntry("ForestryOperationsLite", key, "")
                if ok and val:
                    idx = combo.findData(val)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
                        continue
                name_key = key.replace("_id", "_name")
                nval, okn = proj.readEntry("ForestryOperationsLite", name_key, "")
                if okn and nval:
                    kind = "vector" if key.startswith("gpkg") else "raster"
                    lid = self._find_layer_id_by_name(nval, kind)
                    if lid:
                        idx = combo.findData(lid)
                        if idx >= 0:
                            combo.setCurrentIndex(idx)
                            continue
                saved = s.value(key, "")
                if saved:
                    idx = combo.findData(saved)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
                        continue
                sname = s.value(name_key, "")
                if sname:
                    kind = "vector" if key.startswith("gpkg") else "raster"
                    lid = self._find_layer_id_by_name(sname, kind)
                    if lid:
                        idx = combo.findData(lid)
                        if idx >= 0:
                            combo.setCurrentIndex(idx)
        finally:
            s.endGroup()

    def _load_settings(self):
        """保存済み設定を復元する。コンボボックスはレイヤーが存在する場合のみ復元。"""
        s = QSettings()
        s.beginGroup(self._SK)

        def b(key, default=True):
            v = s.value(key, default)
            return v if isinstance(v, bool) else str(v).lower() not in ("false", "0", "")

        def f(key, default=0.0):
            try:
                return float(s.value(key, default))
            except (TypeError, ValueError):
                return default

        def i(key, default=0):
            try:
                return int(s.value(key, default))
            except (TypeError, ValueError):
                return default

        def restore_combo(combo, key):
            saved = s.value(key, "")
            if saved:
                idx = combo.findData(saved)
                if idx >= 0:
                    combo.setCurrentIndex(idx)

        # ── 地表データ ──
        path = s.value("dem_path", "")
        if path in DemBrowserDialog.SESSION_ONLY_SENTINELS:
            path = ""
        if path:
            _tile_labels = {
                DemBrowserDialog.GSI_DEM1A_SENTINEL:       ("GSI DEM1A (1m)",    "Fetch DEM1A 1m tiles for canvas extent"),
                DemBrowserDialog.GSI_DEM5A_SENTINEL:       ("GSI DEM5A (5m)",    "Fetch DEM5A 5m tiles for canvas extent"),
                DemBrowserDialog.GSI_DEM10B_SENTINEL:      ("GSI DEM10B (10m)",  "Fetch DEM10B 10m tiles for canvas extent"),
                DemBrowserDialog.TERRARIUM_FINE_SENTINEL:     ("Terrarium (~2m)",  "Fetch Terrarium ~2m tiles for canvas extent"),
                DemBrowserDialog.TERRARIUM_STANDARD_SENTINEL: ("Terrarium (~5m)",  "Fetch Terrarium ~5m tiles for canvas extent"),
                DemBrowserDialog.TERRARIUM_WIDE_SENTINEL:     ("Terrarium (~10m)", "Fetch Terrarium ~10m tiles for canvas extent"),
            }
            if path in _tile_labels:
                # タイルセンチネルの場合: 表示ラベルを復元し、タイル取得は解析時に実行
                display, tooltip = _tile_labels[path]
                self._dem_path = path
                self.txtDemPath.setText(display)
                self.txtDemPath.setToolTip(f"Elevation tile ({tooltip})")
                self.btnBrowseDem.setText("Clear")
                self.lblDemInfo.setText("Extent will be fetched at analysis time")
            else:
                self._dem_path = path
                self.txtDemPath.setText(os.path.basename(path))
                self.txtDemPath.setToolTip(path)
                self._load_dem_info()
        dsm_path = s.value("dsm_path", "")
        if dsm_path in DemBrowserDialog.SESSION_ONLY_SENTINELS:
            dsm_path = ""
        if dsm_path:
            self._dsm_path = dsm_path
            self.txtDsmPath.setText(os.path.basename(dsm_path))
            self.txtDsmPath.setToolTip(dsm_path)
            self._load_dsm_info()
        _fb = s.value("flow_buffer_state", "off")
        if _fb in ("off", "weak", "strong"):
            self._flow_buffer_state = _fb
        _fb_v = s.value("flow_buffer_state_tc", "off")
        if _fb_v in ("off", "weak", "strong"):
            self._flow_buffer_state_tc = _fb_v
        self._sync_flow_aux_controls()

        # ── レイヤー設定 ──
        restore_combo(self.cmbBackgroundLayer, "bg_layer_id")
        restore_combo(self.cmbTileLayer,       "tile_layer_id")
        restore_combo(self.cmbGpkgLayer,       "gpkg_layer_id")
        self.spinTileOpacity.setValue(i("tile_opacity", 60))
        self.spinGpkgOpacity.setValue(i("gpkg_opacity", 100))
        self.spinBgOpacity.setValue(i("bg_opacity", 100))
        self.btnGpkgLayerVis.setChecked(b("gpkg_vis", True))
        self.btnTileLayerVis.setChecked(b("tile_vis", True))
        self.btnBgLayerVis.setChecked(b("bg_vis", True))
        self.chkMapLock.setChecked(b("map_locked", False))


        # ── 解析設定 ──
        self.spinOpacityStability.setValue( i("opacity_stability",  70))
        self.spinOpacityValley.setValue(    i("opacity_valley",     70))
        self.spinOpacityWetland.setValue(   i("opacity_wetland",    70))
        self.spinOpacityFlow.setValue(      i("opacity_flow",       70))
        self.spinOpacityIntegrated.setValue(i("opacity_integrated", 70))
        for _fkey, _fbtn in (("wetland", self.btnFilterWetland), ("flow", self.btnFilterFlow)):
            _fval = s.value(f"filter_state_{_fkey}", "off")
            if _fval in ("off", "low", "mid"):
                self._filter_state[_fkey] = _fval
                _fbtn.setText(_fval)
        _fv = s.value("filter_state_flow_tc", "off")
        if _fv in ("off", "low", "mid"):
            self._filter_state_tc = _fv
        self._sync_flow_aux_controls()
        self.chkOverwrite.setChecked(    b("chk_overwrite",   True))
        self.chkStability.setChecked(    b("chk_stability",   True))
        self.chkValley.setChecked(       b("chk_valley",      True))
        self.chkFlow.setChecked(         b("chk_flow",        False))
        _flow_method = str(s.value("flow_method", "d8")).lower()
        if _flow_method == "mfd":
            self.chkFlowMFD.setChecked(True)
        elif _flow_method == "dinf":
            self.chkFlowDinf.setChecked(True)
        else:
            self.chkFlowD8.setChecked(True)
        self.cmbAreaLimit.setCurrentIndex(i("area_limit_idx", 1))

        # ── 解析パラメータ ──
        self.spinPhiDeg.setValue(    f("spin_phi_deg",     30.0))
        self.spinCKpa.setValue(      f("spin_c_kpa",        3.0))
        self.spinZm.setValue(        f("spin_zm",           0.5))
        self.spinMSat.setValue(      f("spin_m_sat",        0.5))
        self.spinFsThresh.setValue(  f("spin_fs_thresh",    1.2))
        self.spinTwiThresh.setValue( f("spin_twi_thresh",   8.0))
        self.spinMinArea.setValue(   f("spin_min_area",  1000.0))
        self.spinRainfall.setValue(       f("spin_rainfall",        50.0))
        self.spinRunoff.setValue(         f("spin_runoff",           0.8))
        self.spinTotalRainfall.setValue(  f("spin_total_rainfall",   100.0))
        self.spinDuration.setValue(       f("spin_duration",          6.0))
        self.spinVelocityCoef.setValue(   f("spin_velocity_coef",     0.3))

        s.endGroup()

    def closeEvent(self, event):
        # 解析中・DEM 取得中・書き出し中なら中止して終わるのを待つ（スレッドを残して閉じない）
        self.shutdown_analysis()
        self._shutdown_dem_job()
        self._shutdown_side_jobs()
        self._save_settings()
        self._save_layer_settings_to_project()  # レイヤー設定をプロジェクトにも保存
        self._unload_terrain_group()
        try:
            QgsProject.instance().readProject.disconnect(self._on_project_read)
        except Exception:  # nosec B110
            pass
        try:
            QgsProject.instance().projectSaved.disconnect(self._update_out_dir_label)
        except Exception:  # nosec B110
            pass
        try:
            QgsProject.instance().projectSaved.disconnect(self._save_layer_settings_to_project)
        except Exception:  # nosec B110
            pass
        try:
            QgsProject.instance().cleared.disconnect(self._on_project_cleared)
        except Exception:  # nosec B110
            pass
        for _sig in (QgsProject.instance().layerTreeRoot().addedChildren,
                     QgsProject.instance().layerTreeRoot().removedChildren):
            try:
                _sig.disconnect(self._update_export_button)
            except Exception:  # nosec B110
                pass
        if self.iface is not None:
            try:
                self.iface.mapCanvas().extentsChanged.disconnect(self._on_main_canvas_changed)
            except Exception:  # nosec B110
                pass
        # 開き直したときに initialize_window_mode でつなぎ直す
        self._project_signals_connected = False
        self._restore_base_layer_opacity()
        self.closingPlugin.emit()
        event.accept()
