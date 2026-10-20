-- SÓ para testar a Fase 3 no navegador (NÃO entra no reset): prepara a conexão por QR Code e alguns leads. Rodar: psql -v tok=... -f 90_demo_fase3.sql
update whatsapp_config set connection_type = 'baileys', status = 'connected', evolution_instance_token = :'tok', user_id = '44444444-4444-4444-4444-444444444444'
 where id = 'eeeeeeee-0000-0000-0000-000000000001';
update whatsapp_config set connection_type = 'baileys', status = 'disconnected', evolution_instance_token = :'tok', user_id = '44444444-4444-4444-4444-444444444444'
 where id = 'eeeeeeee-0000-0000-0000-000000000002';
insert into radar.pesquisas (id, nome, nicho_texto, lugar_estado, lugar_cidade, fonte_maps, status, total_encontrado, agency_id) values
 ('aaaaaaaa-0000-0000-0000-0000000000f1','Padarias Teresina','padaria','PI','Teresina','gosom_maps','concluida',7,'aaaaaaaa-1111-0000-0000-000000000001');
insert into radar.leads (id, place_id, nome, endereco, link_maps, fonte_maps, agency_id, telefone, tem_whatsapp_provavel, cidade, estado, nicho, status) values
 ('bbbbbbbb-0000-0000-0000-000000000001','p1','Padaria Aurora','Rua A, 1','https://maps/1','gosom_maps','aaaaaaaa-1111-0000-0000-000000000001','(86) 99999-0001',true,'Teresina','PI','padaria','pontuado'),
 ('bbbbbbbb-0000-0000-0000-000000000002','p2','Padaria Belém','Rua B, 2','https://maps/2','gosom_maps','aaaaaaaa-1111-0000-0000-000000000001','(86) 99999-0002',true,'Teresina','PI','padaria','pontuado'),
 ('bbbbbbbb-0000-0000-0000-000000000003','p3','Padaria Sem Zap','Rua C, 3','https://maps/3','gosom_maps','aaaaaaaa-1111-0000-0000-000000000001','(86) 3222-1111',false,'Teresina','PI','padaria','pontuado'),
 ('bbbbbbbb-0000-0000-0000-000000000004','p4','Padaria Sem Telefone','Rua D, 4','https://maps/4','gosom_maps','aaaaaaaa-1111-0000-0000-000000000001',null,false,'Teresina','PI','padaria','pontuado'),
 ('bbbbbbbb-0000-0000-0000-000000000005','p5','Padaria Bloqueada','Rua E, 5','https://maps/5','gosom_maps','aaaaaaaa-1111-0000-0000-000000000001','(86) 99999-0005',true,'Teresina','PI','padaria','pontuado'),
 ('bbbbbbbb-0000-0000-0000-000000000006','p6','Padaria Sem Whats','Rua F, 6','https://maps/6','gosom_maps','aaaaaaaa-1111-0000-0000-000000000001','(86) 90000-0000',true,'Teresina','PI','padaria','pontuado');
insert into radar.pesquisas_leads (pesquisa_id, lead_id, agency_id) select 'aaaaaaaa-0000-0000-0000-0000000000f1', id, agency_id from radar.leads;
insert into radar.bloqueios (telefone, lead_id, motivo, agency_id) values ('+5586999990005','bbbbbbbb-0000-0000-0000-000000000005','pediu_para_nao_receber','aaaaaaaa-1111-0000-0000-000000000001');
