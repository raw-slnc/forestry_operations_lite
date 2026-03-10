"""
project_state.py — プロジェクト設定の永続化・切り替え

役割:
  - プラグインで使用したファイルパス、レイヤー選択、解析設定を QgsProject / QSettings に保存
  - 起動時に前回の設定を復元
  - 複数プロジェクト（設定セット）の保存と切り替え

TODO: 実装予定
"""

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QSettings


class ProjectState:
    """プラグインの設定永続化を担当するクラス。"""

    SETTINGS_KEY = "ForestryOperationsPlanner"

    def __init__(self):
        self._settings = QSettings()

    # ── QSettings ベースの簡易保存 ──────────────────────────────

    def save(self, key: str, value):
        self._settings.setValue(f"{self.SETTINGS_KEY}/{key}", value)

    def load(self, key: str, default=None):
        return self._settings.value(f"{self.SETTINGS_KEY}/{key}", default)

    # ── QgsProject カスタムプロパティ ────────────────────────────
    # QgsProject に保存することで .qgz ファイルに設定が付随する

    def save_to_project(self, key: str, value: str):
        QgsProject.instance().writeEntry(self.SETTINGS_KEY, key, value)

    def load_from_project(self, key: str, default: str = "") -> str:
        val, ok = QgsProject.instance().readEntry(self.SETTINGS_KEY, key, default)
        return val if ok else default
