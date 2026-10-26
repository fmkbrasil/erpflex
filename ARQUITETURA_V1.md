# Arquitetura V1.3.2 — núcleo central + Roteirizador integrado

```text
                   ERPFlex API                 NF Stock API
                  somente leitura             somente leitura
                         │                           │
                         └──────────┬────────────────┘
                                    ▼
                    ┌───────────────────────────┐
                    │ NÚCLEO CENTRAL            │
                    │ FastAPI / Python          │
                    │                           │
                    │ Autenticação              │
                    │ Perfis / permissões       │
                    │ Empresa / filial          │
                    │ Sincronizações            │
                    │ Regras compartilhadas    │
                    │ API interna /api/v1       │
                    │ Auditoria                 │
                    └─────────────┬─────────────┘
                                  │
                                  ▼
                    ┌───────────────────────────┐
                    │ PostgreSQL / SQLite       │
                    │ BASE OPERACIONAL ÚNICA    │
                    │                           │
                    │ SalesInvoice = NF central │
                    └─────────────┬─────────────┘
                                  │
                     ┌────────────┼────────────┐
                     ▼            ▼            ▼
                  Entrega      Financeiro    Compras
                     │
            ┌────────┼───────────────┐
            ▼        ▼               ▼
        Região   Responsável       Caixa
                     │
                     ▼
                   Roteiro
                     │
           ┌─────────┼──────────┐
           ▼         ▼          ▼
         Baixa   Ocorrência   Custo/KM
                     │
                     ▼
             Painéis / Relatórios
```

## Princípios

1. Uma NF de saída existe uma única vez em `sales_invoices`.
2. O módulo de logística não duplica dados fiscais: ele relaciona `deliveries` à NF central.
3. ERPFlex é a origem preferencial dos dados comerciais/fiscais.
4. NF Stock é a origem fiscal das NF-e recebidas/XML.
5. Região, responsável, roteiro, status, ocorrência, caixa, KM, custo e regras internas pertencem à Plataforma.
6. Excel é contingência, não uma segunda fonte operacional permanente.
7. Quando Excel e ERPFlex representam a mesma NF, a sincronização deve convergir para um único registro.
8. Apps/interfaces não acessam PostgreSQL diretamente; usam os serviços/regras/API do núcleo.
9. Autorização acontece no backend.
10. `raw_records` preserva fonte, ID externo, hash e JSON original para rastreabilidade.
11. Empresa é a partição transacional atual; filial continua sendo contexto/autorização até validarmos o identificador real nas APIs.

## Entidades logísticas

A NF central (`SalesInvoice`) relaciona-se com:

- `Delivery`: estado logístico da NF;
- `DeliveryAssignment`: responsável atual;
- `DeliveryRule`: memória por cliente/CNPJ;
- `Responsible`: motorista interno, externo, transportadora ou COLETA;
- `Region`: regiões/zona;
- `BoxType`: tipo de caixa;
- `Route`: roteiro;
- `RouteStop`: ordem da entrega dentro do roteiro;
- `RouteCost`: frete/KM do roteiro;
- `DeliveryMovement`: histórico de alterações;
- `DeliveryOccurrence`: ocorrências;
- `LogisticsImport`: histórico de importações XLSX/CSV de contingência.

## Entrada de dados logísticos

### ERPFlex

O sincronizador grava/atualiza `SalesInvoice` e assegura a existência de `Delivery`. Regras logísticas são aplicadas apenas onde não existe decisão operacional já consolidada.

### Excel de contingência

O importador lê XLSX/CSV, agrupa linhas por NF, grava `RawRecord(source=EXCEL)`, atualiza a NF central e aplica as mesmas regras logísticas. Não cria banco/tabela paralela.

### Convergência

Quando o ERPFlex sincroniza uma NF criada anteriormente pelo Excel:

1. procura pelo ID ERPFlex;
2. procura pela chave de acesso;
3. sem chave, reutiliza a NF se houver exatamente um registro com o mesmo número na empresa;
4. substitui o identificador temporário `EXCEL:...` pelo ID real do ERPFlex.

## Telas operacionais

- `/roteirizar`
- `/roteiros`
- `/painel-entregas`
- `/baixa-entregas`
- `/ocorrencias`
- `/responsaveis`
- `/regras-entrega`
- `/parametros-logistica`
- `/custos-roteiro`
- `/painel-gerencial`
- `/relatorios-gerenciais`
- `/importar-logistica`

Previsão/Entrega não funciona como filtro escondido nem como seletor nas telas operacionais. Os Relatórios Gerenciais podem usar período explicitamente porque são uma consulta histórica.

## Próximas extensões sobre a mesma arquitetura

- Google Routes/otimização;
- geocodificação/cache de coordenadas;
- frota/manutenção/documentos;
- Portal Motorista PWA;
- fotos de ocorrência;
- API interna específica do motorista;
- dashboards avançados cruzando venda, logística, cobrança e compras.


## V1.3.1 — navegação por módulo

A navegação foi reorganizada para tratar Logística como um módulo único da plataforma. O menu principal aponta para `/logistica/inicio`; as telas internas permanecem nas rotas existentes e recebem navegação secundária por Operação, Gestão e Configuração.

## V1.3.3 — UX operacional do Roteirizador

O fluxo da tela de roteirização foi reorganizado para refletir a rotina operacional: **Zona -> NFs -> Motorista -> Gravar -> continuar**. A zona é o filtro visual principal. Motoristas são apresentados somente em modal após a seleção das notas. A gravação cria o `Route`, os `RouteStop` e o `DeliveryAssignment`, mantendo a regra de que somente entregas efetivamente roteirizadas podem seguir para a Baixa.


## V1.3.6 — persistência e configurações de integração

- `integration_configs`: configuração por empresa para ERPFlex/NF Stock. Segredos ficam criptografados.
- `schema_state`: controla a versão estrutural do banco; alvo atual = 2.
- a sincronização recebe a configuração da empresa atual a partir do banco, com `.env` como fallback.
- SQLite local usa diretório persistente independente da versão quando `DATA_DIR` não foi configurado; no primeiro boot, se o destino estiver vazio, pode copiar a base mais recente de uma versão irmã anterior.
- backup automático do SQLite é criado antes de mudança de schema; backup manual também pode ser disparado pela interface.
- local/SQLite: a chave das credenciais fica em `DATA_DIR/integration.key`; Railway/PostgreSQL: use `INTEGRATION_SECRET_KEY` ou uma `SECRET_KEY` estável.

## V1.3.7 — camada de sincronização e cadastros

A sincronização ERPFlex foi organizada em dois grupos: cadastros mestres e movimentações. Um lote selecionado gera uma execução (`sync_runs`) por módulo e roda sequencialmente sob o mesmo lock. O payload original continua em `raw_records`, enquanto as entidades consolidadas alimentam `products`, `partners`, `sales_orders`, `sales_invoices`, `financial_titles` e `purchase_invoices`.

A preferência de apresentação dos cadastros é armazenada em `user_view_preferences` (schema 3), sem alterar ou duplicar os dados de negócio. O detalhe cadastral usa os campos consolidados e, quando disponível, o payload original para exibir a informação completa da origem.


## Motor ERPFlex V1.3.9

A camada de conectores usa como referência operacional o ERPFlex Analytics V7.8. `sync_cursors` persiste os equivalentes a `LastValid`, `FirstInvalid` e `NextOffset` por empresa/módulo. Esses cursores pertencem à integração e não substituem os registros de negócio armazenados na base central.


## V1.3.12 — Estratégia de sincronização recente
- Faturamento: resolve a borda atual e varre regressivamente até ultrapassar a data inicial do período.
- Extração de envelopes JSON gerais: maior array de objetos, equivalente ao extractBestArray do Analytics V7.8.
- Compras: varredura regressiva por offset e enriquecimento opcional via `/api/compra/confirmado/{id}`.
- O raw JSON permanece a fonte integral para campos ainda não materializados em colunas do banco.
- O espelho de impressão de Compra/NF-e é uma representação local e não substitui o DANFE fiscal oficial.


## V1.3.13 — Cursor validado do Faturamento
O cursor `last_valid` passa a representar exclusivamente uma página comprovada por resposta HTTP 200 com registros. Timeouts e páginas vazias são tratados como candidatos inválidos durante a recuperação e não são persistidos como última página válida.


## V1.3.14 — Âncora V7.8 para Faturamento
Quando um cursor mais novo não responde, a plataforma não faz uma sequência de timeouts regressivos. Ela retorna diretamente à página P15983, âncora inicial comprovada pelo Analytics V7.8, valida essa página e avança sequencialmente até a borda atual. Somente páginas HTTP 200 com registros podem ser persistidas como `last_valid`.


## V1.3.16 — Motor ERPFlex V7.8 nativo em Go

A camada HTTP do ERPFlex foi desacoplada do cliente Python. Por padrão, `ERPFlexClient._get()` envia a requisição por stdin ao bridge Go empacotado com a aplicação. O bridge replica o `http.Transport`, Basic Auth, Accept e `extractBestArray` do ERPFlex Analytics V7.8 fornecido como referência. A persistência, normalização e regras de negócio permanecem em Python. No Docker o bridge é recompilado em estágio Go; no Windows há executável x64 incluído.


### Observabilidade de sincronização — V1.3.16
`sync_progress` mantém percentual, fase, contador, heartbeat e instante da última mudança de etapa por execução. A tabela é separada de `sync_runs` para preservar o histórico e permitir evolução aditiva do schema.

## V1.3.17 — Faturamento regressivo pela última página válida

O Faturamento por período passa a usar a rotina operacional padrão:
1. validar a última página válida conhecida;
2. recuperar a partir da âncora P15983 caso o cursor não responda;
3. avançar até a primeira página inválida para confirmar a borda atual;
4. ler da última página válida para trás;
5. interromper quando uma página inteira estiver anterior à data inicial.

Para períodos com mais de 120 dias de distância da emissão mais recente, ou quando a varredura regressiva atingir o limite configurado, o algoritmo histórico por busca binária do Analytics V7.8 é usado como fallback. O schema permanece 5.

### V1.3.18 — camada de consulta de documentos
As telas documentais reutilizam `UserViewPreference` para salvar colunas e quantidade por página, sem nova tabela. Os filtros rápidos são aplicados sobre datas normalizadas da base central. O detalhe de NF-e de saída usa `sales_invoices` + `raw_records`; Compras usa `purchase_invoices` + `purchase_items` + `raw_records`. O Financeiro tenta relacionar o número do título à NF de saída/entrada para oferecer navegação e impressão do espelho DANFE.

### V1.3.19 — apresentação de documentos
- Faturamento e Compras exibem resumo de produtos/quantidade de itens nas listagens.
- Faturamento usa itens do payload da NF e, quando ausentes, tenta reutilizar os itens do pedido relacionado.
- Financeiro mantém os campos normalizados, mas a camada de apresentação também consulta o payload bruto para recuperar cliente/fornecedor, emissão, banco e carteira sem exigir nova sincronização.
- A estrutura do banco permanece no schema 5.


### V1.3.20 — Financeiro enriquecido / DANFE
A consulta Título a Receber V1 é usada de forma controlada para enriquecer id_banco/id_carteira. O detalhe é preservado como raw_record separado. O espelho DANFE continua sendo uma apresentação interna dos dados sincronizados, sem substituir o documento fiscal oficial.


### V1.3.21 — Itens dos documentos
Corrige chaves de relacionamento de pedidos/faturamentos/compras e mantém compatibilidade com raw_records históricos. O schema permanece 5.

## V1.3.35 — BI comercial e classificação de produtos

O schema 12 adiciona somente `products.subcategory`. Categoria e subcategoria pertencem ao cadastro normalizado de produtos e podem ser complementadas pelos `raw_records` do módulo `products` sem criar nova fonte de verdade.

No BI de Compras, o valor gerencial de uma NF-e usa a primeira fonte não zerada entre `total_erpflex`, `total_nfstock` e a soma de `purchase_items.total`. Essa regra é restrita à apresentação/indicadores do BI e evita que um cabeçalho zerado elimine o valor real já disponível nos itens.

## V1.3.36 — vendedor do pedido, carteira descritiva e UX de roteirização

O schema 13 adiciona `sales_orders.seller_id` e `sales_orders.seller_name`. O sincronizador apenas materializa campos realmente presentes no JSON de pedido; não existe endpoint novo de vendedor nesta versão.

No BI, a resolução do vendedor segue: vendedor presente no faturamento/pedido detalhado → pedido sincronizado por ID exato → pedido sincronizado por número exato e não ambíguo. Não existe fallback por cliente, evitando atribuição histórica incorreta quando a carteira comercial muda.

A descrição da carteira financeira é resolvida na camada de leitura a partir de `receber_detalhe`, `boleto_options`, `banks` e `bank_detail` já persistidos. O ID original permanece a referência quando nenhuma descrição foi retornada.

A tela `/roteirizar` mantém as mesmas rotas e regras de negócio, alterando apenas a camada de interação: seleção visual por cards, resumo das NFs marcadas, volumes/valor selecionados, etapas explícitas e maior destaque para endereço, região e motorista.


## V1.3.37 — segurança de período no roteirizador

A roteirização passou a trabalhar com uma única **data operacional** por tela. A referência prioriza `SalesInvoice.forecast_date` (previsão/entrega); quando ela não existe, utiliza `SalesInvoice.issue_date` de forma explicitamente sinalizada. NFs sem ambas as datas ficam em uma visão separada **Sem data**.

O filtro é aplicado no backend antes de montar regiões, KPIs e lista de seleção. No POST de criação do roteiro, o servidor revalida todas as NFs e rejeita qualquer tentativa de misturar datas, mesmo em formulário manipulado. `Route.route_date` continua independente da data operacional da NF.

## V1.3.38 — endereço efetivo e UX de baixa

A logística passa a usar um conceito de **endereço efetivo de entrega** sem criar nova tabela. A resolução é feita em tempo de leitura com a prioridade: campos de entrega da NF (`sales_invoices`) → endereço de entrega do cliente (`partners.delivery_*`) → endereço principal do cliente (`partners.address*`). A interface identifica a origem para facilitar auditoria e não cria valores quando nenhuma fonte possui endereço.

A normalização das chaves da API foi fortalecida somente na comparação de nomes de campos: caixa, acentos, espaços, hífens e sublinhados deixam de impedir o reconhecimento de uma chave. Os valores recebidos permanecem inalterados. O faturamento também consolida número e complemento quando o payload entrega os componentes do endereço no mesmo nível.

A tela de baixa continua usando as mesmas regras de negócio e endpoints existentes. A alteração é de apresentação/operabilidade: status possuem códigos visuais distintos, há contadores por situação e o formulário individual adapta a ação selecionada. O schema permanece 13.

## V1.3.39 — camada de DRE Gerencial

A DRE foi implementada dentro da camada de BI (`app/bi_service.py`) para reutilizar as mesmas regras de Faturamento, filtros globais e normalização financeira.

Regras estruturais:
- Receita Bruta: somente `sales_invoices` válidas conforme a regra fiscal do BI.
- Despesas: somente `financial_titles.kind = DESPESA`.
- `PAGAR` não é somado à DRE, evitando duplicidade quando o mesmo compromisso já estiver representado como despesa.
- A categoria da despesa é extraída do JSON bruto real com parser tolerante; nenhuma categoria contábil é inventada.
- O usuário classifica a categoria de origem em um grupo gerencial na tabela `dre_expense_mappings`.
- Compras não são tratadas como CMV.
- Linhas dependentes de dados não normalizados permanecem indisponíveis.

Schema 14: nova tabela aditiva `dre_expense_mappings(company_id, source_category, dre_group, updated_at)`.


## V1.3.40 — camada de apresentação configurável e acesso

O schema 15 adiciona duas tabelas exclusivamente de apresentação: `ui_theme_settings` (uma configuração de tema por empresa) e `ui_label_overrides` (substituições de legendas por empresa). Nenhuma delas altera os dados operacionais. O layout base injeta as cores em variáveis CSS e aplica as legendas no navegador somente aos elementos de interface elegíveis.

A permissão `bi.view` separa a consulta do BI da permissão da tela inicial. A permissão `ui.customize` protege a tela de Aparência e o modo de edição rápida de legendas. Perfis legados que tinham `dashboard.view` recebem `bi.view` durante a migração para preservar o comportamento anterior.

A paginação introduzida na camada de interface limita a quantidade de linhas visíveis por página (25/50/100) nas tabelas padrão. Tabelas operacionais com seleção em lote, como roteirização e baixa, não são paginadas automaticamente para evitar alteração do fluxo operacional.

No smartphone, o BI usa navegação compacta por seletor, grids responsivos de KPIs/filtros e rolagem controlada de tabelas detalhadas.


## V1.3.41 — Qualidade de cadastros e BI
- Cadastro de clientes aceita estruturas documentadas `dadosComerciais` / `dadosResidenciais` e aliases SA1_*.
- Fornecedor aceita o bloco documentado `endereco`/`cnpjcpf` quando presente no payload de compra e pode ser reenriquecido também pelo `enderEmit` do XML NF-e de entrada já obtido pelo NFStock.
- Catálogo financeiro resolve banco por SA6_ID/SA6_Desc em todos os registros persistidos e mantém fallback apenas para IDs previamente confirmados na conta real.
- BI diferencia zero calculado de indicador indisponível por ausência de data de referência.
- Hierarquias de BI: vendedor → categoria e cliente → categoria.
- Schema: 15, sem alteração estrutural.

## Complemento V1.3.42 — relações comerciais e fornecedores

- `Partner` continua sendo o cadastro central de clientes/fornecedores.
- Fornecedores podem ser enriquecidos por uma segunda família de endpoints oficiais do ERPFlex (API REST v1.x com token de sessão), sem substituir o conector V7.8 já comprovado.
- Cliente × Produto é derivado de `SalesInvoice -> SalesInvoiceItem`.
- Fornecedor × Produto é derivado de `PurchaseInvoice -> PurchaseItem`.
- O detalhe de Produto oferece a navegação inversa para clientes e fornecedores.
- Relações históricas só existem quando há documentos/itens sincronizados; não são inferidas por similaridade textual.

## Complemento V1.3.43 — ERPFlex Cadastros v1 isolado

A conexão principal do ERPFlex continua responsável pelos endpoints `/api` e `/api_v2` e pelo motor V7.8. A API REST de cadastros v1.x passa a ser uma conexão lógica independente, com URL, ativação e credenciais próprias em `integration_configs` usando `provider=ERPFLEX_V1`.

A senha da v1 pode herdar da integração principal quando não houver credencial específica, mas a **URL v1 nunca é derivada da URL principal**. Isso evita enviar `/v1.2/auth` para o host do motor V7.8 por engano.

Fluxo de fornecedores:

`Worker -> módulo fornecedores -> ERPFlexClient._v1_token() -> /v1.2/auth -> token -> /v1.5/fornecedor?lastid=... -> Partner(role_supplier)`

A tela Integrações possui teste isolado da conexão v1. O histórico de sincronização exibe `SyncRun.message` em um detalhe expansível para diagnosticar HTTP, autenticação, JSON ou paginação sem registrar senha ou token.

## Complemento V1.3.45 — Dashboard de Faturamento

O serviço `bi_service.build_bi` passou a gerar `sales_dashboard`, uma camada de apresentação derivada exclusivamente de `sales_invoices`, `sales_invoice_items`, `partners`, `products`, `sales_orders` e registros brutos já persistidos.

Princípios:
- total fiscal: `SalesInvoice.total` de NF-e válida;
- produto/categoria/subcategoria: `SalesInvoiceItem.total` e `qty`;
- cliente/UF/cidade: relacionamento da NF-e com `Partner`;
- vendedor: resolução já existente por JSON fiscal/pedido exato;
- drill-down: IDs locais da NF-e apontam para `/faturamento/{id}`;
- comparação: não altera persistência; somente recalcula períodos no backend;
- nenhuma tabela ou coluna nova foi criada.

## Complemento V1.3.46 — BI de Contas a Pagar

O BI de Contas a Pagar foi separado em três leituras gerenciais:

1. **Previsto x realizado**
   - previsto: títulos PAGAR com vencimento no período selecionado;
   - realizado: baixas PAGAR com data de pagamento no período;
   - desvio: realizado menos previsto;
   - percentual de execução: realizado dividido pelo previsto.

2. **Vencidos e atraso**
   - posição atual da carteira em aberto;
   - aging de atraso;
   - quantidade de títulos vencidos;
   - fornecedores com maior saldo vencido;
   - pagamentos realizados em atraso dentro do período.

3. **Fluxo de caixa de curto prazo**
   - horizontes acumulados de 7, 15 e 30 dias;
   - pagamentos previstos a partir dos títulos em aberto;
   - recebimentos conhecidos a partir do contas a receber;
   - saldo líquido projetado e índice de cobertura;
   - sem alegar saldo bancário real.

Nenhuma nova tabela foi criada. O schema continua 15.


## Complemento V1.3.47 — Drill-down transversal do BI

O BI adota um padrão comum de drill-down em todas as áreas. Agrupamentos analíticos carregam referências aos documentos reais que os compõem. O frontend usa blocos expansíveis e links para os detalhes existentes de NF-e, título financeiro, compra e entrega. A Visão Geral utiliza drill-through para as áreas especializadas, enquanto as demais áreas expandem os próprios agrupamentos. O schema permanece 15.


## Complemento V1.3.48 — Interface comercial e BI mobile

- A camada de apresentação mantém personalizações em `ui_theme_settings` e `ui_label_overrides`.
- `app/static/ui.js` centraliza aplicação de textos comerciais, edição visual, paginação client-side e navegação interna do BI no mobile.
- O menu principal foi dividido entre áreas de negócio e submenu Sistema.
- A navegação interna do BI é progressiva no smartphone e não altera a renderização desktop.
- Nenhuma regra de negócio, integração ou schema foi modificada nesta versão.

### Interface BI V1.3.51
A camada de apresentação do BI usa navegação progressiva por resultados. O formulário principal expõe apenas o período e o intervalo de datas. Filtros contextuais eventualmente recebidos por drill-through podem permanecer como campos ocultos até a limpeza da consulta. A seleção de resultados é feita no cliente por JavaScript, sem nova consulta ao banco ao alternar entre blocos já carregados.


### Interface BI V1.3.52

A camada mobile do BI usa um seletor visual de módulos e uma navegação interna por cards de resultado. A implementação é cliente-side em `ui.js`/`style.css` e não altera contratos de dados nem schema.


### Interface BI V1.3.53
A camada de apresentação do BI passa a usar shell responsivo: sidebar por área no desktop, seletor de módulo no mobile, cards de análise responsivos e KPIs em grade. Não altera modelos, sincronizadores nem schema.
