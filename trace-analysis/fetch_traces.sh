#!/usr/bin/env bash
# Download and decompress Alibaba cluster-trace-microservices-v2022 CallGraph
# shards from the public upstream OSS bucket into <repo>/traces/CallGraph/.
#
# Usage: fetch_traces.sh START END       fetch shards START..END-1 (half-open)
#        fetch_traces.sh --only I        fetch just shard I
#
# Env:   KEEP_ARCHIVES=1   keep the .tar.gz after extraction (default: delete)
#        DOWNLOAD_ONLY=1   do not decompress
#        DRY_RUN=1         print URLs and targets, download nothing
#        BASE_URL=...      override the upstream bucket URL
#
# Each shard is ~220 MB compressed and ~1.4 GB as CSV (measured on CallGraph_0).
# Upstream: https://github.com/alibaba/clusterdata/tree/master/cluster-trace-microservices-v2022
set -euo pipefail

BASE_URL="${BASE_URL:-https://aliopentrace.oss-cn-beijing.aliyuncs.com/v2022MicroservicesTraces/CallGraph}"

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

if [[ ${1:-} == "--only" && $# -eq 2 ]]; then
  START=$2; END=$(( $2 + 1 ))
elif [[ $# -eq 2 ]]; then
  START=$1; END=$2
else
  usage
fi
[[ $START =~ ^[0-9]+$ && $END =~ ^[0-9]+$ ]] || usage
if (( START >= END )); then echo "Error: START must be < END" >&2; exit 1; fi

OUT="$(cd "$(dirname "$0")/.." && pwd)/traces/CallGraph"
if [[ ${DRY_RUN:-0} != 1 ]]; then mkdir -p "$OUT"; cd "$OUT"; fi

download() {  # url dest
  if command -v wget >/dev/null; then
    wget -c --retry-connrefused --tries=5 --timeout=50 -O "$2" "$1"
  else
    curl -fL -C - --retry 5 --retry-connrefused -o "$2" "$1"
  fi
}

for ((i=START; i<END; i++)); do
  csv="CallGraph_${i}.csv"
  tgz="CallGraph_${i}.tar.gz"
  url="${BASE_URL}/${tgz}"
  if [[ -f $csv ]]; then echo "Skipping $csv (already exists)"; continue; fi
  if [[ ${DRY_RUN:-0} == 1 ]]; then echo "would fetch $url -> $OUT/$tgz"; continue; fi
  download "$url" "$tgz"
  if [[ ${DOWNLOAD_ONLY:-0} == 1 ]]; then continue; fi
  echo "Decompressing $tgz..."
  tar -xzf "$tgz"
  [[ -f $csv ]] || { echo "Error: $csv not produced by $tgz" >&2; exit 1; }
  [[ ${KEEP_ARCHIVES:-0} == 1 ]] || rm -f "$tgz"
done
