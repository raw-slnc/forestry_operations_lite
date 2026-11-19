"""Window close confirmation regressions."""

from types import SimpleNamespace
import unittest
from unittest import mock

from forestry_operations_lite import forestry_operations_lite as plugin_ui
from forestry_operations_lite import forestry_operations_lite_dockwidget as ui


class CloseConfirmationTests(unittest.TestCase):

    def test_idle_window_closes_without_a_dialog(self):
        dummy = SimpleNamespace(
            _analysis_worker=None,
            _analysis_ctx=None,
            _dem_worker=None,
            btnStopAnalysis=mock.Mock(),
        )
        with mock.patch.object(ui.QtWidgets.QMessageBox, "question") as question:
            allowed = ui.ForestryOperationsLiteDockWidget._confirm_close_during_analysis(
                dummy)

        self.assertTrue(allowed)
        question.assert_not_called()

    def test_no_keeps_the_window_and_analysis_open(self):
        no = ui.QtWidgets.QMessageBox.StandardButton.No
        dummy = SimpleNamespace(
            _analysis_worker=object(),
            _analysis_ctx={"is_overwrite": True},
            _dem_worker=None,
            btnStopAnalysis=mock.Mock(),
            _host_window=None,
        )
        with mock.patch.object(
                ui.QtWidgets.QMessageBox, "question", return_value=no) as question:
            allowed = ui.ForestryOperationsLiteDockWidget._confirm_close_during_analysis(
                dummy)

        self.assertFalse(allowed)
        self.assertIn("cannot be restored", question.call_args.args[2])
        self.assertEqual(question.call_args.args[4], no)

    def test_yes_allows_close_and_cancel(self):
        yes = ui.QtWidgets.QMessageBox.StandardButton.Yes
        dummy = SimpleNamespace(
            _analysis_worker=object(),
            _analysis_ctx={"is_overwrite": False},
            _dem_worker=None,
            btnStopAnalysis=mock.Mock(),
            _host_window=None,
        )
        with mock.patch.object(
                ui.QtWidgets.QMessageBox, "question", return_value=yes):
            allowed = ui.ForestryOperationsLiteDockWidget._confirm_close_during_analysis(
                dummy)

        self.assertTrue(allowed)

    def test_analysis_dem_refetch_also_requires_confirmation(self):
        no = ui.QtWidgets.QMessageBox.StandardButton.No
        stop_button = mock.Mock()
        stop_button.isEnabled.return_value = True
        dummy = SimpleNamespace(
            _analysis_worker=None,
            _analysis_ctx=None,
            _dem_worker=object(),
            btnStopAnalysis=stop_button,
            _host_window=None,
        )
        with mock.patch.object(
                ui.QtWidgets.QMessageBox, "question", return_value=no) as question:
            allowed = ui.ForestryOperationsLiteDockWidget._confirm_close_during_analysis(
                dummy)

        self.assertFalse(allowed)
        question.assert_called_once()

    def test_rejected_close_does_not_start_shutdown(self):
        dummy = SimpleNamespace(
            _confirm_close_during_analysis=mock.Mock(return_value=False),
            shutdown_analysis=mock.Mock(),
        )
        event = mock.Mock()

        ui.ForestryOperationsLiteDockWidget.closeEvent(dummy, event)

        event.ignore.assert_called_once_with()
        dummy.shutdown_analysis.assert_not_called()

    def test_host_window_respects_rejected_content_close(self):
        event = mock.Mock()
        event.isAccepted.return_value = False
        content = mock.Mock()
        dummy = SimpleNamespace(content=content)

        ui.ForestryOperationsLiteWindow.closeEvent(dummy, event)

        content.closeEvent.assert_called_once_with(event)


class ToolbarActivationTests(unittest.TestCase):

    def test_first_click_still_initializes_the_window(self):
        dockwidget = mock.Mock()
        on_close = mock.Mock()
        dummy = SimpleNamespace(
            plugin_is_active=False,
            dockwidget=dockwidget,
            on_close_plugin=on_close,
        )

        plugin_ui.ForestryOperationsLite.run(dummy)

        self.assertTrue(dummy.plugin_is_active)
        dockwidget.closingPlugin.connect.assert_called_once_with(on_close)
        dockwidget.initialize_window_mode.assert_called_once_with()
        dockwidget._show_as_window.assert_not_called()

    def test_reclick_brings_existing_window_forward_without_reinitializing(self):
        dockwidget = mock.Mock()
        dummy = SimpleNamespace(
            plugin_is_active=True,
            dockwidget=dockwidget,
        )

        plugin_ui.ForestryOperationsLite.run(dummy)

        dockwidget._show_as_window.assert_called_once_with()
        dockwidget.initialize_window_mode.assert_not_called()

    def test_minimized_maximized_window_keeps_maximized_state(self):
        minimized = ui.Qt.WindowState.WindowMinimized
        maximized = ui.Qt.WindowState.WindowMaximized
        host = mock.Mock()
        host.isMinimized.return_value = True
        host.windowState.return_value = minimized | maximized
        dummy = SimpleNamespace(_host_window=host)

        ui.ForestryOperationsLiteDockWidget._show_as_window(dummy)

        restored = host.setWindowState.call_args.args[0]
        self.assertFalse(bool(restored & minimized))
        self.assertTrue(bool(restored & maximized))
        host.show.assert_called_once_with()
        host.raise_.assert_called_once_with()
        host.activateWindow.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
