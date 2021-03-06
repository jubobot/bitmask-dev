# -*- coding: utf-8 -*-
# app.py
# Copyright (C) 2016-2017 LEAP Encryption Acess Project
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Main entrypoint for the Bitmask Qt GUI.
It just launches a wekbit browser that runs the local web-ui served by bitmaskd
when the web service is running.
"""

import os
import platform
import signal
import sys
import time
import webbrowser

from functools import partial
from multiprocessing import Process

from leap.bitmask.core.launcher import run_bitmaskd, pid
from leap.bitmask.gui import app_rc
from leap.common.config import get_path_prefix
from leap.common.events import client as leap_events
from leap.common.events import catalog

if platform.system() == 'Windows':
    from multiprocessing import freeze_support
    from PySide import QtCore, QtGui
    from PySide.QtGui import QDialog
    from PySide.QtGui import QApplication
    from PySide.QtWebKit import QWebView, QGraphicsWebView
else:
    from PyQt5 import QtCore, QtGui
    from PyQt5.QtCore import QObject, pyqtSlot
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtGui import QIcon
    from PyQt5.QtGui import QPixmap
    from PyQt5.QtWidgets import QAction
    from PyQt5.QtWidgets import QMenu
    from PyQt5.QtWidgets import QSystemTrayIcon
    from PyQt5.QtWidgets import QDialog
    from PyQt5.QtWidgets import QMessageBox

    try:
        from PyQt5.QtWebKitWidgets import QWebView
        from PyQt5.QtWebKit import QWebSettings
    except ImportError:
        from PyQt5.QtWebEngineWidgets import QWebEngineView as QWebView
        from PyQt5.QtWebEngineWidgets import QWebEngineSettings as QWebSettings


IS_WIN = platform.system() == "Windows"
DEBUG = os.environ.get("DEBUG", False)

BITMASK_URI = 'http://localhost:7070/'
PIXELATED_URI = 'http://localhost:9090/'

qApp = None
bitmaskd = None
browser = None

# TODO do switch based on theme

TRAY_ICONS = (
    ':/black/22/wait.png',
    ':/black/22/on.png',
    ':/black/22/off.png')


class WithTrayIcon(QDialog):

    user_closed = False

    def setupSysTray(self):
        self._createIcons()
        self._createActions()
        self._createTrayIcon()
        self.trayIcon.activated.connect(self.iconActivated)
        self.setVPNStatus('off')
        self.setUpEventListener()
        self.trayIcon.show()

    def setVPNStatus(self, status):
        seticon = self.trayIcon.setIcon
        settip = self.trayIcon.setToolTip
        # XXX this is an oversimplification, see #9131
        # the simple state for failure is off too, for now.
        if status == 'off':
            seticon(self.ICON_OFF)
            settip('VPN: Off')
        elif status == 'on':
            seticon(self.ICON_ON)
            settip('VPN: On')
        elif status == 'starting':
            seticon(self.ICON_WAIT)
            settip('VPN: Starting')
        elif status == 'stopping':
            seticon(self.ICON_WAIT)
            settip('VPN: Stopping')

    def setUpEventListener(self):
        leap_events.register(
            catalog.VPN_STATUS_CHANGED,
            self._handle_vpn_event)

    def _handle_vpn_event(self, *args):
        status = None
        if len(args) > 1:
            status = args[1]
            self.setVPNStatus(status.lower())

    def _createIcons(self):
        self.ICON_WAIT = QIcon(QPixmap(TRAY_ICONS[0]))
        self.ICON_ON = QIcon(QPixmap(TRAY_ICONS[1]))
        self.ICON_OFF = QIcon(QPixmap(TRAY_ICONS[2]))

    def _createActions(self):
        self.quitAction = QAction(
            "&Quit", self,
            triggered=self.closeFromSystray)

    def iconActivated(self, reason):
        # can use .Trigger also for single click
        if reason in (QSystemTrayIcon.DoubleClick, ):
            self.showNormal()

    def closeFromSystray(self):
        self.user_closed = True
        self.close()

    def _createTrayIcon(self):
        self.trayIconMenu = QMenu(self)
        self.trayIconMenu.addAction(self.quitAction)
        self.trayIcon = QSystemTrayIcon(self)
        self.trayIcon.setContextMenu(self.trayIconMenu)

    def closeEvent(self, event):
        if self.trayIcon.isVisible() and not self.user_closed:
            QMessageBox.information(
                self, "Bitmask",
                "Bitmask will minimize to the system tray. "
                "You can choose 'Quit' from the menu with a "
                "right click on the icon, and restore the window "
                "with a double click.")
        self.hide()
        if not self.user_closed:
            event.ignore()


class BrowserWindow(QWebView, WithTrayIcon):
    """
    This qt-webkit window exposes a couple of callable objects through the
    python-js bridge:

        bitmaskApp.shutdown() -> shut downs the backend and frontend.
        bitmaskApp.openSystemBrowser(url) -> opens URL in system browser
        bitmaskBrowser.openPixelated() -> opens Pixelated app in a new window.

    This BrowserWindow assumes that the backend is already running, since it is
    going to look for the authtoken in the configuration folder.
    """
    def __init__(self, *args, **kw):
        url = kw.pop('url', None)
        first = False
        if not url:
            url = "http://localhost:7070"
            path = os.path.join(get_path_prefix(), 'leap', 'authtoken')
            waiting = 20
            while not os.path.isfile(path):
                if waiting == 0:
                    # If we arrive here, something really messed up happened,
                    # because touching the token file is one of the first
                    # things the backend does, and this BrowserWindow
                    # should be called *right after* launching the backend.
                    raise NoAuthToken(
                        'No authentication token found!')
                time.sleep(0.1)
                waiting -= 1
            token = open(path).read().strip()
            url += '#' + token
            first = True
        self.url = url
        self.closing = False

        super(QWebView, self).__init__(*args, **kw)
        self.setWindowTitle('Bitmask')
        self.bitmask_browser = NewPageConnector(self) if first else None
        self.loadPage(self.url)

        self.proxy = AppProxy(self) if first else None
        self.frame.addToJavaScriptWindowObject(
            "bitmaskApp", self.proxy)

        icon = QtGui.QIcon()
        icon.addPixmap(
            QtGui.QPixmap(":/mask-icon.png"),
            QtGui.QIcon.Normal, QtGui.QIcon.Off)
        self.setWindowIcon(icon)

    def loadPage(self, web_page):
        try:
            self.settings().setAttribute(
                QWebSettings.DeveloperExtrasEnabled, True)
        except Exception:
            pass

        if os.environ.get('DEBUG'):
            self.inspector = QWebInspector(self)
            self.inspector.setPage(self.page())
            self.inspector.show()

        if os.path.isabs(web_page):
            web_page = os.path.relpath(web_page)

        url = QtCore.QUrl(web_page)
        # TODO -- port this to QWebEngine
        self.frame = self.page().mainFrame()
        self.frame.addToJavaScriptWindowObject(
            "bitmaskBrowser", self.bitmask_browser)
        self.load(url)

    def shutdown(self, *args):
        if self.closing:
            return
        self.closing = True
        global bitmaskd
        bitmaskd.join()
        if os.path.isfile(pid):
            with open(pid) as f:
                pidno = int(f.read())
            print('[bitmask] terminating bitmaskd...')
            os.kill(pidno, signal.SIGTERM)
        print('[bitmask] shutting down gui...')
        try:
            self.stop()
            try:
                global pixbrowser
                pixbrowser.stop()
                del pixbrowser
            except:
                pass
            QtCore.QTimer.singleShot(0, qApp.deleteLater)

        except Exception as ex:
            print('exception catched: %r' % ex)
            sys.exit(1)


class AppProxy(QObject):

    @pyqtSlot()
    def shutdown(self):
        """To be exposed from the js bridge"""
        global browser
        if browser:
            browser.user_closed = True
            browser.close()

    @pyqtSlot(str)
    def openSystemBrowser(self, url):
        webbrowser.open(url)


pixbrowser = None


class NewPageConnector(QObject):

    @pyqtSlot()
    def openPixelated(self):
        global pixbrowser
        pixbrowser = BrowserWindow(url=PIXELATED_URI)
        pixbrowser.show()


def _handle_kill(*args, **kw):
    win = kw.get('win')
    if win:
        QtCore.QTimer.singleShot(0, win.close)
    global pixbrowser
    if pixbrowser:
        QtCore.QTimer.singleShot(0, pixbrowser.close)


def launch_gui():
    global qApp
    global bitmaskd
    global browser

    if IS_WIN:
        freeze_support()
    bitmaskd = Process(target=run_bitmaskd)
    bitmaskd.start()

    qApp = QApplication([])
    try:
        browser = BrowserWindow(None)
    except NoAuthToken as e:
        print('ERROR: ' + e.message)
        sys.exit(1)

    browser.setupSysTray()

    qApp.setQuitOnLastWindowClosed(True)
    qApp.lastWindowClosed.connect(browser.shutdown)

    signal.signal(
        signal.SIGINT,
        partial(_handle_kill, win=browser))

    # Avoid code to get stuck inside c++ loop, returning control
    # to python land.
    timer = QtCore.QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(500)

    browser.show()
    sys.exit(qApp.exec_())


def start_app():
    from leap.bitmask.util import STANDALONE
    os.environ['QT_AUTO_SCREEN_SCALE_FACTOR'] = '1'

    # Allow the frozen binary in the bundle double as the cli entrypoint
    # Why have only a user interface when you can have two?

    if platform.system() == 'Windows':
        # In windows, there are some args added to the invocation
        # by PyInstaller, I guess...
        MIN_ARGS = 3
    else:
        MIN_ARGS = 1

    # DEBUG ====================================
    if STANDALONE and len(sys.argv) > MIN_ARGS:
        if sys.argv[1] == 'bitmask_helpers':
            from leap.bitmask.vpn.helpers import main
            return main()

        from leap.bitmask.cli import bitmask_cli
        return bitmask_cli.main()

    prev_auth = os.path.join(get_path_prefix(), 'leap', 'authtoken')
    try:
        os.remove(prev_auth)
    except OSError:
        pass

    launch_gui()


class NoAuthToken(Exception):
    pass


if __name__ == "__main__":
    start_app()
