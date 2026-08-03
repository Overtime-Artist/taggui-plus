import logging
import os
import sys
import traceback
import warnings

import transformers
from PySide6.QtCore import QtMsgType, QTimer, qInstallMessageHandler
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import QApplication, QMessageBox

from utils.settings import get_settings

PNG_WARNING_TO_SUPPRESS = 'libpng warning: iCCP:'
# Exit code used to ask the managed launcher (start.py) to relaunch the app in
# the same console window. Keep this in sync with the constant in start.py.
RESTART_EXIT_CODE = 1010
QT_MESSAGES_TO_SUPPRESS = (
    "Error with Permissions-Policy header: Unrecognized feature: 'payment'.",
    "Error with Permissions-Policy header: Unrecognized feature: 'usb'.",
    'Failed to create WebGPU Context Provider',
    '%c%d font-size:0;color:transparent NaN',
    'Failed to parse audio contentType:',
    'Failed to parse video contentType:',
    'challenges.cloudflare.com/cdn-cgi/challenge-platform/'
)
_previous_qt_message_handler = None


def suppress_warnings():
    """Suppress all warnings when not in a development environment."""
    environment = os.getenv('TAGGUI_ENVIRONMENT')
    if environment == 'development':
        print('Running in development environment.')
        return
    logging.basicConfig(level=logging.ERROR)
    warnings.simplefilter('ignore')
    transformers.logging.set_verbosity_error()
    try:
        import auto_gptq
        auto_gptq_logger = logging.getLogger(auto_gptq.modeling._base.__name__)
        auto_gptq_logger.setLevel(logging.ERROR)
    except ImportError:
        pass


def configure_embedded_browser_runtime():
    existing_flags = os.getenv('QTWEBENGINE_CHROMIUM_FLAGS', '')
    flags = [flag for flag in existing_flags.split() if flag]
    required_flags = [
        '--disable-logging',
        '--log-level=3',
        '--disable-gpu',
        '--disable-features=WebGPU',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'
    ]
    for flag in required_flags:
        if flag not in flags:
            flags.append(flag)
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = ' '.join(flags)


def install_qt_message_filter():
    def qt_message_handler(message_type, context, message):
        if PNG_WARNING_TO_SUPPRESS in message:
            return
        for suppressed_message in QT_MESSAGES_TO_SUPPRESS:
            if suppressed_message in message:
                return
        if _previous_qt_message_handler is not None:
            _previous_qt_message_handler(message_type, context, message)
            return
        message_type_labels = {
            QtMsgType.QtDebugMsg: 'qt.core.debug',
            QtMsgType.QtInfoMsg: 'qt.core.info',
            QtMsgType.QtWarningMsg: 'qt.core.warning',
            QtMsgType.QtCriticalMsg: 'qt.core.critical',
            QtMsgType.QtFatalMsg: 'qt.core.fatal'
        }
        label = message_type_labels.get(message_type, 'qt.core')
        print(f'{label}: {message}', file=sys.__stderr__)

    global _previous_qt_message_handler
    _previous_qt_message_handler = qInstallMessageHandler(qt_message_handler)


def run_gui():
    configure_embedded_browser_runtime()
    from widgets.main_window import MainWindow

    app = QApplication([])
    # The application name is shown in the taskbar.
    app.setApplicationName('TagGUI Plus')
    # The application display name is shown in the title bar.
    app.setApplicationDisplayName('TagGUI Plus')
    app.setStyle('Fusion')
    # Disable the allocation limit to allow loading large images.
    QImageReader.setAllocationLimit(0)
    install_qt_message_filter()
    def _create_and_show():
        try:
            app.main_window = MainWindow(app)
            # show() is deferred — MainWindow calls _show_window() once the
            # first image has been rendered (or immediately if there is none).
        except Exception as exception:
            settings = get_settings()
            settings.clear()
            error_box = QMessageBox()
            error_box.setWindowTitle('Error')
            error_box.setIcon(QMessageBox.Icon.Critical)
            error_box.setText(str(exception))
            error_box.setDetailedText(traceback.format_exc())
            error_box.exec()
            app.quit()

    QTimer.singleShot(0, _create_and_show)
    exit_code = app.exec()
    # If the app asked to restart itself (see MainWindow._restart_application),
    # exit with the special code so the managed launcher relaunches us in the
    # same console window instead of a second window being opened.
    if getattr(app, 'restart_requested', False):
        exit_code = RESTART_EXIT_CODE
    sys.exit(exit_code)


if __name__ == '__main__':
    # Prevent PyTorch from opening multiple windows when running inside a
    # PyInstaller bundle.
    if len(sys.argv) > 1 and 'compile_worker' in sys.argv[1]:
        import runpy

        sys.argv = sys.argv[1:]
        runpy.run_path(sys.argv[0], run_name='__main__')
        sys.exit(0)
    suppress_warnings()
    try:
        run_gui()
    except Exception as exception:
        settings = get_settings()
        settings.clear()
        error_message_box = QMessageBox()
        error_message_box.setWindowTitle('Error')
        error_message_box.setIcon(QMessageBox.Icon.Critical)
        error_message_box.setText(str(exception))
        error_message_box.setDetailedText(traceback.format_exc())
        error_message_box.exec()
        raise exception
