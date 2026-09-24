"""最小限の7zコンテナリーダー（地域非依存の共通部品）。

【現状: 呼び出し元なし、意図的に残置】
2026-09-24、長野県オルソ画像の.7z配布分向けに作成したが、同機能（長野の
WODMIエクスポート対応）自体をユーザー判断で廃止したため、現在このモジュールを
使う機能は無い。それでも削除せず残しているのは、追加依存ゼロで実データ検証済み
（下記参照）の汎用7zリーダーとして、配布形式が.7zの他データソースに将来
遭遇した場合に再利用できるため。長野オルソのような「配布形式がzipから.7zに
変わる／一部地域だけ.7zになる」パターンが今後も起きない限りは、当面使わない
見込み。動作原理の記録として、また将来同種の要件が出た際の出発点として残す。

7zはzipと違い、標準でファイルを1本のLZMA/LZMA2ストリームへまとめて圧縮する
「ソリッド圧縮」を使う（2026-09-24、長野オルソの.7z配布分を実機で確認、
`7z l -slt`で`Solid = +`・全ファイルが`Block = 0`を共有）。ソリッド圧縮では
個々のファイルを他とは独立に取り出すことができず（LZMAの辞書状態が前の
ファイルのデータに依存するため）、zip側のremote_zip.pyのようなHTTP Rangeに
よる「必要な1ファイルだけ部分取得」はできない。よってこのモジュールは
「アーカイブ全体（の対象フォルダ）をダウンロード→まとめて解凍→中の全ファイルを
書き出す」方式を取る。呼び出し側は不要なファイルを解凍後に削除する想定。

対応範囲（意図的に限定）:
    - 単一コーダーのフォルダのみ（LZMA2 または LZMA1）。BCJ/Delta等のフィルタ
      チェーンや複数コーダーの複合フォルダ、暗号化アーカイブは非対応（該当
      した場合は例外を送出し、誤った結果を静かに返すことを避ける）。
    - 上記制約は、長野県オルソ画像配布の.7z（Adobe系ツールでの単純な
      LZMA2ソリッド圧縮のみ）で実機確認した範囲に対応するためのもの。

依存はPython標準ライブラリのlzmaのみ（py7zr等は導入しない。py7zrは
pycryptodomex/brotli/pyppmd等コンパイル済み依存が多く、実際に使う機能
（無圧縮鍵なしLZMA2ソリッド）に対して過大なため、2026-09-24に自作した）。

コンテナ構造の解析は公開されている7zフォーマット仕様（7-Zipの7zFormat.txt）
に基づく。2026-09-24、実データ（下條村.7z、845MB）で全量ダウンロード＋
解凍→タイル座標をnagano_sabo.tile_bbox()と突合し完全一致を確認済み。
"""

import lzma
import struct
import urllib.request

_USER_AGENT = "Mozilla/5.0 (compatible; QGIS plugin)"
_CHUNK_SIZE = 65536

# property IDs（7zFormat.txt準拠）
_K_END = 0x00
_K_HEADER = 0x01
_K_MAIN_STREAMS_INFO = 0x04
_K_FILES_INFO = 0x05
_K_PACK_INFO = 0x06
_K_UNPACK_INFO = 0x07
_K_SUBSTREAMS_INFO = 0x08
_K_SIZE = 0x09
_K_CRC = 0x0A
_K_FOLDER = 0x0B
_K_CODERS_UNPACK_SIZE = 0x0C
_K_NUM_UNPACK_STREAM = 0x0D
_K_EMPTY_STREAM = 0x0E
_K_EMPTY_FILE = 0x0F
_K_NAME = 0x11
_K_ENCODED_HEADER = 0x17

_LZMA2_ID = b"\x21"
_LZMA1_ID = b"\x03\x01\x01"


class Cancelled(Exception):
    """cancel_cb がキャンセルを示した場合に送出される。"""


class UnsupportedArchive(Exception):
    """対応範囲外の7z構造（複合コーダー・未知コーデック・暗号化等）。"""


class _Reader:
    """バイト列に対する7z独自の可変長整数(Number)・ビットベクトル読み取り。"""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b

    def bytes(self, n: int) -> bytes:
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def number(self) -> int:
        first = self.byte()
        mask = 0x80
        value = 0
        for i in range(8):
            if (first & mask) == 0:
                value |= (first & (mask - 1)) << (8 * i)
                return value
            value |= (self.byte() << (8 * i))
            mask >>= 1
        return value

    def bitvector(self, n: int):
        bits = []
        b = 0
        mask = 0
        for _ in range(n):
            if mask == 0:
                b = self.byte()
                mask = 0x80
            bits.append(bool(b & mask))
            mask >>= 1
        return bits

    def all_or_bitvector(self, n: int):
        if self.byte() != 0:
            return [True] * n
        return self.bitvector(n)


def _parse_folder(r: _Reader):
    num_coders = r.number()
    if num_coders != 1:
        raise UnsupportedArchive(f"multi-coder folder not supported (num_coders={num_coders})")
    flags = r.byte()
    id_size = flags & 0x0F
    is_complex = bool(flags & 0x10)
    has_attrs = bool(flags & 0x20)
    if is_complex:
        raise UnsupportedArchive("complex (multi in/out stream) coder not supported")
    codec_id = r.bytes(id_size)
    props = r.bytes(r.number()) if has_attrs else b""
    return {"codec_id": codec_id, "props": props}


def _parse_unpack_info(r: _Reader):
    if r.byte() != _K_FOLDER:
        raise UnsupportedArchive("expected kFolder")
    num_folders = r.number()
    if r.byte() != 0:
        raise UnsupportedArchive("external folder data not supported")
    folders = [_parse_folder(r) for _ in range(num_folders)]

    if r.byte() != _K_CODERS_UNPACK_SIZE:
        raise UnsupportedArchive("expected kCodersUnpackSize")
    for f in folders:
        f["unpack_size"] = r.number()  # 単一コーダー前提なのでout streamは1個のみ

    tag = r.byte()
    if tag == _K_CRC:
        for defined in r.all_or_bitvector(num_folders):
            if defined:
                r.bytes(4)
        tag = r.byte()
    if tag != _K_END:
        raise UnsupportedArchive("expected kEnd after UnpackInfo")
    return folders


def _parse_pack_info(r: _Reader):
    pack_pos = r.number()
    num_pack_streams = r.number()
    sizes = None
    tag = r.byte()
    while tag != _K_END:
        if tag == _K_SIZE:
            sizes = [r.number() for _ in range(num_pack_streams)]
        elif tag == _K_CRC:
            for defined in r.all_or_bitvector(num_pack_streams):
                if defined:
                    r.bytes(4)
        else:
            raise UnsupportedArchive(f"unexpected tag in PackInfo: {hex(tag)}")
        tag = r.byte()
    return pack_pos, sizes


def _parse_substreams_info(r: _Reader, folders):
    num_unpack_streams = [1] * len(folders)
    tag = r.byte()
    if tag == _K_NUM_UNPACK_STREAM:
        num_unpack_streams = [r.number() for _ in folders]
        tag = r.byte()

    sizes_per_folder = []
    for fi, f in enumerate(folders):
        n = num_unpack_streams[fi]
        if n == 0:
            sizes_per_folder.append([])
            continue
        sizes = []
        if tag == _K_SIZE:
            remaining = f["unpack_size"]
            for _ in range(n - 1):
                s = r.number()
                sizes.append(s)
                remaining -= s
            sizes.append(remaining)
        else:
            sizes.append(f["unpack_size"])
        sizes_per_folder.append(sizes)
    if tag == _K_SIZE:
        tag = r.byte()

    if tag == _K_CRC:
        total_streams = sum(num_unpack_streams)
        for defined in r.all_or_bitvector(total_streams):
            if defined:
                r.bytes(4)
        tag = r.byte()

    if tag != _K_END:
        raise UnsupportedArchive("expected kEnd after SubStreamsInfo")
    return sizes_per_folder


def _parse_streams_info(r: _Reader):
    pack_pos = pack_sizes = None
    folders = []
    substream_sizes = None
    tag = r.byte()
    if tag == _K_PACK_INFO:
        pack_pos, pack_sizes = _parse_pack_info(r)
        tag = r.byte()
    if tag == _K_UNPACK_INFO:
        folders = _parse_unpack_info(r)
        tag = r.byte()
    if tag == _K_SUBSTREAMS_INFO:
        substream_sizes = _parse_substreams_info(r, folders)
        tag = r.byte()
    if substream_sizes is None:
        substream_sizes = [[f["unpack_size"]] for f in folders]
    if tag != _K_END:
        raise UnsupportedArchive("expected kEnd after StreamsInfo")
    return {"pack_pos": pack_pos, "pack_sizes": pack_sizes,
            "folders": folders, "substream_sizes": substream_sizes}


def _parse_files_info(r: _Reader):
    num_files = r.number()
    empty_stream = [False] * num_files
    names = []
    while True:
        prop_type = r.number()
        if prop_type == _K_END:
            break
        size = r.number()
        end_pos = r.pos + size
        if prop_type == _K_EMPTY_STREAM:
            empty_stream = r.bitvector(num_files)
        elif prop_type == _K_NAME:
            if r.byte() != 0:
                raise UnsupportedArchive("external name data not supported")
            raw = r.bytes(end_pos - r.pos)
            names = raw.decode("utf-16-le").split("\x00")[:num_files]
        r.pos = end_pos
    return num_files, empty_stream, names


def _lzma2_dict_size(props_byte: int) -> int:
    if props_byte > 40:
        raise UnsupportedArchive("invalid LZMA2 dict size byte")
    if props_byte == 40:
        return 0xFFFFFFFF
    return (2 | (props_byte & 1)) << (props_byte // 2 + 11)


def _lzma_filters(coder: dict):
    codec_id = coder["codec_id"]
    if codec_id == _LZMA2_ID:
        return [{"id": lzma.FILTER_LZMA2, "dict_size": _lzma2_dict_size(coder["props"][0])}]
    if codec_id == _LZMA1_ID:
        props = coder["props"]
        d = props[0]
        lc, d = d % 9, d // 9
        lp, pb = d % 5, d // 5
        dict_size = struct.unpack("<I", props[1:5])[0]
        return [{"id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb, "dict_size": dict_size}]
    raise UnsupportedArchive(f"unsupported codec: {codec_id.hex()}")


class _RangeFetcher:
    """7z解析用の小さなRange取得ヘルパー（remote_zip.RemoteZipFileの簡易版、
    こちらはランダムアクセスではなく明示的な(start, end)取得のみでよいため
    file-likeにはしていない）。"""

    def __init__(self, url):
        self.url = url
        self._resolved_url = url

    def size(self) -> int:
        req = urllib.request.Request(
            self.url, headers={"User-Agent": _USER_AGENT, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
            self._resolved_url = resp.geturl() or self.url
            cr = resp.headers.get("Content-Range")
            if cr and "/" in cr:
                return int(cr.rsplit("/", 1)[-1])
            size = resp.headers.get("Content-Length")
            if size is None:
                raise RuntimeError(f"size unavailable: {self.url}")
            return int(size)

    def get(self, start: int, end: int) -> bytes:
        req = urllib.request.Request(
            self._resolved_url,
            headers={"User-Agent": _USER_AGENT, "Range": f"bytes={start}-{end}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:  # nosec B310
            return resp.read()

    def get_chunked(self, start: int, end: int, cancel_cb=None, progress_cb=None) -> bytes:
        req = urllib.request.Request(
            self._resolved_url,
            headers={"User-Agent": _USER_AGENT, "Range": f"bytes={start}-{end}"},
        )
        total = end - start + 1
        chunks = []
        downloaded = 0
        with urllib.request.urlopen(req, timeout=600) as resp:  # nosec B310
            while True:
                if cancel_cb and cancel_cb():
                    raise Cancelled("download cancelled")
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    progress_cb(downloaded, total)
        return b"".join(chunks)


def list_archive(url: str, cancel_cb=None):
    """アーカイブのファイル一覧とストリーム構造を取得する（ヘッダ部のみ取得、
    本体はダウンロードしない）。戻り値の辞書は extract_all() にそのまま渡せる。"""
    fetcher = _RangeFetcher(url)
    total_size = fetcher.size()
    head = fetcher.get(0, 31)
    if head[0:6] != b"7z\xbc\xaf\x27\x1c":
        raise UnsupportedArchive("not a 7z file (bad signature)")
    next_header_offset = struct.unpack("<Q", head[12:20])[0]
    next_header_size = struct.unpack("<Q", head[20:28])[0]
    hdr_bytes = fetcher.get(32 + next_header_offset, 32 + next_header_offset + next_header_size - 1)

    r = _Reader(hdr_bytes)
    tag = r.byte()
    if tag == _K_ENCODED_HEADER:
        si = _parse_streams_info(r)
        if len(si["folders"]) != 1:
            raise UnsupportedArchive("multi-folder encoded header not supported")
        folder = si["folders"][0]
        pack_start = 32 + si["pack_pos"]
        pack_size = si["pack_sizes"][0]
        packed = fetcher.get(pack_start, pack_start + pack_size - 1)
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=_lzma_filters(folder))
        real_header = dec.decompress(packed, max_length=folder["unpack_size"])
        r = _Reader(real_header)
        tag = r.byte()

    if tag != _K_HEADER:
        raise UnsupportedArchive(f"unexpected top-level tag: {hex(tag)}")
    tag = r.byte()
    streams_info = None
    num_files = empty_stream = names = None
    while tag != _K_END:
        if tag == _K_MAIN_STREAMS_INFO:
            streams_info = _parse_streams_info(r)
        elif tag == _K_FILES_INFO:
            num_files, empty_stream, names = _parse_files_info(r)
        else:
            raise UnsupportedArchive(f"unexpected top-level tag: {hex(tag)}")
        tag = r.byte()

    # ファイル名(空ストリームのディレクトリ含む全件)を、実データを持つ
    # ファイルだけの順序(substream_sizesと1:1対応)に絞り込む。
    data_names = [n for n, empty in zip(names, empty_stream) if not empty]

    return {
        "resolved_url": fetcher._resolved_url,
        "total_size": total_size,
        "streams_info": streams_info,
        "data_names": data_names,
    }


def extract_all(archive_info: dict, out_dir: str, cancel_cb=None, progress_cb=None) -> dict:
    """list_archive()の戻り値を使い、アーカイブ内の全ファイルをout_dirへ書き出す。
    戻り値は {相対パス: 絶対パス}。

    progress_cb(phase, downloaded, total) で段階を通知する:
        phase="downloading"     ソリッドブロック本体のダウンロード中
        phase="decompressing"   解凍中（チャンク単位、downloaded/totalは解凍後バイト数）
    ソリッド圧縮のため、1ファイルだけ欲しい場合でも該当フォルダ全体の
    ダウンロード・解凍が必要（モジュールdocstring参照）。"""
    import os

    si = archive_info["streams_info"]
    fetcher = _RangeFetcher(archive_info["resolved_url"])
    result = {}
    file_idx = 0
    for fi, folder in enumerate(si["folders"]):
        pack_start = 32 + sum(si["pack_sizes"][:fi]) + si["pack_pos"]
        pack_size = si["pack_sizes"][fi]

        def _dl_progress(downloaded, total, _fi=fi):
            if progress_cb:
                progress_cb("downloading", downloaded, total)

        packed = fetcher.get_chunked(pack_start, pack_start + pack_size - 1,
                                      cancel_cb=cancel_cb, progress_cb=_dl_progress)

        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=_lzma_filters(folder))
        raw = bytearray()
        step = 32 * 1024 * 1024
        for off in range(0, len(packed), step):
            if cancel_cb and cancel_cb():
                raise Cancelled("decompress cancelled")
            raw.extend(dec.decompress(packed[off:off + step]))
            if progress_cb:
                progress_cb("decompressing", len(raw), folder["unpack_size"])
        if len(raw) != folder["unpack_size"]:
            raise UnsupportedArchive(
                f"decompressed size mismatch: got {len(raw)}, expected {folder['unpack_size']}")

        pos = 0
        for size in si["substream_sizes"][fi]:
            name = archive_info["data_names"][file_idx]
            file_idx += 1
            dest = os.path.join(out_dir, name.replace("\\", "/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(raw[pos:pos + size])
            result[name] = dest
            pos += size
    return result
