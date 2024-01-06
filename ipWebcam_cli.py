#!/usr/bin/env python

import os
import sys
import argparse
import logging
import contextlib
import subprocess
import requests

from typing import List
from pydantic import BaseModel, Field

from urllib.parse import urlparse, urlunparse


class Const:
    LOG_LEVELS = ['critical', 'error', 'warn', 'info', 'debug']
    MODPROBE = 'sudo modprobe'
    GST_LAUNCH = 'gst-launch-1.0 -q --no-position'
    FFMPEG = 'ffmpeg -nostdin -loglevel warning'
    MPV = 'mpv'


class Utils:

    @staticmethod
    def config_logging(loglevel: int = logging.DEBUG):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(loglevel)
        console_handler.setFormatter(
            logging.Formatter(fmt='{asctime}.{msecs:03.0f} {levelname[0]} {name}: {message}',
                              style='{',
                              datefmt='%Y%m%d_%H%M%S'))

        logger = logging.getLogger()
        logger.setLevel(loglevel)
        logger.addHandler(console_handler)

        # silent some other no care messages
        import urllib3
        urllib3.disable_warnings()
        logging.getLogger('urllib3').setLevel(logging.WARNING)

    @staticmethod
    def m_subp_run(cmd: str | List[str], wait: bool = False, **kwargs) -> subprocess.Popen:
        logging.getLogger('subp').debug(cmd)
        p = subprocess.Popen(cmd, shell=isinstance(cmd, str), **kwargs)
        if p and wait:
            p.wait()
        return p

    @staticmethod
    def check_url(url: str, ssl_strict: bool = True) -> bool:
        logger = logging.getLogger('check')
        try:
            logger.debug(f'get url: {url} ..')
            resp = requests.get(url, timeout=3, stream=True, verify=ssl_strict)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f'on get url: {e}')
            return False

        return True

    @staticmethod
    def url_remove_auth(url: str) -> str:
        _url = urlparse(url)
        _url_netloc = _url.netloc.split('@')[-1]
        return urlunparse(
            (_url.scheme, _url_netloc, _url.path, _url.params, _url.query, _url.fragment))


class Config(BaseModel):
    ip: str
    port: int
    username: str = ''
    password: str = ''
    tls: bool = True
    ssl_strict: bool = True
    use_adb: bool = False

    sync: bool = False
    audio: bool = True
    video: bool = True
    v_fps: str = '60/1'
    v_method: str = Field(default='gst', pattern='(gst)|(ffmpeg)|(mpv)')
    a_codec: str = Field(default='opus', pattern='(opus)|(aac)|(wav)')
    sinkname: str = 'IPWebcam'
    loglevel: str = 'debug'


class Webcam:
    config: Config
    subp: List[subprocess.Popen] = []
    pw_rules: List[str] = []

    def __init__(self):
        self.logger = logging.getLogger('main')

    def parse_args(self, argv: List[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog='ipWebcam_cli',
            description='tool to use IPWebcam as v4l2 webcam or microphone')

        parser.add_argument('--loglevel', default='info', choices=Const.LOG_LEVELS)
        parser.add_argument('--use-adb', help='connect with adb', action='store_true')
        parser.add_argument('--ip', help='webcam ip addr', default='127.0.0.1')
        parser.add_argument('--port', help='ipWebcam listen port', default=8686)
        parser.add_argument('--username', help='username to access IPWebcam', default='')
        parser.add_argument('--password', help='password to access IPWebcam', default='')
        parser.add_argument('--tls', help='use https, default to http', action='store_true')
        parser.add_argument('--ssl-strict',
                            help='force TLS cert verification',
                            action='store_true')

        parser.add_argument('--an',
                            help='disable audio capture',
                            dest='audio',
                            action='store_false')
        parser.add_argument('--vn',
                            help='disable video capture',
                            dest='video',
                            action='store_false')
        parser.add_argument('--acodec',
                            help='audio codec to seclect the stream',
                            dest='a_codec',
                            default='opus',
                            choices=['wav', 'aac', 'opus'])
        parser.add_argument('--vmethod',
                            help='video capture method',
                            dest='v_method',
                            default='gst',
                            choices=['gst', 'ffmpeg', 'mpv'])
        parser.add_argument('--fps', help='provide video fps', dest='v_fps', default='60/1')
        parser.add_argument('--sync', help='enable gst A/V timesync', action='store_true')
        parser.add_argument('--sinkname', help='audio sink name', default='IPWebcam')

        sub_parsers = parser.add_subparsers(dest='mode', help='operation mode', required=True)
        sub_parsers.add_parser('run', help='launch webcam with v4l2 and audio as microphone')
        sub_parsers.add_parser('test', help='check and play directly')

        return parser.parse_args(argv)

    def adb_forward(self):
        self.config.ip = '127.0.0.1'    # force reset to local loopback
        Utils.m_subp_run(f'adb forward tcp:{self.config.port} tcp:{self.config.port}',
                         True,
                         stdout=subprocess.DEVNULL)

    def adb_forward_reset(self):
        Utils.m_subp_run(f'adb forward --remove tcp:{self.config.port}', True)

    def load_kmod_v4l2loopback(self):
        mod_name, mod_args = ('v4l2loopback', 'exclusive_caps=1')
        mod_loaded = False

        with open('/proc/modules') as fd:
            for line in fd:
                if line.startswith(f'{mod_name} '):
                    mod_loaded = True
                    break

        if not mod_loaded:
            self.logger.info(f'load kernel module: {mod_name} ..')
            Utils.m_subp_run(f'{Const.MODPROBE} {mod_name} {mod_args}', True)

    def get_v4l2_virtual_dev(self) -> str:
        for vdev in os.scandir('/sys/devices/virtual/video4linux/'):
            return f'/dev/{vdev.name}'
        else:
            return ''

    def virtual_mic_setup(self, sinkname: str):
        self.logger.info(f'setup virtual microphone for sink: {sinkname} ..')
        PA_LOAD = 'pactl load-module module-null-sink'

        # TODO: the virtual Mic may not appear in gnome's settings, and once
        #   user switch the audio output device, the pw-link will be reset

        self.logger.debug('create virual microphone ..')
        cmd = f'{PA_LOAD} media.class=Audio/Source/Virtual'
        cmd += f' sink_name={sinkname}.Mic channel_map=front-left,front-right'
        p = Utils.m_subp_run(cmd, stdout=subprocess.PIPE)
        rule_id, _ = p.communicate()
        self.pw_rules.append(rule_id.decode())

        self.logger.debug('create virual sink ..')
        cmd = f'{PA_LOAD} media.class=Audio/Sink sink_name={sinkname} channel_map=stereo'
        p = Utils.m_subp_run(cmd, stdout=subprocess.PIPE)
        rule_id, _ = p.communicate()
        self.pw_rules.append(rule_id.decode())

        self.logger.debug('link sink to microphone ..')
        Utils.m_subp_run(f'pw-link {sinkname}:monitor_FL {sinkname}.Mic:input_FL')
        Utils.m_subp_run(f'pw-link {sinkname}:monitor_FR {sinkname}.Mic:input_FR')

    def virtual_mic_cleanup(self):
        for rule in self.pw_rules:
            Utils.m_subp_run(f'pactl unload-module {rule}')

        self.pw_rules.clear()

    def get_gst_source_elem(self, url: str) -> str:
        return f'souphttpsrc location="{url}" is-live=true ssl-strict=' + \
                str(self.config.ssl_strict).lower()

    def audio_launch_gst(self, url: str, sinkname: str = ''):
        self.logger.info(f'audio launch (gst) to sink: {sinkname} ..')

        sink_args = 'sync=' + str(self.config.sync).lower()
        if sinkname:
            sink_args += f' device={sinkname}'

        p = Utils.m_subp_run(f'{Const.GST_LAUNCH} {self.get_gst_source_elem(url)}'
                             f' ! queue ! decodebin ! pulsesink {sink_args}')
        self.subp.append(p)

    def video_launch_gst(self, url: str, sink_dev: str = ''):
        self.logger.info(f'video launch (gst) to sink: {sink_dev} ..')

        if sink_dev:
            sink_args = 'videoscale ! video/x-raw,format=YUY2 ! '
            sink_args += f'tee ! v4l2sink device={sink_dev}'
        else:
            sink_args = 'autovideosink'

        sink_args += ' sync=' + str(self.config.sync).lower()

        p = Utils.m_subp_run(
            f'{Const.GST_LAUNCH} {self.get_gst_source_elem(url)} do-timestamp=true'
            f' ! queue ! multipartdemux ! image/jpeg,framerate={self.config.v_fps}'
            f' ! decodebin ! videoconvert ! {sink_args}')
        self.subp.append(p)

    def video_launch_ffmpeg(self, url: str, sink_dev: str):
        self.logger.info(f'video launch (ffmpeg) to sink: {sink_dev} ..')

        p = Utils.m_subp_run(f'{Const.FFMPEG} -i "{url}" -c:v copy -f v4l2 {sink_dev}')
        self.subp.append(p)

    def video_play_mpv(self, url: str):
        self.logger.info('video play with mpv ..')

        p = Utils.m_subp_run(f'{Const.MPV} {url} --no-cache --untimed --video-sync=audio'
                             f' --no-demuxer-thread --vd-lavc-threads=1')
        self.subp.append(p)

    def get_url_base(self, with_auth: bool = True) -> str:
        c = self.config
        url_base = 'https://' if c.tls else 'http://'

        if with_auth and (c.username or c.password):
            url_base += f'{c.username}:{c.password}@'

        return f'{url_base}{c.ip}:{c.port}'

    def setup(self, mode: str):
        c = self.config

        if c.use_adb:
            self.adb_forward()

        url_base = self.get_url_base()
        url_video = f'{url_base}/video'
        url_audio = f'{url_base}/audio.{c.a_codec}'

        self.logger.info(f'>> web control: {url_base}')

        if c.audio and Utils.check_url(url_audio, c.ssl_strict):
            if mode == 'test':
                self.audio_launch_gst(url_audio)
            else:
                self.virtual_mic_setup(c.sinkname)
                self.audio_launch_gst(url_audio, c.sinkname)

        if c.video and Utils.check_url(url_video, c.ssl_strict):
            mode_methods = {
                'test': ('gst', 'mpv'),
                'run': ('gst', 'ffmpeg'),
            }

            method_handlers = {
                'gst': self.video_launch_gst,
                'ffmpeg': self.video_launch_ffmpeg,
                'mpv': self.video_play_mpv,
            }

            if not (mode in mode_methods and c.v_method in mode_methods[mode]):
                self.logger.warning(f'mode {mode}: {c.v_method} method not supported')
                return

            if mode == 'test':
                method_handlers[c.v_method](url_video)
            else:
                self.load_kmod_v4l2loopback()

                sink_dev = self.get_v4l2_virtual_dev()
                if not sink_dev:
                    self.logger.warning('failed to find v4l2 device')
                    return

                method_handlers[c.v_method](url_video, sink_dev)

    def cleanup(self, mode: str):
        if mode == 'run' and self.config.audio:
            self.virtual_mic_cleanup()

        if self.config.use_adb:
            self.adb_forward_reset()

    def run(self, argv: List[str]):
        try:
            args = self.parse_args(argv)
            self.config = Config.model_validate(vars(args))
        except Exception as e:
            print(f'** failed to parse args and init config: {e}')
            return

        Utils.config_logging(logging.getLevelName(self.config.loglevel.upper()))

        self.logger.debug(f'config:\n{self.config.model_dump_json(indent=2)}')
        self.setup(args.mode)

        for p in self.subp:
            with contextlib.suppress(KeyboardInterrupt):
                p.wait()

        self.subp.clear()
        self.cleanup(args.mode)


if __name__ == '__main__':
    webcam = Webcam()
    webcam.run(sys.argv[1:])
