#!/bin/bash
# uso: t2run.sh NOME DEPTH "KEYWORD" LAT LON
cd /root/radar-gosom-teste
N=$1; D=$2; K=$3; LA=$4; LO=$5; A=http://127.0.0.1:18080/api/v1
S=$(date +%s)
ID=$(curl -s -X POST $A/jobs -H "Content-Type: application/json" -d "{\"name\":\"$N\",\"keywords\":[\"$K\"],\"lang\":\"pt\",\"zoom\":15,\"lat\":\"$LA\",\"lon\":\"$LO\",\"depth\":$D,\"max_time\":900,\"email\":false}" | sed -E "s/.*\"id\":\"([^\"]+)\".*/\1/")
echo "$ID" > $N.id; echo "start $(date -u +%T) id=$ID" > $N.log
while :; do
  T=$(( $(date +%s)-S ))
  ST=$(curl -s $A/jobs/$ID | sed -E "s/.*\"Status\":\"([^\"]+)\".*/\1/;s/.*\"status\":\"([^\"]+)\".*/\1/" | head -c 20)
  echo "t=$T st=$ST load=$(cut -d" " -f1-3 /proc/loadavg) $(docker stats --no-stream --format "cpu={{.CPUPerc}} mem={{.MemUsage}}" radar-gosom-teste)" >> $N.samples
  L=$(cut -d" " -f1 /proc/loadavg | cut -d. -f1)
  if [ "$L" -ge 8 ]; then echo "ABORT load=$L" >> $N.log; docker stop radar-gosom-teste; break; fi
  case "$ST" in ok|failed|error|done) break;; esac
  [ $T -gt 960 ] && break
  sleep 8
done
echo "end $(date -u +%T) total=$(( $(date +%s)-S ))s st=$ST" >> $N.log
curl -s $A/jobs/$ID/download -o $N.csv
