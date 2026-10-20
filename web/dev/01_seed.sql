-- Dados de exemplo (fixtures) SÓ para o banco local de desenvolvimento.
select setseed(0.42);
insert into auth.users (id, email, nome) values
 ('11111111-1111-1111-1111-111111111111','luis@rcohub.dev','Luis'),
 ('22222222-2222-2222-2222-222222222222','ana@rcohub.dev','Ana');
insert into radar.usuarios (id, nome, perfil) values
 ('11111111-1111-1111-1111-111111111111','Luis','admin'),
 ('22222222-2222-2222-2222-222222222222','Ana','operador');
insert into radar.config (chave, valor) values
 ('custo_unitario_brl','{"maps":0.026,"cnpj":0.01,"instagram":0.011,"anuncios":0.019}'),
 ('limite_diario_custo_brl','50'),
 ('anuncios_ativos','{"ligado": false}');
insert into radar.regras_pontuacao (versao, nicho, pesos, faixas) values
 (1, null, '{"tem_whatsapp":25,"presenca_media_ou_forte":20,"faturamento_30k_ou_mais":20,"google_bem_avaliado":20,"cnpj_ativo":15}', '{"quente":70,"morno":40,"nota_minima":4.0,"min_avaliacoes":50}');
insert into radar.contas_envio (tipo, identificador, rotulo) values
 ('whatsapp_nao_oficial','5586900000001','Número 1'),('whatsapp_nao_oficial','5586900000002','Número 2');

insert into radar.pesquisas (id, nome, nicho_texto, categoria_maps, lugar_estado, lugar_cidade, filtros, fonte_maps, usuario_id, criada_em, status, custo_estimado, custo_real, total_encontrado, total_aprovado) values
 ('aaaaaaaa-0000-0000-0000-000000000001','barbearia · Teresina · 02/10','barbearia','Barbearia','PI','Teresina','{"max_resultados":200,"nota_minima":4.0,"min_avaliacoes":0}','gosom_maps','11111111-1111-1111-1111-111111111111', now()-interval '17 days','concluida',15,14.2,70,12),
 ('aaaaaaaa-0000-0000-0000-000000000002','barbearia · Teresina · 17/10','barbearia','Barbearia','PI','Teresina','{"max_resultados":200,"nota_minima":4.0,"min_avaliacoes":0}','apify','22222222-2222-2222-2222-222222222222', now()-interval '2 days','concluida',15,13.1,60,31),
 ('aaaaaaaa-0000-0000-0000-000000000003','clínica odonto · Fortaleza · 19/10','clínica odontológica','Dentista','CE','Fortaleza','{"max_resultados":120,"nota_minima":4.2,"min_avaliacoes":10}','apify','22222222-2222-2222-2222-222222222222', now()-interval '3 hours','enriquecendo',9,3.1,45,0);

-- pesquisas que FALHARAM (simulam o erro do servidor próprio, com a mensagem do job para a tela)
insert into radar.pesquisas (id, nome, nicho_texto, categoria_maps, lugar_estado, lugar_cidade, filtros, fonte_maps, usuario_id, criada_em, status, custo_estimado, custo_real, total_encontrado, total_aprovado) values
 ('aaaaaaaa-0000-0000-0000-000000000004','padaria · Parnaíba · 18/10','padaria','Padaria','PI','Parnaíba','{"max_resultados":200,"nota_minima":4.0,"min_avaliacoes":0}','gosom_maps','22222222-2222-2222-2222-222222222222', now()-interval '1 day','falhou',4.2,0,0,0),
 ('aaaaaaaa-0000-0000-0000-000000000005','mercado · Picos · 18/10','mercado','Supermercado','PI','Picos','{"max_resultados":100,"nota_minima":4.0,"min_avaliacoes":0}','apify_maps','22222222-2222-2222-2222-222222222222', now()-interval '30 hours','falhou',4.7,0,0,0);
insert into radar.jobs (tipo, pesquisa_id, status, erro, tentativas, concluido_em) values
 ('minerar','aaaaaaaa-0000-0000-0000-000000000004','falhou','O servidor próprio não conseguiu buscar no Google Maps (tempo esgotado). Você pode tentar de novo mais tarde ou buscar pela API (Apify), que é paga.',1, now()-interval '1 day'),
 ('minerar','aaaaaaaa-0000-0000-0000-000000000005','falhou','A API (Apify) recusou a busca: limite de uso do provedor atingido. Tente de novo em alguns minutos.',1, now()-interval '30 hours');

-- 175 leads
insert into radar.leads (id, place_id, nome, categoria, nicho, endereco, cidade, estado, telefone, tem_whatsapp_provavel, site, nota, qtd_avaliacoes, link_maps, fonte_maps, status, motivo_descarte, motivo_descarte_texto, classificacao, pontuacao, versao_regra,
  faturamento_estimado_faixa, faturamento_origem, faturamento_explicacao, faturamento_informado_faixa, presenca_digital, anuncio_ativo, qtd_anuncios_ativos, cnpj_status, instagram_status, site_status, criado_em, coletado_maps_em)
select
  ('bbbbbbbb-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid, 'place_' || i,
  (array['Barbearia','Barber Shop','Studio','Espaço','Salão','Clínica','Odonto','Cell','Mega'])[1 + (i % 9)] || ' ' || (array['do Zé','Imperial','Vip','Premium','Central','Alfa','Prime','Bom Corte','Elite','Style'])[1 + (i*7 % 10)] || ' ' || i,
  c.categoria, c.nicho, 'Rua ' || chr(64 + (i % 26)) || ', ' || (100+i) , c.cidade, c.uf,
  case when i % 11 = 0 then null else '+55' || c.ddd || (case when i % 3 = 0 then '3' || lpad((2000000 + i*37)::text,7,'0') else '9' || lpad((90000000 + i*7919)::text,8,'0') end) end,
  (i % 3 <> 0 and i % 11 <> 0),
  case when i % 4 = 0 then 'https://exemplo' || i || '.com.br' end,
  round((3.4 + (i*13 % 17)/10.0)::numeric, 1), 5 + (i*29 % 400),
  'https://maps.google.com/?cid=' || i, 'apify',
  st.status, case when st.status='descartado' then (array['fora_do_perfil','sem_contato','duplicado','numero_invalido','pediu_para_nao_receber','outro'])[1 + (i % 6)] end,
  case when st.status='descartado' and i % 6 = 5 then 'Fora da região de atuação' end,
  case when st.status in ('encontrado') then null else cl.classe end,
  case when st.status in ('encontrado') then null else cl.nota end,
  case when st.status in ('encontrado') then null else 1 end,
  case when st.status = 'encontrado' then null else (array['ate_30k','30k_80k','30k_80k','80k_200k','sem_estimativa','200k_500k'])[1 + (i % 6)] end,
  case when st.status = 'encontrado' then null else 'calculado' end,
  case when st.status = 'encontrado' then null else 'Estimado a partir de: porte ME, ' || (5 + (i*29 % 400)) || ' avaliações' end,
  case when st.status in ('respondeu','qualificado','entregue') then (array['ate_30k','30k_80k','80k_200k','30k_80k'])[1 + (i % 4)] end,
  case when st.status = 'encontrado' then 'nao_verificado' else (array['nenhuma','fraca','media','forte','media'])[1 + (i % 5)] end,
  'nao_verificado',
  null,
  case when st.status = 'encontrado' then 'nao_verificado' when i % 6 = 0 then 'nao_encontrado' when i % 13 = 0 then 'revisao' else 'encontrado' end,
  case when st.status = 'encontrado' then 'nao_verificado' when i % 3 = 0 then 'nao_encontrado' else 'encontrado' end,
  case when i % 4 = 0 then 'encontrado' when st.status='encontrado' then 'nao_verificado' else 'nao_encontrado' end,
  now() - ((i % 20) || ' days')::interval, now() - ((i % 20) || ' days')::interval
from generate_series(1,175) i
cross join lateral (select (array['Barbearia','Barbearia','Barbearia','Clínica odontológica','Loja de celulares'])[1 + (i % 5)] categoria,
   (array['barbearia','barbearia','barbearia','clínica odonto','loja de celular'])[1 + (i % 5)] nicho,
   (array['Teresina','Teresina','Fortaleza','Teresina','São Luís'])[1 + (i % 5)] cidade,
   (array['PI','PI','CE','PI','MA'])[1 + (i % 5)] uf,
   (array['86','86','85','86','98'])[1 + (i % 5)] ddd) c
cross join lateral (select (array['encontrado','enriquecido','pontuado','pontuado','pontuado','pontuado','aprovado_para_contato','contatado','contatado','respondeu','qualificado','entregue','descartado','descartado'])[1 + (i*5 % 14)] status) st
cross join lateral (select least(100, (i*37 % 101)) nota, case when (i*37 % 101) >= 70 then 'quente' when (i*37 % 101) >= 40 then 'morno' else 'frio' end classe) cl;

-- descartados precisam de texto quando 'outro'
update radar.leads set motivo_descarte_texto = 'Fora da região de atuação' where motivo_descarte = 'outro';

insert into radar.pesquisas_leads (pesquisa_id, lead_id)
select case when id::text like '%1' or id::text like '%3' then 'aaaaaaaa-0000-0000-0000-000000000001'::uuid
            when cidade = 'Fortaleza' then 'aaaaaaaa-0000-0000-0000-000000000003'::uuid
            else 'aaaaaaaa-0000-0000-0000-000000000002'::uuid end, id from radar.leads;

-- empresas (CNPJ) para quem tem cnpj encontrado
insert into radar.empresas (lead_id, escolhido, cnpj, razao_social, nome_fantasia, situacao, abertura, porte, natureza, capital_social, cnae_principal, cnae_descricao, regime, qtd_socios, socios, confianca_cnpj, motivo_confianca, fonte)
select id, true, lpad((10000000000000 + row_number() over ())::text, 14, '0'), upper(split_part(nome,' ',1)) || ' S SILVA ME', nome, 'ATIVA', date '2019-03-01' + (random()*1500)::int, 'ME', 'Empresário individual', 5000, '9602-5/01', 'Cabeleireiros, manicure e pedicure', 'Simples Nacional', 1, '[{"nome":"José da Silva","qualificacao":"Sócio-Administrador"}]', 95, 'nome fantasia e endereço iguais', 'casa_dos_dados'
from radar.leads where cnpj_status = 'encontrado';
insert into radar.empresas (lead_id, escolhido, cnpj, razao_social, situacao, confianca_cnpj, motivo_confianca, fonte)
select id, false, lpad((20000000000000 + row_number() over ())::text, 14, '0'), 'CANDIDATO A LTDA', 'ATIVA', 45, 'só o nome parecido', 'casa_dos_dados' from radar.leads where cnpj_status = 'revisao';
update radar.leads set revisao_cnpj = true where cnpj_status = 'revisao';

insert into radar.presenca (lead_id, instagram_user, instagram_origem, instagram_confianca, seguidores, seguindo, posts_total, ultimo_post_em, posts_30d, bio, bio_tem_whatsapp, conta_comercial, site_existe, site_responde, site_https, site_tem_whatsapp, site_tem_meta_pixel, fonte_instagram, fonte_site)
select id, lower(regexp_replace(nome, '[^A-Za-z0-9]', '', 'g')), 'ficha_maps', 90, 300 + (random()*5000)::int, 200, 120, now() - ((random()*20)::int || ' days')::interval, (random()*15)::int, 'Agende pelo WhatsApp', true, true, site is not null, site is not null, site is not null, false, false, 'apify', 'robo_site'
from radar.leads where instagram_status = 'encontrado';
insert into radar.avaliacoes (lead_id, autor, nota, texto, data)
select l.id, 'Cliente ' || n, 3 + (n % 3), (array['Ótimo atendimento, recomendo!','Cortou muito bem, voltarei.','Demorou um pouco mas valeu a pena.','Ambiente agradável.','Preço justo.'])[n], now() - (n || ' days')::interval
from radar.leads l, generate_series(1,5) n where l.status <> 'encontrado' and l.nome like '%1';

insert into radar.regras_pontuacao (versao, nicho, pesos, faixas) values (1, 'barbearia', '{"tem_whatsapp":30,"presenca_media_ou_forte":15,"faturamento_30k_ou_mais":20,"google_bem_avaliado":20,"cnpj_ativo":15}', '{"quente":75,"morno":45,"nota_minima":4.0,"min_avaliacoes":50}');
insert into radar.pontuacoes (lead_id, versao_regra, regra_id, criterios, total)
select id, 1, 1, jsonb_build_array(
  jsonb_build_object('nome','presenca_media_ou_forte','bateu',presenca_digital in ('media','forte'),'pontos',case when presenca_digital in ('media','forte') then 20 else 0 end),
  jsonb_build_object('nome','tem_whatsapp','bateu',tem_whatsapp_provavel,'pontos',case when tem_whatsapp_provavel then 25 else 0 end),
  jsonb_build_object('nome','faturamento_30k_ou_mais','bateu',faturamento_estimado_faixa in ('30k_80k','80k_200k','200k_500k'),'pontos',case when faturamento_estimado_faixa in ('30k_80k','80k_200k','200k_500k') then 20 else 0 end),
  jsonb_build_object('nome','google_bem_avaliado','bateu',nota>=4 and qtd_avaliacoes>=50,'pontos',case when nota>=4 and qtd_avaliacoes>=50 then 20 else 0 end),
  jsonb_build_object('nome','cnpj_ativo','bateu',cnpj_status='encontrado','pontos',case when cnpj_status='encontrado' then 15 else 0 end),
  jsonb_build_object('nome','zera_fechado_ou_baixado','bateu',false,'pontos',0),
  jsonb_build_object('nome','zera_sem_canal','bateu',false,'pontos',0)), pontuacao
from radar.leads where pontuacao is not null;

-- conversas para leads que responderam
insert into radar.conversas (lead_id, estado, campos_coletados)
select id, case when status='entregue' then 'encerrada' else 'ia' end, '{"decisor":"sim, é o dono","faturamento_informado":"30 a 80 mil (\"uns 40 mil\")","investe_anuncio":"não","dor":"de terça a quinta fica vazio","melhor_horario":"depois das 14h","aceita_contato":"sim"}'
from radar.leads where status in ('respondeu','qualificado','entregue');
insert into radar.mensagens (conversa_id, canal, autor, texto, recebida_em)
select c.id, 'whatsapp_nao_oficial', m.autor, m.texto, now() - m.atras::interval from radar.conversas c, (values
 ('ia','Oi, falo com o responsável? Vi que vocês têm boa nota no Google. Se não quiser receber, responda SAIR.','3 hours'),
 ('lead','Sou o dono. Fim de semana enche, de terça a quinta fica vazio.','2 hours 45 minutes'),
 ('ia','Entendi. E de faturamento, mais ou menos quanto entra por mês?','2 hours 44 minutes'),
 ('lead','Uns 40 mil.','2 hours 30 minutes')) as m(autor,texto,atras);

-- histórico de status (dois eventos por lead avançado) e de uma edição
insert into radar.historico (entidade, entidade_id, acao, antes, depois, usuario_id, em)
select 'lead', id::text, 'status', '{"status":"pontuado"}', jsonb_build_object('status', status), null, now() - interval '1 day' from radar.leads where status in ('aprovado_para_contato','contatado','respondeu','qualificado','entregue');
insert into radar.historico (entidade, entidade_id, acao, antes, depois, usuario_id, em)
select 'lead', id::text, 'status', '{"status":"contatado"}', '{"status":"respondeu"}', null, now() - interval '20 hours' from radar.leads where status in ('respondeu','qualificado','entregue');
insert into radar.historico (entidade, entidade_id, acao, antes, depois, usuario_id, em)
select 'lead', id::text, 'status', '{"status":"respondeu"}', '{"status":"qualificado"}', null, now() - interval '10 hours' from radar.leads where status in ('qualificado','entregue');
insert into radar.historico (entidade, entidade_id, acao, antes, depois, usuario_id, em)
select 'lead', id::text, 'status', '{"status":"qualificado"}', '{"status":"entregue"}', null, now() - interval '5 hours' from radar.leads where status = 'entregue';
update radar.leads set editado_por='22222222-2222-2222-2222-222222222222', editado_em = now() - interval '3 minutes' where place_id = 'place_1';
insert into radar.historico (entidade, entidade_id, acao, antes, depois, usuario_id, em)
select 'lead', id::text, 'editar_campo', '{"instagram":"@antigo"}', '{"instagram":"@barbeariadoze"}', '22222222-2222-2222-2222-222222222222', now() - interval '3 minutes' from radar.leads where place_id='place_1';

-- custos, jobs e disparos
insert into radar.custos_api (fonte, operacao, pesquisa_id, qtd, valor, estimado, em)
select 'apify','maps','aaaaaaaa-0000-0000-0000-000000000002', 60, 2.1, true, now()-interval '2 days';
insert into radar.custos_api (fonte, operacao, pesquisa_id, qtd, valor, estimado, em)
select 'casa_dos_dados','cnpj','aaaaaaaa-0000-0000-0000-000000000002', 60, 0.6, true, now()-interval '2 days';
insert into radar.jobs (tipo, pesquisa_id, status) values ('minerar','aaaaaaaa-0000-0000-0000-000000000003','feito');
insert into radar.jobs (tipo, lead_id, status) select 'enriquecer_site', id, 'pendente' from radar.leads where nome like '%9' and status='encontrado' limit 5;
insert into radar.abordagens (nome, canal, texto) values ('Sem anúncio ativo','whatsapp_nao_oficial','Oi, falo com o responsável pela {nome}?');
