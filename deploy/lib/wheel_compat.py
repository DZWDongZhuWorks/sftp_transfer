#!/usr/bin/env python3
# wheel_compat.py — 在動任何東西之前，確認 wheelhouse 真的能裝到「這個」直譯器上。
# ---------------------------------------------------------------------------
# 為什麼需要這支：deploy_offline.sh 一直都算得出直譯器的 cp 標籤，但只印出來、
# 沒有拿去比對 wheelhouse。於是一台 Bionic（py3.6 / glibc 2.27）對著 cp310 +
# manylinux_2_34 的 wheelhouse 跑 --check-only 會回 0，宣稱「全部通過」，然後在
# 階段 B 的 pip 才失敗 —— 而階段 A 已經改完 systemd / sudoers / docker 群組 / tmux，
# 操作者也已經看到「以下不再需要任何輸入」離開終端機了。
#
# 那個失敗點的位置最糟：兩支 OTA 腳本（run_sftp_self_update.sh /
# run_scheduler_download.sh）都以這個 venv 的 python 執行，venv 建好了、pip 沒裝完，
# 於是它們的 `[ -x "$VENV_PY" ]` 守門過得去、卻在 import paramiko 時炸。那條船就
# 失去了唯一的 OTA 通道，只能派人上船。**先擋在階段 A 之前，最壞情況才會是
# 「還沒部署」而不是「部署到一半且失去自救能力」。**
#
# 本檔刻意只用標準庫、且必須在 **Python 3.6** 上跑得起來（Bionic 的船端就是 3.6）。
# 不要在這裡用 f-string 的 `=`、dataclasses、`from __future__ import annotations`
# 或 PEP 585/604 的註解語法 —— 加了就等於讓守門自己先炸在它要守的那個平台上。
#
#   python3 wheel_compat.py <wheelhouse 目錄> <glibc 版本> [必要套件 ...]
#
# 離開碼：0 全部相容；4 wheelhouse 缺漏/為空/缺必要套件；6 有 wheel 與本機不相容。
# ---------------------------------------------------------------------------
import os
import re
import sys

# wheel 檔名：{name}-{ver}(-{build})?-{pytag}-{abitag}-{platformtag}.whl
_WHEEL_RE = re.compile(r"^(?P<name>[^-]+)-(?P<ver>[^-]+)"
                       r"(?:-(?P<build>\d[^-]*))?"
                       r"-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$")

_MANYLINUX_LEGACY = {
    "manylinux1": (2, 5),
    "manylinux2010": (2, 12),
    "manylinux2014": (2, 17),
}


def _norm(name):
    """套件名正規化（PEP 503）：底線與點都當成連字號，大小寫不敏感。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_ver(text):
    parts = []
    for chunk in str(text).split("."):
        m = re.match(r"^(\d+)", chunk)
        if not m:
            break
        parts.append(int(m.group(1)))
    return tuple(parts) if parts else (0,)


def _cp_versions(tag_field):
    """從 python/abi 標籤欄位取出所有 cpXY 對應的版本，例如 cp36 → (3, 6)。"""
    out = []
    for tag in tag_field.split("."):
        m = re.match(r"^cp(\d)(\d+)$", tag)
        if m:
            out.append((int(m.group(1)), int(m.group(2))))
    return out


def _glibc_required(plat_field):
    """回傳這個平台標籤需要的最低 glibc；None = 與 glibc 無關（純 Python / any）。"""
    need = None
    for tag in plat_field.split("."):
        if tag == "any":
            return None
        m = re.match(r"^manylinux_(\d+)_(\d+)_", tag)
        if m:
            cand = (int(m.group(1)), int(m.group(2)))
        else:
            cand = None
            for legacy, ver in _MANYLINUX_LEGACY.items():
                if tag.startswith(legacy + "_"):
                    cand = ver
                    break
        if cand is None:
            continue
        # 同一個 wheel 可以宣告多個平台標籤，取**最寬鬆**的那個即可安裝。
        if need is None or cand < need:
            need = cand
    return need


def _arch_of(plat_field):
    arches = set()
    for tag in plat_field.split("."):
        if tag == "any":
            continue
        m = re.match(r"^(?:manylinux[_0-9a-z]*|linux)_(.+)$", tag)
        if m:
            arches.add(m.group(1))
    return arches


def check(wheelhouse, glibc_text, required, py_ver, arch):
    problems = []
    found = set()

    if not os.path.isdir(wheelhouse):
        return 4, ["wheelhouse 目錄不存在：" + wheelhouse], found
    names = sorted(n for n in os.listdir(wheelhouse) if n.endswith(".whl"))
    if not names:
        return 4, ["wheelhouse 內沒有任何 .whl：" + wheelhouse], found

    glibc = _parse_ver(glibc_text)
    incompatible = []

    for fn in names:
        m = _WHEEL_RE.match(fn)
        if not m:
            problems.append("檔名不是合法的 wheel：" + fn)
            continue
        found.add(_norm(m.group("name")))
        why = []

        # 1) 直譯器 ABI。cpXY-cpXY 要求完全相符；cpXY-abi3 只要求 >= XY。
        py_tags = _cp_versions(m.group("py"))
        abi_tags = _cp_versions(m.group("abi"))
        is_abi3 = "abi3" in m.group("abi").split(".")
        if py_tags:
            if is_abi3:
                if py_ver < min(py_tags):
                    why.append("需要 Python >= %d.%d（abi3）" % min(py_tags))
            elif not any(t == py_ver for t in py_tags):
                why.append("只適用 Python " +
                           "/".join("%d.%d" % t for t in py_tags))
        if abi_tags and not is_abi3:
            if not any(t == py_ver for t in abi_tags):
                why.append("ABI 標籤是 " +
                           "/".join("cp%d%d" % t for t in abi_tags))

        # 2) glibc 下限。這是 Bionic 最容易踩到的一項：manylinux_2_34 的輪子在
        #    glibc 2.27 上裝得進去卻載入失敗，pip 也不會挑出來。
        need = _glibc_required(m.group("plat"))
        if need is not None and need > glibc:
            why.append("需要 glibc >= %d.%d" % need)

        # 3) 架構。
        arches = _arch_of(m.group("plat"))
        if arches and arch and arch not in arches:
            why.append("架構是 " + "/".join(sorted(arches)))

        if why:
            incompatible.append((fn, "；".join(why)))

    missing = [p for p in required if _norm(p) not in found]

    if incompatible:
        problems.append("以下 wheel 與本機不相容（Python %d.%d / glibc %s / %s）："
                        % (py_ver[0], py_ver[1], glibc_text, arch or "?"))
        for fn, why in incompatible:
            problems.append("    %s  ←  %s" % (fn, why))
        return 6, problems, found

    if missing:
        problems.append("wheelhouse 缺少必要套件：" + ", ".join(missing))
        return 4, problems, found

    return 0, [], found


def main(argv):
    # 目標平台一律由參數指定，**不從執行本檔的直譯器推**。這兩件事必須分開：
    # 判斷「輪子能不能裝到目標 Python」是一個純函式，不該取決於誰來跑這段程式。
    # 綁在一起會有兩個後果：一是無法測（測試機是 3.10，就永遠測不到 3.6 的判斷），
    # 二是船端直譯器有任何毛病時守門會連帶失效 —— 而那正是最需要它出聲的時候。
    py_ver = None
    glibc_text = None
    arch = None
    positional = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--py" and i + 1 < len(argv):
            parts = argv[i + 1].split(".")
            py_ver = (int(parts[0]), int(parts[1]))
            i += 2
        elif a == "--glibc" and i + 1 < len(argv):
            glibc_text = argv[i + 1]
            i += 2
        elif a == "--arch" and i + 1 < len(argv):
            arch = argv[i + 1]
            i += 2
        elif a.startswith("-"):
            sys.stderr.write("未知參數：" + a + "\n")
            return 2
        else:
            positional.append(a)
            i += 1

    if not positional:
        sys.stderr.write(
            "用法：wheel_compat.py [--py X.Y] [--glibc X.Y] [--arch NAME] "
            "<wheelhouse> [必要套件 ...]\n")
        return 2
    wheelhouse = positional[0]
    required = positional[1:]

    if py_ver is None:
        py_ver = (sys.version_info[0], sys.version_info[1])
    if glibc_text is None:
        try:
            import subprocess
            glibc_text = subprocess.check_output(
                ["getconf", "GNU_LIBC_VERSION"]).decode().split()[-1]
        except Exception:
            glibc_text = "0"
    if arch is None:
        # 架構要用 uname 的命名（aarch64 / x86_64），**不是** dpkg 的（arm64 / amd64）——
        # wheel 的平台標籤跟隨前者。拿 dpkg 的值比會把每個 aarch64 輪子誤判成不相容。
        import platform
        arch = platform.machine()

    rc, problems, found = check(wheelhouse, glibc_text, required, py_ver, arch)
    for line in problems:
        sys.stderr.write(line + "\n")
    if rc == 0:
        sys.stdout.write("wheelhouse 與本機相容：%d 個 wheel，Python %d.%d / glibc %s\n"
                         % (len(found), py_ver[0], py_ver[1], glibc_text))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
