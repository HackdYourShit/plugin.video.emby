# -*- coding: utf-8 -*-

#################################################################################################

import logging
import sys
import time
import _strptime # Workaround for threads using datetime: _striptime is locked
from datetime import datetime
import platform

import xbmc
import xbmcgui

import userclient
import clientinfo
import initialsetup
import kodimonitor
import librarysync
import player
import websocket_client as wsc
from views import VideoNodes
from utils import window, settings, dialog, language as lang
#from ga_client import GoogleAnalytics
import hashlib

#################################################################################################

log = logging.getLogger("EMBY."+__name__)

#################################################################################################


class Service(object):

    startup = False
    server_online = True
    capabitilities = False
    warn_auth = True

    userclient_running = False
    userclient_thread = None
    websocket_running = False
    websocket_thread = None
    library_running = False
    library_thread = None

    last_progress = datetime.today()

    def __init__(self):

        self.client_info = clientinfo.ClientInfo()
        self.addon_name = self.client_info.get_addon_name()
        log_level = settings('logLevel')

        # General settings which are used by other entrypoints
        window('emby_logLevel', value=str(log_level))
        window('emby_kodiProfile', value=xbmc.translatePath('special://profile'))
        window('emby_context', value="true" if settings('enableContext') == "true" else "")
        window('emby_context_transcode', value="true" if settings('enableContextTranscode') == "true" else "")

        # Initial logging
        log.warn("======== START %s ========", self.addon_name)
        log.warn("Python Version: %s", sys.version)
        log.warn("Platform: %s", self.client_info.get_platform())
        log.warn("KODI Version: %s", xbmc.getInfoLabel('System.BuildVersion'))
        log.warn("%s Version: %s", self.addon_name, self.client_info.get_version())
        log.warn("Using plugin paths: %s", settings('useDirectPaths') == "0")
        log.warn("Log Level: %s", log_level)

        # Reset window props for profile switch
        properties = [

            "emby_online", "emby_state.json", "emby_serverStatus", "emby_onWake",
            "emby_syncRunning", "emby_dbCheck", "emby_kodiScan",
            "emby_shouldStop", "emby_currUser", "emby_dbScan", "emby_sessionId",
            "emby_initialScan", "emby_customplaylist", "emby_playbackProps",
            "emby.external_check", "emby.external", "emby.resume", "emby.connected"
        ]
        for prop in properties:
            window(prop, clear=True)

        # Clear video nodes properties
        VideoNodes().clearProperties()
        # assume offline mode
        log.info("Loading existing views...")
        librarysync.LibrarySync().offline_mode_views()

        # Set the minimum database version
        window('emby_minDBVersion', value="1.1.63")


    def service_entry_point(self):

        # Important: Threads depending on abortRequest will not trigger
        # if profile switch happens more than once.
        self.monitor = kodimonitor.KodiMonitor()
        self.kodi_player = player.Player()
        kodi_profile = xbmc.translatePath('special://profile')

        # Server auto-detect
        initialsetup.InitialSetup().setup()

        # Initialize important threads
        self.userclient_thread = userclient.UserClient()
        user_client = self.userclient_thread
        self.websocket_thread = wsc.WebSocketClient()
        self.library_thread = librarysync.LibrarySync()

        while not self.monitor.abortRequested():

            if window('emby_kodiProfile') != kodi_profile:
                # Profile change happened, terminate this thread and others
                log.info("Kodi profile was: %s and changed to: %s. Terminating old Emby thread.",
                         kodi_profile, window('emby_kodiProfile'))
                exc = Exception("Kodi profile changed detected")
                exc.quiet = True
                raise exc

            # Before proceeding, need to make sure:
            # 1. Server is online
            # 2. User is set
            # 3. User has access to the server

            if window('emby_online') == "true":

                # Emby server is online
                # Verify if user is set and has access to the server
                if user_client.get_user() is not None and user_client.get_access():

                    if self.kodi_player.isPlaying():
                        self._report_progress()

                    # If an item is playing
                    if not self.startup:
                        self.startup = self._startup()

                    if not self.websocket_running:
                        # Start the Websocket Client
                        self.websocket_running = True
                        self.websocket_thread.start()
                    if not self.library_running:
                        # Start the syncing thread
                        self.library_running = True
                        self.library_thread.start()
                    if not self.capabitilities and user_client.post_capabilities():
                        self.capabitilities = True

                    if self.monitor.waitForAbort(15):
                        # Abort was requested while waiting. We should exit
                        break
                else:

                    if (user_client.get_user() is None) and self.warn_auth:
                        # Alert user is not authenticated and suppress future warning
                        self.warn_auth = False
                        log.info("Not authenticated yet.")

                    # User access is restricted.
                    # Keep verifying until access is granted
                    # unless server goes offline or Kodi is shut down.
                    self._access_check()
            else:
                # Wait until Emby server is online
                # or Kodi is shut down.
                self._server_online_check()
                
            if self.monitor.waitForAbort(1):
                # Abort was requested while waiting. We should exit
                break

        ##### Emby thread is terminating. #####
        self.shutdown()

    def _startup(self):

        # Start up events
        self.warn_auth = True

        username = self.userclient_thread.get_username()
        if settings('connectMsg') == "true" and username:
            # Get additional users
            add_users = settings('additionalUsers')
            if add_users:
                add_users = ", "+", ".join(add_users.split(','))

            dialog(type_="notification",
                   heading="{emby}",
                   message=("%s %s%s"
                            % (lang(33000), username.decode('utf-8'),
                               add_users.decode('utf-8'))),
                   icon="{emby}",
                   time=2000,
                   sound=False)
        return True

    def _server_online_check(self):
        # Set emby_online true/false property
        user_client = self.userclient_thread
        while not self.monitor.abortRequested():

            if user_client.get_server() is None:
                # No server info set in add-on settings
                pass

            elif not user_client.verify_server():
                # Server is offline.
                # Alert the user and suppress future warning
                if self.server_online:
                    log.info("Server is offline")
                    window('emby_online', value="false")

                    if settings('offlineMsg') == "true":
                        dialog(type_="notification",
                               heading=lang(33001),
                               message="%s %s" % (self.addon_name, lang(33002)),
                               icon="{emby}",
                               sound=False)

                self.server_online = False

                if self.monitor.waitForAbort(7):
                    # Abort was requested while waiting.
                    break

            elif window('emby_online') in ("sleep", "reset"):
                # device going to sleep
                if self.websocket_running:
                    self.websocket_thread.stop_client()
                    self.websocket_thread = wsc.WebSocketClient()
                    self.websocket_running = False

                if self.library_running:
                    self.library_thread.stopThread()
                    self.library_thread = librarysync.LibrarySync()
                    self.library_running = False
            else:
                # Server is online
                if not self.server_online:
                    # Server was offline when Kodi started.
                    # Wait for server to be fully established.
                    if self.monitor.waitForAbort(5):
                        # Abort was requested while waiting.
                        break
                    # Alert the user that server is online.
                    dialog(type_="notification",
                           heading="{emby}",
                           message=("%s %s"
                                    % (lang(33000), user_client.get_username().decode('utf-8'))),
                           icon="{emby}",
                           time=2000,
                           sound=False)

                self.server_online = True
                self.capabitilities = False
                window('emby_online', value="true")
                log.info("Server is online and ready")

                # Start the userclient thread
                if not self.userclient_running:
                    self.userclient_running = True
                    user_client.start()

                break

            if self.monitor.waitForAbort(1):
                # Abort was requested while waiting.
                break

    def _access_check(self):
        # Keep verifying until access is granted
        # unless server goes offline or Kodi is shut down.
        while not self.userclient_thread.get_access():

            if window('emby_online') != "true":
                # Server went offline
                break

            if self.monitor.waitForAbort(30):
                # Abort was requested while waiting. We should exit
                break

    def _report_progress(self):
        # Update and report playback progress
        kodi_player = self.kodi_player
        try:
            play_time = kodi_player.getTime()
            filename = kodi_player.getPlayingFile()
            # Update positionticks
            if filename in kodi_player.played_info and play_time > 0:
                kodi_player.played_info[filename]['currentPosition'] = play_time

            difference = datetime.today() - self.last_progress
            difference_seconds = difference.seconds

            # Ping session every 4-5 minutes
            if difference_seconds > 270:
                kodi_player.reportPlayback()
                self.last_progress = datetime.today()

        except Exception as error:
            log.exception(error)

    def shutdown(self):

        if self.monitor.special_monitor:
            self.monitor.special_monitor.stop_monitor()

        if self.userclient_running:
            self.userclient_thread.stop_client()

        if self.library_running:
            self.library_thread.stopThread()

        if self.websocket_running:
            self.websocket_thread.stop_client()

        log.warn("======== STOP %s ========", self.addon_name)
