#!/bin/sh
# Import the user's local publications and rebuild the search index.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

python_bin=${MA3_DOCS_PYTHON:-python3}
manual_dir=${MA3_MANUAL_SOURCE:-"$project_dir/sources/manual"}
manual_version=${MA3_MANUAL_VERSION:-2.5}
release_dir="$project_dir/release-notes-pdf"

if [ -d "$manual_dir" ] || [ -n "${MA3_MANUAL_SOURCE:-}" ]; then
  if [ ! -d "$manual_dir/HTML" ] || [ ! -f "$manual_dir/_webHelpScripts/Master/toc_nav.js" ] || [ ! -f "$manual_dir/_webHelpScripts/Index/index_data.js" ]; then
    echo 'Expected an installed language directory containing HTML/ and the _webHelpScripts navigation files. See README.md.' >&2
    exit 1
  fi
fi

set -- "$release_dir"/*.pdf
if [ ! -d "$manual_dir" ] && [ ! -f "$1" ]; then
  echo 'Supply an installed manual in sources/manual/ or release-note PDFs in release-notes-pdf/. See README.md.' >&2
  exit 1
fi

if [ -d "$manual_dir" ]; then
  "$python_bin" -c 'import lxml' || { echo 'Install lxml in the active Python environment to import a manual.' >&2; exit 1; }
fi
if [ -f "$1" ]; then
  command -v pdftotext >/dev/null || { echo 'Install Poppler (pdftotext) to import release notes.' >&2; exit 1; }
  "$python_bin" -c 'import pdfplumber, pypdf, PIL' || { echo 'Install pdfplumber and pypdf[image] in the active Python environment to import release notes.' >&2; exit 1; }
fi

if [ -d "$manual_dir" ]; then
  "$python_bin" scripts/ingest_manual.py --source "$manual_dir" --version "$manual_version"
fi
if [ -f "$1" ]; then
  "$python_bin" scripts/ingest_releases.py --source "$release_dir" --manual "$manual_dir/HTML"
fi
"$python_bin" scripts/build_index.py
