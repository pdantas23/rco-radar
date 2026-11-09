"""Rodada de teste do modo só dados abertos, em SOMENTE LEITURA no banco.

Não passa pela fila: um job em radar.jobs poderia ser pego pelo worker de produção, que usa fontes pagas.
Busca no gosom, procura o CNPJ na base local, lê o site e mostra o que SERIA gravado.

    RADAR_DATABASE_URL=... GOSOM_URL=... python -m radar.rodada_dados_abertos \\
        --termo barbearia --cidade Teresina --uf PI --limite 20 --saida resultado.json
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter

import httpx
import psycopg

from radar import cnpj_match
from radar.fontes.base import ConsultaMaps
from radar.fontes.cnpj import completar_brasilapi
from radar.fontes.cnpj_local import BaseCnpjSemDados, CnpjBaseLocal
from radar.fontes.instagram_site import instagram_do_site
from radar.fontes.maps_gosom import MapsGosom
from radar.site import verificar as verificar_site
from radar.telefone import normalizar

NOMINATIM = "https://nominatim.openstreetmap.org/search"


async def geocodificar(cidade: str, uf: str) -> tuple[float, float]:
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "RCO-Radar/0.1 (uso interno RCO Hub)"}) as c:
        r = await c.get(NOMINATIM, params={"city": cidade, "state": uf, "country": "Brasil", "format": "json", "limit": 1})
    d = r.json() if r.status_code == 200 else []
    if not d:
        raise SystemExit(f"cidade não encontrada: {cidade}-{uf}")
    return float(d[0]["lat"]), float(d[0]["lon"])


async def avaliar(n, cnpj, site, instagram_site) -> dict:
    """Um negócio do Maps -> o que iria para radar.leads, radar.empresas e radar.presenca."""
    tel = normalizar(n.telefone)
    lead = {"place_id": n.place_id, "nome": n.nome, "categoria": n.categoria, "endereco": n.endereco, "cidade": n.cidade,
            "estado": n.estado, "cep": n.cep, "telefone": tel.e164 if tel else None, "site": n.site, "nota": n.nota,
            "qtd_avaliacoes": n.qtd_avaliacoes, "fonte_maps": "gosom_maps"}
    empresas, status, revisao = [], "nao_encontrado", False
    try:
        cands = await cnpj.buscar_por_nome(n.nome, n.cidade, n.estado)
        dec = cnpj_match.decidir(
            cnpj_match.Negocio(n.nome, n.endereco, n.cidade, n.cep, tel.e164 if tel else ""),
            [cnpj_match.Candidato(c.cnpj, c.razao_social, c.nome_fantasia, c.logradouro, c.numero, c.municipio, c.cep, "",
                                  c.situacao) for c in cands])
        por_cnpj = {c.cnpj: c for c in cands}
        escolhido = dec.escolhido.candidato.cnpj if dec.escolhido else None
        if escolhido and (det := await cnpj.detalhar(escolhido)):
            completar_brasilapi(por_cnpj[escolhido], det)
        for r in dec.candidatos:
            d = por_cnpj[r.candidato.cnpj]
            empresas.append({"escolhido": d.cnpj == escolhido, "cnpj": d.cnpj, "razao_social": d.razao_social,
                             "nome_fantasia": d.nome_fantasia, "porte": d.porte, "regime": d.regime,
                             "cnae_principal": d.cnae_principal, "confianca_cnpj": r.confianca,
                             "motivo_confianca": r.motivo, "fonte": cnpj.chave})
        status = "encontrado" if escolhido else ("revisao" if dec.revisao_humana else "nao_encontrado")
        revisao = dec.revisao_humana
    except BaseCnpjSemDados:
        status = "nao_verificado"
    r = await site(n.site)
    usuario = r.instagram_user or await instagram_site(n.site)
    lead |= {"cnpj_status": status, "revisao_cnpj": revisao,
             "site_status": "nao_se_aplica" if not r.site_existe else ("encontrado" if r.site_responde else "nao_encontrado"),
             "instagram_status": "encontrado" if usuario else "nao_verificado", "anuncio_ativo": "nao_verificado"}
    presenca = {"site_existe": r.site_existe, "site_responde": r.site_responde, "instagram_user": usuario,
                "instagram_origem": "site" if usuario else None}
    return {"radar.leads": lead, "radar.empresas": empresas, "radar.presenca": presenca,
            "radar.custos_api": [{"fonte": "gosom_maps", "valor": 0}, {"fonte": cnpj.chave, "valor": 0}]}


async def rodar(args, conn, maps=None, cnpj=None, site=verificar_site, instagram_site=instagram_do_site) -> list[dict]:
    os.environ["RADAR_SO_DADOS_ABERTOS"] = "1"          # esta rodada nunca gasta, venha de onde vier
    maps = maps or MapsGosom()
    cnpj = cnpj or CnpjBaseLocal(conn)
    lat, lng = (args.lat, args.lng) if args.lat is not None else await geocodificar(args.cidade, args.uf)
    negocios = await maps.buscar(ConsultaMaps(args.termo, lat, lng, args.raio_km, None, args.limite), uf_padrao=args.uf)
    return [await avaliar(n, cnpj, site, instagram_site) for n in negocios if not n.fechado_permanentemente][:args.limite]


def resumo(itens: list[dict]) -> dict:
    return {"negocios": len(itens),
            "cnpj": dict(Counter(i["radar.leads"]["cnpj_status"] for i in itens)),
            "instagram": dict(Counter(i["radar.leads"]["instagram_status"] for i in itens)),
            "site": dict(Counter(i["radar.leads"]["site_status"] for i in itens)),
            "custo_brl": 0}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--termo", required=True)
    p.add_argument("--cidade", required=True)
    p.add_argument("--uf", required=True)
    p.add_argument("--limite", type=int, default=20)
    p.add_argument("--raio-km", type=float, default=5.0)
    p.add_argument("--lat", type=float)
    p.add_argument("--lng", type=float)
    p.add_argument("--saida", default="rodada.json", help="arquivo com o que seria gravado")
    a = p.parse_args(argv)
    # a própria conexão recusa escrita
    with psycopg.connect(os.environ["RADAR_DATABASE_URL"], autocommit=True,
                         options="-c default_transaction_read_only=on") as conn:
        itens = asyncio.run(rodar(a, conn))
    with open(a.saida, "w", encoding="utf-8") as f:
        json.dump(itens, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(resumo(itens), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
