#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
apk_path="${1:-${repo_root}/base..apk}"
asset_path="res/drawable-nodpi-v4/cartopview_complete_fallback.png"
destination="${repo_root}/custom_components/volvooncall_cn/frontend/cartopview_complete_fallback.png"

if [[ ! -f "${apk_path}" ]]; then
  echo "APK not found: ${apk_path}" >&2
  echo "Usage: $0 /path/to/base.apk" >&2
  exit 1
fi

mkdir -p "$(dirname "${destination}")"
unzip -p "${apk_path}" "${asset_path}" > "${destination}"

if [[ ! -s "${destination}" ]]; then
  echo "Asset extraction failed: ${asset_path}" >&2
  exit 1
fi

echo "Extracted local-only vehicle asset to: ${destination}"
