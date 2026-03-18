# Forestry Operations Lite

A QGIS plugin for terrain analysis supporting forestry site assessment.

![UI Panel](forestry_operations_lite_UI_panel.png)

---

## Features

- Load DEM / DSM from local files or tile services (GSI elevation tiles for Japan; AWS Terrarium for global coverage)
- Compute **slope**, **TWI** (Topographic Wetness Index), **stability index** (infinite-slope factor of safety), and **flow accumulation**
- Preview canvas with bidirectional sync to the QGIS main map window
- Layer settings (background / tile / GPKG) displayed in the preview independent of analysis data
- Map lock: fix the preview to the analysis extent while continuing to navigate the main window freely
- Analysis results are grouped and managed by run number in the QGIS layer panel
- Preview status bar shows centre coordinates, scale, area (ha), and CRS

![QGIS Main Window](forestry_operations_lite_QGIS_window.png)

---

## Global Coverage (v0.1.3)

The plugin supports worldwide terrain analysis via **AWS Terrain Tiles (Mapzen Terrarium)** — free, no registration required.

| Source | Coverage | Resolution | Notes |
|--------|----------|------------|-------|
| GSI DEM1A | Japan | ~1 m | Auto-fetched from canvas extent |
| GSI DEM5A | Japan | ~5 m | Auto-fetched from canvas extent |
| GSI DEM10B | Japan | ~10 m | Auto-fetched from canvas extent |
| AWS Terrarium | Worldwide | ~2–10 m eq. | Auto-fetched from canvas extent |
| Local file | Any | As-is | GeoTIFF, ZIP, or folder |
| Copernicus GLO-30 | Worldwide | 30 m | Free account required (OpenTopography) |

### CRS recommendations for global use

- Set the project CRS to the **UTM zone** covering your analysis area for best accuracy.
- UTM is valid up to ±84° latitude. Accuracy degrades above ±70° (Arctic/Antarctic regions are outside the intended use range).
- The plugin automatically corrects cell size for geographic CRS (EPSG:4326) and Web Mercator (EPSG:3857) inputs.

**UTM zone examples:**

| Region | Recommended CRS |
|--------|----------------|
| Japan (126–132°E) | EPSG:32653 |
| Japan (132–138°E) | EPSG:32654 |
| Japan (138–144°E) | EPSG:32655 |
| Peru / Bolivia (66–72°W) | EPSG:32719 |
| Peru / Bolivia (72–78°W) | EPSG:32718 |
| Southeast Asia | UTM zone for longitude |

---

## Requirements

- QGIS 3.16 or later
- Python 3.7+
- numpy, GDAL, scipy (bundled with QGIS)

---

## Installation

1. Download the ZIP from [Releases](https://github.com/raw-slnc/forestry_operations_lite/releases)
2. In QGIS: **Plugins > Manage and Install Plugins > Install from ZIP**
3. The plugin appears in the **Raster toolbar** and **Raster menu**

---

## Usage

1. Click the **FOL** icon in the Raster toolbar to open the plugin window
2. Select a DEM/DSM source under **Data Setup**
3. Set background / tile / GPKG layers under **Layer Settings**
4. Run terrain analysis — results are added to the QGIS layer panel grouped by run number
5. Toggle analysis layers on/off using the buttons in the preview panel

---

## License

This project is licensed under the GNU General Public License v2 or later.

---

## Support

If you find this plugin useful, your support is appreciated.
https://paypal.me/rawslnc

---

---

# Forestry Operations Lite（日本語）

林業サイトの地形解析を支援するQGISプラグインです。

---

## 機能

- ローカルファイルまたはタイルサービス（国土地理院標高タイル・AWS Terrarium全球対応）からDEM / DSMを読み込み
- **斜度**・**TWI**（地形湿潤指数）・**斜面安定性指数**（無限斜面安全率）・**流量推測**を計算
- QGISメインマップとの双方向同期プレビューキャンバス
- 解析データの有無に関わらず、レイヤー設定（背景・タイル・GPKG）をプレビューに表示
- 地図ロック：解析範囲にプレビューを固定しながら、メインウィンドウは自由に操作可能
- 解析結果はQGISレイヤーパネルに解析番号グループで管理
- プレビューステータスバーに中心座標・縮尺・面積（ha）・CRSを表示

---

## 全球対応（v0.1.3）

**AWS Terrain Tiles（Mapzen Terrarium）** により全球の地形解析に対応しました。登録不要・無料で使用できます。

| ソース | カバレッジ | 解像度 | 備考 |
|--------|-----------|--------|------|
| 国土地理院 DEM1A | 日本 | 約1 m | キャンバス範囲から自動取得 |
| 国土地理院 DEM5A | 日本 | 約5 m | キャンバス範囲から自動取得 |
| 国土地理院 DEM10B | 日本 | 約10 m | キャンバス範囲から自動取得 |
| AWS Terrarium | 全球 | 約2〜10 m相当 | キャンバス範囲から自動取得 |
| ローカルファイル | 任意 | 元データ準拠 | GeoTIFF・ZIP・フォルダ |
| Copernicus GLO-30 | 全球 | 30 m | 無料アカウント必要（OpenTopography） |

### 全球使用時のCRS推奨

- 解析精度を高めるため、プロジェクトCRSを対象地域の**UTMゾーン**に設定してください。
- UTMは緯度±84°まで定義されています。±70°を超える高緯度（北極・南極圏）は想定使用範囲外です。
- 地理座標系（EPSG:4326）・Web Mercator（EPSG:3857）のDEMを使用する場合、セルサイズは自動補正されます。

**UTMゾーン例：**

| 地域 | 推奨CRS |
|------|---------|
| 日本（東経126〜132°） | EPSG:32653 |
| 日本（東経132〜138°） | EPSG:32654 |
| 日本（東経138〜144°） | EPSG:32655 |
| ペルー・ボリビア（西経66〜72°） | EPSG:32719 |
| ペルー・ボリビア（西経72〜78°） | EPSG:32718 |
| 東南アジア | 経度に対応するUTMゾーン |

---

## 動作環境

- QGIS 3.16 以降
- Python 3.7+
- numpy、GDAL、scipy（QGIS同梱）

---

## インストール

1. [Releases](https://github.com/raw-slnc/forestry_operations_lite/releases) からZIPをダウンロード
2. QGISで **プラグイン > プラグインの管理とインストール > ZIPからインストール**
3. **ラスターツールバー**および**ラスターメニュー**にプラグインが追加されます

---

## 使い方

1. ラスターツールバーの **FOL** アイコンをクリックしてプラグインウィンドウを開く
2. **Data Setup** でDEM/DSMソースを選択
3. **レイヤー設定** で背景・タイル・GPKGレイヤーを設定
4. 地形解析を実行 — 解析結果は解析番号グループとしてQGISレイヤーパネルに追加
5. プレビューパネルのボタンで解析レイヤーの表示/非表示を切替

---

## ライセンス

GNU General Public License v2 以降

---

## サポート

開発を応援していただけると嬉しいです。
https://paypal.me/rawslnc
