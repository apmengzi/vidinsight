"""下载参数拼装单测（stub 网络，不发起任何真实请求）。

覆盖：
- 默认值：B站 dash 单文件场景实测最优 = 不分块、不并发
- BILI_DL_CHUNK_MB / BILI_DL_CONC_FRAG 环境变量覆盖（含非法值回退）
- DOWNLOAD_PROXY_URL 的接入与"不自动继承全局代理"
- ffmpeg 抽音频参数（16kHz 单声道）
- 两个 downloader 真实 opts 拼装结果（YoutubeDL 用 stub 替换）
"""
import sys
import types

import pytest

from app.downloaders import dl_opts


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("BILI_DL_CHUNK_MB", "BILI_DL_CONC_FRAG", "DOWNLOAD_PROXY_URL",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)


class TestDefaults:
    def test_defaults_are_single_connection(self):
        """实测：1MB 分块 0.90~1.48s，不分块 0.17~0.37s → 默认必须全关。"""
        assert dl_opts.DEFAULT_CHUNK_MB == 0
        assert dl_opts.DEFAULT_CONC_FRAG == 0
        assert dl_opts.get_chunk_mb() == 0
        assert dl_opts.get_conc_frag() == 0

    def test_transfer_opts_empty_by_default(self):
        assert dl_opts.build_transfer_opts() == {}

    def test_no_proxy_by_default_even_if_global_proxy_set(self, monkeypatch):
        """下载器不应自动继承全局 HTTP_PROXY（会把国内 CDN 误送出国）。"""
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
        assert dl_opts.get_download_proxy() is None
        assert "proxy" not in dl_opts.build_transfer_opts()


class TestEnvOverride:
    def test_chunk_mb_env(self, monkeypatch):
        monkeypatch.setenv("BILI_DL_CHUNK_MB", "5")
        opts = dl_opts.build_transfer_opts()
        assert opts["http_chunk_size"] == 5 * 1024 * 1024

    def test_conc_frag_env(self, monkeypatch):
        monkeypatch.setenv("BILI_DL_CONC_FRAG", "8")
        assert dl_opts.build_transfer_opts()["concurrent_fragment_downloads"] == 8

    def test_both_env(self, monkeypatch):
        monkeypatch.setenv("BILI_DL_CHUNK_MB", "10")
        monkeypatch.setenv("BILI_DL_CONC_FRAG", "4")
        opts = dl_opts.build_transfer_opts()
        assert opts["http_chunk_size"] == 10 * 1024 * 1024
        assert opts["concurrent_fragment_downloads"] == 4

    @pytest.mark.parametrize("bad", ["", "abc", "-3", "3.5"])
    def test_invalid_chunk_env_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("BILI_DL_CHUNK_MB", bad)
        assert dl_opts.get_chunk_mb() == 0
        assert "http_chunk_size" not in dl_opts.build_transfer_opts()

    def test_explicit_zero_disables(self, monkeypatch):
        monkeypatch.setenv("BILI_DL_CHUNK_MB", "5")
        # 显式传参优先于环境变量（给单测/调用方留的手动开关）
        assert dl_opts.build_transfer_opts(chunk_mb=0) == {}

    def test_download_proxy_env(self, monkeypatch):
        monkeypatch.setenv("DOWNLOAD_PROXY_URL", " http://vps.example.com:3128 ")
        assert dl_opts.get_download_proxy() == "http://vps.example.com:3128"
        assert dl_opts.build_transfer_opts()["proxy"] == "http://vps.example.com:3128"

    def test_blank_proxy_ignored(self, monkeypatch):
        monkeypatch.setenv("DOWNLOAD_PROXY_URL", "   ")
        assert dl_opts.get_download_proxy() is None


class TestAudioPostprocessor:
    def test_downsample_args_present(self):
        args = dl_opts.build_postprocessor_args()
        assert "extractaudio+ffmpeg" in args
        argv = args["extractaudio+ffmpeg"]
        assert argv[argv.index("-ar") + 1] == "16000"
        assert argv[argv.index("-ac") + 1] == "1"

    def test_pp_dict_has_no_unknown_keys(self):
        """FFmpegExtractAudioPP 只认 3 个参数，多余键会让真实下载直接崩。"""
        pp = dl_opts.build_audio_postprocessors()
        assert len(pp) == 1
        assert pp[0]["key"] == "FFmpegExtractAudio"
        assert pp[0]["preferredcodec"] == "mp3"
        assert pp[0]["preferredquality"] == "64"
        assert set(pp[0]) == {"key", "preferredcodec", "preferredquality"}

    def test_real_ytdlp_accepts_generated_pp(self):
        """用 yt-dlp 真实构造器实例化，防止再出现 unexpected keyword argument。

        走 ``add_post_processor``（yt-dlp 内部构造后处理器走的正是这里），
        键名不合法会在这一步直接抛 TypeError。
        """
        import yt_dlp
        from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP

        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            for spec in dl_opts.build_audio_postprocessors():
                kwargs = {k: v for k, v in spec.items() if k != "key"}
                pp = FFmpegExtractAudioPP(ydl, **kwargs)
                ydl.add_post_processor(pp)
                assert pp.pp_key() == "ExtractAudio"

    def test_real_ytdlp_resolves_downsample_args(self):
        """顶层 postprocessor_args 必须真的被 ExtractAudio 读到。"""
        from yt_dlp.utils import _configuration_args

        argv = _configuration_args(
            "ExtractAudio", dl_opts.build_postprocessor_args(), "ffmpeg")
        assert argv == ["-ar", "16000", "-ac", "1"]

    def test_quality_override(self):
        assert dl_opts.build_audio_postprocessors("128")[0]["preferredquality"] == "128"

    def test_apply_audio_opts(self):
        opts = dl_opts.apply_audio_opts({})
        assert opts["postprocessors"][0]["preferredcodec"] == "mp3"
        assert "extractaudio+ffmpeg" in opts["postprocessor_args"]


class TestBilibiliOpts:
    def _captured_opts(self, monkeypatch, method, *args, **kwargs):
        """跑 downloader 的真实 opts 构造，把传给 YoutubeDL 的 opts 截下来。"""
        captured = {}

        class FakeYDL:
            def __init__(self, opts):
                captured.update(opts)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True):
                # download_video 会校验落盘文件，stub 里按 yt-dlp 的
                # outtmpl 规则补一个空文件，不触碰网络
                out = captured.get("outtmpl", "")
                if "%(id)s" in out:
                    path = out.replace("%(id)s", "BVtest").replace("%(ext)s", "mp4")
                    open(path, "wb").close()
                return {"id": "BVtest", "title": "t", "duration": 1}

        monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
        from app.downloaders.bilibili_downloader import BilibiliDownloader

        dl = BilibiliDownloader()
        monkeypatch.setattr(dl, "_cookiefile", None, raising=False)
        getattr(dl, method)(*args, **kwargs)
        return captured

    def test_download_opts_default(self, monkeypatch, tmp_path):
        opts = self._captured_opts(
            monkeypatch, "download", "https://www.bilibili.com/video/BVtest",
            output_dir=str(tmp_path))
        assert opts["format"] == "bestaudio[ext=m4a]/bestaudio/best"
        assert "http_chunk_size" not in opts
        assert "concurrent_fragment_downloads" not in opts
        assert "proxy" not in opts
        pp = opts["postprocessors"][0]
        assert pp["preferredcodec"] == "mp3"
        assert "extractaudio+ffmpeg" in opts["postprocessor_args"]

    def test_download_opts_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BILI_DL_CHUNK_MB", "5")
        monkeypatch.setenv("BILI_DL_CONC_FRAG", "4")
        monkeypatch.setenv("DOWNLOAD_PROXY_URL", "http://vps:8080")
        opts = self._captured_opts(
            monkeypatch, "download", "https://www.bilibili.com/video/BVtest",
            output_dir=str(tmp_path))
        assert opts["http_chunk_size"] == 5 * 1024 * 1024
        assert opts["concurrent_fragment_downloads"] == 4
        assert opts["proxy"] == "http://vps:8080"

    def test_download_video_opts_has_transfer_opts(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BILI_DL_CONC_FRAG", "8")
        opts = self._captured_opts(
            monkeypatch, "download_video",
            "https://www.bilibili.com/video/BVtest", output_dir=str(tmp_path))
        assert opts["concurrent_fragment_downloads"] == 8
        assert opts["merge_output_format"] == "mp4"

class TestYoutubeOpts:
    def test_download_proxy_takes_priority(self, monkeypatch, tmp_path):
        captured = {}

        class FakeYDL:
            def __init__(self, opts):
                captured.update(opts)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True):
                return {"id": "yt", "title": "t", "duration": 1, "ext": "m4a"}

        monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
        monkeypatch.setattr(
            "app.services.proxy_config_manager.ProxyConfigManager.get_proxy_url",
            lambda self: "http://global-proxy:7890")

        from app.downloaders.youtube_downloader import YoutubeDownloader

        monkeypatch.setenv("DOWNLOAD_PROXY_URL", "http://vps:8080")
        YoutubeDownloader().download("https://youtu.be/x", output_dir=str(tmp_path))
        assert captured["proxy"] == "http://vps:8080"

        captured.clear()
        monkeypatch.delenv("DOWNLOAD_PROXY_URL")
        YoutubeDownloader().download("https://youtu.be/x", output_dir=str(tmp_path))
        assert captured["proxy"] == "http://global-proxy:7890"
