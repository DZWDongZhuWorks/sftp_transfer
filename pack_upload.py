"""把 SFTP upload 設定原本會上傳的內容封裝成未壓縮的本地 tar。

此工具不建立任何網路連線。選檔、gitignore 規則、內建 manifest/暫存檔排除，
以及 local_path/remote_path 的多來源映射，都直接沿用 :class:`SFTPUploader`。

與 SFTP 上傳的差別在符號連結：tar 能表達連結本身，因此指向封裝範圍內的
symlink 會原樣保留；指向範圍外（絕對路徑或跳出來源根目錄）的連結若照樣保留，
解開後會指向目的端不存在的路徑，所以仍沿用上傳端的行為改存實際內容。

用法：
    python pack_upload.py --config config/radar_upload_settings.json --output radar.tar
"""

import argparse
import json
import logging
import os
import posixpath
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from settings import PlaceholderError, resolve_placeholders
from uploader import SFTPUploader


PACK_SETTING_FIELDS = ("mode", "local_path", "remote_path", "recursive", "ignore_file")


@dataclass(frozen=True)
class ArchiveFile:
    """一個 tar 檔案成員及其本地來源。"""

    source: Path
    archive_path: str


@dataclass(frozen=True)
class ArchiveDirectory:
    """一個 tar 資料夾成員；source=None 代表只為補齊父目錄而建立。"""

    source: Optional[Path]
    archive_path: str


@dataclass(frozen=True)
class ArchiveSymlink:
    """一個保留原樣的 tar 符號連結成員；target 為連結字面值，不做解析。"""

    source: Path
    archive_path: str
    target: str


@dataclass(frozen=True)
class ArchivePlan:
    """完成 ignore 與目的路徑映射後的封裝計畫。"""

    directories: tuple
    files: tuple
    total_bytes: int
    symlinks: tuple = ()


class _LocalUploadPlanner(SFTPUploader):
    """只重用 uploader 的本地規劃，不允許它碰觸 SFTP。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.planned_remote_dirs = []
        self.planned_symlinks = []
        # 由 build_archive_plan 在走訪每個來源前設定，作為「封裝範圍」的判準。
        self.pack_source_root = None

    def _ensure_remote_dir(self, remote_dir):
        # SFTPUploader._walk_local_dir 會在這裡建立遠端資料夾；封裝模式只記下
        # 同一個目的路徑，稍後轉成 tar 的資料夾成員。
        self.planned_remote_dirs.append(str(remote_dir))

    def _handle_symlink(self, local_path, rel_path):
        local_path = Path(local_path)
        target = os.readlink(local_path)
        if self._points_inside_source(local_path):
            # 目標同在來源樹內，代表它也會被封進同一個 tar，連結解開後依然成立。
            self.planned_symlinks.append((local_path, rel_path))
            return True
        if not local_path.exists():
            # 指向範圍外又是斷鏈：沒有內容可存，保留連結也只會在目的端斷掉。
            self.logger.warning(f"略過指向封裝範圍外的斷鏈符號連結: {rel_path} -> {target}")
            return True
        self.logger.warning(f"符號連結指向封裝範圍外，改存實際內容: {rel_path} -> {target}")
        return False

    def _points_inside_source(self, local_path):
        """連結解析後是否仍落在目前來源根目錄內（含根目錄本身）。"""
        root = self.pack_source_root
        if root is None or os.path.isabs(os.readlink(local_path)):
            return False
        # strict=False：斷鏈連結也要能判斷，指向來源內的斷鏈仍值得原樣保留。
        resolved = local_path.resolve(strict=False)
        return resolved == root or root in resolved.parents


def load_pack_settings(config_path):
    """只載入封裝所需欄位，避免無關的帳密或 device_name 影響本地操作。"""
    path = Path(config_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise ValueError(f"找不到設定檔: {path}") from e
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"設定檔 {path} 讀取失敗: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"設定檔 {path} 內容必須是 JSON 物件")

    selected = {key: data[key] for key in PACK_SETTING_FIELDS if key in data}
    return resolve_placeholders(selected)


def _validate_settings(settings):
    mode = settings.get("mode") or "download"
    if mode != "upload":
        raise ValueError(f"設定檔 mode 必須是 upload，目前為 {mode!r}")

    for field in ("local_path", "remote_path"):
        value = settings.get(field)
        values = value if isinstance(value, list) else [value]
        if not values or any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError(f"設定檔缺少有效的 {field}")

    recursive = settings.get("recursive", True)
    if not isinstance(recursive, bool):
        raise ValueError("設定檔 recursive 必須是 JSON boolean")


def _normal_remote_path(path):
    text = str(path)
    # 保留根目錄 /，其餘尾端斜線不影響 tar 成員映射。
    return posixpath.normpath(text.rstrip("/") or "/")


def _archive_base(jobs):
    """求 tar 要省略的遠端共同父路徑，讓單一工作保留目的資料夾名稱。"""
    roots = [_normal_remote_path(remote_root) for _, remote_root in jobs]
    absolute = {posixpath.isabs(root) for root in roots}
    if len(absolute) != 1:
        raise ValueError("remote_path 不可混用絕對路徑與相對路徑")

    if len(roots) == 1:
        base = posixpath.dirname(roots[0])
    else:
        try:
            common = posixpath.commonpath(roots)
        except ValueError as e:
            raise ValueError(f"無法建立 remote_path 的共同封裝根目錄: {e}") from e
        # 若其中一個工作本身就是共同路徑，仍保留它的資料夾名稱，避免該工作
        # 的檔案散落在 tar 根目錄、其他工作卻位於子資料夾。
        base = posixpath.dirname(common) if common in roots else common
    return base or "."


def _to_archive_path(remote_path, archive_base):
    normalized = _normal_remote_path(remote_path)
    archive_path = posixpath.relpath(normalized, archive_base)
    if archive_path == ".":
        return ""
    pure = PurePosixPath(archive_path)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise ValueError(f"遠端路徑無法安全地映射到 tar: {remote_path}")
    return pure.as_posix()


def _join_remote(remote_root, rel_path):
    root = str(remote_root).rstrip("/")
    return (root + "/" + rel_path) if root else ("/" + rel_path)


def _relative_remote_dir(remote_dir, remote_root):
    remote_dir = _normal_remote_path(remote_dir)
    remote_root = _normal_remote_path(remote_root)
    rel = posixpath.relpath(remote_dir, remote_root)
    if rel == ".":
        return ""
    pure = PurePosixPath(rel)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise ValueError(f"規劃出的遠端資料夾不在目的根目錄內: {remote_dir}")
    return pure.as_posix()


def _resolved_paths(paths):
    return {Path(path).resolve(strict=False) for path in paths}


def _add_parent_directories(directories, archive_path):
    parent = PurePosixPath(archive_path).parent
    while parent.as_posix() not in ("", "."):
        name = parent.as_posix()
        directories.setdefault(name, None)
        parent = parent.parent


def build_archive_plan(settings, excluded_paths=(), logger=None):
    """依 upload 設定建立 tar 成員清單，全程只讀本地檔案系統。"""
    _validate_settings(settings)
    logger = logger or logging.getLogger(__name__)
    planner = _LocalUploadPlanner(
        host="",
        port=0,
        username="",
        remote_path=settings["remote_path"],
        local_path=settings["local_path"],
        recursive=settings.get("recursive", True),
        ignore_file=settings.get("ignore_file") or None,
        logger=logger,
    )
    planner._ignore_spec = planner._load_ignore_spec()
    jobs = planner._build_jobs()
    if jobs is None:
        raise ValueError("local_path 與 remote_path 的配對數量不符")

    all_sources = [Path(local) for sources, _ in jobs for local in sources]
    existing_sources = [source for source in all_sources if source.exists()]
    if len(all_sources) == 1 and not existing_sources:
        raise ValueError(f"來源路徑不存在: {all_sources[0]}")
    if not existing_sources:
        raise ValueError("所有上傳來源路徑皆不存在")
    for source in all_sources:
        if not source.exists():
            logger.warning(f"來源路徑不存在，略過此來源: {source}")

    archive_base = _archive_base(jobs)
    excluded = _resolved_paths(excluded_paths)
    directories = {}
    files = {}
    symlinks = {}

    for job_sources, remote_root in jobs:
        for local in job_sources:
            source = Path(local)
            if not source.exists():
                continue

            planner.pack_source_root = source.resolve(strict=False)
            first_dir = len(planner.planned_remote_dirs)
            first_link = len(planner.planned_symlinks)
            selected_files = planner._list_local_files(source, remote_root)
            new_remote_dirs = planner.planned_remote_dirs[first_dir:]
            new_symlinks = planner.planned_symlinks[first_link:]

            for remote_dir in new_remote_dirs:
                archive_path = _to_archive_path(remote_dir, archive_base)
                if not archive_path:
                    continue
                rel_dir = _relative_remote_dir(remote_dir, remote_root)
                local_dir = source if not rel_dir else source.joinpath(*PurePosixPath(rel_dir).parts)
                if archive_path in files or archive_path in symlinks:
                    raise ValueError(f"目的路徑同時是檔案與資料夾: {archive_path}")
                if local_dir.is_symlink():
                    local_dir = local_dir.resolve(strict=False)
                directories[archive_path] = local_dir
                _add_parent_directories(directories, archive_path)

            for local_file, rel_path in selected_files:
                local_file = Path(local_file)
                if local_file.is_symlink():
                    # 走到這裡的連結都是已決定「存實際內容」的；tar 不再跟隨連結，
                    # 因此成員來源要換成解析後的實體路徑。
                    local_file = local_file.resolve(strict=False)
                if local_file.resolve(strict=False) in excluded:
                    logger.warning(f"輸出 tar 位於來源內，已避免把 tar 自己封裝進去: {local_file}")
                    continue
                remote_file = _join_remote(remote_root, rel_path)
                archive_path = _to_archive_path(remote_file, archive_base)
                if not archive_path:
                    raise ValueError(f"檔案無法映射成有效的 tar 路徑: {local_file}")
                if archive_path in directories:
                    raise ValueError(f"目的路徑同時是檔案與資料夾: {archive_path}")
                if archive_path in files and files[archive_path] != local_file:
                    logger.warning(f"多個來源都含有 {archive_path}，tar 以後面的來源為準: {source}")
                if symlinks.pop(archive_path, None) is not None:
                    logger.warning(f"多個來源都含有 {archive_path}，tar 以後面的來源為準: {source}")
                files[archive_path] = local_file
                _add_parent_directories(directories, archive_path)

            for local_link, rel_path in new_symlinks:
                remote_link = _join_remote(remote_root, rel_path)
                archive_path = _to_archive_path(remote_link, archive_base)
                if not archive_path:
                    raise ValueError(f"符號連結無法映射成有效的 tar 路徑: {local_link}")
                if archive_path in directories:
                    raise ValueError(f"目的路徑同時是檔案與資料夾: {archive_path}")
                if files.pop(archive_path, None) is not None:
                    logger.warning(f"多個來源都含有 {archive_path}，tar 以後面的來源為準: {source}")
                symlinks[archive_path] = (local_link, os.readlink(local_link))
                _add_parent_directories(directories, archive_path)

    # 父路徑若已被另一個來源規劃成檔案，SFTP 上傳同樣無法成立；封裝時提早報錯。
    for archive_path in directories:
        if archive_path in files or archive_path in symlinks:
            raise ValueError(f"目的路徑同時是檔案與資料夾: {archive_path}")

    _warn_unpacked_symlink_targets(symlinks, files, directories, logger)

    directory_entries = tuple(
        ArchiveDirectory(source=directories[name], archive_path=name)
        for name in sorted(directories, key=lambda item: (item.count("/"), item))
    )
    file_entries = tuple(
        ArchiveFile(source=files[name], archive_path=name)
        for name in sorted(files)
    )
    symlink_entries = tuple(
        ArchiveSymlink(source=symlinks[name][0], archive_path=name, target=symlinks[name][1])
        for name in sorted(symlinks)
    )
    try:
        total_bytes = sum(entry.source.stat().st_size for entry in file_entries)
    except OSError as e:
        raise ValueError(f"計算來源檔案大小失敗: {e}") from e
    return ArchivePlan(directory_entries, file_entries, total_bytes, symlink_entries)


def _warn_unpacked_symlink_targets(symlinks, files, directories, logger):
    """連結目標可能被忽略規則排除；那種連結解開後會是斷鏈，值得先講一聲。"""
    members = set(files) | set(directories) | set(symlinks)
    for name in sorted(symlinks):
        target = symlinks[name][1]
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
        if resolved in (".", "") or resolved.startswith(".."):
            # 指向封裝根目錄本身或更外層，成不成立要看解開的位置，不在這裡判斷。
            continue
        if resolved not in members:
            logger.warning(f"符號連結的目標未納入封裝，解開後會是斷鏈: {name} -> {target}")


def _portable_tarinfo(info):
    """保留權限與 mtime，但不把本機帳號、群組資訊寫入可攜式封裝。"""
    # 註：符號連結成員的 mode 在解開時會被忽略，這裡不特別處理。
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def write_archive(plan, output_path, force=False):
    """原子地寫出 tar；未指定 force 時絕不覆蓋既有輸出。"""
    output = Path(output_path).resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not force:
        raise FileExistsError(f"輸出檔已存在（如要覆蓋請加 --force）: {output}")

    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=str(output.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        # dereference=False：符號連結成員要存成連結本身。需要存實際內容的來源，
        # build_archive_plan 已經先解析成實體路徑了。
        with tarfile.open(temp_path, mode="w", format=tarfile.PAX_FORMAT, dereference=False) as archive:
            for entry in plan.directories:
                if entry.source is None:
                    info = tarfile.TarInfo(entry.archive_path + "/")
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.mtime = int(time.time())
                    archive.addfile(_portable_tarinfo(info))
                else:
                    archive.add(
                        str(entry.source),
                        arcname=entry.archive_path,
                        recursive=False,
                        filter=_portable_tarinfo,
                    )
            for entry in plan.files:
                archive.add(
                    str(entry.source),
                    arcname=entry.archive_path,
                    recursive=False,
                    filter=_portable_tarinfo,
                )
            # 連結最後寫入：目標成員都已就位，解開的順序才不會受影響。
            for entry in plan.symlinks:
                archive.add(
                    str(entry.source),
                    arcname=entry.archive_path,
                    recursive=False,
                    filter=_portable_tarinfo,
                )

        if force:
            os.replace(temp_path, output)
        else:
            # 同檔案系統內以 hard link 發佈，可原子地保證「若已存在就不覆蓋」。
            try:
                os.link(temp_path, output)
            except FileExistsError as e:
                raise FileExistsError(f"輸出檔已存在（如要覆蓋請加 --force）: {output}") from e
            temp_path.unlink()
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return output


def _default_output(config_path):
    stem = Path(config_path).stem
    suffix = "_upload_settings"
    if stem.endswith(suffix):
        stem = stem[: -len(suffix)]
    return Path.cwd() / f"{stem or 'upload'}.tar"


def _format_size(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024


def build_parser():
    parser = argparse.ArgumentParser(description="把 SFTP upload 設定原本會上傳的內容打包成本地 tar")
    parser.add_argument("--config", required=True, help="upload 設定檔（例如 config/radar_upload_settings.json）")
    parser.add_argument("--output", help="輸出的 .tar；預設由設定檔名稱推導並放在目前目錄")
    parser.add_argument("--force", action="store_true", help="允許覆蓋既有輸出檔")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("pack_upload")
    output = Path(args.output) if args.output else _default_output(args.config)

    try:
        settings = load_pack_settings(args.config)
        # 在走訪來源前即檢查，避免花時間規劃後才發現不可覆蓋。
        if output.exists() and not args.force:
            raise FileExistsError(f"輸出檔已存在（如要覆蓋請加 --force）: {output.resolve()}")
        plan = build_archive_plan(settings, excluded_paths=(output,), logger=logger)
        symlink_note = f"、{len(plan.symlinks)} 個符號連結" if plan.symlinks else ""
        print(
            f"準備封裝 {len(plan.files)} 個檔案、{len(plan.directories)} 個資料夾{symlink_note}"
            f"（來源資料 {_format_size(plan.total_bytes)}）...",
            flush=True,
        )
        written = write_archive(plan, output, force=args.force)
    except (ValueError, FileExistsError, PlaceholderError, OSError, tarfile.TarError) as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消封裝。", file=sys.stderr)
        return 130

    print(f"完成：{written}（tar 大小 {_format_size(written.stat().st_size)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
