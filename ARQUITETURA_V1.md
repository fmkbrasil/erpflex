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
