
import json
import os
import random
import socket
import subprocess
import time
from constants import *
import shutil

from subprocess import check_output
from pathlib import Path, PosixPath

from spleeter.__main__ import separate
from spleeter.audio import Codec, STFTBackend
import qrcode

from lib import vlcclient
from lib.get_platform import get_platform
from urllib.parse import urlparse, parse_qsl

if get_platform() != "windows":
    from signal import SIGALRM, alarm, signal


class Karaoke:
    raspi_wifi_config_ip = "10.0.0.1"
    raspi_wifi_conf_file = "/etc/raspiwifi/raspiwifi.conf"
    raspi_wifi_config_installed = os.path.exists(raspi_wifi_conf_file)

    queue = []
    available_songs = []
    now_playing = None
    now_playing_filename = None
    now_playing_user = None
    now_playing_transpose = 0
    is_paused = True
    process = None
    qr_code_path = None
    base_path = os.path.dirname(__file__)
    volume_offset = 0
    loop_interval = 500  # in milliseconds
    default_logo_path = os.path.join(base_path, "logo.png")
    playing_type = ACCOMPANIMENT_SUFFIX

    def __init__(
            self,
            port=5000,
            download_path="/usr/lib/pikaraoke/songs",
            hide_ip=False,
            hide_raspiwifi_instructions=False,
            hide_splash_screen=False,
            omxplayer_adev="both",
            dual_screen=False,
            high_quality=False,
            volume=0,
            splash_delay=2,
            youtubedl_path="/Users/francis/code/pikaraoke/Env/bin/youtube-dl",
            omxplayer_path=None,
            use_omxplayer=False,
            vlc_path=None,
            vlc_port=None,
            logo_path=None,
            show_overlay=False,
            logger=None
    ):

        # override with supplied constructor args if provided
        self.port = port
        self.hide_ip = hide_ip
        self.hide_raspiwifi_instructions = hide_raspiwifi_instructions
        self.hide_splash_screen = hide_splash_screen
        self.omxplayer_adev = omxplayer_adev
        self.download_path = download_path
        self.dual_screen = dual_screen
        self.high_quality = high_quality
        self.splash_delay = int(splash_delay)
        self.volume_offset = volume
        self.youtubedl_path = youtubedl_path
        self.vlc_path = vlc_path
        self.vlc_port = vlc_port
        self.logo_path = self.default_logo_path if logo_path == None else logo_path
        self.show_overlay = show_overlay

        # other initializations
        self.platform = get_platform()
        self.vlcclient = None
        self.omxclient = None
        self.screen = None
        self.logger = logger

        self.logger.debug(
            """
    http port: %s
    hide IP: %s
    hide RaspiWiFi instructions: %s,
    hide splash: %s
    splash_delay: %s
    omx audio device: %s
    dual screen: %s
    high quality video: %s
    download path: %s
    default volume: %s
    youtube-dl path: %s
    logo path: %s
    VLC path: %s
    VLC port: %s
    show overlay: %s"""
            % (
                self.port,
                self.hide_ip,
                self.hide_raspiwifi_instructions,
                self.hide_splash_screen,
                self.splash_delay,
                self.omxplayer_adev,
                self.dual_screen,
                self.high_quality,
                self.download_path,
                self.volume_offset,
                self.youtubedl_path,
                self.logo_path,
                self.vlc_path,
                self.vlc_port,
                self.show_overlay
            )
        )

        # Generate connection URL and QR code, retry in case pi is still starting up
        # and doesn't have an IP yet (occurs when launched from /etc/rc.local)
        end_time = int(time.time()) + 30

        if self.platform == "raspberry_pi":
            while int(time.time()) < end_time:
                addresses_str = check_output(["hostname", "-I"]).strip().decode("utf-8")
                addresses = addresses_str.split(" ")
                self.ip = addresses[0]
                if not self.is_network_connected():
                    self.logger.debug("Couldn't get IP, retrying....")
                else:
                    break
        else:
            self.ip = self.get_ip()

        self.logger.debug("IP address (for QR code and splash screen): " + self.ip)

        self.url = "http://%s:%s" % (self.ip, self.port)

        # get songs from download_path
        self.get_available_songs()

        self.get_youtubedl_version()

        # clean up old sessions
        self.kill_player()

        self.generate_qr_code()

        if self.show_overlay:
            self.vlcclient = vlcclient.VLCClient(port=self.vlc_port, path=self.vlc_path, qrcode=self.qr_code_path, url=self.url, logger=self.logger)
        else:
            self.vlcclient = vlcclient.VLCClient(port=self.vlc_port, path=self.vlc_path, logger=self.logger)


    # Other ip-getting methods are unreliable and sometimes return 127.0.0.1
    # https://stackoverflow.com/a/28950776
    def get_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # doesn't even have to be reachable
            s.connect(("10.255.255.255", 1))
            IP = s.getsockname()[0]
        except Exception:
            IP = "127.0.0.1"
        finally:
            s.close()
        return IP

    def get_raspi_wifi_conf_vals(self):
        """Extract values from the RaspiWiFi configuration file."""
        f = open(self.raspi_wifi_conf_file, "r")

        # Define default values.
        #
        # References: 
        # - https://github.com/jasbur/RaspiWiFi/blob/master/initial_setup.py (see defaults in input prompts)
        # - https://github.com/jasbur/RaspiWiFi/blob/master/libs/reset_device/static_files/raspiwifi.conf
        #
        server_port = "80"
        ssid_prefix = "RaspiWiFi Setup"
        ssl_enabled = "0"

        # Override the default values according to the configuration file.
        for line in f.readlines():
            if "server_port=" in line:
                server_port = line.split("t=")[1].strip()
            elif "ssid_prefix=" in line:
                ssid_prefix = line.split("x=")[1].strip()
            elif "ssl_enabled=" in line:
                ssl_enabled = line.split("d=")[1].strip()

        return (server_port, ssid_prefix, ssl_enabled)

    def get_youtubedl_version(self):
        self.youtubedl_version = (
            check_output([self.youtubedl_path, "--version"]).strip().decode("utf8")
        )
        return self.youtubedl_version

    def upgrade_youtubedl(self):
        self.logger.info(
            "Upgrading youtube-dl, current version: %s" % self.youtubedl_version
        )
        output = check_output([self.youtubedl_path, "-U"]).decode("utf8").strip()
        self.logger.info(output)
        if "It looks like you installed youtube-dl with a package manager" in output:
            try:
                self.logger.info("Attempting youtube-dl upgrade via pip3...")
                output = check_output(
                    ["pip3", "install", "--upgrade", "youtube-dl"]
                ).decode("utf8")
            except FileNotFoundError:
                self.logger.info("Attempting youtube-dl upgrade via pip...")
                output = check_output(
                    ["pip", "install", "--upgrade", "youtube-dl"]
                ).decode("utf8")
            self.logger.info(output)
        self.get_youtubedl_version()
        self.logger.info("Done. New version: %s" % self.youtubedl_version)

    def is_network_connected(self):
        return not len(self.ip) < 7

    def generate_qr_code(self):
        self.logger.debug("Generating URL QR code")
        qr = qrcode.QRCode(
            version=1,
            box_size=1,
            border=4,
        )
        qr.add_data(self.url)
        qr.make()
        img = qr.make_image()
        self.qr_code_path = os.path.join(self.base_path, "qrcode.png")
        img.save(self.qr_code_path)


    def get_search_results(self, textToSearch):
        self.logger.info("Searching YouTube for: " + textToSearch)
        num_results = 15
        yt_search = 'ytsearch%d:"%s"' % (num_results, textToSearch)
        cmd = [self.youtubedl_path, "-j", "--no-playlist", "--flat-playlist", yt_search]
        self.logger.debug("Youtube-dl search command: " + " ".join(cmd))
        try:
            output = subprocess.check_output(cmd).decode("utf-8")
            self.logger.debug("Search results: " + output)
            rc = []
            video_url_base = "https://www.youtube.com/watch?v="
            for each in output.split("\n"):
                if len(each) > 2:
                    j = json.loads(each)
                    if (not "title" in j) or (not "url" in j):
                        continue
                    rc.append([j["title"], video_url_base + j["url"], j["id"]])
            return rc
        except Exception as e:
            self.logger.debug("Error while executing search: " + str(e))
            raise e

    def get_karaoke_search_results(self, songTitle):
        return self.get_search_results(songTitle + " karaoke")

    def download_video(self, video_url, enqueue=False, user="Pikaraoke"):
        self.logger.info("Downloading video: " + video_url)
        dl_path = self.download_path + "%(title)s---%(id)s.%(ext)s"
        file_quality = (
            "bestvideo[ext!=webm][height<=1080]+bestaudio[ext!=webm]/best[ext!=webm]"
            if self.high_quality
            else "mp4"
        )
        cmd = [self.youtubedl_path, "-f", file_quality, "-o", dl_path, video_url]
        self.logger.debug("Youtube-dl command: " + " ".join(cmd))
        rc = subprocess.call(cmd)
        if rc != 0:
            self.logger.error("Error code while downloading, retrying once...")
            rc = subprocess.call(cmd)  # retry once. Seems like this can be flaky
        if rc == 0:
            self.logger.debug("Song successfully downloaded: " + video_url)

            y = self.get_youtube_id_from_url(video_url)
            s = self.find_song_by_youtube_id(y)
            item = self.post_process_video(s)

            if item:
                self.get_available_songs()
                if enqueue:
                    self.enqueue(item, user)
                else:
                    self.logger.error("Error queueing song: " + video_url)
        else:
            self.logger.error("Error downloading song: " + video_url)
        return rc

    def post_process_video(self, file_path):
        base, ext = os.path.splitext(file_path)
        accompaniment_path = base + ACCOMPANIMENT_SUFFIX + ext
        vocal_path = base + VOCAL_SUFFIX + ext

        tmp_path = os.path.expanduser(TMP_DIR)
        tmp_path = os.path.join(tmp_path, str(random.randint(1, 10000000)))

        if not tmp_path.endswith("/"):
            tmp_path += "/"
        if not os.path.exists(tmp_path):
            print("Creating tmp path: " + tmp_path)
            os.makedirs(tmp_path)

        split_result = 0
        # Split vocal and accompaniment
        # separate(files=[Path(file_path)],output_path=Path(tmp_dir), filename_format="{filename}_{instrument}.{codec}")
        cmd = ["spleeter", "separate", "-o", tmp_path, "-f", "{instrument}.{codec}", file_path]
        self.logger.info(str(cmd))
        split_result = subprocess.call(cmd)

        # try:
        #     self.seperate_audio(file_path, tmp_path)
        # except Exception as e:
        #     self.logger.error('Split audio failed due to %s' % e)
        #     split_result = 1

        if split_result == 0:
            # Split video and audio
            s_cmd = ["ffmpeg", "-i", file_path, "-an", "-c", "copy", f"{tmp_path}/video.mp4"]
            split_rs = subprocess.call(s_cmd)

            # Merge video and accompaniment
            m_cmd = ["ffmpeg", "-i", f"{tmp_path}/video.mp4", "-i", f"{tmp_path}/accompaniment.wav", "-c:v", "copy", "-c:a", "aac", accompaniment_path]
            merge_rs = subprocess.call(m_cmd)

            os.rename(file_path, vocal_path)
            self.logger.info('Spleeter song succeed!')
            shutil.rmtree(tmp_path, ignore_errors=True)
        return accompaniment_path

    def seperate_audio(self, file_path, output_path):
        separate(
            deprecated_files=None,
            files=[PosixPath(os.path.abspath(file_path))],
            adapter='spleeter.audio.ffmpeg.FFMPEGProcessAudioAdapter',
            bitrate='128k',
            codec=Codec.WAV,
            duration=600.0,
            offset=0.0,
            output_path=PosixPath(os.path.abspath(output_path)),
            stft_backend=STFTBackend.AUTO,
            filename_format="{instrument}.{codec}",
            params_filename='spleeter:2stems',
            mwf=False,
            verbose=False
        )

    def get_available_songs(self):
        self.logger.info("Fetching available songs in: " + self.download_path)
        types = ['.mp4', '.mp3', '.zip', '.mkv', '.avi', '.webm', '.mov']
        files_grabbed = []
        P = Path(self.download_path)
        for file in P.rglob('*.*'):
            base, ext = os.path.splitext(file.as_posix())
            if ext.lower() in types and base.endswith(ACCOMPANIMENT_SUFFIX):
                if os.path.isfile(file.as_posix()):
                    self.logger.debug("adding song: " + file.name)
                    files_grabbed.append(file.as_posix())

        self.available_songs = sorted(files_grabbed, key=lambda f: str.lower(os.path.basename(f)))

    def delete(self, song_path):
        self.logger.info("Deleting song: " + song_path)

        accompaniment_path = song_path.replace(VOCAL_SUFFIX, ACCOMPANIMENT_SUFFIX)
        if os.path.exists(accompaniment_path):
            os.remove(accompaniment_path)

        vocal_path = song_path.replace(ACCOMPANIMENT_SUFFIX, VOCAL_SUFFIX)
        if os.path.exists(vocal_path):
            os.remove(vocal_path)

        ext = os.path.splitext(song_path)
        # if we have an associated cdg file, delete that too
        cdg_file = song_path.replace(ext[1], ".cdg")
        if os.path.exists(cdg_file):
            os.remove(cdg_file)

        self.get_available_songs()

    def rename(self, song_path, new_name):
        self.logger.info("Renaming song: '" + song_path + "' to: " + new_name)
        ext = os.path.splitext(song_path)
        if len(ext) == 2:
            new_file_name = new_name + ext[1]
        os.rename(song_path, self.download_path + new_file_name)
        # if we have an associated cdg file, rename that too
        cdg_file = song_path.replace(ext[1], ".cdg")
        if (os.path.exists(cdg_file)):
            os.rename(cdg_file, self.download_path + new_name + ".cdg")
        self.get_available_songs()

    def filename_from_path(self, file_path):
        rc = os.path.basename(file_path)
        rc = os.path.splitext(rc)[0]
        rc = rc.split("---")[0]  # removes youtube id if present
        return rc

    def find_song_by_youtube_id(self, youtube_id):

        P = Path(self.download_path)
        for file in P.rglob('*.*'):
            if os.path.isfile(file.as_posix()) and youtube_id in file.as_posix():
                return file.as_posix()

        self.logger.error("New downloaded song not found: " + youtube_id)
        return None

    def get_youtube_id_from_url(self, url):
        query = urlparse(url).query
        query_dict = dict(parse_qsl(query))
        youtube_id = query_dict['v']
        if youtube_id:
            return youtube_id
        else:
            self.logger.error("Error parsing youtube id from url: " + url)
            return None

    def kill_player(self):
        self.logger.debug("Killing old VLC processes")
        if self.vlcclient:
            self.vlcclient.kill()

    def play_file(self, file_path, semitones=0):
        self.now_playing = self.filename_from_path(file_path)
        self.now_playing_filename = file_path

        self.logger.info("Playing video in VLC: " + self.now_playing)
        if semitones == 0:
            self.vlcclient.play_file(file_path, playing_type=self.playing_type)
        else:
            self.vlcclient.play_file_transpose(file_path, semitones)

        self.is_paused = False

    def transpose_current(self, semitones):
        self.logger.info("Transposing song by %s semitones" % semitones)
        self.now_playing_transpose = semitones
        self.play_file(self.now_playing_filename, semitones)

    def is_file_playing(self):
        if self.vlcclient != None and self.vlcclient.is_running():
            return True
        else:
            self.now_playing = None
            return False

    def is_song_in_queue(self, song_path):
        for each in self.queue:
            if each["file"] == song_path:
                return True
        return False

    def enqueue(self, song_path, user="Pikaraoke"):
        if self.is_song_in_queue(song_path):
            self.logger.warn("Song is already in queue, will not add: " + song_path)
            return False
        else:
            self.logger.info("'%s' is adding song to queue: %s" % (user, song_path))
            self.queue.append({"user": user, "file": song_path, "title": self.filename_from_path(song_path)})
            return True

    def queue_add_random(self, amount):
        self.logger.info("Adding %d random songs to queue" % amount)
        songs = list(self.available_songs)  # make a copy
        if len(songs) == 0:
            self.logger.warn("No available songs!")
            return False

        selected_songs = random.sample(songs, amount)
        for song in selected_songs:
            self.queue.append({"user": "Randomizer", "file": song, "title": self.filename_from_path(song)})
        return True

    def queue_clear(self):
        self.logger.info("Clearing queue!")
        self.queue = []
        self.skip()

    def queue_edit(self, song_name, action):
        index = 0
        song = None
        for each in self.queue:
            if song_name in each["file"]:
                song = each
                break
            else:
                index += 1
        if song == None:
            self.logger.error("Song not found in queue: " + song["file"])
            return False
        if action == "up":
            if index < 1:
                self.logger.warn("Song is up next, can't bump up in queue: " + song["file"])
                return False
            else:
                self.logger.info("Bumping song up in queue: " + song["file"])
                del self.queue[index]
                self.queue.insert(index - 1, song)
                return True
        elif action == "down":
            if index == len(self.queue) - 1:
                self.logger.warn(
                    "Song is already last, can't bump down in queue: " + song["file"]
                )
                return False
            else:
                self.logger.info("Bumping song down in queue: " + song["file"])
                del self.queue[index]
                self.queue.insert(index + 1, song)
                return True
        elif action == "delete":
            self.logger.info("Deleting song from queue: " + song["file"])
            del self.queue[index]
            return True
        else:
            self.logger.error("Unrecognized direction: " + action)
            return False

    def skip(self):
        if self.is_file_playing():
            self.logger.info("Skipping: " + self.now_playing)
            self.vlcclient.stop()
            self.vlcclient.kill()
            self.reset_now_playing()
            return True
        else:
            self.logger.warning("Tried to skip, but no file is playing!")
            return False

    def pause(self):
        if self.is_file_playing():
            self.logger.info("Toggling pause: " + self.now_playing)
            if self.vlcclient.is_playing():
                self.vlcclient.pause()
            else:
                self.vlcclient.play()

            self.is_paused = not self.is_paused
            return True
        else:
            self.logger.warning("Tried to pause, but no file is playing!")
            return False

    def vol_up(self):
        if self.is_file_playing():
            self.vlcclient.vol_up()
            return True
        else:
            self.logger.warning("Tried to volume up, but no file is playing!")
            return False

    def vol_down(self):
        if self.is_file_playing():
            self.vlcclient.vol_down()
            return True
        else:
            self.logger.warning("Tried to volume down, but no file is playing!")
            return False

    def fast_forward(self):
        if self.is_file_playing():
            self.vlcclient.fast_forward()
            return True
        else:
            self.logger.warning("Tried to fast forward 7 seconds, but no file is playing!")
            return False

    def fast_backward(self):
        if self.is_file_playing():
            self.vlcclient.fast_backward()
            return True
        else:
            self.logger.warning("Tried to fast backward 7 seconds, but no file is playing!")
            return False

    def switch_vocals_accompaniment(self, playing_type):
        self.logger.info(f'{self.playing_type} to {playing_type}')
        if self.playing_type != playing_type:
            self.playing_type = playing_type
            self.vlcclient.switch_vocals_accompaniment()

    def restart(self):
        if self.is_file_playing():
            self.vlcclient.restart()
            self.is_paused = False
            return True
        else:
            self.logger.warning("Tried to restart, but no file is playing!")
            return False

    def stop(self):
        self.running = False

    def handle_run_loop(self):
        time.sleep(self.loop_interval / 1000)

    def reset_now_playing(self):
        self.now_playing = None
        self.now_playing_filename = None
        self.now_playing_user = None
        self.is_paused = True
        self.now_playing_transpose = 0

    def run(self):
        self.logger.info("Starting PiKaraoke!")
        self.running = True
        while self.running:
            try:
                if not self.is_file_playing() and self.now_playing != None:
                    self.reset_now_playing()
                if len(self.queue) > 0:
                    if not self.is_file_playing():
                        self.reset_now_playing()

                        # i = 0
                        # while i < (self.splash_delay * 1000):
                        self.handle_run_loop()
                            # i += self.loop_interval
                        self.play_file(self.queue[0]["file"])
                        self.now_playing_user = self.queue[0]["user"]
                        self.queue.pop(0)
                self.handle_run_loop()

            except KeyboardInterrupt:
                self.logger.warn("Keyboard interrupt: Exiting pikaraoke...")
                self.running = False
