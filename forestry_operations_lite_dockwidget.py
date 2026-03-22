import math
import os
import shutil

from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import QEvent, QSettings, Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
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


class _StopAnalysis(Exception):
    """解析キャンセル用の内部例外。"""
    pass


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
                _vs_item("🌿  VS LP/Ground → DSM  0.5m  (Shizuoka, auto-fetch)", self.VS_LP_GROUND_SENTINEL)

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
        # VS LP/Ground 自動取得 → DSM 変換
        if path == self.VS_LP_GROUND_SENTINEL:
            self._lbl_info.setText(
                "Virtual Shizuoka LP/Ground → DSM 0.5m — EPSG:6676, CC BY 4.0.\n"
                "Ground-filtered LAS tiles fetched from S3 (newest year first) and converted to DSM (~200 MB/tile).\n"
                "Coverage: Shizuoka prefecture (all areas, 2019–2025)."
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
                except Exception:
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
        except Exception:
            pass
        super().deactivate()

    def deactivate(self):
        try:
            self.canvas().viewport().removeEventFilter(self)
        except Exception:
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


class ForestryOperationsLiteDockWidget(QtWidgets.QDockWidget, FORM_CLASS):
    closingPlugin = pyqtSignal()

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setupUi(self)

        self.preview_canvas = None
        self._added_to_main_window = False

        self._syncing = False
        self._initializing = True     # 起動完了まで preview→main 同期をブロック
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
        # プロジェクトを開いた後にもレイヤー選択を復元する
        QgsProject.instance().readProject.connect(self._on_project_read)
        # プロジェクト保存時に出力先ラベル更新 + レイヤー設定をプロジェクトへ書込
        QgsProject.instance().projectSaved.connect(self._update_out_dir_label)
        QgsProject.instance().projectSaved.connect(self._save_layer_settings_to_project)
        # プロジェクトクリア時に地形グループ参照をリセット
        QgsProject.instance().cleared.connect(self._on_project_cleared)
        self._load_settings()
        self._restore_layer_combos_from_project()  # プロジェクト組み込み設定を優先適用

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
        dem_lay.addWidget(QtWidgets.QLabel("DSM/DTM Data"), 2, 0)
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
        self._dem_loading = False
        self._dem_load_cancel = False
        self._dsm_loading = False
        self._dsm_load_cancel = False
        self._vs_export_dir = ""
        self._vs_export_cancel = False
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

        left_layout.addWidget(self.leftTabs)

        # ── 解析データの表示管理（プレビュー上部・2行）────────────────
        grpDisplayMgmt = QtWidgets.QGroupBox("Analysis Layer Display")
        dm_vlay = QtWidgets.QVBoxLayout(grpDisplayMgmt)
        dm_vlay.setContentsMargins(6, 2, 6, 4)
        dm_vlay.setSpacing(2)
        # Row1: 解析番号セレクタ
        dm_row1 = QtWidgets.QHBoxLayout()
        dm_row1.setSpacing(4)
        dm_row1.addWidget(self.cmbAnalysisNumber)
        dm_row1.addWidget(self.btnRefreshAnalysis)
        dm_row1.addSpacing(20)
        dm_row1.addWidget(self.lblLoadStatus)
        dm_row1.addStretch(1)
        dm_vlay.addLayout(dm_row1)
        # Row2: 種別トグルボタン + 透過率
        dm_row2 = QtWidgets.QHBoxLayout()
        dm_row2.setSpacing(2)
        dm_row2.addWidget(self.btnTerrainToggle)
        dm_row2.addSpacing(6)
        for _b, _sp, _fb, _fb2 in (
            (self.chkLoadStability,  self.spinOpacityStability,  None,                   None),
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
        self._filter_state = {"wetland": "off", "flow": "off"}  # off/low/mid
        self._flow_buffer_state = "off"  # off/weak/strong
        self._flow_buffer_layer_ids = []  # バッファ滲みレイヤーのID管理
        self._flow_buffer_mem_paths = []  # vsimem パス（解放用）
        self._loaded_terrain_basenames = {}  # key → base_name（フィルタ再適用用）
        self._terrain_layer_group = None   # QgsLayerTreeGroup or None
        self._exclusive_hidden = {}        # 排他非表示中のキー → 保存済みcycle state

        # --- 解析種別 ---
        grpTypes = QtWidgets.QGroupBox("Analysis Types")
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
        self.spinPhiDeg.setRange(0, 60); self.spinPhiDeg.setValue(35); self.spinPhiDeg.setSuffix("°")
        self.spinCKpa   = QtWidgets.QDoubleSpinBox()
        self.spinCKpa.setRange(0, 200);  self.spinCKpa.setValue(0);  self.spinCKpa.setSuffix(" kPa")
        self.spinZm     = QtWidgets.QDoubleSpinBox()
        self.spinZm.setRange(0.1, 10);   self.spinZm.setValue(1.0); self.spinZm.setSuffix(" m")
        self.spinMSat   = QtWidgets.QDoubleSpinBox()
        self.spinMSat.setRange(0, 1);    self.spinMSat.setValue(0.5); self.spinMSat.setSingleStep(0.1)
        self.spinFsThresh = QtWidgets.QDoubleSpinBox()
        self.spinFsThresh.setRange(0.5, 3.0); self.spinFsThresh.setValue(1.5); self.spinFsThresh.setSingleStep(0.1)
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
        self.spinTwiThresh = QtWidgets.QDoubleSpinBox()
        self.spinTwiThresh.setRange(1, 20); self.spinTwiThresh.setValue(8.0); self.spinTwiThresh.setSingleStep(0.5)
        self.spinMinArea = QtWidgets.QDoubleSpinBox()
        self.spinMinArea.setRange(100, 100000); self.spinMinArea.setValue(1000); self.spinMinArea.setSuffix(" m²"); self.spinMinArea.setSingleStep(500)
        _pv_defs = [
            ("TWI Threshold",       self.spinTwiThresh, "1–20  (higher = more wetland areas captured)"),
            ("Min. Catchment Area", self.spinMinArea,   "100–100000 m²  (smaller = finer stream network)"),
        ]
        for i, (en, w, hint) in enumerate(_pv_defs):
            row = i * 2
            ql = QtWidgets.QLabel(en)
            pv_lay.addWidget(ql, row, 0)
            pv_lay.addWidget(w, row, 1)
            _hl = QtWidgets.QLabel(hint)
            _hl.setStyleSheet("color:#888;font-size:8pt;margin-bottom:10px;")
            pv_lay.addWidget(_hl, row + 1, 0, 1, 2)
        pv_lay.setRowStretch(len(_pv_defs) * 2, 1)

        # --- 流量 パラメータ ---
        self.grpParamFlow = QtWidgets.QGroupBox("Flow (Rational Method) Parameters")
        pf_lay = QtWidgets.QGridLayout(self.grpParamFlow)
        pf_lay.setVerticalSpacing(2)
        pf_lay.setColumnStretch(0, 1)
        self.spinRainfall = QtWidgets.QDoubleSpinBox()
        self.spinRainfall.setRange(1, 500); self.spinRainfall.setValue(50); self.spinRainfall.setSuffix(" mm/h")
        self.spinRainfall.setToolTip("Peak rainfall intensity i_peak (used for Qp calculation)")
        self.spinRunoff   = QtWidgets.QDoubleSpinBox()
        self.spinRunoff.setRange(0.1, 1.0); self.spinRunoff.setValue(0.8); self.spinRunoff.setSingleStep(0.05)
        self.spinRunoff.setToolTip("Runoff coefficient C")
        self.spinTotalRainfall = QtWidgets.QDoubleSpinBox()
        self.spinTotalRainfall.setRange(1, 2000); self.spinTotalRainfall.setValue(100); self.spinTotalRainfall.setSuffix(" mm")
        self.spinTotalRainfall.setToolTip("Total rainfall for period (used for Qm/V calculation)")
        self.spinDuration = QtWidgets.QDoubleSpinBox()
        self.spinDuration.setRange(0.5, 72); self.spinDuration.setValue(6.0); self.spinDuration.setSuffix(" h"); self.spinDuration.setSingleStep(0.5)
        self.spinDuration.setToolTip("Rainfall duration T (reference time vs Tc)")
        self.spinVelocityCoef = QtWidgets.QDoubleSpinBox()
        self.spinVelocityCoef.setRange(0.01, 5.0); self.spinVelocityCoef.setValue(0.3)
        self.spinVelocityCoef.setSingleStep(0.05); self.spinVelocityCoef.setDecimals(2)
        self.spinVelocityCoef.setToolTip("Flow velocity coef. v_coef [m/s]  velocity = v_coef × tan(slope)^0.5\nForest: 0.3, grass: 0.6, pavement: 1.5")
        self._lbl_vel_coef = QtWidgets.QLabel("Flow velocity coef.")
        _pf_defs = [
            ("i_peak",              self.spinRainfall,      "Peak rainfall intensity  1–500 mm/h"),
            ("Runoff coef. C",      self.spinRunoff,        "0.1–1.0  (forest 0.3, grass 0.6, bare soil 0.9)"),
            ("Total rainfall",      self.spinTotalRainfall, "Total precipitation for the period  1–2000 mm"),
            ("Duration T",          self.spinDuration,      "Rainfall duration vs. Tc  0.5–72 h"),
            (self._lbl_vel_coef,    self.spinVelocityCoef,  "Forest 0.3, grass 0.6, pavement 1.5  (0.01–5.0 m/s)"),
        ]
        for i, (en, w, hint) in enumerate(_pf_defs):
            row = i * 2
            ql = en if isinstance(en, QtWidgets.QLabel) else QtWidgets.QLabel(en)
            pf_lay.addWidget(ql, row, 0)
            pf_lay.addWidget(w, row, 1)
            _hl = QtWidgets.QLabel(hint)
            _hl.setStyleSheet("color:#888;font-size:8pt;margin-bottom:10px;")
            pf_lay.addWidget(_hl, row + 1, 0, 1, 2)
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
        self.btnRunAnalysis = QtWidgets.QPushButton("Run Analysis")
        self.btnRunAnalysis.setMinimumHeight(32)
        out_lay.addWidget(self.btnRunAnalysis)
        self.lblAnalysisStatus = QtWidgets.QLabel("Ready")
        self.lblAnalysisStatus.setWordWrap(True)
        out_lay.addWidget(self.lblAnalysisStatus)
        self.progressAnalysis = QtWidgets.QProgressBar()
        self.progressAnalysis.setRange(0, 0)
        self.progressAnalysis.setVisible(False)
        out_lay.addWidget(self.progressAnalysis)
        lay.addWidget(grpOut)

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
        combo.blockSignals(True)
        combo.clear()
        for text, value in data:
            combo.addItem(text, value)
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

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
                except Exception:
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
                    except Exception:
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
        """showMaximized()後にキャンバスサイズが確定してから呼ばれる。
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

    def initialize_window_mode(self):
        standalone = True
        QSettings().setValue("forestry_operations_lite/standalone_window", True)
        self.chkStandaloneWindow.blockSignals(True)
        self.chkStandaloneWindow.setChecked(standalone)
        self.chkStandaloneWindow.blockSignals(False)
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
            if self.iface is not None and self._added_to_main_window:
                self.iface.mainWindow().removeDockWidget(self)
                self._added_to_main_window = False
            self.setParent(None, Qt.Window)
            self.showMaximized()
            QTimer.singleShot(300, self._finish_init)
        else:
            if self.iface is not None and not self._added_to_main_window:
                self.setParent(self.iface.mainWindow())
                self.iface.addDockWidget(Qt.BottomDockWidgetArea, self)
                self._added_to_main_window = True
            self.setAllowedAreas(Qt.BottomDockWidgetArea)
            if self.isFloating():
                self.setFloating(False)
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
        has_base_layers = any(
            lyr is not None and lyr.isValid() for lyr in (bg, tile, gpkg)
        )
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
            flow_lids = set(getattr(self, "_loaded_terrain_layers", {}).get("flow", []))
            flow_buf_added = False
            for key, ids in getattr(self, "_loaded_terrain_layers", {}).items():
                for lid in ids:
                    lyr = proj.mapLayer(lid)
                    if lyr is not None and lyr.isValid():
                        terrain_lyrs.append(lyr)
                    # flowレイヤーの直後にバッファ滲みレイヤーを挿入
                    if lid in flow_lids and not flow_buf_added:
                        for blid in getattr(self, "_flow_buffer_layer_ids", []):
                            blyr = proj.mapLayer(blid)
                            if blyr is not None and blyr.isValid():
                                terrain_lyrs.append(blyr)
                        flow_buf_added = True
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
        except Exception:
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
            except Exception:
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
        # レイヤー透過率を設定（プロジェクトレイヤー側に反映）
        bg   = self._get_selected_layer(self.cmbBackgroundLayer)
        tile = self._get_selected_layer(self.cmbTileLayer)
        gpkg = self._get_selected_layer(self.cmbGpkgLayer)
        has_base_layers = any(
            lyr is not None and lyr.isValid() for lyr in (bg, tile, gpkg)
        )
        if bg is not None:
            bg.setOpacity(self.spinBgOpacity.value() / 100.0)
            bg.triggerRepaint()
        if tile is not None:
            tile.setOpacity(self.spinTileOpacity.value() / 100.0)
            tile.triggerRepaint()
        if gpkg is not None:
            gpkg.setOpacity(self.spinGpkgOpacity.value() / 100.0)
            gpkg.triggerRepaint()

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

    # 低値透過フィルタ対象キー（twi, 流量系）
    _FILTER_KEYS = {"twi", "flow_peak", "flow_mean", "flow_vtotal"}

    @staticmethod
    def _apply_raster_color(lyr, base_name, filter_mode="off"):
        """base_name ごとに目的別カラーランプを設定。
        固定値型: FS・統合リスクなど物理的意味のある値を使用。
        動的型: 実データの min/max に合わせてスケール（流量は対数スケール）。
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
            "tc": {
                "log": False,
                "colors": [
                    (QColor(255, 255, 220), "Short (near outlet)"),
                    (QColor(180, 210, 140), "Mid"),
                    (QColor( 50, 120,  50), "Long (near summit)"),
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
            "flow_vtotal": {
                "log": True,
                "colors": [
                    (QColor(255, 228, 232), "Low"),
                    (QColor(210,  50,  80), "Mid"),
                    (QColor(130,   0,  45), "High"),
                ],
            },
        }

        # サンプル数を制限して高速化（全ピクセルスキャンは大規模ラスターで数秒かかる）
        from qgis.core import QgsRasterBandStats
        stats = lyr.dataProvider().bandStatistics(
            1,
            QgsRasterBandStats.Min | QgsRasterBandStats.Max,
            lyr.extent(),
            250_000,
        )
        vmin, vmax = stats.minimumValue, stats.maximumValue
        if vmin >= vmax:
            return

        if base_name in FIXED:
            items = [
                QgsColorRampShader.ColorRampItem(v, c, l)
                for v, c, l in FIXED[base_name]
            ]
        elif base_name in DYNAMIC:
            cfg = DYNAMIC[base_name]
            colors = cfg["colors"]
            n = len(colors)
            if cfg["log"] and vmin > 0:
                log_min = math.log10(vmin)
                log_max = math.log10(vmax)
                values = [10 ** (log_min + (log_max - log_min) * i / (n - 1))
                          for i in range(n)]
            else:
                values = [vmin + (vmax - vmin) * i / (n - 1) for i in range(n)]
            items = [
                QgsColorRampShader.ColorRampItem(v, c, l)
                for v, (c, l) in zip(values, colors)
            ]

            # 低値透過フィルタ適用（対象キーのみ）
            if base_name in {"twi", "flow_peak", "flow_mean", "flow_vtotal"} and filter_mode in ("low", "mid"):
                n = len(items)
                if filter_mode == "low":
                    # 中間ストップを上限の35%に固定
                    alphas = [0] + [int(255 * 0.35)] * (n - 2) + [255]
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
        shader_func.setColorRampItemList(items)
        shader_func.setMinimumValue(vmin)
        shader_func.setMaximumValue(vmax)

        raster_shader = QgsRasterShader()
        raster_shader.setRasterShaderFunction(shader_func)

        renderer = QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, raster_shader)
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

    def _toggle_filter(self, key):
        """湿潤地形/流量推測の低値透過フィルタを off→low→mid→off と循環する。"""
        states = ["off", "low", "mid"]
        current = self._filter_state.get(key, "off")
        next_state = states[(states.index(current) + 1) % len(states)]
        self._filter_state[key] = next_state

        btn = self.btnFilterWetland if key == "wetland" else self.btnFilterFlow
        btn.setText(next_state)

        # 表示中のラスタレイヤへ即時再適用
        base_name = self._loaded_terrain_basenames.get(key)
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
        if self.preview_canvas is not None:
            self.preview_canvas.refresh()

    # バッファ強度ごとのブラー半径（ピクセル）
    _FLOW_BUFFER_SIGMA  = {"weak": 2, "strong": 3}   # Gaussian sigma（ピクセル）
    _FLOW_BUFFER_OPACITY = {"weak": 0.45, "strong": 0.65}  # 滲みレイヤーの透過率

    def _on_terrain_toggle(self, checked: bool):
        """解析データ表示トグル: ON=表示・OFF=非表示（個別設定は保持）。"""
        self._terrain_layers_visible = checked
        self._refresh_preview_canvas()

    def _cycle_flow_buffer(self):
        """流量レイヤーのバッファ（滲み）表現を off→弱→強→off と循環する。"""
        states = ["off", "weak", "strong"]
        labels = {
            "off":    "Buffer: Off",
            "weak":   "Buffer: Low",
            "strong": "Buffer: High",
        }
        cur = self._flow_buffer_state
        nxt = states[(states.index(cur) + 1) % len(states)]
        self._flow_buffer_state = nxt
        self.btnFlowBuffer.setText(labels[nxt])
        active = nxt != "off"
        self.btnFlowBuffer.setStyleSheet(
            ("font-size:8pt; padding:1px 2px;"
             "background:#e07050; color:white; border-radius:2px;")
            if active else
            "font-size:8pt; padding:1px 2px;"
        )
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

        state = self._flow_buffer_state
        if state == "off":
            self._refresh_preview_canvas()
            return

        sigma   = self._FLOW_BUFFER_SIGMA[state]
        opacity = self._FLOW_BUFFER_OPACITY[state]

        for lid in self._loaded_terrain_layers.get("flow", []):
            lyr = proj.mapLayer(lid)
            if lyr is None or lyr.type() != lyr.RasterLayer:
                continue
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

            base_name = self._loaded_terrain_basenames.get("flow")
            if base_name:
                self._apply_raster_color(blur_lyr, base_name,
                                         self._filter_state.get("flow", "off"))
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

            self._flow_buffer_layer_ids.append(blur_lyr.id())
            self._flow_buffer_mem_paths.append(mem_path)

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
            ("flow_peak",   "Peak Flow: Qp[m³/s]",   "raster", ".tif"),
            ("flow_mean",   "Mean Flow: Qm[m³/s]",   "raster", ".tif"),
            ("flow_vtotal", "Total Flow Vol.: V[m³]", "raster", ".tif"),
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

    # レイヤパネル内の順序: 値が大きいほど上（描画上位）
    # 下から: stability → wetland → valley → flow → integrated（最上位）
    # 下から: stability → integrated → wetland → valley → flow（最上位）
    _KEY_RANK = {"stability": 0, "integrated": 1, "wetland": 2, "valley": 3, "flow": 4}

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
        btn.blockSignals(True)
        btn.setChecked(False)
        btn.setText(self._BTN_LABELS[key][0])
        btn.setStyleSheet(self._BTN_STYLE_NORMAL)
        btn.blockSignals(False)

    def _reset_load_buttons(self):
        """すべての読込ボタンを非表示状態にリセットする。"""
        self._terrain_cycle_state.clear()
        for key, (label, _) in self._BTN_LABELS.items():
            btn = self._btn(key)
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.setText(label)
            btn.blockSignals(False)

    def _cycle_terrain_layer(self, key):
        """クリック毎に 非表示→ファイル1→ファイル2→...→非表示 と循環する。"""
        analysis_number = self.cmbAnalysisNumber.currentData()
        btn = self._btn(key)
        base_label = self._BTN_LABELS[key][0]

        if not analysis_number:
            self.lblLoadStatus.setText("Select an analysis run")
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
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
            self.lblLoadStatus.setText("No file")
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
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
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.setText(base_label)
            btn.setStyleSheet(self._BTN_STYLE_NORMAL)
            btn.blockSignals(False)
            self.lblLoadStatus.setText("")
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
                filter_mode = self._filter_state.get(key, "off")
                self._apply_raster_color(lyr, base_name, filter_mode)
                self._loaded_terrain_basenames[key] = base_name
                lyr.renderer().setOpacity(opacity)
            else:
                self._apply_vector_style(lyr, base_name)
                lyr.setOpacity(opacity)
            proj.addMapLayer(lyr, False)
            self._insert_terrain_layer_ordered(key, lyr)
            self._loaded_terrain_layers[key] = [lyr.id()]
            self._refresh_preview_canvas()
            # プレビュー表示域にレイヤが含まれない場合はレイヤ範囲へズーム（CRS変換あり）
            if self.preview_canvas is not None:
                self._zoom_preview_to_layer_if_needed(lyr)
            self._zoom_preview_to_analysis_extent_if_available()
            status = f"Showing {label} ({next_state + 1}/{n})." if n > 1 else f"Showing {label}."
            self.lblLoadStatus.setText(status)
            # 流量レイヤー切替後にバッファ状態を引き継ぐ
            if key == "flow" and self._flow_buffer_state != "off":
                self._apply_flow_buffer()
        else:
            self.lblLoadStatus.setText(f"Load error: {label}")
            self._terrain_cycle_state[key] = current  # 状態を戻す

        btn.blockSignals(True)
        btn.setChecked(True)
        btn.setText(base_label)
        btn.setStyleSheet(
            self._BTN_STYLE_BLOCKING if _blocking else self._BTN_STYLE_NORMAL)
        btn.blockSignals(False)

    def _toggle_terrain_layer(self, key, checked):
        """チェックON→選択解析番号のファイルを読込、OFF→該当レイヤを削除"""
        if checked:
            analysis_number = self.cmbAnalysisNumber.currentData()
            if not analysis_number:
                self.lblLoadStatus.setText("Select an analysis run")
                btn_map = {
                    "stability":  self.chkLoadStability,
                    "valley":     self.chkLoadValley,
                    "wetland":    self.chkLoadWetland,
                    "flow":       self.chkLoadFlow,
                    "integrated": self.chkLoadIntegrated,
                }
                btn = btn_map.get(key)
                if btn:
                    btn.blockSignals(True)
                    btn.setChecked(False)
                    btn.blockSignals(False)
                return
            out_dir = self._terrain_output_dir()
            # グループがなければ作成
            self._ensure_terrain_group(analysis_number)
            added = []
            ids = []
            for pat in self._TERRAIN_PATTERNS[key]:
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
                        except Exception:
                            pass
                        lyr.renderer().setOpacity(opacity)
                    else:
                        try:
                            self._apply_vector_style(lyr, color_name)
                        except Exception:
                            pass
                        lyr.setOpacity(opacity)
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
                n_total = len(self._TERRAIN_PATTERNS[key])
                if n_added > 1 or n_total > 1:
                    status = f"Showing {names} ({n_added}/{n_total})."
                else:
                    status = f"Showing {names}."
                self.lblLoadStatus.setText(status)
            else:
                self.lblLoadStatus.setText("No file")
        else:
            ids = self._loaded_terrain_layers.pop(key, [])
            proj = QgsProject.instance()
            for lid in ids:
                if proj.mapLayer(lid):
                    proj.removeMapLayer(lid)
            self._refresh_preview_canvas()
            self.lblLoadStatus.setText("")
            self._zoom_preview_to_analysis_extent_if_available()

    def _on_browse_dem(self):
        if self._dem_loading:
            self._dem_load_cancel = True
            self.btnBrowseDem.setEnabled(False)
            self.lblDemInfo.setText("Cancelling…")
            return
        if self._dem_path:
            # クリア
            _was_vs = (self._dem_path == DemBrowserDialog.VS_LP_GRID_SENTINEL)
            self._dem_path = ""
            self._terrain_loader = None
            self._vs_dem_codes = []
            self.txtDemPath.clear()
            self.txtDemPath.setToolTip("")
            self.lblDemInfo.setText("Not set")
            self.btnBrowseDem.setText("Browse")
            if _was_vs:
                # VS LP/Grid だった場合は DSM も連動クリア
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
                self.lblDsmInfo.setText("Auto: VS LP/Ground (fetching after DEM…)")
                self._load_vs_lp_grid()  # auto_dsm=True (default)
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

    def _load_gsi_dem(self, sentinel=None):
        """国土地理院タイルをキャンバス範囲で取得し GeoTIFF に保存後 DEMLoader で読み込む。
        sentinel で DEM1A/DEM5A/DEM10B を選択（省略時は _dem_path から判定）。
        """
        from .terrain.dem_loader import GSITileDEMLoader, DEMLoader, save_as_geotiff
        from qgis.core import (QgsCoordinateReferenceSystem,
                               QgsCoordinateTransform, QgsProject)
        import numpy as _np
        import datetime as _dt

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
        self._dem_loading = True
        self._dem_load_cancel = False
        self.btnBrowseDem.setText("Cancel")
        QtWidgets.QApplication.processEvents()

        gsi_loader = GSITileDEMLoader()
        try:
            gsi_loader.fetch_for_extent(
                ext84.xMinimum(), ext84.yMinimum(),
                ext84.xMaximum(), ext84.yMaximum(),
                sources=sources,
                cancel_cb=lambda: self._dem_load_cancel,
            )
        except MemoryError as e:
            self.lblDemInfo.setText(f"⚠ {e}")
            return
        finally:
            self._dem_loading = False
            self.btnBrowseDem.setEnabled(True)
            self.btnBrowseDem.setText("Clear" if self._dem_path else "Browse")

        if getattr(gsi_loader, "_cancelled", False):
            self.lblDemInfo.setText("Cancelled")
            return

        if gsi_loader.data is None or _np.all(_np.isnan(gsi_loader.data)):
            errs = getattr(gsi_loader, "last_errors", [])
            detail = errs[0] if errs else "out of range or connection error"
            self.lblDemInfo.setText(f"⚠ Tile fetch failed: {detail}")
            return

        # GeoTIFF として保存
        out_dir = os.path.join(self._terrain_output_dir(), "dem")
        os.makedirs(out_dir, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        tif_path = os.path.join(out_dir, f"{fname_prefix}_{ts}.tif")

        self.lblDemInfo.setText("Converting to GeoTIFF...")
        QtWidgets.QApplication.processEvents()

        try:
            save_as_geotiff(gsi_loader, tif_path)
        except Exception as e:
            self.lblDemInfo.setText(f"⚠ GeoTIFF save error: {e}")
            return

        # EPSG:4326 → UTM 自動変換（メートル単位で解析するため）
        centre_lon = (ext84.xMinimum() + ext84.xMaximum()) / 2
        centre_lat = (ext84.yMinimum() + ext84.yMaximum()) / 2
        self.lblDemInfo.setText("Reprojecting to UTM...")
        QtWidgets.QApplication.processEvents()
        tif_path = self._reproject_to_utm(tif_path, centre_lon, centre_lat)

        # 標準 DEMLoader で読み直す
        dem_loader = DEMLoader()
        dem_loader.load(tif_path)
        self._terrain_loader = dem_loader
        self._dem_actual_path = tif_path  # 実ファイルパス（params.json 用）
        # _dem_path は sentinel のまま維持（GSI ソースを追跡するため）
        self._vs_dem_codes = []  # 非 VS ソース: コードなし

        self.txtDemPath.setToolTip(tif_path)
        self.lblDemInfo.setText(dem_loader.info_text())

    def _load_vs_lp_grid(self, auto_dsm=True):
        """Virtual Shizuoka LP/Grid タイルをキャンバス範囲で S3 取得し DEM としてロード。
        auto_dsm=True のとき、DEM 成功後に LP/Ground DSM を自動取得する（初回選択時）。
        解析時の再取得など DSM 不要の場合は auto_dsm=False を指定する。"""
        from .vs_lp import resolve_years, tiles_for_extent, download_grid_tif, merge_tifs
        from .terrain.dem_loader import DEMLoader
        from qgis.core import (QgsCoordinateReferenceSystem,
                               QgsCoordinateTransform, QgsProject)
        import datetime as _dt

        if self.preview_canvas is None or self.preview_canvas.extent().isEmpty():
            self.lblDemInfo.setText("⚠ No extent on preview canvas.")
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
            return

        self.lblDemInfo.setText(f"Resolving {len(codes)} tile(s)...")
        self._dem_loading = True
        self._dem_load_cancel = False
        self.btnBrowseDem.setText("Cancel")
        QtWidgets.QApplication.processEvents()

        try:
            def _prog(done, total):
                self.lblDemInfo.setText(f"Checking S3… {done}/{total}")
                QtWidgets.QApplication.processEvents()

            resolved = resolve_years(codes, progress_cb=_prog)
            if not resolved:
                self.lblDemInfo.setText("⚠ No VS LP Grid tiles available for this area.")
                return

            out_dir = os.path.join(self._terrain_output_dir(), "vs_lp_grid")
            os.makedirs(out_dir, exist_ok=True)

            tif_paths = []
            errors = []
            for i, (code, year) in enumerate(resolved.items()):
                if self._dem_load_cancel:
                    self.lblDemInfo.setText("Cancelled")
                    return
                self.lblDemInfo.setText(f"Downloading {i + 1}/{len(resolved)}…")
                QtWidgets.QApplication.processEvents()
                try:
                    tif_paths.append(download_grid_tif(code, year, out_dir))
                except Exception as e:
                    errors.append(f"{code}: {e}")

            if not tif_paths:
                self.lblDemInfo.setText("⚠ All tile downloads failed.\n" + "\n".join(errors[:3]))
                return

            self.lblDemInfo.setText("Processing…")
            QtWidgets.QApplication.processEvents()

            ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            if len(tif_paths) == 1:
                tif_path = tif_paths[0]
            else:
                tif_path = os.path.join(out_dir, f"vs_grid_{ts}.tif")
                merge_tifs(tif_paths, tif_path)

            dem_loader = DEMLoader()
            dem_loader.load(tif_path)
            self._terrain_loader = dem_loader
            self._dem_actual_path = tif_path
            self.txtDemPath.setToolTip(tif_path)
            self.lblDemInfo.setText(
                f"{dem_loader.info_text()}  [{len(resolved)} tile(s), "
                f"year(s): {', '.join(str(y) for y in sorted(set(resolved.values())))}]"
            )
            self._vs_dem_codes = list(resolved.keys())

            # ── 自動 DSM（VS LP/Ground）──────────────────────────────
            if auto_dsm and not self._dem_load_cancel:
                self._dsm_path = DemBrowserDialog.VS_LP_GROUND_SENTINEL
                self.txtDsmPath.setText("VS LP/Ground → DSM (0.5m)")
                self.txtDsmPath.setToolTip(
                    "Virtual Shizuoka LP/Original → DSM — auto-fetch from S3 (newest year, all areas)"
                )
                self.btnBrowseDsm.setText("")
                self._load_vs_lp_ground(auto_mode=True)
                # 取得失敗・キャンセル時は DSM パスをクリア
                if not getattr(self, "_dsm_loader", None):
                    self._dsm_path = ""
                    self.txtDsmPath.clear()
                    self.txtDsmPath.setToolTip("")

            self._update_vs_export_buttons()
        finally:
            self._dem_loading = False
            self.btnBrowseDem.setEnabled(True)
            self.btnBrowseDem.setText("Clear" if self._dem_path else "Browse")

    def _load_vs_lp_ground(self, auto_mode=False):
        """Virtual Shizuoka LP/Ground タイルをキャンバス範囲で S3 取得し LAS→DSM 変換して DSM としてロード。
        LP/Ground は全年度・全エリア（OE含む）を提供。
        auto_mode=True のとき DEM ロードの内部から呼ばれ、キャンセルは _dem_load_cancel を使う。"""
        from .vs_lp import (resolve_years, tiles_for_extent,
                            download_las, las_to_dsm, merge_tifs)
        from .terrain.dem_loader import DEMLoader
        from qgis.core import (QgsCoordinateReferenceSystem,
                               QgsCoordinateTransform, QgsProject)
        import datetime as _dt

        if self.preview_canvas is None or self.preview_canvas.extent().isEmpty():
            self.lblDsmInfo.setText("⚠ No canvas extent.")
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
            return

        # auto_mode: キャンセルは DEM 側のフラグを使う。ボタン管理は DEM 側が行う。
        def _is_cancelled():
            return self._dem_load_cancel if auto_mode else self._dsm_load_cancel

        self.lblDsmInfo.setText(f"Resolving {len(codes)} tile(s)...")
        if not auto_mode:
            self._dsm_loading = True
            self._dsm_load_cancel = False
            self.btnBrowseDsm.setText("Cancel")
        QtWidgets.QApplication.processEvents()

        try:
            def _prog(done, total):
                self.lblDsmInfo.setText(f"Checking S3… {done}/{total}")
                QtWidgets.QApplication.processEvents()

            resolved = resolve_years(codes, progress_cb=_prog, lp_type="Ground")
            if not resolved:
                self.lblDsmInfo.setText("⚠ No LP/Ground tiles available for this area.")
                return

            ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = os.path.join(self._terrain_output_dir(), "vs_lp_ground")
            os.makedirs(out_dir, exist_ok=True)

            dsm_tifs = []
            self._vs_las_paths = []
            for i, (code, year) in enumerate(resolved.items()):
                if _is_cancelled():
                    self.lblDsmInfo.setText("Cancelled")
                    return
                self.lblDsmInfo.setText(
                    f"DSM {i + 1}/{len(resolved)} — downloading LAS (~237 MB)…"
                )
                QtWidgets.QApplication.processEvents()
                try:
                    las_path = download_las(code, year, out_dir)
                    self._vs_las_paths.append(las_path)
                    if _is_cancelled():
                        self.lblDsmInfo.setText("Cancelled")
                        return
                    self.lblDsmInfo.setText(f"DSM {i + 1}/{len(resolved)} — converting LAS…")
                    QtWidgets.QApplication.processEvents()
                    dsm_tif = os.path.join(out_dir, f"{code}_dsm.tif")
                    las_to_dsm(las_path, dsm_tif)
                    dsm_tifs.append(dsm_tif)
                except Exception as e:
                    self.lblDsmInfo.setText(f"Error ({code}): {e}")
                    return

            if not dsm_tifs:
                self.lblDsmInfo.setText("⚠ All DSM downloads failed.")
                return

            if len(dsm_tifs) == 1:
                tif_path = dsm_tifs[0]
            else:
                tif_path = os.path.join(out_dir, f"vs_dsm_{ts}.tif")
                merge_tifs(dsm_tifs, tif_path)

            dsm_loader = DEMLoader()
            dsm_loader.load(tif_path)
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

            # 今回使用しない古い LAS・DSM TIF を削除
            current_las = {os.path.normpath(p) for p in self._vs_las_paths}
            current_dsm = {os.path.normpath(p) for p in dsm_tifs}
            for fname in os.listdir(out_dir):
                fpath = os.path.normpath(os.path.join(out_dir, fname))
                if not os.path.isfile(fpath):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext == ".las" and fpath not in current_las:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
                elif ext == ".tif" and fpath not in current_dsm and fpath != os.path.normpath(tif_path):
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
        finally:
            if not auto_mode:
                self._dsm_loading = False
                self.btnBrowseDsm.setEnabled(True)
                self.btnBrowseDsm.setText("Clear" if self._dsm_path else "Browse")

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
                except Exception:
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
        except Exception:
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

    def _scan_analysis_numbers(self):
        """出力ディレクトリから解析番号サブフォルダの一覧を返す"""
        import re as _re
        out_dir = self._terrain_output_dir()
        if not os.path.isdir(out_dir):
            return []
        numbers = [
            name for name in os.listdir(out_dir)
            if os.path.isdir(os.path.join(out_dir, name))
            and _re.fullmatch(r'\d{4}(\+\d+)?', name)
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
        overwrite=True の場合は最新シーケンスのサブフォルダを丸ごと削除して同番号を返す。
        """
        import re as _re, shutil as _shutil
        numbers = self._scan_analysis_numbers()
        if not numbers:
            return "001"
        max_seq = max(int(n[:3]) for n in numbers)
        if overwrite:
            out_dir = self._terrain_output_dir()
            prefix = f"{max_seq:03d}"
            for name in os.listdir(out_dir):
                if _re.fullmatch(r'\d{4}(\+\d+)?', name) and name[:3] == prefix:
                    folder = os.path.join(out_dir, name)
                    if os.path.isdir(folder):
                        try:
                            _shutil.rmtree(folder)
                        except Exception:
                            pass
            return prefix
        return f"{max_seq + 1:03d}"

    def _refresh_analysis_combo(self, select_latest=True):
        """解析番号コンボを出力ディレクトリの内容で更新する。
        select_latest=True（デフォルト）: 最新番号を自動選択して表示待機状態にする。
        select_latest=False: 現在の選択を維持する（解析完了後の更新など）。
        """
        current = self.cmbAnalysisNumber.currentData()
        self.cmbAnalysisNumber.blockSignals(True)
        self.cmbAnalysisNumber.clear()
        self.cmbAnalysisNumber.addItem("Select run", None)
        numbers = self._scan_analysis_numbers()
        for num in numbers:
            self.cmbAnalysisNumber.addItem(num, num)
        if select_latest and numbers:
            # 最新番号（リスト末尾）を選択して表示待機状態にする
            self.cmbAnalysisNumber.setCurrentIndex(self.cmbAnalysisNumber.count() - 1)
        else:
            # 以前の選択を復元
            idx = self.cmbAnalysisNumber.findData(current)
            self.cmbAnalysisNumber.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmbAnalysisNumber.blockSignals(False)
        # 選択変更を手動で通知（blockSignals 中に setCurrentIndex したため）
        self._on_analysis_number_changed(self.cmbAnalysisNumber.currentIndex())
        # トグルボタンの有効/無効を更新
        has_data = self.cmbAnalysisNumber.count() > 1
        for _b in (self.chkLoadStability, self.chkLoadValley,
                   self.chkLoadWetland, self.chkLoadFlow, self.chkLoadIntegrated):
            _b.setEnabled(has_data)
        # 解析番号に紐づかない DEM キャッシュを削除
        self._gc_dem_cache()

    def _gc_dem_cache(self):
        """解析番号に紐づかない DEM キャッシュファイルを削除する。
        dem/ と vs_lp_grid/ 内のファイルのうち、いずれかの解析フォルダの
        params.json に dem_path として記録されていないものを削除する。
        現在ロード中のファイルは保持する。
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
            except Exception:
                pass

        # 現在ロード中のファイルも保持
        cur = getattr(self, "_dem_actual_path", None)
        if cur:
            referenced.add(os.path.normpath(cur))

        # dem/ と vs_lp_grid/ 内の未参照ファイルを削除
        for sub in ("dem", "vs_lp_grid"):
            sub_dir = os.path.join(out_dir, sub)
            if not os.path.isdir(sub_dir):
                continue
            for fname in os.listdir(sub_dir):
                fpath = os.path.normpath(os.path.join(sub_dir, fname))
                if os.path.isfile(fpath) and fpath not in referenced:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass

    _GROUP_PROP = "fop_managed"  # プラグイン管理グループ識別用プロパティ

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
        group = root.insertGroup(0, f"Run {analysis_number}")
        group.setExpanded(False)
        group.setCustomProperty(self._GROUP_PROP, "1")
        self._terrain_layer_group = group
        return group

    def _unload_terrain_group(self):
        """プラグイン管理グループをカスタムプロパティで検索して削除する。
        Python参照（_terrain_layer_group）が stale でも確実に除去できる。"""
        import sip
        layer_ids = []
        try:
            root = QgsProject.instance().layerTreeRoot()
            if not sip.isdeleted(root):
                for child in list(root.children()):
                    if sip.isdeleted(child):
                        continue
                    if (hasattr(child, "customProperty") and
                            child.customProperty(self._GROUP_PROP) == "1"):
                        try:
                            layer_ids += [
                                n.layerId() for n in child.findLayers()
                            ]
                            root.removeChildNode(child)
                        except Exception:
                            pass
        except Exception:
            pass
        self._terrain_layer_group = None
        proj = QgsProject.instance()
        for lid in layer_ids:
            try:
                if proj.mapLayer(lid):
                    proj.removeMapLayer(lid)
            except Exception:
                pass
        self._loaded_terrain_layers = {}
        self._flow_buffer_layer_ids = []
        self._reset_load_buttons()
        try:
            self._refresh_preview_canvas()
        except Exception:
            pass
        if self.iface is not None:
            try:
                self.iface.mapCanvas().refresh()
            except Exception:
                pass

    def _on_project_cleared(self):
        """プロジェクトクリア時: QGISが既にレイヤを削除済みなので参照だけリセット"""
        self._terrain_layer_group = None
        self._loaded_terrain_layers = {}
        self._reset_load_buttons()
        try:
            self._refresh_preview_canvas()
        except Exception:
            pass

    def _on_analysis_number_changed(self, _index):
        """解析番号コンボ変更時: 表示中レイヤをすべて解除してボタンをリセット"""
        self._unload_terrain_group()
        analysis_number = self.cmbAnalysisNumber.currentData()
        if analysis_number:
            self._create_terrain_group(analysis_number)
        self._update_analysis_condition_label(analysis_number)
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
        if "flow" in analyses:
            parts.append(
                f"Flow: {p.get('duration_h','-')}h · "
                f"{p.get('rainfall_mmh','-')}mm/h · "
                f"total {p.get('total_mm','-')}mm"
            )
        if "stability" in analyses:
            parts.append(
                f"Stability: φ{p.get('phi_deg','-')}° · "
                f"C={p.get('c_kpa','-')}kPa · "
                f"z={p.get('z_m','-')}m · "
                f"m={p.get('m_sat','-')}"
            )
        if "valley" in analyses:
            parts.append(
                f"Valley: TWI≥{p.get('twi_thresh','-')} · "
                f"min area {p.get('min_area','-')} m²"
            )
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
        self.btnBrowseDem.setText("Clear")

    def _on_browse_dsm(self):
        if self._dsm_loading:
            self._dsm_load_cancel = True
            self.btnBrowseDsm.setEnabled(False)
            self.lblDsmInfo.setText("Cancelling…")
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
                DemBrowserDialog.VS_LP_GROUND_SENTINEL: ("VS LP/Ground → DSM (0.5m)", "auto-fetch from S3 (newest year, all areas)"),
            }
            if path in _orig_labels:
                txt, tip = _orig_labels[path]
                self._dsm_path = path
                self.txtDsmPath.setText(txt)
                self.txtDsmPath.setToolTip(
                    f"Virtual Shizuoka LP/Original → DSM — {tip}"
                )
                self.btnBrowseDsm.setText("Clear")
                self._load_vs_lp_ground()
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
        """DSM/DTM設定時は流出係数・流速係数をCSから自動計算するためグレーアウト。"""
        has_dsm = bool(self._dsm_path)
        for w in (self.spinRunoff, self.spinVelocityCoef):
            w.setEnabled(not has_dsm)
            w.setToolTip(
                "Auto-calculated from canopy height when DSM/DTM is set" if has_dsm
                else w.toolTip()
            )

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
          DEM+DSM両方セット   → Export on / Open off / Cancel off
          エクスポート処理中  → Export off / Open off / Cancel on
          エクスポート完了    → Export on / Open on  / Cancel off
          Open in WODMI後    → 全off（DEM/DSM変更で Export が復活）
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
        _dsm = getattr(self, "_dsm_loader", None)
        has_dem = bool(_dem and getattr(_dem, "path", None) and os.path.isfile(_dem.path))
        has_dsm = bool(_dsm and getattr(_dsm, "path", None) and os.path.isfile(_dsm.path))
        # Export は VS Shizuoka ソース専用
        is_vs_source = (self._dem_path == DemBrowserDialog.VS_LP_GRID_SENTINEL
                        and self._dsm_path == DemBrowserDialog.VS_LP_GROUND_SENTINEL)
        can_export = is_vs_source and has_dem and has_dsm
        has_export = bool(self._vs_export_dir and os.path.isfile(self._vs_export_dir))

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
            # VS DEM+DSM 揃い: Export が使える
            self.btnVsExport.setEnabled(True)
            self.btnVsExport.setToolTip(
                "Virtual Shizuoka LP/Grid (DTM) + LP/Ground (DSM) を\n"
                "Ortho・LAS とともに WODMI ZIP にエクスポートします。"
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

    def _on_vs_export(self):
        """設定済みDTM/DSMを使い、LP/Ortho を S3 から取得して WODMI ZIP に書き出す。"""
        from .vs_lp import (resolve_years, tiles_for_extent,
                            download_grid_tif, merge_tifs)
        from qgis.core import (QgsCoordinateReferenceSystem,
                               QgsCoordinateTransform, QgsProject)
        import zipfile as _zf
        import datetime as _dt

        # ── DTM チェック ─────────────────────────────────────────────
        dtm_loader = getattr(self, "_terrain_loader", None)
        if dtm_loader is None or not dtm_loader.path or not os.path.isfile(dtm_loader.path):
            QtWidgets.QMessageBox.warning(self, "VS Export",
                                          "DEMデータが設定されていません。先にDEMを読み込んでください。")
            return

        dsm_loader = getattr(self, "_dsm_loader", None)
        has_dsm = (dsm_loader is not None
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

        self.lblVsExportStatus.setText("Checking Ortho…")
        QtWidgets.QApplication.processEvents()
        resolved_ortho = resolve_years(codes, lp_type="Ortho") if codes else {}
        has_ortho = bool(resolved_ortho)

        msg = (f"• DTM: {os.path.basename(dtm_loader.path)}\n"
               f"• DSM: {'設定済み (' + os.path.basename(dsm_loader.path) + ')' if has_dsm else '未設定（スキップ）'}\n"
               f"• Ortho: LP/Ortho  — {'included' if has_ortho else 'not available for this area'}\n\n"
               "Proceed?")
        if QtWidgets.QMessageBox.question(
            self, "VS Export", msg,
            QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel
        ) != QtWidgets.QMessageBox.Ok:
            self.lblVsExportStatus.setText("—")
            return

        dtm_path = dtm_loader.path
        dsm_path = dsm_loader.path if has_dsm else None

        # ── 作業ディレクトリ（Ortho用）───────────────────────────────
        tmp_dir = os.path.join(self._terrain_output_dir(), ".vs_export_tmp")
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        os.makedirs(tmp_dir)

        # ── エクスポート開始 ──────────────────────────────────────────
        self._vs_export_cancel = False
        self._vs_exporting = True
        self._update_vs_export_buttons()

        zip_path = None
        success = False
        try:
            # ── Ortho (LP/Ortho) ─────────────────────────────────────
            ortho_path = None
            if has_ortho:
                ortho_tifs = []
                for i, (code, year) in enumerate(resolved_ortho.items()):
                    if self._vs_export_cancel:
                        self.lblVsExportStatus.setText("Cancelled")
                        return
                    self.lblVsExportStatus.setText(f"Ortho {i + 1}/{len(resolved_ortho)}…")
                    QtWidgets.QApplication.processEvents()
                    try:
                        ortho_tifs.append(download_grid_tif(code, year, tmp_dir, "Ortho"))
                    except Exception:
                        pass
                if ortho_tifs:
                    self.lblVsExportStatus.setText("Merging Ortho…")
                    QtWidgets.QApplication.processEvents()
                    ortho_path = os.path.join(tmp_dir, "odm_orthophoto.tif")
                    if len(ortho_tifs) == 1:
                        shutil.copy2(ortho_tifs[0], ortho_path)
                    else:
                        merge_tifs(ortho_tifs, ortho_path)

            # ── ZIP 作成 ──────────────────────────────────────────────
            zip_dir = os.path.join(self._terrain_output_dir(), "zip")
            os.makedirs(zip_dir, exist_ok=True)
            today = _dt.datetime.now().strftime("%Y%m%d")
            prefix = f"FOL_{today}"
            existing = [f for f in os.listdir(zip_dir)
                        if f.startswith(prefix) and f.endswith("-all.zip")]
            seq = len(existing) + 1
            zip_name = f"{prefix}-all.zip" if seq == 1 else f"{prefix}_{seq}-all.zip"
            zip_path = os.path.join(zip_dir, zip_name)

            self.lblVsExportStatus.setText("Creating ZIP…")
            QtWidgets.QApplication.processEvents()
            las_paths = [p for p in getattr(self, "_vs_las_paths", []) if os.path.isfile(p)]
            try:
                with _zf.ZipFile(zip_path, "w", _zf.ZIP_DEFLATED) as zf:
                    zf.write(dtm_path, "odm_dem/dtm.tif")
                    if dsm_path:
                        zf.write(dsm_path, "odm_dem/dsm.tif")
                    if ortho_path:
                        zf.write(ortho_path, "odm_orthophoto/odm_orthophoto.tif")
                    for i, las_path in enumerate(las_paths):
                        if self._vs_export_cancel:
                            self.lblVsExportStatus.setText("Cancelled")
                            return
                        self.lblVsExportStatus.setText(
                            f"Packing LAS {i + 1}/{len(las_paths)}…")
                        QtWidgets.QApplication.processEvents()
                        zf.write(las_path,
                                 f"odm_georeferencing/{os.path.basename(las_path)}")
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "VS Export", f"ZIP creation failed:\n{e}")
                self.lblVsExportStatus.setText("—")
                return

            success = True
            self._vs_export_dir = zip_path
            parts = ["DTM included"]
            parts.append("DSM included" if dsm_path else "DSM not available for this area")
            parts.append("Ortho included" if ortho_path else "Ortho not available for this area")
            QtWidgets.QMessageBox.information(
                self, "VS Export",
                f"Export complete.\n{zip_name}\n" + ", ".join(parts)
            )

        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if not success and zip_path and os.path.exists(zip_path):
                os.remove(zip_path)
            self._vs_exporting = False
            self._update_vs_export_buttons()

    def _on_vs_cancel(self):
        """エクスポート処理にキャンセルフラグを立てる。"""
        self._vs_export_cancel = True
        self.btnVsCancel.setEnabled(False)
        self.lblVsExportStatus.setText("Cancelling…")

    def _on_open_wodmi(self):
        """エクスポート ZIP を展開して webodm_importer パネルを開く。"""
        import zipfile as _zf
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

    def _run_terrain_analysis(self):
        if not any([
            self.chkStability.isChecked(),
            self.chkValley.isChecked(),
            self.chkFlow.isChecked(),
        ]):
            self.lblAnalysisStatus.setText("Select at least one analysis type.")
            return

        # ── 解析面積チェック（プレビューキャンバス範囲） ──
        _limit_ha = self.cmbAreaLimit.currentData()
        if _limit_ha and _limit_ha > 0:
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
            except Exception:
                pass

        # タイルソースの場合は解析ごとに現在のキャンバス範囲で再取得・変換
        _TILE_SENTINELS = (
            DemBrowserDialog.GSI_DEM1A_SENTINEL,
            DemBrowserDialog.GSI_DEM5A_SENTINEL,
            DemBrowserDialog.GSI_DEM10B_SENTINEL,
            DemBrowserDialog.TERRARIUM_FINE_SENTINEL,
            DemBrowserDialog.TERRARIUM_STANDARD_SENTINEL,
            DemBrowserDialog.TERRARIUM_WIDE_SENTINEL,
            DemBrowserDialog.VS_LP_GRID_SENTINEL,
        )
        # ── 解析範囲外チェック（タイルソースのDEMは自動再取得されるためスキップ）──
        _dem_path_cur = getattr(self, "_dem_path", "")
        _check_dem = (_dem_path_cur not in _TILE_SENTINELS
                      and getattr(self, "_terrain_loader", None) is not None)
        _check_dsm = getattr(self, "_dsm_loader", None) is not None
        _outside = ((_check_dem and self._canvas_outside_loader(self._terrain_loader))
                    or (_check_dsm and self._canvas_outside_loader(self._dsm_loader)))
        if _outside:
            reply = QtWidgets.QMessageBox.question(
                self, "解析範囲の確認",
                "解析範囲にDEM,DSM/DTM設定外が含まれます。\n再取得しますか？",
                QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Ok:
                return
            # 再取得（VS LP/Ground DSM のみ可能）
            if self._dsm_path == DemBrowserDialog.VS_LP_GROUND_SENTINEL:
                self._load_vs_lp_ground()

        if _dem_path_cur in _TILE_SENTINELS:
            self._terrain_loader = None  # 古いデータをクリアしてから再取得
            self.lblAnalysisStatus.setText("Fetching elevation tiles...")
            QtWidgets.QApplication.processEvents()
            if self._dem_path == DemBrowserDialog.VS_LP_GRID_SENTINEL:
                self._load_vs_lp_grid(auto_dsm=False)
            else:
                self._load_gsi_dem(self._dem_path)

        loader = getattr(self, "_terrain_loader", None)
        if loader is None:
            self._load_dem_info()
            loader = getattr(self, "_terrain_loader", None)
        if loader is None:
            self.lblAnalysisStatus.setText("Specify a DEM file.")
            return

        out_dir = self._terrain_output_dir()

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
            dem = loader.clip_to_extent(ext.xMinimum(), ext.yMinimum(),
                                        ext.xMaximum(), ext.yMaximum())
        except Exception as e:
            self.lblAnalysisStatus.setText(f"Clip failed: {e}")
            return

        # 解析シーケンス番号を決定（ファイル保存前に確定）
        seq = self._next_seq(self.chkOverwrite.isChecked())
        # 隠し一時フォルダ（解析完了後に1回だけリネーム → Thunar inotify を最小化）
        import shutil as _shutil
        tmp_folder = os.path.join(out_dir, f".tmp_{seq}")
        if os.path.exists(tmp_folder):      # 中断残骸をクリア
            _shutil.rmtree(tmp_folder)
        os.makedirs(tmp_folder)

        self._cancel_analysis = False
        self.btnRunAnalysis.setEnabled(False)
        self.btnStopAnalysis.setEnabled(True)
        self.lblAnalysisStatus.setVisible(False)
        self.progressAnalysis.setRange(0, 100)
        self.progressAnalysis.setValue(5)
        self.progressAnalysis.setVisible(True)
        QtWidgets.QApplication.processEvents()

        def _pe():
            QtWidgets.QApplication.processEvents()
            if self._cancel_analysis:
                raise _StopAnalysis()

        saved = []
        try:
            from .terrain import analysis as ta
            from .terrain import result_writer as rw
            import numpy as np

            slope = ta.compute_slope_deg(dem.data, dem.cell_size)
            self.progressAnalysis.setValue(15)
            _pe()

            if self.chkStability.isChecked() or self.chkValley.isChecked() \
                    or self.chkFlow.isChecked():
                fdir = ta.d8_flow_direction(dem.data)
                self.progressAnalysis.setValue(25)
                _pe()
                accum = ta.flow_accumulation(dem.data, fdir)
                self.progressAnalysis.setValue(45)
                _pe()

            # ④ 斜面安定解析
            if self.chkStability.isChecked():
                fs = ta.stability_fs(
                    slope,
                    phi_deg=self.spinPhiDeg.value(),
                    c_kpa=self.spinCKpa.value(),
                    z_m=self.spinZm.value(),
                    m=self.spinMSat.value(),
                )
                p = rw.save_raster(fs, dem.gt, dem.crs_wkt, tmp_folder,
                                   "stability_fs", overwrite=True)
                saved.append(("Slope Stability FS", p, "raster"))
                mask = (fs < self.spinFsThresh.value()) & ~np.isnan(fs)
                if mask.any():
                    p2 = rw.mask_to_polygons(mask, dem.gt, dem.crs_wkt,
                                             tmp_folder, "unstable_zones",
                                             overwrite=True)
                    saved.append(("Unstable zones", p2, "vector"))
                self.progressAnalysis.setValue(60)
                _pe()

            # ① 沢地形判定
            if self.chkValley.isChecked():
                twi = ta.compute_twi(accum, slope, dem.cell_size)
                p = rw.save_raster(twi, dem.gt, dem.crs_wkt, tmp_folder,
                                   "twi", overwrite=True)
                saved.append(("TWI", p, "raster"))
                min_cells = self.spinMinArea.value() / (dem.cell_size ** 2)
                vmask = (twi >= self.spinTwiThresh.value()) \
                        & (accum >= min_cells) & ~np.isnan(twi)
                if vmask.any():
                    p2 = rw.mask_to_polygons(vmask, dem.gt, dem.crs_wkt,
                                             tmp_folder, "valley_zones",
                                             overwrite=True)
                    saved.append(("Valley Terrain", p2, "vector"))
                self.progressAnalysis.setValue(70)
                _pe()

            # ③ 流量推測（修正合理式 + 到達時間 Tc ルーティング）
            if self.chkFlow.isChecked():
                # DSM/DTM 設定時は CS（樹冠高さ）から係数を空間化
                dsm_loader = getattr(self, "_dsm_loader", None)
                # ピクセルデータが未ロードなら遅延読み込み
                if dsm_loader is not None and dsm_loader.data is None:
                    dsm_loader.read_data()
                if dsm_loader is not None and dsm_loader.data is not None \
                        and dsm_loader.data.shape == dem.data.shape:
                    cs = dsm_loader.data - dem.data
                    c_local, velocity_coef = ta.cs_to_flow_coefficients(cs)
                    # C は上流域の面積加重平均を使用（修正合理式の理論的要件）
                    c_accum = ta.flow_accumulation(dem.data, fdir, weight=c_local)
                    runoff_coef = c_accum / np.maximum(accum, 1.0)
                    self.progressAnalysis.setValue(65)
                    _pe()
                else:
                    runoff_coef = self.spinRunoff.value()
                    velocity_coef = self.spinVelocityCoef.value()
                self.progressAnalysis.setValue(75)
                _pe()
                local_tt = ta.compute_travel_time(
                    dem.data, fdir, dem.cell_size,
                    velocity_coef=velocity_coef,
                )
                tc = ta.compute_tc(dem.data, fdir, local_tt)
                self.progressAnalysis.setValue(85)
                _pe()
                Q_peak, Q_mean, V_total = ta.flow_routing_3metrics(
                    accum, tc, dem.cell_size,
                    duration_h=self.spinDuration.value(),
                    i_peak_mmh=self.spinRainfall.value(),
                    runoff_coef=runoff_coef,
                    total_mm=self.spinTotalRainfall.value(),
                )
                p0 = rw.save_raster(tc, dem.gt, dem.crs_wkt, tmp_folder,
                                    "tc", overwrite=True)
                saved.append(("Tc[h]", p0, "raster"))
                p1 = rw.save_raster(Q_peak, dem.gt, dem.crs_wkt, tmp_folder,
                                    "flow_peak", overwrite=True)
                saved.append(("Qp[m³/s]", p1, "raster"))
                p2 = rw.save_raster(Q_mean, dem.gt, dem.crs_wkt, tmp_folder,
                                    "flow_mean", overwrite=True)
                saved.append(("Qm[m³/s]", p2, "raster"))
                p3 = rw.save_raster(V_total, dem.gt, dem.crs_wkt, tmp_folder,
                                    "flow_vtotal", overwrite=True)
                saved.append(("V[m³]", p3, "raster"))

            # 統合リスク指標（FS/TWI/流量のいずれかがあれば自動生成）
            self.progressAnalysis.setValue(90)
            _pe()
            try:
                from .terrain import integration as ti
                int_result = ti.build_integrated_index(
                    tmp_folder, analysis_prefix="")
                if int_result["integrated_risk_index"]:
                    saved.append(("Overall Risk Index", int_result["integrated_risk_index"], "raster"))
                if int_result["integrated_high_risk"]:
                    saved.append(("High Risk Areas", int_result["integrated_high_risk"], "vector"))
            except FileNotFoundError:
                pass  # 対象ラスタなし（単独の湧水のみ解析時など）
            except _StopAnalysis:
                raise
            except Exception as e_int:
                self.lblAnalysisStatus.setText(f"Integrated risk error: {e_int}")

        except _StopAnalysis:
            self.progressAnalysis.setVisible(False)
            self.progressAnalysis.setRange(0, 0)
            self.lblAnalysisStatus.setVisible(True)
            self.lblAnalysisStatus.setText("Analysis cancelled.")
            self.btnRunAnalysis.setEnabled(True)
            self.btnStopAnalysis.setEnabled(False)
            import shutil as _shutil
            if os.path.exists(tmp_folder):
                _shutil.rmtree(tmp_folder)
            return
        except Exception as e:
            self.progressAnalysis.setVisible(False)
            self.progressAnalysis.setRange(0, 0)
            self.lblAnalysisStatus.setVisible(True)
            self.lblAnalysisStatus.setText(f"Analysis error: {e}")
            self.btnRunAnalysis.setEnabled(True)
            self.btnStopAnalysis.setEnabled(False)
            return
        finally:
            # DSM ピクセルデータを解放（メタデータ・GDALハンドルは保持）
            _dsm = getattr(self, "_dsm_loader", None)
            if _dsm is not None:
                _dsm.data = None

        # ── ファイル数が確定したので解析番号を決定し tmp_folder を1回だけリネーム ──
        N = len(saved)
        analysis_number = self._format_analysis_number(seq, N)
        folder = os.path.join(out_dir, analysis_number)

        # 解析条件を params.json に保存（リネーム前に tmp_folder へ書き込む）
        import json as _json
        _params = {
            "dem_path": getattr(self, "_dem_actual_path", None) or getattr(self, "_dem_path", ""),
            "analyses": [
                k for k, chk in [("stability", self.chkStability),
                                  ("valley",    self.chkValley),
                                  ("flow",      self.chkFlow)]
                if chk.isChecked()
            ],
        }
        if self.chkFlow.isChecked():
            _params.update({
                "duration_h":    self.spinDuration.value(),
                "rainfall_mmh":  self.spinRainfall.value(),
                "total_mm":      self.spinTotalRainfall.value(),
                "runoff":        self.spinRunoff.value(),
                "velocity_coef": self.spinVelocityCoef.value(),
            })
        if self.chkStability.isChecked():
            _params.update({
                "phi_deg":   self.spinPhiDeg.value(),
                "c_kpa":     self.spinCKpa.value(),
                "z_m":       self.spinZm.value(),
                "m_sat":     self.spinMSat.value(),
                "fs_thresh": self.spinFsThresh.value(),
            })
        if self.chkValley.isChecked():
            _params.update({
                "twi_thresh": self.spinTwiThresh.value(),
                "min_area":   self.spinMinArea.value(),
            })
        try:
            with open(os.path.join(tmp_folder, "params.json"), "w", encoding="utf-8") as _f:
                _json.dump(_params, _f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        # tmp_folder を最終フォルダ名に一括リネーム（inotify イベントを1回に集約）
        if os.path.exists(folder):
            _shutil.rmtree(folder)
        os.rename(tmp_folder, folder)
        # saved のパスを新フォルダに更新
        saved = [(label, os.path.join(folder, os.path.basename(p)), kind)
                 for label, p, kind in saved]

        # 解析番号コンボを更新して新番号を選択（解析完了後は最新が当該番号なのでそのまま）
        self._refresh_analysis_combo(select_latest=False)
        idx = self.cmbAnalysisNumber.findData(analysis_number)
        if idx >= 0:
            self.cmbAnalysisNumber.blockSignals(True)
            self.cmbAnalysisNumber.setCurrentIndex(idx)
            self.cmbAnalysisNumber.blockSignals(False)

        self._update_analysis_condition_label(analysis_number)
        names = ", ".join(n for n, _, _ in saved)
        self.progressAnalysis.setValue(100)
        self.progressAnalysis.setVisible(False)
        self.progressAnalysis.setRange(0, 0)
        self.lblAnalysisStatus.setVisible(True)
        self.lblAnalysisStatus.setText(
            f"Done [{analysis_number}]: {names}"
        )
        if self.iface is not None:
            self.iface.mapCanvas().refresh()
        self.btnRunAnalysis.setEnabled(True)
        self.btnStopAnalysis.setEnabled(False)

    def _on_stop_analysis(self):
        self._cancel_analysis = True

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
        s.setValue("dem_path", self._dem_path)
        s.setValue("dsm_path", self._dsm_path)
        s.setValue("flow_buffer_state", self._flow_buffer_state)

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
        s.setValue("chk_overwrite",       self.chkOverwrite.isChecked())
        s.setValue("chk_stability",       self.chkStability.isChecked())
        s.setValue("chk_valley",          self.chkValley.isChecked())
        s.setValue("chk_flow",            self.chkFlow.isChecked())
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
        if dsm_path:
            self._dsm_path = dsm_path
            self.txtDsmPath.setText(os.path.basename(dsm_path))
            self.txtDsmPath.setToolTip(dsm_path)
            self._load_dsm_info()
        _fb = s.value("flow_buffer_state", "off")
        if _fb in ("off", "weak", "strong"):
            self._flow_buffer_state = _fb
            _fb_labels = {
                "off":    "Buffer: Off",
                "weak":   "Buffer: Low",
                "strong": "Buffer: High",
            }
            self.btnFlowBuffer.setText(_fb_labels[_fb])
            self.btnFlowBuffer.setStyleSheet(
                ("font-size:8pt; padding:1px 2px;"
                 "background:#e07050; color:white; border-radius:2px;")
                if _fb != "off" else
                "font-size:8pt; padding:1px 2px;"
            )

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
        self.chkOverwrite.setChecked(    b("chk_overwrite",   True))
        self.chkStability.setChecked(    b("chk_stability",   True))
        self.chkValley.setChecked(       b("chk_valley",      True))
        self.chkFlow.setChecked(         b("chk_flow",        False))
        self.cmbAreaLimit.setCurrentIndex(i("area_limit_idx", 1))

        # ── 解析パラメータ ──
        self.spinPhiDeg.setValue(    f("spin_phi_deg",     35.0))
        self.spinCKpa.setValue(      f("spin_c_kpa",        0.0))
        self.spinZm.setValue(        f("spin_zm",           1.0))
        self.spinMSat.setValue(      f("spin_m_sat",        0.5))
        self.spinFsThresh.setValue(  f("spin_fs_thresh",    1.5))
        self.spinTwiThresh.setValue( f("spin_twi_thresh",   8.0))
        self.spinMinArea.setValue(   f("spin_min_area",  1000.0))
        self.spinRainfall.setValue(       f("spin_rainfall",        50.0))
        self.spinRunoff.setValue(         f("spin_runoff",           0.8))
        self.spinTotalRainfall.setValue(  f("spin_total_rainfall",   100.0))
        self.spinDuration.setValue(       f("spin_duration",          6.0))
        self.spinVelocityCoef.setValue(   f("spin_velocity_coef",     0.3))

        s.endGroup()

    def closeEvent(self, event):
        self._save_settings()
        self._save_layer_settings_to_project()  # レイヤー設定をプロジェクトにも保存
        self._unload_terrain_group()
        try:
            QgsProject.instance().readProject.disconnect(self._on_project_read)
        except Exception:
            pass
        try:
            QgsProject.instance().projectSaved.disconnect(self._update_out_dir_label)
        except Exception:
            pass
        try:
            QgsProject.instance().projectSaved.disconnect(self._save_layer_settings_to_project)
        except Exception:
            pass
        try:
            QgsProject.instance().cleared.disconnect(self._on_project_cleared)
        except Exception:
            pass
        if self.iface is not None:
            try:
                self.iface.mapCanvas().extentsChanged.disconnect(self._on_main_canvas_changed)
            except Exception:
                pass
        self.closingPlugin.emit()
        event.accept()
