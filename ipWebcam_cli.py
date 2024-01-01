#!/usr/bin/env python

import sys
import argparse
import logging
import subprocess
import requests

from typing import List
from pydantic import BaseModel, Field

from urllib.parse import urlparse, urlunparse


class Const:
    GST_LAUNCH = 'gst-launch-1.0 -q --no-position'
    FFMPEG = 'ffmpeg -nostdin -loglevel warning'
    MPV = 'mpv'


class Utils:

    @staticmethod
    def parse_args(argv: List[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog='ipWebcam_cli',
            description='script to use IPWebcam as v4l2 webcam / sound source')

        parser.add_argument('--ip', help='webcam ip addr', type=str)
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
                            help='audio codec',
                            dest='a_codec',
                            choices=['wav', 'aac', 'opus'],
                            default='opus')
        parser.add_argument('--vmethod',
                            help='video capture method',
                            dest='v_method',
                            choices=['ffmpeg', 'gst'],
                            default='ffmpeg')
        parser.add_argument('--fps',
                            help='gst video fps',
                            dest='v_fps',
                            choices=[30, 60],
                            default=60,
                            type=int)
        parser.add_argument('--sync', help='enable gst timesync', action='store_true')

        parser.add_argument('op', help='webcam operation', choices=['run', 'test'])

        return parser.parse_args(argv)

    @staticmethod
    def config_logging(loglevel=logging.DEBUG):

        class SingleCharFormatter(logging.Formatter):

            def format(self, record):    # noqa: A003
                record.levelname = record.levelname[0]
                return super().format(record)

        logger = logging.getLogger()
        logger.setLevel(loglevel)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(loglevel)
        console_handler.setFormatter(
            SingleCharFormatter('%(asctime)s.%(msecs)d %(levelname)s %(name)s: %(message)s',
                                datefmt='%Y%m%d_%H%M%S'))

        logger.addHandler(console_handler)

        # silent some other no care messages
        logging.getLogger('urllib3').setLevel(logging.WARNING)

        import urllib3
        urllib3.disable_warnings()

    @staticmethod
    def m_subp_run(cmd: str, wait: bool = False) -> subprocess.Popen:
        logging.getLogger('subp').debug(cmd)
        p = subprocess.Popen(cmd, shell=True)
        if p and wait:
            p.wait()
        return p

    @staticmethod
    def check_http_url(url: str, ssl_strict: bool = True) -> bool:
        try:
            resp = requests.get(url, timeout=3, stream=True, verify=ssl_strict)
            resp.raise_for_status()
        except requests.RequestException as e:
            logging.debug(f'failed to access URL({url}), {e}')
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

    tls: bool = Field(default=True, description='use https')
    ssl_strict: bool = Field(default=True, description='strict SSL certificate checking')

    sync: bool = Field(default=False, description='enable gst A/V timesync')
    audio: bool = Field(default=True, description='enable audio capture')
    video: bool = Field(default=True, description='enable video capture')
    v_fps: int = Field(default=60, description='video source FPS')
    v_method: str = Field(default='ffmpeg',
                          pattern='(ffmpeg)|(gst)',
                          description='video capture method')
    a_codec: str = Field(default='opus',
                         pattern='(opus)|(aac)|(wav)',
                         description='audio codec to seclect the stream')
    sinkname: str = Field(default='IPWebcam', description='audio sink name')


class Webcam:
    config: Config
    subp: List[subprocess.Popen] = []

    def __init__(self):
        Utils.config_logging()
        self.logger = logging.getLogger('webcam')

    def load_kmod_v4l2loopback(self):
        self.logger.info('load kernel module: v4l2loopback ..')

        v4l2_args = 'exclusive_caps=1'
        Utils.m_subp_run(
            f'lsmod | grep -q v4l2loopback || sudo modprobe v4l2loopback {v4l2_args}', True)

    def get_v4l2_virtual_dev_last(self) -> str:
        p = subprocess.Popen('ls /sys/devices/virtual/video4linux/'.split(),
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
        out, _ = p.communicate()
        return f'/dev/{out.decode().split()[-1]}' if out else ''

    def get_gst_source_elem(self, url: str) -> str:
        return (f'souphttpsrc location="{url}" do-timestamp=true is-live=true'
                f' ssl-strict={self.config.ssl_strict}')

    def audio_launch_gst(self, url: str, sink_name: str):
        self.logger.info(f'audio launch (gst) to sink: {sink_name} ..')

        p = Utils.m_subp_run(
            f'{Const.GST_LAUNCH} {self.get_gst_source_elem(url)}'
            f' ! queue ! decodebin ! pulsesink device="{sink_name}" sync={self.config.sync}')
        self.subp.append(p)

    def video_launch_gst(self, url: str, sink_dev: str):
        self.logger.info(f'video launch (gst) to sink: {sink_dev} ..')

        # TODO: video detect and copy
        p = Utils.m_subp_run(
            f'{Const.GST_LAUNCH} {self.get_gst_source_elem(url)}'
            f' ! queue ! multipartdemux ! image/jpeg,framerate={self.config.v_fps}'
            f' ! decodebin ! videoconvert ! videoscale ! video/x-raw,format=YUY2'
            f' ! v4l2sink device="{sink_dev}" sync={self.config.sync}')
        self.subp.append(p)

    def video_launch_ffmpeg(self, url: str, sink_dev: str):
        self.logger.info(f'video launch (ffmpeg) to sink: {sink_dev} ..')

        p = Utils.m_subp_run(f'{Const.FFMPEG} -i "{url}" -v copy -f v4l2 {sink_dev}')
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

        url_base = self.get_url_base()
        url_video = f'{url_base}/video'
        url_audio = f'{url_base}/audio.{c.a_codec}'

        self.logger.info(f'web control: {url_base}')

        if c.audio and Utils.check_http_url(url_audio, c.ssl_strict):
            self.audio_launch_gst(url_audio, c.sinkname)

        if c.video and Utils.check_http_url(url_video, c.ssl_strict):
            if mode == 'test':    # display video without v4l2
                self.video_play_mpv(url_video)
            else:    # launch to v4l2 sink device as virtual webcam
                self.load_kmod_v4l2loopback()

                sink_dev = self.get_v4l2_virtual_dev_last()
                if not sink_dev:
                    return ''

                { # the loader map, maybe lambda style ..
                    'gst': self.video_launch_gst,
                    'ffmpeg': self.video_launch_ffmpeg,
                }[c.v_method](url_video, sink_dev)

    def run(self, argv: List[str]):
        try:
            args = Utils.parse_args(argv)
            self.config = Config.model_validate(vars(args))
        except Exception as e:
            self.logger.error(f'failed to parse args: {e}')
            return

        self.logger.debug(f'config:\n{self.config.model_dump_json(indent=2)}')
        self.setup(args.op)

        for p in self.subp:
            p.wait()


if __name__ == '__main__':
    webcam = Webcam()
    webcam.run(sys.argv[1:])
