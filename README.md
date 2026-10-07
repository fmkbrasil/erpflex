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
