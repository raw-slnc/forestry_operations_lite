# terrain/ — 地形解析モジュール
#
# 担当する機能:
#   - DEM/DSM 読み込みと差分による植生判断 (dem_loader.py)
#   - 傾斜・流向・TWI・斜面安定性・合理式流量 (analysis.py)
#   - 解析結果の出力 (result_writer.py)
#   - QGIS レイヤー統合 (integration.py)
#
# UIコントローラ: forestry_operations_lite_dockwidget.py 内の
#                 _build_terrain_tab() および _run_terrain_analysis() など
#                 → 将来的に ui/terrain_tab.py として分離する
