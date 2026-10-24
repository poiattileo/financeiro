# Financeiro com Pluggy

Aplicação independente baseada no projeto `poiattileo/financas-app` (revisão `f012506`). Mantém as telas e funções existentes (salário, fixos, parcelas, lançamentos, metas, registros, histórico, anotações, admin e bot Telegram opcional) e acrescenta a aba **Bancos** com Pluggy.

## O que já funciona

- Conectar uma instituição pelo Pluggy Connect.
- Importar contas e transações com `GET /v2/transactions` e paginação por cursor.
- Mostrar extrato, saldos sincronizados, saídas bancárias e compras de cartão separadamente.
- Categorizar movimentações e comparar saídas por categoria sem somar conta e cartão duas vezes.
- Cadastrar uma regra de destinatário para um gasto fixo. Quando um Pix/saída bancária efetivada corresponde ao destinatário, ao valor previsto e ao mês, ela é vinculada e o gasto aparece como pago.
- Vincular ou desvincular uma saída manualmente; isso permite registrar pagamentos parciais ou casos ambíguos.
- Receber eventos de atualização em fila durável e processá-los em serviço separado.
- Importar um `itemId` já criado no Dashboard pelo conector Meu Pluggy, para uso pessoal. Esses itens também são relidos diariamente como reserva caso não haja webhook público.
- Registrar saídas e entradas do Neon manualmente, mês a mês, na aba **Bancos**. Também aceita CSV ou OFX, caso você tenha esses formatos. O arquivo não é guardado; envios repetidos não duplicam movimentações. A conta manual aparece sem saldo e não é sincronizada pela Pluggy.

Os valores planejados originais **não são alterados** pela importação. Isso evita descontar duas vezes uma compra, uma transferência entre contas ou o pagamento de uma fatura. O painel Bancos mostra previsto, pago e restante para os gastos fixos. Saídas de conta e compras no cartão aparecem em totais separados. Um pagamento parcial é exibido como parcial, mas o reconhecimento automático só acontece quando o valor completa o previsto; parcelas parciais podem ser vinculadas manualmente.

## Requisitos

- VM Ubuntu 22.04 ou 24.04, com acesso `sudo`.
- Credenciais `CLIENT_ID` e `CLIENT_SECRET` de uma aplicação no [Dashboard Pluggy](https://dashboard.pluggy.ai/).
- Para contas reais e webhooks: domínio apontado para a VM e HTTPS válido.

**Não coloque credenciais Pluggy no frontend nem neste repositório.** O `CLIENT_SECRET` fica apenas em `/opt/financeiro/backend/.env` na VM. A Pluggy define as condições comerciais; para uso exclusivamente pessoal, confira a opção Meu Pluggy. A conexão com banco exige autorização do titular.

O repositório original contém um arquivo `backend/.env` versionado. Esta cópia não inclui esse arquivo e gera novos segredos na instalação. Se o `JWT_SECRET` publicado no original também estiver em uso na sua VM atual, troque-o lá e encerre as sessões antigas.

## Instalar em outra pasta na mesma VM

Copie esta pasta para a VM. Exemplo, no seu Mac:

```bash
scp -r /Users/leonardopoiatti/Projetos/financeiro USUARIO@IP_DA_VM:~/financeiro
```

Na VM, instale:

```bash
cd ~/financeiro
sudo bash instalar_vm.sh
```

O instalador usa `/opt/financeiro`, banco MariaDB `financeiro`, serviços `financeiro` e `financeiro-pluggy-worker`, e porta interna 8001. Ele não altera `/opt/financas`, o banco `financas` ou o serviço antigo. Sem domínio, o Nginx escuta **apenas em `127.0.0.1:8081`** para testes. Acesse com túnel SSH:

```bash
ssh -L 8081:127.0.0.1:8081 USUARIO@IP_DA_VM
```

Abra `http://127.0.0.1:8081` no navegador. O instalador pede usuário e senha; o primeiro usuário vira administrador. Não há senha padrão.

## Caminho gratuito para suas próprias contas

1. Crie uma conta em [Meu Pluggy](https://meu.pluggy.ai/) e conecte ali seus bancos.
2. Crie uma conta no [Dashboard Pluggy](https://dashboard.pluggy.ai/) e uma aplicação demo. Em **Aplicações**, copie `Client ID` e `Client Secret` (o secret aparece uma única vez).
3. Na demo da aplicação, conecte **Meu Pluggy** (conector 200), autorize cada banco e copie o **Item ID** mostrado para a conexão. Escolher o banco diretamente no Dashboard é outro fluxo e pode pedir aprovação de produção.
4. Na VM, rode `sudo bash configurar_pluggy_vm.sh` na pasta clonada. O script pede o Client Secret sem exibi-lo, valida as credenciais e reinicia os serviços. Se o Client ID ainda estiver vazio, também o pedirá. Nunca envie o secret por chat nem o salve no GitHub.
5. Abra este app como administrador, vá à aba **Bancos** e use **Importar item** para cada Item ID. O app lerá contas e transações e fará novas leituras diariamente. Sem domínio, acesse pelo túnel SSH descrito acima.

Esse uso pessoal é descrito pela [Pluggy como gratuito](https://meu.pluggy.ai/api-guide). O fluxo comercial com usuários de terceiros tem condições diferentes.

## Neon sem conexão automática

Na aba **Bancos**, escolha o mês no topo e preencha data, descrição, valor e se a movimentação é saída ou entrada. A [Neon informa que envia o extrato oficial em PDF](https://neon.com.br/aprenda/economizar-dinheiro/o-que-extrato-bancario/); esse PDF ainda não é importado diretamente. Se você tiver um CSV com colunas `Data`, `Descrição` e `Valor` (ou `Débito`/`Crédito`), ou OFX com transações `STMTTRN`, pode usar **Importar arquivo**. Valores negativos representam saídas. Revise os lançamentos e associe pagamentos a gastos fixos quando necessário. Reenvios do mesmo arquivo não duplicam as transações; o arquivo é descartado após a leitura. O app não deduz saldo atual a partir de lançamentos ou arquivos manuais.

## Ativar HTTPS e conexão direta no app

Quando tiver um domínio com DNS apontado para a VM, rode novamente a instalação com as variáveis abaixo:

```bash
cd ~/financeiro
sudo APP_DOMAIN=financeiro.seudominio.com LETSENCRYPT_EMAIL=seu-email@exemplo.com bash instalar_vm.sh
```

O instalador cria o host Nginx na porta 80, solicita certificado Let's Encrypt pelo Certbot e configura a URL do webhook. É necessário que as portas 80 e 443 estejam acessíveis e o DNS já esteja correto.

Edite `/opt/financeiro/backend/.env` e preencha:

```dotenv
PLUGGY_CLIENT_ID=seu_client_id
PLUGGY_CLIENT_SECRET=seu_client_secret
PLUGGY_SANDBOX=true
```

Depois:

```bash
sudo systemctl restart financeiro financeiro-pluggy-worker
sudo bash /opt/financeiro/backend/configurar_webhooks.sh
```

Na aba Bancos, clique em **Conectar banco**. Com `PLUGGY_SANDBOX=true`, o widget também mostra conectores de teste. O conector Pluggy Bank aceita usuário `user-ok`, senha `password-ok` e MFA `123456` quando solicitado. Para uso real, altere `PLUGGY_SANDBOX=false`, reinicie o serviço e use uma conexão permitida pelo seu plano.

Se quiser usar o bot Telegram, preencha `TELEGRAM_BOT_TOKEN` no `.env` e execute:

```bash
sudo systemctl enable --now financeiro-bot
```

## Atualizar e operar

Depois de editar os arquivos no projeto local e copiá-los novamente para a VM:

```bash
cd ~/financeiro
sudo bash atualizar_vm.sh
```

Esse script faz backup do banco, copia código e frontend, aplica esquemas idempotentes e reinicia os serviços. Logs:

```bash
sudo journalctl -u financeiro -f
sudo journalctl -u financeiro-pluggy-worker -f
```

## Notas de conciliação

- O vínculo automático exige conta do tipo `BANK`, transação `DEBIT` e status `POSTED`.
- A regra usa nome do destinatário ou CPF/CNPJ (quando esses dados vierem do conector), mês e valor previsto. Se houver mais de uma regra candidata, o app não decide sozinho.
- O botão **Vincular** permite associar saídas que a Pluggy não identificou pelo nome, inclusive pagamentos parciais. **Desvincular** impede que a próxima sincronização vincule a mesma transação novamente.
- A Pluggy pode alterar ou recriar IDs de transação e a disponibilidade de dados varia por instituição. Revise os vínculos após reconexões e trate o extrato importado como dado sincronizado, não como saldo em tempo real.
- O widget depende do script oficial hospedado em `cdn.pluggy.ai`.
