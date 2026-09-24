"""zip全体をダウンロードせず、central directory と個別エントリだけを
HTTP Range リクエストで読み込むための汎用ユーティリティ。

1zipに大量のタイル/ファイルが入っている配布形式（長野県砂防課DEM等）で、
必要な1ファイルだけを部分取得するために使う。データソース固有のロジック
（タイル座標計算・URL索引）は呼び出し側（nagano_sabo.py等）が持つ。

前提: 配布サーバがHTTP Rangeリクエスト（206 Partial Content）に対応していること。
2026-09-23、geospatial.jp経由のS3配信（CKAN redirect / 直リンクとも）で対応を確認済み。

速度最適化: CKAN経由のURLはS3プレサインURLへの302リダイレクトを挟むため、
read()のたびに元URLへアクセスすると毎回リダイレクト解決の往復が発生する。
初回のリダイレクト解決結果（resp.geturl()）を使い回し、以降は解決済みURLへ
直接アクセスすることでこの往復を省く（失効時は元URLから自動的に再解決）。
接続そのもの（TCP/TLS）の使い回しはしていない（urllib.requestの標準機能の
範囲内に留め、http.clientでの手動コネクション管理は行わない）。
"""

import io
import time
import urllib.error
import urllib.request
import zipfile

_USER_AGENT = "Mozilla/5.0 (compatible; QGIS plugin)"
_CHUNK_SIZE = 65536  # vs_lp.py の _download と同じチャンクサイズ


class Cancelled(Exception):
    """cancel_cb がキャンセルを示した場合に read() から送出される。"""


class RemoteZipFile:
    """HTTP Range経由でランダムアクセス読み込みできる file-like オブジェクト。
    zipfile.ZipFile にそのまま渡せる（read/seek/tell のみのダックタイピング、
    io.RawIOBase は継承しない。環境によりRawIOBase経由のread呼び出しが
    不安定になるケースがあったため単純なオブジェクトにしている）。

    cancel_cb を渡すと、大きなエントリ（タイル本体等）のダウンロード中も
    チャンク単位（65536バイト）でキャンセルを確認できる。cancel_cb は
    呼び出しごとにQtのイベント処理（processEvents）も行う想定で、これが
    無いとキャンセルボタンのクリック自体がイベントループに届かず、
    ダウンロード完了までUIが反応しなくなる。"""

    def __init__(self, url, cancel_cb=None):
        self._chunk_cb = None  # fetch_entry_bytes 等がエントリ本体の読み込み直前に設定する
        # HEAD は一部の配信元（CKAN経由のS3プレサインURL等）で403を返すため使わない。
        # 2026-09-23、geospatial.jp配信で実際にHEAD=403 / GET+Range=206を確認済み。
        # 1バイトのRange GETでサイズを取得する（Content-Range: "bytes 0-0/12345"）。
        self.url = url
        self._cancel_cb = cancel_cb
        # CKAN経由のURLはリダイレクト先(S3プレサインURL)まで毎回解決すると
        # 往復が余分にかかる。初回のリダイレクト解決結果を使い回し、以降の
        # read() は解決済みURLへ直接アクセスする（プレサインURLの有効期限は
        # 1時間あり、1インスタンスの生存期間内での使い回しは問題にならない）。
        self._resolved_url = url
        self._pos = 0
        req = urllib.request.Request(
            url, headers={"User-Agent": _USER_AGENT, "Range": "bytes=0-0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
            self._resolved_url = resp.geturl() or url
            content_range = resp.headers.get("Content-Range")
            if content_range and "/" in content_range:
                self._size = int(content_range.rsplit("/", 1)[-1])
            else:
                size = resp.headers.get("Content-Length")
                if size is None:
                    raise RuntimeError(f"Size unavailable: {url}")
                self._size = int(size)

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._size + offset
        return self._pos

    def tell(self):
        return self._pos

    def _open_range(self, url, start, end):
        req = urllib.request.Request(
            url, headers={"User-Agent": _USER_AGENT, "Range": f"bytes={start}-{end}"}
        )
        return urllib.request.urlopen(req, timeout=30)  # nosec B310

    def read(self, n=-1):
        end = (self._size - 1) if (n is None or n < 0) else min(self._pos + n, self._size) - 1
        if end < self._pos:
            return b""
        try:
            resp = self._open_range(self._resolved_url, self._pos, end)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404) and self._resolved_url != self.url:
                # 解決済みURL(プレサインURL等)が失効した場合、元URLから再解決する。
                resp = self._open_range(self.url, self._pos, end)
                self._resolved_url = resp.geturl() or self.url
            else:
                raise

        # チャンク単位で読み、cancel_cb でキャンセルを確認できるようにする。
        # 小さいRange（central directory確認等）は数チャンクで終わるが、
        # タイル本体（数十〜百MB超）はここで長時間ブロックし得るため重要。
        chunks = []
        try:
            with resp:
                while True:
                    if self._cancel_cb and self._cancel_cb():
                        raise Cancelled("download cancelled")
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if self._chunk_cb:
                        self._chunk_cb(len(chunk))
        except Cancelled:
            raise
        data = b"".join(chunks)
        self._pos += len(data)
        return data


def _attach_progress(f, zip_url, total, progress_cb):
    """z.read()直前に呼び、以後のチャンク読み込みをスロットル付きで
    progress_cb("downloading", (zip_url, downloaded, total)) として通知する。
    totalはcompress_size（実際にネットワーク転送されるバイト数。展開後サイズではない）。"""
    downloaded = 0
    last_report = 0.0

    def _chunk_cb(n):
        nonlocal downloaded, last_report
        downloaded += n
        now = time.monotonic()
        if now - last_report >= 0.15 or downloaded >= total:
            last_report = now
            progress_cb("downloading", (zip_url, downloaded, total))

    progress_cb("downloading", (zip_url, 0, total))
    f._chunk_cb = _chunk_cb


def fetch_entry_bytes(zip_url: str, name_suffix: str, progress_cb=None, cancel_cb=None):
    """zip_url内で末尾が name_suffix と一致する最初のエントリを読み込んで返す。
    見つからない・取得失敗・キャンセルの場合は None。

    progress_cb(phase, info) を渡すと段階を通知する:
        phase="checking"    info=zip_url                    central directory 確認中（対象が実在するか）
        phase="downloading" info=(zip_url, downloaded, total)  対象エントリの実データ取得中。
                             downloaded/totalは実転送バイト数（compress_size）で、
                             チャンク単位（0.15秒間隔にスロットル）で更新される。
    central directory の確認（実在するかのチェック）と、実データのダウンロードは
    コストが大きく異なる（前者は数KB、後者はタイル1枚分＝数十〜百MB超）ため、
    呼び出し側でメッセージを出し分けられるようにしている。

    cancel_cb を渡すと、central directory確認・実データ取得ともチャンク単位で
    キャンセルを確認する（RemoteZipFile参照）。"""
    try:
        if progress_cb:
            progress_cb("checking", zip_url)
        f = RemoteZipFile(zip_url, cancel_cb=cancel_cb)
        with zipfile.ZipFile(f) as z:
            match = [n for n in z.namelist() if n.endswith(name_suffix)]
            if not match:
                return None
            if progress_cb:
                _attach_progress(f, zip_url, z.getinfo(match[0]).compress_size, progress_cb)
            return z.read(match[0])
    except Exception:
        return None


def fetch_entries_bytes(zip_url: str, name_suffixes, progress_cb=None, cancel_cb=None):
    """fetch_entry_bytesの複数エントリ版。central directoryの確認を1回にまとめ、
    互いに関連する複数ファイル（例: 長野オルソのtif本体+tfw世界ファイル）を
    まとめて取得する場合に使う。戻り値は {suffix: bytes}（見つからなかった
    suffixは含まれない）。取得失敗・キャンセルの場合は {}。"""
    result = {}
    try:
        if progress_cb:
            progress_cb("checking", zip_url)
        f = RemoteZipFile(zip_url, cancel_cb=cancel_cb)
        with zipfile.ZipFile(f) as z:
            names = z.namelist()
            for suffix in name_suffixes:
                match = [n for n in names if n.endswith(suffix)]
                if not match:
                    continue
                if progress_cb:
                    _attach_progress(f, zip_url, z.getinfo(match[0]).compress_size, progress_cb)
                result[suffix] = z.read(match[0])
    except Exception:
        return {}
    return result
