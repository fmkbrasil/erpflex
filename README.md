# V1.3.48 — Interface comercial, textos globais e BI mobile

Esta versão profissionaliza a interface para uso comercial, amplia a personalização de textos e cria uma experiência dedicada para BI em smartphones.

## Principais mudanças

- Menu superior reorganizado por áreas de negócio: Início, BI, Faturamento, Financeiro, Compras, Logística e Cadastros.
- Configurações técnicas agrupadas no submenu **Sistema**.
- Menu mobile compacto, sem a sequência horizontal de todos os módulos.
- Editor visual ampliado para títulos, descrições, botões, labels, mensagens, placeholders, tooltips e textos de navegação.
- Substituições de textos continuam por empresa e não alteram dados do ERP.
- A personalização também é aplicada à tela de login usando a identidade da empresa padrão.
- KPIs do BI padronizados em grade estável, com alturas e espaçamentos consistentes.
- No smartphone, cada módulo do BI passa a ter submenus internos; apenas o resultado selecionado fica visível por vez.
- Desktop continua exibindo o conjunto completo do BI.
- Schema permanece 15; não há migração estrutural de banco.

---

# V1.3.47 — Drill-down completo do BI

Esta versão padroniza a navegação analítica do BI: valores consolidados podem ser expandidos até documentos e registros que compõem o resultado, preservando os filtros e os vínculos reais existentes no banco. O schema permanece 15.

Esta versão melhora o painel **BI Gerencial → Contas a Pagar** sem alterar o schema do banco. O dashboard passa a separar explicitamente o valor **previsto por vencimento** do valor **realizado por data de pagamento**, mostrar o desvio de caixa e a execução percentual, detalhar títulos vencidos por faixa de atraso, destacar fornecedores com maior saldo vencido e exibir o fluxo de caixa conhecido para 7, 15 e 30 dias.

Regras principais:

- **Previsto no período** = valor nominal dos títulos `PAGAR` cujo vencimento está no intervalo selecionado.
- **Realizado no período** = valor efetivamente baixado dos títulos `PAGAR` cuja data de pagamento está no intervalo.
- **Desvio realizado × previsto** = realizado − previsto. Ele não é tratado como saldo restante, pois o realizado pode incluir atrasados ou antecipações.
- **Vencido em aberto** usa a posição atual dos títulos e é separado em 1–7, 8–15, 16–30, 31–60, 61–90 e acima de 90 dias.
- **Pago em atraso** considera pagamentos do período cuja data de pagamento ocorreu depois do vencimento.
- **Fluxo de caixa de curto prazo** cruza pagamentos previstos e recebimentos conhecidos nos próximos 7/15/30 dias. Não representa saldo bancário real.
- Cobertura das datas de vencimento e pagamento é exibida para evitar interpretar dados incompletos como zero real.
- A exportação Excel ganhou a aba **Pagar Resumo** e a listagem de títulos inclui status e dias em atraso.

O schema permanece **15**.

# V1.3.41 — Cadastros completos, bancos e BI hierárquico

Esta versão corrige a resolução de nomes de bancos no Financeiro/BI, amplia o parser de endereços de clientes com as estruturas reais `dadosComerciais`/`dadosResidenciais` e aliases legados, reconhece o bloco `endereco` dos fornecedores e também aproveita o endereço do XML da NF-e de entrada, além de evitar que ausência de datas financeiras seja exibida como falso `R$ 0,00`. O BI Comercial passa a apresentar **Vendedor → Categorias** e **Cliente → Categorias** em ramificações expansíveis. O schema permanece **15**; não há migração estrutural.

# V1.3.40 — Interface configurável, acesso e BI mobile

Esta versão simplifica a Central de Sincronização, cria personalização de cores e legendas por empresa, melhora o BI para smartphone, adiciona paginação automática às tabelas detalhadas e amplia o controle de acesso com permissões próprias para BI e customização visual. O schema passa para 15 de forma aditiva, com `ui_theme_settings` e `ui_label_overrides`.

# V1.3.18 — Consultas de documentos, filtros e espelho DANFE

Filtros rápidos, colunas personalizáveis, paginação e visualização/impressão de NF-e em formato de espelho DANFE.

# V1.3.17 — Faturamento regressivo pela última página válida

O Faturamento agora localiza a última página válida comprovada e percorre as páginas para trás até ultrapassar a data inicial. Períodos antigos usam a busca binária do Analytics V7.8 somente como fallback. O banco continua no schema 5 e os dados existentes são preservados.

# V1.3.16 — Progresso de Sincronização

Esta versão mantém o motor ERPFlex V7.8 nativo em Go da V1.3.15 e adiciona acompanhamento visual da sincronização: barra de progresso geral, módulo/fase atual, contador de registros, heartbeat do worker e aviso quando a mesma etapa demora muito. O schema passa para 5 apenas para criar a tabela aditiva `sync_progress`; os dados operacionais existentes são preservados.

# V1.3.13 — Faturamento com cursor autocorretivo + Compras detalhadas

Esta versão corrige a leitura do Faturamento para seguir a extração de arrays do Analytics V7.8 e prioriza a varredura da última página para trás em períodos recentes. Em Compras, o detalhe de cada compra do período pode ser enriquecido pelo endpoint individual, preservando cabeçalho, itens e campos fiscais. A tela de Compras agora possui Resumo em formato de nota e impressão A4. O schema permanece 4 e a mesma base central é reutilizada.

# V1.3.10 — sincronização estável

Esta versão corrige o fluxo de Faturamento que podia permanecer muito tempo em EM_EXECUCAO na V1.3.9. O algoritmo histórico foi alinhado ao ERPFlex Analytics V7.8 e a tela mostra a etapa atual em tempo real. Não há alteração de schema nem recriação da base.

# Plataforma Gestão Integrada V1.3.9 — Motor ERPFlex baseado no Analytics V7.8

## V1.3.9 — integração ERPFlex revisada a partir do aplicativo comprovado

A camada de consulta ERPFlex foi revisada usando como referência direta o código-fonte do **ERPFlex Analytics V7.8** fornecido pelo usuário. A Plataforma Integrada mantém sua base única e o Roteirizador, mas passa a reproduzir as estratégias comprovadas de endpoints, offsets e localização histórica do V7.8.

Principais mudanças:

- conexão HTTP persistente, com keep-alive e timeout de leitura compatível com chamadas lentas de Faturamento;
- respostas HTTP de borda (inclusive 5xx) não são confundidas com falhas de transporte durante a descoberta de paginação;
- Faturamento usa `/api_v2/faturamento/P{página}` e âncora validada do V7.8, localizando o período por busca binária;
- Contas a Receber usa `/api_v2/baixareceita/pagina/{posição}` e Contas a Pagar usa `/api_v2/baixadespesa/pagina/{posição}`, sempre com posição numérica;
- Pedidos usam `/api/venda/solicitacoes/{offset}` com a estratégia histórica do V7.8;
- o estado de navegação é persistido em `sync_cursors`, evitando redescoberta completa e preservando continuidade entre versões;
- schema 4 é criado por migração sem apagar os dados existentes.


A V1.3 utiliza o núcleo central criado na V1.2 e migra o fluxo principal do **Roteirizador de Entregas** para a base única.


## V1.3.6 — persistência entre versões e credenciais no sistema

Esta versão corrige a infraestrutura antes de avançar com BI/API em produção. Agora existe uma tela **Integrações** onde o administrador pode gravar e testar as credenciais do **ERPFlex** e do **NF Stock** por empresa. Senha e token são criptografados antes de serem persistidos no banco central e nunca voltam preenchidos para o navegador. O `.env` continua disponível apenas como fallback.

A sincronização passou a consultar primeiro as configurações salvas no banco. Também foi criado controle de versão do schema (`schema_state`) e backup automático do SQLite antes de mudanças estruturais. Em instalações locais sem `DATA_DIR`, o SQLite passa a ficar em uma pasta persistente fora do diretório da versão. Na primeira execução, se esse destino estiver vazio, a aplicação procura automaticamente uma base `data/plataforma_integrada.db` de uma versão irmã mais antiga e copia a mais recente sem apagar a origem.

**Importante:** em uso local/SQLite a V1.3.6 cria `DATA_DIR/integration.key`, fora da pasta da versão, para que as credenciais continuem legíveis mesmo após trocar o ZIP. Em Railway/PostgreSQL, defina uma `INTEGRATION_SECRET_KEY` estável (ou preserve a `SECRET_KEY`) entre deploys.

## V1.3.5 — informar/corrigir região e memorizar

Nesta versão a classificação de região ficou mais direta para o operador: cada NF na tela **Roteirizar** possui a ação **Informar** ou **Corrigir** ao lado da região. A escolha abre os cards das regiões ativas, permite memorizar a regra por cliente/CNPJ e retorna para a mesma tela/filtro após gravar. A classificação em lote continua disponível para várias NFs.


- A tela **Roteirizar** mostra somente cards de regiões com NFs disponíveis.
- NFs selecionadas podem ter a região classificada em lote e essa região pode ser memorizada por cliente/CNPJ para próximas entregas.
- Roteirizações do mesmo motorista/data são consolidadas no mesmo roteiro PLANEJADO.
- A tela **Roteiros** usa cards de motorista e listas de NFs agrupadas por motorista/roteiro.
- É possível selecionar uma ou várias NFs e trocar o motorista em modal, retornando para a mesma tela para continuar os ajustes.
- A impressão do roteiro foi redesenhada para A4 paisagem, com cabeçalho, resumo, manifesto e assinaturas.


A regra arquitetural principal é simples: **a NF-e de saída existe uma única vez** em `sales_invoices`. O módulo logístico acrescenta região, responsável, caixa, roteiro, status, KM, custo, ocorrência e histórico por relacionamento, sem duplicar a nota.

## V1.3.2 — operação simplificada do Roteirizador

- **Baixa somente para entregas roteirizadas**: a tela de baixa exibe apenas entregas vinculadas a roteiro ativo (PLANEJADO ou EM ROTA), e o backend também bloqueia baixa direta de NF não roteirizada.
- **Baixa em lote**: permite selecionar uma, várias ou todas as entregas exibidas e dar baixa como ENTREGUE.
- **Reprogramação em lote**: as selecionadas passam para REPROGRAMADA e são retiradas do roteiro ativo para voltarem à fila de roteirização.
- **Cards coloridos de motoristas** na tela Roteirizar, usados tanto como filtro rápido quanto para definir o responsável das NFs selecionadas.
- **Layout operacional em etapas**: filtrar → selecionar NFs/motorista → criar roteiro.
- **Troca de motorista após roteirização** dentro do detalhe do roteiro, usando cards; funciona para uma NF, várias ou todas. A NF é movida para um roteiro do novo motorista na mesma data, mantendo histórico da alteração.
- A base fiscal continua única; nenhuma NF é duplicada para executar essas operações.

## Organização do menu Logística — V1.3.1

As funções do Roteirizador deixam de ocupar vários itens do menu principal. Agora existe um único acesso **Logística**, que abre uma central organizada em **Operação**, **Gestão** e **Cadastros e regras**. Em todas as telas logísticas também é exibido um menu secundário do módulo, respeitando as permissões do usuário. As URLs e funcionalidades da V1.3 foram preservadas.


## Fluxo de dados

```text
ERPFlex API ───────────────┐
                           │
Excel de contingência ─────┼──> NF central / Cliente / Endereço
                           │              │
NF Stock API ──────────────┘              ▼
                                   Entrega logística
                                           │
                         Região -> Responsável -> Roteiro
                                           │
                         Em rota -> Baixa -> Ocorrência
                                           │
                            Painéis / Relatórios / Custos
```

O ERPFlex continua sendo a origem preferencial dos dados comerciais/fiscais. A importação Excel existe apenas como contingência operacional e alimenta a mesma estrutura.

## Roteirizar

`/roteirizar` lista NFs PENDENTES ou REPROGRAMADAS disponíveis para roteiro.

Recursos:

- pesquisa por NF, cliente, endereço, município ou bairro;
- filtro por região e responsável;
- classificação automática de região;
- alteração em lote de região, responsável e caixa;
- memorização das escolhas por cliente/CNPJ;
- seleção de NFs e criação de roteiro;
- quantidade de volumes e valor transportável;
- links Google Maps e Waze;
- alertas de endereço.

Não existe filtro de **Previsão/Entrega** nas telas operacionais. Essa data pode permanecer armazenada na NF e é usada apenas quando o usuário solicita análise histórica nos Relatórios Gerenciais.

## Regras logísticas consolidadas

A V1.3 reaproveita as regras comprovadas no Roteirizador V46.5.20, incluindo:

- `Transportadora 2 = DESTINATARIO COLETA` -> região **COLETA** e responsável COLETA;
- `LEBANON CENTRO` -> **Centro**;
- `LEBANON ÔNIBUS/ONIBUS` -> **Centro** e alerta **VERIFICAR ENDEREÇO NA NOTA FISCAL**;
- classificação por município, CEP e bairro para as zonas de São Paulo e regiões especiais;
- exibição do cliente como `RAZÃO SOCIAL — NOME FANTASIA` quando os dois dados existem e são diferentes.

Regiões iniciais:

- Zona Sul
- Zona Leste
- Zona Norte
- Zona Oeste
- Centro
- ABC Paulista
- Guarulhos
- Osasco / Barueri
- Alto Tietê
- Interior SP
- Litoral SP
- COLETA
- A Classificar
- Outras Regiões

## Roteiros

Os roteiros usam as entregas da base central.

- responsável e data do roteiro;
- ordem das paradas editável enquanto PLANEJADO;
- início da rota altera entregas aplicáveis para EM ROTA;
- finalização do roteiro;
- volumes e valor total;
- Maps/Waze por parada;
- impressão amigável para A4 e opção de Salvar como PDF pelo navegador;
- proteção para não colocar a mesma entrega simultaneamente em dois roteiros ativos.

## Baixa de Entregas

A tela `/baixa-entregas` mantém **somente o filtro de status**, conforme a regra operacional definida para o projeto.

Status suportados:

- PENDENTE
- EM ROTA
- ENTREGUE
- NAO ENTREGUE
- OCORRENCIA
- REPROGRAMADA

Mudanças de status geram histórico. Uma baixa como OCORRENCIA também cria o registro correspondente para acompanhamento.

## Painel de Entregas e Painel Gerencial

O Painel de Entregas apresenta a situação operacional da base central, com filtros de status/responsável/busca.

O Painel Gerencial consolida:

- NFs/entregas;
- volumes;
- valor transportado;
- custo de frete;
- KM;
- percentual entregue;
- entregas por região;
- resultado/custo por responsável.

Região e responsável são clicáveis e permitem conferir as NFs que formam o indicador.

### Regra de Frete / Faturamento

A V1.3 preserva a regra consolidada do Roteirizador:

```text
SOMA DOS CUSTOS DE FRETE INFORMADOS
÷
FATURAMENTO DOS RESPONSÁVEIS COM CUSTO DE FRETE INFORMADO/APURADO
```

O faturamento de responsáveis sem custo não entra no denominador.

## Relatórios Gerenciais

`/relatorios-gerenciais` é a área histórica/analítica e, por isso, pode filtrar por período de Previsão/Entrega, além de região, responsável, tipo e status.

Possui:

- resumo executivo;
- por motorista/responsável;
- por região;
- matriz Motorista x Região;
- terceirizados/transportadoras;
- detalhamento das NFs;
- impressão / Salvar PDF pelo navegador;
- exportação Excel.

O Excel contém:

1. Resumo
2. Por Motorista
3. Por Região
4. Motorista x Região
5. Terceirizados
6. NFs Detalhadas

## Importação Excel de contingência

`/importar-logistica` aceita XLSX/CSV compatível com os campos usados pelo Roteirizador legado.

A importação:

- cria/atualiza a NF na tabela central;
- atualiza cliente e endereço;
- cria a entrega quando necessário;
- aplica classificação e regras logísticas;
- registra o histórico em `logistics_imports`;
- não apaga dados antigos;
- não cria um banco paralelo.

Quando posteriormente a mesma NF chegar pelo ERPFlex, o sincronizador tenta convergir para o registro central existente usando chave de acesso e, na ausência dela, número único da NF dentro da empresa.

## Autenticação, empresas e permissões

A V1.3 mantém todo o núcleo da V1.2:

- usuários no banco;
- senhas PBKDF2-SHA256 + salt;
- perfis/permissões no backend;
- Empresa/Filial como contexto de acesso;
- Administração e auditoria;
- API interna `/api/v1`.

Perfis padrão continuam Administrador, Diretoria e Operacional. O perfil Operacional recebeu as permissões necessárias para trabalhar com roteirização, importação de contingência, responsáveis, regras e parâmetros logísticos.

## Banco de dados e atualização entre versões

Em produção, prefira PostgreSQL via `DATABASE_URL`. Sem `DATABASE_URL`, o aplicativo usa SQLite. Se `DATA_DIR` estiver vazio, o diretório padrão é persistente e fica fora da pasta do ZIP/versão (no Windows, em `LOCALAPPDATA/PlataformaGestaoIntegrada/data`; em outros sistemas, na pasta pessoal do usuário).

A V1.3.6 introduz `schema_state` e schema versão **2**. Antes de uma alteração de schema, um SQLite existente recebe cópia em `DATA_DIR/backups`. `create_all` é usado apenas para estruturas ausentes; tabelas existentes não são recriadas nem apagadas. O botão **Criar backup agora** aparece em Integrações quando o banco é SQLite.

Para PostgreSQL, a aplicação não executa `pg_dump`: use os backups/snapshots do provedor antes de migrações relevantes.

## Configuração e execução

Copie `.env.example` para `.env` para definir aplicação, banco e chave criptográfica. Depois do primeiro login, prefira cadastrar **ERPFlex** e **NF Stock** em **Integrações**; não é mais necessário manter senha/token no `.env`.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

No Windows, pode ser usado `INICIAR_WINDOWS.bat`.

O pacote inclui `Dockerfile` e `railway.json` para Railway.

## Limites planejados para a próxima etapa

Esta V1.3 ainda não migra:

- otimização automática Google Routes;
- geocodificação automática avançada;
- frota/manutenção/documentos;
- Portal Motorista mobile/PWA;
- foto de ocorrência pelo smartphone.

Esses recursos podem ser acrescentados sobre a mesma base central sem recriar NF, clientes ou entregas.

## V1.3.3 — fluxo simplificado de roteirização

A tela `/roteirizar` agora é orientada por **zona**, não por motorista. Os cards coloridos representam Zona Sul, Zona Leste, Zona Norte, Zona Oeste, Centro, COLETA e demais regiões cadastradas. Após filtrar a zona, o operador marca as NFs e usa **Roteirizar selecionadas**. Uma janela exibe os cards dos motoristas/responsáveis; ao escolher um card e gravar, o roteiro é criado e a tela retorna à mesma zona para que o operador continue a rotina. A escolha do motorista deixou de ocupar espaço permanente na tela principal.

## V1.3.7 — sincronização modular e cadastros completos

A Central de Sincronização do ERPFlex passa a permitir marcar vários módulos ou usar **Sincronizar tudo disponível**. Cadastros mestres e movimentações são separados: Produtos e Bancos não dependem de período; Clientes conhecidos podem receber enriquecimento pelo endpoint individual de cliente já validado. Pedidos, Faturamento, Contas a Receber, Contas a Pagar, Compras e Despesas utilizam o período informado. Despesas usam consulta diária; nos demais módulos o período é aplicado aos blocos/páginas consultados.

Produtos agora usam paginação extensa configurável (padrão de segurança: até 500 páginas de 50 registros), evitando a limitação anterior de 1.000 produtos quando o limite estava em 20 páginas.

A área **Cadastros** recebeu páginas separadas para Produtos, Clientes, Fornecedores, Bancos e Todos os parceiros, com pesquisa, 25/50/100/250 registros por página, escolha de colunas e ficha detalhada. Quando há `raw_record` da integração, a ficha mostra também o JSON integral recebido da API para que nenhum campo disponível seja perdido na visualização.

O schema passa para **3**, adicionando apenas a tabela `user_view_preferences`, usada para guardar por usuário/empresa as colunas e a quantidade de registros por página. Dados operacionais existentes não são recriados.

## V1.3.8 — correção de sincronização financeira/faturamento

Esta versão corrige a descoberta de paginação dos endpoints de Faturamento, Contas a Receber e Contas a Pagar e passa a exibir no histórico a mensagem técnica de falha em **Detalhes**. A alteração não recria o banco nem apaga dados já sincronizados.


## V1.3.13 — Autocorreção do cursor de Faturamento
Se o cursor salvo apontar uma página que não responde ou não possui registros, o sistema recua automaticamente até encontrar uma página válida. Somente páginas confirmadas com HTTP 200 e registros podem ser salvas como `last_valid`. O histórico da sincronização informa quando o cursor foi corrigido.


## V1.3.16 — Motor ERPFlex V7.8 em Go

A comunicação com o ERPFlex passa por padrão pelo bridge `bin/erpflex_v78_bridge_windows.exe` no Windows ou `bin/erpflex_v78_bridge` no Linux. O bridge foi derivado da implementação HTTP do ERPFlex Analytics V7.8 usado como referência comprovada. A aplicação Python envia credenciais e rota por `stdin`; senha não é colocada na linha de comando.

No Railway/Docker, o bridge é recompilado no estágio Go do Dockerfile. Em uso local no Windows, o executável x64 já acompanha o ZIP. `ERPFLEX_ENGINE=go_v78` é o modo padrão; `ERPFLEX_ENGINE=python` deve ser usado somente para diagnóstico.


## V1.3.16 — progresso de sincronização
A Central de Sincronização exibe barra de progresso, módulo/etapa atual e heartbeat do worker. Durante chamadas lentas da API, o heartbeat continua sendo atualizado independentemente da thread que aguarda o ERPFlex, permitindo diferenciar “API lenta” de ausência de sinal do processo. O percentual é estimado durante descoberta/paginação e exato ao processar os registros recebidos.

## V1.3.18 — consultas de documentos
Faturamento, Compras/NF-e e Financeiro agora compartilham um padrão de consulta com filtros rápidos Hoje/Semana/Mês/Personalizado/Todos, paginação 25/50/100/250 e seleção de colunas persistida por usuário. NFs de saída e entrada possuem detalhe em formato de espelho DANFE e impressão no mesmo layout. Títulos financeiros mostram detalhes e, quando o número do documento corresponde a uma NF sincronizada, oferecem acesso direto à NF-e e ao espelho DANFE.

O espelho DANFE é uma visualização interna construída com os dados disponíveis na integração; não substitui o DANFE fiscal oficial quando este não estiver disponível pela origem.

## V1.3.19
Melhoria das telas de Faturamento, Compras e Financeiro: produtos/itens nas consultas, fallback de itens do pedido para faturamento, enriquecimento visual de cliente/banco/carteira no financeiro e layout mais legível. Mantém schema 5.


## V1.3.20

Plataforma Gestão Integrada V1.3.20 — Carteira identificada + DANFE A4

1. Financeiro / Contas a Receber
- Adicionada consulta do endpoint comprovado no ERPFlex Analytics V7.8: GET /api/titulo_receber/{id}.
- Botão para enriquecer banco/carteira dos títulos exibidos na página (até 100 por execução).
- Botão individual no detalhe do título.
- O retorno enriquecido é preservado em raw_records com módulo receber_detalhe.
- Banco/carteira passam a usar primeiro o Título V1 quando disponível.
- Quando houver apenas id_carteira, a tela exibe explicitamente Carteira <ID> em vez de “Sem carteira identificada”.
- IDs de banco/carteira são exibidos como informação auxiliar quando disponíveis.

2. Espelho DANFE
- Layout totalmente reformulado seguindo o padrão visual do modelo fornecido pelo usuário.
- A4 retrato, canhoto, identificação do emitente, bloco DANFE, chave, natureza/protocolo, destinatário/remetente, cálculo de imposto, transporte/volumes, produtos e dados adicionais.
- Aplicado a NF-e de saída e NF-e de entrada/Compras.
- Impressão usa o mesmo layout.
- Mantido destaque “SEM VALOR FISCAL” porque o documento é um espelho interno, não substitui DANFE/XML oficial.

3. Compatibilidade
- Schema permanece 5.
- Nenhuma tabela é apagada ou recriada.
- Dados sincronizados e configurações existentes são preservados.


## V1.3.21

Correção de identificação e persistência dos itens de Faturamento e Compras, alinhada aos identificadores reais usados pelo ERPFlex Analytics V7.8.

## V1.3.22 — itens e dados bancários
O Faturamento passou a consultar o endpoint específico de itens por faturamento quando o resumo paginado não contém os produtos. Itens são armazenados em `sales_invoice_items`. Compras cruzam identificadores dos itens com o cadastro central de produtos para completar descrições. No Financeiro, banco/carteira são enriquecidos usando o Título a Receber e o detalhe individual do banco; quando a API não fornece nome de carteira, o sistema preserva e exibe o ID sem inventar descrição.


## V1.3.23 — correção de itens e dados financeiros

- Remove o uso operacional do endpoint não validado `/api_v2/faturamento/itens/{id}`.
- Itens de faturamento passam a seguir a relação comprovada do Analytics V7.8: `orcamento_id`/pedido relacionado.
- Quando o pedido local não possui itens, usa o endpoint oficial `GET /api/venda/solicitacao/{id}` e armazena o detalhe para reaproveitamento.
- `/faturamento/atualizar-itens` foi protegido para nunca gerar Internal Server Error por falha de enriquecimento.
- Cadastro de bancos passa a reconhecer os campos reais `SA6_ID`, `SA6_Codigo`, `SA6_Desc` e `SA6_Carteira`.
- A atualização financeira recarrega `/api/bancos/` uma vez e cruza `banco_id` com o nome real (ex.: IDs internos como 25325).
- Carteira: exibe o nome quando a API o fornece; quando só houver ID, mantém `Carteira <ID>` sem inventar descrição.
- Schema permanece 6; nenhuma tabela existente é recriada.

## V1.3.24
Correção de referência fiscal (NF-e x pedido/documento), dados de pagamento no Faturamento, situação ativa/cancelada, nome de carteira via opções de boleto e consulta/impressão/reenvio de boleto bancário com SMTP configurável.

## V1.3.27 — Cobrança automática
No Financeiro > Contas a receber, acesse **Cobrança automática** para configurar avisos a vencer/vencidos, intervalo mínimo de reenvio e marcar clientes com contrato de cobrança. A automação usa o SMTP já configurado e roda em verificação horária quando ativada.

Em Compras, o filtro **Notas fiscais** inicia em **Somente NF > 0**. Use **Todas** ou **Somente NF 0** quando precisar auditar registros não fiscais.


## V1.3.28 — DANFE unificado e ações financeiras

- Remove o botão redundante **NF-e** da coluna Ações do Financeiro; a referência fiscal continua em sua coluna própria e o DANFE permanece acessível.
- Faturamento e Financeiro/Contas a Receber passam a imprimir pelo mesmo template de DANFE usado como padrão principal.
- Compras também usa o mesmo template de impressão, garantindo uma única fonte de layout para DANFE de entrada e saída.
- O indicador 0-Entrada / 1-Saída agora respeita a origem do documento.
- Nenhum endpoint ERPFlex foi adicionado ou alterado.

## V1.3.29 — sincronização incremental e índice local de páginas

A Central de Sincronização mantém um índice persistente das páginas, posições e offsets já percorridos no ERPFlex. O índice registra cobertura de datas, IDs, quantidade de registros e assinatura do conteúdo para acelerar a localização de intervalos já conhecidos, sem duplicar os documentos na base.

A rotina normal continua incremental e preserva o checkpoint principal de cada módulo. Para auditoria ou recuperação, a opção **Forçar sincronização** permite escolher movimentações e um período específico. O reprocessamento usa o índice quando disponível, mas não move o checkpoint incremental principal para o passado.

O schema desta versão é 9 e a migração é somente aditiva (`sync_page_index`).

## V1.3.30 — worker de sincronização automática

A V1.3.30 adiciona um processo independente (`python -m app.worker`) para executar a sincronização ERPFlex em segundo plano, sem depender de usuário abrir a tela ou clicar em Sincronizar.

### O que foi criado

- agenda persistente por empresa e por módulo;
- frequência independente para Produtos, Bancos, Clientes, Pedidos, Faturamento, Contas a Receber, Contas a Pagar, Compras e Despesas;
- sincronização automática no modo incremental/recent, reaproveitando `sync_cursors` e `sync_page_index`;
- heartbeat do worker visível na Central de Sincronização;
- status Online / Offline / Pausado / Executando;
- botão para colocar os módulos ativos na fila imediatamente;
- janela de horário configurável;
- lease persistente no banco para impedir que Web e Worker executem a mesma integração ERPFlex simultaneamente;
- recuperação de sincronizações interrompidas baseada em heartbeat: reiniciar somente o serviço Web não interrompe mais uma sincronização viva no Worker;
- schema 10, somente com novas tabelas (migração aditiva).

### Railway

Use dois serviços apontando para o mesmo código e o mesmo PostgreSQL:

1. **Web**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
2. **Worker**: `python -m app.worker`

O Worker não abre porta HTTP. Configure uma única réplica do serviço Worker. Os dois serviços precisam compartilhar `DATABASE_URL`, `SECRET_KEY`, `INTEGRATION_SECRET_KEY` e as mesmas credenciais/variáveis necessárias às integrações. A configuração de agenda fica no banco e é editada em **Sincronização → Robô de sincronização**.

Por segurança, a automação é criada **desativada** após a atualização. Ative-a na Central de Sincronização depois que o serviço Worker estiver online.

## V1.3.32 — Dashboard Gerencial
A versão 1.3.32 adiciona o menu **Dashboard** (`/dashboard-gerencial`) com visão consolidada de Faturamento, Comercial, Compras, Contas a Receber/Aging, Contas a Pagar, Despesas e Logística. Os gráficos são renderizados localmente no navegador, sem dependências externas. O schema do banco permanece 10.

## V1.3.33 — Cadastro de clientes enriquecido
A versão 1.3.33 amplia o cadastro de Clientes usando exclusivamente o endpoint individual já validado `/api/cliente/{id}`. O cadastro passa a consolidar endereço principal, endereço de entrega, endereço de cobrança, forma/condição de pagamento e transportadora vinculada quando esses campos forem retornados pela integração. O JSON bruto permanece disponível na tela de detalhe para auditoria. O schema passa a 11 por migração aditiva da tabela `partners`; dados existentes são preservados. Após o deploy, execute a sincronização do módulo **Clientes** para popular os novos campos.

## V1.3.34 — BI Gerencial

Acesse **BI Gerencial** no menu ou diretamente em `/bi/visao-geral`.
O módulo lê somente o banco já sincronizado e separa indicadores por Faturamento, Financeiro, Comercial, Clientes, Produtos, Compras, Logística e Alertas. Métricas sem fonte confiável são exibidas como **Dados indisponíveis** em vez de estimadas.

## V1.3.35 — Ajustes do BI por produto, carteira, fornecedor e vendedor

O BI passa a exibir **código + nome** dos produtos/itens em Faturamento, Produtos e Compras. Contas a Receber recebe uma visão consolidada **por carteira**. Produtos ganha resultados por **categoria e subcategoria**; a subcategoria passa a ser normalizada no cadastro de produtos por migração aditiva para o schema 12.

Em Compras, quando o total do cabeçalho ERPFlex estiver zerado, o BI usa uma regra de fallback auditável: total ERPFlex → total NFStock → soma dos itens da própria NF-e. Isso corrige Total comprado, Ticket médio e os valores dos principais fornecedores sem criar valores simulados.

Em Comercial — Vendedores, metas foram removidas. A página mostra faturamento por vendedor, comparação com o mês anterior equivalente, crescimento percentual e o cruzamento **Vendedor × Categoria**.

## V1.3.36 — Vendedor, descrição de carteira e roteirização operacional

- Comercial — Vendedores passa a procurar o vendedor no JSON do faturamento/pedido detalhado e, quando necessário, usa somente vínculo exato com o pedido sincronizado por ID ou número. Não atribui vendedor pelo cliente.
- `sales_orders` recebe `seller_id` e `seller_name` por migração aditiva (schema 13). Os campos são preenchidos quando efetivamente retornados pela API; JSONs históricos continuam sendo consultados como fallback.
- A página de vendedores exibe diagnóstico da origem do vínculo, permitindo identificar quantas NF-e possuem vendedor e quantas permanecem sem identificação.
- Contas a Receber resolve a descrição de carteira usando, em ordem, detalhe do título, dados armazenados no título e catálogos já sincronizados de bancos/opções de boleto. Se só houver ID, mantém `Carteira <ID>` em vez de inventar uma descrição.
- A tela **Roteirizar** foi redesenhada para operação diária: fluxo visual em 3 etapas, pesquisa destacada, cards de região, cards de entrega, resumo fixo da seleção com quantidade/volumes/valor e ações mais claras para corrigir região e escolher motorista.


## V1.3.37 — Roteirização isolada por data operacional

- Tela inspirada no fluxo operacional do roteirizador V46.5: resumo de regiões, lista compacta de NFs e seleção clara do responsável.
- A tela abre por padrão somente a data de hoje.
- O operador escolhe uma data exata; nunca existe mistura automática de vários períodos na mesma lista.
- Data operacional = previsão/entrega; quando ausente, a emissão é usada apenas como fallback explícito e sinalizado.
- Contadores separados de NFs de hoje, anteriores, futuras e sem data.
- NFs de outras datas ficam fora da lista corrente e aparecem apenas como contagem/atalhos de datas pendentes.
- Cada card mostra previsão/entrega e emissão.
- O backend valida a data operacional novamente antes de criar o roteiro e bloqueia lote de período diferente.
- Data da NF e data do roteiro são apresentadas como conceitos distintos.
- Schema permanece 13; nenhuma migração de banco é necessária.

## V1.3.38 — logística visual, baixa operacional e endereços resilientes

- Roteirizador com cores mais vivas e maior contraste em período, regiões, seleção de NFs, motoristas e alertas.
- **Baixa de Entregas** redesenhada com cards de status, contadores, ícones, cores próprias para Pendente / Em rota / Entregue / Não entregue / Ocorrência e ações individuais mais claras.
- O endereço logístico agora usa a seguinte prioridade: endereço da NF → endereço de entrega do cadastro do cliente → endereço principal do cliente. A origem usada fica visível no roteirizador.
- O parser de chaves da API passou a tolerar diferenças de espaços, sublinhados, hífens, acentos e maiúsculas/minúsculas. Assim, por exemplo, `Endereco Entrega` e `endereco_entrega` são tratados como a mesma chave de integração.
- Quando o ERPFlex retorna o endereço de entrega em campos planos (`Endereco Entrega`, `Numero`, `Complemento`, `Bairro`, `Municipio`, `Estado`, `CEP`), os componentes são consolidados corretamente.
- Pesquisa e reclassificação de região também consideram o endereço do cadastro do cliente quando a NF não possui endereço materializado.
- Schema permanece **13**; nenhuma migração de banco é necessária.
- Após o deploy, recomenda-se sincronizar **Clientes** e **Faturamento** para reprocessar endereços já armazenados com o parser aprimorado.

## V1.3.39 — DRE Gerencial

A nova página **BI Gerencial → DRE Gerencial** cria um demonstrativo econômico gerencial usando somente informações verificáveis já sincronizadas. A Receita Bruta utiliza NF-e válidas do Faturamento e as despesas são obtidas somente do módulo **Despesas**, sem somar novamente Contas a Pagar.

As categorias reais encontradas no JSON das despesas podem ser classificadas pelo usuário em **Comerciais, Administrativas, Logísticas, Outras Operacionais, Financeiras** ou **Não classificadas**. O mapeamento é persistido por empresa e não altera o lançamento original do ERP.

A DRE mostra período atual x período anterior, visão mensal do ano, detalhamento dos lançamentos e exportação para Excel. Quando não há base suficiente para uma linha — por exemplo **deduções/tributos sobre vendas, CMV, Receita Líquida ou Lucro Bruto** — a plataforma mostra **Dados indisponíveis** em vez de estimar números. Compras do mês nunca substituem o CMV.

O indicador **Resultado gerencial parcial** representa apenas `Receita Bruta - Despesas conhecidas`; ele é identificado explicitamente como parcial e não deve ser tratado como lucro contábil enquanto deduções, tributos e CMV não estiverem normalizados.

O schema passa para **14** com a nova tabela aditiva `dre_expense_mappings`. Dados existentes são preservados.

## V1.3.40 — interface, acesso e mobile

- Central de sincronização simplificada, personalização de cores/legendas, BI responsivo, paginação visual e permissões adicionais.
- Schema 15 com `ui_theme_settings` e `ui_label_overrides`.

## V1.3.41 — qualidade de dados do BI e cadastros

- Reforço da resolução de bancos/carteiras, parser de endereços de clientes/fornecedores e hierarquias Vendedor × Categoria e Cliente × Categoria.
- Schema permanece 15.

## V1.3.42 — histórico Cliente × Produto e Fornecedor × Produto

- O detalhe do cliente passa a mostrar os produtos comprados, total faturado, quantidade acumulada, preço médio, primeira/última compra e histórico das NF-e de saída.
- O detalhe do fornecedor passa a mostrar os itens efetivamente comprados desse fornecedor, valores, quantidades, faixa de preço e histórico das NF-e de entrada.
- O detalhe do produto recebe a visão inversa: clientes que compraram e fornecedores que forneceram o item.
- O cadastro mestre de fornecedores pode ser sincronizado diretamente pela API REST documentada do ERPFlex (`POST /v1.2/auth` + `GET /v1.5/fornecedor?lastid=...`), usando sessão/token sem substituir o motor V7.8 dos demais módulos.
- A Central de Sincronização passa a oferecer o módulo **Fornecedores** em execução manual e no Worker. O módulo inicia desabilitado em instalações existentes até ser marcado pelo operador.
- O vínculo histórico produto-fornecedor é calculado pelas compras/NF-e já sincronizadas; não são criadas relações artificiais quando não existe documento de compra.
- O schema permanece **15**; nenhuma migração estrutural é necessária.


## V1.3.43 — ERPFlex Cadastros v1
A API REST v1.x usada por Fornecedores agora possui configuração independente em Integrações. O motor V7.8 permanece inalterado. Teste a conexão v1 antes de habilitar/reprocessar Fornecedores. O histórico de sincronização mostra o motivo técnico das falhas na coluna Detalhe.

## V1.3.44 — Dashboard de Faturamento

A área `BI Gerencial > Faturamento` foi transformada em um dashboard de análise comercial/fiscal com drill-down. Ao acessar diretamente `/bi/faturamento`, o período padrão é Hoje.

Recursos principais:
- faturamento do período e comparação com mês anterior equivalente, período anterior ou período específico;
- produtos mais vendidos com expansão para as NF-e relacionadas;
- categoria > subcategoria > produtos;
- venda por vendedor;
- estado > cidade;
- cliente > NF-e > detalhe completo da nota;
- filtros globais do BI preservados;
- layout responsivo.

A receita principal continua sendo calculada pelo cabeçalho das NF-e válidas. Resultados por produto/categoria/subcategoria são baseados nos itens identificados e podem não somar exatamente ao total fiscal quando houver frete, tributos ou outros componentes não distribuídos por item.

O schema permanece na versão 15.


## V1.3.45 — BI Contas a Receber por vendedor e qualidade dos dados

A área `BI Gerencial > Contas a Receber` foi revisada para reduzir resultados enganosamente zerados e ampliar a identificação de cliente, NF-e e vendedor.

Principais mudanças:
- diferencia zero real de dado indisponível;
- novo indicador `Emitido no período`;
- painel de qualidade mostrando cobertura de cliente, vendedor, banco, carteira, emissão e vencimento;
- cliente dos títulos é resolvido por cadastro normalizado, ID/CNPJ/nome no título e vínculos exatos com NF-e/pedido;
- novo `Contas a receber por vendedor` com drill-down dos títulos;
- vendedor é obtido do título/detalhe, NF-e/pedido e, apenas como fallback operacional, do vendedor atual do cadastro do cliente;
- títulos em aberto passam a exibir cliente, vendedor e NF-e relacionada quando disponível;
- previsão futura mostra mensagem explícita quando não existem títulos a vencer, em vez de uma tabela de zeros sem contexto;
- o enriquecimento do Financeiro também passa a recuperar cliente e datas a partir do detalhe do título/NF/pedido, além de banco/carteira.

O schema permanece na versão 15.