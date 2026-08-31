#!/bin/sh
# Regenerates index.json from the scripts in this directory.
# Description and usage come from the second and third comment lines of each script,
# so the catalogue in the app can never drift from the script itself.
set -eu

cd "$(dirname "$0")"

# JSON has no way to carry a bare quote or backslash; scripts document both.
escape() {
  sed 's/\\/\\\\/g; s/"/\\"/g'
}

hash_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

{
  printf '{\n  "version": 1,\n  "scripts": [\n'
  first=1
  for file in wt-*; do
    [ -f "$file" ] || continue
    description=$(sed -n '2s/^# *[^ ]* *- *//p' "$file")
    usage=$(sed -n '/^# *[Uu]sage:/ { s/^# *[Uu]sage: *//; p; q; }' "$file")
    size=$(wc -c < "$file" | tr -d ' ')
    sha=$(hash_of "$file")
    [ "$first" -eq 1 ] || printf ',\n'
    first=0
    printf '    {\n'
    printf '      "name": "%s",\n' "$file"
    printf '      "description": "%s",\n' "$description"
    printf '      "usage": "%s",\n' "$usage"
    printf '      "size": %s,\n' "$size"
    printf '      "sha256": "%s"\n' "$sha"
    printf '    }'
  done
  printf '\n  ]\n}\n'
} > index.json

echo "wrote $(pwd)/index.json"
