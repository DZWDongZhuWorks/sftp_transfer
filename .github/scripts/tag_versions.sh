#!/usr/bin/env bash
# 沿主線 first-parent 找出 VERSION.json 版號「第一次出現」的 commit,補上缺的 v<版號> annotated tag。
#
# 用法: .github/scripts/tag_versions.sh [--dry-run] [--push] [base-ref]
#   base-ref  預設 origin/HEAD(沒有就 HEAD)
#   --dry-run 只列出會打哪些 tag,什麼都不建立
#   --push    把「這次新打的」tag 推上 origin(既有 tag 不重推)
#
# 由 .github/workflows/tag-versions.yml 在每次 push 到主分支時執行;本機也能直接跑。
# 同一支腳本放在 scheduler / sftp_transfer / SHM-stream-manager / device_monitor / radar,
# 改動時五份要一起改。
#
# 規則(與 2026-09 前手動打的 tag 位置一致):
#   - 只看主線 first-parent;PR 分支中途 bump 過、沒進主線的版號不打
#   - 已存在的同名 tag 一律不動(不移動、不覆蓋、不重推)
#   - 每次都掃整條主線,所以漏跑一次會在下次補上
#   - 版號不是 semver、倒退或重複出現 → 只警告,不打,不讓 CI 失敗
set -euo pipefail

dry=0 push=0 base=""
for a in "$@"; do
  case "$a" in
    --dry-run) dry=1 ;;
    --push)    push=1 ;;
    -*)        echo "unknown option: $a" >&2; exit 2 ;;
    *)         base="$a" ;;
  esac
done
if [ -z "$base" ]; then
  base=$(git symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null || echo HEAD)
fi

json_field() {  # $1=commit $2=欄位 → 印出 VERSION.json 的該欄位,讀不到印空字串
  git show "$1:VERSION.json" 2>/dev/null | python3 -c '
import json, sys
try: print(json.load(sys.stdin).get(sys.argv[1], "") or "")
except Exception: print("")' "$2"
}
# semver 比較:$1 < $2 則回傳 0
ver_lt() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]; }

declare -A seen=()
prev="" last="" created=0 existed=0 warned=0
new_tags=()

while read -r c; do
  v=$(json_field "$c" version)
  [ -z "$v" ] && continue
  [ "$v" = "$last" ] && continue
  short=$(git rev-parse --short "$c")
  date=$(git log -1 --format=%cs "$c")
  tag="v$v"

  semver=0
  [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+].*)?$ ]] && semver=1

  if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    # 既有 tag 優先判斷:不管它在哪都不動,也不因倒退/重複而警告
    at=$(git rev-parse --short "$tag^{commit}")
    if [ "$at" = "$short" ]; then
      echo "ok    $short $date $tag 已存在"
    else
      echo "ok    $short $date $tag 已存在,但在 $at(不移動)"
    fi
    existed=$((existed+1))
  elif [ $semver = 0 ]; then
    echo "WARN  $short $date 版號不是 semver: '$v',略過"; warned=$((warned+1))
  elif [ -n "${seen[$v]:-}" ]; then
    echo "WARN  $short $date $tag 重複出現(第一次在 ${seen[$v]}),略過"; warned=$((warned+1))
  elif [ -n "$prev" ] && ver_lt "$v" "$prev"; then
    echo "WARN  $short $date $tag 比前一版 v$prev 小(倒退),略過"; warned=$((warned+1))
  else
    if [ $dry = 0 ]; then
      git tag -a "$tag" "$c" -m "$tag" -m "$(json_field "$c" notes)"
      new_tags+=("refs/tags/$tag")
    fi
    echo "NEW   $short $date $tag  ← $(git log -1 --format=%s "$c")"
    created=$((created+1))
  fi
  seen[$v]=$short
  # 舊制版號(例如 radar 早期的 9.7)不當作倒退比較的基準
  if [ $semver = 1 ]; then prev=$v; fi
  last=$v
done < <(git log --first-parent --reverse --format=%H "$base" -- VERSION.json)

echo "== base=$base  new=$created existing=$existed warnings=$warned$([ $dry = 1 ] && echo '  (dry-run)')"

if [ $push = 1 ] && [ ${#new_tags[@]} -gt 0 ]; then
  git push origin "${new_tags[@]}"
fi
