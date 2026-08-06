#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Verwendung: $0 /Pfad/zum/iOS-SDK-1.9.2.zip" >&2
  exit 64
fi

archive="$1"
if [[ ! -f "$archive" ]]; then
  echo "SDK-Archiv nicht gefunden: $archive" >&2
  exit 66
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/.." && pwd)"
destination="$project_dir/Vendor/Insta360"
temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/showroomflow-insta360.XXXXXX")"
trap 'rm -rf "$temporary_dir"' EXIT

unzip -q "$archive" -d "$temporary_dir"

frameworks=(
  "INSCameraSDK.xcframework"
  "INSCameraServiceSDK.xcframework"
  "INSCoreMedia.xcframework"
  "SSZipArchive.xcframework"
)

mkdir -p "$destination"
for framework in "${frameworks[@]}"; do
  source_path="$(
    find "$temporary_dir" \
      -type d \
      -name "$framework" \
      -path '*/Frameworks/*' \
      ! -path '*/Carthage/*' \
      ! -path '*/__MACOSX/*' \
      -print \
      -quit
  )"
  if [[ -z "$source_path" ]]; then
    echo "Erforderliches Framework fehlt im Archiv: $framework" >&2
    exit 65
  fi
  rm -rf "$destination/$framework"
  cp -R "$source_path" "$destination/$framework"
done

# SDK 1.9.2 exposes four player/render headers whose referenced internal
# declarations are missing from the public package. They are not needed for
# ShowroomFlow's live camera preview, but prevent modern Swift compilers from
# importing INSCameraSDK at all. Remove only those umbrella imports after each
# local SDK installation.
find "$destination/INSCameraSDK.xcframework" \
  -type f \
  -name "INSCameraSDK.h" \
  -exec perl -0pi -e \
    's{#import <INSCameraSDK/(?:INSCameraPlayerRenderSession|INSCameraSessionPlayer|INSCameraPlayerRender|INSCameraPlayerRenderView)\.h>\n}{}g' \
    {} +

# The matching INSCoreMedia package also publishes optional Meishe/NVIDIA
# effect categories in its umbrella header, although NvEffectSdkCore is not
# included in Insta360's archive. ShowroomFlow does not use these effects.
# Keeping their imports would make the otherwise complete camera SDK
# impossible to import on a real device.
find "$destination/INSCoreMedia.xcframework" \
  -type f \
  -name "INSCoreMedia.h" \
  -exec perl -0pi -e \
    's{#import <INSCoreMedia/(?:NvsVideoEffect\+Custom|NvsEffect\+Custom)\.h>\n}{}g' \
    {} +

# Patching files inside a signed framework invalidates its resource seal. In
# particular, the iOS simulator's dyld can crash while mapping INSCoreMedia
# instead of reporting a regular signature error. Remove the vendor signatures
# from all local slices; Xcode signs the selected slice when it embeds it.
while IFS= read -r -d '' framework_bundle; do
  codesign --remove-signature "$framework_bundle" 2>/dev/null || true
done < <(find "$destination" -type d -name "*.framework" -print0)

echo "Insta360 SDK lokal installiert unter: $destination"
echo "Die Frameworks und Insta360Secrets.xcconfig werden nicht von Git erfasst."
