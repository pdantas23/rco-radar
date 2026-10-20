#!/bin/bash
docker service ls --format "{{.Name}} {{.Replicas}}" | grep -E "crm_|evolution|supabase" | tr "\n" ";"; echo
for s in servicos_evolution-go crm_saas-rco crm_crm-rco; do echo "$s erros: $(docker service logs $s --since ${1:-10m} 2>&1 | grep -ciE "error|exception|panic|fatal")"; done
